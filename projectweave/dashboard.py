"""Read-only localhost HTTP dashboard owned by the coordinator process."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import unquote, urlsplit

ASSETS = Path(__file__).with_name("web")


class Dashboard:
    def __init__(self, registry, port=8765):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                assets = {"/": ("dashboard.html", "text/html; charset=utf-8"),
                          "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
                          "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8")}
                if path in assets:
                    filename, content_type = assets[path]
                    self.respond(200, (ASSETS / filename).read_bytes(), content_type)
                    return
                if path.startswith("/api/"):
                    snapshot = registry.snapshot()
                    value = None
                    if path == "/api/state":
                        value = snapshot
                    elif path == "/api/projects":
                        value = snapshot["projects"]
                    elif path.startswith("/api/projects/"):
                        name = unquote(path[len("/api/projects/"):])
                        project = next((project for project in snapshot["projects"] if project["name"] == name), None)
                        if project:
                            value = {"project": project, "runs": [run for run in snapshot["runs"] if run["project"] == name]}
                    elif path.startswith("/api/runs/"):
                        identity = unquote(path[len("/api/runs/"):])
                        value = next((run for run in snapshot["runs"] if run["id"] == identity), None)
                    if value is not None:
                        self.respond(200, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                                     "application/json; charset=utf-8")
                        return
                self.respond(404, b'{"error":"Not found"}', "application/json; charset=utf-8")

            def respond(self, status, body, content_type):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="projectweave-dashboard", daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}/"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
