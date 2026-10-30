"""YAML the merge request touches: it parses, has no duplicate key, says what it means."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import NamedTuple

from flux_preflight.model import Finding, Severity
from flux_preflight.yamlfiles import YamlProblem, is_yaml, iter_docs

YAMLLINT_SECONDS = 60
_PARSABLE = re.compile(r"stdin:(\d+):\d+: \[(error|warning)\] (.*?)(?: \(([a-z-]+)\))?")

# Only rules about meaning. `yes`/`on` are booleans to Kubernetes' YAML 1.1 parser, and 0755 is
# the number 493: both make an apply fail or do something else than written.
LINT = """
rules:
  key-duplicates: enable
  anchors: enable
  truthy:
    allowed-values: ["true", "false", "True", "False", "TRUE", "FALSE"]
    check-keys: false
    level: warning
  octal-values:
    forbid-implicit-octal: true
    forbid-explicit-octal: false
    level: warning
"""


class LintError(Exception):
    """yamllint itself failed, so what it would have said is unknown."""


class Problem(NamedTuple):
    line: int
    level: str
    desc: str
    rule: str | None


def _lint(text: str) -> list[Problem]:
    """yamllint is GPL, so it runs as its own program rather than being imported into this one."""
    done = subprocess.run(
        ["yamllint", "--format", "parsable", "--config-data", LINT, "-"],
        input=text,
        capture_output=True,
        encoding="utf-8",
        timeout=YAMLLINT_SECONDS,
        check=False,
    )
    if done.returncode not in (0, 1):  # 1 is problems found
        raise LintError(f"yamllint exited {done.returncode}: {done.stderr.strip()[:300]}")
    problems = []
    for said in done.stdout.splitlines():
        match = _PARSABLE.fullmatch(said)
        if not match:
            raise LintError(f"yamllint printed a line this does not read: {said[:300]}")
        line, level, desc, rule = match.groups()
        problems.append(Problem(int(line), level, desc, rule))
    return problems


def _warnings_before(base: Path, rel: str) -> set[tuple[str | None, str]]:
    """Warnings the file already had, by rule and line text: those are not this change's."""
    file = base / rel
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    lines = text.splitlines()
    return {
        (p.rule, lines[p.line - 1].strip() if 0 < p.line <= len(lines) else "")
        for p in _lint(text)
        if p.level != "error"
    }


def check(base: Path, tree: Path, changed: set[str], ignore: list[str]) -> list[Finding]:
    found: list[Finding] = []
    for rel in sorted(changed):
        if not is_yaml(rel) or any(rel == i or rel.startswith(i + "/") for i in ignore):
            continue
        file = tree / rel
        if not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            found.append(Finding("GC002", Severity.BLOCK, "", rel, f"{rel} is not UTF-8", rel))
            continue
        broken = False
        try:
            for _ in iter_docs(text):
                pass
        except YamlProblem as exc:
            broken = True
            found.append(
                Finding(
                    "GC002", Severity.BLOCK, "", f"{rel}:{exc.line}", f"{rel}: {exc}", rel, exc.line
                )
            )
        before = _warnings_before(base, rel)
        lines = text.splitlines()
        for problem in _lint(text):
            line = lines[problem.line - 1].strip() if 0 < problem.line <= len(lines) else ""
            if problem.level != "error" and (problem.rule, line) in before:
                continue
            if problem.level == "error":
                if broken:
                    continue  # the parser already said it
                found.append(
                    Finding(
                        "GC002",
                        Severity.BLOCK,
                        "",
                        f"{rel}:{problem.line}",
                        f"{rel}:{problem.line}: {problem.desc}",
                        rel,
                        problem.line,
                    )
                )
            else:
                found.append(
                    Finding(
                        "GC306",
                        Severity.WARN,
                        "",
                        f"{rel}:{problem.line}",
                        f"{rel}:{problem.line}: {problem.desc}",
                        rel,
                        problem.line,
                    )
                )
    return found
