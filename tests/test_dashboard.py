import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from projectweave.contracts import Failure
from projectweave.dashboard import Dashboard
from projectweave.execution import ExecutionRegistry, graph_view
from projectweave.executors import gitweave_progress, invoke, output_observer, process
from projectweave.workspace import run_task

ROOT = Path(__file__).resolve().parents[1]
TASK = {"number": 79, "title": '<script>alert("x")</script>', "repository": "o/r",
        "url": "https://github.com/o/r/issues/79"}
GRAPH = {"nodes": {"first": {"kind": "command"}, "last": {"kind": "agent"}}, "flow": ["first", "last"]}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        (self.directory / "gitweave.json").write_text(json.dumps(GRAPH))
        self.now = 0
        self.registry = ExecutionRegistry(10, log_lines=3, log_chars=10, clock=lambda: self.now)
        self.registry.add_project("p", {"owner": "o", "owner_type": "user", "number": 1})
        self.registry.add_project("idle")
        self.identity = self.registry.start("p", TASK, self.directory)

    def execution(self, kind="gitweave"):
        self.registry.event(self.identity, {"type": "executor_started", "execution_id": "execute-1",
                                           "config": {"type": kind, "graph": "gitweave.json"}})
        return self.registry.snapshot()["runs"][0]["executions"][-1]

    def test_duration_counts_terminal_states_and_restart(self):
        self.registry.event(self.identity, {"type": "run_started", "run_id": "pw"})
        self.now = 10
        self.assertFalse(self.registry.snapshot()["runs"][0]["long_running"])
        self.now = 11
        snapshot = self.registry.snapshot()
        self.assertEqual([p["running"] for p in snapshot["projects"]], [1, 0])
        self.assertEqual(snapshot["projects"][0]["long_running"], 1)
        self.assertTrue(snapshot["runs"][0]["long_running"])
        self.registry.finish(self.identity, {"status": "completed", "run_id": "pw"})
        self.now = 100
        snapshot = self.registry.snapshot()
        self.assertEqual(snapshot["runs"][0]["elapsed_seconds"], 11)
        self.assertEqual(snapshot["projects"][0]["running"], 0)
        self.assertEqual(snapshot["projects"][0]["long_running"], 0)
        failed = self.registry.start("p", TASK, self.directory)
        self.registry.finish(failed, {"status": "failed", "failure": {"message": "boom"}})
        self.assertEqual(self.registry.snapshot()["projects"][0]["failed"], 1)
        self.assertIsNotNone(self.registry.snapshot()["runs"][1]["ended_at"])
        self.assertEqual(ExecutionRegistry().snapshot()["runs"], [])

    def test_bounded_logs_and_detached_snapshots(self):
        for index in range(5):
            self.registry.event(self.identity, {"type": "stdout", "text": str(index) * 20})
        snapshot = self.registry.snapshot()
        self.assertEqual([log["text"] for log in snapshot["runs"][0]["logs"]], ['2' * 10, '3' * 10, '4' * 10])
        snapshot["runs"][0]["logs"][0]["text"] = "mutated"
        self.assertEqual(self.registry.snapshot()["runs"][0]["logs"][0]["text"], '2' * 10)

    def test_logs_preserve_explicit_node_and_execution_attribution(self):
        self.execution()
        self.registry.event(self.identity, {"type": "agent_output", "node_id": "first",
                                           "instance_id": "first-2", "text": "line one\nline two"})
        self.registry.event(self.identity, {"type": "stderr", "text": "raw output"})
        logs = self.registry.snapshot()["runs"][0]["logs"]
        for log in logs[:2]:
            self.assertEqual(log["node_id"], "first")
            self.assertEqual(log["instance_id"], "first-2")
            self.assertEqual(log["execution_id"], "execute-1")
        self.assertNotIn("node_id", logs[-1])
        self.assertEqual(logs[-1]["execution_id"], "execute-1")
        self.registry.event(self.identity, {"type": "executor_started", "execution_id": "execute-2",
                                           "config": {"type": "command"}})
        self.registry.event(self.identity, {"type": "stdout", "node_id": {"invalid": True}, "text": "next"})
        log = self.registry.snapshot()["runs"][0]["logs"][-1]
        self.assertEqual(log["execution_id"], "execute-2")
        self.assertNotIn("node_id", log)

    def test_graph_events_parallel_instances_and_unknown_progress(self):
        self.assertEqual([node["status"] for node in self.execution()["graph"]["nodes"]], ["not_executed"] * 2)
        for instance in ("first-1", "first-2"):
            self.registry.event(self.identity, {"type": "node_started", "node_id": "first", "instance_id": instance,
                                               "run_id": "gw"})
        self.registry.event(self.identity, {"type": "node_completed", "node_id": "first", "instance_id": "first-1"})
        execution = self.registry.snapshot()["runs"][0]["executions"][0]
        self.assertEqual(execution["current_nodes"], ["first"])
        self.assertEqual(execution["graph"]["nodes"][0]["status"], "active")
        self.registry.event(self.identity, {"type": "node_completed", "node_id": "first", "instance_id": "first-2"})
        self.registry.event(self.identity, {"type": "node_started", "node_id": "last"})
        self.registry.finish(self.identity, {"status": "failed", "failure": {"message": "interrupted"}})
        execution = self.registry.snapshot()["runs"][0]["executions"][0]
        self.assertEqual(execution["current_nodes"], [])
        self.assertEqual(execution["recent_node"], "last")
        self.assertEqual(execution["run_id"], "gw")
        self.assertEqual([node["status"] for node in execution["graph"]["nodes"]], ["completed", "unknown"])

    def test_generic_executor_and_missing_graph(self):
        self.assertIsNone(self.execution("command")["graph"])
        (self.directory / "gitweave.json").unlink()
        self.assertIsNotNone(self.execution()["graph_error"])

    def test_structured_flow_edges(self):
        graph = {"nodes": {name: {} for name in "abcdefg"}, "flow": ["a", {"parallel": [["b"], ["c"]]},
                 {"if": {"then": ["d"], "else": []}}, {"map": {"flow": ["e"]}},
                 {"loop": {"flow": ["f"]}}, "g"]}
        edges = graph_view(graph)["edges"]
        for edge in (["a", "b"], ["a", "c"], ["b", "d"], ["c", "d"], ["d", "e"], ["f", "f"], ["f", "g"]):
            self.assertIn(edge, edges)

    def test_concurrent_writes_and_snapshots(self):
        threads = [threading.Thread(target=lambda: [self.registry.event(self.identity, {"type": "stderr", "text": "x"})
                                                    for _ in range(100)]) for _ in range(4)]
        for thread in threads:
            thread.start()
        for _ in range(100):
            json.dumps(self.registry.snapshot())
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.registry.snapshot()["runs"][0]["logs"]), 3)


class WebTests(unittest.TestCase):
    def test_assets_project_run_api_and_shutdown(self):
        registry = ExecutionRegistry()
        registry.add_project("p")
        identity = registry.start("p", TASK, ".")
        with Dashboard(registry, port=0) as dashboard:
            self.assertEqual(dashboard.server.server_address[0], "127.0.0.1")
            for path, content_type in (("", "text/html"), ("dashboard.js", "text/javascript"), ("dashboard.css", "text/css")):
                with urlopen(dashboard.url + path, timeout=2) as response:
                    self.assertIn(content_type, response.headers["Content-Type"])
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertTrue(response.read())
            for path in ("api/state", "api/projects", "api/projects/p", "api/runs/" + identity):
                with urlopen(dashboard.url + path, timeout=2) as response:
                    data = json.load(response)
                    self.assertIsNotNone(data)
            with urlopen(dashboard.url + "api/runs/" + identity, timeout=2) as response:
                record = json.load(response)
                self.assertEqual(record["task"]["title"], TASK["title"])
                self.assertNotIn("_directory", record)
            for path in ("api/runs/missing", "api/projects/missing", "../project.json"):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(dashboard.url + path, timeout=2)
                self.assertEqual(caught.exception.code, 404)
                caught.exception.close()
        self.assertFalse(dashboard.thread.is_alive())
        with self.assertRaises(URLError):
            urlopen(dashboard.url, timeout=1)


class TelemetryTests(unittest.TestCase):
    def test_generic_command_keeps_json_contract_and_captures_logs(self):
        registry = ExecutionRegistry()
        registry.add_project("p")
        identity = registry.start("p", TASK, ".")
        config = {"type": "command", "argv": [sys.executable, "-c",
                  'import json,sys; request=json.load(sys.stdin); print("working",file=sys.stderr,flush=True); '
                  'print(json.dumps({"message":"done","data":{},"references":[],"usage":{}}))']}
        registry.event(identity, {"type": "executor_started", "execution_id": "execute-1", "config": config})
        output = invoke(config, {"task": TASK}, on_event=lambda event: registry.event(identity, event))
        self.assertEqual(output["message"], "done")
        snapshot = registry.snapshot()["runs"][0]
        self.assertEqual(snapshot["executor_type"], "command")
        self.assertIsNone(snapshot["executions"][0]["graph"])
        self.assertTrue(any(log["text"] == "working" for log in snapshot["logs"]))
        self.assertTrue(any(log["type"] == "stdout" for log in snapshot["logs"]))

    def test_streaming_receives_live_output_before_child_finishes(self):
        output, seen, release = [], threading.Event(), threading.Event()

        def callback(stream, text):
            output.append((stream, text))
            if stream == "stderr":
                seen.set()
                self.assertTrue(release.wait(3))

        errors = []
        def run():
            try:
                process([sys.executable, "-c", 'import sys,time; print("live",file=sys.stderr,flush=True); time.sleep(.3); print("result")'],
                        None, 3, on_output=callback)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(seen.wait(2))
            self.assertTrue(thread.is_alive())
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(errors)
        self.assertIn(("result", "result\n"), output)

    def test_streaming_timeout_and_failure_retain_output(self):
        for code, kind in (('import time; time.sleep(3)', "timeout"),
                           ('import sys; print("boom",file=sys.stderr,flush=True); sys.exit(7)', "transport")):
            events = []
            with self.assertRaises(Failure) as caught:
                process([sys.executable, "-c", code], None, .1, on_output=lambda *args: events.append(args))
            self.assertEqual(caught.exception.kind, kind)
            if kind == "transport":
                self.assertIn("boom", caught.exception.details["stderr"])

    def test_partial_json_events_and_terminal_outputs(self):
        events = []
        output = output_observer(events.append, gitweave=True)
        output("stderr", '{"type":"node_started","node_')
        output("stderr", 'id":"first"}\nordinary log\n')
        output("stderr", '{"type":"run_started","run_id":"gw"}\n')
        output("result", json.dumps({"run_id": "gw", "outputs": [{"node_id": "last"}]}))
        self.assertIn({"type": "node_started", "node_id": "first"}, events)
        self.assertIn({"type": "node_completed", "node_id": "last"}, events)
        self.assertIn({"type": "gitweave_run", "run_id": "gw"}, events)
        output("stderr", 'x' * 50000)
        output("stderr", '\n{"type":"command_stdout","text":"live"}\n')
        self.assertIn({"type": "command_stdout", "text": "live"}, events)

    def test_provenance_uses_real_git_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args, stdin=None):
                return subprocess.run(["git", "-C", directory, *args], input=stdin, text=True, check=True,
                                      capture_output=True).stdout.strip()
            git("init", "--bare", "--quiet")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.com")
            tree = git("mktree", stdin="")
            attempt = git("commit-tree", tree, stdin="attempt")
            notes = "refs/notes/gitweave/gw"
            git("notes", f"--ref={notes}", "add", "-F", "-", attempt,
                stdin=json.dumps({"node_id": "first", "instance_id": "first-1", "status": "completed"}))
            blob = git("hash-object", "-w", "--stdin", stdin=json.dumps({"attempts": [{"commit": attempt}]}))
            tree = git("mktree", stdin=f"100644 blob {blob}\trun.json\n")
            run_commit = git("commit-tree", tree, stdin="run")
            ref = "refs/gitweave/gw/run"
            git("update-ref", ref, run_commit)
            events = []
            gitweave_progress({"run_id": "gw", "repository": directory, "run_ref": ref, "notes_ref": notes}, events.append)
            self.assertIn({"type": "node_completed", "node_id": "first", "instance_id": "first-1"}, events)

    def test_run_task_observer_connects_graph_logs_and_run_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "project.json").write_text(json.dumps({"owner": "o", "owner_type": "user", "number": 1}))
            (path / "graph.json").write_text((ROOT / "projectweave/templates/graph.json").read_text())
            (path / "gitweave.json").write_text(json.dumps(GRAPH))
            registry = ExecutionRegistry()
            registry.add_project("p")
            identity = registry.start("p", TASK, path)
            record = {"run_id": "gw", "status": "completed", "repository": "missing", "run_ref": "r", "notes_ref": "n",
                      "outputs": [{"node_id": "last", "commit": "sha", "message": "done", "data": {}}]}
            def boundary(*args, on_output):
                on_output("stderr", '{"type":"node_started","node_id":"first"}\n')
                snapshot = registry.snapshot()["runs"][0]
                self.assertEqual(snapshot["executions"][0]["current_nodes"], ["first"])
                self.assertTrue(snapshot["logs"])
                on_output("stderr", '{"type":"node_completed","node_id":"first"}\n')
                on_output("result", json.dumps(record))
                return json.dumps(record)
            with patch("projectweave.executors.process", side_effect=boundary):
                result = run_task(path, TASK, observer=lambda event: registry.event(identity, event))
            registry.finish(identity, result)
            snapshot = registry.snapshot()["runs"][0]
            self.assertEqual(snapshot["run_id"], result["run_id"])
            execution = snapshot["executions"][0]
            self.assertEqual(execution["run_id"], "gw")
            self.assertEqual([node["status"] for node in execution["graph"]["nodes"]], ["completed", "completed"])
