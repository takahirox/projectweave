"""Built-in, read-only observers of provider subscription usage (Claude, Codex).

Each observer returns the remaining percentage (0-100) or raises Failure; callers
treat any failure as unknown usage. Observers never invoke a model or change state.
"""
import json
import os
import re
import selectors
import signal
import subprocess
import time
from .contracts import Failure, decode, number, require
from .executors import process

TIMEOUT = 60
# "Current session: 11% used · resets …"
LINE = r"^{}: (\d+(?:\.\d+)?)% used\b"
CLAUDE_LIMITS = {"session": "Current session", "week": r"Current week \(all models\)", "fable": r"Current week \(Fable\)"}


def fable(node):
    # A node without model is treated as Fable because the native default model is unknown.
    return "model" not in node or "fable" in str(node["model"]).lower()


def remaining(used):
    require(number(used) and used <= 100, f"Invalid used percentage: {used!r}", "usage")
    return 100 - used


def claude(uses_fable, timeout=TIMEOUT):
    """Smallest remaining percentage of the Claude plan limits that apply."""
    record = decode(process(["claude", "-p", "--output-format", "json", "/usage"], None, timeout))
    require(isinstance(record, dict) and record.get("is_error") is False and isinstance(record.get("result"), str),
            "Unexpected claude /usage output", "usage")
    names = ["session", "week"] + (["fable"] if uses_fable else [])
    values = []
    for name in names:
        match = re.search(LINE.format(CLAUDE_LIMITS[name]), record["result"], re.MULTILINE)
        require(match is not None, f"claude /usage has no '{CLAUDE_LIMITS[name]}' line", "usage")
        values.append(remaining(float(match[1]) if "." in match[1] else int(match[1])))
    return min(values)


def codex(timeout=TIMEOUT):
    """100 - rateLimits.primary.usedPercent from the (experimental) app-server API."""
    requests = [{"method": "initialize", "id": 1, "params": {
                    "clientInfo": {"name": "projectweave", "title": None, "version": "0"}, "capabilities": None}},
                {"method": "initialized"}, {"method": "account/rateLimits/read", "id": 2}]
    try:
        child = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise Failure("usage", f"Cannot launch codex: {exc}") from exc
    try:
        # The server exits on end of input before answering, so keep stdin open and read with a deadline.
        try:
            child.stdin.write("".join(json.dumps(m) + "\n" for m in requests).encode())
            child.stdin.flush()
        except OSError as exc:
            raise Failure("usage", f"codex app-server closed its input: {exc}") from exc
        response = read_response(child.stdout, 2, time.monotonic() + timeout)
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass  # Already gone; macOS reports EPERM for a group of zombies.
        for stream in (child.stdin, child.stdout):
            try:
                stream.close()
            except OSError:
                pass
        child.wait()
    try:
        return remaining(response["result"]["rateLimits"]["primary"]["usedPercent"])
    except (KeyError, TypeError) as exc:
        raise Failure("usage", "codex rate limits have no primary usedPercent") from exc


def read_response(stream, identity, deadline):
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    buffer = b""
    while True:
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            try:
                message = decode(line.decode())
            except (Failure, UnicodeError):
                continue
            # A server-to-client request can also carry an id; only a response has no method.
            if isinstance(message, dict) and message.get("id") == identity and "method" not in message:
                require("error" not in message, f"codex app-server error: {message.get('error')}", "usage")
                return message
        wait = deadline - time.monotonic()
        require(wait > 0 and selector.select(wait), "codex app-server timed out", "usage")
        chunk = os.read(stream.fileno(), 65536)
        require(bool(chunk), "codex app-server exited without a rate limit response", "usage")
        buffer += chunk


def providers(nodes):
    """Map each provider used by GitWeave agent nodes to whether any Claude node runs Fable."""
    used = {}
    for node in nodes:
        provider = node.get("provider")
        used[provider] = used.get(provider, False) or (provider == "claude" and fable(node))
    return used


def observe(nodes):
    """Observe every provider the agent nodes use: {provider: {remaining_percent} | {error}}."""
    used = providers(nodes)
    observations = {}
    if not used:
        return {"(none)": {"error": "No GitWeave agent providers to observe"}}
    for provider, uses_fable in sorted(used.items()):
        try:
            if provider == "claude":
                value = claude(uses_fable)
            elif provider == "codex":
                value = codex()
            else:
                raise Failure("usage", f"No usage observer for provider {provider!r}")
            observations[provider] = {"remaining_percent": value}
        except (Failure, OSError) as exc:
            observations[provider] = {"error": str(exc)}
    return observations
