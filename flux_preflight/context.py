"""What the rules read: both renders of a cluster, what the merge does, and the files."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import PurePosixPath

from flux_preflight.config import Config
from flux_preflight.inventory import Outcome
from flux_preflight.model import Doc, Finding, Ref, Render, Severity, dig
from flux_preflight.yamlfiles import RawDoc, YamlProblem, normalize


@dataclass
class Files:
    """The repository's YAML on both sides, indexed to point a finding at its line."""

    base: list[RawDoc]
    head: list[RawDoc]
    broken_head: dict[str, YamlProblem]
    changed: set[str]
    _index: dict[tuple[str, str], tuple[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for side in (self.base, self.head):
            for item in side:
                meta = item.doc.get("metadata") or {}
                key = (str(item.doc.get("kind")), str(meta.get("name")))
                self._index[key] = (item.file, item.line)

    def locate(self, ref: Ref) -> tuple[str | None, int | None]:
        """The file that defines it (head first); names may be patched, so this is best effort."""
        found = self._index.get((ref.kind, ref.name))
        return found if found else (None, None)


@dataclass
class ClusterCheck:
    name: str
    root_path: str
    base: Render
    head: Render
    outcome: Outcome
    files: Files
    config: Config

    @property
    def touched(self) -> set[Ref]:
        """Objects this merge request adds or changes: the ones it is answerable for."""
        return set(self.outcome.added) | set(self.outcome.changed)

    @cached_property
    def paths(self) -> set[str]:
        """The repository directories this cluster's Kustomizations build from."""
        found = set()
        for ks, doc in self.head.kustomizations.items():
            if ks not in self.head.foreign:
                path = str(dig(doc, "spec.path") or "")
                found.add(normalize(PurePosixPath(path)))
        return found

    def deploys(self, file: str) -> bool:
        return any(not p or file.startswith(p + "/") for p in self.paths)

    @cached_property
    def head_objects(self) -> dict[Ref, Doc]:
        return self.head.objects()

    @cached_property
    def base_objects(self) -> dict[Ref, Doc]:
        return self.base.objects()

    def finding(
        self, rule: str, severity: Severity, ref: Ref | str, message: str, line: int | None = None
    ) -> Finding:
        path, found_line = self.files.locate(ref) if isinstance(ref, Ref) else (None, None)
        return Finding(rule, severity, self.name, str(ref), message, path, line or found_line)
