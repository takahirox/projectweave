import argparse
from functools import partial
import json
from pathlib import Path
import sys
from .checkout import resolve
from .contracts import Failure, decode
from .graph import validate
from .runtime import Runtime
from .setup import add_init_arguments, initialize


def main(argv=None):
    parser = argparse.ArgumentParser(prog="projectweave")
    commands = parser.add_subparsers(dest="command", required=True)
    add_init_arguments(commands.add_parser("init", help="Prepare a Project workspace for its first Run"))
    check = commands.add_parser("validate", help="Validate a graph without external operations")
    check.add_argument("--graph", required=True, type=Path)
    run = commands.add_parser("run", help="Run one graph against a GitHub Project")
    run.add_argument("--graph", required=True, type=Path)
    run.add_argument("--project", required=True, type=Path)
    run.add_argument("--resources", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "init":
        report = initialize(args)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["initialized"] else 2
    try:
        graph = validate(decode(args.graph.read_text()))
        if args.command == "validate":
            print(json.dumps({"status": "valid"}))
            return 0
        # The directory containing project.json is the Project workspace.
        workspace = args.project.absolute().parent
        record = Runtime(graph, decode(args.project.read_text()), decode(args.resources.read_text()),
                         checkout=partial(resolve, workspace), workspace=str(workspace)).run()
        print(json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2))
        return 1 if record["failure"] else 0
    except (Failure, OSError, UnicodeError, RecursionError) as exc:
        failure = exc.record() if isinstance(exc, Failure) else {"kind": "input", "message": str(exc)}
        print(json.dumps({"status": "failed", "failure": failure}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
