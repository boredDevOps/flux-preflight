"""GitHub Actions: the pull request read from the event file, and the report on the run and the PR.

The end-to-end cases build the merge commit actions/checkout checks out (refs/pull/N/merge) while
the base branch has moved on, which is where comparing with the wrong commit shows up.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from flux_preflight import cli, github
from flux_preflight.model import Finding, Severity
from tests.conftest import Repo

Cap = pytest.CaptureFixture[str]

DROP_NAMESPACE = ("  - namespace.yaml\n", "")


class PullRequest:
    """A pull request from `feature` into `main`, with `main` moved on since the branch was cut."""

    def __init__(self, repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.repo, self.tmp, self.monkeypatch = repo, tmp_path, monkeypatch
        repo.git("checkout", "-q", "-b", "feature")
        repo.edit("apps/kustomization.yaml", *DROP_NAMESPACE)
        self.head = repo.commit("drop the namespace")
        repo.git("checkout", "-q", "main")
        repo.edit("apps/web.yaml", "nginx:1.27.0", "nginx:1.27.1")
        self.main = repo.commit("main moves on")
        self.summary = tmp_path / "summary.md"

    def merge_ref(self) -> None:
        """HEAD as actions/checkout leaves it by default: the pull request merged into main."""
        self.repo.git("checkout", "-q", "--detach", "main")
        self.repo.git("merge", "-q", "--no-ff", "--no-edit", "feature")
        self.env("refs/pull/7/merge")

    def env(self, ref: str, body: str = "", base_sha: str | None = None) -> None:
        event = self.tmp / "event.json"
        pull = {
            "number": 7,
            "body": body,
            "base": {"ref": "main", "sha": base_sha or self.main},
            "head": {"ref": "feature", "sha": self.head},
        }
        event.write_text(json.dumps({"pull_request": pull}), encoding="utf-8")
        for name, value in {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_REF": ref,
            "GITHUB_STEP_SUMMARY": str(self.summary),
        }.items():
            self.monkeypatch.setenv(name, value)

    def check(self, capsys: Cap, repo: Path | None = None) -> tuple[int, dict[str, Any], str]:
        out = self.tmp / "out"
        code = cli.main(["--repo", str(repo or self.repo.path), "--out", str(out)])
        printed = capsys.readouterr()
        report_file = out / "report.json"
        report = json.loads(report_file.read_text(encoding="utf-8")) if code != 2 else {}
        return code, report, printed.out + printed.err


@pytest.fixture
def pr(repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PullRequest:
    return PullRequest(repo, tmp_path, monkeypatch)


def rules(report: dict[str, Any]) -> list[str]:
    return sorted(f["rule"] for f in report["findings"] if f["severity"] != "info")


def test_the_merge_ref_is_compared_with_the_base_it_merges_into(
    pr: PullRequest, capsys: Cap
) -> None:
    pr.merge_ref()
    code, report, printed = pr.check(capsys)
    assert code == 3
    assert rules(report) == ["GC101"]
    # main's own image change is on both sides, so it is not this pull request's.
    assert report["clusters"][0]["changed"] == 0
    assert report["clusters"][0]["deleted"] == 1
    blocking = report["findings"][0]
    title = f"title=GC101 {blocking['cluster']} · {blocking['subject']}::"
    assert any(line.startswith("::error ") and title in line for line in printed.splitlines()), (
        printed
    )
    assert "::warning title=flux-preflight::something would block" in printed
    summary = pr.summary.read_text(encoding="utf-8")
    assert "<!-- flux-preflight -->" in summary and "GC101" in summary


def test_the_pull_requests_own_commit_is_compared_with_where_it_left_main(
    pr: PullRequest, capsys: Cap
) -> None:
    pr.repo.git("checkout", "-q", "feature")
    pr.env("refs/pull/7/head")
    code, report, _ = pr.check(capsys)
    assert code == 3
    assert rules(report) == ["GC101"]
    # Compared with main as it is now, the image change would read as this branch undoing it.
    assert report["clusters"][0]["changed"] == 0


def test_a_pull_request_whose_own_tip_is_a_merge_is_not_taken_for_the_merge_ref(
    pr: PullRequest, capsys: Cap
) -> None:
    """ "Update branch" merges main into the pull request: its tip has two parents too."""
    pr.repo.git("checkout", "-q", "feature")
    pr.repo.git("merge", "-q", "--no-ff", "--no-edit", "main")
    pr.head = pr.repo.git("rev-parse", "HEAD")
    pr.env("refs/pull/7/merge")
    code, report, _ = pr.check(capsys)
    assert code == 3
    assert rules(report) == ["GC101"]
    assert report["clusters"][0]["changed"] == 0


def test_an_allow_line_in_the_description_accepts_the_finding(pr: PullRequest, capsys: Cap) -> None:
    pr.merge_ref()
    _, report, _ = pr.check(capsys)
    key = next(f["key"] for f in report["findings"] if f["rule"] == "GC101")
    # The description as GitHub keeps it, with Windows line ends from the browser.
    pr.env("refs/pull/7/merge", body=f"Moving it.\r\n\r\nflux-preflight allow {key}\r\n")
    code, report, _ = pr.check(capsys)
    assert code == 0, key
    assert [f["rule"] for f in report["findings"] if f["allowed"]] == ["GC101"]


def test_a_shallow_clone_says_how_to_fix_it(pr: PullRequest, tmp_path: Path, capsys: Cap) -> None:
    shallow = tmp_path / "shallow"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--depth=1",
            "--branch=feature",
            f"file://{pr.repo.path.as_posix()}",
            str(shallow),
        ],
        check=True,
    )
    pr.env("refs/pull/7/head", base_sha=pr.repo.base)
    code, _, printed = pr.check(capsys, shallow)
    assert code == 2
    assert "fetch-depth: 0" in printed


def test_outside_github_the_event_file_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"body": "x"}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert github.pull_request() is None
    blocking = [Finding("GC101", Severity.BLOCK, "p", "x", "y")]
    assert github.workflow_commands(blocking, "block") == []
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    assert github.pull_request() is None


def test_annotations_escape_what_would_end_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    findings = [
        Finding("GC101", Severity.BLOCK, "prod", "ConfigMap/a,b:c", "100%\ngone", "apps/x.yaml", 4),
        Finding("GC207", Severity.WARN, "", "apps/y.yaml", "later"),
        Finding("GC301", Severity.INFO, "", "s", "already there"),
        Finding("GC102", Severity.BLOCK, "prod", "s", "accepted", allowed=True),
    ]
    assert github.workflow_commands(findings, "block") == [
        "::error file=apps/x.yaml,line=4,title=GC101 prod · ConfigMap/a%2Cb%3Ac::100%25%0Agone",
        "::warning title=GC207 apps/y.yaml::later",
    ]


class _Api(BaseHTTPRequestHandler):
    comments: list[dict[str, Any]] = []
    calls: list[tuple[str, str, Any]] = []
    status = 200

    def _reply(self, body: Any) -> None:
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _body(self) -> Any:
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def do_GET(self) -> None:  # noqa: N802 - the handler's own naming
        self.calls.append(("GET", self.path, self.headers["Authorization"]))
        self._reply(self.comments if self.status == 200 else {"message": "no"})

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        self.calls.append(("POST", self.path, body))
        self.comments.append({"id": 1, "body": body["body"]})
        self._reply({"id": 1})

    def do_PATCH(self) -> None:  # noqa: N802
        body = self._body()
        self.calls.append(("PATCH", self.path, body))
        mine = next(c for c in self.comments if self.path.endswith(f"/{c['id']}"))
        mine["body"] = body["body"]
        self._reply({"id": 1})

    def log_message(self, *_: Any) -> None:
        pass


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Api]]:
    _Api.comments, _Api.calls, _Api.status = [{"id": 9, "body": "someone else"}], [], 200
    server = HTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/flux")
    monkeypatch.setenv("GITHUB_API_URL", f"http://127.0.0.1:{server.server_port}")
    yield _Api
    server.shutdown()


def test_one_comment_is_written_then_updated(
    api: type[_Api], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert github.post_comment("<!-- flux-preflight -->\nfirst") is None  # no token: nothing
    assert api.calls == []
    monkeypatch.setenv(github.TOKEN_ENV, "t0ken")
    assert github.post_comment("<!-- flux-preflight -->\nfirst") is None
    assert github.post_comment("<!-- flux-preflight -->\nsecond") is None
    methods = [(method, path) for method, path, _ in api.calls]
    assert methods == [
        ("GET", "/repos/acme/flux/issues/7/comments?per_page=100&page=1"),
        ("POST", "/repos/acme/flux/issues/7/comments"),
        ("GET", "/repos/acme/flux/issues/7/comments?per_page=100&page=1"),
        ("PATCH", "/repos/acme/flux/issues/comments/1"),
    ]
    assert api.calls[0][2] == "Bearer t0ken"
    assert [c["body"] for c in api.comments] == ["someone else", "<!-- flux-preflight -->\nsecond"]


def test_a_refused_comment_is_reported_not_raised(
    api: type[_Api], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(github.TOKEN_ENV, "read-only")
    api.status = 403
    problem = github.post_comment("<!-- flux-preflight -->\nx")
    assert problem and problem.startswith("could not write the comment")
