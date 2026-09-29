"""Task worktrees for command executors.

<workspace>/repos/<owner>/<repo> is a shared checkout used only as the Git object source (cloned lazily,
fetched before each use). Every command-executor invocation gets its own detached worktree under
<workspace>/worktrees/, created at the fetched remote default branch tip and removed when the executor
exits, so concurrent Tasks never share a mutable working tree. No registry or mapping.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
from .contracts import Failure, require
from .executors import process

# Command executors run against the fetched remote default branch tip, never a local working branch.
BASE = "origin/HEAD"

REPOSITORY = re.compile(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+")
ORIGIN = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?")


def valid_repository(repository):
    return (isinstance(repository, str) and REPOSITORY.fullmatch(repository) is not None
            and repository.split("/")[1] not in (".", ".."))


def origin_matches(url, repository):
    match = ORIGIN.fullmatch(url.strip())
    return match is not None and match[1].lower() == repository.lower()


@contextmanager
def repository_lock(path, repository):
    # Concurrent Tasks of one repository must not race on the shared checkout (clone, fetch, worktree metadata).
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(path.parent / f".{path.name}.lock", "a")
    except OSError as exc:
        raise Failure("checkout", f"Cannot prepare checkout for {repository}: {exc}") from exc
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def resolve(workspace, repository):
    """Return the shared checkout path for a Task repository, cloning it lazily and fetching origin."""
    require(valid_repository(repository), f"Invalid Task repository: {repository!r}", "checkout")
    path = Path(workspace) / "repos" / repository
    with repository_lock(path, repository):
        return prepare(path, repository)


@contextmanager
def worktree(workspace, repository, name, cleanup_failed=None):
    """Yield a fresh detached worktree of the Task repository at origin/HEAD; remove it on exit, whatever
    the executor did. Creation failure is a checkout Failure; removal failure is only reported."""
    require(valid_repository(repository), f"Invalid Task repository: {repository!r}", "checkout")
    shared = Path(workspace) / "repos" / repository
    path = Path(workspace).absolute() / "worktrees" / name
    with repository_lock(shared, repository):
        prepare(shared, repository)
        try:
            require(not path.exists() and not path.is_symlink(), f"{path} already exists", "checkout")
            path.parent.mkdir(parents=True, exist_ok=True)
            process(["git", "-C", str(shared), "worktree", "add", "--detach", str(path), BASE], None, 120)
        except (Failure, OSError) as exc:
            raise Failure("checkout", f"Cannot create a Task worktree for {repository}: {exc}") from exc
    try:
        yield str(path)
    finally:
        with repository_lock(shared, repository):
            try:
                process(["git", "-C", str(shared), "worktree", "remove", "--force", str(path)], None, 120)
                process(["git", "-C", str(shared), "worktree", "prune"], None, 60)
            except (Failure, OSError) as exc:
                if cleanup_failed:
                    cleanup_failed({"worktree": str(path), "message": str(exc)})


def prepare(path, repository):
    hint = ""
    try:
        if not path.exists() and not path.is_symlink():
            process(["gh", "repo", "clone", f"github.com/{repository}", str(path)], None, 600)
        else:
            require(path.is_dir() and not path.is_symlink(), f"{path} exists but is not a checkout directory", "checkout")
            # Git searches upward, so first ensure the path is itself a repository root, not inside another one.
            top = process(["git", "-C", str(path), "rev-parse", "--show-toplevel"], None, 30).strip()
            require(os.path.samefile(top, path), f"{path} exists but is not itself a Git checkout", "checkout")
            origin = process(["git", "-C", str(path), "remote", "get-url", "origin"], None, 30)
            require(origin_matches(origin, repository),
                    f"{path} has origin {origin.strip()!r}, not {repository}; refusing to reuse it", "checkout")
        process(["git", "-C", str(path), "fetch", "origin"], None, 600)
        hint = "; if origin/HEAD is missing or stale, run `git remote set-head origin --auto` there"
        process(["git", "-C", str(path), "rev-parse", "--verify", f"{BASE}^{{commit}}"], None, 30)
    except (Failure, OSError) as exc:
        details = exc.details if isinstance(exc, Failure) else {}
        raise Failure("checkout", f"Cannot prepare checkout for {repository}: {exc}{hint}", details) from exc
    return str(path)
