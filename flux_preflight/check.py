"""One run: both sides of the merge request, every cluster, every rule."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from flux_preflight import git
from flux_preflight.clusters import Root, RootError, read_root, sync_files
from flux_preflight.config import Config
from flux_preflight.context import ClusterCheck, Files
from flux_preflight.inventory import Outcome, compare
from flux_preflight.model import Finding, Render, Severity
from flux_preflight.render import render_cluster
from flux_preflight.rules import build, change, deletion, hygiene, lint, schema
from flux_preflight.yamlfiles import ignored, is_yaml, raw_docs


@dataclass
class ClusterSummary:
    name: str
    sync: str
    skipped: str | None = None
    kustomizations: int = 0
    objects: int = 0
    added: int = 0
    changed: int = 0
    deleted: int = 0


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    clusters: list[ClusterSummary] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.BLOCK and not f.allowed]


@dataclass
class Tools:
    flux: str = "flux"
    kubeconform: str = "kubeconform"


_ALLOW = re.compile(r"^\s*flux-preflight\s+allow\s+(GC\d{3})\s+(\S+)\s*$", re.MULTILINE)


def allowances(description: str) -> set[str]:
    """`flux-preflight allow GC101 cluster:Namespace/falco` lines of the description."""
    return {f"{rule} {subject}" for rule, subject in _ALLOW.findall(description or "")}


def run(
    *,
    config: Config,
    base_tree: Path,
    head_tree: Path,
    changed: set[str],
    work: Path,
    target_branch: str | None,
    description: str,
    tools: Tools,
) -> Result:
    result = Result()
    base_raw, _ = raw_docs(base_tree, config.ignore)
    head_raw, broken = raw_docs(head_tree, config.ignore)
    files = Files(base=base_raw, head=head_raw, broken_head=broken, changed=changed)

    result.findings += lint.check(
        base_tree, head_tree, changed, config.ignore, config.yamllint_seconds
    )
    result.findings += hygiene.check_files(files, config.min_secret_length)

    names = {entry.sync: entry.name for entry in config.clusters}
    checked: list[ClusterCheck] = []
    for sync in sorted(set(sync_files(base_tree, config)) | set(sync_files(head_tree, config))):
        summary = ClusterSummary(name=names.get(sync) or sync, sync=sync)
        result.clusters.append(summary)
        try:
            base_root = read_root(base_tree, sync, names.get(sync))
            head_root = read_root(head_tree, sync, names.get(sync))
        except RootError as exc:
            result.findings.append(
                Finding("GC001", Severity.BLOCK, summary.name, sync, str(exc), sync)
            )
            continue
        # The cluster keeps its root when the file goes: it is what Flux runs from.
        root = head_root or base_root
        if root is None:
            summary.skipped = f"{sync} is in neither side"
            continue
        summary.name = root.name
        if target_branch and root.branch and root.branch != target_branch:
            summary.skipped = f"it deploys from branch {root.branch}, not {target_branch}"
            continue
        cc, found = _cluster(
            config, root, base_root, base_tree, head_tree, files, work, tools, summary
        )
        checked.append(cc)
        result.findings += found

    if not result.clusters:
        result.findings.append(
            Finding(
                "GC005",
                Severity.WARN,
                "",
                "clusters",
                "no cluster found: no gotk-sync.yaml and no `clusters:` in .flux-preflight.yaml, "
                "so nothing was rendered",
            )
        )
    for rel in sorted(changed):
        if is_yaml(rel) and not rel.startswith(".") and not ignored(rel, config.ignore):
            if checked and not any(cc.deploys(rel) for cc in checked):
                result.not_checked.append(f"{rel}: no checked cluster deploys this path")
    result.not_checked.append(
        "what Helm charts render: HelmRelease specs and values are checked, charts are not built"
    )

    allowed = allowances(description)
    for finding in result.findings:
        if finding.key in allowed:
            finding.allowed = True
    return result


def _cluster(
    config: Config,
    root: Root,
    base_root: Root | None,
    base_tree: Path,
    head_tree: Path,
    files: Files,
    work: Path,
    tools: Tools,
    summary: ClusterSummary,
) -> tuple[ClusterCheck, list[Finding]]:
    seconds = config.flux_build_seconds
    head = render_cluster(tools.flux, head_tree, root.name, root.kustomization, seconds)
    if base_root is None:
        # A new cluster: nothing is deleted, everything is added.
        base = Render(cluster=root.name, root=head.root)
    else:
        base = render_cluster(tools.flux, base_tree, root.name, base_root.kustomization, seconds)
    outcome: Outcome = compare(base, head)
    cc = ClusterCheck(root.name, root.path, base, head, outcome, files, config)
    summary.kustomizations = len(head.kustomizations)
    summary.objects = len(cc.head_objects)
    summary.added, summary.changed = len(outcome.added), len(outcome.changed)
    summary.deleted = len(outcome.deletions)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", root.name)
    return cc, [
        *build.check(cc),
        *deletion.check(cc),
        *change.check(cc),
        *hygiene.check(cc),
        *schema.check(cc, tools.kubeconform, work / "schemas" / safe),
    ]


def prepare(repo: Path, base: str, head: str, work: Path) -> tuple[Path, Path, set[str]]:
    base_sha = git.ensure_commit(repo, base)
    head_ref = head if head == git.WORKTREE else git.ensure_commit(repo, head)
    base_tree, head_tree = work / "base", work / "head"
    git.export(repo, base_sha, base_tree)
    git.export(repo, head_ref, head_tree)
    return base_tree, head_tree, git.changed_files(base_tree, head_tree)
