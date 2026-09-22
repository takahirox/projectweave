"""Bounded single-run graph interpreter."""
from copy import deepcopy
import uuid
from .contracts import Failure, require, pointer, equal, result, check_result
from .graph import validate
from .resources import Resources
from .github import GitHub
from .executors import invoke


class Runtime:
    def __init__(self, graph, project, envelope, backend=None, executor=invoke):
        self.graph = validate(graph)
        self.resources = Resources(envelope)
        self.backend = backend or GitHub(project)
        self.executor = executor
        self.context = {"run_id": uuid.uuid4().hex, "project": project,
                        "resources": self.resources.state, "results": {}, "last": None}
        self.steps = 0
        self.active = None
        self.events = []

    def node(self, node, inputs):
        action = node.get("action")
        if node["kind"] == "agent" or action == "execute":
            require(isinstance(inputs["task"], dict), "Execution needs a task object", "input")
            allocation = node["requires"]
            if node["executor"]["type"] == "gitweave":
                require(all(self.resources.state.get(k, {}).get("accounting", "reservation") == "reservation"
                            for k in allocation), "GitWeave requires reservation accounting", "accounting")
            if not self.resources.reserve(allocation):
                return result("Required resources unavailable; executor was not launched",
                              {"status": "resource_exhausted", "required": allocation})
            request = {"task": inputs["task"], "context": inputs.get("context", {}),
                       "resources": deepcopy(self.resources.state), "allocation": allocation,
                       "instruction": node.get("instruction"), "run_id": self.context["run_id"]}
            output = None
            try:
                output = check_result(self.executor(node["executor"], deepcopy(request)))
                self.resources.settle(allocation, output["usage"])
            except Failure as exc:
                if output is not None:
                    exc.details["result"] = output
                raise
            return output
        if action == "load":
            return result("Project loaded", {"items": self.backend.load()})
        if action == "select":
            task = self.backend.select(inputs["items"])
            return result("Task selected" if task else "No eligible tasks", {"task": task})
        if action == "resources":
            allocation = node.get("config", {}).get("requires", {})
            available = all(k in self.resources.state and self.resources.state[k]["available"] >= v
                            for k, v in allocation.items())
            return result("Resource state", {"resources": deepcopy(self.resources.state), "available": available})
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
        try:
            self.flow(self.graph["flow"])
        except Failure as exc:
            failure = {**exc.record(), "node": self.active}
        return {"run_id": self.context["run_id"], "status": "failed" if failure else "completed",
                "failure": failure, "steps": self.steps, "results": self.context["results"],
                "events": self.events, "last": self.context["last"], "resources": self.resources.state}
