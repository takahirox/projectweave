"""Bounded single-run graph interpreter."""
from copy import deepcopy
import re
import uuid
from .contracts import Failure, require, pointer, equal, result, check_result
from .graph import validate
from .resources import Resources
from .github import GitHub
from .checkout import valid_repository
from .executors import invoke
from .graph_routes import select_executor
from . import usage


class Runtime:
    def __init__(self, graph, project, envelope=None, backend=None, executor=invoke, checkout=None, workspace=None,
                 task=None, observer=None, operator_input=None):
        self.graph = validate(graph)
        self.resources = Resources(envelope or {})
        self.backend = backend or GitHub(project)
        self.executor = executor
        # (repository, name, cleanup_failed) -> context manager yielding an isolated Task worktree (checkout.worktree).
        self.checkout = checkout
        self.cleanup_failures = []
        # GitWeave Issue mode and Taskless commands run from the Project workspace.
        self.workspace = workspace
        # run-task supplies the already-claimed Task at /task; the graph never claims or selects it itself.
        self.context = {"run_id": uuid.uuid4().hex, "project": project, "task": task,
                        "resources": self.resources.state, "results": {}, "last": None,
                        "input": operator_input}
        self.steps = 0
        self.active = None
        self.events = []
        self.executions = []
        self.observer = observer

    def notify(self, event):
        if self.observer:
            self.observer(event)

    def node(self, node, inputs):
        action = node.get("action")
        if node["kind"] == "agent" or action == "execute":
            task = inputs.get("task")
            gitweave = node["executor"]["type"] == "gitweave"
            if "task" in inputs or gitweave:
                require(isinstance(task, dict), "Execution needs a task object", "input")
            allocation = node.get("requires", {})
            if gitweave:
                require(all(self.resources.state.get(k, {}).get("accounting", "reservation") == "reservation"
                            for k in allocation), "GitWeave requires reservation accounting", "accounting")
            if not self.resources.reserve(allocation):
                return result("Required resources unavailable; executor was not launched",
                              {"status": "resource_exhausted", "required": allocation})
            request = {"task": task, "context": inputs.get("context", {}),
                       "resources": deepcopy(self.resources.state), "allocation": allocation,
                       "instruction": node.get("instruction"), "run_id": self.context["run_id"]}
            if gitweave:
                require(self.workspace is not None, "Execution needs a Project workspace", "checkout")
                require(valid_repository(task.get("repository")) and type(task.get("number")) is int
                        and task["number"] > 0, "GitWeave execution needs the Task repository and Issue number", "input")
            elif task is not None:
                require(self.checkout is not None, "Execution needs a Project workspace", "checkout")
            else:
                require(self.workspace is not None, "Execution needs a Project workspace", "checkout")
                request.update(project=deepcopy(self.context["project"]), input=deepcopy(self.context["input"]))
            output = None
            config = select_executor(node["executor"], self.context["project"], task, self.workspace)
            execution = {"execution_id": f"{self.active}-{self.steps}", "config": deepcopy(config)}
            self.executions.append(execution)
            self.notify({"type": "executor_started", **deepcopy(execution)})
            try:
                if gitweave or task is None:
                    output = check_result(self.executor(config, deepcopy(request), self.workspace))
                else:
                    # One isolated worktree per invocation; it is removed when the executor exits.
                    node_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(self.active)).strip(".") or "node"
                    name = f"{self.context['run_id']}-{node_id}-{self.steps}"
                    with self.checkout(task.get("repository"), name, self.cleanup_failures.append) as path:
                        request["checkout"] = path
                        output = check_result(self.executor(config, deepcopy(request)))
                self.resources.settle(allocation, output["usage"])
            except Failure as exc:
                if output is not None:
                    exc.details["result"] = output
                raise
            finally:
                self.notify({"type": "executor_finished"})
            return output
        if action == "load":
            return result("Project loaded", {"items": self.backend.load()})
        if action == "select":
            task = self.backend.select(inputs["items"])
            return result("Task selected" if task else "No eligible tasks", {"task": task})
        if action == "resources":
            available = self.resources.admits(node.get("config", {}).get("requires", {}))
            return result("Resource state", {"resources": deepcopy(self.resources.state), "available": available})
        if action == "usage":
            config = node["config"]
            try:
                observations = usage.observe(config["providers"])
            except Exception as exc:
                raise Failure("usage", f"Usage observation failed: {exc}") from exc
            ok = all("error" not in value for value in observations.values())
            data = {"status": "ok" if ok else "error", "usage": observations}
            if "min_remaining_percent" in config:
                data["available"] = ok and all(
                    remaining >= config["min_remaining_percent"]
                    for value in observations.values() for remaining in value["windows"].values())
            return result("Provider usage observed" if ok else "Provider usage observation failed", data)
        if action == "status":
            return self.backend.set_status(inputs["task"], node["config"]["status"])
        if action == "complete":
            return self.backend.complete(inputs["task"])
        if action == "writeback":
            return self.backend.writeback(inputs["task"], inputs["result"], self.context["run_id"],
                                          node.get("config", {}).get("status"))
        return deepcopy(node["config"])

    def activate(self, name):
        self.active = name
        self.steps += 1
        require(self.steps <= self.graph.get("max_steps", 100), "Run step limit exceeded", "limit")

    def flow(self, entries):
        for entry in entries:
            self.activate(entry if isinstance(entry, str) else next(iter(entry)))
            if isinstance(entry, dict):
                if "if" in entry:
                    spec = entry["if"]
                    branch = "then" if equal(pointer(self.context, spec["path"]), spec["equals"]) else "else"
                    self.flow(spec[branch])
                else:
                    spec = entry["loop"]
                    while True:
                        self.activate("loop")
                        self.flow(spec["flow"])
                        self.active = "loop"
                        condition = spec["while"]
                        if not equal(pointer(self.context, condition["path"]), condition["equals"]):
                            break
                continue
            node = self.graph["nodes"][entry]
            inputs = {key: deepcopy(pointer(self.context, path)) for key, path in node.get("inputs", {}).items()}
            output = check_result(self.node(node, inputs))
            self.context["results"][entry] = output
            self.context["last"] = output
            self.events.append({"node": entry, "result": output})

    def run(self):
        failure = None
        self.notify({"type": "run_started", "run_id": self.context["run_id"]})
        try:
            self.flow(self.graph["flow"])
        except Failure as exc:
            failure = {**exc.record(), "node": self.active}
        return {"run_id": self.context["run_id"], "status": "failed" if failure else "completed",
                "failure": failure, "steps": self.steps, "results": self.context["results"],
                "events": self.events, "executions": self.executions, "last": self.context["last"], "resources": self.resources.state,
                "cleanup_failures": self.cleanup_failures}
