"""What should not be in Git, or not like this: plain-text secrets, floating versions, old APIs.

The merge request answers for what it adds or changes; what was already there is listed once,
as information, so it can be cleaned up without blocking unrelated work.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from typing import Any

from flux_preflight.context import ClusterCheck, Files
from flux_preflight.model import FLUX_HELM, Doc, Finding, Ref, Severity, dig
from flux_preflight.yamlfiles import RawDoc

# ---- plain-text Secrets and password-like values (files, so the whole repository) ----

_PLACEHOLDER = re.compile(r"^\s*(\$\{[^}]*\}|\$\([^)]*\)|\{\{.*\}\}|<[^>]*>)?\s*$")


def _plain_secret(doc: Doc) -> list[str] | None:
    """The keys of a Secret written in plain text, None if it is not one (or SOPS-encrypted)."""
    if doc.get("kind") != "Secret" or str(doc.get("apiVersion")) != "v1" or "sops" in doc:
        return None
    keys: list[str] = []
    for field in ("data", "stringData"):
        for key, value in (doc.get(field) or {}).items():
            if value is not None and not _PLACEHOLDER.match(str(value)):
                keys.append(str(key))
    return keys or None


_SECRET_KEYS = (
    "password",
    "passwd",
    "token",
    "apikey",
    "accesskey",
    "secretkey",
    "clientsecret",
    "privatekey",
    "webhookurl",
)


def _secretish(key: str) -> bool:
    flat = re.sub(r"[_\-.]", "", key).lower()
    return flat.endswith(_SECRET_KEYS) and not flat.startswith("existing")


def _literals(doc: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """(key path, value) of every literal string under a password-like key."""
    if isinstance(doc, dict):
        name, value = doc.get("name"), doc.get("value")
        if isinstance(name, str) and isinstance(value, str) and _secretish(name):
            yield f"{path}[{name}].value", value
        for key, item in doc.items():
            sub = f"{path}.{key}" if path else str(key)
            if isinstance(item, str) and _secretish(str(key)):
                yield sub, item
            else:
                yield from _literals(item, sub)
    elif isinstance(doc, list):
        for index, item in enumerate(doc):
            yield from _literals(item, f"{path}[{index}]")


def _looks_real(value: str, min_length: int) -> bool:
    return (
        len(value) >= min_length
        and not _PLACEHOLDER.match(value)
        and not value.startswith(("ref+", "vault:", "arn:", "awssm:", "secretsmanager:"))
    )


def _fingerprints(raw: list[RawDoc], min_length: int) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for item in raw:
        if item.doc.get("kind") == "Secret" or "sops" in item.doc:
            continue
        for path, value in _literals(item.doc):
            if _looks_real(value, min_length):
                digest = hashlib.sha256(value.encode()).hexdigest()
                found.add((item.file, f"{Ref.of(item.doc)}:{path}", digest))
    return found


def check_files(files: Files, min_secret_length: int) -> list[Finding]:
    found: list[Finding] = []
    existing: list[str] = []
    for item in files.head:
        keys = _plain_secret(item.doc)
        if keys is None:
            continue
        ref = Ref.of(item.doc)
        if item.file in files.changed:
            found.append(
                Finding(
                    "GC301",
                    Severity.BLOCK,
                    "",
                    f"{item.file}:{ref}",
                    f"{ref} in {item.file} holds {len(keys)} value(s) in plain text "
                    f"({', '.join(sorted(keys)[:5])}). Anyone who can read the repository "
                    "has them: encrypt it with SOPS or use an ExternalSecret, and rotate them.",
                    item.file,
                    item.line,
                )
            )
        else:
            existing.append(f"{item.file} ({ref.name})")
    if existing:
        found.append(
            Finding(
                "GC301",
                Severity.INFO,
                "",
                "existing",
                f"{len(existing)} Secret(s) already in Git in plain text: "
                + "; ".join(existing[:20])
                + (f" and {len(existing) - 20} more" if len(existing) > 20 else ""),
            )
        )
    new = _fingerprints(files.head, min_secret_length) - _fingerprints(
        files.base, min_secret_length
    )
    lines = {(i.file, f"{Ref.of(i.doc)}"): i.line for i in files.head}
    for file, where, _ in sorted(new):
        name, _, path = where.partition(":")
        found.append(
            Finding(
                "GC302",
                Severity.WARN,
                "",
                f"{file}:{where}",
                f"{path} of {name} in {file} looks like a password or token written in Git. "
                "If it is one, take it from a Secret instead (valuesFrom, secretKeyRef).",
                file,
                lines.get((file, name)),
            )
        )
    return found


# ---- floating versions and old APIs (rendered objects, per cluster) ----

_POD_SPECS = {
    "Pod": "spec",
    "Deployment": "spec.template.spec",
    "StatefulSet": "spec.template.spec",
    "DaemonSet": "spec.template.spec",
    "ReplicaSet": "spec.template.spec",
    "Job": "spec.template.spec",
    "CronJob": "spec.jobTemplate.spec.template.spec",
}


def _images(ref: Ref, doc: Doc) -> set[str]:
    spec = dig(doc, _POD_SPECS[ref.kind]) if ref.kind in _POD_SPECS else None
    if not isinstance(spec, dict):
        return set()
    found: set[str] = set()
    for key in ("containers", "initContainers"):
        for container in spec.get(key) or []:
            if isinstance(container, dict) and container.get("image"):
                found.add(str(container["image"]))
    return found


def unpinned(image: str) -> bool:
    if "@" in image:
        return False
    tail = image.rsplit("/", 1)[-1]
    return ":" not in tail or tail.endswith(":latest")


def _latest_in_values(values: Any, path: str = "values") -> Iterator[str]:
    if isinstance(values, dict):
        for key, item in values.items():
            sub = f"{path}.{key}"
            if key == "tag" and str(item).strip() == "latest":
                yield sub
            elif key == "image" and isinstance(item, str) and unpinned(item) and ":" in item:
                yield sub
            else:
                yield from _latest_in_values(item, sub)
    elif isinstance(values, list):
        for index, item in enumerate(values):
            yield from _latest_in_values(item, f"{path}[{index}]")


_PINNED = re.compile(r"^[v=]?\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")

DEPRECATED: dict[str, str] = {
    "extensions/v1beta1": "removed in Kubernetes 1.22",
    "apps/v1beta1": "removed in Kubernetes 1.16",
    "apps/v1beta2": "removed in Kubernetes 1.16",
    "networking.k8s.io/v1beta1": "removed in Kubernetes 1.22",
    "rbac.authorization.k8s.io/v1beta1": "removed in Kubernetes 1.22",
    "apiextensions.k8s.io/v1beta1": "removed in Kubernetes 1.22",
    "admissionregistration.k8s.io/v1beta1": "removed in Kubernetes 1.22",
    "storage.k8s.io/v1beta1": "removed in Kubernetes 1.22–1.27",
    "policy/v1beta1": "removed in Kubernetes 1.25",
    "batch/v1beta1": "removed in Kubernetes 1.25",
    "discovery.k8s.io/v1beta1": "removed in Kubernetes 1.25",
    "node.k8s.io/v1beta1": "removed in Kubernetes 1.25",
    "autoscaling/v2beta1": "removed in Kubernetes 1.25",
    "autoscaling/v2beta2": "removed in Kubernetes 1.26",
    "flowcontrol.apiserver.k8s.io/v1beta1": "removed in Kubernetes 1.26",
    "flowcontrol.apiserver.k8s.io/v1beta2": "removed in Kubernetes 1.29",
    "flowcontrol.apiserver.k8s.io/v1beta3": "removed in Kubernetes 1.32",
    "kustomize.toolkit.fluxcd.io/v1beta1": "deprecated by Flux; newer Flux stops serving it",
    "kustomize.toolkit.fluxcd.io/v1beta2": "deprecated by Flux; newer Flux stops serving it",
    "helm.toolkit.fluxcd.io/v2beta1": "deprecated by Flux; newer Flux stops serving it",
    "helm.toolkit.fluxcd.io/v2beta2": "deprecated by Flux; newer Flux stops serving it",
    "source.toolkit.fluxcd.io/v1beta1": "deprecated by Flux; newer Flux stops serving it",
    "source.toolkit.fluxcd.io/v1beta2": "deprecated by Flux for Git, Helm and Bucket sources",
    "notification.toolkit.fluxcd.io/v1beta1": "deprecated by Flux; newer Flux stops serving it",
    "notification.toolkit.fluxcd.io/v1beta2": "deprecated by Flux; newer Flux stops serving it",
}


def check(cc: ClusterCheck) -> list[Finding]:
    found: list[Finding] = []
    old_apis: list[str] = []
    for ref, doc in sorted(cc.head_objects.items()):
        api = str(doc.get("apiVersion"))
        if api in DEPRECATED:
            if ref in cc.touched:
                found.append(
                    cc.finding("GC305", Severity.WARN, ref, f"uses {api}: {DEPRECATED[api]}")
                )
            else:
                old_apis.append(f"{ref} ({api})")
        if ref not in cc.touched:
            continue
        before = cc.base_objects.get(ref)
        for image in sorted(_images(ref, doc) - (_images(ref, before) if before else set())):
            if unpinned(image):
                found.append(
                    cc.finding(
                        "GC303",
                        Severity.WARN,
                        ref,
                        f"runs {image} without a pinned version: each new pod may run "
                        "something else",
                    )
                )
        if ref.group == FLUX_HELM and ref.kind == "HelmRelease":
            found.extend(_helm_versions(cc, ref, doc, before))
    if old_apis:
        found.append(
            Finding(
                "GC305",
                Severity.INFO,
                cc.name,
                "existing",
                f"{len(old_apis)} object(s) already use an old API: "
                + "; ".join(old_apis[:15])
                + (f" and {len(old_apis) - 15} more" if len(old_apis) > 15 else ""),
            )
        )
    return found


def _helm_versions(cc: ClusterCheck, ref: Ref, doc: Doc, before: Doc | None) -> list[Finding]:
    found: list[Finding] = []
    version = dig(doc, "spec.chart.spec.version")
    old_version = dig(before, "spec.chart.spec.version") if before else None
    if dig(doc, "spec.chart") and (before is None or version != old_version):
        if not version or not _PINNED.match(str(version).strip()):
            said = f"version {version!r} is a range" if version else "no version"
            found.append(
                cc.finding(
                    "GC304",
                    Severity.WARN,
                    ref,
                    f"installs its chart with {said}: Flux upgrades it on its own when a "
                    "new one is published",
                )
            )
    old_latest = set(_latest_in_values(dig(before, "spec.values"))) if before else set()
    for path in sorted(set(_latest_in_values(dig(doc, "spec.values"))) - old_latest):
        found.append(
            cc.finding("GC303", Severity.WARN, ref, f"sets {path} to an image without a version")
        )
    return found
