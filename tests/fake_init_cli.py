"""Strict, stateful external-command fixture for init; no network or AI."""
import json
import os
from pathlib import Path
import sys

name, args = Path(sys.argv[0]).name, sys.argv[1:]
mode = os.environ.get("INIT_MODE", "success")
request = json.load(sys.stdin) if name == "gh" and args[:2] == ["api", "graphql"] else None
with open(os.environ["INIT_LOG"], "a") as log:
    log.write(json.dumps({"name": name, "args": args, "request": request}) + "\n")
state_file = Path(os.environ["INIT_STATE"])
state = json.loads(state_file.read_text())


def emit(value):
    print(json.dumps(value))
    sys.exit(0)


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


if name == "git":
    if args == ["rev-parse", "--show-toplevel"]:
        print(os.environ["INIT_ROOT"])
    elif args == ["remote", "get-url", "origin"]:
        print(os.environ.get("INIT_ORIGIN", "git@github.com:o/r.git"))
    elif args == ["rev-parse", "--verify", "HEAD^{commit}"]:
        if mode == "unborn":
            fail("no commit")
        print("a" * 40)
    else:
        fail("unexpected git operation")
    sys.exit(0)
if name == "gitweave":
    if args[:2] != ["validate", "--graph"] or len(args) != 3:
        fail("AI execution forbidden")
    graph = json.loads(Path(args[2]).read_text())
    assert graph["version"] == 1 and graph["flow"] == ["work"]
    assert graph["retries"] == 0 and graph["nodes"]["work"]["workspace_base"] == 0
    assert graph["nodes"]["work"]["kind"] == "agent"
    if mode == "invalid_graph":
        fail("graph rejected")
    print("Graph passes static validation")
    sys.exit(0)
if name != "gh":
    fail("unexpected tool")
if args == ["auth", "status", "--hostname", "github.com"]:
    if mode == "auth":
        fail("not authenticated")
    sys.exit(0)
if args[:3] == ["api", "--hostname", "github.com"]:
    if args[3] == "repos/o/r":
        if mode == "repo":
            fail("repository inaccessible")
        if mode == "repo_redirect":
            emit({"full_name": "other/r"})
        if mode == "malformed":
            emit({"full_name": 123})
        emit({"full_name": "o/r"})
    if args[3] in ("users/o", "users/team"):
        if mode == "owner_type":
            emit({"type": "Bot"})
        emit({"type": state.get("owner_type", "Organization"), "node_id": "OWNER"})
if request:
    query = request["query"]
    if "projectV2(number:" in query:
        if mode == "project":
            emit({"errors": [{"message": "Project inaccessible"}]})
        kind = "user" if state.get("owner_type") == "User" else "organization"
        assert kind + "(login:" in query
        emit({"data": {kind: {"projectV2": {"id": "P"}}}})
    if "createProjectV2(" in query:
        state["projects"] += 1
        state_file.write_text(json.dumps(state))
        if mode == "create_lost":
            fail("response lost")
        if mode == "local_save":
            (Path(os.environ["INIT_ROOT"]) / ".projectweave").write_text("concurrent file")
        emit({"data": {"createProjectV2": {"projectV2": {"id": "P", "number": 9}}}})
    if "label(name:" in query:
        if mode == "label_read":
            fail("label read failed")
        label = {"name": state.get("label_name", "projectweave-ready")} if state["label"] else None
        emit({"data": {"repository": {"label": label}}})
if args == ["label", "create", "projectweave-ready", "--repo", "github.com/o/r", "--color", "0E8A16", "--description", "Explicitly eligible for ProjectWeave"]:
    if mode == "label_write":
        fail("label write denied")
    state["label"] = True
    state_file.write_text(json.dumps(state))
    if mode == "label_lost":
        fail("response lost")
    sys.exit(0)
fail("Unexpected gh operation: " + repr(args))
