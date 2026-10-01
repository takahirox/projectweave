"""Mechanical first-use setup; no runtime, agents, or setup state database."""
from copy import deepcopy
from importlib.resources import files
import json
from pathlib import Path
import re
import shlex
import tempfile

from .checkout import valid_repository
from .contracts import Failure, decode, equal, require, text
from .executors import process
from .github import ELIGIBILITY_FIELD, ELIGIBILITY_OPTIONS, READY, GitHub, validate_project
from .graph import validate
from .usage import OBSERVERS
from .workspace import NAME, PROJECTS, ROOT_CONFIG, percent, validate_root

PRIORITIES = ["P0", "P1", "P2"]
# Only Todo Tasks are claimed; claim sets In Progress and complete sets Done, so init requires all three.
STATUSES = ["Todo", "In Progress", "Done"]
FILES = ("project.json", "graph.json", "gitweave.json")
PROVIDERS = ("codex", "claude")


def ensure_field(backend, report, fields, name, options, create=True):
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
        require(create, f"Missing {name} field: add a single-select {name} field with {', '.join(options)} in the Project")
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
    report["missing"].extend(pending(name, options) for name, options in required + (("Status", STATUSES),))
    # Exhaust pagination before deciding a field is absent (or unambiguous).
    fields = list(backend.pages(backend.resolve(), "ProjectV2", "fields",
        "__typename ... on ProjectV2FieldCommon { name dataType } "
        "... on ProjectV2SingleSelectField { options { name } }"))
    # GitHub Projects provide Status; init only verifies it (first, so nothing is created
    # when it is unusable) and never creates or repairs it.
    ensure_field(backend, report, fields, "Status", STATUSES, create=False)
    for name, options in required:
        ensure_field(backend, report, fields, name, options)
    report["fields"] = f"Priority, {ELIGIBILITY_FIELD} and Status ({'/'.join(STATUSES)}) verified"


def link_repositories(backend, report, repositories):
    """Link explicitly named repositories; already-linked ones are reused and nothing is unlinked."""
    entries = [f"Linked repository {repository}" for repository in repositories]
    report["missing"].extend(entries)
    # Exhaust pagination before deciding a repository is not linked yet.
    linked = {node["nameWithOwner"].lower() for node in
              backend.pages(backend.resolve(), "ProjectV2", "repositories", "nameWithOwner")}
    for repository, entry in zip(repositories, entries):
        if repository.lower() in linked:
            report["existing"].append(f"Repository link {repository}")
        else:
            owner, name = repository.split("/")
            try:
                # GitHub answers a missing or invisible repository with a GraphQL error (gh exits nonzero).
                found = backend.query("query($owner:String!,$name:String!) { repository(owner:$owner,name:$name) { id } }",
                                      {"owner": owner, "name": name})["repository"]
                require(isinstance(found, dict) and text(found.get("id")), "repository not found")
                response = backend.query("""mutation($project:ID!,$repository:ID!) {
                  linkProjectV2ToRepository(input:{projectId:$project,repositoryId:$repository}) {
                    repository { nameWithOwner } } }""", {"project": backend.resolve(), "repository": found["id"]})
                require(response["linkProjectV2ToRepository"]["repository"]["nameWithOwner"].lower() == repository.lower(),
                        "unexpected link response; rerun to inspect linked repositories")
            except (Failure, KeyError, TypeError) as exc:
                raise Failure("setup", f"Cannot link {repository}: {exc}") from exc
            report["created"].append(f"Repository link {repository}")
        report["missing"].remove(entry)


def add_init_arguments(parser):
    parser.add_argument("--project-owner", help="Project owner login (required unless saved in project.json)")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--project-number", type=int, help="Use this existing Project v2")
    choice.add_argument("--create-project", metavar="TITLE", help="Explicitly create a Project, unless saved configuration exists")
    parser.add_argument("--provider", choices=PROVIDERS, help="GitWeave provider (default codex; claude also enables bypassPermissions)")
    parser.add_argument("--model", help="Explicit model (default: the provider's native default model)")
    parser.add_argument("--link-repository", action="append", default=[], metavar="OWNER/REPO",
                        help="Link this repository to the Project (repeatable; same owner as the Project)")
    parser.add_argument("--resource", action="append", default=[], metavar="PROVIDER:MIN:ESTIMATE",
                        help="Opt in to shared resource admission for this Project, e.g. codex:20:10 "
                             "(min_remaining_percent 20, estimated_usage_percent_per_task 10); repeatable")


def template(name):
    return decode(files("projectweave").joinpath("templates", name).read_text())


def agents(worker):
    nodes = worker.get("nodes") if isinstance(worker, dict) else None
    return [node for node in (nodes.values() if isinstance(nodes, dict) else [])
            if isinstance(node, dict) and node.get("kind", "agent") == "agent"]


def templates(workspace, provider=None, model=None):
    """The canonical default workflow pair, materialized for one Project workspace."""
    graph = template("graph.json")
    # The packaged graph names gitweave.json relative to the workspace; generated files use an absolute path.
    graph["nodes"]["execute"]["executor"]["graph"] = str(workspace / "gitweave.json")
    worker = template("gitweave.json")
    # Provider/model choices apply to every agent node of the Task graph.
    for node in agents(worker):
        node["provider"] = provider or node["provider"]
        if model:
            node["model"] = model
        if node["provider"] == "claude":
            # Non-interactive Claude cannot edit or run commands without it; opted into via --provider claude.
            node["permission_mode"] = "bypassPermissions"
    return {"graph.json": validate(graph), "gitweave.json": worker}


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
    if name == "graph.json":
        validate(candidate)
    elif name == "gitweave.json":
        expected = deepcopy(expected)
        require(isinstance(candidate.get("nodes"), dict) and candidate["nodes"].keys() == expected["nodes"].keys(),
                f"nodes must be {', '.join(expected['nodes'])}; regenerate the workspace for the current template (no migration)")
        # Per agent node, provider/instruction and the optional model/permission knobs are human-editable.
        for node_id, default in expected["nodes"].items():
            node = candidate["nodes"][node_id]
            for key in ("provider", "instruction"):
                require(text(node[key]), f"{node_id}.{key} must be nonblank")
                node[key] = default[key]
            for key in ("model", "effort", "sandbox", "permission_mode"):
                if key in node:
                    require(text(node.pop(key)), f"{node_id}.{key} must be nonblank")
                default.pop(key, None)
    require(equal(candidate, expected), f"Incompatible {name}; review manually (never overwritten)")


def empty_report():
    return {"initialized": False, "ready": False, "created": [], "existing": [],
            "missing": [], "next_commands": [], "human_actions": [], "failure": None}


def init_root(args):
    """Create (or verify) the root workspace: projectweave.json and projects/. Never overwrites."""
    report = empty_report()
    root = Path.cwd().resolve()
    operation = f"Review {ROOT_CONFIG} manually; init never overwrites it"
    try:
        config = read_file(root / ROOT_CONFIG)
        if config is None:
            with (root / ROOT_CONFIG).open("x") as output:
                output.write(json.dumps({"projects": {}}, indent=2) + "\n")
            report["created"].append(str(root / ROOT_CONFIG))
        else:
            try:
                validate_root(config)
            except Failure as exc:
                raise Failure("setup", f"Incompatible {ROOT_CONFIG}: {exc}") from exc
            report["existing"].append(str(root / ROOT_CONFIG))
        operation = f"Check write access to {root}"
        require(not (root / PROJECTS).is_symlink(), f"Incompatible symlink: {root / PROJECTS}")
        if (root / PROJECTS).is_dir():
            report["existing"].append(str(root / PROJECTS))
        else:
            (root / PROJECTS).mkdir()
            report["created"].append(str(root / PROJECTS))
        report["initialized"] = True
        report["next_commands"] = [
            "projectweave init-project NAME --project-owner OWNER --project-number NUMBER  # or --create-project TITLE",
        ]
        report["human_actions"] = [
            f"Add each managed Project with `projectweave init-project NAME ...`; it becomes {PROJECTS}/NAME/. "
            f"Resource constraints are opt-in per Project and provider in {ROOT_CONFIG} (init-project --resource PROVIDER:MIN:ESTIMATE); "
            "a Project without them is not limited by shared AI resource admission.",
        ]
    except (Failure, OSError, UnicodeError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        report["failure"] = {"message": str(exc), "action": operation}
        report["human_actions"].append(operation)
    return report


def parse_resources(values):
    """--resource PROVIDER:MIN:ESTIMATE -> {provider: {min_remaining_percent, estimated_usage_percent_per_task}}."""
    rules = {}
    for value in values:
        parts = value.split(":")
        try:
            provider, low, estimate = parts[0], float(parts[1]), float(parts[2])
        except (IndexError, ValueError) as exc:
            raise Failure("setup", f"--resource must be PROVIDER:MIN:ESTIMATE: {value!r}") from exc
        low, estimate = (int(v) if v.is_integer() else v for v in (low, estimate))
        require(len(parts) == 3 and text(provider) and percent(low) and percent(estimate),
                f"--resource must be PROVIDER:MIN:ESTIMATE with percentages 0-100: {value!r}")
        require(provider not in rules, f"--resource names {provider} twice")
        # A provider without an observer would always be unknown and block the Project forever.
        require(provider in OBSERVERS, f"--resource provider must be one of {', '.join(sorted(OBSERVERS))}: {value!r}")
        rules[provider] = {"min_remaining_percent": low, "estimated_usage_percent_per_task": estimate}
    return rules


def init_project(args):
    """Create (or verify) projects/NAME/ under the root, then record any requested resource policy."""
    root = Path.cwd().resolve()
    report = empty_report()
    operation = "Run projectweave init in the root workspace first, then init-project NAME there"
    try:
        require(NAME.fullmatch(args.name) is not None, f"Invalid Project name: {args.name!r}")
        config = read_file(root / ROOT_CONFIG)
        require(config is not None and (root / PROJECTS).is_dir(), f"No {ROOT_CONFIG} and {PROJECTS}/ here")
        validate_root(config)
        operation = "Pass --resource as PROVIDER:MIN:ESTIMATE with percentages 0-100, or omit it"
        rules = parse_resources(args.resource)
        # Explicit choices must match existing root policy entries; they are never rewritten.
        existing = config["projects"].get(args.name, {}).get("resources", {})
        for provider, rule in rules.items():
            require(provider not in existing or existing[provider] == rule,
                    f"{ROOT_CONFIG} already constrains {args.name}.{provider} differently; edit it manually or omit --resource")
        workspace = root / PROJECTS / args.name
        require(not workspace.is_symlink(), f"Incompatible symlink: {workspace}")
        workspace.mkdir(exist_ok=True)
    except (Failure, OSError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
        report["failure"] = {"message": str(exc), "action": operation}
        report["human_actions"].append(operation)
        return report
    report = initialize(args, workspace, root)
    if report["initialized"] and rules:
        added = {provider: rule for provider, rule in rules.items() if provider not in existing}
        if added:
            operation = f"Check write access to {root / ROOT_CONFIG}, then rerun init-project with the same --resource"
            try:
                # Re-read just before writing so other Projects' entries are kept as they are now.
                current = validate_root(read_file(root / ROOT_CONFIG))
                current["projects"].setdefault(args.name, {}).setdefault("resources", {}).update(added)
                (root / ROOT_CONFIG).write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n")
            except (Failure, OSError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
                report["initialized"] = False
                report["failure"] = {"message": str(exc), "action": operation}
                report["human_actions"].append(operation)
                return report
            report["created"].extend(f"{ROOT_CONFIG} policy {args.name}.{provider}" for provider in added)
        report["existing"].extend(f"{ROOT_CONFIG} policy {args.name}.{provider}" for provider in rules if provider not in added)
    return report


def initialize(args, workspace, root):
    report = empty_report()
    review_files = "Review incompatible files manually; init never overwrites files"
    # projects/NAME/ is the Project workspace: configuration and GitWeave's .gitweave/ live here.
    key = workspace.name  # The Project's directory name under projects/.
    try:
        # Argument checks name the argument in their recovery action; file checks keep review_files.
        operation = "Pass a nonblank --model MODEL, or omit it to use the provider's native default model"
        require(args.model is None or text(args.model), "--model must be nonblank")
        operation = review_files
        expected = templates(workspace, args.provider, args.model)
        saved = read_file(workspace / "project.json")
        # Without the flag the owner comes from project.json, so a bad value is a file problem.
        operation = (review_files if args.project_owner is None and saved is not None
                     else "Pass --project-owner LOGIN (letters, digits, - or _) naming the GitHub Project owner")
        owner = args.project_owner or (saved.get("owner") if isinstance(saved, dict) else None)
        require(owner is not None, "Choose --project-owner LOGIN for the GitHub Project")
        require(bool(re.fullmatch(r"[A-Za-z0-9_-]+", owner)), "Invalid --project-owner")
        operation = "Pass --link-repository as OWNER/REPO owned by the Project owner, or omit it"
        links = []
        for repository in args.link_repository:
            require(valid_repository(repository), f"--link-repository must be OWNER/REPO: {repository!r}")
            require(repository.split("/")[0].lower() == owner.lower(),
                    f"--link-repository {repository}: only repositories owned by the Project owner {owner} can be linked")
            if repository.lower() not in (link.lower() for link in links):
                links.append(repository)
        operation = review_files
        number = args.project_number
        if saved is not None:
            validate_project(saved)
            require(saved["owner"].lower() == owner.lower() and (number is None or saved["number"] == number),
                    "Existing project.json conflicts with explicit Project selection")
            number = saved["number"]
        operation = "Pass a positive --project-number NUMBER for an existing Project, or --create-project TITLE"
        require(number is None or number > 0, "--project-number must be positive")
        require(number is not None or text(args.create_project),
                "Choose --project-number NUMBER or explicitly --create-project TITLE; no Project is auto-selected")
        operation = review_files
        project = {"owner": owner, "owner_type": saved["owner_type"] if saved else "user",
                   "number": number or 1, "priority_order": PRIORITIES, "eligible_statuses": STATUSES[:1]}
        if saved is not None:
            # An explicit repository filter from older setups remains an accepted optional policy.
            unscoped = {key: value for key, value in saved.items() if key != "repository"}
            require(equal(unscoped, project), "Incompatible project.json; default setup uses Priority order P0/P1/P2 and eligible_statuses [\"Todo\"]; review manually or regenerate")
            report["existing"].append(str(workspace / "project.json"))
        values = {}
        for name, default in expected.items():
            value = read_file(workspace / name)
            if value is not None:
                if name == "gitweave.json" and (args.provider or args.model):
                    # Explicit choices must match an existing file; it is never rewritten to follow them.
                    existing = agents(value)
                    require(args.provider is None or all(node.get("provider") == args.provider for node in existing),
                            f"Existing gitweave.json provider conflicts with --provider {args.provider}; edit it manually or omit the flag")
                    require(args.model is None or all(node.get("model") == args.model for node in existing),
                            f"Existing gitweave.json model conflicts with --model {args.model}; edit it manually or omit the flag")
                try:
                    compatible(name, value, default)
                except (Failure, KeyError, TypeError) as exc:
                    raise Failure("setup", f"Incompatible {name}: {exc}") from exc
                report["existing"].append(str(workspace / name))
            values[name] = value if value is not None else default
        workers = agents(values["gitweave.json"])
        choices = sorted({node["provider"] + (f" with model {node['model']}" if node.get("model") else " with its native default model")
                          for node in workers})
        # Workspaces generated before the quick-start defaults may still hold placeholders.
        if any(node["provider"] == "CONFIGURE_PROVIDER" or node.get("model") == "CONFIGURE_MODEL" for node in workers):
            report["missing"].append("Explicit provider and model in gitweave.json")
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
        operation = (f"Check workspace write access, then rerun `projectweave init-project {key} "
                     f"--project-owner {owner} --project-number {project['number']}` to reuse this Project")
        for name, value in {"project.json": project, **values}.items():
            path = workspace / name
            if not path.exists():
                with path.open("x") as output:
                    output.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
                report["created"].append(str(path))
        operation = (f"Check Project field read access and write access for missing Priority/{ELIGIBILITY_FIELD} creation; "
                     "add Todo/In Progress/Done options to the Project's Status field if missing; review incompatible fields manually. Rerun init to read all fields and reuse any "
                     "field created before a failure; existing fields are never repaired")
        ensure_fields(backend, report)
        if links:
            operation = (f"Check that each --link-repository exists, is visible to you, and that you may link it to Project "
                         f"{owner} #{project['number']} (repository admin/write access). Rerun init to reuse links already made; init never unlinks")
            link_repositories(backend, report, links)
        report["initialized"] = True
        q = shlex.quote
        report["next_commands"] = [
            f"cd {q(str(root))}",
            f"gitweave validate --graph {q(PROJECTS + '/' + key + '/gitweave.json')}",
            f"projectweave validate --graph {q(PROJECTS + '/' + key + '/graph.json')}",
            "# Manual onboarding; built-in auto-add: docs/usage.md#onboard-issues",
            "ISSUE_URL='REPLACE_WITH_OPEN_ISSUE_URL'  # e.g. https://github.com/OWNER/REPO/issues/123",
            f'GH_HOST=github.com gh project item-add {project["number"]} --owner {q(owner)} --url "$ISSUE_URL"',
            f"projectweave run {q(key)}  # one Task; or: projectweave coordinate"
        ]
        report["human_actions"] = ([
            "Claude is configured with permission_mode bypassPermissions: the agents may edit files and run commands in their GitWeave worktrees without asking. Remove it from gitweave.json to restrict Claude (it may then be unable to implement Issues)."]
            if any(node.get("permission_mode") == "bypassPermissions" for node in workers) else []) + ([
            "Claude has no permission_mode on some gitweave.json nodes, so a non-interactive Run may be unable to edit files or run commands there; choose one deliberately."]
            if any(node["provider"] == "claude" and "permission_mode" not in node for node in workers) else []) + [
            "The GitWeave Task graph implements the Issue, opens a pull request whose body says Closes #N, iterates review and fix until the review agent approves, then MERGES it into the default branch with a merge commit and closes the Issue, without a human review. Agents push, open and merge PRs with your gh and Git credentials. Edit gitweave.json first if you want a human to review before merging.",
            "gitweave.json agents run " + "; ".join(choices)
            + ". Install and authenticate that provider CLI yourself. To change provider/model later, edit gitweave.json (init never rewrites it).",
            f"Shared AI resource admission is opt-in: only providers listed for {key} in the root {ROOT_CONFIG} (init-project --resource PROVIDER:MIN:ESTIMATE) are observed (Claude `/usage`, Codex app-server; read-only) and must keep every window at or above min_remaining_percent after reserving estimated_usage_percent_per_task per running Task. Without an entry, {key} is not limited and coordinate may start all its eligible Tasks at once.",
            f"Add an open Issue to the Project using the commands below, or configure GitHub's built-in auto-add for its repository. Then set its {ELIGIBILITY_FIELD} field to {READY} and its Status to Todo manually. Only Todo Tasks are selected. No Issue has been selected or changed.",
            "For one Project per repository, configure one built-in auto-add workflow in each Project. Auto-add handles membership and does not set AI execution. Manage Ready/Not ready manually. See docs/usage.md#onboard-issues. Init does not configure auto-add, create labels, or mark Issues Ready.",
            "ProjectWeave never sets Ready; coordinate selects open, Todo, Ready Issues under the configured resource policy. Set AI execution to Not ready when an Issue is no longer intended for execution, including after completion. Not ready does not stop a running execution. Verify GitHub's Item closed workflow sets Done.",
            "A Run invokes `gitweave run --repo OWNER/REPO --issue N` from this workspace. GitWeave fetches the repository's default branch into its shared per-repository store .gitweave/repos/OWNER/REPO.git here (reused across Runs, so only new objects are fetched) and pushes provenance refs/notes to the repository. Git transport uses your Git credentials: for HTTPS run `gh auth setup-git` or use SSH. Init fetches nothing.",
            "Run readiness is not certified: provider credentials, repository access, push/PR/merge permission, Issue comment permission, Project item-add access and Git identity need human verification. Init never runs AI or pushes.",
            f"claim sets the Task's Status to In Progress before it runs, so it is not selected again, even if the Run then fails. The default path to Done is GitHub's Project workflow when the Issue closes; `projectweave complete` (or a graph complete action) sets Done explicitly. To retry a Task left In Progress, set its Status back to Todo; {ELIGIBILITY_FIELD} is never changed by ProjectWeave; manage Ready/Not ready manually."
        ]
    except (Failure, OSError, UnicodeError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        report["failure"] = {"message": str(exc), "action": operation}
        report["human_actions"].append(operation)
    report["missing"].extend(str(workspace / name) for name in FILES if not (workspace / name).is_file())
    return report
