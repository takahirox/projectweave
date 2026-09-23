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
    if "fields(first:" in query:
        assert "... on ProjectV2FieldCommon { name dataType }" in query
        cursor = request["variables"]["cursor"]
        if mode == "field_read" or (mode == "field_page" and cursor):
            fail("field read denied")
        # Priority is deliberately on the final page. Status is unrelated.
        fields = state.get("fields", []) if cursor else state.get("first_fields", [
            {"__typename": "ProjectV2Field", "name": "Status", "dataType": "TEXT"}])
        emit({"data": {"node": {"fields": {"nodes": fields,
            "pageInfo": {"hasNextPage": cursor is None, "endCursor": "last" if cursor is None else None}}}}})
    if "createProjectV2Field(" in query:
        assert query.startswith("mutation($input:CreateProjectV2FieldInput!)")
        field_input = request["variables"]["input"]
        options = {"Priority": ["P0", "P1", "P2"], "AI execution": ["Ready", "Not ready"]}[field_input["name"]]
        assert field_input == {"projectId": "P", "name": field_input["name"], "dataType": "SINGLE_SELECT",
            "singleSelectOptions": [{"name": n, "color": "GRAY", "description": ""} for n in options]}
        assert not any(f["name"] == field_input["name"] for f in state.get("fields", [])), "duplicate field"
        if mode == "field_write":
            fail("field write denied")
        field = {"__typename": "ProjectV2SingleSelectField", "id": "F", "name": field_input["name"],
                 "dataType": "SINGLE_SELECT", "options": [{"name": n} for n in options]}
        state.setdefault("fields", []).append(field)
        state_file.write_text(json.dumps(state))
        if mode == "field_lost":
            fail("response lost")
        emit({"data": {"createProjectV2Field": {"projectV2Field": field}}})
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
fail("Unexpected gh operation: " + repr(args))
