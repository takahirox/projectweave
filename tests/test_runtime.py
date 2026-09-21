import copy
import json
import unittest
from unittest.mock import Mock
from projectweave.contracts import Failure, result, decode, pointer, equal
from projectweave.graph import validate
from projectweave.runtime import Runtime
from projectweave.resources import Resources
from projectweave.github import GitHub

PROJECT = {"owner": "example", "owner_type": "organization", "number": 1}
TASK = {"id": "I", "item_id": "ITEM", "project_id": "P", "state": "OPEN", "labels": ["projectweave-ready"],
        "priority": "P1", "status": "Todo", "created_at": "2026-01-01T00:00:00Z", "url": "https://github.com/o/r/issues/1"}


def envelope(amount=2, mode="reservation"):
    return {"ai": {"unit": "calls", "available": amount, "accounting": mode}}


def graph():
    return {"version": 1, "nodes": {
        "load": {"kind": "action", "action": "load"},
        "select": {"kind": "action", "action": "select", "inputs": {"items": "/results/load/data/items"}},
        "work": {"kind": "agent", "instruction": "Review this task", "executor": {"type": "command", "argv": ["fake"]},
                 "requires": {"ai": 1}, "inputs": {"task": "/results/select/data/task", "context": "/last"}},
        "post": {"kind": "action", "action": "writeback", "inputs": {"task": "/results/select/data/task", "result": "/last"}}
    }, "flow": ["load", "select", {"if": {"path": "/last/data/task", "equals": None, "then": [], "else": ["work", "post"]}}]}


class RuntimeTests(unittest.TestCase):
    def run_graph(self, g=None, env=None, executor=None, tasks=None):
        backend = Mock()
        backend.load.return_value = [TASK] if tasks is None else tasks
        backend.select.side_effect = GitHub(PROJECT).select
        backend.writeback.return_value = result("posted", references=["comment"])
        execute = executor or Mock(return_value=result("Rejected", {"approved": False}))
        record = Runtime(g or graph(), PROJECT, env if env is not None else envelope(), backend, execute).run()
        return record, backend, execute

    def test_handoff_agent_and_task_rejection(self):
        record, backend, execute = self.run_graph()
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["resources"]["ai"]["charged"], 1)
        request = execute.call_args.args[1]
        self.assertEqual(request["instruction"], "Review this task")
        self.assertEqual(request["task"], TASK)
        self.assertEqual(request["context"]["data"]["task"], TASK)
        self.assertFalse(backend.writeback.call_args.args[1]["data"]["approved"])

    def test_action_execute(self):
        g = graph()
        g["nodes"]["work"].update(kind="action", action="execute")
        del g["nodes"]["work"]["instruction"]
        record, _, execute = self.run_graph(g)
        self.assertEqual(record["status"], "completed")
        self.assertIsNone(execute.call_args.args[1]["instruction"])

    def test_empty_work(self):
        record, backend, execute = self.run_graph(tasks=[])
        self.assertIsNone(record["last"]["data"]["task"])
        execute.assert_not_called()
        backend.writeback.assert_not_called()

    def test_resource_exhausted_and_missing(self):
        for env in (envelope(0), {}):
            record, backend, execute = self.run_graph(env=env)
            execute.assert_not_called()
            self.assertEqual(record["results"]["work"]["data"]["status"], "resource_exhausted")
            self.assertEqual(record["status"], "completed")

    def test_failure_no_retry_retains_reservation(self):
        execute = Mock(side_effect=Failure("transport", "usage limit"))
        record, backend, _ = self.run_graph(executor=execute)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(record["failure"]["kind"], "transport")
        self.assertEqual(record["resources"]["ai"]["charged"], 1)
        backend.writeback.assert_not_called()

    def test_failure_diagnostic(self):
        g = graph()
        g["nodes"]["work"]["config"] = {"failure_comment": True}
        record, backend, _ = self.run_graph(g, executor=Mock(side_effect=Failure("launch", "missing")))
        self.assertEqual(record["failure"]["details"]["diagnostic_references"], ["comment"])
        self.assertEqual(backend.writeback.call_args.args[1]["data"]["failure"]["kind"], "launch")

    def test_accounting_and_invalid_output(self):
        for output, kind in ((result(usage={"ai": 2}), "accounting"), ({"message": "oops"}, "result")):
            record, backend, _ = self.run_graph(executor=Mock(return_value=output))
            self.assertEqual(record["failure"]["kind"], kind)
            backend.writeback.assert_not_called()

    def test_reported_refund_and_missing_report(self):
        record, _, _ = self.run_graph(env=envelope(mode="reported"), executor=Mock(return_value=result(usage={"ai": .25})))
        self.assertEqual(record["resources"]["ai"]["available"], 1.75)
        record, _, _ = self.run_graph(env=envelope(mode="reported"))
        self.assertEqual(record["failure"]["kind"], "accounting")
        self.assertEqual(record["resources"]["ai"]["charged"], 1)

    def test_step_limit_and_missing_path(self):
        g = graph()
        g["max_steps"] = 2
        record, _, execute = self.run_graph(g)
        self.assertEqual(record["failure"]["kind"], "limit")
        execute.assert_not_called()
        g = graph()
        g["nodes"]["work"]["inputs"]["task"] = "/missing"
        record, _, _ = self.run_graph(g)
        self.assertEqual(record["failure"]["kind"], "input")

    def test_conditional_resources_and_fallback(self):
        g = graph()
        g["nodes"]["inspect"] = {"kind": "action", "action": "resources", "config": {"requires": {"ai": 3}}}
        g["flow"] = ["load", "select", "inspect", {"if": {"path": "/last/data/available", "equals": False, "then": ["work"], "else": []}}]
        record, _, execute = self.run_graph(g)
        execute.assert_called_once()
        self.assertFalse(record["results"]["inspect"]["data"]["available"])

    def test_atomic_multi_resource_admission(self):
        resources = Resources(envelope())
        self.assertFalse(resources.reserve({"ai": 1, "missing": 1}))
        self.assertEqual(resources.state["ai"]["available"], 2)

    def test_no_hidden_refund_on_failure(self):
        resources = Resources(envelope())
        resources.reserve({"ai": 1})
        with self.assertRaises(Failure):
            resources.settle({"ai": 1}, {"ai": 3})
        self.assertEqual(resources.state["ai"]["charged"], 3)
        self.assertEqual(resources.state["ai"]["available"], -1)

    def test_validation_rejects_unselected_invalid_node(self):
        for change in (lambda g: g["nodes"]["work"].update(kind="planner"),
                       lambda g: g["nodes"]["work"].update(requires={"ai": True}),
                       lambda g: g.update(max_steps=False),
                       lambda g: g["nodes"]["work"]["executor"].update(timeout=0),
                       lambda g: g["flow"].append({"loop": {}}),
                       lambda g: g["flow"].append("unknown")):
            g = graph()
            change(g)
            with self.assertRaises(Failure):
                validate(g)

    def test_json_and_pointer(self):
        for value in ('{"x": 1, "x": 2}', '{"x": NaN}'):
            with self.assertRaises(Failure):
                decode(value)
        self.assertEqual(pointer({"a/b": {"~": [4]}}, "/a~1b/~0/0"), 4)
        self.assertFalse(equal({"x": [True]}, {"x": [1]}))
        with self.assertRaises(Failure):
            pointer([1], "/-1")

    def test_selection_priority_age_ties_and_status(self):
        backend = GitHub(PROJECT)
        tasks = [dict(TASK, id="old", priority="P2", created_at="2020"),
                 dict(TASK, id="priority", priority="P0", created_at="2025"),
                 dict(TASK, id="old-priority", priority="P0", created_at="2024"),
                 dict(TASK, id="excluded", priority="P0", created_at="2000", labels=[])]
        for ordering in (tasks, list(reversed(tasks))):
            self.assertEqual(backend.select(ordering)["id"], "old-priority")
        backend.config = dict(PROJECT, eligible_statuses=["Done"])
        self.assertIsNone(backend.select(tasks))
        backend.config = PROJECT
        self.assertEqual(backend.select([dict(TASK, priority=None), dict(TASK, priority="P2")])["priority"], "P2")

    def test_repeated_execution_cannot_reuse_reservation(self):
        g = graph()
        g["flow"] = ["load", "select", "work", "work"]
        record, _, execute = self.run_graph(g, env=envelope(1))
        execute.assert_called_once()
        self.assertEqual(record["last"]["data"]["status"], "resource_exhausted")
        self.assertEqual(len([e for e in record["events"] if e["node"] == "work"]), 2)

    def test_undeclared_usage_stops_run(self):
        record, backend, _ = self.run_graph(executor=Mock(return_value=result(usage={"other": 1})))
        self.assertEqual(record["failure"]["kind"], "accounting")
        backend.writeback.assert_not_called()

    def test_gitweave_reported_usage_rejected_before_launch(self):
        g = graph()
        g["nodes"]["work"]["executor"] = {"type": "gitweave", "graph": "g", "repo": "r", "commit": "HEAD"}
        record, _, execute = self.run_graph(g, env=envelope(mode="reported"))
        self.assertEqual(record["failure"]["kind"], "accounting")
        self.assertEqual(record["resources"]["ai"]["charged"], 0)
        execute.assert_not_called()

    def test_diagnostic_write_failure_keeps_primary_failure(self):
        g = graph()
        g["nodes"]["work"]["config"] = {"failure_comment": True}
        backend = Mock()
        backend.load.return_value = [TASK]
        backend.select.return_value = TASK
        backend.writeback.side_effect = Failure("writeback", "denied")
        record = Runtime(g, PROJECT, envelope(), backend,
                         Mock(side_effect=Failure("transport", "executor stopped"))).run()
        self.assertEqual(record["failure"]["kind"], "transport")
        self.assertEqual(record["failure"]["details"]["diagnostic_failure"]["kind"], "writeback")

    def test_deterministic_final_tiebreak_and_missing_priority(self):
        backend = GitHub(PROJECT)
        tasks = [dict(TASK, item_id="B"), dict(TASK, item_id="A"),
                 dict(TASK, priority=None, created_at="2000")]
        for ordering in (tasks, list(reversed(tasks))):
            self.assertEqual(backend.select(ordering)["item_id"], "A")
        with self.assertRaises(Failure):
            decode('{"value": 1e999}')
