"""Builds a cluster the way Flux does: its root Kustomization, then each one that builds out.

`flux build kustomization --dry-run` applies the Flux Kustomization's own patches, target
namespace and substitutions, and generates a kustomization.yaml where a path has none, as the
controller does. It never talks to a cluster: KUBECONFIG points at nothing.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from flux_preflight.model import Doc, Ref, Render, dig
from flux_preflight.yamlfiles import YamlProblem, load_all


class BuildError(Exception):
    pass


@dataclass(frozen=True)
class Source:
    kind: str
    namespace: str
    name: str

    def __str__(self) -> str:
        return f"{self.kind}/{self.namespace}/{self.name}"


def source_of(ks: Doc) -> Source:
    ref = dig(ks, "spec.sourceRef") or {}
    namespace = ref.get("namespace") or (ks.get("metadata") or {}).get("namespace") or ""
    return Source(str(ref.get("kind", "")), str(namespace), str(ref.get("name", "")))


def build(flux: str, tree: Path, ks: Doc, seconds: int) -> list[Doc]:
    meta = ks.get("metadata") or {}
    rel = str(dig(ks, "spec.path") or "./")
    path = (tree / rel.lstrip("/")).resolve()
    if not path.is_relative_to(tree.resolve()):
        raise BuildError(f"spec.path {rel} leads out of the repository")
    if not path.is_dir():
        raise BuildError(f"spec.path {rel} does not exist")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as file:
        yaml.safe_dump(ks, file)
    env = {**os.environ, "KUBECONFIG": os.devnull}
    try:
        done = subprocess.run(
            [
                flux,
                "build",
                "kustomization",
                str(meta.get("name", "")),
                "--namespace",
                str(meta.get("namespace") or "flux-system"),
                "--path",
                str(path),
                "--kustomization-file",
                file.name,
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=seconds,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(
            f"flux build took longer than {seconds} s (timeouts.flux_build_seconds)"
        ) from exc
    finally:
        os.unlink(file.name)
    if done.returncode:
        said = "\n".join(line.strip("✗ ").strip() for line in done.stderr.splitlines()[-5:])
        raise BuildError(said or f"flux build exited {done.returncode}")
    try:
        return load_all(done.stdout)
    except YamlProblem as exc:
        raise BuildError(f"flux build printed YAML that does not parse: {exc}") from exc


def render_cluster(flux: str, tree: Path, cluster: str, root: Doc, seconds: int) -> Render:
    """Every Kustomization the root leads to that reads this repository, with what it applies."""
    root_ref = Ref.of(root)
    source = source_of(root)
    render = Render(cluster=cluster, root=root_ref)
    queue: list[tuple[Ref, Doc, Ref | None]] = [(root_ref, root, None)]
    while queue:
        ref, ks, parent = queue.pop(0)
        if ref in render.kustomizations:
            continue
        render.kustomizations[ref] = ks
        render.parent[ref] = parent
        if source_of(ks) != source:
            render.foreign[ref] = f"it reads {source_of(ks)}, not this repository"
            continue
        try:
            docs = build(flux, tree, ks, seconds)
        except BuildError as exc:
            render.errors[ref] = str(exc)
            continue
        inventory: dict[Ref, Doc] = {}
        for doc in docs:
            obj = Ref.of(doc)
            inventory[obj] = doc
            if obj.is_flux_kustomization and obj != ref:
                queue.append((obj, doc, ref))
        render.inventories[ref] = inventory
    return render
