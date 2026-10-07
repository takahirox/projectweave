import copy
from contextlib import nullcontext
import unittest
from unittest.mock import Mock, patch

from projectweave.contracts import Failure, result
from projectweave.graph import validate
from projectweave.runtime import Runtime


PROJECT = {"owner": "example", "owner_type": "organization", "number": 1}


def graph(kind="agent"):
    node = {"kind": kind, "executor": {"type": "command", "argv": ["worker"]},
            "inputs": {"context": ""}}
    node.update({"instruction": "Analyze the Project"} if kind == "agent" else {"action": "execute"})
    return {"version": 1, "nodes": {"work": node}, "flow": ["work"]}


class TasklessRuntimeTests(unittest.TestCase):
    def test_taskless_control_flow_handoff_and_resource_accounting(self):
        g = graph()
        g["nodes"]["work"]["requires"] = {"api": 1}
        g["nodes"]["after"] = {"kind": "action", "action": "result", "config": result("Finished")}
        g["flow"] = [{"if": {"path": "/project/number", "equals": 1,
                     "then": [{"loop": {"flow": ["work"], "while": {"path": "/last/data/again", "equals": True}}}],
                     "else": []}}, "after"]
        g["max_steps"] = 7
        outputs = [result("First", {"again": True}, usage={"api": .25}),
                   result("Second", {"again": False}, usage={"api": .5})]
        backend, checkout = Mock(), Mock()
        executor = Mock(side_effect=outputs)
        env = {"api": {"unit": "calls", "available": 2, "accounting": "reported"}}
        runtime = Runtime(g, PROJECT, env, backend, executor, checkout=checkout, workspace="/project",
                          operator_input="Analyze")
        record = runtime.run()
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["steps"], 7)
        self.assertEqual([event["node"] for event in record["events"]], ["work", "work", "after"])
        self.assertEqual(record["results"]["work"], outputs[1])
        self.assertEqual(record["last"], result("Finished"))
        self.assertEqual(record["resources"]["api"]["available"], 1.25)
        self.assertEqual(record["resources"]["api"]["charged"], .75)
        request = executor.call_args_list[1].args[1]
        self.assertEqual(request["context"]["last"], outputs[0])
        self.assertEqual(request["context"]["results"]["work"], outputs[0])
        self.assertEqual(request["project"], PROJECT)
        self.assertEqual(request["input"], "Analyze")
        self.assertEqual(executor.call_args.args[2], "/project")
        checkout.assert_not_called()
        self.assertEqual(backend.mock_calls, [])

    def test_taskless_loop_stops_at_step_limit(self):
        g = graph("action")
        g["flow"] = [{"loop": {"flow": ["work"], "while": {"path": "/last/data/again", "equals": True}}}]
        g["max_steps"] = 5
        executor = Mock(return_value=result(data={"again": True}))
        record = Runtime(g, PROJECT, executor=executor, workspace="/project").run()
        self.assertEqual((record["status"], record["failure"]["kind"], record["steps"]), ("failed", "limit", 6))
        self.assertEqual(executor.call_count, 2)

    def test_taskless_execution_needs_workspace_and_valid_result(self):
        executor = Mock(return_value={"message": "Invalid result"})
        record = Runtime(graph(), PROJECT, executor=executor).run()
        self.assertEqual(record["failure"]["kind"], "checkout")
        executor.assert_not_called()
        record = Runtime(graph(), PROJECT, executor=executor, workspace="/project").run()
        self.assertEqual(record["failure"]["kind"], "result")
        executor.assert_called_once()

    def test_explicit_task_input_remains_strict_and_uses_worktree(self):
        g = graph()
        g["nodes"]["work"]["inputs"]["task"] = "/task"
        for task in (None, "invalid", {"repository": "o/r"}):
            with self.subTest(task=task):
                executor = Mock(return_value=result())
                checkout = Mock(return_value=nullcontext("/worktree"))
                record = Runtime(g, PROJECT, executor=executor, checkout=checkout, workspace="/project", task=task).run()
                if isinstance(task, dict):
                    self.assertEqual(record["status"], "completed")
                    checkout.assert_called_once()
                    self.assertEqual(checkout.call_args.args[0], "o/r")
                    self.assertEqual(executor.call_args.args[1]["checkout"], "/worktree")
                    self.assertEqual(len(executor.call_args.args), 2)
                else:
                    self.assertEqual(record["failure"]["kind"], "input")
                    executor.assert_not_called()
                    checkout.assert_not_called()

    def test_gitweave_still_requires_issue_backed_task(self):
        g = graph()
        g["nodes"]["work"]["executor"] = {"type": "gitweave", "graph": "gitweave.json"}
        with self.assertRaisesRegex(Failure, "task input"):
            validate(g)
        g["nodes"]["work"]["inputs"]["task"] = "/task"
        for task in (None, {}, {"repository": "o/r", "number": True}, {"repository": "../r", "number": 1}):
            with self.subTest(task=task):
                executor = Mock()
                record = Runtime(g, PROJECT, executor=executor, workspace="/project", task=task).run()
                self.assertEqual(record["failure"]["kind"], "input")
                executor.assert_not_called()

    def test_task_actions_reject_missing_invalid_and_foreign_tasks(self):
        for action in ("status", "complete", "writeback"):
            node = {"kind": "action", "action": action, "inputs": {"task": "/task"}}
            if action == "status":
                node["config"] = {"status": "Done"}
            if action == "writeback":
                node["inputs"]["result"] = "/results/seed"
            g = {"version": 1, "nodes": {"seed": {"kind": "action", "action": "result", "config": result()},
                 "mutate": node}, "flow": ["seed", "mutate"]}
            missing = copy.deepcopy(g)
            del missing["nodes"]["mutate"]["inputs"]["task"]
            with self.subTest(action=action), self.assertRaises(Failure):
                validate(missing)
            for task in (None, "invalid", {}, {"id": "I", "item_id": "ITEM"},
                         {"id": "I", "item_id": "ITEM", "project_id": "OTHER"}):
                with self.subTest(action=action, task=task), patch("projectweave.github.GitHub.query") as query:
                    runtime = Runtime(g, PROJECT, task=task)
                    runtime.backend.project_id = "P"
                    record = runtime.run()
                    self.assertEqual(record["failure"]["kind"], "input")
                    query.assert_not_called()
