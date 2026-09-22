"""Mechanical first-use setup; no runtime, agents, or setup state database."""
from copy import deepcopy
import json
from pathlib import Path
import re
import shlex
import tempfile

from .contracts import Failure, decode, equal, require, text
from .executors import process
from .github import GitHub, validate_project
from .graph import validate
from .resources import Resources

LABEL = "projectweave-ready"
FILES = ("project.json", "resources.json", "graph.json", "gitweave.json")


def add_init_arguments(parser):
    parser.add_argument("--repo", required=True, help="github.com OWNER/REPO; must match checkout origin")
    parser.add_argument("--project-owner", help="Project owner login (defaults to saved owner, then repository owner)")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--project-number", type=int, help="Use this existing Project v2")
    choice.add_argument("--create-project", metavar="TITLE", help="Explicitly create a Project, unless saved configuration exists")


def templates(root):
    directory = root / ".projectweave"
    resources = {"gitweave": {"unit": "runs", "available": 0, "accounting": "reservation"}}
    worker = {"version": 1, "retries": 0, "max_steps": 1, "nodes": {"work": {
        "kind": "agent", "provider": "CONFIGURE_PROVIDER", "model": "CONFIGURE_MODEL",
        "workspace_base": 0,
        "instruction": "Implement only the selected Issue in the supplied ProjectWeave request. "
                       "Follow repository development instructions, run relevant checks, and summarize the result. "
                       "Leave artifacts in the assigned worktree. Do not publish, push, merge, or change GitHub state. "
                       "Do not reset usage limits, buy allowance, or switch models/providers; stop on a usage limit."
    }}, "flow": ["work"]}
    graph = {"version": 1, "nodes": {
        "load": {"kind": "action", "action": "load"},
        "select": {"kind": "action", "action": "select", "inputs": {"items": "/results/load/data/items"}},
        "capacity": {"kind": "action", "action": "resources", "config": {"requires": {"gitweave": 1}}},
        "execute": {"kind": "action", "action": "execute", "requires": {"gitweave": 1},
                    "executor": {"type": "gitweave", "graph": str(directory / "gitweave.json"),
                                 "repo": str(root), "commit": "HEAD"},
                    "inputs": {"task": "/results/select/data/task"}},
        "comment": {"kind": "action", "action": "writeback", "inputs": {
            "task": "/results/select/data/task", "result": "/results/execute"}}
    }, "flow": ["load", "select", {"if": {"path": "/results/select/data/task", "equals": None,
        "then": [], "else": ["capacity", {"if": {"path": "/results/capacity/data/available",
        "equals": True, "then": ["execute", "comment"], "else": []}}]}}]}
    return {"resources.json": resources, "graph.json": validate(graph), "gitweave.json": worker}


def read_file(path):
    require(not path.is_symlink(), f"Incompatible symlink: {path}")
    if not path.exists():
        return None
    try:
        value = decode(path.read_text())
    except Failure as exc:
        raise Failure("setup", f"Incompatible {path}: {exc}") from exc
    require(isinstance(value, dict), f"Incompatible {path}: expected a JSON object")
    return value


def compatible(name, value, expected):
    """Only the fixed scaffold and its documented human-editable knobs are reused."""
    candidate = deepcopy(value)
    if name == "resources.json":
        Resources(candidate)
        candidate["gitweave"]["available"] = 0
    elif name == "graph.json":
        validate(candidate)
    elif name == "gitweave.json":
        node = candidate["nodes"]["work"]
        for key in ("provider", "model", "instruction"):
            require(text(node[key]), f"{key} must be nonblank")
            node[key] = expected["nodes"]["work"][key]
        for key in ("effort", "sandbox", "permission_mode"):
            if key in node:
                require(text(node.pop(key)), f"{key} must be nonblank")
    require(equal(candidate, expected), f"Incompatible {name}; review manually (never overwritten)")


def initialize(args):
    report = {"initialized": False, "ready": False, "created": [], "existing": [],
              "missing": [], "next_commands": [], "human_actions": [], "failure": None}
    directory = None
    operation = "Verify git is installed and run init inside the intended checkout"
    try:
        require(bool(re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", args.repo))
                and args.repo.split("/")[1] not in (".", ".."), "--repo must be OWNER/REPO on github.com")
        root = Path(process(["git", "rev-parse", "--show-toplevel"], None, 30).strip()).resolve()
        origin = process(["git", "remote", "get-url", "origin"], None, 30).strip()
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?", origin)
        require(match is not None and match[1].lower() == args.repo.lower(),
                "Checkout origin does not match --repo; use the matching checkout (github.com only)")
        operation = "Create a first commit manually; GitWeave requires a committed base"
        process(["git", "rev-parse", "--verify", "HEAD^{commit}"], None, 30)
        operation = "Review incompatible files manually; init never overwrites files"
        directory = root / ".projectweave"
        require(not directory.is_symlink(), f"Incompatible symlink: {directory}")
        expected = templates(root)
        saved = read_file(directory / "project.json")
        owner = args.project_owner or (saved.get("owner") if isinstance(saved, dict) else None) or args.repo.split("/")[0]
        require(bool(re.fullmatch(r"[A-Za-z0-9_-]+", owner)), "Invalid --project-owner")
        number = args.project_number
        if saved is not None:
            validate_project(saved)
            require(saved["owner"].lower() == owner.lower() and (number is None or saved["number"] == number),
                    "Existing project.json conflicts with explicit Project selection")
            number = saved["number"]
        require(number is None or number > 0, "--project-number must be positive")
        require(number is not None or text(args.create_project),
                "Choose --project-number NUMBER or explicitly --create-project TITLE; no Project is auto-selected")
        project = {"owner": owner, "owner_type": saved["owner_type"] if saved else "user",
                   "number": number or 1, "repository": args.repo, "label": LABEL, "priority_order": []}
        if saved is not None:
            require(equal(saved, project), "Incompatible project.json; default setup uses repository + label only, no Status/Priority policy")
            report["existing"].append(str(directory / "project.json"))
        values = {}
        for name, template in expected.items():
            value = read_file(directory / name)
            if value is not None:
                try:
                    compatible(name, value, template)
                except (Failure, KeyError, TypeError) as exc:
                    raise Failure("setup", f"Incompatible {name}: {exc}") from exc
                report["existing"].append(str(directory / name))
            values[name] = value if value is not None else template
        worker = values["gitweave.json"]["nodes"]["work"]
        if worker["provider"] == "CONFIGURE_PROVIDER" or worker["model"] == "CONFIGURE_MODEL":
            report["missing"].append("Explicit provider and model in .projectweave/gitweave.json")
        if values["resources.json"]["gitweave"]["available"] < 1:
            report["missing"].append("Human-approved capacity: set resources.json gitweave.available to at least 1")
        # GitWeave's public validator does not run agents or access the network.
        operation = "Install GitWeave with its public validate/run CLI on PATH; review gitweave.json if validation fails"
        with tempfile.TemporaryDirectory(prefix="projectweave-validate-") as tmp:
            check = Path(tmp) / "gitweave.json"
            check.write_text(json.dumps(values["gitweave.json"]))
            process(["gitweave", "validate", "--graph", str(check)], None, 30)
        operation = "Install gh on PATH and check `gh auth status --hostname github.com`; grant access manually if needed"
        process(["gh", "auth", "status", "--hostname", "github.com"], None, 30)
        operation = f"Check repository access with `gh repo view {args.repo}`; label creation requires repository write access"
        repository = decode(process(["gh", "api", "--hostname", "github.com", f"repos/{args.repo}"], None, 120))
        require(repository["full_name"].lower() == args.repo.lower(), "GitHub repository identity differs from --repo")
        operation = f"Check Project owner {owner}; supported owner types are user and organization"
        account = decode(process(["gh", "api", "--hostname", "github.com", f"users/{owner}"], None, 120))
        require(account["type"] in ("User", "Organization"), "Unsupported Project owner type")
        kind = {"User": "user", "Organization": "organization"}[account["type"]]
        require(saved is None or saved["owner_type"] == kind, "Existing owner_type conflicts with GitHub owner")
        project["owner_type"] = kind
        backend = GitHub(project)
        operation = f"Check Project access for {owner}; Projects read access is required, write access only for explicit creation"
        if number is None:
            operation = (f"Project creation may have succeeded. Inspect `GH_HOST=github.com gh project list --owner {owner}` and rerun with "
                         "--project-number NUMBER if present; do not blindly repeat --create-project")
            response = backend.query("mutation($owner:ID!,$title:String!) { createProjectV2(input:{ownerId:$owner,title:$title}) { projectV2 { id number } } }",
                                     {"owner": account["node_id"], "title": args.create_project})
            created = response["createProjectV2"]["projectV2"]
            require(type(created["number"]) is int and created["number"] > 0 and text(created["id"]), "Invalid created Project response")
            project["number"] = created["number"]
            report["created"].append(f"Project {owner} #{project['number']}")
        else:
            backend.resolve()
            report["existing"].append(f"Project {owner} #{number}")
        report["project"] = project
        # Save the Project identity first so ordinary reruns reuse a created Project.
        operation = (f"Check .projectweave write access, then rerun `projectweave init --repo {args.repo} "
                     f"--project-owner {owner} --project-number {project['number']}` to reuse this Project")
        directory.mkdir(exist_ok=True)
        for name, value in {"project.json": project, **values}.items():
            path = directory / name
            if not path.exists():
                with path.open("x") as output:
                    output.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
                report["created"].append(str(path))
        operation = f"Check label access in {args.repo}; rerun to reuse any label created before a failure"
        repo_owner, repo_name = args.repo.split("/")
        response = backend.query("query($owner:String!,$name:String!,$label:String!) { repository(owner:$owner,name:$name) { label(name:$label) { name } } }",
                                 {"owner": repo_owner, "name": repo_name, "label": LABEL})
        label = response["repository"]["label"]
        if label is not None:
            require(label["name"] == LABEL, "Incompatible label casing; rename it manually to projectweave-ready")
            report["existing"].append(f"label {LABEL}")
        else:
            report["missing"].append(f"Confirmed label {LABEL} (rerun to check after an uncertain creation)")
            process(["gh", "label", "create", LABEL, "--repo", f"github.com/{args.repo}", "--color", "0E8A16",
                     "--description", "Explicitly eligible for ProjectWeave"], None, 120)
            report["created"].append(f"label {LABEL}")
            report["missing"].remove(f"Confirmed label {LABEL} (rerun to check after an uncertain creation)")
        report["fields"] = "None required; Status and Priority are neither checked nor modified"
        report["initialized"] = True
        q = shlex.quote
        report["next_commands"] = [
            f"cd {q(str(root))}",
            f"gitweave validate --graph {q(str(directory / 'gitweave.json'))}",
            f"projectweave validate --graph {q(str(directory / 'graph.json'))}",
            "ISSUE_NUMBER='REPLACE_WITH_OPEN_ISSUE_NUMBER'",
            f'GH_HOST=github.com gh project item-add {project["number"]} --owner {q(owner)} --url "https://github.com/{args.repo}/issues/$ISSUE_NUMBER"',
            f'gh issue edit "$ISSUE_NUMBER" --repo github.com/{args.repo} --add-label {LABEL}',
            "projectweave run --graph .projectweave/graph.json --project .projectweave/project.json --resources .projectweave/resources.json"
        ]
        report["human_actions"] = [
            "Choose provider/model explicitly in gitweave.json; review instruction and any effort/permission settings. Install and authenticate that provider yourself.",
            "Set resources.json capacity deliberately. It is a per-Run reservation count, not a token or money budget; every Run reloads the file.",
            "Choose an open Issue, add it to the Project, then label it ready using the commands below. No Issue has been selected or changed.",
            "Review and commit intended repository changes before running: GitWeave executes HEAD, not uncommitted edits.",
            "Run readiness is not certified: provider credentials, Issue comment permission, Project item-add access, Git identity and provenance push access need human verification. A live GitWeave Run may push refs/notes to origin; init never runs AI or pushes.",
            "Remove the ready label manually after processing if the Issue should not run again; this workflow comments results and does not change Status."
        ]
    except (Failure, OSError, UnicodeError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        report["failure"] = {"message": str(exc), "action": operation}
        report["human_actions"].append(operation)
    if directory is not None:
        report["missing"].extend(str(directory / name) for name in FILES if not (directory / name).is_file())
    return report
