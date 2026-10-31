"""What Flux deletes when the merge lands, and what it takes with it."""

from __future__ import annotations

from collections import Counter

from flux_preflight.context import ClusterCheck
from flux_preflight.inventory import Deletion, takes_along
from flux_preflight.model import FLUX_HELM, FLUX_KUSTOMIZE, FLUX_SOURCE, Finding, Ref, Severity, dig

SOURCE_KINDS = ("GitRepository", "OCIRepository", "HelmRepository", "Bucket", "HelmChart")
PROTECTED = {
    ("", "Namespace"),
    ("apiextensions.k8s.io", "CustomResourceDefinition"),
    ("", "PersistentVolumeClaim"),
    ("", "PersistentVolume"),
    ("apps", "StatefulSet"),
    ("storage.k8s.io", "StorageClass"),
    (FLUX_HELM, "HelmRelease"),
    (FLUX_KUSTOMIZE, "Kustomization"),
    *((FLUX_SOURCE, kind) for kind in SOURCE_KINDS),
}
FLUX_NAMESPACE = "flux-system"


def protected(ref: Ref) -> bool:
    return (ref.group, ref.kind) in PROTECTED or ref.namespace == FLUX_NAMESPACE


def _counts(refs: list[Ref]) -> str:
    counts = Counter(ref.kind for ref in refs)
    return ", ".join(f"{n} {kind}" for kind, n in counts.most_common())


def _unknowable(cc: ClusterCheck, ks: Ref) -> str | None:
    """Why what a deleted Kustomization takes along cannot be counted; None when it can."""
    if not takes_along(cc.base.kustomizations.get(ks, {})):
        return None
    if ks in cc.base.foreign:
        return cc.base.foreign[ks]
    if ks in cc.base.errors:
        return "it did not build before this change"
    return None


def _why(cc: ClusterCheck, item: Deletion) -> str:
    ref, doc = item.ref, item.doc
    if ref == cc.base.root:
        return (
            "the root Kustomization no longer builds itself, so Flux deletes it, and with it "
            "everything in this cluster that Flux applied"
        )
    if ref.is_flux_kustomization:
        unknowable = _unknowable(cc, ref)
        if unknowable:
            return (
                "Flux deletes it and everything it applied. How much that is cannot be known "
                f"here: {unknowable}"
            )
        if ref in cc.base.foreign:
            return "Flux deletes it; what it applied stays in the cluster, and nothing manages it"
        taken = [d.ref for d in cc.outcome.taken_by(ref)]
        named = sorted(str(r) for r in taken if protected(r) and not r.is_flux_kustomization)
        noun = "object" if len(taken) == 1 else "objects"
        said = f"Flux deletes everything it applied: {len(taken)} {noun} ({_counts(taken)})"
        if named:
            said += ". Among them: " + ", ".join(named[:15])
            if len(named) > 15:
                said += f" and {len(named) - 15} more"
        uncounted = sorted(str(r) for r in taken if _unknowable(cc, r))
        if uncounted:
            said += (
                ". Not counted, and gone too: everything applied by "
                + ", ".join(uncounted)
                + ", which cannot be known here"
            )
        return said if taken else "Flux deletes it; it applied nothing that would go with it"
    if ref.kind == "Namespace" and ref.group == "":
        inside = [r for r in cc.base_objects if r.namespace == ref.name]
        return (
            f"Kubernetes deletes everything in it: {len(inside)} objects from Git"
            + (f" ({_counts(inside)})" if inside else "")
            + ", and all that was created in it outside Git: pods, StatefulSet volumes, "
            "Argo CD's applications"
        )
    if ref.kind == "CustomResourceDefinition":
        kind = str(dig(doc, "spec.names.kind") or ref.name)
        group = str(dig(doc, "spec.group") or "")
        mine = [r for r in cc.base_objects if r.kind == kind and r.group == group]
        return (
            f"every {kind} in the cluster is deleted with it: {len(mine)} from Git, and any "
            "that a chart or an operator made"
        )
    if ref.kind == "HelmRelease":
        return "Flux uninstalls the release: everything the chart installed goes (CRDs stay)"
    if ref.kind == "PersistentVolumeClaim":
        return "its volume goes too, and the data with it, unless the StorageClass retains it"
    if ref.kind == "PersistentVolume":
        return "the volume is released; with reclaimPolicy Delete its data is gone"
    if ref.kind == "StatefulSet":
        return "its pods stop; their volumes stay, but nothing uses them"
    if ref.kind == "StorageClass":
        return "volumes cannot be made from it any more"
    if ref.group == FLUX_SOURCE:
        return "whatever reads it stops getting updates and turns not ready"
    if ref.namespace == FLUX_NAMESPACE:
        return "this is part of Flux itself: Flux may stop applying anything"
    return "Flux deletes it"


def check(cc: ClusterCheck) -> list[Finding]:
    found: list[Finding] = []
    outcome = cc.outcome
    for item in outcome.deletions:
        if item.via is not None:
            continue  # told under the Kustomization that takes it along
        if protected(item.ref):
            found.append(cc.finding("GC101", Severity.BLOCK, item.ref, _why(cc, item) + "."))
        else:
            found.append(cc.finding("GC102", Severity.WARN, item.ref, _why(cc, item) + "."))
    found.extend(_mass(cc))
    for move in outcome.moves:
        if protected(move.ref):
            found.append(
                cc.finding(
                    "GC104",
                    Severity.BLOCK,
                    move.ref,
                    f"moves from {move.old_owner} to {move.new_owner}. If {move.old_owner} "
                    f"reconciles first, Flux deletes it and {move.new_owner} makes it again: "
                    f"for a {move.ref.kind} that is an outage. Mark it prune: disabled for "
                    "the move, or move it in two merges.",
                )
            )
        else:
            found.append(
                cc.finding(
                    "GC104",
                    Severity.INFO,
                    move.ref,
                    f"moves from {move.old_owner} to {move.new_owner}; it may be deleted and "
                    "made again in between",
                )
            )
    for kept in outcome.kept:
        if kept.ref in cc.head_objects:
            continue
        stays = (
            "it goes on applying its last spec from the cluster"
            if kept.ref.is_flux_kustomization
            else "delete it by hand if it should go"
        )
        found.append(
            cc.finding(
                "GC105",
                Severity.WARN if "prune: disabled" in kept.reason else Severity.INFO,
                kept.ref,
                f"is no longer in Git, but stays in the cluster because {kept.reason}: {stays}",
            )
        )
    return found


def _mass(cc: ClusterCheck) -> list[Finding]:
    found: list[Finding] = []
    every = [item.ref for item in cc.outcome.deletions]
    if len(every) > cc.config.max_deletions:
        kept_kinds = [r for r in every if protected(r) and not r.is_flux_kustomization]
        found.append(
            cc.finding(
                "GC103",
                Severity.BLOCK,
                "cluster",
                f"in all, this merge makes Flux delete {len(every)} objects in this cluster "
                f"({_counts(every)}); {len(kept_kinds)} of them are Namespaces, CRDs, volumes, "
                "Helm releases or Flux sources",
            )
        )
    direct: dict[Ref, list[Ref]] = {}
    for item in cc.outcome.deletions:
        if item.via is None and item.ref != cc.base.root:
            direct.setdefault(item.owner, []).append(item.ref)
    limit, ratio = cc.config.max_deletions, cc.config.max_deletion_ratio
    least = cc.config.min_objects_for_ratio
    for ks, refs in sorted(direct.items()):
        size = len(cc.base.inventories.get(ks, {}))
        share = len(refs) / size if size else 0.0
        if len(refs) > limit or (size >= least and share > ratio):
            found.append(
                cc.finding(
                    "GC103",
                    Severity.BLOCK,
                    ks,
                    f"Flux deletes {len(refs)} of the {size} objects it applies "
                    f"({share:.0%}): {_counts(refs)}. The limit is {limit} objects, or "
                    f"{ratio:.0%} of a Kustomization with at least {least} objects.",
                )
            )
    return found
