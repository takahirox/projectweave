"""Per-Project, ordered label-to-GitWeave graph selection and static preflight."""
from pathlib import Path, PureWindowsPath

from .contracts import Failure, keys, require, text
from .executors import process


def validate_routes(routes):
    require(isinstance(routes, list), "graph_routes must be an array")
    labels = set()
    for route in routes:
        keys(route, {"label", "graph", "base_branch"}, {"label", "graph"})
        require(text(route["label"]) and text(route["graph"]),
                "graph_routes label and graph must be nonblank strings")
        if "base_branch" in route:
            require(text(route["base_branch"]), "graph_routes base_branch must be a nonblank string")
            require("\0" not in route["base_branch"], "graph_routes base_branch must not contain NUL")
        label = route["label"].casefold()
        require(label not in labels, f"Duplicate graph_routes label: {route['label']}")
        labels.add(label)
        graph = route["graph"]
        require("\0" not in graph and not Path(graph).is_absolute()
                and not PureWindowsPath(graph).drive and "\\" not in graph,
                f"graph_routes graph must be Project-workspace-relative: {graph}")
        depth = 0
        for part in Path(graph).parts:
            depth += -1 if part == ".." else 0 if part == "." else 1
            require(depth >= 0, f"graph_routes graph escapes the Project workspace: {graph}")


def route_path(directory, graph):
    directory = Path(directory).resolve()
    path = (directory / graph).resolve()
    require(path.is_relative_to(directory),
            f"graph_routes graph escapes the Project workspace: {graph}", "setup")
    return path


def validate_worker(directory, graph):
    """GitWeave's public static validator launches no agents and performs no Task mutation."""
    path = Path(graph)
    if not path.is_absolute():
        path = Path(directory) / path
    try:
        require(path.is_file(), f"GitWeave graph does not exist: {graph}", "setup")
        process(["gitweave", "validate", "--graph", str(path.resolve())], None, 30, cwd=directory)
    except (Failure, OSError, ValueError) as exc:
        raise Failure("setup", f"Invalid GitWeave graph {graph}: {exc}",
                      exc.details if isinstance(exc, Failure) else None) from exc


def check_routes(project, directory, defaults=()):
    """Check every configured route before claiming, including routes on unselected labels."""
    routes = project.get("graph_routes", [])
    if not routes:
        return
    paths = [route_path(directory, route["graph"]) for route in routes]
    paths.extend(Path(graph) if Path(graph).is_absolute() else Path(directory) / graph for graph in defaults)
    for path in dict.fromkeys(path.resolve() for path in paths):
        validate_worker(directory, path)


def select_executor(config, project, task, directory):
    """Return a new config when routed; never mutate a node's fallback graph."""
    if config["type"] != "gitweave" or not project.get("graph_routes"):
        return config
    validate_routes(project["graph_routes"])
    labels = task.get("labels", [])
    require(isinstance(labels, list) and all(text(label) for label in labels),
            "Task labels must be an array of nonblank strings", "input")
    labels = {label.casefold() for label in labels}
    for route in project["graph_routes"]:
        if route["label"].casefold() in labels:
            route_path(directory, route["graph"])
            config = dict(config, graph=route["graph"])
            if "base_branch" in route:
                config["base_branch"] = route["base_branch"]
            break
    validate_worker(directory, config["graph"])
    return config
