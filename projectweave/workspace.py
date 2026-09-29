"""Root workspace layout and per-Project Task lifecycle: claim, run-task, complete.

A root workspace holds `projectweave.json` and `projects/<name>/`; each Project
directory is an ordinary Project workspace (project.json, graph.json,
gitweave.json). The directory name is the Project key used by the root config.
"""
from contextlib import contextmanager
import fcntl
from functools import partial
from pathlib import Path
import re
import sys
from .checkout import resolve
from .contracts import Failure, decode, keys, number, require, text
from .executors import invoke
from .github import GitHub, validate_project
from .graph import validate
from .runtime import Runtime

ROOT_CONFIG = "projectweave.json"
PROJECTS = "projects"
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
POLL_SECONDS = 300
TODO, IN_PROGRESS = "Todo", "In Progress"


def percent(value):
    return number(value) and value <= 100


def validate_root(config):
    """{"projects": {name: {"weight": N, "resources": {provider: {min_remaining_percent,
    estimated_usage_percent_per_task}}}}, "poll_seconds": N}; providers are constrained only when listed (opt-in)."""
    keys(config, {"projects", "poll_seconds"}, {"projects"})
    require(isinstance(config["projects"], dict), "projects must be an object")
    require(type(config.get("poll_seconds", POLL_SECONDS)) is int and config.get("poll_seconds", POLL_SECONDS) > 0,
            "poll_seconds must be a positive integer")
    for name, entry in config["projects"].items():
        require(NAME.fullmatch(name) is not None, f"Invalid Project name: {name!r}")
        keys(entry, {"resources", "weight"})
        require(type(entry.get("weight", 1)) is int and entry.get("weight", 1) > 0,
                f"{name}.weight must be a positive integer")
        rules = entry.get("resources", {})
        require(isinstance(rules, dict), f"{name}.resources must be an object")
        for provider, rule in rules.items():
            require(text(provider), f"{name}: provider names must be nonblank")
            keys(rule, {"min_remaining_percent", "estimated_usage_percent_per_task"},
                 {"min_remaining_percent", "estimated_usage_percent_per_task"})
            require(percent(rule["min_remaining_percent"]) and percent(rule["estimated_usage_percent_per_task"]),
                    f"{name}.{provider}: percentages must be 0-100")
    return config


def load_root(root):
    path = Path(root) / ROOT_CONFIG
    require(path.is_file(), f"No {ROOT_CONFIG} here; run projectweave init in the root workspace first", "input")
    config = validate_root(decode(path.read_text()))
    names = projects(root)
    unknown = sorted(set(config["projects"]) - set(names))
    require(not unknown, f"{ROOT_CONFIG} names Projects without a projects/<name>/project.json: {', '.join(unknown)}")
    return config


def projects(root):
    """Managed Projects, auto-discovered as projects/<name>/ directories holding project.json."""
    directory = Path(root) / PROJECTS
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.iterdir()
                  if NAME.fullmatch(path.name) and (path / "project.json").is_file())


def policy(config):
    return {name: entry.get("resources", {}) for name, entry in config["projects"].items()}


def weights(config, names):
    """Scheduling weight per managed Project (default 1)."""
    return {name: config["projects"].get(name, {}).get("weight", 1) for name in names}


def project_dir(root, name):
    require(isinstance(name, str) and NAME.fullmatch(name) is not None, f"Invalid Project name: {name!r}", "input")
    path = Path(root).absolute() / PROJECTS / name
    require((path / "project.json").is_file(), f"No Project workspace at {PROJECTS}/{name}", "input")
    return path


def load_project(directory):
    return validate_project(decode((Path(directory) / "project.json").read_text()))


@contextmanager
def project_lock(directory):
    # Local single-machine lock shared by manual claims and the coordinator; held only while claiming.
    with open(Path(directory) / ".projectweave.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def check_project(directory):
    """Validate a Project workspace before claiming, so a broken setup never strands a Task In Progress."""
    directory = Path(directory)
    load_project(directory)
    return validate(decode((directory / "graph.json").read_text()))


def claim(directory, backend=None):
    """Select one runnable Task and set it In Progress under the Project lock; None when there is none.

    Only Todo Tasks are runnable, whatever eligible_statuses says, so a claimed (In Progress) or Done Task
    is never selected again."""
    backend = backend or GitHub(load_project(directory))
    with project_lock(directory):
        task = backend.select([item for item in backend.load() if item.get("status") == TODO])
        if task is not None:
            backend.set_status(task, IN_PROGRESS)
            task = dict(task, status=IN_PROGRESS)
    return task


def run_task(directory, task, backend=None, executor=invoke):
    """Run the Project's required graph.json for an already-claimed Task (at /task); no lifecycle side effects."""
    require(isinstance(task, dict), "run-task needs a claimed Task object", "input")
    directory = Path(directory)
    graph = check_project(directory)
    project = load_project(directory)
    return Runtime(graph, project, backend=backend, executor=executor, checkout=partial(resolve, directory),
                   workspace=str(directory), task=task).run()


def complete(directory, task, backend=None):
    """Explicitly set the claimed Task's Project item to Done."""
    return (backend or GitHub(load_project(directory))).complete(task)


def read_task(path):
    """A claimed Task JSON object from a file, or stdin for "-"."""
    raw = sys.stdin.read() if str(path) == "-" else Path(path).read_text()
    task = decode(raw)
    require(isinstance(task, dict), "The claimed Task must be a JSON object", "input")
    return task


def failure_record(exc):
    return exc.record() if isinstance(exc, Failure) else {"kind": "input", "message": str(exc)}
