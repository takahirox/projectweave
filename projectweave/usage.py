"""Built-in, read-only observers of provider subscription usage (Claude, Codex).

Each observer returns the remaining percentage (0-100) per usage window or raises
Failure; callers treat any failure as unknown usage. Observers never invoke a model
or change state.
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


def remaining(used):
    require(number(used) and used <= 100, f"Invalid used percentage: {used!r}", "usage")
    return 100 - used


def claude(timeout=TIMEOUT):
    """Remaining percentage of every Claude plan window /usage reports (session and weekly limits)."""
    record = decode(process(["claude", "-p", "--output-format", "json", "/usage"], None, timeout))
    require(isinstance(record, dict) and record.get("is_error") is False and isinstance(record.get("result"), str),
            "Unexpected claude /usage output", "usage")
    windows = {}
    for name, label in CLAUDE_LIMITS.items():
        match = re.search(LINE.format(label), record["result"], re.MULTILINE)
        if match is not None:
            windows[name] = remaining(float(match[1]) if "." in match[1] else int(match[1]))
    # The session and all-models weekly windows must be present; a model-specific window is optional.
    for name in ("session", "week"):
        require(name in windows, f"claude /usage has no '{CLAUDE_LIMITS[name]}' line", "usage")
    return windows


def codex(timeout=TIMEOUT):
    """{"primary": 100 - rateLimits.primary.usedPercent} from the (experimental) app-server API."""
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
        return {"primary": remaining(response["result"]["rateLimits"]["primary"]["usedPercent"])}
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


OBSERVERS = {"claude": claude, "codex": codex}


def observe(providers):
    """{provider: {"windows": {name: remaining_percent}} | {"error": reason}} for each named provider."""
    observations = {}
    for provider in sorted(providers):
        try:
            require(provider in OBSERVERS, f"No usage observer for provider {provider!r}", "usage")
            observations[provider] = {"windows": OBSERVERS[provider]()}
        except (Failure, OSError) as exc:
            observations[provider] = {"error": str(exc)}
    return observations
