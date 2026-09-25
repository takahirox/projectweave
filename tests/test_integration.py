import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from projectweave.contracts import Failure
from projectweave.executors import invoke, process
from projectweave.github import GitHub

ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        for name in ("gh", "gitweave", "worker", "git", "claude", "codex"):
            path = self.directory / name
            path.write_text(f"#!{sys.executable}\n" + (ROOT / "tests/fake_cli.py").read_text())
            path.chmod(0o755)
        self.log = self.directory / "calls.jsonl"
        # Observed Codex usage (fake app-server) starts at 50% used, above the 20% stop line.
        self.env = dict(os.environ, PATH=str(self.directory) + os.pathsep + os.environ["PATH"], FAKE_LOG=str(self.log),
                        FAKE_CODEX_USED="50")
        # The canonical default workflow; the observer reads providers from its GitWeave Task graph.
        self.task_graph = self.directory / "gitweave.json"
        self.task_graph.write_text((ROOT / "projectweave/templates/gitweave.json").read_text())
        self.graph = json.loads((ROOT / "projectweave/templates/graph.json").read_text())
        self.graph["nodes"]["execute"]["executor"]["graph"] = str(self.task_graph)
        self.resources = json.loads((ROOT / "projectweave/templates/resources.json").read_text())
        self.project = json.loads((ROOT / "examples/project.json").read_text())

    @staticmethod
    def mutation_options(calls):
        return [c["request"]["variables"].get("option", "comment") for c in calls
                if c["command"] == "gh" and c["request"] and c["request"]["query"].startswith("mutation")]

    def run_cli(self, mode="success"):
        for name, value in (("graph", self.graph), ("project", self.project), ("resources", self.resources)):
            (self.directory / (name + ".json")).write_text(json.dumps(value))
        args = [sys.executable, "-m", "projectweave", "run"]
        for name in ("graph", "project", "resources"):
            args += ["--" + name, str(self.directory / (name + ".json"))]
        completed = subprocess.run(args, cwd=ROOT, env=dict(self.env, FAKE_MODE=mode), capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.stderr, "", completed.stderr)
        record = json.loads(completed.stdout)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return completed.returncode, record, calls

    def add_writeback(self, status):
        # The canonical graph has no writeback; custom graphs can still add one after execute.
        self.graph["nodes"]["writeback"] = {"kind": "action", "action": "writeback", "config": {"status": status},
                                            "inputs": {"task": "/results/select/data/task", "result": "/results/execute"}}
        self.graph["flow"][2]["if"]["else"][1]["if"]["then"].append("writeback")

    def test_gitweave_end_to_end_pagination_and_literal_request(self):
        code, record, calls = self.run_cli()
        self.assertEqual(code, 0, record)
        self.assertEqual(record["results"]["select"]["data"]["task"]["priority"], "P0")
        launch = [c for c in calls if c["command"] == "gitweave"]
        self.assertEqual(len(launch), 1)
        self.assertEqual(record["results"]["subscription"]["data"]["observations"], {"codex": {"remaining_percent": 50}})
        self.assertEqual(launch[0]["argv"][:7], ["run", "--graph", str(self.task_graph),
                                                 "--repo", "o/r", "--issue", "7"])
        self.assertEqual(Path(launch[0]["cwd"]).resolve(), self.directory.resolve())  # The workspace holds .gitweave/.
        # GitWeave fetches the repository itself; ProjectWeave clones and fetches nothing.
        self.assertFalse(any(c["command"] == "git" or c["argv"][:2] == ["repo", "clone"] for c in calls))
        self.assertIn("task $(literal) `literal`", launch[0]["argv"][-1])
        self.assertEqual(record["results"]["execute"]["references"], ["abc123"])
        self.assertTrue(record["results"]["execute"]["data"]["outputs"][0]["data"]["merged"])
        for field in ("items", "labels", "fieldValues", "fields"):
            pages = [c for c in calls if c["command"] == "gh" and c["request"] and field + "(first:" in c["request"]["query"]]
            self.assertEqual([p["request"]["variables"]["cursor"] for p in pages], [None, "next"])
        # The canonical graph only marks the Task In Progress before launch; the GitWeave graph comments the
        # outcome itself, so ProjectWeave posts no raw result comment.
        self.assertEqual(self.mutation_options(calls), ["PROGRESS"])
        self.assertLess(calls.index(next(c for c in calls if c["request"] and "mutation" in c["request"]["query"])),
                        calls.index(launch[0]))
        self.assertEqual(list(record["results"])[-1], "execute")

    def test_custom_writeback_comments_and_sets_status(self):
        self.add_writeback("Done")
        code, record, calls = self.run_cli()
        self.assertEqual(code, 0, record)
        mutations = [c for c in calls if c["command"] == "gh" and c["request"] and c["request"]["query"].startswith("mutation")]
        self.assertEqual(self.mutation_options(calls), ["PROGRESS", "comment", "DONE"])
        self.assertIn(record["run_id"], mutations[1]["request"]["variables"]["body"])
        self.assertIn("https://github.com/o/r/pull/12", mutations[1]["request"]["variables"]["body"])

    def use_command_agent(self):
        self.graph["nodes"]["execute"] = {"kind": "agent", "instruction": "Implement the selected task.",
                                          "executor": {"type": "command", "argv": ["worker"]},
                                          "inputs": {"task": "/results/select/data/task"}}
        # A command executor has no GitWeave providers to observe, so these tests skip the subscription check.
        self.graph["nodes"]["subscription"]["config"] = {}

    def test_agent_command_path(self):
        self.use_command_agent()
        code, record, calls = self.run_cli()
        self.assertEqual(code, 0)
        worker = next(c for c in calls if c["command"] == "worker")
        self.assertEqual(worker["request"]["task"]["id"], "I")
        self.assertEqual(worker["request"]["checkout"], str(self.directory / "repos" / "o" / "r"))
        self.assertIsInstance(worker["request"]["instruction"], str)
        self.assertFalse(any(c["command"] == "gitweave" for c in calls))
        self.assertTrue(record["results"]["execute"]["data"]["approved"])

    def test_empty_cli(self):
        code, record, calls = self.run_cli("empty")
        self.assertEqual(code, 0)
        self.assertEqual(record["last"]["data"]["status"], "no_work")
        self.assertEqual(len(calls), 2)

    def test_not_ready_field_is_not_selected(self):
        code, record, calls = self.run_cli("not_ready")
        self.assertEqual(code, 0)
        self.assertEqual(record["results"]["select"]["data"]["task"], None)
        self.assertEqual(record["results"]["load"]["data"]["items"][0]["ai_execution"], "Not ready")
        self.assertTrue(all(c["command"] == "gh" and not c["request"]["query"].startswith("mutation") for c in calls))

    def test_subscription_at_stop_line_or_unknown_starts_nothing(self):
        for extra in ({"FAKE_CODEX_USED": "80"}, {"FAKE_USAGE": "codex_error"}):
            with self.subTest(extra=extra):
                self.log.unlink(missing_ok=True)
                self.env.update(extra)
                code, record, calls = self.run_cli()
                self.env.pop("FAKE_USAGE", None)
                self.env["FAKE_CODEX_USED"] = "50"
                self.assertEqual(code, 0)
                data = record["results"]["subscription"]["data"]
                self.assertFalse(data["available"])
                self.assertEqual(list(data["observations"]), ["codex"])  # Observed (or failed) value in the receipt.
                self.assertNotIn("execute", record["results"])
                # Only GitHub reads and the Codex observation; no clone, fetch, GitWeave or Claude.
                self.assertEqual({c["command"] for c in calls}, {"gh", "codex"})
                calls = [c for c in calls if c["command"] == "gh"]
                self.assertFalse(any(c["request"]["query"].startswith("mutation") for c in calls))
                self.assertFalse((self.directory / "repos").exists())

    def test_executor_failure_stays_in_progress_without_comment(self):
        for kind, command in (("gitweave", "gitweave"), ("command", "worker")):
            with self.subTest(kind=kind):
                if kind == "command":
                    self.use_command_agent()
                self.log.unlink(missing_ok=True)
                code, record, calls = self.run_cli("executor_failure")
                self.assertEqual(code, 1)
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["failure"]["kind"], "transport")
                self.assertEqual(record["failure"]["node"], "execute")
                self.assertEqual(len([c for c in calls if c["command"] == command]), 1)
                self.assertNotIn("execute", record["results"])
                self.assertNotIn("writeback", record["results"])
                # The Task stays In Progress, so the next Run does not select it again; nothing is commented.
                self.assertEqual(self.mutation_options(calls), ["PROGRESS"])

    def test_existing_checkout_is_reused_after_origin_check(self):
        self.use_command_agent()  # Checkouts are resolved only for command executors.
        self.assertEqual(self.run_cli()[0], 0)
        self.log.unlink()
        code, record, calls = self.run_cli()
        self.assertEqual(code, 0, record)
        self.assertFalse(any(c["argv"][:2] == ["repo", "clone"] for c in calls))
        checkout = str(self.directory / "repos" / "o" / "r")
        self.assertEqual([c["argv"][2:] for c in calls if c["command"] == "git"],
                         [["rev-parse", "--show-toplevel"], ["remote", "get-url", "origin"], ["fetch", "origin"],
                          ["rev-parse", "--verify", "origin/HEAD^{commit}"]])
        self.assertEqual(next(c for c in calls if c["command"] == "worker")["request"]["checkout"], checkout)

    def test_checkout_failures_stop_before_executor_and_writeback(self):
        self.use_command_agent()
        checkout = self.directory / "repos" / "o" / "r"
        cases = {"clone_failure": None, "fetch_failure": None,
                 "wrong_origin": "https://github.com/o/other.git\n", "not_checkout": ""}
        for case, origin in cases.items():
            with self.subTest(case=case):
                self.log.unlink(missing_ok=True)
                if checkout.exists():
                    for path in checkout.iterdir():
                        path.unlink()
                    checkout.rmdir()
                if origin is not None:
                    checkout.mkdir(parents=True)
                    if origin:
                        (checkout / ".fake-origin").write_text(origin)
                    (checkout / "keep").write_text("user data")
                code, record, calls = self.run_cli(case if case.endswith("failure") else "success")
                self.assertEqual(code, 1, record)
                self.assertEqual(record["failure"]["kind"], "checkout")
                self.assertEqual(record["failure"]["node"], "execute")
                self.assertIn("o/r", record["failure"]["message"])
                self.assertFalse(any(c["command"] == "worker" for c in calls))
                self.assertNotIn("writeback", record["results"])
                self.assertEqual(self.mutation_options(calls), ["PROGRESS"])  # Only In Progress, before launch.
                if origin is not None:
                    self.assertEqual((checkout / "keep").read_text(), "user data")
                    self.assertFalse(any(c["argv"][:2] == ["repo", "clone"] for c in calls))

    def test_status_update_failure_stops_before_launch(self):
        code, record, calls = self.run_cli("status_failure")
        self.assertEqual(code, 1)
        self.assertEqual(record["failure"]["kind"], "status")
        self.assertEqual(record["failure"]["node"], "start")
        self.assertFalse(any(c["command"] == "gitweave" for c in calls))
        self.assertNotIn("writeback", record["results"])

    def test_non_todo_task_is_not_selected(self):
        self.project["eligible_statuses"] = ["Done"]
        code, record, calls = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(record["last"]["data"]["status"], "no_work")
        self.assertEqual(self.mutation_options(calls), [])

    def test_partial_writeback_preserves_result(self):
        self.add_writeback("Done")
        code, record, _ = self.run_cli("writeback_failure")
        self.assertEqual(code, 1)
        self.assertEqual(record["failure"]["kind"], "writeback")
        self.assertEqual(len(record["failure"]["details"]["completed_references"]), 1)
        self.assertIn("execute", record["results"])

    def test_missing_status_preflight_does_not_comment(self):
        self.add_writeback("Unknown")
        code, record, calls = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(record["failure"]["kind"], "writeback")
        self.assertFalse(any(c["command"] == "gh" and c["request"] and "addComment(" in c["request"]["query"] for c in calls))

    def test_graphql_error(self):
        code, record, calls = self.run_cli("graphql_error")
        self.assertEqual(code, 1)
        self.assertEqual(record["failure"]["kind"], "github")
        self.assertEqual(len(calls), 1)

    def test_invalid_input_exits_before_io(self):
        self.resources["subscription"]["stop_at_remaining_percent"] = 101
        code, _, calls = self.run_cli()
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])


class BoundaryTests(unittest.TestCase):
    def test_real_process_contract(self):
        code = 'import json,sys; x=json.load(sys.stdin); print(json.dumps({"message":x["task"],"data":{},"references":[],"usage":{}}))'
        value = invoke({"type": "command", "argv": [sys.executable, "-c", code]}, {"task": "literal $()"})
        self.assertEqual(value["message"], "literal $()")

    def test_process_launch_timeout_exit_and_bad_json(self):
        with self.assertRaises(Failure) as caught:
            process(["/nonexistent/projectweave-executor"], "", 1)
        self.assertEqual(caught.exception.kind, "launch")
        with self.assertRaises(Failure) as caught:
            process([sys.executable, "-c", "import time; time.sleep(10)"], "", .02)
        self.assertEqual(caught.exception.kind, "timeout")
        with self.assertRaises(Failure) as caught:
            process([sys.executable, "-c", "raise SystemExit(7)"], "", 2)
        self.assertEqual(caught.exception.kind, "transport")
        with self.assertRaises(Failure):
            invoke({"type": "command", "argv": [sys.executable, "-c", "print('not JSON')"]}, {})

    def test_gitweave_malformed_or_failed_records(self):
        config = {"type": "gitweave", "graph": "g"}
        for record in ({}, {"status": "failed", "outputs": []}, {"status": "completed", "outputs": [{}]}):
            with patch("projectweave.executors.process", return_value=json.dumps(record)):
                with self.assertRaises(Failure):
                    invoke(config, {"task": {"repository": "o/r", "number": 1}})

    def test_pagination_repeated_cursor_fails(self):
        backend = GitHub({"owner": "o", "owner_type": "user", "number": 1})
        with patch.object(backend, "query", return_value={"node": {"items": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "same"}}}}):
            with self.assertRaises(Failure):
                list(backend.pages("P", "ProjectV2", "items", "id"))

    def test_user_project_resolution(self):
        backend = GitHub({"owner": "o", "owner_type": "user", "number": 1})
        with patch.object(backend, "query", return_value={"user": {"projectV2": {"id": "P"}}}) as query:
            self.assertEqual(backend.resolve(), "P")
            self.assertIn("user(login:", query.call_args.args[0])

    def test_missing_project_and_malformed_connection(self):
        backend = GitHub({"owner": "o", "owner_type": "user", "number": 1})
        with patch.object(backend, "query", return_value={"user": None}):
            with self.assertRaises(Failure):
                backend.resolve()
        with patch.object(backend, "query", return_value={"node": None}):
            with self.assertRaises(Failure):
                list(backend.pages("P", "ProjectV2", "items", "id"))

    def test_malformed_status_has_no_mutation(self):
        backend = GitHub({"owner": "o", "owner_type": "user", "number": 1})
        backend.project_id = "P"
        with patch.object(backend, "pages", return_value=iter([{"name": "Status"}])):
            with patch.object(backend, "query") as query:
                with self.assertRaises(Failure) as caught:
                    backend.writeback({"id": "I", "item_id": "ITEM", "project_id": "P"},
                                      {"message": "done", "data": {}, "references": [], "usage": {}}, "RUN", "Done")
                self.assertEqual(caught.exception.kind, "writeback")
                query.assert_not_called()
