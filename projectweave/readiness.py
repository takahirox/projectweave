"""Deterministic GitWeave Issue readiness commands; no model or workspace writes."""
import hashlib
import json
import time

from .checkout import valid_repository
from .contracts import decode, equal, keys, require, text
from .executors import process

MARKER = "<!-- projectweave:issue-readiness:"


class Issue:
    def __init__(self, context):
        repository = context.get("github_repository")
        source = context.get("run_input")
        require(valid_repository(repository) and isinstance(source, dict)
                and source.get("kind") == "issue" and type(source.get("number")) is int
                and source["number"] > 0, "Readiness requires github_repository and an Issue run_input", "input")
        self.endpoint = f"repos/{repository}/issues/{source['number']}"

    def api(self, endpoint, *, body=None, paginate=False):
        argv = ["gh", "api", "--hostname", "github.com", endpoint]
        if paginate:
            argv += ["--paginate", "--slurp"]
        if body is not None:
            argv += ["--method", "POST", "--input", "-"]
        return decode(process(argv, json.dumps(body) if body is not None else None, 120))

    def read(self):
        issue = self.api(self.endpoint)
        require(isinstance(issue, dict) and issue.get("state") in ("open", "closed")
                and isinstance(issue.get("title"), str)
                and (issue.get("body") is None or isinstance(issue["body"], str))
                and "pull_request" not in issue, "Malformed GitHub Issue", "github")
        snapshot = {"title": issue["title"], "body": issue.get("body") or "", "comments": []}
        # Closure takes priority over comments, including deleted/inaccessible comments.
        if issue["state"] == "closed":
            return True, snapshot, []
        pages = self.api(self.endpoint + "/comments?per_page=100", paginate=True)
        require(isinstance(pages, list) and all(isinstance(page, list) for page in pages),
                "Malformed GitHub comment pages", "github")
        automation = []
        for page in pages:
            for comment in page:
                require(isinstance(comment, dict) and type(comment.get("id")) is int
                        and isinstance(comment.get("body"), str), "Malformed GitHub comment", "github")
                body = comment["body"]
                if body.startswith(MARKER):
                    automation.append(body)
                else:
                    # Do not exclude the authenticated account: humans can use it too.
                    snapshot["comments"].append({"id": comment["id"], "body": body})
        return False, snapshot, automation

    def comment(self, body):
        self.api(self.endpoint + "/comments", body={"body": body})


def outcome(status, snapshot, questions=()):
    return {"message": f"Issue readiness: {status}", "data": {
        "status": status, "questions": list(questions), "snapshot": snapshot}}


def reviewed(context):
    inputs = context.get("inputs")
    require(isinstance(inputs, list) and inputs and isinstance(inputs[0], dict)
            and isinstance(inputs[0].get("data"), dict), "Missing reviewed Issue snapshot", "input")
    data = inputs[0]["data"]
    keys(data, {"status", "questions", "snapshot"}, {"status", "questions", "snapshot"})
    snapshot = data["snapshot"]
    keys(snapshot, {"title", "body", "comments"}, {"title", "body", "comments"})
    require(isinstance(snapshot["title"], str) and isinstance(snapshot["body"], str)
            and isinstance(snapshot["comments"], list), "Invalid reviewed Issue snapshot", "input")
    for comment in snapshot["comments"]:
        keys(comment, {"id", "body"}, {"id", "body"})
        require(type(comment["id"]) is int and isinstance(comment["body"], str),
                "Invalid reviewed comment", "input")
    require(isinstance(data["questions"], list) and all(text(q) for q in data["questions"]),
            "Invalid readiness questions", "input")
    return data


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
        closed, snapshot, _ = issue.read()
        return outcome("closed" if closed else "review", snapshot)
    data = reviewed(context)
    baseline = data["snapshot"]
    if operation == "wait":
        started = clock()
        while True:
            closed, snapshot, _ = issue.read()
            if closed:
                return outcome("closed", snapshot)
            if not equal(snapshot, baseline):
                return outcome("updated", snapshot)
            sleep(poll_seconds(clock() - started))
    require(operation in ("comment", "guard"), "Unknown readiness operation", "input")
    closed, snapshot, automation = issue.read()
    if closed:
        return outcome("closed", snapshot)
    if not equal(snapshot, baseline):
        # Preserve changes arriving during the review/comment handoff and re-review them.
        return outcome("updated", snapshot)
    if operation == "guard":
        require(data["status"] == "ready", "Open-state guard requires readiness approval", "input")
        return outcome("ready", baseline)
    require(data["status"] == "needs_information" and data["questions"],
            "A not-ready Issue requires concrete questions", "input")
    digest = hashlib.sha256(json.dumps(baseline, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    marker = f"{MARKER}{digest} -->"
    if not any(body.startswith(marker) for body in automation):
        issue.comment(marker + "\nTo make this Issue ready for implementation, please clarify:\n\n"
                      + "\n".join(f"- {question.strip()}" for question in data["questions"]))
    # Keep the reviewed baseline, not a post-comment snapshot; a concurrent reply must wake the wait.
    return outcome("needs_information", baseline, data["questions"])
