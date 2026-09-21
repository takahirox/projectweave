"""Static validation; only sequence and conditional flow."""
from .contracts import keys, require, number, text, valid_pointer, check_result


def executor(config):
    require(isinstance(config, dict), "executor must be an object")
    if config.get("type") == "command":
        keys(config, {"type", "argv", "timeout"}, {"type", "argv"})
        require(isinstance(config["argv"], list) and config["argv"] and
                all(text(x) for x in config["argv"]), "argv must be nonempty strings")
    else:
        keys(config, {"type", "graph", "repo", "commit", "provenance_remote", "timeout"},
             {"type", "graph", "repo", "commit"})
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
            require("executor" in node and "requires" in node, "Execution requires executor and resources")
            executor(node["executor"])
            cost = node["requires"]
            require(isinstance(cost, dict) and cost and all(text(k) and number(v, True)
                    for k, v in cost.items()), "requires must contain positive resource allocations")
            require("task" in inputs, "Execution requires a task input")
            keys(cfg, {"failure_comment"})
            require("failure_comment" not in cfg or type(cfg["failure_comment"]) is bool,
                    "failure_comment must be boolean")
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
                keys(cfg, {"requires"})
                cost = cfg.get("requires", {})
                require(isinstance(cost, dict) and all(text(k) and number(v, True)
                        for k, v in cost.items()), "Invalid resource inspection allocation")
            else:
                keys(cfg, set())
                if action == "select":
                    require("items" in inputs, "select needs items input")

    def flow(entries, depth=0):
        require(depth <= 32, "Conditional nesting exceeds 32")
        require(isinstance(entries, list), "flow must be an array")
        for entry in entries:
            if isinstance(entry, str):
                require(entry in graph["nodes"], f"Unknown node: {entry}")
            else:
                keys(entry, {"if"}, {"if"})
                spec = entry["if"]
                keys(spec, {"path", "equals", "then", "else"}, {"path", "equals", "then", "else"})
                require(valid_pointer(spec["path"]), "Invalid condition pointer")
                flow(spec["then"], depth + 1)
                flow(spec["else"], depth + 1)
    flow(graph["flow"])
    require(bool(graph["flow"]), "flow must be nonempty")
    return graph
