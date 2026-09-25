"""Deterministic external CLI doubles; never contacts GitHub or AI services."""
import json
import os
from pathlib import Path
import sys

mode = os.environ.get("FAKE_MODE", "success")
name = Path(sys.argv[0]).name
args = sys.argv[1:]
request = json.load(sys.stdin) if name == "worker" or (name == "gh" and args[:2] == ["api", "graphql"]) else None
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"command": name, "argv": args, "request": request, "cwd": os.getcwd()}) + "\n")
usage = os.environ.get("FAKE_USAGE", "")
if name == "claude":
    # claude -p --output-format json /usage: the plan limits as text, no model call.
    assert args == ["-p", "--output-format", "json", "/usage"], args
    if usage == "claude_failure":
        print("not logged in", file=sys.stderr)
        sys.exit(1)
    session, week, fable = os.environ.get("FAKE_CLAUDE_USED", "10,20,30").split(",")
    lines = ["You are currently using your subscription to power your Claude Code usage", "",
             f"Current session: {session}% used · resets Sep 25 at 4:40pm (Asia/Tokyo)",
             f"Current week (all models): {week}% used · resets Sep 29 at 12pm (Asia/Tokyo)",
             f"Current week (Fable): {fable}% used · resets Sep 29 at 12pm (Asia/Tokyo)", "",
             "What's contributing to your limits usage?", "  94% of your usage came from long sessions"]
    if usage == "claude_no_week":
        lines = [line for line in lines if "all models" not in line]
    print(json.dumps({"type": "result", "is_error": False, "num_turns": 0, "result": "\n".join(lines)}))
    sys.exit(0)
if name == "codex":
    # codex app-server: newline-delimited JSON-RPC; it only answers while stdin stays open.
    assert args == ["app-server"], args
    for line in sys.stdin:
        message = json.loads(line)
        if usage == "codex_hang":
            continue
        if message.get("id") == 1:
            print(json.dumps({"id": 1, "result": {"userAgent": "fake"}}), flush=True)
            print(json.dumps({"method": "account/updated", "params": {}}), flush=True)
        elif message.get("method") == "account/rateLimits/read":
            if usage == "codex_error":
                reply = {"id": message["id"], "error": {"code": -32000, "message": "not signed in"}}
            else:
                primary = None if usage == "codex_no_primary" else {
                    "usedPercent": float(os.environ.get("FAKE_CODEX_USED", "50")), "windowDurationMins": 10080, "resetsAt": 1}
                reply = {"id": message["id"], "result": {"rateLimits": {"limitId": "codex", "primary": primary, "secondary": None}}}
            print(json.dumps(reply), flush=True)
    sys.exit(0)
if name == "gh" and args[:2] == ["repo", "clone"]:
    # A clone records its origin in a marker file that the fake git reads back.
    if mode == "clone_failure":
        print("clone denied", file=sys.stderr)
        sys.exit(1)
    Path(args[3]).mkdir()
    (Path(args[3]) / ".fake-origin").write_text("https://github.com/" + args[2].removeprefix("github.com/") + ".git\n")
    sys.exit(0)
if name == "git":
    assert args[0] == "-C", args
    origin = Path(args[1]) / ".fake-origin"
    if args[2:] == ["rev-parse", "--show-toplevel"] and origin.exists():
        print(args[1])
    elif args[2:] == ["remote", "get-url", "origin"] and origin.exists():
        print(origin.read_text(), end="")
    elif args[2:] == ["fetch", "origin"] and mode != "fetch_failure":
        pass
    elif args[2:] == ["rev-parse", "--verify", "origin/HEAD^{commit}"]:
        print("f" * 40)
    else:
        print("fatal: git operation failed", file=sys.stderr)
        sys.exit(128)
    sys.exit(0)
if name in ("gitweave", "worker") and mode == "executor_failure":
    print("usage limit reached", file=sys.stderr)
    sys.exit(1)
if name == "gitweave":
    print(json.dumps({"run_id": "GW", "status": "completed", "repository": "/repo", "run_ref": "refs/gitweave/GW/run",
                      "notes_ref": "refs/notes/gitweave/GW", "outputs": [
                          {"commit": "abc123", "message": "Merged and closed", "data": {
                              "pr": {"number": 12, "url": "https://github.com/o/r/pull/12", "head_sha": "def456"},
                              "merged": True, "merge_commit": "fed789", "closed": True}}]}))
    sys.exit(0)
if name == "worker":
    print(json.dumps({"message": "Agent finished", "data": {"approved": True}, "references": ["artifact"], "usage": {}}))
    sys.exit(0)
query, variables = request["query"], request["variables"]
if mode == "graphql_error":
    print(json.dumps({"data": {}, "errors": [{"message": "permission denied"}]}))
    sys.exit(0)


def connection(field, nodes, more=False):
    cursor = "next" if more else None
    print(json.dumps({"data": {"node": {field: {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}}))


if "projectV2(number:" in query:
    print(json.dumps({"data": {"organization": {"projectV2": {"id": "P"}}}}))
elif "items(first:" in query:
    if mode == "empty":
        connection("items", [])
    elif variables["cursor"] is None:
        connection("items", [
            {"id": "draft", "isArchived": False, "content": {"__typename": "DraftIssue"}},
            {"id": "pr", "isArchived": False, "content": {"__typename": "PullRequest"}},
            {"id": "private", "isArchived": False, "content": None},
            {"id": "closed", "isArchived": False, "content": {"__typename": "Issue", "state": "CLOSED"}},
            {"id": "archived", "isArchived": True, "content": {"__typename": "Issue"}}
        ], True)
    else:
        connection("items", [{"id": "ITEM", "isArchived": False, "content": {
            "__typename": "Issue", "id": "I", "number": 7, "title": "Task", "body": "task $(literal) `literal`",
            "url": "https://github.com/o/r/issues/7", "state": "OPEN", "createdAt": "2026-01-01T00:00:00Z",
            "repository": {"nameWithOwner": "o/r"}}}])
elif "labels(first:" in query:
    connection("labels", [{"name": "other"}] if variables["cursor"] is None else [{"name": "projectweave-ready"}], variables["cursor"] is None)
elif "fieldValues(first:" in query:
    values = [("Status", "Todo")] if variables["cursor"] is None else [("Priority", "P0"), ("AI execution", mode != "not_ready" and "Ready" or "Not ready")]
    connection("fieldValues", [{"field": {"name": f}, "name": v} for f, v in values], variables["cursor"] is None)
elif "fields(first:" in query:
    connection("fields", [{}] if variables["cursor"] is None else [
        {"id": "STATUS", "name": "Status", "options": [{"id": "PROGRESS", "name": "In Progress"}, {"id": "DONE", "name": "Done"}]}], variables["cursor"] is None)
elif "addComment(" in query:
    print(json.dumps({"data": {"addComment": {"commentEdge": {"node": {"url": "https://github.com/o/r/issues/7#comment"}}}}}))
elif "updateProjectV2ItemFieldValue(" in query:
    if mode == "writeback_failure" and variables["option"] == "DONE":
        print(json.dumps({"errors": [{"message": "update denied"}]}))
    elif mode == "status_failure" and variables["option"] == "PROGRESS":
        print(json.dumps({"errors": [{"message": "update denied"}]}))
    else:
        print(json.dumps({"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM"}}}}))
else:
    print("Unexpected GraphQL operation", file=sys.stderr)
    sys.exit(3)
