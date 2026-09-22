"""Deterministic external CLI doubles; never contacts GitHub or AI services."""
import json
import os
from pathlib import Path
import sys

mode = os.environ.get("FAKE_MODE", "success")
name = Path(sys.argv[0]).name
request = json.load(sys.stdin) if name in ("gh", "worker") else None
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"command": name, "argv": sys.argv[1:], "request": request}) + "\n")
if name in ("gitweave", "worker") and mode == "executor_failure":
    print("usage limit reached", file=sys.stderr)
    sys.exit(1)
if name == "gitweave":
    print(json.dumps({"run_id": "GW", "status": "completed", "repository": "/repo", "run_ref": "refs/gitweave/GW/run",
                      "notes_ref": "refs/notes/gitweave/GW", "outputs": [
                          {"commit": "abc123", "message": "Task rejected", "data": {"approved": False}}]}))
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
    field, value = ("Status", "Todo") if variables["cursor"] is None else ("Priority", "P0")
    connection("fieldValues", [{"field": {"name": field}, "name": value}], variables["cursor"] is None)
elif "fields(first:" in query:
    connection("fields", [{}] if variables["cursor"] is None else [
        {"id": "STATUS", "name": "Status", "options": [{"id": "DONE", "name": "Done"}]}], variables["cursor"] is None)
elif "addComment(" in query:
    print(json.dumps({"data": {"addComment": {"commentEdge": {"node": {"url": "https://github.com/o/r/issues/7#comment"}}}}}))
elif "updateProjectV2ItemFieldValue(" in query:
    if mode == "writeback_failure":
        print(json.dumps({"errors": [{"message": "update denied"}]}))
    else:
        print(json.dumps({"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM"}}}}))
else:
    print("Unexpected GraphQL operation", file=sys.stderr)
    sys.exit(3)
