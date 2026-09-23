"""Mechanical first-use setup; no runtime, agents, or setup state database."""
from copy import deepcopy
import json
from pathlib import Path
import re
import shlex
import tempfile

from .contracts import Failure, decode, equal, require, text
from .executors import process
from .github import ELIGIBILITY_FIELD, ELIGIBILITY_OPTIONS, READY, GitHub, validate_project
from .graph import validate
from .resources import Resources

PRIORITIES = ["P0", "P1", "P2"]
FILES = ("project.json", "resources.json", "graph.json", "gitweave.json")


def ensure_field(backend, report, fields, name, options):
    """Reuse a compatible single-select field or create it; incompatible fields are never repaired."""
    summary = f"{name} field ({'/'.join(options)})"
    matches = [field for field in fields if field["name"] == name]
    if matches:
        require(len(matches) == 1, f"Incompatible {name} fields: name is ambiguous; review manually")
        field = matches[0]
        require(field["__typename"] == "ProjectV2SingleSelectField" and field["dataType"] == "SINGLE_SELECT",
                f"Incompatible {name} field: expected SINGLE_SELECT; existing field left unchanged")
        names = [option["name"] for option in field["options"]]
        require(all(names.count(option) == 1 for option in options),
                f"Incompatible {name} options: require {', '.join(options)} exactly once each; existing options left unchanged")
        report["existing"].append(summary)
    else:
        response = backend.query("""mutation($input:CreateProjectV2FieldInput!) {
          createProjectV2Field(input:$input) { projectV2Field {
            ... on ProjectV2SingleSelectField { id name options { name } }
          } } }""", {"input": {"projectId": backend.resolve(), "name": name,
            "dataType": "SINGLE_SELECT", "singleSelectOptions": [
                {"name": option, "color": "GRAY", "description": ""} for option in options]}})
        field = response["createProjectV2Field"]["projectV2Field"]
        require(text(field["id"]) and field["name"] == name
                and [option["name"] for option in field["options"]] == options,
                f"Unexpected {name} creation response; rerun to inspect existing fields")
        report["created"].append(summary)
    report["missing"].remove(pending(name, options))


def pending(name, options):
    return f"Verified {name} single-select field with {'/'.join(options)} options"


def ensure_fields(backend, report):
    required = (("Priority", PRIORITIES), (ELIGIBILITY_FIELD, ELIGIBILITY_OPTIONS))
    report["missing"].extend(pending(name, options) for name, options in required)
    # Exhaust pagination before deciding a field is absent (or unambiguous).
    fields = list(backend.pages(backend.resolve(), "ProjectV2", "fields",
        "__typename ... on ProjectV2FieldCommon { name dataType } "
        "... on ProjectV2SingleSelectField { options { name } }"))
    for name, options in required:
        ensure_field(backend, report, fields, name, options)
    report["fields"] = f"Priority and {ELIGIBILITY_FIELD} verified; Status unchanged (no filter or update)"


def add_init_arguments(parser):
    parser.add_argument("--project-owner", help="Project owner login (required unless saved in project.json)")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--project-number", type=int, help="Use this existing Project v2")
    choice.add_argument("--create-project", metavar="TITLE", help="Explicitly create a Project, unless saved configuration exists")


def templates(workspace):
    resources = {"gitweave": {"unit": "runs", "available": 0, "accounting": "reservation"}}
    worker = {"version": 1, "retries": 0, "max_steps": 1, "nodes": {"work": {
        "kind": "agent", "provider": "CONFIGURE_PROVIDER", "model": "CONFIGURE_MODEL",
        "workspace_base": 0,
        "instruction": "Implement only the selected Issue in the supplied ProjectWeave request. "
                       "Follow repository development instructions, run relevant checks, and summarize the result. "
                       "Leave artifacts in the assigned worktree. Do not publish, push, merge, or change GitHub state. "
                       "Do not reset usage limits, buy allowance, or switch models/providers; stop on a usage limit."
    }}, "flow": ["work"]}
    # The runtime supplies each selected Task's checkout; no project-wide repository path.
    graph = {"version": 1, "nodes": {
        "load": {"kind": "action", "action": "load"},
        "select": {"kind": "action", "action": "select", "inputs": {"items": "/results/load/data/items"}},
        "capacity": {"kind": "action", "action": "resources", "config": {"requires": {"gitweave": 1}}},
        "execute": {"kind": "action", "action": "execute", "requires": {"gitweave": 1},
                    "executor": {"type": "gitweave", "graph": str(workspace / "gitweave.json")},
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
    operation = "Review incompatible files manually; init never overwrites files"
    # The current directory is the Project workspace: configuration here, Task checkouts under repos/.
    workspace = Path.cwd().resolve()
    try:
        expected = templates(workspace)
        saved = read_file(workspace / "project.json")
        owner = args.project_owner or (saved.get("owner") if isinstance(saved, dict) else None)
        require(owner is not None, "Choose --project-owner LOGIN for the GitHub Project")
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
                   "number": number or 1, "priority_order": PRIORITIES}
        if saved is not None:
            # An explicit repository filter from older setups remains an accepted optional policy.
            unscoped = {key: value for key, value in saved.items() if key != "repository"}
            require(equal(unscoped, project), "Incompatible project.json; default setup uses Priority order P0/P1/P2 and no Status policy; review manually")
            report["existing"].append(str(workspace / "project.json"))
        values = {}
        for name, template in expected.items():
            value = read_file(workspace / name)
            if value is not None:
                try:
                    compatible(name, value, template)
                except (Failure, KeyError, TypeError) as exc:
                    raise Failure("setup", f"Incompatible {name}: {exc}") from exc
                report["existing"].append(str(workspace / name))
            values[name] = value if value is not None else template
        worker = values["gitweave.json"]["nodes"]["work"]
        if worker["provider"] == "CONFIGURE_PROVIDER" or worker["model"] == "CONFIGURE_MODEL":
            report["missing"].append("Explicit provider and model in gitweave.json")
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
        operation = (f"Check workspace write access, then rerun `projectweave init "
                     f"--project-owner {owner} --project-number {project['number']}` to reuse this Project")
        for name, value in {"project.json": project, **values}.items():
            path = workspace / name
            if not path.exists():
                with path.open("x") as output:
                    output.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
                report["created"].append(str(path))
        operation = (f"Check Project field read access and write access for missing Priority/{ELIGIBILITY_FIELD} creation; "
                     "review incompatible fields manually. Rerun init to read all fields and reuse any "
                     "field created before a failure; existing fields are never repaired")
        ensure_fields(backend, report)
        report["initialized"] = True
        q = shlex.quote
        report["next_commands"] = [
            f"cd {q(str(workspace))}",
            "gitweave validate --graph gitweave.json",
            "projectweave validate --graph graph.json",
            "ISSUE_URL='REPLACE_WITH_OPEN_ISSUE_URL'  # e.g. https://github.com/OWNER/REPO/issues/123",
            f'GH_HOST=github.com gh project item-add {project["number"]} --owner {q(owner)} --url "$ISSUE_URL"',
            "projectweave run --graph graph.json --project project.json --resources resources.json"
        ]
        report["human_actions"] = [
            "Choose provider/model explicitly in gitweave.json; review instruction and any effort/permission settings. Install and authenticate that provider yourself.",
            "Set resources.json capacity deliberately. It is a per-Run reservation count, not a token or money budget; every Run reloads the file.",
            f"Choose an open Issue from any repository and add it to the Project using the commands below, then set its {ELIGIBILITY_FIELD} field to {READY} in the Project. No Issue has been selected or changed.",
            "A Run clones the selected Issue's repository lazily into repos/OWNER/REPO under this workspace with `gh repo clone`, fetches origin, and executes the remote default branch tip (origin/HEAD); push changes you want included. Init clones nothing.",
            "Run readiness is not certified: provider credentials, repository clone access, Issue comment permission, Project item-add access, Git identity and provenance push access need human verification. A live GitWeave Run may push refs/notes to origin; init never runs AI or pushes.",
            f"Set {ELIGIBILITY_FIELD} back to Not ready manually after processing if the Issue should not run again; this workflow comments results and does not change Status or {ELIGIBILITY_FIELD}."
        ]
    except (Failure, OSError, UnicodeError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        report["failure"] = {"message": str(exc), "action": operation}
        report["human_actions"].append(operation)
    report["missing"].extend(str(workspace / name) for name in FILES if not (workspace / name).is_file())
    return report
