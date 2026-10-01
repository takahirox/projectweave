"""Thread-safe, process-local execution state for the optional dashboard."""
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
import uuid


def graph_view(graph):
    """Collapse structured flow into node edges; branches/loops remain visible."""
    nodes = {name: {"id": name, "kind": spec.get("kind"), "status": "not_executed"}
             for name, spec in graph["nodes"].items()}
    edges = set()

    def flow(entries, previous):
        for entry in entries:
            if isinstance(entry, str):
                edges.update((parent, entry) for parent in previous)
                previous = {entry}
            elif "parallel" in entry:
                previous = set().union(*(flow(branch, previous) for branch in entry["parallel"]))
            elif "if" in entry:
                spec = entry["if"]
                previous = flow(spec["then"], previous) | flow(spec["else"], previous)
            else:
                spec = entry.get("loop", entry.get("map"))
                previous = flow(spec["flow"], previous)
                if "loop" in entry:
                    flow(spec["flow"], previous)
        return previous

    flow(graph["flow"], set())
    return {"nodes": list(nodes.values()), "edges": [list(edge) for edge in sorted(edges)]}


class ExecutionRegistry:
    def __init__(self, long_running_seconds=3600, log_lines=500, log_chars=2000, clock=time.monotonic):
        if long_running_seconds <= 0 or log_lines <= 0 or log_chars <= 0:
            raise ValueError("Execution registry limits must be positive")
        self.threshold = long_running_seconds
        self.log_lines, self.log_chars = log_lines, log_chars
        self.clock = clock
        self.lock = threading.RLock()
        self.projects = {}
        self.runs = {}

    def add_project(self, name, project=None):
        project = project or {}
        owner = project.get("owner")
        url = None
        if owner and project.get("number"):
            prefix = "orgs" if project.get("owner_type") == "organization" else "users"
            url = f"https://github.com/{prefix}/{owner}/projects/{project['number']}"
        with self.lock:
            self.projects[name] = {"name": name, "url": url}

    def start(self, project, task, directory):
        identity = uuid.uuid4().hex
        with self.lock:
            self.runs[identity] = {
                "id": identity, "project": project,
                "task": {key: deepcopy(task.get(key)) for key in ("number", "title", "repository", "url")},
                "status": "running", "started_at": datetime.now(timezone.utc).isoformat(), "ended_at": None,
                "run_id": None, "failure": None, "executor_type": None, "executions": [],
                "logs": deque(maxlen=self.log_lines), "_started": self.clock(), "_ended": None,
                "_directory": str(directory),
            }
        return identity

    def event(self, identity, event):
        """Consume Runtime notifications and executor events; never infer an active GitWeave node."""
        if not isinstance(event, dict):
            return
        with self.lock:
            run = self.runs[identity]
            kind = event.get("type") or event.get("event")
            if not isinstance(kind, str):
                return
            if kind == "run_started":
                run["run_id"] = event.get("run_id")
            elif kind == "executor_started":
                config = event["config"]
                execution = {"id": event["execution_id"], "executor_type": config["type"], "run_id": None,
                             "current_nodes": [], "recent_node": None, "graph": None, "graph_error": None}
                if config["type"] == "gitweave":
                    try:
                        path = Path(config["graph"])
                        if not path.is_absolute():
                            path = Path(run["_directory"]) / path
                        execution["graph"] = graph_view(json.loads(path.read_text()))
                    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
                        execution["graph_error"] = str(exc)
                run["executions"].append(execution)
                run["executor_type"] = config["type"]
            else:
                execution = run["executions"][-1] if run["executions"] else None
                if execution and kind == "executor_finished":
                    execution["current_nodes"] = []
                    execution.pop("active_instances", None)
                    for node in (execution.get("graph") or {}).get("nodes", []):
                        if node["status"] == "active":
                            node["status"] = "unknown"
                if execution and execution["executor_type"] == "gitweave":
                    if isinstance(event.get("run_id"), str):
                        execution["run_id"] = event["run_id"]
                    node_id = event.get("node_id")
                    state = {"node_started": "active", "node_completed": "completed", "node_failed": "failed"}.get(kind)
                    if isinstance(node_id, str) and state and execution["graph"]:
                        for node in execution["graph"]["nodes"]:
                            if node["id"] == node_id:
                                node["status"] = state
                                execution["recent_node"] = node_id
                        # Use instance IDs for concurrent fan-out/retries of the same graph node.
                        active = execution.setdefault("active_instances", {})
                        instance = event.get("instance_id")
                        if not isinstance(instance, str):
                            instance = node_id
                        if state == "active":
                            active[instance] = node_id
                        else:
                            active.pop(instance, None)
                        execution["current_nodes"] = sorted(set(active.values()))
                        for node in execution["graph"]["nodes"]:
                            if node["id"] in execution["current_nodes"]:
                                node["status"] = "active"
                if kind in ("stdout", "stderr", "agent_output", "command_stdout", "command_stderr",
                            "node_started", "node_completed", "node_failed", "log"):
                    output = event.get("text", event.get("message", event.get("output", "")))
                    if not isinstance(output, str):
                        output = json.dumps(output, ensure_ascii=False)
                    if not output:
                        output = str(event.get("node_id", ""))
                    attribution = {key: event[key] for key in ("node_id", "instance_id")
                                   if isinstance(event.get(key), str)}
                    if execution:
                        attribution["execution_id"] = execution["id"]
                    for line in output.splitlines() or [""]:
                        run["logs"].append({"type": kind, "text": line[:self.log_chars],
                                            "at": datetime.now(timezone.utc).isoformat(), **attribution})

    def finish(self, identity, record):
        with self.lock:
            run = self.runs[identity]
            run["status"] = "failed" if record.get("failure") or record.get("status") == "failed" else "completed"
            run["failure"] = deepcopy(record.get("failure"))
            run["run_id"] = record.get("run_id") or run["run_id"]
            run["_ended"] = self.clock()
            run["ended_at"] = datetime.now(timezone.utc).isoformat()
            for execution in run["executions"]:
                execution["current_nodes"] = []
                execution.pop("active_instances", None)
                for node in (execution.get("graph") or {}).get("nodes", []):
                    if node["status"] == "active":
                        node["status"] = "unknown"

    def snapshot(self):
        with self.lock:
            now = self.clock()
            runs = []
            for run in self.runs.values():
                entry = deepcopy({key: value for key, value in run.items() if not key.startswith("_") and key != "logs"})
                entry["logs"] = deepcopy(list(run["logs"]))
                entry["elapsed_seconds"] = max(0, (run["_ended"] if run["_ended"] is not None else now) - run["_started"])
                entry["long_running"] = entry["status"] == "running" and entry["elapsed_seconds"] > self.threshold
                runs.append(entry)
            projects = []
            for project in self.projects.values():
                members = [run for run in runs if run["project"] == project["name"]]
                projects.append(dict(project, running=sum(run["status"] == "running" for run in members),
                                     long_running=sum(run["long_running"] for run in members),
                                     failed=sum(run["status"] == "failed" for run in members)))
            return {"projects": projects, "runs": runs, "long_running_seconds": self.threshold}
