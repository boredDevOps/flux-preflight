"""Which clusters the repository deploys: each has a root Kustomization Flux bootstrapped."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from flux_preflight.config import Config
from flux_preflight.model import FLUX_SOURCE, Doc, Ref, dig
from flux_preflight.render import source_of
from flux_preflight.yamlfiles import YamlProblem, ignored, load_all

SYNC_FILE = "gotk-sync.yaml"


@dataclass
class Root:
    name: str
    sync: str
    kustomization: Doc
    # The branch the cluster deploys from; None when it follows a tag, a semver range or a commit.
    branch: str | None

    @property
    def path(self) -> str:
        return str(dig(self.kustomization, "spec.path") or "").strip("./").strip("/")


class RootError(Exception):
    pass


def sync_files(tree: Path, config: Config) -> list[str]:
    if config.clusters:
        return [entry.sync for entry in config.clusters]
    found = []
    for file in sorted(tree.rglob(SYNC_FILE)):
        rel = file.relative_to(tree).as_posix()
        if not ignored(rel, config.ignore):
            found.append(rel)
    return found


def read_root(tree: Path, sync: str, name: str | None) -> Root | None:
    """The root in `sync`, or None when the file is not there on this side."""
    file = tree / sync
    if not file.is_file():
        return None
    try:
        docs = load_all(file.read_text(encoding="utf-8"))
    except YamlProblem as exc:
        raise RootError(f"{sync} does not parse: {exc}") from exc
    roots = [d for d in docs if Ref.of(d).is_flux_kustomization]
    if len(roots) != 1:
        raise RootError(f"{sync} should hold one Flux Kustomization, it holds {len(roots)}")
    root = roots[0]
    source = source_of(root)
    branch: str | None = None
    for doc in docs:
        ref = Ref.of(doc)
        if (ref.group, ref.kind, ref.name) == (FLUX_SOURCE, source.kind, source.name):
            ref_spec = dig(doc, "spec.ref") or {}
            if set(ref_spec) <= {"branch"}:
                branch = ref_spec.get("branch")
    path = str(dig(root, "spec.path") or "").strip("./").strip("/")
    return Root(name or path or sync, sync, root, branch)
