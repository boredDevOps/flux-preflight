"""One note on the merge request, updated on each run. Only when the job is given a token."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

from flux_preflight.report import MARKER

TOKEN_ENV = "FLUX_PREFLIGHT_GITLAB_TOKEN"


def _call(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("PRIVATE-TOKEN", token)
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - GitLab's own API URL
        return json.loads(response.read() or b"null")


def post_note(markdown: str) -> str | None:
    """Writes or updates the note; returns why it did not, None when it did."""
    token = os.environ.get(TOKEN_ENV)
    api = os.environ.get("CI_API_V4_URL")
    project = os.environ.get("CI_PROJECT_ID")
    iid = os.environ.get("CI_MERGE_REQUEST_IID")
    if not token:
        return None
    if not (api and project and iid):
        return "not a merge request pipeline: no note written"
    base = f"{api}/projects/{urllib.parse.quote(project, safe='')}/merge_requests/{iid}/notes"
    try:
        notes = _call("GET", f"{base}?per_page=100&sort=desc", token)
        mine = next((n for n in notes if MARKER in str(n.get("body", ""))), None)
        if mine:
            _call("PUT", f"{base}/{mine['id']}", token, {"body": markdown})
        else:
            _call("POST", base, token, {"body": markdown})
    except OSError as exc:
        return f"could not write the note: {exc}"
    return None
