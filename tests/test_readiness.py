import asyncio
import copy
import importlib
import io
import inspect
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from projectweave.cli import main
from projectweave.contracts import Failure
from projectweave.graph import executor
from projectweave.readiness import Issue, MARKER, REVIEW_MARKER, execute, outcome, poll_seconds
from projectweave.review import execute as review_command, outcome as review_outcome
from projectweave.routing import execute as route_issue
from projectweave.setup import compatible, templates

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = {"title": "Add feature", "body": "Missing acceptance criteria", "comments": []}
CONTEXT = {"github_repository": "owner/repo", "run_input": {"kind": "issue", "number": 71}}


from issue_fixtures import IssueFixture


def revision(snapshot=SNAPSHOT):
    # Synthetic API edit tokens for the older routing scenarios below.
    return {"repository": "owner/repo", "number": 71, "issue_id": "issue-71",
            "body_edited_at": "edit:" + snapshot["body"], "title_event": "rename:" + snapshot["title"],
            "comments": [{"id": c["id"], "node_id": str(c["id"]), "edited_at": "edit:" + c["body"],
                          "automation": False} for c in snapshot["comments"]]}


def context(status="needs_information", snapshot=SNAPSHOT, questions=("What behavior is expected?",)):
    return dict(CONTEXT, inputs=[{"data": {"status": status, "questions": list(questions)}},
                                outcome("review", copy.deepcopy(snapshot), revision(snapshot))])


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = IssueFixture()
        self.patch = patch("projectweave.readiness.process", self.fixture.process)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.capture = execute("snapshot", CONTEXT)

    def context(self, status="needs_information", questions=("What behavior is expected?",)):
        return dict(CONTEXT, inputs=[{"data": {"status": status, "questions": list(questions)}},
                                    copy.deepcopy(self.capture)])

    def test_github_reads_all_pages_and_excludes_only_marked_comments(self):
        self.fixture.page_size = 1
        self.fixture.reply(MARKER + "own -->\nQuestion")
        self.fixture.reply("Human reply from the same account")
        self.fixture.reply("Another reply")
        result = execute("snapshot", CONTEXT)["data"]
        self.assertEqual(result["snapshot"], self.fixture.content())
        self.assertEqual(result["revision"], self.fixture.revision())
        self.assertEqual(len(result["snapshot"]["comments"]), 2)
        self.fixture.fail_cursor = True
        with self.assertRaises(Failure):
            execute("snapshot", CONTEXT)

    def test_post_uses_json_stdin_not_shell_interpolation(self):
        body = "Question with `quotes`, $(commands), and\nnewlines"
        with patch("projectweave.readiness.process", return_value="{}") as process:
            Issue(CONTEXT).comment(body)
        argv, stdin, timeout = process.call_args.args
        self.assertEqual(argv[-4:], ["--method", "POST", "--input", "-"])
        self.assertEqual(json.loads(stdin), {"body": body})
        self.assertEqual(timeout, 120)

    def test_missing_metadata_errors_and_malformed_responses_fail_closed(self):
        for response in ({"data": {"repository": {"issue": None}}}, {"errors": [{"message": "Denied"}]},
                         {"data": {"repository": {"issue": {"id": "I", "state": "OPEN"}}}}):
            with self.subTest(response=response), patch("projectweave.readiness.process", return_value=json.dumps(response)):
                with self.assertRaises(Failure):
                    execute("snapshot", CONTEXT)
        for invalid in ({}, dict(CONTEXT, github_repository="../repo"),
                        dict(CONTEXT, run_input={"kind": "pull_request", "number": 71}),
                        dict(CONTEXT, run_input={"kind": "issue", "number": True})):
            with self.subTest(invalid=invalid), self.assertRaises(Failure):
                execute("snapshot", invalid)

    def test_questions_post_once_and_baseline_survives_serialized_retry(self):
        value = self.context()
        first = execute("comment", value)
        self.assertEqual(first["data"]["revision"], self.capture["data"]["revision"])
        self.assertEqual(first["data"]["snapshot"], SNAPSHOT)
        execute("comment", json.loads(json.dumps(value)))
        value["inputs"][0]["data"]["questions"] = ["Different wording"]
        execute("comment", value)
        self.assertEqual(len(self.fixture.comments), 1)
        self.assertIn("What behavior is expected?", self.fixture.comments[0]["body"])
        self.fixture.edit_body("Changed requirements")
        self.capture = execute("snapshot", CONTEXT)
        execute("comment", self.context())
        self.assertEqual(len(self.fixture.comments), 2)

    def test_poll_fetches_no_unchanged_bodies_and_ignores_unrelated_metadata(self):
        handoff = execute("comment", self.context())
        self.fixture.reply(REVIEW_MARKER + "other -->\nQuestion")
        self.fixture.calls = []
        polls = []
        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 1:
                self.fixture.edit_comment(1, MARKER + "edited -->\nQuestion")
            if len(polls) == 3:
                self.fixture.edit_title("Changed")
        result = execute("wait", dict(CONTEXT, inputs=[handoff]), clock=lambda: 0, sleep=sleep)
        self.assertEqual(result["data"]["status"], "updated")
        self.assertEqual(polls, [60] * 3)
        queries = [payload["query"] for _, payload in self.fixture.calls]
        issue_queries = [q for q in queries if "repository(" in q]
        self.assertEqual(sum(" body" in q for q in issue_queries), 1)  # Only after title metadata changes.
        self.assertTrue(all("updatedAt" not in q for q in queries))
        # Only new/edited automation bodies were fetched for classification during unchanged polls.
        self.assertEqual(sum("IssueComment" in q for q in queries), 3)

    def test_body_title_and_external_comment_creation_edit_deletion_wake_both_paths(self):
        from projectweave.review import execute as review
        pr = {"number": 1, "url": "https://github.com/owner/repo/pull/1", "head_sha": "a" * 40}
        for path in ("readiness", "review"):
            for change in ("body", "body_revert", "title", "title_revert", "create", "edit", "delete"):
                with self.subTest(path=path, change=change):
                    fixture = IssueFixture()
                    fixture.reply("Initial answer")
                    with patch("projectweave.readiness.process", fixture.process):
                        capture = execute("snapshot", CONTEXT)
                        if path == "readiness":
                            command = execute
                            value = dict(CONTEXT, inputs=[{"data": {"status": "needs_information", "questions": ["Why?"]}}, capture])
                        else:
                            command = review
                            value = dict(CONTEXT, inputs=[{"data": {"pr": pr, "status": "needs_confirmation", "approved": False,
                                "findings": [], "questions": ["Why?"], "retry_review": True}}, capture])
                        handoff = command("comment", value)
                        def sleep(_):
                            if change.startswith("body"):
                                fixture.edit_body("Changed")
                                if change.endswith("revert"): fixture.edit_body(SNAPSHOT["body"])
                            elif change.startswith("title"):
                                fixture.edit_title("Changed")
                                if change.endswith("revert"): fixture.edit_title(SNAPSHOT["title"])
                            elif change == "create": fixture.reply("Answer")
                            elif change == "edit": fixture.edit_comment(1, "Edited")
                            else: fixture.comments = [c for c in fixture.comments if c["id"] != 1]
                        result = command("wait", dict(CONTEXT, inputs=[handoff]), clock=lambda: 0, sleep=sleep)
                    self.assertEqual(result["data"]["status"], "updated")
                    self.assertEqual(result["data"]["snapshot"], fixture.content())
                    self.assertEqual(result["data"]["revision"], fixture.revision())

    def test_concurrent_reply_during_assessment_and_post_handoff(self):
        for during_post in (False, True):
            with self.subTest(during_post=during_post):
                self.fixture.comments = []
                if during_post:
                    self.fixture.after_post = lambda: self.fixture.reply("Answer")
                else:
                    self.fixture.reply("Answer")
                handoff = execute("comment", self.context())
                if during_post:
                    sleep = Mock()
                    handoff = execute("wait", dict(CONTEXT, inputs=[handoff]), sleep=sleep)
                    sleep.assert_not_called()
                self.assertEqual(handoff["data"]["status"], "updated")
                self.assertEqual(len(self.fixture.comments), 2 if during_post else 1)

    def test_closure_priority_at_capture_request_wait_and_guard(self):
        for operation in ("snapshot", "comment", "wait", "guard"):
            with self.subTest(operation=operation):
                self.fixture.closed = True
                value = self.context("ready" if operation == "guard" else "needs_information")
                if operation == "wait": value = dict(CONTEXT, inputs=[dict(data=dict(self.capture["data"], status="needs_information"))])
                self.fixture.calls = []
                sleep = Mock()
                result = execute(operation, value, sleep=sleep)
                self.assertEqual(result["data"]["status"], "closed")
                self.assertEqual(len(self.fixture.calls), 1)
                sleep.assert_not_called()
                self.assertFalse(self.fixture.comments)

    def test_comment_marker_edits_and_deletion_during_classification(self):
        self.fixture.reply(MARKER + "own -->\nQuestion")
        self.capture = execute("snapshot", CONTEXT)
        handoff = execute("comment", self.context())
        self.fixture.edit_comment(1, "Now an external reply")
        result = execute("wait", dict(CONTEXT, inputs=[handoff]), sleep=Mock())
        self.assertEqual(result["data"]["status"], "updated")
        self.assertEqual(result["data"]["snapshot"]["comments"][0]["body"], "Now an external reply")
        self.capture = execute("snapshot", CONTEXT)
        handoff = execute("comment", self.context())
        self.fixture.edit_comment(1, MARKER + "now marked -->\nQuestion")
        self.assertEqual(execute("wait", dict(CONTEXT, inputs=[handoff]), sleep=Mock())["data"]["status"], "updated")

        # External edit observed in a metadata page, then deleted before body classification.
        self.fixture.comments = []
        self.fixture.reply("External")
        self.capture = execute("snapshot", CONTEXT)
        handoff = execute("comment", self.context())
        identity = self.fixture.comments[0]["id"]
        self.fixture.edit_comment(identity, "Edited")
        process = self.fixture.process
        def deleted(argv, stdin, timeout):
            if "IssueComment" in json.loads(stdin).get("query", ""):
                self.fixture.comments = [c for c in self.fixture.comments if c["id"] != identity]
            return process(argv, stdin, timeout)
        with patch("projectweave.readiness.process", deleted):
            result = execute("wait", dict(CONTEXT, inputs=[handoff]), sleep=Mock())
        self.assertEqual(result["data"]["status"], "updated")
        self.assertEqual(result["data"]["snapshot"]["comments"], [])

    def test_wait_cadence_has_no_24_hour_cutoff(self):
        for elapsed, expected in ((0, 60), (3599, 60), (3600, 300), (86399, 300), (86400, 3600), (1e9, 3600)):
            self.assertEqual(poll_seconds(elapsed), expected)
        handoff = execute("comment", self.context())
        clock = Mock(side_effect=[0, 0, 3600, 86400, 172800])
        pauses = []
        def sleep(seconds):
            pauses.append(seconds)
            if len(pauses) == 4: self.fixture.closed = True
        self.assertEqual(execute("wait", dict(CONTEXT, inputs=[handoff]), clock=clock, sleep=sleep)["data"]["status"], "closed")
        self.assertEqual(pauses, [60, 300, 3600, 3600])

    def test_guard_forwards_diagnosis_only_for_unchanged_open_issue(self):
        value = self.context("ready", ())
        value["inputs"][0]["data"]["diagnosis"] = "Reproduced: fix the missing bounds check."
        result = execute("guard", value)["data"]
        self.assertEqual(result["diagnosis"], value["inputs"][0]["data"]["diagnosis"])
        self.fixture.edit_body("Changed scope")
        result = execute("guard", value)["data"]
        self.assertEqual(result["status"], "updated")
        self.assertNotIn("diagnosis", result)

    def test_runs_are_isolated_and_foreign_or_agent_baselines_fail(self):
        run_a, run_b = self.context(), self.context()
        self.fixture.edit_body("Updated")
        run_b["inputs"][1] = execute("snapshot", CONTEXT)
        self.assertEqual(execute("comment", run_b)["data"]["status"], "needs_information")
        self.assertEqual(execute("comment", run_a)["data"]["status"], "updated")
        for target in (dict(CONTEXT, github_repository="owner/other"), dict(CONTEXT, run_input={"kind": "issue", "number": 72})):
            with self.assertRaises(Failure):
                execute("comment", dict(target, inputs=run_a["inputs"]))
        # Old saved graphs cannot fall back to comparing an agent-echoed snapshot.
        with self.assertRaises(Failure):
            execute("comment", dict(CONTEXT, inputs=[run_a["inputs"][0]]))
        run_b["inputs"][0]["data"]["snapshot"] = dict(SNAPSHOT, body="Changed space\nto newline")
        with self.assertRaises(Failure):
            execute("comment", run_b)

    def test_empty_questions_and_github_failures_stop_without_posting(self):
        with self.assertRaises(Failure): execute("comment", self.context(questions=()))
        self.assertFalse(self.fixture.comments)
        with patch.object(Issue, "read", side_effect=Failure("github", "Denied")) as read:
            with self.assertRaises(Failure): execute("guard", self.context("ready"))
            read.assert_called_once()

    def test_cli_emits_gitweave_envelope(self):
        with patch("sys.stdin", new=io.StringIO(json.dumps(CONTEXT))), patch("sys.stdout", new=io.StringIO()) as stdout:
            self.assertEqual(main(["issue-readiness", "snapshot"]), 0)
            envelope = json.loads(stdout.getvalue())
        self.assertEqual(set(envelope), {"message", "data"})
        self.assertEqual(envelope["data"]["status"], "review")

    def test_unlimited_executor_timeout_and_provider_settings_remain_compatible(self):
        for kind in ("command", "gitweave"):
            config = {"type": kind, **({"argv": ["worker"]} if kind == "command" else {"graph": "graph.json"})}
            executor(config)
            executor(dict(config, timeout=None))
            executor(dict(config, timeout=10))
            for invalid in (0, -1, True, "forever", float("inf")):
                with self.assertRaises(Failure): executor(dict(config, timeout=invalid))
        generated = templates(ROOT, "claude", "opus")
        self.assertIsNone(generated["graph.json"]["nodes"]["execute"]["executor"]["timeout"])
        worker = generated["gitweave.json"]
        compatible("gitweave.json", worker, worker)
        for node in worker["nodes"].values():
            if node["kind"] == "command": self.assertFalse({"provider", "model", "permission_mode"} & node.keys())
        changed = copy.deepcopy(worker)
        changed["nodes"]["wait_for_issue_update"]["argv"] = ["other-command"]
        with self.assertRaises(Failure): compatible("gitweave.json", changed, worker)


@unittest.skipUnless(os.environ.get("GITWEAVE_SOURCE"), "Optional public GitWeave runtime routing checks")
class GitWeaveRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.environ["GITWEAVE_SOURCE"])
        cls.runtime = importlib.import_module("gitweave.runtime")
        cls.model = importlib.import_module("gitweave.model")
        cls.graph = templates(ROOT)["gitweave.json"]

    def run_route(self, reads, approvals=("ready",), *, merge_results=(True,), review_results=(True,),
                  merge_issue_states=("open",), labels=(), diagnosis_inputs=None):
        runtime = self.runtime.Runtime.__new__(self.runtime.Runtime)
        runtime.graph = self.runtime.validate_graph(copy.deepcopy(self.graph))
        runtime.steps, runtime.stopped = 0, False
        runtime.visits, runtime.invocations = 0, {}
        last_snapshot = SNAPSHOT
        called, approved, reviewed = [], iter(approvals), iter(review_results)
        issue_states = iter(merge_issue_states)
        self.merge_pr = Mock(side_effect=merge_results)
        async def node(name, inputs, item, origin, invocation=None):
            nonlocal last_snapshot, current
            if len(inspect.signature(runtime.tick).parameters):
                runtime.tick(invocation or str(len(called)))
            else:
                runtime.tick()
            called.append(name)
            spec = runtime.graph["nodes"][name]
            if name == "issue_route":
                data = route_issue(CONTEXT)["data"]
            elif spec["kind"] == "command" and spec["argv"][1] == "issue-review":
                if spec["argv"][-1] == "snapshot":
                    # These readiness fixtures keep post-publication Issue content unchanged.
                    data = review_outcome("review", inputs[0]["data"]["pr"], last_snapshot, revision(last_snapshot))["data"]
                else:
                    data = review_command(spec["argv"][-1], dict(CONTEXT, inputs=inputs))["data"]
            elif spec["kind"] == "command":
                if spec["argv"][-1] == "snapshot":
                    current = None
                data = execute(spec["argv"][-1], dict(CONTEXT, inputs=inputs), clock=lambda: 0, sleep=Mock())["data"]
                last_snapshot = data["snapshot"]
            elif name in ("readiness", "diagnose"):
                status = next(approved)
                data = {"status": status, "questions": ["What is expected?"] if status == "needs_information" else []}
                if name == "diagnose":
                    data["diagnosis"] = "Assessed: " + inputs[0]["data"]["snapshot"]["body"].replace(" ", "\n", 1) + " Reproduced: missing bounds check. Add regression coverage."
            elif name == "implement":
                if diagnosis_inputs is not None:
                    diagnosis_inputs.append(inputs[0]["data"])
                data = None
            else:
                pr = {"number": 1, "url": "https://github.com/owner/repo/pull/1", "head_sha": "a" * 40}
                data = {"pr": pr}
                if name == "review":
                    approved_review = next(reviewed)
                    data = review_outcome("approved" if approved_review else "needs_fixes", pr,
                                          inputs[0]["data"]["snapshot"], revision(inputs[0]["data"]["snapshot"]))["data"]
                    del data["snapshot"], data["revision"]
                    data.update(approved=approved_review, retry_review=not approved_review,
                                findings=[] if approved_review else ["Fix defect"])
                if name == "fix":
                    data.update(summary="Fixed")
                if name == "merge":
                    # Mock the agent's Issue read and merge action; the real runtime routes its result.
                    state = next(issue_states)
                    success = self.merge_pr() if state == "open" else False
                    data.update(merged=success, retry=state == "open" and not success,
                                merge_commit="b" * 40 if success else "")
                if name == "close_issue":
                    data = {key: inputs[0]["data"][key] for key in ("pr", "merged", "merge_commit")}
                    data["closed"] = True
            if "schema" in spec:
                self.model.validate(data, spec["schema"])
            return {"node_id": name, "commit": "c" * 40, "message": name, "data": data,
                    "data_validated": "schema" in spec}
        runtime.node = node
        read_iter = iter(reads)
        current = None
        def read(*, content=True, **kwargs):
            nonlocal current
            if not content or current is None:
                current = next(read_iter)
            closed, snapshot, automation = current
            return closed, snapshot if content else None, automation, revision(snapshot)
        with patch.object(Issue, "read", side_effect=read), patch.object(Issue, "comment") as post, \
                patch.object(Issue, "api", return_value={"state": "open", "labels": list(labels)}):
            outputs = asyncio.run(runtime.flow(runtime.graph["flow"], [{"data": None}]))
        return called, outputs[0]["data"], post

    def test_formatted_assessment_posts_once_and_waits_without_false_update_loop(self):
        for labels, assessment in (((), "readiness"), (("bug",), "diagnose")):
            with self.subTest(assessment=assessment):
                called, result, post = self.run_route([(False, SNAPSHOT, [])] * 12 + [(True, SNAPSHOT, [])],
                                                      ("needs_information",), labels=labels)
                self.assertEqual(called, ["issue_route", "issue_snapshot", assessment,
                                          "ask_information", "wait_for_issue_update"])
                self.assertEqual(result["status"], "closed")
                post.assert_called_once()

    def test_ready_and_existing_review_fix_merge_retries(self):
        called, result, post = self.run_route([(False, SNAPSHOT, [])] * 2,
                                             merge_results=(False, True), review_results=(False, True, True),
                                             merge_issue_states=("open", "open"))
        self.assertEqual(called, ["issue_route", "issue_snapshot", "readiness", "check_issue_open", "implement", "publish",
                                 "review_snapshot", "review", "fix", "publish", "review_snapshot", "review", "merge", "review_snapshot", "review", "merge", "close_issue"])
        self.assertTrue(result["merged"])
        self.assertEqual(self.merge_pr.call_count, 2)
        post.assert_not_called()

    def test_open_issue_merge_succeeds_without_retry(self):
        for labels, assessment in (((), "readiness"), (("bug",), "diagnose")):
            with self.subTest(labels=labels):
                called, result, _ = self.run_route([(False, SNAPSHOT, [])] * 2, labels=labels)
                self.assertEqual(called, ["issue_route", "issue_snapshot", assessment, "check_issue_open",
                                         "implement", "publish", "review_snapshot", "review", "merge", "close_issue"])
                self.merge_pr.assert_called_once()
                self.assertTrue(result["merged"])
                self.assertEqual(result["merge_commit"], "b" * 40)

    def test_closed_issue_before_merge_skips_merge_and_retry(self):
        for labels, assessment in (((), "readiness"), (("bug",), "diagnose")):
            with self.subTest(labels=labels):
                called, result, _ = self.run_route([(False, SNAPSHOT, [])] * 2, labels=labels,
                                                  merge_issue_states=("closed",), merge_results=())
                self.assertEqual(called, ["issue_route", "issue_snapshot", assessment, "check_issue_open",
                                         "implement", "publish", "review_snapshot", "review", "merge", "close_issue"])
                self.merge_pr.assert_not_called()
                self.assertFalse(result["merged"])
                self.assertEqual(result["merge_commit"], "")
                self.assertTrue(result["closed"])
                self.assertEqual(result["pr"], {"number": 1, "url": "https://github.com/owner/repo/pull/1",
                                              "head_sha": "a" * 40})

    def test_issue_closed_after_failed_merge_stops_next_attempt(self):
        for labels, assessment in (((), "readiness"), (("bug",), "diagnose")):
            with self.subTest(labels=labels):
                called, result, _ = self.run_route([(False, SNAPSHOT, [])] * 2, labels=labels,
                                                  merge_results=(False,), review_results=(True, True),
                                                  merge_issue_states=("open", "closed"))
                self.assertEqual(called[:5], ["issue_route", "issue_snapshot", assessment,
                                             "check_issue_open", "implement"])
                self.assertEqual(called[-7:], ["review_snapshot", "review", "merge", "review_snapshot", "review", "merge", "close_issue"])
                self.merge_pr.assert_called_once()
                self.assertEqual(called.count("review"), 2)
                self.assertFalse(result["merged"])
                self.assertEqual(result["merge_commit"], "")
                self.assertTrue(result["closed"])

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
                                             merge_results=(False, True), review_results=(False, True, True),
                                             merge_issue_states=("open", "open"))
        self.assertEqual(called[:5], ["issue_route", "issue_snapshot", "diagnose", "check_issue_open", "implement"])
        self.assertNotIn("readiness", called)
        self.assertIn("missing bounds check", inputs[0]["diagnosis"])
        self.assertEqual(called[5:], ["publish", "review_snapshot", "review", "fix", "publish", "review_snapshot", "review", "merge", "review_snapshot", "review", "merge", "close_issue"])
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
