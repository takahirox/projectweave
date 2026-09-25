"""GitHub Projects v2 via authenticated gh GraphQL, with cursor pagination."""
import json
import re
from .contracts import Failure, keys, require, text, decode, result, check_result
from .executors import process

PAGE = "pageInfo { hasNextPage endCursor }"
# Standard Project-native eligibility switch; deliberately not configurable.
ELIGIBILITY_FIELD = "AI execution"
ELIGIBILITY_OPTIONS = ["Ready", "Not ready"]
READY = "Ready"


def validate_project(config):
    require(not isinstance(config, dict) or "label" not in config,
            f"Label eligibility was removed: delete \"label\" and set the Project field "
            f"\"{ELIGIBILITY_FIELD}\" to {READY} instead")
    keys(config, {"owner", "number", "owner_type", "priority_field", "status_field",
                  "priority_order", "eligible_statuses", "repository"}, {"owner", "number", "owner_type"})
    require(text(config["owner"]) and re.fullmatch(r"[A-Za-z0-9_-]+", config["owner"]), "Invalid project owner")
    require(type(config["number"]) is int and config["number"] > 0, "Invalid project number")
    require(config["owner_type"] in ("user", "organization"), "Invalid owner_type")
    if "repository" in config:
        require(text(config["repository"]) and re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", config["repository"])
                and config["repository"].split("/")[1] not in (".", ".."), "Invalid repository")
    for field in ("priority_field", "status_field"):
        require(field not in config or text(config[field]), f"Invalid {field}")
    for field in ("priority_order", "eligible_statuses"):
        if field in config:
            values = config[field]
            require(isinstance(values, list) and all(text(v) for v in values)
                    and len(set(values)) == len(values), f"Invalid {field}")
    return config


class GitHub:
    def __init__(self, config):
        self.config = validate_project(config)
        self.project_id = None

    def query(self, query, variables):
        try:
            response = decode(process(["gh", "api", "graphql", "--hostname", "github.com", "--input", "-"],
                                      json.dumps({"query": query, "variables": variables}), 120))
            if not isinstance(response, dict) or response.get("errors") or not isinstance(response.get("data"), dict):
                raise Failure("github", "GitHub GraphQL request failed", {"errors": response.get("errors") if isinstance(response, dict) else None})
            return response["data"]
        except Failure as exc:
            raise Failure("github", str(exc), exc.details) from exc

    def resolve(self):
        if self.project_id:
            return self.project_id
        kind = self.config["owner_type"]
        data = self.query(f"query($owner:String!,$number:Int!) {{ {kind}(login:$owner) {{ projectV2(number:$number) {{ id }} }} }}",
                          {"owner": self.config["owner"], "number": self.config["number"]})
        try:
            self.project_id = data[kind]["projectV2"]["id"]
            require(text(self.project_id), "Project not found", "github")
        except (KeyError, TypeError, AttributeError) as exc:
            raise Failure("github", "Project not found or inaccessible") from exc
        return self.project_id

    def pages(self, identity, typename, field, selection):
        cursor = None
        seen = set()
        query = (f"query($id:ID!,$cursor:String) {{ node(id:$id) {{ ... on {typename} {{ "
                 f"{field}(first:100,after:$cursor) {{ nodes {{ {selection} }} {PAGE} }} }} }} }}")
        while True:
            data = self.query(query, {"id": identity, "cursor": cursor})
            try:
                connection = data["node"][field]
                nodes, page = connection["nodes"], connection["pageInfo"]
                require(isinstance(nodes, list) and type(page["hasNextPage"]) is bool,
                        "Malformed GitHub connection", "github")
                yield from (node for node in nodes if node is not None)
                if not page["hasNextPage"]:
                    return
                cursor = page["endCursor"]
                require(text(cursor) and cursor not in seen, "Invalid or repeated GitHub cursor", "github")
                seen.add(cursor)
            except (KeyError, TypeError, AttributeError) as exc:
                raise Failure("github", f"Malformed {field} response") from exc

    def load(self):
        project_id = self.resolve()
        selection = """id isArchived content { __typename ... on Issue {
          id number title body url state createdAt repository { nameWithOwner }
        } }"""
        tasks = []
        try:
            for item in self.pages(project_id, "ProjectV2", "items", selection):
                issue = item.get("content")
                if item["isArchived"] or not issue or issue.get("__typename") != "Issue" or issue["state"] != "OPEN":
                    continue
                labels = [v["name"] for v in self.pages(issue["id"], "Issue", "labels", "name")]
                values = self.pages(item["id"], "ProjectV2Item", "fieldValues",
                                    "... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }")
                fields = {v["field"]["name"]: v["name"] for v in values if "field" in v}
                tasks.append({"id": issue["id"], "item_id": item["id"], "project_id": project_id,
                              "number": issue["number"], "title": issue["title"], "body": issue["body"],
                              "url": issue["url"], "created_at": issue["createdAt"], "state": issue["state"],
                              "repository": issue["repository"]["nameWithOwner"], "labels": labels,
                              "priority": fields.get(self.config.get("priority_field", "Priority")),
                              "status": fields.get(self.config.get("status_field", "Status")),
                              "ai_execution": fields.get(ELIGIBILITY_FIELD)})
        except (KeyError, TypeError, AttributeError) as exc:
            raise Failure("github", "Malformed project item") from exc
        return tasks

    def select(self, tasks):
        require(isinstance(tasks, list), "select items must be an array", "input")
        priorities = self.config.get("priority_order", ["P0", "P1", "P2"])
        ranks = {name: i for i, name in enumerate(priorities)}
        try:
            eligible = [t for t in tasks if t["state"] == "OPEN"
                        and ("repository" not in self.config or
                             t["repository"].lower() == self.config["repository"].lower())
                        and t["ai_execution"] == READY
                        and ("eligible_statuses" not in self.config or t["status"] in self.config["eligible_statuses"])]
            return min(eligible, key=lambda t: (ranks.get(t["priority"], len(ranks)),
                                               t["created_at"], t["url"], t["item_id"]), default=None)
        except (KeyError, TypeError, AttributeError) as exc:
            raise Failure("input", "Invalid task selection input") from exc

    def check_task(self, task, kind):
        require(isinstance(task, dict) and all(text(task.get(k)) for k in ("id", "item_id", "project_id")),
                f"{kind} requires a selected task", "input")
        require(task["project_id"] == self.resolve(), "Task belongs to a different project", "input")

    def status_option(self, status, kind):
        """Resolve a Status option to (field id, option id) before any mutation."""
        try:
            fields = list(self.pages(self.resolve(), "ProjectV2", "fields",
                                    "... on ProjectV2SingleSelectField { id name options { id name } }"))
            matches = [f for f in fields if f.get("name") == self.config.get("status_field", "Status")]
            require(len(matches) == 1, "Status field missing or ambiguous", kind)
            options = [v for v in matches[0]["options"] if v["name"] == status]
            require(len(options) == 1, f"Unknown Status option: {status}", kind)
            return matches[0]["id"], options[0]["id"]
        except (KeyError, TypeError, AttributeError) as exc:
            raise Failure(kind, "Malformed Status field metadata") from exc

    def update_status(self, task, option):
        response = self.query("""mutation($project:ID!,$item:ID!,$field:ID!,$option:String!) {
          updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,fieldId:$field,
            value:{singleSelectOptionId:$option}}) { projectV2Item { id } } }""",
                              {"project": self.resolve(), "item": task["item_id"], "field": option[0], "option": option[1]})
        require(response["updateProjectV2ItemFieldValue"]["projectV2Item"]["id"] == task["item_id"],
                "Missing updated item", "writeback")

    def set_status(self, task, status):
        """Mark the selected Task (for example In Progress before execution) without commenting."""
        self.check_task(task, "status")
        option = self.status_option(status, "status")
        try:
            self.update_status(task, option)
        except (Failure, KeyError, TypeError) as exc:
            raise Failure("status", f"Status update failed; it may have been applied: {exc}") from exc
        return result(f"Status set to {status}", {"status": status})

    def writeback(self, task, outcome, run_id, status=None):
        check_result(outcome)
        self.check_task(task, "writeback")
        option = self.status_option(status, "writeback") if status is not None else None
        completed = []
        try:
            body = f"ProjectWeave Run `{run_id}`\n\n" + json.dumps(outcome, ensure_ascii=False, indent=2)
            response = self.query("mutation($id:ID!,$body:String!) { addComment(input:{subjectId:$id,body:$body}) { commentEdge { node { url } } } }",
                                  {"id": task["id"], "body": body})
            url = response["addComment"]["commentEdge"]["node"]["url"]
            require(text(url), "Missing comment URL", "writeback")
            completed.append(url)
            if option:
                self.update_status(task, option)
        except (Failure, KeyError, TypeError) as exc:
            raise Failure("writeback", f"Writeback failed; remote effects may have occurred: {exc}",
                          {"completed_references": completed}) from exc
        return result("Execution result posted", {"status": status}, completed)
