import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from projectweave.contracts import Failure
from projectweave.coordinator import Admission, coordinate, run_one
from projectweave.github import GitHub
from projectweave.workspace import claim, load_root

ROOT = Path(__file__).resolve().parents[1]
CODEX = {"min_remaining_percent": 20, "estimated_usage_percent_per_task": 10}


def task(name):
    return {"item_id": name, "number": 1, "repository": "o/r"}


def ids(values):
    return sorted(value["item_id"] for value in values)


class AdmissionTests(unittest.TestCase):
    def test_unconstrained_projects_are_always_admitted(self):
        admission = Admission({}, observe=lambda providers: self.fail("nothing to observe"))
        admission.refresh(["a"])
        self.assertEqual(admission.admits("a"), (True, None))

    def test_every_window_of_every_listed_provider_must_stay_above_minimum(self):
        seen = {"claude": {"windows": {"session": 90, "week": 45, "fable": 80}}, "codex": {"windows": {"primary": 60}}}
        rules = {"a": {"claude": {"min_remaining_percent": 30, "estimated_usage_percent_per_task": 10}, "codex": CODEX}}
        admission = Admission(rules, observe=lambda providers: {p: seen[p] for p in providers})
        self.assertEqual(admission.refresh(["a"]), seen)
        self.assertEqual(admission.admits("a"), (True, None))  # week: 45 - 0 - 10 >= 30.
        admission.reserve("a")
        admitted, reason = admission.admits("a")  # week: 45 - 10 - 10 < 30; the estimate applies to every window.
        self.assertFalse(admitted)
        self.assertIn("claude week", reason)

    def test_unknown_usage_blocks_and_reservations_are_shared_then_released(self):
        rules = {"a": {"codex": CODEX}, "b": {"codex": dict(CODEX, estimated_usage_percent_per_task=15)}}
        observed = {"codex": {"windows": {"primary": 50}}}
        admission = Admission(rules, observe=lambda providers: observed)
        admission.refresh(["a", "b"])
        first = admission.reserve("a")
        second = admission.reserve("a")
        self.assertEqual(admission.reserved, {"codex": 20})
        self.assertFalse(admission.admits("b")[0])  # 50 - 20 reserved by a - 15 < 20: shared across Projects.
        admission.release(first)
        self.assertTrue(admission.admits("b")[0])
        admission.release(second)
        self.assertEqual(admission.reserved, {"codex": 0})
        observed["codex"] = {"error": "timed out"}
        admission.refresh(["a"])
        self.assertIn("usage unknown", admission.admits("a")[1])


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rules = {}
        self.queues = {}

    def project(self, name, tasks, rules=None):
        directory = self.root / "projects" / name
        directory.mkdir(parents=True)
        (directory / "project.json").write_text(json.dumps({"owner": "o", "owner_type": "user", "number": 1}))
        (directory / "graph.json").write_text((ROOT / "projectweave/templates/graph.json").read_text())
        self.queues[name] = [task(value) for value in tasks]
        if rules:
            self.rules[name] = {"resources": rules}
        (self.root / "projectweave.json").write_text(json.dumps({"projects": self.rules}))
        return directory

    def claim(self, directory):
        pending = self.queues[Path(directory).name]
        return pending.pop(0) if pending else None

    def test_once_launches_every_admitted_task_concurrently_and_waits(self):
        self.project("a", ["a1", "a2"])
        self.project("b", ["b1", "b2", "b3"], {"codex": CODEX})
        started, release = [], threading.Event()

        def run(directory, claimed):
            started.append(claimed)
            self.assertTrue(release.wait(5))
            return {"status": "completed", "failure": None}

        observations = []
        observe = lambda providers: observations.append(sorted(providers)) or {"codex": {"windows": {"primary": 45}}}
        threading.Timer(0.3, release.set).start()
        summary = coordinate(self.root, once=True, observe=observe, claim_task=self.claim, run=run)
        # a is unconstrained; b fits two Tasks: 45 - 0 - 10 >= 20, 45 - 10 - 10 >= 20, 45 - 20 - 10 < 20.
        self.assertEqual(ids(started), ["a1", "a2", "b1", "b2"])
        self.assertEqual(ids(run["task"] for run in summary["runs"]), ["a1", "a2", "b1", "b2"])
        self.assertEqual(ids(self.queues["b"]), ["b3"])  # Not claimed: claims happen only after admission.
        self.assertEqual(observations, [["codex"]])
        self.assertEqual(summary["reserved"], {"codex": 0})

    def test_loop_releases_reobserves_and_admits_more_until_stopped(self):
        self.project("b", ["b1", "b2", "b3"], {"codex": CODEX})
        stop, finished = threading.Event(), []
        observe_calls = []

        def observe(providers):
            observe_calls.append(time.monotonic())
            return {"codex": {"windows": {"primary": 35}}}  # Room for exactly one Task at a time.

        def run(directory, claimed):
            time.sleep(0.05)
            return {"status": "completed", "failure": None}

        def report(outcome):
            finished.append(outcome["task"]["item_id"])
            if len(finished) == 3:
                stop.set()

        thread = threading.Thread(target=lambda: self.__dict__.setdefault("summary", coordinate(
            self.root, poll_seconds=60, stop=stop, observe=observe, claim_task=self.claim, run=run, report=report)))
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(finished, ["b1", "b2", "b3"])  # Serialized by the reservation, not by a slot count.
        self.assertGreaterEqual(len(observe_calls), 3)  # Re-observed after each Task ended.
        self.assertEqual(self.summary["reserved"], {"codex": 0})

    def test_crashed_or_failed_tasks_release_and_claim_or_setup_failures_are_recorded(self):
        self.project("a", ["a1"], {"codex": CODEX})
        self.project("b", [])
        broken = self.project("c", ["c1"])
        (broken / "graph.json").write_text("{}")

        def run(directory, claimed):
            raise RuntimeError("boom")

        def claim_task(directory):
            if Path(directory).name == "b":
                raise KeyError("unexpected")  # Any claim error is recorded, never aborting the loop.
            return self.claim(directory)

        summary = coordinate(self.root, once=True, observe=lambda p: {"codex": {"windows": {"primary": 90}}},
                             claim_task=claim_task, run=run)
        outcomes = {run["project"]: run for run in summary["runs"]}
        self.assertEqual(outcomes["a"]["record"]["failure"]["message"], "boom")
        self.assertIn("claim_failure", outcomes["b"])
        self.assertIn("setup_failure", outcomes["c"])  # A broken graph is caught before any claim.
        self.assertEqual(ids(self.queues["c"]), ["c1"])
        self.assertEqual(summary["reserved"], {"codex": 0})

    def test_unexpected_error_still_waits_for_running_tasks(self):
        self.project("a", ["a1"], {"codex": CODEX})
        finished, calls = [], []

        def observe(providers):
            calls.append(1)
            if len(calls) > 1:  # The second pass (after the poll interval) fails unexpectedly...
                raise RuntimeError("observer crashed")
            return {"codex": {"windows": {"primary": 90}}}

        def run(directory, claimed):
            time.sleep(1.5)  # ...while a1 is still running.
            return {"status": "completed", "failure": None}

        with self.assertRaisesRegex(RuntimeError, "observer crashed"):
            coordinate(self.root, poll_seconds=1, observe=observe, claim_task=self.claim, run=run,
                       report=lambda outcome: finished.append(outcome["task"]["item_id"]))
        self.assertEqual(finished, ["a1"])  # Waited for (and reported) the running Task before re-raising.

    def test_a_running_task_is_never_launched_twice(self):
        self.project("a", [])
        repeated = task("same")
        launched, release = [], threading.Event()
        summary = coordinate(self.root, once=True, observe=lambda p: {}, claim_task=lambda d: repeated,
                             run=lambda d, t: launched.append(t) or release.wait(5) or {"status": "completed", "failure": None},
                             report=lambda outcome: release.set())
        self.assertEqual(len(launched), 1)
        self.assertEqual(len(summary["runs"]), 1)

    def test_invalid_poll_seconds_rejected(self):
        self.project("a", [])
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(Failure):
                    coordinate(self.root, poll_seconds=value, observe=lambda p: {}, claim_task=self.claim)

    def test_run_one_admission_no_work_and_config_errors(self):
        self.project("a", ["a1"], {"codex": CODEX})
        low = lambda p: {"codex": {"windows": {"primary": 25}}}
        outcome = run_one(self.root, "a", observe=low, claim_task=self.claim, run=lambda d, t: self.fail("not admitted"))
        self.assertEqual(outcome["status"], "not_admitted")
        self.assertEqual(ids(self.queues["a"]), ["a1"])
        ok = lambda p: {"codex": {"windows": {"primary": 90}}}
        outcome = run_one(self.root, "a", observe=ok, claim_task=self.claim,
                          run=lambda d, t: {"status": "completed", "failure": None})
        self.assertEqual((outcome["status"], outcome["task"]["item_id"]), ("completed", "a1"))
        self.assertEqual(run_one(self.root, "a", observe=ok, claim_task=self.claim)["status"], "no_work")
        with self.assertRaises(Failure):
            run_one(self.root, "missing")
        (self.root / "projectweave.json").write_text(json.dumps({"projects": {"ghost": {}}}))
        with self.assertRaises(Failure):
            load_root(self.root)

    def test_run_one_checks_the_graph_before_claiming_and_names_a_failed_task(self):
        directory = self.project("a", ["a1", "a2"])
        (directory / "graph.json").write_text("{}")
        with self.assertRaises(Failure):
            run_one(self.root, "a", observe=lambda p: {}, claim_task=self.claim)
        self.assertEqual(ids(self.queues["a"]), ["a1", "a2"])  # Nothing was claimed.
        (directory / "graph.json").write_text((ROOT / "projectweave/templates/graph.json").read_text())

        def run(directory, claimed):
            raise RuntimeError("boom")

        outcome = run_one(self.root, "a", observe=lambda p: {}, claim_task=self.claim, run=run)
        self.assertEqual((outcome["status"], outcome["task"]["item_id"]), ("failed", "a1"))
        self.assertEqual(outcome["failure"]["message"], "boom")


class ClaimTests(unittest.TestCase):
    class Backend:
        """An in-memory Project backed by the real selection rules."""

        def __init__(self, config, statuses):
            self.github = GitHub(config)
            self.status = dict(statuses)

        def load(self):
            snapshot = [{"id": name, "item_id": name, "state": "OPEN", "ai_execution": "Ready", "status": status,
                         "priority": "P1", "created_at": name, "url": name} for name, status in self.status.items()]
            time.sleep(0.05)  # Widen the race window between selection and marking.
            return snapshot

        def select(self, items):
            return self.github.select(items)

        def set_status(self, claimed, status):
            self.status[claimed["item_id"]] = status

    def test_concurrent_claims_never_select_the_same_task(self):
        backend = self.Backend({"owner": "o", "owner_type": "user", "number": 1, "eligible_statuses": ["Todo"]},
                               {"t1": "Todo", "t2": "Todo"})
        claimed = []
        with tempfile.TemporaryDirectory() as tmp:
            threads = [threading.Thread(target=lambda: claimed.append(claim(tmp, backend))) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sorted(str(c and c["item_id"]) for c in claimed), ["None", "t1", "t2"])
        self.assertEqual(backend.status, {"t1": "In Progress", "t2": "In Progress"})
        self.assertTrue(all(c is None or c["status"] == "In Progress" for c in claimed))

    def test_only_todo_tasks_are_claimed_even_without_eligible_statuses(self):
        # eligible_statuses is optional in project.json; claim must still never reselect In Progress or Done.
        backend = self.Backend({"owner": "o", "owner_type": "user", "number": 1},
                               {"t0": "Done", "t1": "In Progress", "t2": "Todo", "t3": None})
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(claim(tmp, backend)["item_id"], "t2")
            self.assertIsNone(claim(tmp, backend))
