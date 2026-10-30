"""Reading the repository's YAML strictly: a duplicate key is an error, not last-one-wins."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from yaml.constructor import ConstructorError

from flux_preflight.model import Doc

YAML_SUFFIXES = (".yaml", ".yml")
KUSTOMIZATION_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
KUSTOMIZE_CONFIG = "kustomize.config.k8s.io"


class StrictLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: dict[Any, int] = {}
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            try:
                key = self.construct_object(key_node, deep=True)
                hash(key)
            except (ConstructorError, TypeError):
                continue
            if key in seen:
                raise ConstructorError(
                    None,
                    None,
                    f"duplicate key {key!r} (first at line {seen[key]}): YAML keeps the last one",
                    key_node.start_mark,
                )
            seen[key] = key_node.start_mark.line + 1
        return super().construct_mapping(node, deep)


# YAML 1.1's `=` (the "value" key type) appears in generated CRDs; Kubernetes reads it as "=".
StrictLoader.add_constructor(
    "tag:yaml.org,2002:value",
    lambda loader, node: loader.construct_scalar(node),  # type: ignore[arg-type]
)


class YamlProblem(Exception):
    def __init__(self, message: str, line: int | None) -> None:
        super().__init__(message)
        self.line = line


def iter_docs(text: str) -> Iterator[tuple[int, Any]]:
    """(line where the document starts, the document) for each document; YamlProblem if broken."""
    loader = StrictLoader(text)
    try:
        while loader.check_node():
            node = loader.get_node()
            if node is None:
                continue
            yield node.start_mark.line + 1, loader.construct_document(node)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise YamlProblem(str(exc), mark.line + 1 if mark else None) from exc
    finally:
        loader.dispose()


def load_all(text: str) -> list[Doc]:
    return [doc for _, doc in iter_docs(text) if isinstance(doc, dict)]


def is_yaml(path: str) -> bool:
    return path.endswith(YAML_SUFFIXES)


@dataclass
class RawDoc:
    file: str
    line: int
    doc: Doc


def ignored(path: str, ignore: list[str]) -> bool:
    return any(
        path == item.rstrip("/") or path.startswith(item.rstrip("/") + "/") for item in ignore
    )


def raw_docs(tree: Path, ignore: list[str]) -> tuple[list[RawDoc], dict[str, YamlProblem]]:
    """Every Kubernetes-looking document in the tree, and the files that do not parse."""
    found: list[RawDoc] = []
    broken: dict[str, YamlProblem] = {}
    for file in sorted(tree.rglob("*")):
        rel = file.relative_to(tree).as_posix()
        if (
            not file.is_file()
            or not is_yaml(rel)
            or ignored(rel, ignore)
            or rel.startswith(".git/")
        ):
            continue
        try:
            text = file.read_text(encoding="utf-8")
            for line, doc in iter_docs(text):
                if isinstance(doc, dict) and file.name in KUSTOMIZATION_FILES:
                    # kustomize lets a kustomization.yaml leave out its apiVersion and kind.
                    doc = {
                        "apiVersion": f"{KUSTOMIZE_CONFIG}/v1beta1",
                        "kind": "Kustomization",
                        **doc,
                    }
                if isinstance(doc, dict) and doc.get("apiVersion") and doc.get("kind"):
                    found.append(RawDoc(rel, line, doc))
        except YamlProblem as exc:
            broken[rel] = exc
        except UnicodeDecodeError:
            broken[rel] = YamlProblem("not UTF-8 text", None)
    return found, broken


# Fields of a kustomization.yaml that name files which are not resources to apply.
_NOT_RESOURCES = ("patchesStrategicMerge", "configurations", "crds", "transformers", "generators")


def referenced_non_resources(raw: list[RawDoc]) -> set[str]:
    """Files a kustomization.yaml uses as patches or inputs, so they are not applied as they are."""
    used: set[str] = set()
    for item in raw:
        doc = item.doc
        if not str(doc.get("apiVersion", "")).startswith(KUSTOMIZE_CONFIG):
            continue
        base = PurePosixPath(item.file).parent
        names: list[str] = []
        for key in _NOT_RESOURCES:
            names.extend(str(v) for v in doc.get(key) or [] if isinstance(v, str))
        for key in ("patches", "patchesJson6902", "replacements"):
            for entry in doc.get(key) or []:
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    names.append(entry["path"])
        for key in ("configMapGenerator", "secretGenerator"):
            for entry in doc.get(key) or []:
                for spec in (entry or {}).get("files") or []:
                    names.append(str(spec).split("=", 1)[-1])
        for chart in doc.get("helmCharts") or []:
            if isinstance(chart, dict) and isinstance(chart.get("valuesFile"), str):
                names.append(chart["valuesFile"])
        for name in names:
            used.add(normalize(base / name))
    return used


def normalize(path: PurePosixPath) -> str:
    parts: list[str] = []
    for part in path.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    return "/".join(parts)
