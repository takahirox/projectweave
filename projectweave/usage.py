"""Built-in, read-only observers of provider subscription usage (Claude, Codex).

Each observer returns the remaining percentage (0-100) per usage window or raises
Failure; callers treat any failure as unknown usage. Observers never invoke a model
or change state.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from .contracts import Failure, decode, number, require

TIMEOUT = 60
CLAUDE_ARGV = ["claude", "-p", "--safe-mode", "--no-session-persistence",
               "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
CLAUDE_ADVICE = ("Use Claude Code 2.1.312 or a compatible version and check `claude auth status` for a "
                 "signed-in Claude.ai subscription with profile scope. Check network access and whether "
                 "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC prevents usage retrieval.")


def remaining(used):
    require(number(used) and used <= 100, f"Invalid used percentage: {used!r}", "usage")
    return 100 - used


def claude(timeout=TIMEOUT):
    """Read the experimental get_usage control API; never send a prompt or start a model turn."""
    child, messages = None, None
    deadline = time.monotonic() + timeout
    try:
        child = subprocess.Popen(CLAUDE_ARGV, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        messages = claude_messages(child.stdout, deadline)
        for identity, request in (("initialize", {"subtype": "initialize"}),
                                  ("usage", {"subtype": "get_usage", "skip_behaviors": True})):
            child.stdin.write((json.dumps({"type": "control_request", "request_id": identity,
                                          "request": request}) + "\n").encode())
            child.stdin.flush()
            for message in messages:
                require(message.get("type") != "result", "CLI returned session cost/token statistics, not plan limits", "usage")
                reply = message.get("response")
                if (message.get("type") == "control_response" and isinstance(reply, dict)
                        and reply.get("request_id") == identity):
                    require(reply.get("subtype") == "success",
                            f"{request['subtype']} control request failed: {str(reply.get('error', 'unsupported response'))[:300]}", "usage")
                    record = reply.get("response")
                    break
        return claude_windows(record)
    except (Failure, OSError) as exc:
        raise Failure("usage", f"Claude subscription limits unavailable: {exc}. {CLAUDE_ADVICE}") from exc
    finally:
        if messages is not None:
            messages.close()
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
            for stream in (child.stdin, child.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            child.wait()


def claude_messages(stream, deadline):
    """Keep one buffer across initialization and usage replies, with a single bounded deadline."""
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        buffer = b""
        while True:
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = decode(line)
                except (Failure, UnicodeError) as exc:
                    raise Failure("usage", "Malformed Claude control JSON") from exc
                require(isinstance(message, dict), "Malformed Claude control message", "usage")
                yield message
            wait = deadline - time.monotonic()
            require(wait > 0 and selector.select(wait), "Claude usage control request timed out", "usage")
            chunk = os.read(stream.fileno(), 65536)
            require(bool(chunk), "Claude CLI exited before returning plan limits (unsupported CLI, authentication, or transport failure)", "usage")
            buffer += chunk


def claude_windows(record):
    """Only server plan utilization is quota data; session costs and extra-usage spend are not."""
    require(isinstance(record, dict) and record.get("rate_limits_available") is True,
            "Plan limits are not available for this authentication (API keys, third-party providers and tokens without profile scope are unsupported)", "usage")
    limits = record.get("rate_limits")
    require(isinstance(limits, dict), "Plan limit retrieval failed; Claude returned no rate_limits", "usage")
    windows = {}

    def add(name, window):
        require(name not in windows, "Duplicate Claude plan window", "usage")
        require(isinstance(window, dict) and number(window.get("utilization")) and window["utilization"] <= 100,
                f"Invalid or missing Claude {name} utilization; expected a percentage from 0 to 100", "usage")
        windows[name] = remaining(window["utilization"])

    for key, name in (("five_hour", "session"), ("seven_day", "week")):
        add(name, limits.get(key))
    # The payload also carries metadata such as seven_day_breakdown, not a quota window.
    for key in ("seven_day_oauth_apps", "seven_day_opus", "seven_day_sonnet"):
        window = limits.get(key)
        if window is not None:
            add(key.removeprefix("seven_day_"), window)
    scoped = limits.get("model_scoped", [])
    require(isinstance(scoped, list), "Invalid Claude model_scoped plan windows", "usage")
    for window in scoped:
        label = window.get("display_name") if isinstance(window, dict) else None
        require(isinstance(label, str) and bool(label.strip()), "Missing Claude model window display_name", "usage")
        name = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        require(bool(name), "Unsupported Claude model window display_name", "usage")
        add(name, window)
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
REFRESH_SECONDS = 300


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


class UsageCache:
    """Shared dashboard observations; only the coordinator collects, HTTP readers take snapshots."""

    def __init__(self, interval=REFRESH_SECONDS, clock=time.monotonic,
                 wall_clock=lambda: datetime.now(timezone.utc)):
        require(interval > 0, "Usage refresh interval must be positive")
        self.interval, self.clock, self.wall_clock = interval, clock, wall_clock
        self.lock = threading.RLock()
        self.providers = ()
        self.values, self.attempts, self.successes = {}, {}, {}

    def configure(self, providers):
        with self.lock:
            self.providers = tuple(dict.fromkeys(providers))

    def record(self, observations):
        with self.lock:
            now = self.clock()
            for provider in self.providers:
                if provider not in observations:
                    continue
                seen = observations[provider]
                value = self.values.setdefault(provider, {"windows": None, "updated_at": None, "error": None})
                self.attempts[provider] = now
                if "error" in seen:
                    value["error"] = str(seen["error"])
                else:
                    value.update(windows=deepcopy(seen["windows"]), updated_at=self.wall_clock().isoformat(), error=None)
                    self.successes[provider] = now

    def collect(self, providers, observer):
        """Publish policy observations too, preserving fresh failures for admission decisions."""
        try:
            observations = observer(providers)
        except Exception as exc:
            observations = {provider: {"error": str(exc)} for provider in providers}
        self.record(observations)
        return observations

    def refresh(self, observer=observe, force=False, observed=()):
        with self.lock:
            now = self.clock()
            providers = {provider for provider in self.providers if provider not in observed and
                         (force or now - self.attempts.get(provider, float("-inf")) >= self.interval)}
        if providers:
            self.collect(providers, observer)

    def next_refresh(self):
        with self.lock:
            return min((self.attempts.get(provider, float("-inf")) + self.interval
                        for provider in self.providers), default=float("inf"))

    def snapshot(self):
        with self.lock:
            entries = []
            for provider in self.providers:
                value = deepcopy(self.values.get(provider, {"windows": None, "updated_at": None, "error": None}))
                stale = value["error"] is not None or self.clock() - self.successes.get(provider, float("-inf")) >= self.interval
                status = "unavailable" if value["windows"] is None else "stale" if stale else "current"
                entries.append(dict(value, provider=provider, status=status))
            return entries
