"""Shared AI resource admission and the multi-Project observe/admit/launch/re-observe loop."""
import queue
import threading
import time
from .contracts import Failure
from . import usage
from .workspace import claim, failure_record, load_root, policy, project_dir, projects, run_task, POLL_SECONDS


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


def run_one(root, name, observe=usage.observe, claim_task=claim, run=run_task):
    """Synchronous single-Task path: admission -> claim -> run-task -> wait."""
    directory = project_dir(root, name)
    admission = Admission(policy(load_root(root)), observe)
    observations = admission.refresh([name])
    admitted, reason = admission.admits(name)
    if not admitted:
        return {"status": "not_admitted", "project": name, "reason": reason, "observations": observations}
    task = claim_task(directory)
    if task is None:
        return {"status": "no_work", "project": name, "observations": observations}
    record = run(directory, task)
    return {"status": record["status"], "project": name, "task": task, "record": record, "observations": observations}


def coordinate(root, once=False, poll_seconds=None, stop=None, observe=usage.observe, claim_task=claim, run=run_task,
               report=None):
    """Observe managed Projects and shared resources, claim and launch every admitted Task concurrently, and
    re-observe when a Task ends or every poll interval. With once, make one pass and wait for what it launched."""
    config = load_root(root)
    names = projects(root)
    poll = poll_seconds or config.get("poll_seconds", POLL_SECONDS)
    stop = stop or threading.Event()
    admission = Admission(policy(config), observe)
    finished = queue.Queue()
    running = {}
    runs = []

    def launch(name, directory, task):
        reservation = admission.reserve(name)
        key = object()
        running[key] = reservation

        def work():
            try:
                record = run(directory, task)
            except Exception as exc:  # A crashed Task must still release its reservation.
                record = {"status": "failed", "failure": failure_record(exc)}
            finished.put((key, {"project": name, "task": task, "record": record}))

        threading.Thread(target=work, name=f"projectweave-{name}", daemon=True).start()

    def settle(key, outcome):
        admission.release(running.pop(key))
        runs.append(outcome)
        if report:
            report(outcome)

    while True:
        if not stop.is_set():
            admission.refresh(names)
            for name in names:
                directory = project_dir(root, name)
                while not stop.is_set() and admission.admits(name)[0]:
                    try:
                        task = claim_task(directory)
                    except (Failure, OSError) as exc:
                        runs.append({"project": name, "claim_failure": failure_record(exc)})
                        break
                    if task is None:
                        break
                    launch(name, directory, task)
        if once or stop.is_set():
            while running:
                settle(*finished.get())
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
    return {"observations": admission.observations, "reserved": admission.reserved, "runs": runs}
