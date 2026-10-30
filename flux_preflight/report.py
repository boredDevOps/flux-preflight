"""The result for people (job log, merge request note) and machines (JSON, Code Quality)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from flux_preflight.check import Result
from flux_preflight.model import Finding, Severity
from flux_preflight.rules import RULES

MARKER = "<!-- flux-preflight -->"
ALLOW = "flux-preflight allow "
SHOWN = 100


def verdict(result: Result, mode: str) -> str:
    if result.blocking:
        return "block" if mode == "block" else "would-block"
    if any(f.severity == Severity.WARN and not f.allowed for f in result.findings):
        return "warn"
    return "pass"


def exit_code(result: Result, mode: str) -> int:
    if not result.blocking:
        return 0
    return 1 if mode == "block" else 3


def _where(finding: Finding) -> str:
    return f"{finding.cluster} · {finding.subject}" if finding.cluster else finding.subject


def _groups(result: Result) -> dict[str, list[Finding]]:
    order = {Severity.BLOCK: 0, Severity.WARN: 1, Severity.INFO: 2}
    findings = sorted(
        result.findings, key=lambda f: (order[f.severity], f.cluster, f.rule, f.subject)
    )
    return {
        "block": [f for f in findings if f.severity == Severity.BLOCK and not f.allowed],
        "warn": [f for f in findings if f.severity == Severity.WARN and not f.allowed],
        "info": [f for f in findings if f.severity == Severity.INFO and not f.allowed],
        "allowed": [f for f in findings if f.allowed],
    }


def _headline(result: Result, mode: str) -> str:
    groups = _groups(result)
    blocks, warns = len(groups["block"]), len(groups["warn"])
    if blocks:
        tail = "" if mode == "block" else " (warn mode: the merge is not blocked yet)"
        return f"❌ {blocks} to fix before merging, {warns} warning(s){tail}"
    if warns:
        return f"⚠️ nothing blocks; {warns} warning(s) to read"
    return "✅ nothing found that deletes or breaks something"


def text(result: Result, mode: str) -> str:
    lines = [f"flux-preflight: {_headline(result, mode)}", ""]
    for c in result.clusters:
        if c.skipped:
            lines.append(f"  {c.name}: skipped, {c.skipped}")
        else:
            lines.append(
                f"  {c.name}: {c.kustomizations} Kustomizations, {c.objects} objects; "
                f"+{c.added} ~{c.changed} -{c.deleted}"
            )
    labels = {"block": "BLOCK", "warn": "WARN", "info": "INFO", "allowed": "ALLOWED"}
    for group, findings in _groups(result).items():
        if not findings:
            continue
        lines += ["", f"{labels[group]} ({len(findings)})"]
        for f in findings:
            lines.append(f"  [{f.rule}] {_where(f)}")
            lines.append(f"      {f.message}")
            if group == "block":
                lines.append(
                    f"      to accept, add to the MR description: flux-preflight allow {f.key}"
                )
    if result.not_checked:
        lines += ["", "NOT CHECKED"]
        lines += [f"  - {item}" for item in result.not_checked]
    return "\n".join(lines) + "\n"


def _code(value: str) -> str:
    return "`" + value.replace("`", "'") + "`"


def markdown(result: Result, mode: str) -> str:
    groups = _groups(result)
    out = [MARKER, f"### flux-preflight: {_headline(result, mode)}", ""]
    out += [
        "| Cluster | Kustomizations | Objects | Added | Changed | Deleted |",
        "|---|---|---|---|---|---|",
    ]
    for c in result.clusters:
        if c.skipped:
            out.append(f"| {_code(c.name)} | skipped: {c.skipped} | | | | |")
        else:
            out.append(
                f"| {_code(c.name)} | {c.kustomizations} | {c.objects} | {c.added} | {c.changed} | "
                f"**{c.deleted}** |"
            )

    def items(findings: list[Finding], accept: bool) -> list[str]:
        lines: list[str] = []
        for f in findings[:SHOWN]:
            lines.append(f"- **{f.rule}** {_code(_where(f))}: {f.message}")
            if accept:
                lines.append(
                    f"  <br><sub>If intended, add to the description: {_code(ALLOW + f.key)}</sub>"
                )
        if len(findings) > SHOWN:
            lines.append(f"- … and {len(findings) - SHOWN} more in the job's report")
        return lines

    if groups["block"]:
        out += ["", "#### ❌ Blocking", *items(groups["block"], True)]
    if groups["warn"]:
        out += ["", "#### ⚠️ Warnings", *items(groups["warn"], False)]
    for key, title in (("info", "ℹ️ Information"), ("allowed", "✔️ Accepted in the description")):
        if groups[key]:
            out += ["", f"<details><summary>{title} ({len(groups[key])})</summary>", ""]
            out += [*items(groups[key], False), "", "</details>"]
    if result.not_checked:
        out += ["", "<details><summary>Not checked</summary>", ""]
        out += [f"- {item}" for item in result.not_checked] + ["", "</details>"]
    return "\n".join(out) + "\n"


def as_json(result: Result, mode: str) -> str:
    return json.dumps(
        {
            "version": 1,
            "mode": mode,
            "verdict": verdict(result, mode),
            "clusters": [asdict(c) for c in result.clusters],
            "findings": [
                {**asdict(f), "key": f.key, "title": RULES.get(f.rule, "")} for f in result.findings
            ],
            "not_checked": result.not_checked,
        },
        indent=2,
        ensure_ascii=False,
    )


_QUALITY = {Severity.BLOCK: "blocker", Severity.WARN: "major", Severity.INFO: "info"}


def code_quality(result: Result) -> str:
    """GitLab's Code Quality format: shown on the merge request with no token needed."""
    entries = []
    for f in result.findings:
        if f.allowed:
            continue
        entries.append(
            {
                "description": f"[{f.rule}] {_where(f)}: {f.message}"[:1000],
                "check_name": f.rule,
                "fingerprint": hashlib.sha256(f.key.encode()).hexdigest(),
                "severity": _QUALITY[f.severity],
                "location": {
                    "path": f.path or ".flux-preflight.yaml",
                    "lines": {"begin": f.line or 1},
                },
            }
        )
    return json.dumps(entries, indent=2, ensure_ascii=False)
