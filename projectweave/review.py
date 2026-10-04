"""Bridge PR review results to resident Issue confirmation commands."""
import hashlib
import json
import re
import time

from .contracts import keys, require, text
from .readiness import Issue, REVIEW_MARKER, assessed, changed, poll_seconds, revision_key


def outcome(status, pr, snapshot, revision, questions=()):
    return {"message": f"PR review confirmation: {status}", "data": {
        "pr": pr, "status": status, "approved": False, "findings": [],
        "questions": list(questions), "snapshot": snapshot, "revision": revision,
        "retry_review": status != "closed"}}


def execute(operation, context, *, clock=time.monotonic, sleep=time.sleep):
    require(isinstance(context, dict), "Review context must be an object", "input")
    issue = Issue(context)
    inputs = context.get("inputs")
    require(isinstance(inputs, list) and inputs and isinstance(inputs[0], dict)
            and isinstance(inputs[0].get("data"), dict), "Missing PR review input", "input")
    data = inputs[0]["data"]
    pr = data.get("pr")
    keys(pr, {"number", "url", "head_sha"}, {"number", "url", "head_sha"})
    require(type(pr["number"]) is int and pr["number"] > 0 and text(pr["url"])
            and isinstance(pr["head_sha"], str) and re.fullmatch(r"[0-9a-fA-F]{40}", pr["head_sha"]),
            "Invalid reviewed PR identity", "input")
    if operation == "snapshot":
        closed, snapshot, _, revision = issue.read()
        return outcome("closed" if closed else "review", pr, snapshot, revision)

    fields = {"pr", "status", "approved", "findings", "questions", "retry_review"}
    if operation in ("wait", "closed"):
        fields |= {"snapshot", "revision"}
    keys(data, fields, fields)
    require(isinstance(data["questions"], list) and all(text(q) for q in data["questions"]),
            "Invalid confirmation questions", "input")
    if operation == "closed":
        require(data["status"] == "closed" and data["approved"] is False
                and data["retry_review"] is False, "Expected closed review path", "input")
        # Match merge's skip result, so the existing terminal outcome can report closure.
        return {"message": "Issue closed before review completed; merging skipped", "data": {
            "pr": pr, "merged": False, "retry": False, "merge_commit": ""}}

    require(operation in ("comment", "wait"), "Unknown review operation", "input")
    require(data["status"] == "needs_confirmation" and data["approved"] is False
            and data["retry_review"] is True and data["findings"] == [] and data["questions"],
            "Confirmation wait requires human questions and no agent-fixable findings", "input")
    _, baseline = assessed(context, issue, agent=operation == "comment")
    if operation == "wait":
        started = clock()
        while True:
            status, snapshot, revision, _ = changed(issue, baseline)
            if status:
                return outcome(status, pr, snapshot, revision, data["questions"])
            sleep(poll_seconds(clock() - started))

    status, snapshot, revision, automation = changed(issue, baseline, automation=True)
    if status:
        return outcome(status, pr, snapshot, revision, data["questions"])
    # Include the PR head and questions: a new head or changed human check needs a new request.
    digest = hashlib.sha256(json.dumps({"pr": pr, "revision": revision_key(revision), "questions": data["questions"]},
                                      sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    marker = f"{REVIEW_MARKER}{digest} -->"
    if not any(body.startswith(marker) for body in automation):
        issue.comment(marker + f"\nPR #{pr['number']} ({pr['url']}) at head `{pr['head_sha']}` "
                      "requires human confirmation before approval. Please provide the requested results "
                      "on this Issue; a reply will trigger another review:\n\n"
                      + "\n".join(f"- {q.strip()}" for q in data["questions"]))
    # Do not recapture after posting: a simultaneous human reply must wake the wait.
    return outcome("needs_confirmation", pr, snapshot, revision, data["questions"])
