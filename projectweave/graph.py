"""Static validation of sequence, conditional, and structured loop flow."""
from .contracts import keys, require, number, text, valid_pointer, check_result


def executor(config):
    require(isinstance(config, dict), "executor must be an object")
    if config.get("type") == "command":
        keys(config, {"type", "argv", "timeout"}, {"type", "argv"})
        require(isinstance(config["argv"], list) and config["argv"] and
                all(text(x) for x in config["argv"]), "argv must be nonempty strings")
    else:
        require("repo" not in config and "commit" not in config,
                "GitWeave repo/commit are resolved per Task from the Project workspace; remove them")
        keys(config, {"type", "graph", "provenance_remote", "timeout"}, {"type", "graph"})
        require(config["type"] == "gitweave", "Unknown executor type")
        require(all(text(v) for k, v in config.items() if k != "timeout"),
                "GitWeave configuration must be nonblank text")
    require(number(config.get("timeout", 3600), True), "timeout must be positive and finite")


def validate(graph):
    keys(graph, {"version", "nodes", "flow", "max_steps"}, {"version", "nodes", "flow"})
    require(type(graph["version"]) is int and graph["version"] == 1, "version must be 1")
    require(type(graph.get("max_steps", 100)) is int and graph.get("max_steps", 100) > 0,
            "max_steps must be a positive integer")
    require(isinstance(graph["nodes"], dict) and graph["nodes"], "nodes must be nonempty")
    for name, node in graph["nodes"].items():
        require(text(name), "Node ID must be nonblank")
        keys(node, {"kind", "action", "instruction", "executor", "requires", "inputs", "config"}, {"kind"})
        inputs = node.get("inputs", {})
        require(isinstance(inputs, dict) and all(text(k) and valid_pointer(v)
                for k, v in inputs.items()), "inputs must map names to JSON pointers")
        kind = node["kind"]
        require(kind in ("agent", "action"), "Unknown node kind")
        execute = kind == "agent" or node.get("action") == "execute"
        if kind == "agent":
            require(text(node.get("instruction")) and "action" not in node,
                    "Agents require instruction and no action")
        else:
            require(node.get("action") in ("load", "select", "resources", "execute", "writeback", "result"),
                    "Unknown action")
            require("instruction" not in node, "instruction is only valid for agents")
        cfg = node.get("config", {})
        if execute:
            require("executor" in node, "Execution requires an executor")
            executor(node["executor"])
            # requires is optional: subscription admission happens in a resources action instead.
            if "requires" in node:
                cost = node["requires"]
                require(isinstance(cost, dict) and cost and all(text(k) and number(v, True)
                        for k, v in cost.items()), "requires must contain positive resource allocations")
            require("task" in inputs, "Execution requires a task input")
            keys(cfg, set())
        else:
            require("executor" not in node and "requires" not in node, "Nonexecution node cannot allocate")
            action = node["action"]
            if action == "writeback":
                keys(cfg, {"status"})
                require("status" not in cfg or text(cfg["status"]), "status must be nonblank")
                require({"task", "result"} <= inputs.keys(), "writeback needs task and result")
            elif action == "result":
                check_result(cfg)
            elif action == "resources":
                keys(cfg, {"requires", "subscriptions"})
                cost = cfg.get("requires", {})
                require(isinstance(cost, dict) and all(text(k) and number(v, True)
                        for k, v in cost.items()), "Invalid resource inspection allocation")
                names = cfg.get("subscriptions", [])
                require(isinstance(names, list) and all(text(k) for k in names) and len(set(names)) == len(names),
                        "subscriptions must be unique resource names")
            else:
                keys(cfg, set())
                if action == "select":
                    require("items" in inputs, "select needs items input")

    def flow(entries, depth=0):
        require(depth <= 32, "Control nesting exceeds 32")
        require(isinstance(entries, list), "flow must be an array")
        for entry in entries:
            if isinstance(entry, str):
                require(entry in graph["nodes"], f"Unknown node: {entry}")
            else:
                keys(entry, {"if", "loop"})
                require(len(entry) == 1, "Expected one control: if or loop")
                if "if" in entry:
                    spec = entry["if"]
                    keys(spec, {"path", "equals", "then", "else"}, {"path", "equals", "then", "else"})
                    require(valid_pointer(spec["path"]), "Invalid condition pointer")
                    flow(spec["then"], depth + 1)
                    flow(spec["else"], depth + 1)
                else:
                    spec = entry["loop"]
                    keys(spec, {"flow", "while"}, {"flow", "while"})
                    keys(spec["while"], {"path", "equals"}, {"path", "equals"})
                    require(valid_pointer(spec["while"]["path"]), "Invalid condition pointer")
                    flow(spec["flow"], depth + 1)
                    require(bool(spec["flow"]), "loop flow must be nonempty")
    flow(graph["flow"])
    require(bool(graph["flow"]), "flow must be nonempty")
    return graph
