import copy
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import urlopen
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

    def test_run_graph_executes_selected_graph_without_task_or_external_setup(self):
        for kind in ("agent", "action"):
            with self.subTest(kind=kind):
                node = {"kind": kind, "executor": {"type": "command", "argv": ["worker"]},
                        "inputs": {"context": ""}}
                node.update({"instruction": "Analyze the Project"} if kind == "agent" else {"action": "execute"})
                selected = self.project_dir / "graphs" / "analysis.json"
                selected.parent.mkdir(exist_ok=True)
                selected.write_text(json.dumps({"version": 1, "nodes": {"analyze": node}, "flow": ["analyze"]}))
                # Neither the canonical Task graph nor root resource/coordinator config is used.
                (self.project_dir / "graph.json").write_text("invalid Task graph")
                self.config = {"invalid": True}
                code, record, calls = self.cli("run-graph", "p", "--graph", "graphs/analysis.json",
                                               "--input", "Find problems $(literal) `literal`")
                self.assertEqual((code, record["status"]), (0, "completed"), record)
                self.assertEqual([call["command"] for call in calls], ["worker"])
                self.assertEqual(Path(calls[0]["cwd"]).resolve(), self.project_dir.resolve())
                request = calls[0]["request"]
                project = json.loads((self.project_dir / "project.json").read_text())
                self.assertEqual(request["project"], project)
                self.assertIsNone(request["task"])
                self.assertNotIn("checkout", request)
                self.assertEqual(request["input"], "Find problems $(literal) `literal`")
                context = request["context"]
                self.assertEqual(context["project"], project)
                self.assertEqual(context["run_id"], record["run_id"])
                self.assertEqual(context["resources"], {})
                self.assertEqual(context["results"], {})
                self.assertIsNone(context["last"])
                self.assertIsNone(context["task"])
                self.assertEqual(context["input"], request["input"])
                self.assertEqual(record["last"]["data"], {"approved": True})
                self.assertFalse((self.project_dir / ".projectweave.lock").exists())
                self.assertFalse((self.project_dir / "repos").exists())
                self.assertFalse((self.project_dir / "worktrees").exists())

    def test_run_graph_documented_example_executes_without_operator_input(self):
        (self.project_dir / "analysis.json").write_text((ROOT / "examples/project-context.json").read_text())
        code, record, calls = self.cli("run-graph", "p", "--graph", "analysis.json")
        self.assertEqual((code, record["status"]), (0, "completed"), record)
        self.assertEqual(record["last"]["data"]["project"], json.loads((self.project_dir / "project.json").read_text()))
        self.assertIsNone(record["last"]["data"]["input"])
        self.assertEqual(calls, [])

    def test_run_graph_validation_precedes_execution(self):
        valid = {"version": 1, "nodes": {"work": {"kind": "action", "action": "execute",
                 "executor": {"type": "command", "argv": ["worker"]}}}, "flow": ["work"]}
        path = self.project_dir / "analysis.json"
        project_path = self.project_dir / "project.json"
        project = project_path.read_text()
        for invalid in ("not JSON", json.dumps({**valid, "flow": ["work", "missing"]}),
                        json.dumps({**valid, "nodes": {**valid["nodes"], "unreachable": {"kind": "unknown"}}})):
            with self.subTest(invalid=invalid):
                path.write_text(invalid)
                code, record, calls = self.cli("run-graph", "p", "--graph", "analysis.json")
                self.assertEqual((code, record["status"]), (2, "failed"))
                self.assertEqual(calls, [])
        path.write_text(json.dumps(valid))
        project_path.write_text(json.dumps({"owner": "example"}))
        code, record, calls = self.cli("run-graph", "p", "--graph", "analysis.json")
        self.assertEqual((code, record["status"]), (2, "failed"))
        self.assertEqual(calls, [])
        project_path.write_text(project)

    def test_run_graph_rejects_unsafe_or_missing_paths(self):
        outside = self.directory / "outside.json"
        outside.write_text(json.dumps({"version": 1, "nodes": {"work": {"kind": "action", "action": "execute",
                           "executor": {"type": "command", "argv": ["worker"]}}}, "flow": ["work"]}))
        (self.project_dir / "escape.json").symlink_to(outside)
        for path in (str(outside), "../p2/graph.json", "escape.json", "missing.json", "", "C:/outside.json", "..\\outside.json"):
            with self.subTest(path=path):
                code, record, calls = self.cli("run-graph", "p", "--graph", path)
                self.assertEqual((code, record["status"]), (2, "failed"))
                self.assertEqual(calls, [])
        for project in ("missing", "../p"):
            code, record, calls = self.cli("run-graph", project, "--graph", "graph.json")
            self.assertEqual((code, record["status"]), (2, "failed"))
            self.assertEqual(calls, [])

    def test_run_graph_requires_project_and_graph_arguments(self):
        for args in (("run-graph",), ("run-graph", "p"), ("run-graph", "--graph", "graph.json"),
                     ("run-graph", "p", "--graph", "graph.json", "--task", "-")):
            with self.subTest(args=args):
                completed = subprocess.run([sys.executable, "-m", "projectweave", *args], cwd=self.root,
                                           env=dict(self.env, PYTHONPATH=str(ROOT)), capture_output=True, text=True, timeout=30)
                self.assertEqual(completed.returncode, 2)
                self.assertIn("usage:", completed.stderr)
                self.assertFalse(self.log.exists())

    def test_run_graph_executor_failure_returns_runtime_failure_without_retry(self):
        (self.project_dir / "analysis.json").write_text(json.dumps({"version": 1, "nodes": {
            "work": {"kind": "action", "action": "execute", "executor": {"type": "command", "argv": ["worker"]}}},
            "flow": ["work", "work"]}))
        code, record, calls = self.cli("run-graph", "p", "--graph", "analysis.json", mode="executor_failure")
        self.assertEqual((code, record["status"], record["failure"]["kind"]), (1, "failed", "transport"))
        self.assertEqual([call["command"] for call in calls], ["worker"])
        self.assertEqual(record["results"], {})

    def configure_graph_routes(self):
        path = self.project_dir / "project.json"
        project = json.loads(path.read_text())
        project["graph_routes"] = [{"label": "task", "graph": "routed graph.json"},
                                   {"label": "other", "graph": "other.json"}]
        path.write_text(json.dumps(project))
        for name in ("routed graph.json", "other.json"):
            (self.project_dir / name).write_text(self.task_graph.read_text())

    def test_label_routes_validate_before_claim_and_launch_selected_graph(self):
        self.configure_graph_routes()
        code, outcome, calls = self.cli("run", "p")
        self.assertEqual(code, 0, outcome)
        launch = next(c for c in calls if c["command"] == "gitweave" and c["argv"][0] == "run")
        self.assertEqual(launch["argv"][2], "routed graph.json")
        self.assertEqual(outcome["record"]["executions"][0]["config"]["graph"], "routed graph.json")
        claim_index = next(i for i, c in enumerate(calls) if c["request"] and "mutation" in c["request"]["query"])
        before_claim = {Path(c["argv"][2]).name for c in calls[:claim_index]
                        if c["command"] == "gitweave" and c["argv"][0] == "validate"}
        self.assertEqual(before_claim, {"routed graph.json", "other.json", "gitweave.json"})
        self.assertFalse(any(c["command"] in ("codex", "claude") for c in calls))

    def test_broken_unmatched_route_never_claims_or_launches_from_any_entry_point(self):
        self.configure_graph_routes()
        for contents in (None, "{}"):
            path = self.project_dir / "other.json"
            if contents is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(contents)
            for args in (("run", "p"), ("claim", "p"), ("coordinate", "--once")):
                with self.subTest(contents=contents, args=args):
                    code, outcome, calls = self.cli(*args)
                    self.assertNotEqual(code, 0, outcome)
                    failure = outcome["runs"][0]["setup_failure"] if args[0] == "coordinate" else outcome["failure"]
                    self.assertEqual(failure["kind"], "setup")
                    self.assertIn("other.json", failure["message"])
                    self.assertEqual(self.mutation_options(calls), [])
                    self.assertFalse(any(c["command"] == "gitweave" and c["argv"][0] == "run" for c in calls))

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

    def test_no_work_and_non_todo(self):
        code, outcome, calls = self.cli("run", "p", mode="empty")
        self.assertEqual((code, outcome["status"]), (0, "no_work"))
        self.assertEqual(self.mutation_options(calls), [])
        project = json.loads((self.project_dir / "project.json").read_text())
        (self.project_dir / "project.json").write_text(json.dumps(dict(project, eligible_statuses=["Done"])))
        code, outcome, calls = self.cli("run", "p")
        self.assertEqual((code, outcome["status"]), (0, "no_work"))

    def test_runtime_rechecks_labels_on_existing_project_members(self):
        path = self.project_dir / "project.json"
        project = json.loads(path.read_text())
        path.write_text(json.dumps(dict(project, repository="o/r", required_labels=["task"], excluded_labels=["draft"])))
        for mode in ("missing_task_label", "draft_label"):
            with self.subTest(mode=mode):
                code, outcome, calls = self.cli("run", "p", mode=mode)
                self.assertEqual((code, outcome["status"]), (0, "no_work"))
                self.assertEqual(self.mutation_options(calls), [])
                self.assertFalse(any(c["command"] == "gitweave" for c in calls))
        code, outcome, calls = self.cli("run", "p")
        self.assertEqual((code, outcome["status"]), (0, "completed"))
        self.assertNotIn("ai_execution", outcome["task"])
        self.assertEqual(self.mutation_options(calls), ["PROGRESS"])

    def test_eligibility_changes_do_not_stop_claimed_execution(self):
        code, task, _ = self.cli("claim", "p")
        self.assertEqual(code, 0)
        path = self.project_dir / "project.json"
        path.write_text(json.dumps(dict(json.loads(path.read_text()), required_labels=["new-label"])))
        code, record, calls = self.cli("run-task", "p", "--task", "-", stdin=json.dumps(task))
        self.assertEqual((code, record["status"]), (0, "completed"))
        self.assertTrue(any(c["command"] == "gitweave" for c in calls))
        self.assertEqual(self.mutation_options(calls), [])

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
                code, outcome, calls = self.cli("run", "p")
                self.assertEqual((code, outcome["status"]), (0, "no_work"))
                self.assertEqual(self.mutation_options(calls), [])
                self.assertFalse(any(c["command"] == command for c in calls))

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

    def test_web_coordinates_live_events_duration_and_graceful_shutdown(self):
        (self.root / "projectweave.json").write_text(json.dumps(self.config))
        release = self.directory / "release"
        record = {"status": "completed", "run_id": "live-gw", "repository": "missing",
                  "run_ref": "ref", "notes_ref": "notes", "outputs": [
                      {"node_id": "issue_route", "commit": "sha", "message": "done", "data": {}}]}
        (self.bin / "gitweave").write_text(f"#!{sys.executable}\n" + "\n".join([
            "import json,sys,time", "from pathlib import Path",
            'print(json.dumps({"type":"run_started","run_id":"live-gw"}),file=sys.stderr,flush=True)',
            'print(json.dumps({"type":"node_started","node_id":"issue_route"}),file=sys.stderr,flush=True)',
            'print(json.dumps({"type":"agent_output","text":"working live"}),file=sys.stderr,flush=True)',
            f"while not Path({str(release)!r}).exists(): time.sleep(.02)",
            'print(json.dumps({"type":"node_completed","node_id":"issue_route"}),file=sys.stderr,flush=True)',
            f"print(json.dumps({record!r}),flush=True)",
        ]))
        child = subprocess.Popen([sys.executable, "-m", "projectweave", "coordinate", "--web", "--web-port", "0",
                                  "--poll-seconds", "1", "--long-running-seconds", "1"],
                                 cwd=self.root, env=dict(self.env, PYTHONPATH=str(ROOT)),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # The URL is announced before any claims, while stdout keeps its normal summary contract.
            url = json.loads(child.stderr.readline())["dashboard_url"]
            def snapshot():
                with urlopen(url + "api/state", timeout=2) as response:
                    return json.load(response)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                state = snapshot()
                if state["runs"] and any(log["text"] == "working live" for log in state["runs"][0]["logs"]):
                    break
                time.sleep(.02)
            self.assertTrue(state["runs"])
            execution = state["runs"][0]["executions"][0]
            self.assertEqual(execution["current_nodes"], ["issue_route"])
            self.assertEqual(execution["run_id"], "live-gw")
            self.assertEqual(state["projects"][0]["running"], 1)
            before = state["runs"][0]["elapsed_seconds"]
            while time.monotonic() < deadline and not state["projects"][0]["long_running"]:
                time.sleep(.02)
                state = snapshot()
            self.assertGreater(state["runs"][0]["elapsed_seconds"], before)
            self.assertEqual(state["projects"][0]["long_running"], 1)
            child.send_signal(signal.SIGTERM)
            self.assertEqual(snapshot()["projects"][0]["running"], 1)  # Server remains available while draining.
            release.touch()
            stdout, stderr = child.communicate(timeout=8)
            self.assertEqual(child.returncode, 0, stderr)
            summary = json.loads(stdout)
            self.assertEqual(summary["runs"][0]["record"]["status"], "completed")
            with self.assertRaises(URLError):
                urlopen(url, timeout=1)
        finally:
            release.touch()
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_web_once_preserves_executor_failure_exit_status(self):
        code, summary, _ = self.cli("coordinate", "--once", "--web", "--web-port", "0", mode="executor_failure")
        self.assertEqual(code, 1, summary)
        self.assertEqual(summary["runs"][0]["record"]["failure"]["kind"], "transport")


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
