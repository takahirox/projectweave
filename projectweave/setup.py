"""Mechanical first-use setup; no runtime, agents, or setup state database."""
from copy import deepcopy
from importlib.resources import files
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
PROVIDERS = ("codex", "claude")
# The one subscription templates/graph.json checks; ProjectWeave never infers providers from gitweave.json.
SUBSCRIPTION = "subscription"


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
    parser.add_argument("--provider", choices=PROVIDERS, help="GitWeave provider (default codex; claude also enables bypassPermissions)")
    parser.add_argument("--model", help="Explicit model (default: the provider's native default model)")


def template(name):
    return decode(files("projectweave").joinpath("templates", name).read_text())


def templates(workspace, provider=None, model=None):
    """The canonical default workflow pair and resources, materialized for one workspace."""
    graph = template("graph.json")
    # The packaged graph names gitweave.json relative to the workspace; generated files use an absolute path.
    graph["nodes"]["execute"]["executor"]["graph"] = str(workspace / "gitweave.json")
    worker = template("gitweave.json")
    node = worker["nodes"]["work"]
    node["provider"] = provider or node["provider"]
    if model:
        node["model"] = model
    if node["provider"] == "claude":
        # Non-interactive Claude cannot edit or run commands without it; opted into via --provider claude.
        node["permission_mode"] = "bypassPermissions"
    return {"resources.json": template("resources.json"), "graph.json": validate(graph), "gitweave.json": worker}


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
        require(isinstance(candidate.get(SUBSCRIPTION), dict) and candidate[SUBSCRIPTION].get("type") == "subscription",
                f"default setup uses a {SUBSCRIPTION} threshold entry, not gitweave run capacity")
        candidate[SUBSCRIPTION].pop("remaining_percent", None)
        candidate[SUBSCRIPTION]["stop_at_remaining_percent"] = expected[SUBSCRIPTION]["stop_at_remaining_percent"]
    elif name == "graph.json":
        validate(candidate)
    elif name == "gitweave.json":
        node, expected = candidate["nodes"]["work"], deepcopy(expected)
        default = expected["nodes"]["work"]
        for key in ("provider", "instruction"):
            require(text(node[key]), f"{key} must be nonblank")
            node[key] = default[key]
        for key in ("model", "effort", "sandbox", "permission_mode"):
            if key in node:
                require(text(node.pop(key)), f"{key} must be nonblank")
            default.pop(key, None)
    require(equal(candidate, expected), f"Incompatible {name}; review manually (never overwritten)")


def initialize(args):
    report = {"initialized": False, "ready": False, "created": [], "existing": [],
              "missing": [], "next_commands": [], "human_actions": [], "failure": None}
    operation = "Review incompatible files manually; init never overwrites files"
    # The current directory is the Project workspace: configuration here, Task checkouts under repos/.
    workspace = Path.cwd().resolve()
    try:
        require(args.model is None or text(args.model), "--model must be nonblank")
        expected = templates(workspace, args.provider, args.model)
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
        for name, default in expected.items():
            value = read_file(workspace / name)
            if value is not None:
                if name == "gitweave.json" and (args.provider or args.model):
                    # Explicit choices must match an existing file; it is never rewritten to follow them.
                    nodes = value.get("nodes")
                    work = nodes.get("work") if isinstance(nodes, dict) else None
                    work = work if isinstance(work, dict) else {}
                    require(args.provider is None or work.get("provider") == args.provider,
                            f"Existing gitweave.json provider conflicts with --provider {args.provider}; edit it manually or omit the flag")
                    require(args.model is None or work.get("model") == args.model,
                            f"Existing gitweave.json model conflicts with --model {args.model}; edit it manually or omit the flag")
                try:
                    compatible(name, value, default)
                except (Failure, KeyError, TypeError) as exc:
                    raise Failure("setup", f"Incompatible {name}: {exc}") from exc
                report["existing"].append(str(workspace / name))
            values[name] = value if value is not None else default
        worker = values["gitweave.json"]["nodes"]["work"]
        # Workspaces generated before the quick-start defaults may still hold placeholders.
        if worker["provider"] == "CONFIGURE_PROVIDER" or worker.get("model") == "CONFIGURE_MODEL":
            report["missing"].append("Explicit provider and model in gitweave.json")
        if values["resources.json"][SUBSCRIPTION].get("remaining_percent") is None:
            report["missing"].append(f"Observed usage: set resources.json {SUBSCRIPTION}.remaining_percent (0-100) before each Run")
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
        report["human_actions"] = ([
            "Claude is configured with permission_mode bypassPermissions: the agent may edit files and run commands in its GitWeave worktree without asking. Remove it from gitweave.json to restrict Claude (it may then be unable to implement Issues)."]
            if worker.get("permission_mode") == "bypassPermissions" else [
            "Claude has no permission_mode in gitweave.json, so a non-interactive Run may be unable to edit files or run commands; choose one deliberately."]
            if worker["provider"] == "claude" and "permission_mode" not in worker else []) + [
            f"gitweave.json runs provider {worker['provider']} with " + (f"model {worker['model']}" if worker.get("model") else "the provider's native default model")
            + ". Install and authenticate that provider CLI yourself. To change provider/model later, edit gitweave.json (init never rewrites it).",
            f"Before each Run, record the provider subscription's remaining usage as resources.json {SUBSCRIPTION}.remaining_percent (0-100) for the provider chosen in gitweave.json. No Task starts while it is unknown or at/below stop_at_remaining_percent (default 20; adjust deliberately). ProjectWeave does not observe or estimate usage itself; every Run reloads the file.",
            f"Choose an open Issue from any repository and add it to the Project using the commands below, then set its {ELIGIBILITY_FIELD} field to {READY} in the Project. No Issue has been selected or changed.",
            "A Run clones the selected Issue's repository lazily into repos/OWNER/REPO under this workspace with `gh repo clone`, fetches origin, and executes the remote default branch tip (origin/HEAD); push changes you want included. Fetch uses Git credentials: for HTTPS run `gh auth setup-git` or use SSH. Init clones nothing.",
            "Run readiness is not certified: provider credentials, repository clone access, Issue comment permission, Project item-add access, Git identity and provenance push access need human verification. A live GitWeave Run may push refs/notes to origin; init never runs AI or pushes.",
            f"Set {ELIGIBILITY_FIELD} back to Not ready manually after processing if the Issue should not run again; this workflow comments results and does not change Status or {ELIGIBILITY_FIELD}."
        ]
    except (Failure, OSError, UnicodeError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        report["failure"] = {"message": str(exc), "action": operation}
        report["human_actions"].append(operation)
    report["missing"].extend(str(workspace / name) for name in FILES if not (workspace / name).is_file())
    return report
