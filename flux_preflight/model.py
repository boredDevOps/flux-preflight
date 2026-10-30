"""What the check reasons about: objects as Flux would apply them, and what it found."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

Doc = dict[str, Any]

FLUX_KUSTOMIZE = "kustomize.toolkit.fluxcd.io"
FLUX_HELM = "helm.toolkit.fluxcd.io"
FLUX_SOURCE = "source.toolkit.fluxcd.io"
PRUNE_ANNOTATION = "kustomize.toolkit.fluxcd.io/prune"


@dataclass(frozen=True, order=True)
class Ref:
    """An object's identity in the cluster, without the API version: v1beta2 → v1 is no delete."""

    group: str
    kind: str
    namespace: str
    name: str

    @classmethod
    def of(cls, doc: Doc) -> Ref:
        api = str(doc.get("apiVersion") or "")
        meta = doc.get("metadata") or {}
        return cls(
            group=api.rpartition("/")[0],
            kind=str(doc.get("kind") or ""),
            namespace=str(meta.get("namespace") or ""),
            name=str(meta.get("name") or ""),
        )

    def __str__(self) -> str:
        if self.namespace:
            return f"{self.kind}/{self.namespace}/{self.name}"
        return f"{self.kind}/{self.name}"

    @property
    def is_flux_kustomization(self) -> bool:
        return self.group == FLUX_KUSTOMIZE and self.kind == "Kustomization"


def canonical(doc: Doc) -> str:
    return json.dumps(doc, sort_keys=True, default=str)


def dig(doc: Any, path: str) -> Any:
    """`spec.template.spec` → that value, or None anywhere along the way."""
    for part in path.split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(part)
    return doc


def prune_disabled(doc: Doc) -> bool:
    annotations = (doc.get("metadata") or {}).get("annotations") or {}
    return str(annotations.get(PRUNE_ANNOTATION, "")).lower() == "disabled"


class Severity(StrEnum):
    BLOCK = "block"
    WARN = "warn"
    INFO = "info"


@dataclass
class Finding:
    rule: str
    severity: Severity
    cluster: str
    subject: str
    message: str
    path: str | None = None
    line: int | None = None
    allowed: bool = False

    @property
    def key(self) -> str:
        """What a merge request writes to accept exactly this finding."""
        where = f"{self.cluster}:" if self.cluster else ""
        return f"{self.rule} {where}{self.subject}"


@dataclass
class Render:
    """One side (before or after the merge) of one cluster, as Flux would build it."""

    cluster: str
    root: Ref
    kustomizations: dict[Ref, Doc] = field(default_factory=dict)
    inventories: dict[Ref, dict[Ref, Doc]] = field(default_factory=dict)
    parent: dict[Ref, Ref | None] = field(default_factory=dict)
    errors: dict[Ref, str] = field(default_factory=dict)
    foreign: dict[Ref, str] = field(default_factory=dict)

    def objects(self) -> dict[Ref, Doc]:
        found: dict[Ref, Doc] = {}
        for inventory in self.inventories.values():
            found.update(inventory)
        return found

    def owners(self) -> dict[Ref, list[Ref]]:
        owners: dict[Ref, list[Ref]] = {}
        for ks, inventory in self.inventories.items():
            for ref in inventory:
                owners.setdefault(ref, []).append(ks)
        return owners

    def order(self) -> list[Ref]:
        """Kustomizations parent first, the way the cascade of a delete runs."""
        seen: list[Ref] = []
        queue = [self.root]
        while queue:
            ks = queue.pop(0)
            if ks in seen:
                continue
            seen.append(ks)
            queue.extend(sorted(k for k, p in self.parent.items() if p == ks and k not in seen))
        seen.extend(sorted(k for k in self.kustomizations if k not in seen))
        return seen
