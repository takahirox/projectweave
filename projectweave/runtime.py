"""Bounded single-run graph interpreter."""
from copy import deepcopy
from pathlib import Path
import uuid
from .contracts import Failure, decode, require, pointer, equal, result, check_result
from .graph import validate
from .resources import Resources
from .github import GitHub
from .checkout import valid_repository
from .executors import invoke
from . import usage


class Runtime:
    def __init__(self, graph, project, envelope, backend=None, executor=invoke, checkout=None, workspace=None,
                 observe=usage.observe):
        self.graph = validate(graph)
        self.resources = Resources(envelope)
        self.backend = backend or GitHub(project)
        self.executor = executor
        self.observe = observe
        self.observations = None
        # Maps a Task repository to its prepared local checkout path (see checkout.resolve).
        self.checkout = checkout
        # GitWeave runs in Issue mode from the Project workspace and fetches the repository itself.
        self.workspace = workspace
        self.context = {"run_id": uuid.uuid4().hex, "project": project,
                        "resources": self.resources.state, "results": {}, "last": None}
        self.steps = 0
        self.active = None
        self.events = []

    def node(self, node, inputs):
        action = node.get("action")
        if node["kind"] == "agent" or action == "execute":
            require(isinstance(inputs["task"], dict), "Execution needs a task object", "input")
            allocation = node.get("requires", {})
            if node["executor"]["type"] == "gitweave":
                require(all(self.resources.state.get(k, {}).get("accounting", "reservation") == "reservation"
                            for k in allocation), "GitWeave requires reservation accounting", "accounting")
            if not self.resources.reserve(allocation):
                return result("Required resources unavailable; executor was not launched",
                              {"status": "resource_exhausted", "required": allocation})
            task = inputs["task"]
            request = {"task": task, "context": inputs.get("context", {}),
                       "resources": deepcopy(self.resources.state), "allocation": allocation,
                       "instruction": node.get("instruction"), "run_id": self.context["run_id"]}
            gitweave = node["executor"]["type"] == "gitweave"
            if gitweave:
                require(self.workspace is not None, "Execution needs a Project workspace", "checkout")
                require(valid_repository(task.get("repository")) and type(task.get("number")) is int
                        and task["number"] > 0, "GitWeave execution needs the Task repository and Issue number", "input")
            else:
                require(self.checkout is not None, "Execution needs a Project workspace", "checkout")
                request["checkout"] = self.checkout(task.get("repository"))
            output = None
            try:
                args = (node["executor"], deepcopy(request)) + ((self.workspace,) if gitweave else ())
                output = check_result(self.executor(*args))
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
            config = node.get("config", {})
            names = config.get("subscriptions", [])
            data = {"resources": deepcopy(self.resources.state)}
            if names:
                data["observations"] = deepcopy(self.observed())
            data["available"] = (self.resources.admits(config.get("requires", {}))
                                 and all(self.resources.subscribed(k, data["observations"]) for k in names))
            return result("Resource state", data)
        if action == "status":
            return self.backend.set_status(inputs["task"], node["config"]["status"])
        if action == "writeback":
            return self.backend.writeback(inputs["task"], inputs["result"], self.context["run_id"],
                                          node.get("config", {}).get("status"))
        return deepcopy(node["config"])

    def observed(self):
        """Observe provider usage once per Run for the agent nodes of every GitWeave graph used."""
        if self.observations is None:
            nodes = []
            try:
                for node in self.graph["nodes"].values():
                    if node.get("executor", {}).get("type") == "gitweave":
                        # Resolve like the GitWeave executor, which runs from the workspace.
                        with open(Path(self.workspace or ".") / node["executor"]["graph"]) as source:
                            task_graph = decode(source.read())
                        nodes += [n for n in task_graph["nodes"].values()
                                  if isinstance(n, dict) and n.get("kind", "agent") == "agent"]
                self.observations = self.observe(nodes)
            except (Failure, OSError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
                self.observations = {"(graph)": {"error": f"Cannot read GitWeave agent providers: {exc}"}}
        return self.observations

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
