import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from projectweave import usage
from projectweave.contracts import Failure, result
from projectweave.graph import validate
from projectweave.resources import Resources
from projectweave.runtime import Runtime


PROJECT = {"owner": "example", "owner_type": "organization", "number": 1}
EXAMPLE = Path(__file__).resolve().parents[1] / "examples/usage-loop.json"


def graph(**config):
    return {"version": 1, "nodes": {
        "usage": {"kind": "action", "action": "usage", "config": config}}, "flow": ["usage"]}


class GraphUsageTests(unittest.TestCase):
    def test_explicit_observers_structured_result_without_execution_or_accounting(self):
        observers = {"claude": Mock(return_value={"session": 72, "week": 48, "fable": 85}),
                     "codex": Mock(return_value={"primary": 63})}
        envelope = {"api": {"unit": "calls", "available": 2, "accounting": "reported"}}
        for providers in (["claude"], ["codex"], ["codex", "claude"]):
            for task in (None, {"id": "I"}):
                with self.subTest(providers=providers, task=task), patch.dict(usage.OBSERVERS, observers):
                    for observer in observers.values():
                        observer.reset_mock()
                    backend, executor = Mock(), Mock()
                    record = Runtime(graph(providers=providers), PROJECT, envelope, backend, executor, task=task).run()
                    self.assertEqual(record["status"], "completed")
                    self.assertEqual(record["last"], result("Provider usage observed", {
                        "status": "ok", "usage": {p: {"windows": observers[p].return_value} for p in providers}}))
                    self.assertEqual(record["resources"], Resources(envelope).state)
                    self.assertEqual(record["executions"], [])
                    executor.assert_not_called()
                    self.assertEqual(backend.mock_calls, [])
                    for name, observer in observers.items():
                        self.assertEqual(observer.call_count, int(name in providers))

    def test_invalid_config_is_rejected_even_in_unreachable_nodes(self):
        invalid = [{}] + [{"providers": value} for value in
                         (None, "codex", [], {}, ["gemini"], ["Codex"], [""], [None], [{}], [1], ["codex", "codex"])]
        invalid += [{"providers": ["codex"], "min_remaining_percent": value} for value in
                    (None, True, "20", -1, 101, float("nan"), float("inf"), {})]
        invalid.append({"providers": ["codex"], "unexpected": True})
        for config in invalid:
            with self.subTest(config=config), patch("projectweave.usage.observe") as observe:
                g = graph(**config)
                g["nodes"]["finish"] = {"kind": "action", "action": "result", "config": result()}
                g["flow"] = ["finish"]
                with self.assertRaises(Failure) as caught:
                    Runtime(g, PROJECT)
                self.assertEqual(caught.exception.kind, "validation")
                observe.assert_not_called()
        for extra in ({"executor": {"type": "command", "argv": ["worker"]}}, {"requires": {"api": 1}}):
            g = graph(providers=["codex"])
            g["nodes"]["usage"].update(extra)
            with self.subTest(extra=extra), self.assertRaisesRegex(Failure, "Nonexecution node"):
                validate(g)

    def test_threshold_checks_every_window_and_includes_boundary(self):
        for minimum, windows, available in (
                (20, {"session": 72, "week": 20, "fable": 85}, True),
                (20, {"session": 72, "week": 48, "fable": 19}, False),
                (20.5, {"session": 72, "week": 20.25}, False),
                (0, {"session": 0, "week": 0}, True),
                (100, {"session": 100, "week": 100}, True)):
            with self.subTest(minimum=minimum, windows=windows), patch.dict(usage.OBSERVERS, {
                    "claude": Mock(return_value=windows), "codex": Mock(return_value={"primary": 100})}):
                record = Runtime(graph(providers=["claude", "codex"], min_remaining_percent=minimum), PROJECT).run()
                self.assertEqual(record["status"], "completed")
                self.assertEqual(record["last"]["data"]["status"], "ok")
                self.assertIs(record["last"]["data"]["available"], available)

    def test_errors_are_structured_without_fabricated_or_previous_windows(self):
        for error in (Failure("usage", "not signed in"), OSError("missing CLI")):
            for threshold in ({}, {"min_remaining_percent": 0}):
                with self.subTest(error=error, threshold=threshold), patch.dict(usage.OBSERVERS, {
                        "claude": Mock(return_value={"session": 72, "week": 48}),
                        "codex": Mock(side_effect=[{"primary": 63}, error])}):
                    g = graph(providers=["claude", "codex"], **threshold)
                    g["flow"] = ["usage", "usage"]
                    record = Runtime(g, PROJECT).run()
                    self.assertEqual(record["status"], "completed")
                    self.assertEqual(record["last"]["data"]["status"], "error")
                    self.assertEqual(record["last"]["data"]["usage"]["codex"], {"error": str(error)})
                    self.assertEqual(record["last"]["data"]["usage"]["claude"]["windows"]["week"], 48)
                    self.assertEqual(record["events"][0]["result"]["data"]["usage"]["codex"]["windows"], {"primary": 63})
                    if threshold:
                        self.assertIs(record["last"]["data"]["available"], False)
                    else:
                        self.assertNotIn("available", record["last"]["data"])

    def test_unexpected_observation_failure_stops_the_node_without_retry(self):
        with patch.dict(usage.OBSERVERS, {"codex": Mock(side_effect=RuntimeError("observer failed"))}):
            g = graph(providers=["codex"])
            g["flow"] = ["usage", "usage"]
            record = Runtime(g, PROJECT).run()
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["failure"]["kind"], "usage")
            self.assertEqual(record["failure"]["node"], "usage")
            self.assertIn("observer failed", record["failure"]["message"])
            usage.OBSERVERS["codex"].assert_called_once()
            self.assertEqual(record["events"], [])

    def test_raw_usage_is_referenceable_in_existing_conditions(self):
        with patch.dict(usage.OBSERVERS, {"codex": Mock(return_value={"primary": 63})}):
            g = graph(providers=["codex"])
            g["nodes"]["finish"] = {"kind": "action", "action": "result", "config": result("Matched")}
            g["flow"].append({"if": {"path": "/results/usage/data/usage/codex/windows/primary",
                                    "equals": 63, "then": ["finish"], "else": []}})
            self.assertEqual(Runtime(g, PROJECT).run()["last"]["message"], "Matched")

    def test_documented_loop_reobserves_before_work_and_stops_at_threshold_or_error(self):
        example = json.loads(EXAMPLE.read_text())
        for primary in ([19], [20, 50, 19], [50, Failure("usage", "not signed in")]):
            with self.subTest(primary=primary):
                order = []
                readings = iter(primary)

                def codex():
                    order.append("observe")
                    value = next(readings)
                    if isinstance(value, Failure):
                        raise value
                    return {"primary": value}

                def work(*args):
                    order.append("work")
                    return result("Work completed", {"available": True})

                with patch.dict(usage.OBSERVERS, {"claude": Mock(return_value={"session": 100, "week": 100}),
                                                "codex": codex}):
                    executor = Mock(side_effect=work)
                    record = Runtime(copy.deepcopy(example), PROJECT, executor=executor, workspace="/project").run()
                self.assertEqual(record["status"], "completed")
                self.assertEqual(order, [step for _ in primary[:-1] for step in ("observe", "work")] + ["observe"])
                self.assertEqual(executor.call_count, len(primary) - 1)
                self.assertEqual(record["last"], record["results"]["usage"])
                self.assertIs(record["last"]["data"]["available"], False)
                self.assertEqual(record["last"]["data"]["status"], "error" if isinstance(primary[-1], Failure) else "ok")
                self.assertEqual(len(record["events"]), len(primary) * 2 - 1)
