import copy
import json
import unittest
import unittest.mock
from unittest.mock import Mock
from projectweave.contracts import Failure, result, decode, pointer, equal
from projectweave.graph import validate
from projectweave.runtime import Runtime
from projectweave.resources import Resources
from projectweave.github import GitHub

PROJECT = {"owner": "example", "owner_type": "organization", "number": 1}
TASK = {"id": "I", "item_id": "ITEM", "project_id": "P", "state": "OPEN", "labels": [], "ai_execution": "Ready",
        "priority": "P1", "status": "Todo", "created_at": "2026-01-01T00:00:00Z", "url": "https://github.com/o/r/issues/1", "repository": "o/r"}


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
        record = Runtime(g or graph(), PROJECT, env if env is not None else envelope(), backend, execute,
                         checkout=lambda repository: "/workspace/repos/" + repository).run()
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

    def test_execution_failures_stop_without_writeback(self):
        for kind in ("agent", "action"):
            for failure_kind in ("launch", "timeout", "transport", "executor"):
                with self.subTest(kind=kind, failure=failure_kind):
                    g = graph()
                    if kind == "action":
                        g["nodes"]["work"].update(kind="action", action="execute")
                        del g["nodes"]["work"]["instruction"]
                    failure = Failure(failure_kind, "executor stopped", {"stderr": "details"})
                    execute = Mock(side_effect=failure)
                    record, backend, _ = self.run_graph(g, executor=execute)
                    self.assertEqual(record["status"], "failed")
                    self.assertEqual(record["failure"], {**failure.record(), "node": "work"})
                    self.assertEqual(list(record["results"]), ["load", "select"])
                    self.assertEqual([e["node"] for e in record["events"]], ["load", "select"])
                    self.assertEqual(record["last"], record["results"]["select"])
                    self.assertEqual(record["steps"], 4)
                    self.assertEqual(record["resources"]["ai"]["charged"], 1)
                    self.assertEqual(record["resources"]["ai"]["available"], 1)
                    execute.assert_called_once()
                    backend.writeback.assert_not_called()

    def test_accounting_and_invalid_output(self):
        output = result("Completed task", {"approved": False}, ["artifact"])
        for value, kind, charged in ((output, "accounting", 1), ({"message": "oops"}, "result", 1)):
            with self.subTest(kind=kind):
                record, backend, execute = self.run_graph(env=envelope(mode="reported"), executor=Mock(return_value=value))
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["failure"]["kind"], kind)
                self.assertEqual(record["failure"]["node"], "work")
                self.assertEqual(list(record["results"]), ["load", "select"])
                self.assertEqual(record["resources"]["ai"]["charged"], charged)
                if kind == "accounting":
                    self.assertEqual(record["failure"]["details"], {"usage": {}, "result": output})
                execute.assert_called_once()
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
        for second in ({}, {"other": {"unit": "tokens", "available": 0, "accounting": "reported"}}):
            with self.subTest(second=second):
                env = {**envelope(), **second}
                g = graph()
                g["nodes"]["work"]["requires"]["other"] = 1
                record, _, execute = self.run_graph(g, env=env)
                execute.assert_not_called()
                self.assertEqual(record["results"]["work"]["data"]["status"], "resource_exhausted")
                self.assertEqual(record["resources"], Resources(env).state)

    def test_mixed_settlement_and_subsequent_admission(self):
        for reported, calls, available, charged in ((0, 2, 2, 0), (.25, 2, 1.5, .5),
                                                    (1, 2, 0, 2), (3, 1, -1, 3)):
            with self.subTest(reported=reported):
                g = graph()
                g["nodes"]["work"]["requires"]["fixed"] = 1
                g["flow"] = ["load", "select", "work", "work"]
                env = {**envelope(mode="reported"),
                       "fixed": {"unit": "calls", "available": 4, "accounting": "reservation"}}
                output = result(usage={"ai": reported, "fixed": 99})
                record, _, execute = self.run_graph(g, env, Mock(return_value=output))
                self.assertEqual(record["status"], "completed")
                self.assertEqual(execute.call_count, calls)
                self.assertEqual(record["resources"]["ai"]["available"], available)
                self.assertEqual(record["resources"]["ai"]["charged"], charged)
                self.assertEqual(record["resources"]["fixed"]["available"], 4 - calls)
                self.assertEqual(record["resources"]["fixed"]["charged"], calls)
                if reported == 3:
                    self.assertEqual(record["last"]["data"]["status"], "resource_exhausted")
                    self.assertEqual(record["events"][2]["result"], output)

    def test_mixed_failure_retains_every_reservation(self):
        env = {**envelope(mode="reported"),
               "other": {"unit": "tokens", "available": 4, "accounting": "reported"},
               "fixed": {"unit": "calls", "available": 2, "accounting": "reservation"}}
        allocation = {"ai": 1, "other": 2, "fixed": 1}
        reserved = Resources(env)
        self.assertTrue(reserved.reserve(allocation))
        for first in (.25, 3):
            for invalid in (None, -1, True, "1", float("nan"), float("inf")):
                with self.subTest(first=first, invalid=invalid):
                    usage = {"ai": first, "fixed": 99}
                    if invalid is not None:
                        usage["other"] = invalid
                    # Direct settlement must also validate all reports before mutation.
                    resources = Resources(env)
                    resources.reserve(allocation)
                    with self.assertRaises(Failure):
                        resources.settle(allocation, usage)
                    self.assertEqual(resources.state, reserved.state)
                    g = graph()
                    g["nodes"]["work"]["requires"] = allocation
                    output = result(usage=usage)
                    record, backend, execute = self.run_graph(g, env, Mock(return_value=output))
                    self.assertEqual(record["failure"]["kind"], "accounting" if invalid is None else "result")
                    self.assertEqual(record["resources"], reserved.state)
                    execute.assert_called_once()
                    backend.writeback.assert_not_called()
        for kind in ("launch", "timeout", "transport", "executor"):
            with self.subTest(failure=kind):
                g = graph()
                g["nodes"]["work"]["requires"] = allocation
                record, backend, execute = self.run_graph(g, env, Mock(side_effect=Failure(kind, "stopped")))
                self.assertEqual(record["failure"]["kind"], kind)
                self.assertEqual(record["resources"], reserved.state)
                execute.assert_called_once()
                backend.writeback.assert_not_called()

    def test_reservation_ignores_valid_usage(self):
        for usage in ({}, {"ai": 0}, {"ai": .25}, {"ai": 1}, {"ai": 99}):
            with self.subTest(usage=usage):
                record, _, _ = self.run_graph(executor=Mock(return_value=result(usage=usage)))
                self.assertEqual(record["status"], "completed")
                self.assertEqual(record["resources"]["ai"]["available"], 1)
                self.assertEqual(record["resources"]["ai"]["charged"], 1)

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
                 dict(TASK, id="excluded", priority="P0", created_at="2000", ai_execution="Not ready"),
                 dict(TASK, id="unset", priority="P0", created_at="2000", ai_execution=None),
                 dict(TASK, id="label-only", priority="P0", created_at="2000", labels=["projectweave-ready"], ai_execution=None)]
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

    def test_unreserved_usage_ignored_but_result_validation_preserved(self):
        for mode in ("reservation", "reported"):
            for usage in ({"ai": .25, "other": 99, "unknown": 99},
                          {"ai": .25, "other": -1}, {"ai": .25, "unknown": True},
                          {"ai": -1}, {"": 1}, [1], None):
                with self.subTest(mode=mode, usage=usage):
                    env = {**envelope(mode=mode),
                           "other": {"unit": "tokens", "available": 2, "accounting": "reported"}}
                    output = result()
                    output["usage"] = usage
                    record, backend, _ = self.run_graph(env=env, executor=Mock(return_value=output))
                    valid = isinstance(usage, dict) and usage.get("unknown") == 99
                    self.assertEqual(record["status"], "completed" if valid else "failed")
                    self.assertEqual(record["resources"]["other"], Resources(env).state["other"])
                    charge = .25 if valid and mode == "reported" else 1
                    self.assertEqual(record["resources"]["ai"]["charged"], charge)
                    self.assertEqual(record["resources"]["ai"]["available"], 2 - charge)
                    if valid:
                        self.assertEqual(record["results"]["work"], output)
                    else:
                        self.assertEqual(record["failure"]["kind"], "result")
                        backend.writeback.assert_not_called()

    def test_gitweave_reported_usage_rejected_before_launch(self):
        g = graph()
        g["nodes"]["work"]["executor"] = {"type": "gitweave", "graph": "g"}
        record, _, execute = self.run_graph(g, env=envelope(mode="reported"))
        self.assertEqual(record["failure"]["kind"], "accounting")
        self.assertEqual(record["resources"]["ai"]["charged"], 0)
        execute.assert_not_called()

    def test_execution_config_rejects_removed_option_before_io(self):
        for kind in ("agent", "action"):
            for value in (True, False):
                with self.subTest(kind=kind, value=value):
                    g = graph()
                    if kind == "action":
                        g["nodes"]["work"].update(kind="action", action="execute")
                        del g["nodes"]["work"]["instruction"]
                    g["nodes"]["work"]["config"] = {"failure_comment": value}
                    backend, execute = Mock(), Mock()
                    with self.assertRaises(Failure) as caught:
                        Runtime(g, PROJECT, envelope(), backend, execute)
                    self.assertEqual(caught.exception.kind, "validation")
                    self.assertEqual(backend.mock_calls, [])
                    execute.assert_not_called()

    def test_execution_accepts_empty_config(self):
        g = graph()
        g["nodes"]["work"]["config"] = {}
        record, _, execute = self.run_graph(g)
        self.assertEqual(record["status"], "completed")
        execute.assert_called_once()

    def test_deterministic_final_tiebreak_and_missing_priority(self):
        backend = GitHub(PROJECT)
        tasks = [dict(TASK, item_id="B"), dict(TASK, item_id="A"),
                 dict(TASK, priority=None, created_at="2000")]
        for ordering in (tasks, list(reversed(tasks))):
            self.assertEqual(backend.select(ordering)["item_id"], "A")
        with self.assertRaises(Failure):
            decode('{"value": 1e999}')


class CheckoutTests(unittest.TestCase):
    def test_gitweave_repo_and_commit_are_rejected_with_migration_hint(self):
        for extra in ({"repo": "/r"}, {"commit": "HEAD"}):
            g = graph()
            g["nodes"]["work"]["executor"] = {"type": "gitweave", "graph": "g", **extra}
            with self.assertRaises(Failure) as caught:
                validate(g)
            self.assertIn("resolved per Task", str(caught.exception))

    def test_execution_without_workspace_fails_before_launch(self):
        backend = Mock()
        backend.load.return_value = [TASK]
        backend.select.side_effect = GitHub(PROJECT).select
        execute = Mock()
        record = Runtime(graph(), PROJECT, envelope(), backend, execute).run()
        self.assertEqual(record["failure"]["kind"], "checkout")
        execute.assert_not_called()
        backend.writeback.assert_not_called()

    def test_invalid_task_repository_never_touches_disk(self):
        from projectweave.checkout import resolve
        for repository in (None, "", "o", "o/..", "../o/r", "o/r/x", "o/r;x"):
            with self.subTest(repository=repository):
                with unittest.mock.patch("projectweave.checkout.process") as process:
                    with self.assertRaises(Failure) as caught:
                        resolve("/nonexistent-workspace", repository)
                self.assertEqual(caught.exception.kind, "checkout")
                process.assert_not_called()
