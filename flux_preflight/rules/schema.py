"""Every rendered object against its schema, strictly: a misspelt field is an error, not ignored.

Kubernetes drops fields it does not know without a word, so `resource:` for `resources:` or a
value at the wrong depth deploys as if it were not written. Schemas come first from the CRDs the
cluster's own Git defines (the versions it really runs), then from kubeconform's locations.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from flux_preflight.context import ClusterCheck
from flux_preflight.model import Doc, Finding, Ref, Severity, dig

KUBECONFORM_SECONDS = 600
_NETWORK = ("dial tcp", "no such host", "timeout", "connection refused", "failed downloading")


def strict(schema: Any) -> Any:
    """A CRD's OpenAPI v3 schema as the JSON schema kubeconform reads, unknown fields refused."""
    if isinstance(schema, list):
        return [strict(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {key: strict(value) for key, value in schema.items()}
    if out.get("nullable") is True and isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
    if (
        "properties" in out
        and "additionalProperties" not in out
        and not out.get("x-kubernetes-preserve-unknown-fields")
    ):
        out["additionalProperties"] = False
    return out


def write_crd_schemas(objects: dict[Ref, Doc], dest: Path) -> int:
    written = 0
    for ref, doc in objects.items():
        if ref.kind != "CustomResourceDefinition":
            continue
        group = str(dig(doc, "spec.group") or "")
        kind = str(dig(doc, "spec.names.kind") or "").lower()
        for version in dig(doc, "spec.versions") or []:
            schema = dig(version, "schema.openAPIV3Schema")
            if not isinstance(schema, dict) or not kind:
                continue
            top = copy.deepcopy(schema)
            props = top.setdefault("properties", {})
            props.setdefault("apiVersion", {"type": "string"})
            props.setdefault("kind", {"type": "string"})
            props.setdefault("metadata", {"type": "object"})
            target = dest / group / f"{kind}_{version.get('name')}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(strict(top)), encoding="utf-8")
            written += 1
    return written


class Result:
    def __init__(self) -> None:
        self.invalid: dict[tuple[str, str, str], str] = {}
        self.skipped: Counter[str] = Counter()
        self.unreachable: list[str] = []
        self.failed: str | None = None


def run(
    kubeconform: str, objects: dict[Ref, Doc], work: Path, version: str, schemas: list[str]
) -> Result:
    result = Result()
    if not shutil.which(kubeconform):
        result.failed = f"{kubeconform} is not installed"
        return result
    work.mkdir(parents=True, exist_ok=True)
    local = work / "schemas"
    write_crd_schemas(objects, local)
    manifest = work / "objects.yaml"
    manifest.write_text(yaml.safe_dump_all(list(objects.values())), encoding="utf-8")
    cache = work.parent / "kubeconform-cache"
    cache.mkdir(exist_ok=True)
    locations = [
        str(local) + "/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
        *schemas,
    ]
    args = [kubeconform, "-strict", "-ignore-missing-schemas", "-output", "json", "-verbose"]
    args += ["-kubernetes-version", version, "-cache", str(cache)]
    for location in locations:
        args += ["-schema-location", location]
    try:
        done = subprocess.run(
            [*args, str(manifest)],
            capture_output=True,
            text=True,
            timeout=KUBECONFORM_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result.failed = f"kubeconform took longer than {KUBECONFORM_SECONDS} s"
        return result
    try:
        resources = json.loads(done.stdout or "{}").get("resources") or []
    except json.JSONDecodeError:
        result.failed = (done.stderr or done.stdout).strip()[:300] or "kubeconform printed nothing"
        return result
    for item in resources:
        status, kind, name = item.get("status"), str(item.get("kind")), str(item.get("name"))
        message = str(item.get("msg") or "")
        if status == "statusSkipped":
            result.skipped[f"{item.get('version')}/{kind}"] += 1
        elif status == "statusError" and any(word in message for word in _NETWORK):
            result.unreachable.append(message[:200])
        elif status in ("statusInvalid", "statusError"):
            details = [
                f"{e.get('path')}: {e.get('msg')}" for e in item.get("validationErrors") or []
            ]
            said = "; ".join(details) if details else message
            result.invalid[(kind, name, said)] = said[:400]
    return result


def check(cc: ClusterCheck, kubeconform: str, work: Path) -> list[Finding]:
    config = cc.config
    if not config.schema_check:
        return [Finding("GC309", Severity.INFO, cc.name, "schemas", "the schema check is off")]
    head = run(
        kubeconform, cc.head_objects, work / "head", config.kubernetes_version, config.schemas
    )
    if head.failed:
        return [
            Finding(
                "GC309",
                Severity.WARN,
                cc.name,
                "schemas",
                f"the schema check did not run: {head.failed}",
            )
        ]
    base = run(
        kubeconform, cc.base_objects, work / "base", config.kubernetes_version, config.schemas
    )
    found: list[Finding] = []
    by_name: dict[tuple[str, str], Ref] = {(r.kind, r.name): r for r in cc.head_objects}
    old: list[str] = []
    for key, said in sorted(head.invalid.items()):
        kind, name, _ = key
        ref = by_name.get((kind, name))
        if key in base.invalid:
            old.append(f"{kind}/{name}")
            continue
        subject: Ref | str = ref if ref else f"{kind}/{name}"
        found.append(
            cc.finding(
                "GC307",
                Severity.BLOCK,
                subject,
                f"does not match its schema (Kubernetes {config.kubernetes_version}): {said}",
            )
        )
    if old:
        found.append(
            Finding(
                "GC307",
                Severity.INFO,
                cc.name,
                "existing",
                f"{len(old)} object(s) already did not match their schema: " + ", ".join(old[:15]),
            )
        )
    if head.skipped:
        kinds = ", ".join(f"{kind} ({n})" for kind, n in head.skipped.most_common())
        found.append(
            Finding(
                "GC308",
                Severity.INFO,
                cc.name,
                "schemas",
                f"{sum(head.skipped.values())} object(s) had no schema to check against: {kinds}",
            )
        )
    if head.unreachable:
        found.append(
            Finding(
                "GC309",
                Severity.WARN,
                cc.name,
                "schemas",
                f"{len(head.unreachable)} object(s) were not checked: a schema could not be "
                f"downloaded ({head.unreachable[0]})",
            )
        )
    return found
