import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from projectweave.contracts import Failure, result
from projectweave.graph import validate
from projectweave.runtime import Runtime


PROJECT = {"owner": "example", "owner_type": "organization", "number": 1}


def loop(body, path="/last/data/again", equals=True):
    return {"loop": {"flow": body, "while": {"path": path, "equals": equals}}}


def branch(body):
    return {"if": {"path": "/project/number", "equals": 1, "then": body, "else": []}}


def graph(body=None, **options):
    return {"version": 1, "nodes": {
        "seed": {"kind": "action", "action": "result", "config": result(data={"task": {}})},
        "work": {"kind": "agent", "instruction": "Review", "executor": {"type": "command", "argv": ["fake"]},
                 "requires": {"ai": 1}, "inputs": {"task": "/results/seed/data/task", "context": "/last"}},
        "after": {"kind": "action", "action": "load"}},
        "flow": ["seed", loop(["work"] if body is None else body), "after"], **options}


class LoopTests(unittest.TestCase):
    def run_graph(self, g, outputs=(), mode="reservation", amount=5):
        backend = Mock()
        backend.load.return_value = []
        execute = Mock(side_effect=outputs)
        env = {"ai": {"unit": "calls", "available": amount, "accounting": mode}}
        record = Runtime(g, PROJECT, env, backend, execute).run()
        return record, backend, execute

    def test_post_condition_and_state_retention(self):
        for mode, usage in (("reservation", {}), ("reported", {"ai": .25})):
            with self.subTest(mode=mode):
                outputs = [result("first", {"again": True}, usage=usage),
                           result("second", {"again": False}, usage=usage)]
                record, backend, execute = self.run_graph(graph(), outputs, mode)
                self.assertEqual(record["status"], "completed")
                self.assertEqual(record["steps"], 7)  # seed + entry + 2 starts + 2 nodes + after
                self.assertEqual(record["results"]["work"], outputs[1])
                self.assertEqual([e["node"] for e in record["events"]], ["seed", "work", "work", "after"])
                self.assertEqual(record["events"][1]["result"], outputs[0])
                requests = [call.args[1] for call in execute.call_args_list]
                self.assertEqual(requests[1]["context"], outputs[0])
                charge = 1 if mode == "reservation" else .25
                self.assertEqual(requests[1]["resources"]["ai"]["available"], 4 - charge)
                self.assertEqual(record["resources"]["ai"]["charged"], 2 * charge)
                self.assertEqual(requests[0]["run_id"], requests[1]["run_id"])
                backend.load.assert_called_once()

    def test_first_body_runs_even_when_condition_already_false(self):
        g = graph()
        g["flow"][1]["loop"]["while"] = {"path": "/project/number", "equals": 2}
        record, _, execute = self.run_graph(g, [result()])
        self.assertEqual(record["status"], "completed")
        execute.assert_called_once()
        self.assertEqual(record["steps"], 5)

    def test_iteration_and_node_boundaries_never_launch_after_limit(self):
        for limit, calls, active in ((1, 0, "loop"), (2, 0, "loop"), (3, 0, "work"),
                                     (4, 1, "loop"), (5, 1, "work"), (6, 2, "loop")):
            with self.subTest(limit=limit):
                record, backend, execute = self.run_graph(
                    graph(max_steps=limit), [result(data={"again": True})] * 3)
                self.assertEqual(record["failure"]["kind"], "limit")
                self.assertEqual(record["failure"]["node"], active)
                self.assertEqual(record["steps"], limit + 1)
                self.assertEqual(execute.call_count, calls)
                backend.load.assert_not_called()

    def test_exact_budget_can_complete(self):
        record, _, execute = self.run_graph(graph(max_steps=5), [result(data={"again": False})])
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["steps"], 5)
        execute.assert_called_once()

    def test_empty_branch_iterations_are_bounded_and_preserve_last(self):
        g = graph([branch([])], max_steps=8)
        g["flow"][1]["loop"]["while"] = {"path": "/project/number", "equals": 1}
        record, backend, execute = self.run_graph(g)
        self.assertEqual(record["failure"]["kind"], "limit")
        self.assertEqual(record["failure"]["node"], "loop")
        self.assertEqual(record["steps"], 9)
        self.assertEqual(record["last"], record["results"]["seed"])
        self.assertEqual(len(record["events"]), 1)
        backend.load.assert_not_called()
        execute.assert_not_called()

    def test_missing_condition_stops_after_body(self):
        record, backend, execute = self.run_graph(graph(), [result()])
        self.assertEqual(record["failure"]["kind"], "input")
        self.assertEqual(record["failure"]["node"], "loop")
        self.assertEqual(record["last"], record["results"]["work"])
        execute.assert_called_once()
        backend.load.assert_not_called()

    def test_body_failures_stop_without_condition_retry_or_later_nodes(self):
        for output, mode, kind in ((Failure("executor", "stopped"), "reservation", "executor"),
                                   ({}, "reservation", "result"), (result(), "reported", "accounting")):
            with self.subTest(kind=kind):
                record, backend, execute = self.run_graph(graph(["work", "after"]), [output], mode)
                self.assertEqual(record["failure"]["kind"], kind)
                self.assertEqual(record["failure"]["node"], "work")
                self.assertEqual(list(record["results"]), ["seed"])
                self.assertEqual(record["resources"]["ai"]["charged"], 1)
                execute.assert_called_once()
                backend.load.assert_not_called()

    def test_resources_are_not_reset_between_iterations(self):
        g = graph()
        g["flow"][1]["loop"]["while"] = {"path": "/resources/ai/available", "equals": 0}
        record, backend, execute = self.run_graph(g, [result()], amount=1)
        self.assertEqual(record["failure"]["kind"], "limit")
        self.assertEqual(record["last"]["data"]["status"], "resource_exhausted")
        self.assertEqual(record["resources"]["ai"]["charged"], 1)
        execute.assert_called_once()
        backend.load.assert_not_called()

    def test_existing_pointer_and_recursive_type_sensitive_equality(self):
        for actual, expected, repeats in ((False, 0, False), (1, 1.0, False),
                                          ({"x": [True]}, {"x": [1]}, False),
                                          ({"x": [True]}, {"x": [True]}, True), (None, None, True)):
            with self.subTest(actual=actual, expected=expected):
                g = graph(max_steps=5)
                g["flow"][1]["loop"]["while"] = {"path": "/results/work/data/a~1b/~0/0", "equals": expected}
                record, _, execute = self.run_graph(g, [result(data={"a/b": {"~": [actual]}})])
                self.assertEqual(record["status"], "failed" if repeats else "completed")
                if repeats:
                    self.assertEqual(record["failure"]["kind"], "limit")
                execute.assert_called_once()

    def test_mixed_nested_execution_uses_one_step_budget(self):
        g = graph([branch([loop(["work"])])], max_steps=9)
        record, _, execute = self.run_graph(g, [result(data={"again": True}), result(data={"again": False})])
        self.assertEqual(record["failure"]["node"], "after")
        self.assertEqual(record["failure"]["kind"], "limit")
        self.assertEqual(record["steps"], 10)
        self.assertEqual(execute.call_count, 2)

    def test_invalid_nested_controls_fail_before_external_work(self):
        invalid = [None, {}, {"loop": None}, {"loop": {}}, {"loop": {"flow": ["work"]}},
                   loop([]), loop("work"), loop(["unknown"]), loop(["work"], "bad"),
                   loop(["work"], "/bad~2"), loop([{"if": {}}]), loop([{"loop": {}}]),
                   {"loop": {"flow": ["work"], "while": {"path": ""}}},
                   {"loop": {"flow": ["work"], "while": {"path": "", "equals": None, "extra": 1}}},
                   {"loop": {"flow": ["work"], "while": None}},
                   {**loop(["work"]), "if": branch([])["if"]}]
        extra = loop(["work"])
        extra["loop"]["extra"] = True
        invalid.append(extra)
        for entry in invalid:
            with self.subTest(entry=entry):
                g = graph()
                g["flow"] = ["after", branch([])]
                g["flow"][1]["if"]["else"] = [loop([entry])]
                backend, execute = Mock(), Mock()
                with self.assertRaises(Failure) as caught:
                    Runtime(g, PROJECT, {}, backend, execute)
                self.assertEqual(caught.exception.kind, "validation")
                self.assertEqual(backend.mock_calls, [])
                execute.assert_not_called()

    def test_mixed_nesting_limit(self):
        body = ["seed"]
        for i in range(32):
            body = [branch(body) if i % 2 else loop(body, "/project/number", 2)]
        g = graph()
        g["flow"] = body
        validate(g)
        record, _, _ = self.run_graph(g)
        self.assertEqual(record["status"], "completed")
        g["flow"] = [loop(body)]
        with self.assertRaisesRegex(Failure, "Control nesting exceeds 32"):
            validate(g)

    def test_review_fix_example_skips_fix_after_approval(self):
        example = json.loads((Path(__file__).resolve().parents[1] / "examples/review-fix.json").read_text())
        for rejected_first in (False, True):
            with self.subTest(rejected_first=rejected_first):
                backend = Mock()
                backend.load.return_value = [{"id": "task"}]
                backend.select.return_value = {"id": "task"}
                outputs = ([result(data={"approved": False}), result("Fixed")] if rejected_first else [])
                outputs.append(result(data={"approved": True}))
                execute = Mock(side_effect=outputs)
                record = Runtime(copy.deepcopy(example), PROJECT,
                                 {"ai": {"unit": "calls", "available": 3, "accounting": "reservation"}},
                                 backend, execute).run()
                self.assertEqual(record["status"], "completed")
                self.assertEqual([e["node"] for e in record["events"]],
                                 ["load", "select"] + (["review", "fix"] if rejected_first else []) + ["review"])
                self.assertEqual(execute.call_count, 3 if rejected_first else 1)
