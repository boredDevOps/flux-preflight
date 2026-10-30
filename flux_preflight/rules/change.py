"""Changes to objects that stay: the ones Kubernetes refuses, and the ones that replace a thing."""

from __future__ import annotations

import re

from flux_preflight.context import ClusterCheck
from flux_preflight.model import FLUX_HELM, FLUX_SOURCE, Doc, Finding, Ref, Severity, canonical, dig
from flux_preflight.render import source_of

IMMUTABLE: dict[tuple[str, str], tuple[str, ...]] = {
    ("apps", "Deployment"): ("spec.selector",),
    ("apps", "DaemonSet"): ("spec.selector",),
    ("apps", "ReplicaSet"): ("spec.selector",),
    ("apps", "StatefulSet"): (
        "spec.selector",
        "spec.serviceName",
        "spec.volumeClaimTemplates",
        "spec.podManagementPolicy",
    ),
    ("batch", "Job"): ("spec.selector", "spec.template", "spec.completionMode"),
    ("", "PersistentVolumeClaim"): (
        "spec.storageClassName",
        "spec.accessModes",
        "spec.volumeName",
        "spec.volumeMode",
        "spec.selector",
    ),
    ("storage.k8s.io", "StorageClass"): (
        "provisioner",
        "parameters",
        "reclaimPolicy",
        "volumeBindingMode",
    ),
    ("rbac.authorization.k8s.io", "RoleBinding"): ("roleRef",),
    ("rbac.authorization.k8s.io", "ClusterRoleBinding"): ("roleRef",),
    ("apiextensions.k8s.io", "CustomResourceDefinition"): (
        "spec.scope",
        "spec.group",
        "spec.names.kind",
    ),
}

KS_FIELDS = (
    "path",
    "prune",
    "sourceRef",
    "targetNamespace",
    "namePrefix",
    "nameSuffix",
    "patches",
    "components",
    "postBuild",
    "force",
    "dependsOn",
    "suspend",
    "wait",
    "timeout",
    "decryption",
    "serviceAccountName",
    "kubeConfig",
)

_UNITS = {
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "k": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "": 1,
}


# The fields of a Flux Kustomization that decide what it applies.
KS_CONTENT = (
    "path",
    "prune",
    "sourceRef",
    "targetNamespace",
    "namePrefix",
    "nameSuffix",
    "patches",
    "components",
    "postBuild",
)
# The fields of a source that do not change what it serves. Anything else may.
SOURCE_DELIVERY = ("interval", "timeout", "suspend", "secretRef", "proxySecretRef", "certSecretRef")
PRUNE_WARNING = "With prune on, what the new content no longer has is deleted."


def quantity(value: object) -> float | None:
    match = re.fullmatch(r"\s*([0-9.]+)\s*([KMGTP]i|[kMGTP])?\s*", str(value))
    if not match:
        return None
    return float(match.group(1)) * _UNITS[match.group(2) or ""]


def _same(a: object, b: object) -> bool:
    return canonical({"v": a}) == canonical({"v": b})


def check(cc: ClusterCheck) -> list[Finding]:
    found: list[Finding] = []
    for ref in sorted(cc.outcome.changed):
        old, new = cc.base_objects[ref], cc.head_objects[ref]
        found.extend(_immutable(cc, ref, old, new))
        if ref.group == FLUX_HELM and ref.kind == "HelmRelease":
            found.extend(_helm(cc, ref, old, new))
        if ref.is_flux_kustomization:
            found.extend(_kustomization(cc, ref, old, new))
        if ref.group == FLUX_SOURCE:
            found.extend(_foreign_source(cc, ref, old, new))
        found.extend(_switched_off(cc, ref, old, new))
    return found


def _owner_forces(cc: ClusterCheck, ref: Ref) -> bool:
    for ks in cc.head.owners().get(ref, []):
        if dig(cc.head.kustomizations.get(ks, {}), "spec.force") is True:
            return True
    return False


def _immutable(cc: ClusterCheck, ref: Ref, old: Doc, new: Doc) -> list[Finding]:
    changed = [
        path
        for path in IMMUTABLE.get((ref.group, ref.kind), ())
        if not _same(dig(old, path), dig(new, path))
    ]
    if ref.kind == "Service" and ref.group == "":
        before, after = dig(old, "spec.clusterIP"), dig(new, "spec.clusterIP")
        if before and after and before != after:
            changed.append("spec.clusterIP")
    if ref.kind in ("ConfigMap", "Secret") and ref.group == "" and old.get("immutable") is True:
        changed += [
            k for k in ("data", "binaryData", "stringData") if not _same(old.get(k), new.get(k))
        ]
    if ref.kind == "PersistentVolumeClaim" and ref.group == "":
        before = quantity(dig(old, "spec.resources.requests.storage"))
        after = quantity(dig(new, "spec.resources.requests.storage"))
        if before and after and after < before:
            changed.append("spec.resources.requests.storage (a volume cannot shrink)")
    found: list[Finding] = []
    if changed:
        fields = ", ".join(changed)
        if _owner_forces(cc, ref):
            said = (
                f"changes {fields}, which Kubernetes does not let change. Its Kustomization has "
                f"force: true, so Flux deletes the {ref.kind} and makes it again"
            )
        else:
            said = (
                f"changes {fields}, which Kubernetes does not let change: the apply fails and "
                "Flux stops applying everything in this Kustomization until it is reverted"
            )
        found.append(cc.finding("GC201", Severity.BLOCK, ref, said + "."))
    if ref.kind == "CustomResourceDefinition":
        found.extend(_crd_versions(cc, ref, old, new))
    return found


def _crd_versions(cc: ClusterCheck, ref: Ref, old: Doc, new: Doc) -> list[Finding]:
    def versions(doc: Doc) -> dict[str, Doc]:
        return {
            str(v.get("name")): v for v in dig(doc, "spec.versions") or [] if isinstance(v, dict)
        }

    before, after = versions(old), versions(new)
    gone = sorted(set(before) - set(after))
    unserved = sorted(
        name
        for name in set(before) & set(after)
        if before[name].get("served") is not False and after[name].get("served") is False
    )
    found: list[Finding] = []
    if gone:
        found.append(
            cc.finding(
                "GC201",
                Severity.BLOCK,
                ref,
                f"drops version {', '.join(gone)}: objects stored as that version can no "
                "longer be read, and the API server refuses the change while any exist",
            )
        )
    if unserved:
        found.append(
            cc.finding(
                "GC201",
                Severity.WARN,
                ref,
                f"stops serving version {', '.join(unserved)}: whatever still asks for it fails",
            )
        )
    return found


def _release(doc: Doc) -> tuple[str, str, str]:
    meta = doc.get("metadata") or {}
    namespace = str(meta.get("namespace") or "")
    target = str(dig(doc, "spec.targetNamespace") or "")
    name = str(
        dig(doc, "spec.releaseName")
        or (f"{target}-{meta.get('name')}" if target else meta.get("name"))
    )
    storage = str(dig(doc, "spec.storageNamespace") or target or namespace)
    return name, target or namespace, storage


def _major(version: object) -> int | None:
    match = re.match(r"^[v=]?(\d+)\.", str(version or ""))
    return int(match.group(1)) if match else None


def _helm(cc: ClusterCheck, ref: Ref, old: Doc, new: Doc) -> list[Finding]:
    found: list[Finding] = []
    before, after = _release(old), _release(new)
    if before != after:
        found.append(
            cc.finding(
                "GC202",
                Severity.BLOCK,
                ref,
                f"becomes release {after[0]} in {after[1]} (was {before[0]} in {before[1]}): "
                "Helm uninstalls the old release and installs a new one, with an outage in "
                "between and new volumes",
            )
        )
    chart_old = dig(old, "spec.chart.spec") or {}
    chart_new = dig(new, "spec.chart.spec") or {}
    if (
        chart_old.get("chart") != chart_new.get("chart")
        or not _same(chart_old.get("sourceRef"), chart_new.get("sourceRef"))
        or not _same(dig(old, "spec.chartRef"), dig(new, "spec.chartRef"))
    ):
        found.append(
            cc.finding(
                "GC203",
                Severity.WARN,
                ref,
                "installs a different chart or from a different source: what it deploys may "
                "change completely",
            )
        )
    major_old, major_new = _major(chart_old.get("version")), _major(chart_new.get("version"))
    if major_old is not None and major_new is not None and major_new > major_old:
        found.append(
            cc.finding(
                "GC203",
                Severity.WARN,
                ref,
                f"moves the chart from {chart_old.get('version')} to {chart_new.get('version')}, "
                "a new major version: read its upgrade notes",
            )
        )
    return found


def _kustomization(cc: ClusterCheck, ref: Ref, old: Doc, new: Doc) -> list[Finding]:
    changed = [
        name for name in KS_FIELDS if not _same(dig(old, f"spec.{name}"), dig(new, f"spec.{name}"))
    ]
    found: list[Finding] = []
    why = cc.head.foreign.get(ref) or cc.base.foreign.get(ref)
    unseen = [name for name in changed if name in KS_CONTENT] if why else []
    if unseen:
        found.append(
            cc.finding(
                "GC206",
                Severity.WARN,
                ref,
                f"changes {', '.join(unseen)}. What it applies after this cannot be seen here: "
                f"{why}. {PRUNE_WARNING}",
            )
        )
    seen = [name for name in changed if name not in unseen]
    if seen:
        found.append(
            cc.finding(
                "GC205",
                Severity.INFO,
                ref,
                f"changes {', '.join(seen)}; what that does is in the findings above",
            )
        )
    return found


def _foreign_source(cc: ClusterCheck, ref: Ref, old: Doc, new: Doc) -> list[Finding]:
    """A source moved by this merge: the Kustomizations that read it apply something else now."""
    before, after = old.get("spec") or {}, new.get("spec") or {}
    changed = sorted(
        name
        for name in set(before) | set(after)
        if name not in SOURCE_DELIVERY and not _same(before.get(name), after.get(name))
    )
    readers = []
    for ks in sorted(cc.head.foreign):
        source = source_of(cc.head.kustomizations[ks])
        if (source.kind, source.namespace, source.name) == (ref.kind, ref.namespace, ref.name):
            readers.append(str(ks))
    if not changed or not readers:
        return []
    return [
        cc.finding(
            "GC206",
            Severity.WARN,
            ref,
            f"changes {', '.join(changed)}, and {', '.join(readers)} applies what it serves. "
            f"What that is after this cannot be seen here: read the other repository's changes. "
            f"{PRUNE_WARNING}",
        )
    ]


def _switched_off(cc: ClusterCheck, ref: Ref, old: Doc, new: Doc) -> list[Finding]:
    found: list[Finding] = []
    if dig(new, "spec.suspend") is True and dig(old, "spec.suspend") is not True:
        found.append(
            cc.finding(
                "GC204",
                Severity.WARN,
                ref,
                "is suspended: Flux stops applying it, so later changes do not arrive",
            )
        )
    replicas_old, replicas_new = dig(old, "spec.replicas"), dig(new, "spec.replicas")
    if replicas_new == 0 and replicas_old != 0 and ref.kind in ("Deployment", "StatefulSet"):
        found.append(cc.finding("GC204", Severity.WARN, ref, "is scaled to 0 replicas"))
    return found
