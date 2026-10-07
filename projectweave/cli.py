import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import signal
import sys
import threading
from .contracts import Failure, decode
from .coordinator import coordinate, run_one
from .dashboard import Dashboard
from .execution import ExecutionRegistry
from .graph import validate
from .readiness import execute as issue_readiness
from .review import execute as issue_review
from .routing import execute as issue_route
from .setup import add_init_arguments, init_project, init_root
from .workspace import claim, complete, failure_record, project_dir, read_task, run_graph, run_task


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def port(value):
    number = int(value)
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("must be between 0 and 65535 (0 selects an available port)")
    return number


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="projectweave")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create the root workspace (projectweave.json and projects/)")
    setup = commands.add_parser("init-project", help="Create projects/NAME/ for one GitHub Project")
    setup.add_argument("name")
    add_init_arguments(setup)
    check = commands.add_parser("validate", help="Validate a graph without external operations")
    check.add_argument("--graph", required=True, type=Path)
    readiness = commands.add_parser("issue-readiness", help="GitWeave command helper: read Issue context from stdin")
    readiness.add_argument("operation", choices=("snapshot", "comment", "wait", "guard"))
    review = commands.add_parser("issue-review", help="GitWeave command helper: preserve PR context while waiting for confirmation")
    review.add_argument("operation", choices=("snapshot", "comment", "wait", "closed"))
    commands.add_parser("issue-route", help="GitWeave command helper: route Issue labels from stdin context")
    for name, text in (("claim", "Select one runnable Task and set it In Progress"),
                       ("run-task", "Run the Project graph for an already-claimed Task"),
                       ("complete", "Set a claimed Task's Project item to Done"),
                       ("run", "Admission -> claim -> run-task for one Task, synchronously")):
        command = commands.add_parser(name, help=text)
        command.add_argument("project", help="Project directory name under projects/")
        if name in ("run-task", "complete"):
            command.add_argument("--task", required=True, help="Claimed Task JSON file, or - for stdin")
    graph_run = commands.add_parser("run-graph", help="Run an explicit Project graph without claiming a Task")
    graph_run.add_argument("project", help="Project directory name under projects/")
    graph_run.add_argument("--graph", required=True, help="Graph path relative to the Project workspace")
    graph_run.add_argument("--input", help="Operator input text, available at /input")
    loop = commands.add_parser("coordinate", help="Observe, admit and launch Tasks across all Projects")
    loop.add_argument("--once", action="store_true", help="One pass: launch what is admitted and wait for it")
    loop.add_argument("--poll-seconds", type=positive, help="Re-observe interval (default from projectweave.json, else 300)")
    loop.add_argument("--web", action="store_true", help="Serve the current process dashboard on 127.0.0.1")
    loop.add_argument("--web-port", type=port, default=8765, help="Dashboard port (default 8765; 0 selects an available port)")
    loop.add_argument("--long-running-seconds", type=positive, default=3600,
                      help="Dashboard long-running threshold (default 3600 seconds)")
    args = parser.parse_args(argv)
    if args.command in ("init", "init-project"):
        report = init_root(args) if args.command == "init" else init_project(args)
        emit(report)
        return 0 if report["initialized"] else 2
    root = Path.cwd()
    try:
        if args.command == "issue-route":
            emit(issue_route(decode(sys.stdin.read())))
            return 0
        if args.command == "issue-readiness":
            emit(issue_readiness(args.operation, decode(sys.stdin.read())))
            return 0
        if args.command == "issue-review":
            emit(issue_review(args.operation, decode(sys.stdin.read())))
            return 0
        if args.command == "validate":
            validate(decode(args.graph.read_text()))
            emit({"status": "valid"})
            return 0
        if args.command == "claim":
            emit(claim(project_dir(root, args.project)))
            return 0
        if args.command == "run-task":
            record = run_task(project_dir(root, args.project), read_task(args.task))
            emit(record)
            return 1 if record["failure"] else 0
        if args.command == "run-graph":
            record = run_graph(project_dir(root, args.project), args.graph, operator_input=args.input)
            emit(record)
            return 1 if record["failure"] else 0
        if args.command == "complete":
            emit(complete(project_dir(root, args.project), read_task(args.task)))
            return 0
        if args.command == "run":
            outcome = run_one(root, args.project)
            emit(outcome)
            return 1 if outcome.get("failure") or outcome.get("record", {}).get("failure") else 0
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Stop launching new work; Tasks already running are waited for.
            signal.signal(sig, lambda *_: stop.set())
        registry = ExecutionRegistry(args.long_running_seconds) if args.web else None
        with Dashboard(registry, args.web_port) if args.web else nullcontext() as dashboard:
            if dashboard:
                print(json.dumps({"dashboard_url": dashboard.url}), file=sys.stderr, flush=True)
            summary = coordinate(root, once=args.once, poll_seconds=args.poll_seconds, stop=stop, registry=registry,
                                 report=lambda outcome: print(json.dumps(outcome, ensure_ascii=False), file=sys.stderr, flush=True))
        emit(summary)
        return 1 if any(run.get("claim_failure") or run.get("setup_failure") or (run.get("record") or {}).get("failure")
                        for run in summary["runs"]) else 0
    except (Failure, OSError, UnicodeError, RecursionError) as exc:
        emit({"status": "failed", "failure": failure_record(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
