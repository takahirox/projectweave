"""Deterministic GitWeave Issue readiness commands; no model or workspace writes."""
import hashlib
import json
import time

from .checkout import valid_repository
from .contracts import Failure, decode, keys, require, text
from .executors import process

MARKER = "<!-- projectweave:issue-readiness:"
REVIEW_MARKER = "<!-- projectweave:issue-review:"


class Issue:
    def __init__(self, context):
        repository = context.get("github_repository")
        source = context.get("run_input")
        require(valid_repository(repository) and isinstance(source, dict)
                and source.get("kind") == "issue" and type(source.get("number")) is int
                and source["number"] > 0, "Issue commands require github_repository and an Issue run_input", "input")
        self.repository, self.number = repository, source["number"]
        self.endpoint = f"repos/{repository}/issues/{self.number}"
        self.comments = None

    def api(self, endpoint, *, body=None, paginate=False):
        argv = ["gh", "api", "--hostname", "github.com", endpoint]
        if paginate:
            argv += ["--paginate", "--slurp"]
        if body is not None:
            argv += ["--method", "POST", "--input", "-"]
        return decode(process(argv, json.dumps(body) if body is not None else None, 120))

    def query(self, query, variables):
        response = self.api("graphql", body={"query": query, "variables": variables})
        require(isinstance(response, dict) and not response.get("errors")
                and isinstance(response.get("data"), dict), "GitHub GraphQL request failed", "github")
        return response["data"]

    def read(self, *, content=True, baseline=None, automation=False):
        """Fetch bodies for assessment, or metadata and newly changed comment classification.

        lastEditedAt identifies body edits; the latest rename event identifies title
        edits, including rename/revert. Issue updatedAt is deliberately never used.
        Marked comment metadata is cached only within this command invocation.
        """
        owner, name = self.repository.split("/")
        fields = "id state lastEditedAt timelineItems(last:1,itemTypes:[RENAMED_TITLE_EVENT]) { nodes { ... on RenamedTitleEvent { id } } }"
        if content:
            fields += " title body"
        try:
            issue = self.query("query($owner:String!,$name:String!,$number:Int!) { repository(owner:$owner,name:$name) { issue(number:$number) { "
                               + fields + " } } }", {"owner": owner, "name": name, "number": self.number})["repository"]["issue"]
            require(isinstance(issue, dict) and text(issue.get("id"))
                    and issue.get("state") in ("OPEN", "CLOSED") and "lastEditedAt" in issue,
                    "Malformed GitHub Issue", "github")
            events = issue["timelineItems"]["nodes"]
            require(isinstance(events, list) and len(events) <= 1
                    and all(isinstance(e, dict) and text(e.get("id")) for e in events),
                    "Malformed title revision", "github")
            revision = {"repository": self.repository, "number": self.number, "issue_id": issue["id"],
                        "body_edited_at": edited_at(issue), "title_event": events[0]["id"] if events else "",
                        "comments": []}
            snapshot = {"title": issue["title"], "body": issue["body"], "comments": []} if content else None
            if snapshot is not None:
                validate_snapshot(snapshot)
            # Closure takes priority over comments, including deleted/inaccessible comments.
            if issue["state"] == "CLOSED":
                return True, snapshot, [], revision
            if self.comments is None or content:
                self.comments = {c["id"]: c for c in (baseline or {}).get("comments", [])}
            cursor, seen, bodies = None, set(), []
            while True:
                selection = "id databaseId lastEditedAt" + (" body" if content else "")
                page = self.query("query($id:ID!,$cursor:String) { node(id:$id) { ... on Issue { comments(first:100,after:$cursor) { nodes { "
                                  + selection + " } pageInfo { hasNextPage endCursor } } } } }",
                                  {"id": issue["id"], "cursor": cursor})["node"]["comments"]
                require(isinstance(page["nodes"], list) and type(page["pageInfo"]["hasNextPage"]) is bool,
                        "Malformed GitHub comment page", "github")
                for comment in page["nodes"]:
                    require(isinstance(comment, dict) and type(comment.get("databaseId")) is int
                            and text(comment.get("id")), "Malformed GitHub comment", "github")
                    record = {"id": comment["databaseId"], "node_id": comment["id"],
                              "edited_at": edited_at(comment)}
                    old = self.comments.get(record["id"])
                    # Fetch only new/edited comments to determine whether they are automation.
                    # Body and edit metadata come from the same response, avoiding a classification race.
                    if not content and (old is None or any(old[k] != record[k] for k in record)
                                        or (automation and old["automation"])):
                        comment = self.query("query($id:ID!) { node(id:$id) { ... on IssueComment { id databaseId lastEditedAt body } } }",
                                             {"id": record["node_id"]})["node"]
                        # A deletion between listing and classification is already a metadata
                        # change. Omit the vanished ID so polling can assess it or closure.
                        if comment is None:
                            continue
                        require(isinstance(comment, dict) and comment.get("id") == record["node_id"]
                                and comment.get("databaseId") == record["id"],
                                "Malformed GitHub comment identity", "github")
                        record["edited_at"] = edited_at(comment)
                    if "body" in comment:
                        require(isinstance(comment["body"], str), "Malformed GitHub comment body", "github")
                        record["automation"] = comment["body"].startswith((MARKER, REVIEW_MARKER))
                        if record["automation"]:
                            bodies.append(comment["body"])
                        elif content:
                            snapshot["comments"].append({"id": record["id"], "body": comment["body"]})
                    else:
                        record["automation"] = old["automation"]
                    revision["comments"].append(record)
                if not page["pageInfo"]["hasNextPage"]:
                    break
                cursor = page["pageInfo"]["endCursor"]
                require(text(cursor) and cursor not in seen, "Invalid or repeated GitHub cursor", "github")
                seen.add(cursor)
            revision["comments"].sort(key=lambda c: c["id"])
            validate_revision(revision, self)
            self.comments = {c["id"]: c for c in revision["comments"]}
            return False, snapshot, bodies, revision
        except (KeyError, TypeError, AttributeError) as exc:
            raise Failure("github", "Malformed GitHub revision response") from exc

    def comment(self, body):
        self.api(self.endpoint + "/comments", body={"body": body})


def edited_at(value):
    require("lastEditedAt" in value and (value["lastEditedAt"] is None or text(value["lastEditedAt"])),
            "Missing or invalid GitHub edit metadata", "github")
    return value["lastEditedAt"] or ""


def revision_key(revision):
    """Only relevant metadata; no Issue or comment body comparison or hashing."""
    return {k: v for k, v in revision.items() if k != "comments"} | {
        "comments": [{k: v for k, v in c.items() if k != "automation"}
                     for c in revision["comments"] if not c["automation"]]}


def validate_revision(revision, issue):
    fields = {"repository", "number", "issue_id", "body_edited_at", "title_event", "comments"}
    keys(revision, fields, fields)
    require(revision["repository"] == issue.repository and type(revision["number"]) is int
            and revision["number"] == issue.number and text(revision["issue_id"])
            and isinstance(revision["body_edited_at"], str) and isinstance(revision["title_event"], str)
            and isinstance(revision["comments"], list), "Invalid or foreign Issue revision", "input")
    ids = []
    for comment in revision["comments"]:
        fields = {"id", "node_id", "edited_at", "automation"}
        keys(comment, fields, fields)
        require(type(comment["id"]) is int and text(comment["node_id"])
                and isinstance(comment["edited_at"], str) and type(comment["automation"]) is bool,
                "Invalid comment revision", "input")
        ids.append(comment["id"])
    require(ids == sorted(set(ids)), "Duplicate or unordered comment revisions", "input")


def assessed(context, issue, *, agent=False):
    """The identity branch passes the Command's output around the agent unchanged.

    No mutable files or process-global baselines: inputs belong to this invocation,
    survive GitWeave checkpoint/retry, and cannot collide with another Run.
    """
    inputs = context.get("inputs")
    require(isinstance(inputs, list) and len(inputs) == (2 if agent else 1)
            and all(isinstance(i, dict) and isinstance(i.get("data"), dict) for i in inputs),
            "Missing deterministic assessed Issue baseline; update the graph template", "input")
    baseline = inputs[1 if agent else 0]["data"]
    require("revision" in baseline and "snapshot" in baseline,
            "Missing Command-captured Issue baseline", "input")
    validate_revision(baseline["revision"], issue)
    validate_snapshot(baseline["snapshot"])
    return inputs[0]["data"], baseline


def outcome(status, snapshot, revision, questions=()):
    return {"message": f"Issue readiness: {status}", "data": {
        "status": status, "questions": list(questions), "snapshot": snapshot, "revision": revision}}


def reviewed(data):
    keys(data, {"status", "questions", "diagnosis"}, {"status", "questions"})
    if "diagnosis" in data:
        require(text(data["diagnosis"]), "Invalid technical diagnosis", "input")
    require(data["status"] in ("ready", "needs_information")
            and isinstance(data["questions"], list) and all(text(q) for q in data["questions"]),
            "Invalid readiness assessment", "input")


def validate_snapshot(snapshot):
    keys(snapshot, {"title", "body", "comments"}, {"title", "body", "comments"})
    require(isinstance(snapshot["title"], str) and isinstance(snapshot["body"], str)
            and isinstance(snapshot["comments"], list), "Invalid assessed Issue snapshot", "input")
    for comment in snapshot["comments"]:
        keys(comment, {"id", "body"}, {"id", "body"})
        require(type(comment["id"]) is int and isinstance(comment["body"], str),
                "Invalid assessed comment", "input")


def changed(issue, baseline, *, automation=False):
    closed, _, bodies, revision = issue.read(content=False, baseline=baseline["revision"], automation=automation)
    if closed:
        return "closed", baseline["snapshot"], revision, bodies
    if revision_key(revision) != revision_key(baseline["revision"]):
        closed, snapshot, bodies, revision = issue.read()
        return "closed" if closed else "updated", snapshot, revision, bodies
    return None, baseline["snapshot"], baseline["revision"], bodies


def poll_seconds(elapsed):
    if elapsed < 3600:
        return 60
    if elapsed < 86400:
        return 300
    return 3600


def execute(operation, context, *, clock=time.monotonic, sleep=time.sleep):
    require(isinstance(context, dict), "Readiness context must be an object", "input")
    issue = Issue(context)
    if operation == "snapshot":
        closed, snapshot, _, revision = issue.read()
        return outcome("closed" if closed else "review", snapshot, revision)
    require(operation in ("comment", "guard", "wait"), "Unknown readiness operation", "input")
    data, baseline = assessed(context, issue, agent=operation != "wait")
    if operation == "wait":
        require(data["status"] == "needs_information", "Expected question wait", "input")
        started = clock()
        while True:
            status, snapshot, revision, _ = changed(issue, baseline)
            if status:
                return outcome(status, snapshot, revision)
            sleep(poll_seconds(clock() - started))
    reviewed(data)
    status, snapshot, revision, automation = changed(issue, baseline, automation=operation == "comment")
    if status:
        return outcome(status, snapshot, revision)
    if operation == "guard":
        require(data["status"] == "ready", "Open-state guard requires readiness approval", "input")
        result = outcome("ready", snapshot, revision)
        if "diagnosis" in data:
            result["data"]["diagnosis"] = data["diagnosis"]
        return result
    require(data["status"] == "needs_information" and data["questions"],
            "A not-ready Issue requires concrete questions", "input")
    # Hash only metadata for request idempotency, never content for update detection.
    digest = hashlib.sha256(json.dumps(revision_key(revision), sort_keys=True).encode()).hexdigest()
    marker = f"{MARKER}{digest} -->"
    if not any(body.startswith(marker) for body in automation):
        issue.comment(marker + "\nTo make this Issue ready for implementation, please clarify:\n\n"
                      + "\n".join(f"- {question.strip()}" for question in data["questions"]))
    # Keep the assessed baseline, not post-comment metadata; concurrent replies must wake the wait.
    return outcome("needs_information", snapshot, revision, data["questions"])
