"""flux-preflight: what a merge into a Flux repository deletes or breaks, before it is merged.

Exit codes: 0 nothing blocks; 1 something blocks (mode block); 3 something would block (mode
warn: the job is marked allowed to fail with it); 2 the check itself could not run.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import traceback
from pathlib import Path

from flux_preflight import check, git, github, gitlab, report
from flux_preflight.config import ConfigError, load_config


def _default_base(repo: Path, head: str) -> str | None:
    if os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA"):
        return os.environ["CI_MERGE_REQUEST_DIFF_BASE_SHA"]
    if os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME"):
        return f"origin/{os.environ['CI_MERGE_REQUEST_TARGET_BRANCH_NAME']}"
    return github.base(repo, head)


def _default_target_branch() -> str | None:
    return os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME") or github.target_branch()


def _default_description() -> str:
    return os.environ.get("CI_MERGE_REQUEST_DESCRIPTION") or github.description()


def parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="flux-preflight", description=__doc__.split("\n")[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="the Flux repository")
    parser.add_argument("--base", help="what the change is compared with")
    parser.add_argument(
        "--head", default="HEAD", help=f"the change; {git.WORKTREE} for uncommitted"
    )
    parser.add_argument(
        "--target-branch", help="only clusters that deploy from this branch are checked"
    )
    parser.add_argument("--mode", choices=["warn", "block"], help="overrides .flux-preflight.yaml")
    parser.add_argument("--out", type=Path, default=Path("flux-preflight"), help="where reports go")
    parser.add_argument(
        "--description-file", type=Path, help="the merge request's description (allow lines)"
    )
    parser.add_argument("--flux", default=os.environ.get("FLUX_BIN", "flux"))
    parser.add_argument("--kubeconform", default=os.environ.get("KUBECONFORM_BIN", "kubeconform"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except Exception:  # the check failing must not read as "nothing found"
        traceback.print_exc()
        print("flux-preflight: the check itself failed; nothing was verified", file=sys.stderr)
        return 2


def _main(argv: list[str] | None) -> int:
    args = parse(argv)
    repo = args.repo.resolve()
    try:
        base = args.base or _default_base(repo, args.head)
    except git.GitError as exc:
        print(f"flux-preflight: {exc}", file=sys.stderr)
        return 2
    if not base:
        print("flux-preflight: no --base and not a merge request pipeline", file=sys.stderr)
        return 2
    try:
        config = load_config(repo)
    except ConfigError as exc:
        print(f"flux-preflight: {exc}", file=sys.stderr)
        return 2
    mode = args.mode or config.mode
    target_branch = args.target_branch or _default_target_branch()
    description = _default_description()
    if args.description_file:
        description = args.description_file.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="flux-preflight-") as scratch:
        work = Path(scratch)
        try:
            base_tree, head_tree, changed = check.prepare(repo, base, args.head, work)
        except git.GitError as exc:
            print(f"flux-preflight: {exc}", file=sys.stderr)
            return 2
        result = check.run(
            config=config,
            base_tree=base_tree,
            head_tree=head_tree,
            changed=changed,
            work=work,
            target_branch=target_branch,
            description=description,
            tools=check.Tools(flux=args.flux, kubeconform=args.kubeconform),
        )
    args.out.mkdir(parents=True, exist_ok=True)
    note = report.markdown(result, mode)
    (args.out / "report.md").write_text(note, encoding="utf-8")
    (args.out / "report.json").write_text(report.as_json(result, mode), encoding="utf-8")
    (args.out / "code-quality.json").write_text(report.code_quality(result), encoding="utf-8")
    print(report.text(result, mode))
    for line in github.workflow_commands(result.findings, report.verdict(result, mode)):
        print(line)
    github.summary(note)
    for problem in (gitlab.post_note(note), github.post_comment(note)):
        if problem:
            print(f"flux-preflight: {problem}", file=sys.stderr)
    return report.exit_code(result, mode)


if __name__ == "__main__":
    sys.exit(main())
