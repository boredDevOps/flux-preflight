"""GitHub Actions: the pull request from the event file, the report on the run and the pull request.

The event file is read rather than `${{ github.event.pull_request.body }}` in the workflow: a
description pasted into a shell script is a way to run commands in the job.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from flux_preflight import git
from flux_preflight.model import Finding, Severity
from flux_preflight.report import MARKER

TOKEN_ENV = "FLUX_PREFLIGHT_GITHUB_TOKEN"
# A comment is looked for among this many pages of 100; past that a new one is written.
_PAGES = 10


def active() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def pull_request() -> dict[str, Any] | None:
    """The `pull_request` of the event that started the run, None outside a pull request run.

    Not pull_request_target: that checks out the base branch, so there would be nothing to
    compare, and it runs a fork's change with a token that can write.
    """
    if not active() or os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return None
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        return None
    event = json.loads(Path(path).read_text(encoding="utf-8"))
    pr = event.get("pull_request")
    return pr if isinstance(pr, dict) else None


def base(repo: Path, head: str) -> str | None:
    """What the pull request is compared with.

    actions/checkout checks out refs/pull/N/merge, a merge of the pull request into the base
    branch as it is now: its first parent is that base, so the difference is exactly what the
    merge brings. When the pull request's own commit is checked out instead, it is compared with
    where it left the base branch, as GitLab does.
    """
    pr = pull_request()
    if pr is None:
        return None
    base_sha = str(pr.get("base", {}).get("sha") or "")
    head_sha = str(pr.get("head", {}).get("sha") or "")
    if head == "HEAD" and head_sha:
        # Only that merge: the pull request's own tip can be a merge too ("Update branch").
        parents = git.parents(repo, "HEAD")
        if len(parents) == 2 and parents[1] == head_sha:
            return parents[0]
    if not base_sha:
        return None
    if head == git.WORKTREE:
        return base_sha
    return git.merge_base(repo, base_sha, head)


def target_branch() -> str | None:
    pr = pull_request()
    if pr is None:
        return None
    return str(pr.get("base", {}).get("ref") or "") or None


def description() -> str:
    pr = pull_request()
    return str(pr.get("body") or "") if pr else ""


def summary(markdown: str) -> None:
    """The report on the run's summary page: no token needed, fork pull requests included."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if active() and path:
        with open(path, "a", encoding="utf-8") as out:
            out.write(markdown)


def _data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _property(value: str) -> str:
    return _data(value).replace(":", "%3A").replace(",", "%2C")


def workflow_commands(findings: list[Finding], verdict: str) -> list[str]:
    """Workflow commands that put blocking and warning findings on the pull request's lines."""
    if not active():
        return []
    kinds = {Severity.BLOCK: "error", Severity.WARN: "warning"}
    lines = []
    for f in findings:
        if f.allowed or f.severity not in kinds:
            continue
        where = f"{f.cluster} · {f.subject}" if f.cluster else f.subject
        props = [f"title={_property(f'{f.rule} {where}')}"]
        if f.path:
            props.insert(0, f"file={_property(f.path)}")
            if f.line:
                props.insert(1, f"line={f.line}")
        lines.append(f"::{kinds[f.severity]} {','.join(props)}::{_data(f.message)}")
    if verdict == "would-block":
        lines.append(
            "::warning title=flux-preflight::something would block this merge; "
            "warn mode lets it through"
        )
    return lines


def _call(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - GitHub's own API URL
        return json.loads(response.read() or b"null")


def post_comment(markdown: str) -> str | None:
    """Writes or updates the comment; returns why it did not, None when it did."""
    token = os.environ.get(TOKEN_ENV)
    if not token or not active():
        return None
    pr = pull_request()
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if pr is None or not repository or not pr.get("number"):
        return "not a pull request run: no comment written"
    owner_repo = "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/", 1))
    comments = f"{api}/repos/{owner_repo}/issues/{int(pr['number'])}/comments"
    try:
        mine = None
        for page in range(1, _PAGES + 1):
            batch = _call("GET", f"{comments}?per_page=100&page={page}", token)
            mine = next((c for c in batch if MARKER in str(c.get("body", ""))), None)
            if mine or len(batch) < 100:
                break
        if mine:
            url = f"{api}/repos/{owner_repo}/issues/comments/{mine['id']}"
            _call("PATCH", url, token, {"body": markdown})
        else:
            _call("POST", comments, token, {"body": markdown})
    except OSError as exc:
        return f"could not write the comment: {exc}"
    return None
