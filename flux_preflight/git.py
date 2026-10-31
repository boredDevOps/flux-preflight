"""The two sides of a merge request as plain directories, and the files it touches."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path

WORKTREE = "WORKTREE"
_FULL_CLONE = "Clone the whole history (GIT_DEPTH: 0 in GitLab, fetch-depth: 0 on actions/checkout)"


class GitError(Exception):
    pass


def _git(repo: Path, *args: str) -> bytes:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if done.returncode:
        raise GitError(f"git {' '.join(args)}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def ensure_commit(repo: Path, ref: str) -> str:
    """The commit `ref` names, fetching it when a shallow CI clone does not have it yet."""
    try:
        return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    except GitError:
        pass
    try:
        _git(repo, "fetch", "--no-tags", "--depth=1", "origin", ref)
        return _git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}").decode().strip()
    except GitError as exc:
        raise GitError(
            f"the base {ref} is not in this clone and could not be fetched ({exc}). "
            f"{_FULL_CLONE}, or pass --base."
        ) from exc


def parents(repo: Path, ref: str) -> list[str]:
    """The parents of `ref`; none for a root commit, or for the last commit of a shallow clone."""
    return _git(repo, "rev-list", "--parents", "-n", "1", ref).decode().split()[1:]


def merge_base(repo: Path, base: str, head: str) -> str:
    """Where `head` left `base`. A shallow clone does not have it, and comparing with `base`
    itself would show what the base branch did since as this change undoing it."""
    base_sha = ensure_commit(repo, base)
    try:
        return _git(repo, "merge-base", base_sha, head).decode().strip()
    except GitError as exc:
        raise GitError(
            f"no common commit of {base} and {head} in this clone ({exc}). "
            f"{_FULL_CLONE}, or pass --base."
        ) from exc


def export(repo: Path, ref: str, dest: Path) -> None:
    """The tree of `ref` (or the working tree) into `dest`, no .git: flux build writes into it."""
    dest.mkdir(parents=True, exist_ok=True)
    if ref == WORKTREE:
        names = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        for name in filter(None, names.decode().split("\0")):
            src = repo / name
            if not src.exists() and not src.is_symlink():
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target, follow_symlinks=False)
        return
    archive = dest.parent / f"{dest.name}.tar"
    archive.write_bytes(_git(repo, "archive", "--format=tar", ref))
    with tarfile.open(archive) as tar:
        # "data" refuses links and paths that lead out of the tree.
        tar.extractall(dest, filter="data")
    archive.unlink()


def _files(tree: Path) -> dict[str, Path]:
    return {p.relative_to(tree).as_posix(): p for p in tree.rglob("*") if p.is_file()}


def changed_files(base_tree: Path, head_tree: Path) -> set[str]:
    """Files whose bytes differ between the two exported trees (git's view of modes and line
    endings on a mounted checkout is not what Flux reads)."""
    base, head = _files(base_tree), _files(head_tree)

    def text(path: Path) -> bytes:
        return path.read_bytes().replace(b"\r\n", b"\n")

    return {
        name
        for name in base.keys() | head.keys()
        if name not in base or name not in head or text(base[name]) != text(head[name])
    }
