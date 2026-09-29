import copy
from contextlib import nullcontext
import json
from pathlib import Path
import unittest
import unittest.mock
from unittest.mock import Mock
from projectweave.contracts import Failure, result, decode, pointer, equal, keys
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
                         checkout=lambda repository, name, failed: nullcontext("/workspace/worktrees/" + name)).run()
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

    def test_directory_inside_another_repository_is_not_reused(self):
        # Real git: an empty directory inside a checkout with the same origin must not borrow it.
        import subprocess, tempfile
        from pathlib import Path
        from projectweave.checkout import resolve
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp) / "outer"
            subprocess.run(["git", "init", "-q", str(outer)], check=True)
            subprocess.run(["git", "-C", str(outer), "remote", "add", "origin", "https://github.com/o/r.git"], check=True)
            nested = outer / "ws" / "repos" / "o" / "r"
            nested.mkdir(parents=True)
            with self.assertRaises(Failure) as caught:
                resolve(outer / "ws", "o/r")
            self.assertEqual(caught.exception.kind, "checkout")
            self.assertIn("not itself a Git checkout", str(caught.exception))
            self.assertEqual(list(nested.iterdir()), [])


class ClaimedTaskTests(unittest.TestCase):
    def test_invalid_resource_envelopes_and_graphs(self):
        for value in ({"type": "subscription", "stop_at_remaining_percent": 20},
                      {"unit": "calls", "available": 1}, {"unit": "", "available": 1, "accounting": "reservation"}):
            with self.subTest(value=value):
                with self.assertRaises(Failure):
                    Resources({"x": value})
        g = graph()
        g["nodes"]["check"] = {"kind": "action", "action": "resources", "config": {"subscriptions": ["x"]}}
        with self.assertRaises(Failure):
            validate(g)  # Shared subscription admission moved to the coordinator.

    def test_claimed_task_is_available_at_task_and_graph_runs_only_what_it_says(self):
        g = {"version": 1, "nodes": {"work": {"kind": "agent", "instruction": "Do it",
             "executor": {"type": "command", "argv": ["fake"]}, "inputs": {"task": "/task"}}}, "flow": ["work"]}
        backend, execute = Mock(), Mock(return_value=result("Done"))
        record = Runtime(g, PROJECT, backend=backend, executor=execute, checkout=lambda r, name, failed: nullcontext("/c"), task=TASK).run()
        self.assertIsNone(record["failure"])
        self.assertEqual(execute.call_args.args[1]["task"], TASK)
        self.assertEqual(backend.mock_calls, [])  # No implicit selection, status transition or completion.

    def test_complete_action_sets_done_only_when_written(self):
        g = {"version": 1, "nodes": {"done": {"kind": "action", "action": "complete", "inputs": {"task": "/task"}}},
             "flow": ["done"]}
        backend = Mock()
        backend.complete.return_value = result("Status set to Done", {"status": "Done"})
        record = Runtime(g, PROJECT, backend=backend, task=TASK).run()
        self.assertIsNone(record["failure"])
        backend.complete.assert_called_once_with(TASK)
        for bad in ({"config": {"status": "Done"}}, {"inputs": {}}):
            with self.subTest(bad=bad):
                with self.assertRaises(Failure):
                    validate({**g, "nodes": {"done": {**g["nodes"]["done"], **bad}}})

    def test_gitweave_runs_in_issue_mode_from_workspace_without_checkout(self):
        g = graph()
        g["nodes"]["work"]["executor"] = {"type": "gitweave", "graph": "g"}
        for task, failure in ((dict(TASK, number=7), None), (TASK, "input"), (dict(TASK, number=7, repository="../x"), "input")):
            with self.subTest(task=task):
                backend = Mock()
                backend.load.return_value = [task]
                backend.select.side_effect = GitHub(PROJECT).select
                backend.writeback.return_value = result("posted")
                execute, checkout = Mock(return_value=result("Done")), Mock()
                record = Runtime(g, PROJECT, envelope(), backend, execute, checkout=checkout, workspace="/ws").run()
                checkout.assert_not_called()  # GitWeave fetches the repository itself.
                if failure:
                    self.assertEqual(record["failure"]["kind"], failure)
                    execute.assert_not_called()
                else:
                    self.assertIsNone(record["failure"])
                    config, request, workspace = execute.call_args.args
                    self.assertEqual((workspace, request["task"]["number"]), ("/ws", 7))
                    self.assertNotIn("checkout", request)
        record = Runtime(g, PROJECT, envelope(), backend, Mock()).run()
        self.assertEqual(record["failure"]["kind"], "checkout")

    def test_status_action_validation_and_dispatch(self):
        node = {"kind": "action", "action": "status", "inputs": {"task": "/results/select/data/task"},
                "config": {"status": "In Progress"}}
        for bad in ({"config": {}}, {"config": {"status": " "}}, {"inputs": {}}, {"config": {"status": "X", "extra": 1}}):
            with self.subTest(bad=bad):
                g = graph()
                g["nodes"]["mark"] = {**node, **bad}
                with self.assertRaises(Failure):
                    validate(g)
        g = graph()
        g["nodes"]["mark"] = node
        g["flow"] = ["load", "select", "mark", "work"]
        backend = Mock()
        backend.load.return_value = [TASK]
        backend.select.side_effect = GitHub(PROJECT).select
        backend.set_status.return_value = result("Status set", {"status": "In Progress"})
        execute = Mock(return_value=result("Done"))
        record = Runtime(g, PROJECT, envelope(), backend, execute, checkout=lambda r, name, failed: nullcontext("/c")).run()
        self.assertIsNone(record["failure"])
        backend.set_status.assert_called_once_with(TASK, "In Progress")
        backend.writeback.assert_not_called()


class FieldValidationTests(unittest.TestCase):
    def message(self, value, allowed, required=()):
        with self.assertRaises(Failure) as caught:
            keys(value, allowed, required)
        return str(caught.exception)

    def test_errors_name_unexpected_and_missing_fields(self):
        subscription = {"type", "stop_at_remaining_percent"}
        message = self.message({"type": "subscription", "stop_at_remaining_percent": 20, "remaining_percent": 82},
                               subscription, subscription)
        self.assertTrue(message.startswith("Unexpected field: remaining_percent"), message)
        self.assertIn("allowed ['stop_at_remaining_percent', 'type']", message)
        self.assertTrue(self.message({"type": "subscription"}, subscription, subscription)
                        .startswith("Missing field: stop_at_remaining_percent"))
        message = self.message({"b": 1, "z": 1, "y": 1}, {"a", "b"}, {"a", "c"})
        self.assertTrue(message.startswith("Missing fields: a, c; Unexpected fields: y, z"), message)
        keys({"a": 1}, {"a", "b"}, {"a"})  # Valid objects are unchanged.
        self.assertEqual(self.message([], {"a"}), "Expected an object")


class CheckoutLockTests(unittest.TestCase):
    def test_concurrent_checkouts_of_one_repository_clone_once(self):
        import tempfile, threading, time
        from projectweave.checkout import resolve
        clones = []

        def process(argv, stdin, timeout):
            if argv[:3] == ["gh", "repo", "clone"]:
                clones.append(argv)
                time.sleep(0.1)  # A racing second clone would see a half-made directory.
                Path(argv[4]).mkdir()
                return ""
            if argv[3:] == ["rev-parse", "--show-toplevel"]:
                return argv[2] + "\n"
            if argv[3:] == ["remote", "get-url", "origin"]:
                return "https://github.com/o/r.git\n"
            return ""

        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch("projectweave.checkout.process", process):
            paths, errors = [], []

            def work():
                try:
                    paths.append(resolve(tmp, "o/r"))
                except Failure as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=work) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(clones), 1)
        self.assertEqual(len(set(paths)), 1)


class WorktreeTests(unittest.TestCase):
    def test_real_git_worktrees_are_isolated_and_removed(self):
        import subprocess, tempfile
        from projectweave import checkout
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "repos" / "o" / "r"
            git = lambda *args, cwd=shared: subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)
            shared.mkdir(parents=True)
            git("init", "-q")
            (shared / "file.txt").write_text("base\n")
            git("add", "file.txt")
            git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
            git("update-ref", "refs/remotes/origin/main", "HEAD")
            git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
            failures = []
            # Clone/fetch are covered elsewhere; here only the worktree lifecycle runs against real git.
            with unittest.mock.patch.object(checkout, "prepare", lambda path, repository: str(path)):
                with checkout.worktree(tmp, "o/r", "run-a", failures.append) as first, \
                        checkout.worktree(tmp, "o/r", "run-b", failures.append) as second:
                    self.assertNotEqual(first, second)
                    Path(first, "file.txt").write_text("changed by a\n")  # Concurrent edits stay isolated.
                    self.assertEqual(Path(second, "file.txt").read_text(), "base\n")
                    self.assertEqual((shared / "file.txt").read_text(), "base\n")
                self.assertFalse(Path(first).exists() or Path(second).exists())
                listed = subprocess.run(["git", "-C", str(shared), "worktree", "list"], capture_output=True, text=True).stdout
                self.assertEqual(len(listed.strip().splitlines()), 1)  # Only the shared checkout remains.
                with self.assertRaises(RuntimeError):
                    with checkout.worktree(tmp, "o/r", "run-c", failures.append) as third:
                        raise RuntimeError("executor failed")  # Removed even when the executor fails.
                self.assertFalse(Path(third).exists())
            self.assertEqual(failures, [])
