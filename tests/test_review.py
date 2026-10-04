import asyncio
import copy
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from projectweave.cli import main
from projectweave.contracts import Failure
from projectweave.readiness import Issue, MARKER, REVIEW_MARKER
from projectweave.review import execute, outcome
from projectweave.setup import compatible, templates

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = {"github_repository": "owner/repo", "run_input": {"kind": "issue", "number": 84}}
PR = {"number": 7, "url": "https://github.com/owner/repo/pull/7", "head_sha": "a" * 40}
SNAPSHOT = {"title": "Feature", "body": "Human device confirmation required", "comments": []}
QUESTION = "Please confirm playback on the device at the reviewed head, with the observed result."


from issue_fixtures import IssueFixture as GitHubFixture


class IssueFixture(GitHubFixture):
    def __init__(self):
        super().__init__(SNAPSHOT, number=84)


def context(snapshot=SNAPSHOT, pr=PR):
    baseline = outcome("review", copy.deepcopy(pr), copy.deepcopy(snapshot), IssueFixture().revision())
    data = {"pr": copy.deepcopy(pr), "status": "needs_confirmation", "approved": False,
            "findings": [], "questions": [QUESTION], "retry_review": True}
    return dict(CONTEXT, inputs=[{"data": data}, baseline])


class ReviewCommandTests(unittest.TestCase):
    def setUp(self):
        self.fixture = IssueFixture()
        self.patch = patch("projectweave.readiness.process", self.fixture.process)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_unchanged_polls_ignore_both_markers_and_preserve_pr(self):
        handoff = execute("comment", context())
        self.fixture.reply(MARKER + "readiness -->\nQuestion")
        self.fixture.reply(REVIEW_MARKER + "other -->\nQuestion")
        polls = []
        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 3:
                self.fixture.reply("Confirmed: playback succeeded")
        result = execute("wait", dict(CONTEXT, inputs=[handoff]), clock=lambda: 0, sleep=sleep)["data"]
        self.assertEqual(polls, [60] * 3)
        self.assertEqual(result["pr"], PR)
        self.assertEqual(result["status"], "updated")
        self.assertFalse(result["approved"])
        self.assertEqual(result["snapshot"]["comments"], [{"id": 4, "body": "Confirmed: playback succeeded"}])

    def test_request_deduplicates_but_new_head_gets_new_request(self):
        first = execute("comment", context())
        execute("comment", context())
        self.assertEqual(len(self.fixture.comments), 1)
        self.assertIn(PR["url"], self.fixture.comments[0]["body"])
        self.assertIn(PR["head_sha"], self.fixture.comments[0]["body"])
        self.assertIn(QUESTION, self.fixture.comments[0]["body"])
        updated_pr = dict(PR, head_sha="b" * 40)
        execute("comment", context(pr=updated_pr))
        self.assertEqual(len(self.fixture.comments), 2)
        self.assertEqual(first["data"]["snapshot"], SNAPSHOT)

    def test_serialized_retry_new_questions_and_run_isolation(self):
        run_a = context()
        execute("comment", run_a)
        execute("comment", json.loads(json.dumps(run_a)))
        self.assertEqual(len(self.fixture.comments), 1)
        run_b = context()
        run_b["inputs"][0]["data"]["questions"] = ["A different necessary check"]
        execute("comment", run_b)
        self.assertEqual(len(self.fixture.comments), 2)
        self.fixture.edit_body("Changed confirmation requirements")
        run_b["inputs"][1] = execute("snapshot", context())
        self.assertEqual(execute("comment", run_b)["data"]["status"], "needs_confirmation")
        self.assertEqual(execute("comment", run_a)["data"]["status"], "updated")
        self.assertEqual(len(self.fixture.comments), 3)
        for target in (dict(CONTEXT, github_repository="owner/other"), dict(CONTEXT, run_input={"kind": "issue", "number": 85})):
            with self.assertRaises(Failure):
                execute("comment", dict(target, inputs=run_a["inputs"]))

    def test_reply_during_review_or_post_handoff_is_not_lost(self):
        for during_post in (False, True):
            with self.subTest(during_post=during_post):
                self.fixture.comments = []
                if during_post:
                    self.fixture.after_post = lambda: self.fixture.reply("Confirmed")
                else:
                    self.fixture.reply("Confirmed")
                handoff = execute("comment", context())
                if during_post:
                    sleep = Mock()
                    result = execute("wait", dict(CONTEXT, inputs=[handoff]), sleep=sleep)
                    sleep.assert_not_called()
                else:
                    result = handoff
                    self.assertEqual(len(self.fixture.comments), 1)  # No stale request posted.
                self.assertEqual(result["data"]["status"], "updated")
                self.assertEqual(result["data"]["pr"], PR)
                self.assertEqual(result["data"]["snapshot"]["comments"][-1]["body"], "Confirmed")

    def test_closure_at_snapshot_request_or_wait_skips_merge(self):
        for operation in ("snapshot", "comment", "wait"):
            with self.subTest(operation=operation):
                self.fixture.closed = True
                value = context()
                if operation == "wait":
                    value = dict(CONTEXT, inputs=[outcome("needs_confirmation", PR, SNAPSHOT, self.fixture.revision(), [QUESTION])])
                result = execute(operation, value, sleep=Mock())
                self.assertEqual(result["data"]["status"], "closed")
                self.assertEqual(result["data"]["pr"], PR)
                self.assertFalse(result["data"]["retry_review"])
                ended = execute("closed", dict(CONTEXT, inputs=[result]))["data"]
                self.assertEqual(ended, {"pr": PR, "merged": False, "retry": False, "merge_commit": ""})
                self.assertFalse(self.fixture.comments)

    def test_invalid_human_path_and_github_failure_do_not_post(self):
        for edit in ({"questions": []}, {"findings": ["Fix defect"]}, {"approved": True},
                     {"status": "needs_fixes"}, {"retry_review": False},
                     {"pr": dict(PR, head_sha="invalid")}):
            value = context()
            value["inputs"][0]["data"].update(edit)
            with self.subTest(edit=edit), self.assertRaises(Failure):
                execute("comment", value)
        self.assertFalse(self.fixture.comments)
        with patch.object(Issue, "read", side_effect=Failure("github", "Denied")) as read:
            with self.assertRaises(Failure):
                execute("wait", dict(CONTEXT, inputs=[outcome("needs_confirmation", PR, SNAPSHOT, self.fixture.revision(), [QUESTION])]))
            read.assert_called_once()

    def test_cli_and_existing_custom_provider_settings(self):
        with patch("sys.stdin", io.StringIO(json.dumps(context()))), \
                patch("sys.stdout", new=io.StringIO()) as stdout:
            self.assertEqual(main(["issue-review", "snapshot"]), 0)
            self.assertEqual(json.loads(stdout.getvalue())["data"]["pr"], PR)
        expected = templates(ROOT, "claude", "chosen-model")["gitweave.json"]
        customized = copy.deepcopy(expected)
        customized["nodes"]["review"].update(model="custom", effort="high")
        compatible("gitweave.json", customized, expected)
        for node in expected["nodes"].values():
            if node["kind"] == "command":
                self.assertFalse({"provider", "model", "effort"} & node.keys())


@unittest.skipUnless(os.environ.get("GITWEAVE_SOURCE"), "Optional public GitWeave runtime review checks")
class ReviewRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.environ["GITWEAVE_SOURCE"])
        cls.runtime_module = importlib.import_module("gitweave.runtime")
        cls.model = importlib.import_module("gitweave.model")

    def run_review(self, *, updates=(), defect=False, handoff=None, merge_results=(True,)):
        fixture = IssueFixture()
        graph = templates(ROOT)["gitweave.json"]
        runtime = self.runtime_module.Runtime.__new__(self.runtime_module.Runtime)
        runtime.graph = self.runtime_module.validate_graph(graph)
        runtime.steps, runtime.visits, runtime.stopped, runtime.invocations = 0, 0, False, {}
        called, polls, reviews = [], [], []
        changes, merges = iter(updates), iter(merge_results)
        current_pr = copy.deepcopy(PR)
        merge_calls = []

        def sleep(seconds):
            # Each unchanged poll remains inside the same command invocation.
            polls.append((runtime.steps, list(called)))
            update = next(changes)
            if update == "close":
                fixture.closed = True
            elif update is not None:
                fixture.reply(update)

        async def node(name, inputs, item, origin, invocation=None):
            if len(inspect.signature(runtime.tick).parameters):
                runtime.tick(invocation or str(len(called)))
            else:
                runtime.tick()
            called.append(name)
            spec = graph["nodes"][name]
            if spec["kind"] == "command":
                data = execute(spec["argv"][-1], dict(CONTEXT, inputs=inputs),
                               clock=lambda: 0, sleep=sleep)["data"]
            elif name == "review":
                snapshot = inputs[0]["data"]["snapshot"]
                reviews.append(copy.deepcopy(inputs[0]["data"]))
                self.assertNotIn("snapshot", spec["schema"]["properties"])
                self.assertNotIn("revision", spec["schema"]["properties"])
                confirmed = any(c["body"] == "Confirmed: playback succeeded" for c in snapshot["comments"])
                status = "needs_fixes" if defect and len(reviews) == 1 else "approved" if confirmed else "needs_confirmation"
                data = {"pr": copy.deepcopy(current_pr), "status": status,
                        "questions": [] if status == "approved" else [QUESTION]}
                data["approved"] = status == "approved"
                data["retry_review"] = status != "approved"
                data["findings"] = ["Fix playback bug"] if status == "needs_fixes" else []
                if handoff == "review" and len(reviews) == 1:
                    fixture.reply("Confirmed: playback succeeded")
            elif name == "fix":
                self.assertEqual(inputs[0]["data"]["findings"], ["Fix playback bug"])
                self.assertEqual(inputs[0]["data"]["questions"], [QUESTION])
                data = {"pr": inputs[0]["data"]["pr"], "summary": "Fixed playback"}
            elif name == "publish":
                current_pr["head_sha"] = "b" * 40
                data = {"pr": copy.deepcopy(current_pr)}
            elif name == "merge":
                merge_calls.append(copy.deepcopy(inputs[0]["data"]["pr"]))
                merged = next(merges)
                data = {"pr": current_pr, "merged": merged, "retry": not merged,
                        "merge_commit": "c" * 40 if merged else ""}
            elif name == "close_issue":
                data = {k: inputs[0]["data"][k] for k in ("pr", "merged", "merge_commit")}
                data["closed"] = True
            else:
                self.fail(f"Unexpected node {name}")
            self.model.validate(data, spec["schema"])
            return {"node_id": name, "commit": "d" * 40, "message": name,
                    "data": data, "data_validated": True}

        runtime.node = node
        if handoff == "post":
            fixture.after_post = lambda: fixture.reply("Confirmed: playback succeeded")
        flow = graph["flow"][2]["if"]["then"][2:]
        with patch("projectweave.readiness.process", fixture.process):
            result = asyncio.run(runtime.flow(flow, [{"data": {"pr": PR}, "data_validated": True}]))
        return called, polls, reviews, fixture, merge_calls, result[0]["data"]

    def test_unchanged_polling_then_sufficient_confirmation(self):
        called, polls, reviews, fixture, merges, result = self.run_review(
            updates=[None, None, None, "Confirmed: playback succeeded"])
        self.assertEqual(len(reviews), 2)
        self.assertEqual(called.count("wait_for_confirmation"), 1)
        self.assertNotIn("fix", called)
        self.assertNotIn("publish", called)
        self.assertEqual(len({p[0] for p in polls}), 1)
        self.assertTrue(all(p[1].count("review") == 1 for p in polls))
        self.assertEqual(merges, [PR])
        self.assertEqual(reviews[1]["pr"], PR)
        self.assertTrue(result["merged"])

    def test_insufficient_or_unrelated_reply_returns_to_wait(self):
        for reply in ("I will test later", "Unrelated update"):
            with self.subTest(reply=reply):
                called, polls, reviews, fixture, merges, result = self.run_review(
                    updates=[reply, None, "Confirmed: playback succeeded"])
                self.assertEqual(len(reviews), 3)
                self.assertEqual(called.count("wait_for_confirmation"), 2)
                self.assertNotIn("fix", called)
                self.assertNotIn("publish", called)
                self.assertEqual(reviews[1]["snapshot"]["comments"][0]["body"], reply)
                self.assertEqual(merges, [PR])

    def test_reply_during_review_and_request_handoff(self):
        for handoff in ("review", "post"):
            with self.subTest(handoff=handoff):
                called, polls, reviews, fixture, merges, result = self.run_review(handoff=handoff)
                self.assertEqual(len(reviews), 2)
                self.assertFalse(polls)
                self.assertEqual(merges, [PR])
                self.assertEqual(called.count("wait_for_confirmation"), int(handoff == "post"))

    def test_issue_closure_while_waiting_stops_review_and_merge(self):
        called, polls, reviews, fixture, merges, result = self.run_review(updates=[None, "close"])
        self.assertEqual(len(reviews), 1)
        self.assertNotIn("fix", called)
        self.assertNotIn("publish", called)
        self.assertFalse(merges)
        self.assertFalse(result["merged"])
        self.assertTrue(result["closed"])
        self.assertEqual(result["pr"], PR)
        self.assertEqual(result["merge_commit"], "")

    def test_code_fix_then_wait_retains_updated_head(self):
        called, polls, reviews, fixture, merges, result = self.run_review(
            defect=True, updates=[None, "Confirmed: playback succeeded"])
        updated = dict(PR, head_sha="b" * 40)
        self.assertEqual(called.count("fix"), 1)
        self.assertEqual(called.count("publish"), 1)
        self.assertLess(called.index("publish"), called.index("request_confirmation"))
        self.assertEqual(reviews[1]["pr"], updated)
        self.assertEqual(reviews[2]["pr"], updated)
        self.assertEqual(merges, [updated])
        self.assertEqual(result["pr"], updated)
        self.assertIn(updated["head_sha"], fixture.comments[0]["body"])

    def test_failed_merge_rechecks_review_before_retry(self):
        called, polls, reviews, fixture, merges, result = self.run_review(
            updates=["Confirmed: playback succeeded"], merge_results=[False, True])
        self.assertEqual(len(reviews), 3)
        self.assertEqual(merges, [PR, PR])
        self.assertTrue(result["merged"])
