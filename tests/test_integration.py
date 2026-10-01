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
    """The root workspace CLI (run, claim, run-task, complete, coordinate) against fake gh/gitweave/codex/git."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        for name in ("gh", "gitweave", "worker", "git", "claude", "codex"):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + (ROOT / "tests/fake_cli.py").read_text())
            path.chmod(0o755)
        self.log = self.directory / "calls.jsonl"
        # Observed Codex usage (fake app-server) starts at 50% used.
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], FAKE_LOG=str(self.log),
                        FAKE_CODEX_USED="50", FAKE_STATE=str(self.directory / "github.json"))
        self.root = self.directory / "root"
        self.config = {"projects": {}}
        self.project_dir = self.add_project("p")
        self.task_graph = self.project_dir / "gitweave.json"

    def add_project(self, name, number=1):
        directory = self.root / "projects" / name
        directory.mkdir(parents=True)
        project = dict(json.loads((ROOT / "examples/project.json").read_text()), number=number)
        (directory / "project.json").write_text(json.dumps(project))
        (directory / "gitweave.json").write_text((ROOT / "projectweave/templates/gitweave.json").read_text())
        self.write_graph(directory, json.loads((ROOT / "projectweave/templates/graph.json").read_text()))
        return directory

    def write_graph(self, directory, graph):
        graph["nodes"]["execute"]["executor"].setdefault("graph", "gitweave.json")
        if graph["nodes"]["execute"]["executor"].get("type") == "gitweave":
            graph["nodes"]["execute"]["executor"]["graph"] = str(directory / "gitweave.json")
        (directory / "graph.json").write_text(json.dumps(graph))

    def graph(self):
        return json.loads((self.project_dir / "graph.json").read_text())

    def use_command_agent(self):
        g = self.graph()
        g["nodes"]["execute"] = {"kind": "agent", "instruction": "Implement the selected task.",
                                 "executor": {"type": "command", "argv": ["worker"]}, "inputs": {"task": "/task"}}
        (self.project_dir / "graph.json").write_text(json.dumps(g))

    def add_writeback(self, status):
        # The canonical graph has no writeback; a custom Project graph can add one explicitly.
        g = self.graph()
        g["nodes"]["writeback"] = {"kind": "action", "action": "writeback", "config": {"status": status},
                                   "inputs": {"task": "/task", "result": "/results/execute"}}
        g["flow"].append("writeback")
        (self.project_dir / "graph.json").write_text(json.dumps(g))

    @staticmethod
    def mutation_options(calls):
        return [c["request"]["variables"].get("option", "comment") for c in calls
                if c["command"] == "gh" and c["request"] and c["request"]["query"].startswith("mutation")]

    def cli(self, *args, mode="success", stdin=None, **env):
        (self.root / "projectweave.json").write_text(json.dumps(self.config))
        self.log.unlink(missing_ok=True)
        completed = subprocess.run([sys.executable, "-m", "projectweave", *args], cwd=self.root, input=stdin,
                                   env=dict(self.env, FAKE_MODE=mode, PYTHONPATH=str(ROOT), **env),
                                   capture_output=True, text=True, timeout=30)
        output = json.loads(completed.stdout)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return completed.returncode, output, calls

    def test_run_claims_then_runs_gitweave_in_issue_mode(self):
        code, outcome, calls = self.cli("run", "p")
        self.assertEqual(code, 0, outcome)
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(outcome["task"]["priority"], "P0")
        self.assertEqual(outcome["observations"], {})  # No resource policy: unconstrained, nothing observed.
        launch = [c for c in calls if c["command"] == "gitweave"]
        self.assertEqual(len(launch), 1)
        self.assertEqual(launch[0]["argv"][:7], ["run", "--graph", str(self.task_graph), "--repo", "o/r", "--issue", "7"])
        self.assertEqual(Path(launch[0]["cwd"]).resolve(), self.project_dir.resolve())  # .gitweave/ in the Project.
        self.assertFalse(any(c["command"] in ("git", "codex", "claude") or c["argv"][:2] == ["repo", "clone"] for c in calls))
        self.assertIn("task $(literal) `literal`", launch[0]["argv"][-1])
        record = outcome["record"]
        self.assertEqual(record["results"]["execute"]["references"], ["abc123"])
        self.assertTrue(record["results"]["execute"]["data"]["outputs"][0]["data"]["merged"])
        for field in ("items", "labels", "fieldValues", "fields"):
            pages = [c for c in calls if c["command"] == "gh" and c["request"] and field + "(first:" in c["request"]["query"]]
            self.assertEqual([p["request"]["variables"]["cursor"] for p in pages], [None, "next"])
        # claim marks In Progress before launch; the graph only runs GitWeave (no comment, no Done).
        self.assertEqual(self.mutation_options(calls), ["PROGRESS"])
        self.assertLess(calls.index(next(c for c in calls if c["request"] and "mutation" in c["request"]["query"])),
                        calls.index(launch[0]))
        self.assertEqual(list(record["results"]), ["execute"])
        # The claimed Task is In Progress now, so a second run finds no work.
        code, outcome, calls = self.cli("run", "p")
        self.assertEqual((code, outcome["status"]), (0, "no_work"))
        self.assertFalse(any(c["command"] == "gitweave" for c in calls))

    def test_claim_run_task_and_complete_are_separate(self):
        code, task, calls = self.cli("claim", "p")
        self.assertEqual(code, 0)
        self.assertEqual((task["number"], task["status"]), (7, "In Progress"))  # Selected Todo, now marked.
        self.assertEqual(self.mutation_options(calls), ["PROGRESS"])
        self.assertFalse(any(c["command"] == "gitweave" for c in calls))
        self.assertIsNone(self.cli("claim", "p")[1])  # Already In Progress.
        code, record, calls = self.cli("run-task", "p", "--task", "-", stdin=json.dumps(task))
        self.assertEqual(code, 0, record)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(self.mutation_options(calls), [])  # run-task never selects or changes Status.
        self.assertFalse(any(c["request"] and "items(first:" in c["request"]["query"] for c in calls if c["command"] == "gh"))
        (self.directory / "task.json").write_text(json.dumps(task))
        code, done, calls = self.cli("complete", "p", "--task", str(self.directory / "task.json"))
        self.assertEqual(code, 0, done)
        self.assertEqual(done["data"], {"status": "Done"})
        self.assertEqual(self.mutation_options(calls), ["DONE"])

    def test_custom_graph_complete_and_writeback_are_explicit(self):
        self.add_writeback("Done")
        code, outcome, calls = self.cli("run", "p")
        self.assertEqual(code, 0, outcome)
        mutations = [c for c in calls if c["command"] == "gh" and c["request"] and c["request"]["query"].startswith("mutation")]
        self.assertEqual(self.mutation_options(calls), ["PROGRESS", "comment", "DONE"])
        self.assertIn(outcome["record"]["run_id"], mutations[1]["request"]["variables"]["body"])
        self.assertIn("https://github.com/o/r/pull/12", mutations[1]["request"]["variables"]["body"])

    def test_agent_command_path(self):
        self.use_command_agent()
        code, outcome, calls = self.cli("run", "p")
        self.assertEqual(code, 0)
        worker = next(c for c in calls if c["command"] == "worker")
        self.assertEqual(worker["request"]["task"]["id"], "I")
        # The executor gets its own worktree of the shared checkout (never the checkout itself), removed afterwards.
        worktree = Path(worker["request"]["checkout"])
        self.assertEqual(worktree.parent.resolve(), (self.project_dir / "worktrees").resolve())
        self.assertTrue(worktree.name.startswith(outcome["record"]["run_id"] + "-execute-"))
        self.assertEqual(Path(worker["worktree_of"]).resolve(), (self.project_dir / "repos" / "o" / "r").resolve())
        self.assertFalse(worktree.exists())
        self.assertEqual(outcome["record"]["cleanup_failures"], [])
        self.assertIsInstance(worker["request"]["instruction"], str)
        self.assertFalse(any(c["command"] == "gitweave" for c in calls))
        self.assertTrue(outcome["record"]["results"]["execute"]["data"]["approved"])

    def test_no_work_not_ready_and_non_todo(self):
        for mode in ("empty", "not_ready"):
            with self.subTest(mode=mode):
                code, outcome, calls = self.cli("run", "p", mode=mode)
                self.assertEqual((code, outcome["status"]), (0, "no_work"))
                self.assertEqual(self.mutation_options(calls), [])
        project = json.loads((self.project_dir / "project.json").read_text())
        (self.project_dir / "project.json").write_text(json.dumps(dict(project, eligible_statuses=["Done"])))
        code, outcome, calls = self.cli("run", "p")
        self.assertEqual((code, outcome["status"]), (0, "no_work"))

    def test_resource_policy_admits_or_blocks_before_claim(self):
        self.config["projects"]["p"] = {"resources": {"codex": {"min_remaining_percent": 20, "estimated_usage_percent_per_task": 10}}}
        for used, extra, admitted in (("70", {}, True), ("75", {}, False), ("50", {"FAKE_USAGE": "codex_error"}, False)):
            with self.subTest(used=used, extra=extra):
                (self.directory / "github.json").unlink(missing_ok=True)
                code, outcome, calls = self.cli("run", "p", FAKE_CODEX_USED=used, **extra)
                self.assertEqual(code, 0, outcome)
                self.assertIn("codex", outcome["observations"])  # Observed (or failed) value in the output.
                if admitted:  # 30 remaining - 0 reserved - 10 estimate >= 20.
                    self.assertEqual(outcome["status"], "completed")
                else:  # 25 - 10 < 20, or unknown: nothing is claimed or launched.
                    self.assertEqual(outcome["status"], "not_admitted")
                    self.assertTrue(outcome["reason"].startswith("codex"))
                    self.assertEqual({c["command"] for c in calls}, {"codex"})

    def test_executor_failure_stays_in_progress_without_comment(self):
        for kind, command in (("gitweave", "gitweave"), ("command", "worker")):
            with self.subTest(kind=kind):
                if kind == "command":
                    self.use_command_agent()
                (self.directory / "github.json").unlink(missing_ok=True)
                code, outcome, calls = self.cli("run", "p", mode="executor_failure")
                self.assertEqual(code, 1)
                record = outcome["record"]
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["failure"]["kind"], "transport")
                self.assertEqual(record["failure"]["node"], "execute")
                self.assertEqual(len([c for c in calls if c["command"] == command]), 1)
                # The Task stays In Progress, so the next claim does not select it again; nothing is commented.
                self.assertEqual(self.mutation_options(calls), ["PROGRESS"])

    def test_existing_checkout_is_reused_after_origin_check(self):
        self.use_command_agent()  # Checkouts are resolved only for command executors.
        self.assertEqual(self.cli("run", "p")[0], 0)
        (self.directory / "github.json").unlink()
        code, outcome, calls = self.cli("run", "p")
        self.assertEqual(code, 0, outcome)
        self.assertFalse(any(c["argv"][:2] == ["repo", "clone"] for c in calls))
        git = [c["argv"][2:] for c in calls if c["command"] == "git"]
        self.assertEqual(git[:4], [["rev-parse", "--show-toplevel"], ["remote", "get-url", "origin"], ["fetch", "origin"],
                                   ["rev-parse", "--verify", "origin/HEAD^{commit}"]])
        self.assertEqual([g[:3] for g in git[4:]], [["worktree", "add", "--detach"], ["worktree", "remove", "--force"],
                                                    ["worktree", "prune"]])
        self.assertEqual(git[4][4], "origin/HEAD")

    def test_checkout_failures_stop_before_executor(self):
        self.use_command_agent()
        checkout = self.project_dir / "repos" / "o" / "r"
        cases = {"clone_failure": None, "fetch_failure": None,
                 "wrong_origin": "https://github.com/o/other.git\n", "not_checkout": ""}
        for case, origin in cases.items():
            with self.subTest(case=case):
                (self.directory / "github.json").unlink(missing_ok=True)
                if checkout.exists():
                    for path in checkout.iterdir():
                        path.unlink()
                    checkout.rmdir()
                if origin is not None:
                    checkout.mkdir(parents=True)
                    if origin:
                        (checkout / ".fake-origin").write_text(origin)
                    (checkout / "keep").write_text("user data")
                code, outcome, calls = self.cli("run", "p", mode=case if case.endswith("failure") else "success")
                self.assertEqual(code, 1, outcome)
                self.assertEqual(outcome["record"]["failure"]["kind"], "checkout")
                self.assertIn("o/r", outcome["record"]["failure"]["message"])
                self.assertFalse(any(c["command"] == "worker" for c in calls))
                self.assertEqual(self.mutation_options(calls), ["PROGRESS"])
                if origin is not None:
                    self.assertEqual((checkout / "keep").read_text(), "user data")
                    self.assertFalse(any(c["argv"][:2] == ["repo", "clone"] for c in calls))

    def test_each_command_invocation_gets_a_distinct_worktree(self):
        self.use_command_agent()
        g = self.graph()
        g["nodes"]["again"] = dict(g["nodes"]["execute"])
        g["flow"] = ["execute", "again"]  # Two invocations in one Run, like two concurrent Tasks would get.
        (self.project_dir / "graph.json").write_text(json.dumps(g))
        code, outcome, calls = self.cli("run", "p")
        self.assertEqual(code, 0, outcome)
        paths = [c["request"]["checkout"] for c in calls if c["command"] == "worker"]
        self.assertEqual(len(set(paths)), 2)
        self.assertFalse(any(Path(path).exists() for path in paths))

    def test_worktree_creation_and_cleanup_failures(self):
        self.use_command_agent()
        code, outcome, calls = self.cli("run", "p", mode="worktree_failure")
        self.assertEqual(code, 1, outcome)
        self.assertEqual(outcome["record"]["failure"]["kind"], "checkout")
        self.assertIn("worktree", outcome["record"]["failure"]["message"])
        self.assertFalse(any(c["command"] == "worker" for c in calls))  # Before launch.
        (self.directory / "github.json").unlink()
        code, outcome, calls = self.cli("run", "p", mode="cleanup_failure")
        self.assertEqual(code, 0, outcome)  # Cleanup failure is reported but does not change the outcome.
        self.assertEqual(outcome["record"]["status"], "completed")
        self.assertEqual(len(outcome["record"]["cleanup_failures"]), 1)
        self.assertIn("worktrees", outcome["record"]["cleanup_failures"][0]["worktree"])

    def test_claim_status_failure_launches_nothing(self):
        code, outcome, calls = self.cli("run", "p", mode="status_failure")
        self.assertEqual(code, 2)
        self.assertEqual(outcome["failure"]["kind"], "status")
        self.assertFalse(any(c["command"] == "gitweave" for c in calls))

    def test_partial_writeback_and_status_preflight(self):
        self.add_writeback("Done")
        code, outcome, _ = self.cli("run", "p", mode="writeback_failure")
        self.assertEqual(code, 1)
        self.assertEqual(outcome["record"]["failure"]["kind"], "writeback")
        self.assertEqual(len(outcome["record"]["failure"]["details"]["completed_references"]), 1)
        self.assertIn("execute", outcome["record"]["results"])
        self.add_writeback("Unknown")
        (self.directory / "github.json").unlink()
        code, outcome, calls = self.cli("run", "p")
        self.assertEqual(code, 1)
        self.assertEqual(outcome["record"]["failure"]["kind"], "writeback")
        self.assertFalse(any(c["command"] == "gh" and c["request"] and "addComment(" in c["request"]["query"] for c in calls))

    def test_graphql_error_and_invalid_input(self):
        code, outcome, calls = self.cli("run", "p", mode="graphql_error")
        self.assertEqual(code, 2)
        self.assertEqual(outcome["failure"]["kind"], "github")
        self.assertEqual(len(calls), 1)
        self.config["projects"]["p"] = {"resources": {"codex": {"min_remaining_percent": 101, "estimated_usage_percent_per_task": 1}}}
        code, _, calls = self.cli("run", "p")
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        self.config["projects"] = {"missing": {}}
        code, outcome, calls = self.cli("run", "p")
        self.assertEqual(code, 2)
        self.assertIn("missing", outcome["failure"]["message"])

    def test_invalid_poll_seconds_rejected_by_cli(self):
        for value in ("0", "-5"):
            completed = subprocess.run([sys.executable, "-m", "projectweave", "coordinate", "--poll-seconds", value],
                                       cwd=self.root, env=dict(self.env, PYTHONPATH=str(ROOT)), capture_output=True,
                                       text=True, timeout=30)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("positive", completed.stderr)

    def test_coordinate_once_across_projects_with_opt_in_admission(self):
        self.add_project("q", number=2)
        # q opts in: 50% remaining - 40% estimate < 20% minimum, so it is not admitted; p is unconstrained.
        self.config["projects"]["q"] = {"resources": {"codex": {"min_remaining_percent": 20, "estimated_usage_percent_per_task": 40}}}
        code, summary, calls = self.cli("coordinate", "--once")
        self.assertEqual(code, 0, summary)
        self.assertEqual([(run["project"], run["task"]["number"]) for run in summary["runs"]], [("p", 7)])
        self.assertEqual(summary["observations"], {"codex": {"windows": {"primary": 50}}})
        self.assertEqual(summary["reserved"], {})  # Released when the Task ended.
        self.assertEqual([c["argv"][6] for c in calls if c["command"] == "gitweave"], ["7"])
        # Loosen q's estimate: it is admitted and claims its own Task; p has no work left.
        self.config["projects"]["q"]["resources"]["codex"]["estimated_usage_percent_per_task"] = 10
        code, summary, calls = self.cli("coordinate", "--once")
        self.assertEqual([(run["project"], run["task"]["number"]) for run in summary["runs"]], [("q", 8)])


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

    def test_executor_timeout_default_finite_and_explicit_unlimited(self):
        record = {"status": "completed", "outputs": [{"commit": "sha", "message": "Closed", "data": {"status": "closed"}}],
                  "run_id": "run", "repository": "repo", "run_ref": "ref", "notes_ref": "notes"}
        for config in ({"type": "gitweave", "graph": "g"},
                       {"type": "command", "argv": ["worker"]}):
            raw = json.dumps(record if config["type"] == "gitweave" else
                             {"message": "Done", "data": {}, "references": [], "usage": {}})
            for extra, expected in (({}, 3600), ({"timeout": 10}, 10), ({"timeout": None}, None)):
                with self.subTest(config=config, extra=extra), \
                        patch("projectweave.executors.process", return_value=raw) as boundary:
                    invoke(dict(config, **extra), {"task": {"repository": "o/r", "number": 71}}, "workspace")
                    self.assertEqual(boundary.call_args.args[2], expected)

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
