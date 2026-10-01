import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from projectweave.cli import main
from projectweave.contracts import Failure
from projectweave.routing import execute, routing
from projectweave.setup import templates

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = {"github_repository": "owner/repo", "run_input": {"kind": "issue", "number": 75}}


class LabelRoutingTests(unittest.TestCase):
    def test_bug_label_normalization_and_extraction(self):
        self.assertEqual(routing({"state": "open", "labels": [
            {"name": " task "}, {"name": "BUG", "color": "ignored"}, "bug", " Bug "]}),
            {"route": "bug", "labels": ["bug", "task"]})

    def test_unrelated_empty_or_missing_labels_use_default(self):
        for extra in ({}, {"labels": []}, {"labels": [{"name": "task"}, "debug", "bugfix"]}):
            with self.subTest(extra=extra):
                self.assertEqual(routing(dict(state="open", **extra))["route"], "default")

    def test_invalid_metadata_fails_instead_of_selecting_default(self):
        for extra in ({"labels": None}, {"labels": "bug"}, {"labels": [None]},
                      {"labels": [{}]}, {"labels": [" "]}, {"pull_request": {}}, {"state": "unknown"}):
            with self.subTest(extra=extra), self.assertRaises(Failure):
                routing(dict({"state": "open"}, **extra))

    def test_invalid_context_fails_before_github(self):
        for invalid in (None, {}, dict(CONTEXT, github_repository="../repo"),
                        dict(CONTEXT, run_input={"kind": "issue", "number": True}),
                        dict(CONTEXT, run_input={"kind": "issue", "number": 0}),
                        dict(CONTEXT, run_input={"kind": "pull_request", "number": 75})):
            with self.subTest(invalid=invalid), patch("projectweave.readiness.process") as process:
                with self.assertRaises(Failure):
                    execute(invalid)
                process.assert_not_called()

    def test_cli_reads_current_source_labels_with_only_one_gh_call(self):
        # Operator request and upstream Task labels may be stale: use the source Issue.
        context = dict(CONTEXT, request={"labels": ["docs"]}, inputs=[{"data": {"labels": []}}])
        with patch("sys.stdin", io.StringIO(json.dumps(context))), \
                patch("sys.stdout", io.StringIO()) as stdout, \
                patch("projectweave.readiness.process", return_value=json.dumps({
                    "state": "open", "labels": [{"name": "bug"}]})) as process:
            self.assertEqual(main(["issue-route"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["data"], {"route": "bug", "labels": ["bug"]})
        # No model/provider invocation, shell, comment write or comment pagination.
        process.assert_called_once_with(
            ["gh", "api", "--hostname", "github.com", "repos/owner/repo/issues/75"], None, 120)

    def test_github_failure_stops_without_retry_or_fallback(self):
        with patch("projectweave.readiness.process", side_effect=Failure("github", "Denied")) as process:
            with self.assertRaises(Failure):
                execute(CONTEXT)
            process.assert_called_once()

    def test_generated_graph_routes_before_any_agent(self):
        graph = templates(ROOT, "claude", "opus")["gitweave.json"]
        self.assertEqual(graph["flow"][0], "issue_route")
        route = graph["nodes"]["issue_route"]
        self.assertEqual(route["kind"], "command")
        self.assertEqual(route["argv"], ["projectweave", "issue-route"])
        for field in ("provider", "model", "instruction", "permission_mode"):
            self.assertNotIn(field, route)
        branch = graph["flow"][1]["if"]
        self.assertEqual(branch["condition"], {"path": "/0/data/route", "equals": "bug"})
        for path, reviewer in (("then", "diagnose"), ("else", "readiness")):
            review = branch[path][0]["loop"]["flow"][1]["if"]["then"]
            self.assertEqual(review[0], reviewer)
            self.assertEqual(review[1]["if"]["then"][0], "ask_information")
        self.assertEqual(graph["flow"][2]["if"]["condition"], {"path": "/0/data/status", "equals": "ready"})
