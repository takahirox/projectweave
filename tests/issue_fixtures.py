"""Local GitHub metadata/content API fixture; no network or real sleeping."""
import copy
import json

from projectweave.readiness import MARKER, REVIEW_MARKER


class IssueFixture:
    def __init__(self, snapshot=None, number=71):
        self.snapshot = copy.deepcopy(snapshot or {"title": "Add feature", "body": "Missing acceptance criteria", "comments": []})
        self.number = number
        self.closed = False
        self.comments = []
        self.after_post = None
        self.body_edit = None
        self.title_event = None
        self.serial = 0
        self.calls = []
        self.page_size = 100
        self.fail_cursor = False

    def reply(self, body):
        self.serial += 1
        self.comments.append({"id": self.serial, "body": body, "edit": None})

    def edit_body(self, body):
        self.serial += 1
        self.snapshot["body"] = body
        self.body_edit = f"2026-10-04T00:00:{self.serial:02d}Z"

    def edit_title(self, title):
        self.serial += 1
        self.snapshot["title"] = title
        self.title_event = f"rename-{self.serial}"

    def edit_comment(self, identity, body):
        self.serial += 1
        comment = next(c for c in self.comments if c["id"] == identity)
        comment.update(body=body, edit=f"2026-10-04T00:01:{self.serial:02d}Z")

    def revision(self):
        return {"repository": "owner/repo", "number": self.number, "issue_id": f"issue-{self.number}",
                "body_edited_at": self.body_edit or "", "title_event": self.title_event or "",
                "comments": [{"id": c["id"], "node_id": f"comment-{c['id']}", "edited_at": c["edit"] or "",
                              "automation": c["body"].startswith((MARKER, REVIEW_MARKER))}
                             for c in sorted(self.comments, key=lambda c: c["id"])]}

    def content(self):
        return dict(self.snapshot, comments=[{"id": c["id"], "body": c["body"]} for c in self.comments
                                            if not c["body"].startswith((MARKER, REVIEW_MARKER))])

    def comment_node(self, comment, body):
        result = {"id": f"comment-{comment['id']}", "databaseId": comment["id"], "lastEditedAt": comment["edit"]}
        if body:
            result["body"] = comment["body"]
        return result

    def process(self, argv, stdin, timeout):
        self.calls.append((argv, json.loads(stdin) if stdin else None))
        if argv[4] != "graphql":
            assert argv[4].endswith(f"/issues/{self.number}/comments"), argv
            self.reply(json.loads(stdin)["body"])
            if self.after_post:
                self.after_post()
            return "{}"
        payload = json.loads(stdin)
        query, variables = payload["query"], payload["variables"]
        body = " body" in query
        if "repository(" in query:
            assert variables["number"] == self.number
            issue = {"id": f"issue-{self.number}", "state": "CLOSED" if self.closed else "OPEN",
                     "lastEditedAt": self.body_edit, "timelineItems": {"nodes": [{"id": self.title_event}] if self.title_event else []}}
            if body:
                issue.update(title=self.snapshot["title"], body=self.snapshot["body"])
            data = {"repository": {"issue": issue}}
        elif "comments(first:" in query:
            cursor = int(variables["cursor"] or 0)
            comments = self.comments[cursor:cursor + self.page_size]
            next_cursor = cursor + self.page_size
            more = next_cursor < len(self.comments)
            data = {"node": {"comments": {"nodes": [self.comment_node(c, body) for c in comments],
                    "pageInfo": {"hasNextPage": more, "endCursor": ("0" if self.fail_cursor else str(next_cursor)) if more else None}}}}
        else:
            identity = int(variables["id"].split("-")[1])
            comment = next((c for c in self.comments if c["id"] == identity), None)
            data = {"node": self.comment_node(comment, body) if comment else None}
        return json.dumps({"data": data})
