"""Deterministic label routing for the generated GitWeave Task graph."""
from .contracts import require, text
from .readiness import Issue

# Ordered exact matches after normalization; first match wins. Extend alongside
# the generated graph's branches and route schema when adding another route.
LABEL_ROUTES = (("bug", "bug"),)
DEFAULT_ROUTE = "default"


def routing(issue):
    require(isinstance(issue, dict) and "pull_request" not in issue
            and issue.get("state") in ("open", "closed"), "Malformed GitHub Issue", "github")
    labels = issue.get("labels", [])
    require(isinstance(labels, list), "Malformed GitHub Issue labels", "github")
    normalized = []
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        require(text(name), "Malformed GitHub Issue label", "github")
        normalized.append(name.strip().casefold())
    labels = sorted(set(normalized))
    route = next((route for label, route in LABEL_ROUTES if label in labels), DEFAULT_ROUTE)
    return {"route": route, "labels": labels}


def execute(context):
    require(isinstance(context, dict), "Routing context must be an object", "input")
    issue = Issue(context)
    data = routing(issue.api(issue.endpoint))
    return {"message": f"Issue label route: {data['route']}", "data": data}
