"""Deterministic Task checkouts at <workspace>/repos/<owner>/<repo>; no registry or mapping."""
import os
from pathlib import Path
import re
from .contracts import Failure, require
from .executors import BASE, process

REPOSITORY = re.compile(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+")
ORIGIN = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?")


def valid_repository(repository):
    return (isinstance(repository, str) and REPOSITORY.fullmatch(repository) is not None
            and repository.split("/")[1] not in (".", ".."))


def origin_matches(url, repository):
    match = ORIGIN.fullmatch(url.strip())
    return match is not None and match[1].lower() == repository.lower()


def resolve(workspace, repository):
    """Return the checkout path for a Task repository, cloning it lazily and fetching origin."""
    require(valid_repository(repository), f"Invalid Task repository: {repository!r}", "checkout")
    path = Path(workspace) / "repos" / repository
    hint = ""
    try:
        if not path.exists() and not path.is_symlink():
            path.parent.mkdir(parents=True, exist_ok=True)
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
