"""Shared AI resource admission and the multi-Project observe/admit/launch/re-observe loop."""
import queue
import threading
import time
from .contracts import Failure
from . import usage
from .workspace import (claim, check_project, failure_record, load_root, policy, project_dir, projects, run_task,
                        load_project, weights, POLL_SECONDS)


class Admission:
    """Admit a Task only if every constrained provider window stays at or above the Project's minimum after
    subtracting running reservations and the candidate's estimate. Estimates are safety margins, not accounting."""

    def __init__(self, rules, observe=usage.observe):
        self.rules = rules
        self.observe = observe
        self.observations = {}
        self.reserved = {}

    def refresh(self, names):
        providers = {provider for name in names for provider in self.rules.get(name, {})}
        self.observations = self.observe(providers) if providers else {}
        return self.observations

    def admits(self, name):
        """(admitted, reason); a Project without rules is unconstrained by design."""
        for provider, rule in sorted(self.rules.get(name, {}).items()):
            seen = self.observations.get(provider, {"error": "not observed"})
            if "error" in seen:
                return False, f"{provider}: usage unknown ({seen['error']})"
            for window, remaining in sorted(seen["windows"].items()):
                projected = remaining - self.reserved.get(provider, 0) - rule["estimated_usage_percent_per_task"]
                if projected < rule["min_remaining_percent"]:
                    return False, (f"{provider} {window}: {remaining}% remaining - {self.reserved.get(provider, 0)}% "
                                   f"reserved - {rule['estimated_usage_percent_per_task']}% estimate "
                                   f"< {rule['min_remaining_percent']}% minimum")
        return True, None

    def reserve(self, name):
        reservation = {provider: rule["estimated_usage_percent_per_task"]
                       for provider, rule in self.rules.get(name, {}).items()}
        for provider, amount in reservation.items():
            self.reserved[provider] = self.reserved.get(provider, 0) + amount
        return reservation

    def release(self, reservation):
        for provider, amount in reservation.items():
            self.reserved[provider] -= amount


class RoundRobin:
    """Smooth weighted round-robin over Projects that can take a Task now. Credits persist across passes, so a
    Project that sorts first cannot monopolize newly available capacity; higher weights get proportionally more
    launch opportunities. It only decides who goes next; admission still decides whether a Task may start."""

    def __init__(self, weights):
        self.weights = weights
        self.credit = {}

    def pick(self, candidates):
        total = sum(self.weights[name] for name in candidates)
        for name in candidates:
            self.credit[name] = self.credit.get(name, 0) + self.weights[name]
        chosen = max(candidates, key=lambda name: self.credit[name])  # Ties go to the earlier Project.
        self.credit[chosen] -= total
        return chosen


def run_one(root, name, observe=usage.observe, claim_task=claim, run=run_task):
    """Synchronous single-Task path: admission -> claim -> run-task -> wait."""
    directory = project_dir(root, name)
    check_project(directory)  # Before claiming, so a broken setup never strands a Task In Progress.
    admission = Admission(policy(load_root(root)), observe)
    observations = admission.refresh([name])
    admitted, reason = admission.admits(name)
    if not admitted:
        return {"status": "not_admitted", "project": name, "reason": reason, "observations": observations}
    task = claim_task(directory)
    if task is None:
        return {"status": "no_work", "project": name, "observations": observations}
    try:
        record = run(directory, task)
    except Exception as exc:  # The claimed Task stays In Progress; name it so it is not lost.
        return {"status": "failed", "project": name, "task": task, "failure": failure_record(exc),
                "observations": observations}
    return {"status": record["status"], "project": name, "task": task, "record": record, "observations": observations}


def coordinate(root, once=False, poll_seconds=None, stop=None, observe=usage.observe, claim_task=claim, run=run_task,
               report=None, registry=None):
    """Observe managed Projects and shared resources, claim and launch every admitted Task concurrently, and
    re-observe when a Task ends or every poll interval. With once, make one pass and wait for what it launched."""
    config = load_root(root)
    names = projects(root)
    poll = poll_seconds if poll_seconds is not None else config.get("poll_seconds", POLL_SECONDS)
    if poll <= 0:
        raise Failure("input", "poll_seconds must be positive")
    stop = stop or threading.Event()
    admission = Admission(policy(config), observe)
    order = RoundRobin(weights(config, names))
    finished = queue.Queue()
    running = {}
    runs = []
    if registry:
        for name in names:
            try:
                project = load_project(project_dir(root, name))
            except Exception:
                project = None  # Broken Projects still appear; the normal setup check reports the error.
            registry.add_project(name, project)

    def launch(name, directory, task):
        reservation = admission.reserve(name)
        key = object()
        running[key] = (reservation, task.get("item_id"))
        identity = registry.start(name, task, directory) if registry else None

        def work():
            try:
                if registry and run is run_task:
                    record = run(directory, task, observer=lambda event: registry.event(identity, event))
                else:
                    record = run(directory, task)
            except Exception as exc:  # A crashed Task must still release its reservation.
                record = {"status": "failed", "failure": failure_record(exc)}
            if registry:
                registry.finish(identity, record)
            finished.put((key, {"project": name, "task": task, "record": record}))

        threading.Thread(target=work, name=f"projectweave-{name}", daemon=True).start()

    def settle(key, outcome):
        admission.release(running.pop(key)[0])
        runs.append(outcome)
        if report:
            report(outcome)

    last_problem = {}

    def problem(name, kind, value):
        # Record a Project problem once until it changes, so a long-running loop does not grow without bound.
        entry = {"project": name, kind: value}
        if last_problem.get(name) != entry:
            last_problem[name] = entry
            runs.append(entry)
            if report:
                report(entry)

    def admit_and_launch():
        admission.refresh(names)
        candidates = {}
        for name in names:
            try:
                candidates[name] = project_dir(root, name)
                check_project(candidates[name])  # Never claim for a Project whose graph cannot run.
            except Exception as exc:
                candidates.pop(name, None)
                problem(name, "setup_failure", failure_record(exc))
        # One launch opportunity at a time, in weighted round-robin order, until nobody can take another Task.
        while not stop.is_set():
            admitted = [name for name in candidates if admission.admits(name)[0]]
            if not admitted:
                break
            name = order.pick(admitted)
            directory = candidates[name]
            try:
                task = claim_task(directory)
            except Exception as exc:
                problem(name, "claim_failure", failure_record(exc))
                del candidates[name]
                continue
            last_problem.pop(name, None)
            if task is None:
                del candidates[name]  # No runnable Task left in this Project for now.
                continue
            if task.get("item_id") is not None and any(item == task.get("item_id") for _, item in running.values()):
                # Defensive: never launch a Task that is already running; make it visible.
                problem(name, "duplicate_claim", task.get("item_id"))
                del candidates[name]
                continue
            launch(name, directory, task)

    try:
        while True:
            if not stop.is_set():
                admit_and_launch()
            if once or stop.is_set():
                break
            # Wait for a Task to end or the poll interval, in short slices so a stop request is seen promptly.
            deadline = time.monotonic() + poll
            while not stop.is_set():
                try:
                    settle(*finished.get(timeout=max(0.0, min(1.0, deadline - time.monotonic()))))
                    break
                except queue.Empty:
                    if time.monotonic() >= deadline:
                        break
    finally:
        # Whatever happens (stop, --once, or an unexpected error), wait for the Tasks already launched.
        while running:
            settle(*finished.get())
    return {"observations": admission.observations, "reserved": admission.reserved, "runs": runs}
