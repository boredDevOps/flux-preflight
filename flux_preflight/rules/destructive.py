"""Settings that delete or replace something later: at a rollout, a failure or a scale-down.

Nothing goes when such a setting merges, so the deletion rules cannot see it. A merge request
answers for the settings it turns on; those already in Git are listed once.
"""

from __future__ import annotations

from typing import Any

from flux_preflight.context import ClusterCheck
from flux_preflight.model import (
    FLUX_HELM,
    FLUX_SOURCE,
    Doc,
    Finding,
    Ref,
    Render,
    Severity,
    dig,
    prune_disabled,
)
from flux_preflight.render import source_of
from flux_preflight.rules.change import KS_CONTENT, same, source_changes

CRD = "CustomResourceDefinition"
FORCE_MARKER = "kustomize.toolkit.fluxcd.io/force"
RECREATES = (
    "when an apply comes back as invalid, not only on an immutable field, Flux deletes the "
    "object and makes it again"
)
CRD_LOSS = "For a CustomResourceDefinition that deletes every object of its kind."
HELM_SETTINGS: tuple[tuple[str, Any, str], ...] = (
    (
        "upgrade.force",
        True,
        "Helm replaces resources at upgrade instead of patching them, which can mean downtime",
    ),
    (
        "rollback.force",
        True,
        "Helm replaces resources at rollback instead of patching them, which can mean downtime",
    ),
    ("rollback.recreate", True, "a rollback restarts the release's pods"),
    (
        "upgrade.remediation.strategy",
        "uninstall",
        "a failed upgrade uninstalls the release, and everything the chart installed goes",
    ),
    (
        "install.crds",
        "CreateReplace",
        "the chart replaces CustomResourceDefinitions that exist; what that changes cannot be "
        "seen here, because charts are not rendered",
    ),
    (
        "upgrade.crds",
        "CreateReplace",
        "the chart replaces CustomResourceDefinitions that exist; what that changes cannot be "
        "seen here, because charts are not rendered",
    ),
)


def _follows(source: Doc) -> str | None:
    """What a source moves along with, or None when it stays where it is pinned."""
    ref = dig(source, "spec.ref") or {}
    kind = source.get("kind")
    if kind == "GitRepository":
        if ref.get("commit") or ref.get("tag") or str(ref.get("name")).startswith("refs/tags/"):
            return None
        if ref.get("semver"):
            return f"the version range {ref['semver']}"
        return f"the branch {ref.get('branch') or ref.get('name') or 'master'}"
    if kind == "OCIRepository":
        if ref.get("digest") or (ref.get("tag") and ref["tag"] != "latest"):
            return None
        return f"the version range {ref['semver']}" if ref.get("semver") else "the tag latest"
    if kind == "Bucket":
        return "whatever is in the bucket"
    return None


def _kustomization(side: Render, ref: Ref, doc: Doc) -> dict[str, str]:
    found: dict[str, str] = {}
    if dig(doc, "spec.force") is True:
        crds = any(r.kind == CRD for r in side.inventories.get(ref, {}))
        said = f"has force: true: {RECREATES}."
        if crds:
            said += f" {CRD_LOSS}"
        elif ref in side.foreign:
            said += f" What it applies cannot be seen here. {CRD_LOSS}"
        found["force: true"] = f"{said} Flux's docs say to set it only while it is needed."
    policy = dig(doc, "spec.deletionPolicy")
    if policy in ("Delete", "WaitForTermination") and dig(doc, "spec.prune") is not True:
        found[f"deletionPolicy: {policy}"] = (
            f"has deletionPolicy: {policy}: despite prune: false, deleting this Kustomization "
            "deletes everything it applied"
        )
    if ref in side.foreign and dig(doc, "spec.prune") is True:
        source = source_of(doc)
        served = side.objects().get(Ref(FLUX_SOURCE, source.kind, source.namespace, source.name))
        follows = _follows(served) if served else None
        if follows:
            found["unpinned source"] = (
                f"reads {source}, which follows {follows}: when that moves, Flux applies the new "
                "content and deletes what it no longer has, with no merge request here to check "
                "it. Pin the source to a tag or a commit."
            )
    return found


def _settings(side: Render, ref: Ref, doc: Doc) -> dict[str, str]:
    """The settings of one object that can destroy something, by name, with what they do."""
    found: dict[str, str] = {}
    meta = doc.get("metadata") or {}
    marks = {**(meta.get("labels") or {}), **(meta.get("annotations") or {})}
    if str(marks.get(FORCE_MARKER, "")).lower() == "enabled":
        extra = f" {CRD_LOSS}" if ref.kind == CRD else ""
        found[f"{FORCE_MARKER}: enabled"] = f"is marked for force: {RECREATES}.{extra}"
    if ref.is_flux_kustomization:
        found |= _kustomization(side, ref, doc)
    if ref.group == FLUX_HELM and ref.kind == "HelmRelease":
        for path, value, consequence in HELM_SETTINGS:
            if dig(doc, f"spec.{path}") == value:
                found[f"{path}: {value}"] = f"has {path}: {value}: {consequence}"
    if ref.group == "apps" and ref.kind == "StatefulSet":
        for when in ("whenDeleted", "whenScaled"):
            if dig(doc, f"spec.persistentVolumeClaimRetentionPolicy.{when}") == "Delete":
                found[f"{when}: Delete"] = (
                    f"has persistentVolumeClaimRetentionPolicy.{when}: Delete: its volumes "
                    "go with its pods, and the data with them"
                )
    if ref.group == "" and ref.kind == "PersistentVolume":
        if dig(doc, "spec.persistentVolumeReclaimPolicy") == "Delete":
            found["reclaimPolicy: Delete"] = (
                "has persistentVolumeReclaimPolicy: Delete: when its claim goes, the data goes"
            )
    if ref.group == "apps" and ref.kind == "Deployment":
        if dig(doc, "spec.strategy.type") == "Recreate":
            found["strategy: Recreate"] = (
                "has strategy Recreate: each rollout stops all pods before it starts new ones"
            )
    return found


def _force_applies_now(cc: ClusterCheck, ks: Ref, old: Doc, new: Doc) -> bool:
    """This merge changes what a Kustomization with force applies: force is about to matter."""
    if any(r.kind == CRD and r in cc.touched for r in cc.head.inventories.get(ks, {})):
        return True
    if ks not in cc.head.foreign:
        return False
    if any(not same(dig(old, f"spec.{n}"), dig(new, f"spec.{n}")) for n in KS_CONTENT):
        return True
    source = source_of(new)
    served = Ref(FLUX_SOURCE, source.kind, source.namespace, source.name)
    return served in cc.outcome.changed and bool(
        source_changes(cc.base_objects[served], cc.head_objects[served])
    )


def _changes(cc: ClusterCheck, ref: Ref, old: Doc, new: Doc) -> list[Finding]:
    found: list[Finding] = []
    if prune_disabled(old) and not prune_disabled(new):
        found.append(
            cc.finding(
                "GC207",
                Severity.WARN,
                ref,
                "is no longer marked prune: disabled: the next time it leaves Git, Flux deletes it",
            )
        )
    before, after = dig(old, "spec.replicas"), dig(new, "spec.replicas")
    if (
        ref.group == "apps"
        and ref.kind == "StatefulSet"
        and isinstance(before, int)
        and isinstance(after, int)
        and 0 < after < before
    ):
        found.append(
            cc.finding(
                "GC207",
                Severity.WARN,
                ref,
                f"goes from {before} to {after} replicas: the pods with the highest numbers stop, "
                "and what only they held is out of reach",
            )
        )
    return found


def check(cc: ClusterCheck) -> list[Finding]:
    found: list[Finding] = []
    existing: list[str] = []
    for ref, doc in sorted(cc.head_objects.items()):
        before = cc.base_objects.get(ref)
        had = _settings(cc.base, ref, before) if before else {}
        for name, said in _settings(cc.head, ref, doc).items():
            matters = (
                name == "force: true"
                and before is not None
                and _force_applies_now(cc, ref, before, doc)
            )
            if name not in had or matters:
                found.append(cc.finding("GC207", Severity.WARN, ref, said))
            else:
                existing.append(f"{ref} ({name})")
        if before and ref in cc.outcome.changed:
            found.extend(_changes(cc, ref, before, doc))
    if existing:
        found.append(
            cc.finding(
                "GC207",
                Severity.INFO,
                "existing",
                f"{len(existing)} setting(s) already in Git can delete or replace something: "
                + "; ".join(existing[:15])
                + (f" and {len(existing) - 15} more" if len(existing) > 15 else ""),
            )
        )
    return found
