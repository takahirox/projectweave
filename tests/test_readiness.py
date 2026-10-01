import asyncio
import copy
import importlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from projectweave.cli import main
from projectweave.contracts import Failure
from projectweave.graph import executor
from projectweave.readiness import Issue, MARKER, execute, outcome, poll_seconds
from projectweave.routing import execute as route_issue
from projectweave.setup import compatible, templates

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = {"title": "Add feature", "body": "Missing acceptance criteria", "comments": []}
CONTEXT = {"github_repository": "owner/repo", "run_input": {"kind": "issue", "number": 71}}


def context(status="needs_information", snapshot=SNAPSHOT, questions=("What behavior is expected?",)):
    return dict(CONTEXT, inputs=[{"data": outcome(status, copy.deepcopy(snapshot), questions)["data"]}])


class ReadinessTests(unittest.TestCase):
    def test_github_reads_all_comment_pages_and_ignores_only_marked_comments(self):
        issue = {"state": "open", "title": "Title", "body": None, "updated_at": "irrelevant"}
        pages = [[{"id": 1, "body": MARKER + "abc -->\nQuestion", "user": {"login": "owner"}}],
                 [{"id": 2, "body": "Human reply", "user": {"login": "owner"}}]]
        with patch("projectweave.readiness.process", side_effect=[json.dumps(issue), json.dumps(pages)]) as process:
            closed, snapshot, automation = Issue(CONTEXT).read()
        self.assertFalse(closed)
        self.assertEqual(snapshot, {"title": "Title", "body": "", "comments": [{"id": 2, "body": "Human reply"}]})
        self.assertEqual(automation, [pages[0][0]["body"]])
        self.assertEqual(process.call_args_list[1].args,
                         (["gh", "api", "--hostname", "github.com", "repos/owner/repo/issues/71/comments?per_page=100",
                           "--paginate", "--slurp"], None, 120))

    def test_post_uses_json_stdin_not_shell_interpolation(self):
        body = "Question with `quotes`, $(commands), and\nnewlines"
        with patch("projectweave.readiness.process", return_value="{}") as process:
            Issue(CONTEXT).comment(body)
        argv, stdin, timeout = process.call_args.args
        self.assertEqual(argv[-4:], ["--method", "POST", "--input", "-"])
        self.assertEqual(json.loads(stdin), {"body": body})
        self.assertEqual(timeout, 120)

    def test_closed_snapshot_skips_comments_and_invalid_inputs_fail(self):
        with patch("projectweave.readiness.process", return_value=json.dumps({"state": "closed", "title": "T", "body": "B"})) as process:
            self.assertEqual(execute("snapshot", CONTEXT)["data"]["status"], "closed")
            process.assert_called_once()
        for invalid in ({}, dict(CONTEXT, github_repository="../repo"),
                        dict(CONTEXT, run_input={"kind": "pull_request", "number": 71}),
                        dict(CONTEXT, run_input={"kind": "issue", "number": True})):
            with self.subTest(invalid=invalid), self.assertRaises(Failure):
                execute("snapshot", invalid)

    def test_comment_deduplication_and_reviewed_snapshot_handoff(self):
        with patch.object(Issue, "read", return_value=(False, copy.deepcopy(SNAPSHOT), [])), \
                patch.object(Issue, "comment") as post:
            first = execute("comment", context())
        marker_body = post.call_args.args[0]
        self.assertIn("- What behavior is expected?", marker_body)
        self.assertTrue(marker_body.startswith(MARKER))
        self.assertEqual(first["data"]["snapshot"], SNAPSHOT)
        with patch.object(Issue, "read", return_value=(False, copy.deepcopy(SNAPSHOT), [marker_body])), \
                patch.object(Issue, "comment") as post:
            # Different question wording on unchanged content still cannot duplicate the comment.
            execute("comment", context(questions=("Please specify acceptance criteria",)))
        post.assert_not_called()
        changed = dict(SNAPSHOT, body="Changed requirements")
        with patch.object(Issue, "read", return_value=(False, changed, [marker_body])), \
                patch.object(Issue, "comment") as post:
            execute("comment", context(snapshot=changed))
        post.assert_called_once()

    def test_updates_during_review_are_not_overwritten_or_commented_on(self):
        updated = dict(SNAPSHOT, comments=[{"id": 3, "body": "Here are the criteria"}])
        for operation in ("comment", "guard"):
            with self.subTest(operation=operation), \
                    patch.object(Issue, "read", return_value=(False, updated, [])), \
                    patch.object(Issue, "comment") as post:
                result = execute(operation, context("ready" if operation == "guard" else "needs_information"))
                self.assertEqual(result["data"]["status"], "updated")
                self.assertEqual(result["data"]["snapshot"], updated)
                post.assert_not_called()

    def test_wait_resumes_for_title_body_comment_edits_additions_and_deletions(self):
        with_comment = dict(SNAPSHOT, comments=[{"id": 2, "body": "Answer"}])
        pairs = [(SNAPSHOT, dict(SNAPSHOT, title="Updated title")),
                 (SNAPSHOT, dict(SNAPSHOT, body="Updated body")), (SNAPSHOT, with_comment),
                 (with_comment, dict(SNAPSHOT, comments=[{"id": 2, "body": "Edited answer"}])),
                 (with_comment, SNAPSHOT)]
        for before, after in pairs:
            with self.subTest(after=after), patch.object(Issue, "read", side_effect=[
                    (False, before, [MARKER + "own comment"]), (False, before, []), (False, after, [])]):
                sleep = Mock()
                result = execute("wait", context(snapshot=before), clock=lambda: 0, sleep=sleep)
                self.assertEqual(result["data"]["status"], "updated")
                self.assertEqual(result["data"]["snapshot"], after)
                self.assertEqual(sleep.call_count, 2)
                sleep.assert_called_with(60)

    def test_reply_between_comment_and_wait_wakes_immediately(self):
        updated = dict(SNAPSHOT, comments=[{"id": 3, "body": "Answer"}])
        with patch.object(Issue, "read", side_effect=[(False, SNAPSHOT, []), (False, updated, [])]), \
                patch.object(Issue, "comment"):
            handoff = execute("comment", context())
            sleep = Mock()
            result = execute("wait", dict(CONTEXT, inputs=[handoff]), sleep=sleep)
        self.assertEqual(result["data"]["status"], "updated")
        sleep.assert_not_called()

    def test_closed_during_comment_wait_and_guard(self):
        for operation in ("comment", "wait", "guard"):
            with self.subTest(operation=operation), patch.object(Issue, "read", return_value=(True, SNAPSHOT, [])), \
                    patch.object(Issue, "comment") as post:
                sleep = Mock()
                self.assertEqual(execute(operation, context(), sleep=sleep)["data"]["status"], "closed")
                sleep.assert_not_called()
                post.assert_not_called()

    def test_wait_cadence_has_no_24_hour_cutoff(self):
        for elapsed, expected in ((0, 60), (3599, 60), (3600, 300), (86399, 300), (86400, 3600), (1e9, 3600)):
            self.assertEqual(poll_seconds(elapsed), expected)
        with patch.object(Issue, "read", side_effect=[(False, SNAPSHOT, [])] * 4 + [(True, SNAPSHOT, [])]):
            clock = Mock(side_effect=[0, 0, 3600, 86400, 172800])
            sleep = Mock()
            self.assertEqual(execute("wait", context(), clock=clock, sleep=sleep)["data"]["status"], "closed")
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [60, 300, 3600, 3600])

    def test_unchanged_state_metadata_and_automation_comments_do_not_wake_wait(self):
        initial = {"state": "open", "title": SNAPSHOT["title"], "body": SNAPSHOT["body"], "updated_at": "old"}
        changed_metadata = dict(initial, updated_at="new", labels=[{"name": "label"}])
        updated = dict(initial, title="Changed")
        own_comments = [[{"id": 1, "body": MARKER + "own -->\nQuestion"}]]
        with patch("projectweave.readiness.process", side_effect=[json.dumps(v) for v in
                  (initial, [], changed_metadata, own_comments, updated, own_comments)]):
            sleep = Mock()
            result = execute("wait", context(), clock=lambda: 0, sleep=sleep)
        self.assertEqual(result["data"]["status"], "updated")
        self.assertEqual(sleep.call_count, 2)

    def test_empty_questions_and_github_failures_stop_without_posting(self):
        with patch.object(Issue, "read", return_value=(False, SNAPSHOT, [])), patch.object(Issue, "comment") as post:
            with self.assertRaises(Failure):
                execute("comment", context(questions=()))
            post.assert_not_called()
        with patch.object(Issue, "read", side_effect=Failure("github", "Denied")) as read:
            with self.assertRaises(Failure):
                execute("wait", context())
            read.assert_called_once()  # No automatic failure retries.

    def test_cli_emits_gitweave_envelope(self):
        with patch("sys.stdin", new=io.StringIO(json.dumps(CONTEXT))), \
                patch("sys.stdout", new=io.StringIO()) as stdout, \
                patch.object(Issue, "read", return_value=(False, SNAPSHOT, [])):
            self.assertEqual(main(["issue-readiness", "snapshot"]), 0)
            envelope = json.loads(stdout.getvalue())
        self.assertEqual(set(envelope), {"message", "data"})
        self.assertEqual(envelope["data"]["status"], "review")

    def test_diagnosis_reaches_implementation_only_for_unchanged_open_issue(self):
        diagnosed = context("ready", questions=())
        diagnosed["inputs"][0]["data"]["diagnosis"] = "Reproduced with test_x; fix the missing bounds check."
        with patch.object(Issue, "read", return_value=(False, SNAPSHOT, [])):
            result = execute("guard", diagnosed)["data"]
        self.assertEqual(result, diagnosed["inputs"][0]["data"])
        for closed, snapshot, status in ((False, dict(SNAPSHOT, body="Changed"), "updated"),
                                         (True, SNAPSHOT, "closed")):
            with patch.object(Issue, "read", return_value=(closed, snapshot, [])):
                result = execute("guard", diagnosed)["data"]
            self.assertEqual(result["status"], status)
            self.assertNotIn("diagnosis", result)

    def test_undiagnosed_bug_can_ask_for_information_and_wait(self):
        diagnosed = context()
        diagnosed["inputs"][0]["data"]["diagnosis"] = "Cannot reproduce without a failing input."
        with patch.object(Issue, "read", return_value=(False, SNAPSHOT, [])), \
                patch.object(Issue, "comment") as post:
            result = execute("comment", diagnosed)
        self.assertEqual(result["data"]["status"], "needs_information")
        post.assert_called_once()
        diagnosed["inputs"][0]["data"]["diagnosis"] = " "
        with self.assertRaises(Failure):
            execute("guard", diagnosed)

    def test_unlimited_executor_timeout_is_explicit_and_mixed_nodes_are_compatible(self):
        for kind in ("command", "gitweave"):
            config = {"type": kind, **({"argv": ["worker"]} if kind == "command" else {"graph": "graph.json"})}
            executor(config)
            executor(dict(config, timeout=None))
            executor(dict(config, timeout=10))
            for invalid in (0, -1, True, "forever", float("inf")):
                with self.assertRaises(Failure):
                    executor(dict(config, timeout=invalid))
        generated = templates(ROOT, "claude", "opus")
        self.assertIsNone(generated["graph.json"]["nodes"]["execute"]["executor"]["timeout"])
        worker = generated["gitweave.json"]
        compatible("gitweave.json", worker, worker)
        for node in worker["nodes"].values():
            if node["kind"] == "command":
                self.assertNotIn("provider", node)
                self.assertNotIn("model", node)
                self.assertNotIn("permission_mode", node)
        changed = copy.deepcopy(worker)
        changed["nodes"]["wait_for_issue_update"]["argv"] = ["other-command"]
        with self.assertRaises(Failure):
            compatible("gitweave.json", changed, worker)


@unittest.skipUnless(os.environ.get("GITWEAVE_SOURCE"), "Optional public GitWeave runtime routing checks")
class GitWeaveRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.environ["GITWEAVE_SOURCE"])
        cls.runtime = importlib.import_module("gitweave.runtime")
        cls.model = importlib.import_module("gitweave.model")
        cls.graph = templates(ROOT)["gitweave.json"]

    def run_route(self, reads, approvals=("ready",), *, merge_results=(True,), review_results=(True,),
                  labels=(), diagnosis_inputs=None):
        runtime = self.runtime.Runtime.__new__(self.runtime.Runtime)
        runtime.graph = self.runtime.validate_graph(copy.deepcopy(self.graph))
        runtime.steps, runtime.stopped = 0, False
        called, approved, merged, reviewed = [], iter(approvals), iter(merge_results), iter(review_results)
        async def node(name, inputs, item, origin):
            runtime.tick()
            called.append(name)
            spec = runtime.graph["nodes"][name]
            if name == "issue_route":
                data = route_issue(CONTEXT)["data"]
            elif spec["kind"] == "command":
                data = execute(spec["argv"][-1], dict(CONTEXT, inputs=inputs), clock=lambda: 0, sleep=Mock())["data"]
            elif name in ("readiness", "diagnose"):
                status = next(approved)
                data = outcome(status, inputs[0]["data"]["snapshot"],
                               ["What is expected?"] if status == "needs_information" else [])["data"]
                if name == "diagnose":
                    data["diagnosis"] = "Reproduced: missing bounds check. Add validation and regression coverage."
            elif name == "implement":
                if diagnosis_inputs is not None:
                    diagnosis_inputs.append(inputs[0]["data"])
                data = None
            else:
                pr = {"number": 1, "url": "https://github.com/owner/repo/pull/1", "head_sha": "a" * 40}
                data = {"pr": pr}
                if name == "review":
                    data.update(approved=next(reviewed), findings=[])
                if name == "fix":
                    data.update(summary="Fixed")
                if name == "merge":
                    success = next(merged)
                    data.update(merged=success, merge_commit="b" * 40 if success else "")
                if name == "close_issue":
                    data = dict(inputs[0]["data"], closed=True)
            if "schema" in spec:
                self.model.validate(data, spec["schema"])
            return {"node_id": name, "commit": "c" * 40, "message": name, "data": data,
                    "data_validated": "schema" in spec}
        runtime.node = node
        with patch.object(Issue, "read", side_effect=reads), patch.object(Issue, "comment") as post, \
                patch.object(Issue, "api", return_value={"state": "open", "labels": list(labels)}):
            outputs = asyncio.run(runtime.flow(runtime.graph["flow"], [{"data": None}]))
        return called, outputs[0]["data"], post

    def test_ready_and_existing_review_fix_merge_retries(self):
        called, result, post = self.run_route([(False, SNAPSHOT, [])] * 2,
                                             merge_results=(False, True), review_results=(False, True, True))
        self.assertEqual(called, ["issue_route", "issue_snapshot", "readiness", "check_issue_open", "implement", "publish",
                                 "review", "fix", "publish", "review", "merge", "review", "merge", "close_issue"])
        self.assertTrue(result["merged"])
        post.assert_not_called()

    def test_not_ready_unchanged_polls_then_update_and_ready(self):
        changed = dict(SNAPSHOT, body="Acceptance criteria supplied")
        reads = [(False, SNAPSHOT, [])] * 4 + [(False, changed, [])] * 3
        called, result, post = self.run_route(reads, ("needs_information", "ready"))
        self.assertEqual(called[:8], ["issue_route", "issue_snapshot", "readiness", "ask_information", "wait_for_issue_update",
                                     "issue_snapshot", "readiness", "check_issue_open"])
        self.assertEqual(called.count("wait_for_issue_update"), 1)
        self.assertEqual(called.count("readiness"), 2)
        self.assertTrue(result["merged"])
        post.assert_called_once()

    def test_closed_initially_during_wait_or_before_implementation(self):
        cases = [([(True, SNAPSHOT, [])], ("ready",), ["issue_snapshot"]),
                 ([(False, SNAPSHOT, [])] * 2 + [(True, SNAPSHOT, [])], ("needs_information",),
                  ["issue_snapshot", "readiness", "ask_information", "wait_for_issue_update"]),
                 ([(False, SNAPSHOT, []), (True, SNAPSHOT, [])], ("ready",),
                  ["issue_snapshot", "readiness", "check_issue_open"])]
        for reads, approvals, expected in cases:
            with self.subTest(expected=expected):
                called, result, _ = self.run_route(reads, approvals)
                self.assertEqual(called, ["issue_route"] + expected)
                self.assertEqual(result["status"], "closed")
                self.assertNotIn("pr", result)

    def test_update_during_review_skips_stale_questions_and_rechecks(self):
        changed = dict(SNAPSHOT, body="Answered")
        called, _, post = self.run_route([(False, SNAPSHOT, [])] + [(False, changed, [])] * 3,
                                        ("needs_information", "ready"))
        self.assertEqual(called[:7], ["issue_route", "issue_snapshot", "readiness", "ask_information", "issue_snapshot",
                                     "readiness", "check_issue_open"])
        self.assertNotIn("wait_for_issue_update", called)
        post.assert_not_called()

    def test_update_after_approval_rechecks_before_implementation(self):
        changed = dict(SNAPSHOT, body="Changed scope")
        called, _, _ = self.run_route([(False, SNAPSHOT, [])] + [(False, changed, [])] * 3,
                                     ("ready", "ready"))
        self.assertEqual(called[:8], ["issue_route", "issue_snapshot", "readiness", "check_issue_open", "issue_snapshot",
                                     "readiness", "check_issue_open", "implement"])

    def test_bug_selects_diagnosis_and_forwards_it_through_guard(self):
        inputs = []
        called, result, post = self.run_route([(False, SNAPSHOT, [])] * 2, labels=({"name": " BUG "},),
                                             diagnosis_inputs=inputs,
                                             merge_results=(False, True), review_results=(False, True, True))
        self.assertEqual(called[:5], ["issue_route", "issue_snapshot", "diagnose", "check_issue_open", "implement"])
        self.assertNotIn("readiness", called)
        self.assertIn("missing bounds check", inputs[0]["diagnosis"])
        self.assertEqual(called[5:], ["publish", "review", "fix", "publish", "review", "merge", "review", "merge", "close_issue"])
        self.assertTrue(result["merged"])
        post.assert_not_called()

    def test_missing_and_unrelated_labels_use_normal_readiness(self):
        for labels in ((), ({"name": "task"},)):
            with self.subTest(labels=labels):
                inputs = []
                called, _, _ = self.run_route([(False, SNAPSHOT, [])] * 2, labels=labels, diagnosis_inputs=inputs)
                self.assertEqual(called[:4], ["issue_route", "issue_snapshot", "readiness", "check_issue_open"])
                self.assertNotIn("diagnose", called)
                self.assertNotIn("diagnosis", inputs[0])

    def test_bug_needing_information_waits_and_diagnoses_updated_content(self):
        changed = dict(SNAPSHOT, body="Reproduction provided")
        called, result, post = self.run_route([(False, SNAPSHOT, [])] * 3 + [(False, changed, [])] * 3,
                                             ("needs_information", "ready"), labels=("bug",))
        self.assertEqual(called[:8], ["issue_route", "issue_snapshot", "diagnose", "ask_information", "wait_for_issue_update",
                                     "issue_snapshot", "diagnose", "check_issue_open"])
        self.assertEqual(called.index("implement"), 8)
        self.assertTrue(result["merged"])
        post.assert_called_once()

    def test_undiagnosed_bug_closure_never_reaches_implementation(self):
        called, result, post = self.run_route([(False, SNAPSHOT, [])] * 2 + [(True, SNAPSHOT, [])],
                                             ("needs_information",), labels=("bug",))
        self.assertEqual(called, ["issue_route", "issue_snapshot", "diagnose", "ask_information", "wait_for_issue_update"])
        self.assertEqual(result["status"], "closed")
        self.assertNotIn("pr", result)
        post.assert_called_once()

    def test_content_change_after_diagnosis_requires_another_diagnosis(self):
        changed = dict(SNAPSHOT, body="Changed reproduction")
        inputs = []
        called, _, _ = self.run_route([(False, SNAPSHOT, [])] + [(False, changed, [])] * 3,
                                     ("ready", "ready"), labels=("bug",), diagnosis_inputs=inputs)
        self.assertEqual(called[:8], ["issue_route", "issue_snapshot", "diagnose", "check_issue_open", "issue_snapshot",
                                     "diagnose", "check_issue_open", "implement"])
        self.assertEqual(inputs[0]["snapshot"], changed)

    def test_meaningful_update_iterations_obey_shared_step_budget(self):
        snapshots = [dict(SNAPSHOT, body=str(i)) for i in range(50)]
        reads = []
        for before, after in zip(snapshots, snapshots[1:]):
            reads.extend([(False, before, []), (False, before, []), (False, after, [])])
        with self.assertRaises(self.model.Failure) as caught:
            self.run_route(reads, ("needs_information",) * 50)
        self.assertEqual(caught.exception.kind, "step_limit")
