import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from projectweave.contracts import Failure
from projectweave.coordinator import Admission, coordinate, run_one
from projectweave.workspace import claim, load_root

CODEX = {"min_remaining_percent": 20, "estimated_usage_percent_per_task": 10}


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
        self.queues[name] = list(tasks)
        if rules:
            self.rules[name] = {"resources": rules}
        (self.root / "projectweave.json").write_text(json.dumps({"projects": self.rules}))

    def claim(self, directory):
        pending = self.queues[Path(directory).name]
        return pending.pop(0) if pending else None

    def test_once_launches_every_admitted_task_concurrently_and_waits(self):
        self.project("a", ["a1", "a2"])
        self.project("b", ["b1", "b2", "b3"], {"codex": CODEX})
        started, release = [], threading.Event()

        def run(directory, task):
            started.append(task)
            self.assertTrue(release.wait(5))
            return {"status": "completed", "failure": None, "task": task}

        observations = []
        observe = lambda providers: observations.append(sorted(providers)) or {"codex": {"windows": {"primary": 45}}}
        threading.Timer(0.3, release.set).start()
        summary = coordinate(self.root, once=True, observe=observe, claim_task=self.claim, run=run)
        # a is unconstrained; b fits two Tasks: 45 - 0 - 10 >= 20, 45 - 10 - 10 >= 20, 45 - 20 - 10 < 20.
        self.assertEqual(sorted(started), ["a1", "a2", "b1", "b2"])
        self.assertEqual(sorted(run["task"] for run in summary["runs"]), ["a1", "a2", "b1", "b2"])
        self.assertEqual(self.queues["b"], ["b3"])  # Not claimed: claims happen only after admission.
        self.assertEqual(observations, [["codex"]])
        self.assertEqual(summary["reserved"], {"codex": 0})

    def test_loop_releases_reobserves_and_admits_more_until_stopped(self):
        self.project("b", ["b1", "b2", "b3"], {"codex": CODEX})
        stop, finished = threading.Event(), []
        observe_calls = []

        def observe(providers):
            observe_calls.append(time.monotonic())
            return {"codex": {"windows": {"primary": 35}}}  # Room for exactly one Task at a time.

        def run(directory, task):
            time.sleep(0.05)
            return {"status": "completed", "failure": None}

        def report(outcome):
            finished.append(outcome["task"])
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

    def test_crashed_or_failed_tasks_release_and_claim_failures_are_recorded(self):
        self.project("a", ["a1"], {"codex": CODEX})
        self.project("b", [])

        def run(directory, task):
            raise RuntimeError("boom")

        def claim_task(directory):
            if Path(directory).name == "b":
                raise Failure("github", "denied")
            return self.claim(directory)

        summary = coordinate(self.root, once=True, observe=lambda p: {"codex": {"windows": {"primary": 90}}},
                             claim_task=claim_task, run=run)
        outcomes = {run.get("task") or "claim": run for run in summary["runs"]}
        self.assertEqual(outcomes["a1"]["record"]["failure"]["message"], "boom")
        self.assertEqual(outcomes["claim"]["claim_failure"]["kind"], "github")
        self.assertEqual(summary["reserved"], {"codex": 0})

    def test_run_one_admission_no_work_and_config_errors(self):
        self.project("a", ["a1"], {"codex": CODEX})
        low = lambda p: {"codex": {"windows": {"primary": 25}}}
        outcome = run_one(self.root, "a", observe=low, claim_task=self.claim, run=lambda d, t: self.fail("not admitted"))
        self.assertEqual(outcome["status"], "not_admitted")
        self.assertEqual(self.queues["a"], ["a1"])
        ok = lambda p: {"codex": {"windows": {"primary": 90}}}
        outcome = run_one(self.root, "a", observe=ok, claim_task=self.claim,
                          run=lambda d, t: {"status": "completed", "failure": None})
        self.assertEqual((outcome["status"], outcome["task"]), ("completed", "a1"))
        self.assertEqual(run_one(self.root, "a", observe=ok, claim_task=self.claim)["status"], "no_work")
        with self.assertRaises(Failure):
            run_one(self.root, "missing")
        (self.root / "projectweave.json").write_text(json.dumps({"projects": {"ghost": {}}}))
        with self.assertRaises(Failure):
            load_root(self.root)


class ClaimLockTests(unittest.TestCase):
    def test_concurrent_claims_never_select_the_same_task(self):
        class Backend:
            def __init__(self):
                self.status = {"t1": "Todo", "t2": "Todo"}

            def load(self):
                snapshot = dict(self.status)
                time.sleep(0.05)  # Widen the race window between selection and marking.
                return snapshot

            def select(self, items):
                todo = sorted(name for name, status in items.items() if status == "Todo")
                return todo[0] if todo else None

            def set_status(self, task, status):
                self.status[task] = status

        with tempfile.TemporaryDirectory() as tmp:
            backend, claimed = Backend(), []
            threads = [threading.Thread(target=lambda: claimed.append(claim(tmp, backend))) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sorted(claimed, key=str), [None, "t1", "t2"])
        self.assertEqual(backend.status, {"t1": "In Progress", "t2": "In Progress"})
