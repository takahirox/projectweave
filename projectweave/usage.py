"""Built-in, read-only observers of provider subscription usage (Claude, Codex).

Observers return measurements and, for Claude, applicability/unknown metadata.
Retrieval failures raise Failure. Observers never invoke a model or change state.
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
                 "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC prevents usage retrieval. "
                 "Compare missing plan windows with Claude Code's interactive `/usage`; if they remain "
                 "absent, report the missing window and CLI version to Claude Code. "
                 "Partial plan data cannot establish all required limits.")


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
                            f"{request['subtype']} control request failed (authentication, transport, or unsupported request)", "usage")
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
    """Normalize current server rows and legacy projections without guessing absent utilization.

    A non-null limits array enumerates applicable meters. is_active selects the
    headline meter, not applicability. Without rows, legacy base windows remain
    required. Keep useful measurements when another required meter is unknown.
    """
    require(isinstance(record, dict), "Malformed Claude get_usage response; expected an object", "usage")
    require(not any(record.get(key) for key in ("error", "errors", "rate_limits_error")),
            "Claude get_usage reported a usage retrieval error", "usage")
    require(record.get("rate_limits_available") is not False,
            "Plan limits are not available for this authentication (API keys, third-party providers and tokens without profile scope are unsupported)", "usage")
    require(record.get("rate_limits_available") is True,
            "Claude rate_limits_available is missing or invalid; plan usage availability is unknown", "usage")
    limits = record.get("rate_limits")
    require(isinstance(limits, dict),
            f"Plan limit retrieval failed: Claude rate_limits is {claude_field_type(record, 'rate_limits')} "
            "despite rate_limits_available=true; expected an object", "usage")
    windows, details, unknown = {}, {}, {}
    rows = limits.get("limits")
    enumerated = isinstance(rows, list)

    def scoped_name(label, surface=False):
        require(isinstance(label, str) and 0 < len(label.strip()) <= 120,
                "Invalid Claude scoped window display_name", "usage")
        name = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        require(bool(name), "Unsupported Claude scoped window display_name", "usage")
        return ("surface_" if surface else "model_" if name in ("session", "week", "primary") else "") + name

    def add(name, window, path, *, field="utilization", label=None, active=None):
        try:
            require(isinstance(window, dict),
                    f"Claude {path} is {claude_field_type({'value': window}, 'value')}; expected a plan window object", "usage")
            require(number(window.get(field)) and window[field] <= 100,
                    f"Claude {path}.{field} is {claude_field_type(window, field)}; "
                    "expected a finite percentage from 0 to 100", "usage")
            reset = window.get("resets_at")
            if reset is not None:
                try:
                    require(isinstance(reset, str) and datetime.fromisoformat(reset.replace("Z", "+00:00")).tzinfo is not None,
                            f"Invalid Claude {path}.resets_at timestamp", "usage")
                    reset = datetime.fromisoformat(reset.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
                except ValueError:
                    raise Failure("usage", f"Invalid Claude {path}.resets_at timestamp") from None
            value = remaining(window[field])
            previous = details.get(name)
            if previous:
                require(windows[name] == value and previous["label"].casefold() == label.casefold()
                        and (not reset or not previous["resets_at"] or reset == previous["resets_at"]),
                        "Conflicting Claude equivalent plan windows", "usage")
                previous["resets_at"] = previous["resets_at"] or reset
            else:
                windows[name] = value
                details[name] = {"label": label, "resets_at": reset, "is_active": active}
        except Failure as exc:
            unknown[name] = str(exc)

    if rows is not None and not enumerated:
        unknown["limits"] = "Invalid Claude rate_limits.limits; expected an array or null"
    if enumerated:
        for index, row in enumerate(rows):
            path, name = f"rate_limits.limits[{index}]", f"row_{index}"
            try:
                require(isinstance(row, dict), f"Invalid Claude {path}; expected an object", "usage")
                kind, group, scope = row.get("kind"), row.get("group"), row.get("scope")
                unscoped = scope is None or (isinstance(scope, dict) and
                                             all(value is None for value in scope.values()))
                require(type(row.get("is_active")) is bool, f"Invalid Claude {path}.is_active", "usage")
                if kind == "session" and group == "session" and unscoped:
                    name, label = "session", "Current session"
                elif kind == "weekly_all" and group == "weekly" and unscoped:
                    name, label = "week", "Current week (all models)"
                elif kind == "weekly_scoped" and group == "weekly" and isinstance(scope, dict):
                    targets = [key for key in ("model", "surface") if scope.get(key) is not None]
                    require(len(targets) == 1 and isinstance(scope[targets[0]], dict),
                            f"Invalid Claude {path}.scope", "usage")
                    target = targets[0]
                    display = scope[target].get("display_name")
                    name = scoped_name(display, target == "surface")
                    label = f"Current week ({display})"
                else:
                    raise Failure("usage", f"Unsupported Claude {path} kind/group/scope; applicability unknown")
                add(name, row, path, field="percent", label=label, active=row["is_active"])
            except Failure as exc:
                unknown[name] = str(exc)

    for key, name, label in (("five_hour", "session", "Current session"),
                             ("seven_day", "week", "Current week (all models)"),
                             ("seven_day_oauth_apps", "oauth_apps", "Current week (OAuth apps)"),
                             ("seven_day_opus", "opus", "Current week (Opus)"),
                             ("seven_day_sonnet", "sonnet", "Current week (Sonnet)")):
        window = limits.get(key)
        if window is not None:
            add(name, window, f"rate_limits.{key}", label=label)
        elif not enumerated and name in ("session", "week"):
            unknown[name] = (f"Claude rate_limits.{key} is {claude_field_type(limits, key)}; "
                             "applicability and measurement are unknown without server rows")
    scoped = limits.get("model_scoped", [])
    if not isinstance(scoped, list):
        unknown["model_scoped"] = "Invalid Claude model_scoped plan windows"
    else:
        for index, window in enumerate(scoped):
            name, path = f"model_{index}", f"rate_limits.model_scoped[{index}]"
            try:
                display = window.get("display_name") if isinstance(window, dict) else None
                name = scoped_name(display)
                add(name, window, path, label=f"Current week ({display})")
            except Failure as exc:
                unknown[name] = str(exc)
    # A bad/conflicting copy cannot be repaired by a later equivalent projection.
    for name in unknown:
        windows.pop(name, None)
        details.pop(name, None)
    if not windows and not unknown:
        unknown["plan"] = "Claude returned no applicable plan measurements"
    absent = [name for name in ("session", "week") if enumerated and
              not any(key.startswith("row_") for key in unknown) and name not in windows and name not in unknown]
    return {"windows": windows, "window_details": details, "unknown": unknown, "absent": absent}


def claude_field_type(record, key):
    """Describe only a known schema field's presence/type, never its account-provided value."""
    if key not in record:
        return "missing"
    value = record[key]
    if value is None:
        return "null"
    return {dict: "an object", list: "an array", bool: "a boolean", str: "a string",
            int: "a number", float: "a number"}.get(type(value), "an unsupported type")


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


def complete(observation):
    """Only fresh, nonempty measurements with no unresolved limits can admit work."""
    windows = observation.get("windows")
    return ("error" not in observation and not observation.get("unknown") and
            isinstance(windows, dict) and bool(windows) and
            all(number(value) and value <= 100 for value in windows.values()))


def observe(providers):
    """Readings may contain windows plus unknown/applicability metadata, or a retrieval error."""
    observations = {}
    for provider in sorted(providers):
        try:
            require(provider in OBSERVERS, f"No usage observer for provider {provider!r}", "usage")
            reading = OBSERVERS[provider]()
            observations[provider] = reading if "windows" in reading else {"windows": reading}
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
        self.readings = {}

    @staticmethod
    def empty():
        return {"windows": None, "updated_at": None, "last_success_at": None,
                "attempted_at": None, "error": None, "unknown": {}, "absent": [], "window_details": {}}

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
                value = self.values.setdefault(provider, self.empty())
                self.attempts[provider] = now
                timestamp = self.wall_clock().isoformat()
                value["attempted_at"] = timestamp
                if "error" in seen:
                    value["error"] = str(seen["error"])
                else:
                    value.update(windows=deepcopy(seen["windows"]), updated_at=timestamp if seen["windows"] else None, error=None,
                                 unknown=deepcopy(seen.get("unknown", {})), absent=deepcopy(seen.get("absent", [])),
                                 window_details=deepcopy(seen.get("window_details", {})))
                    self.readings[provider] = now
                    if complete(seen):
                        value["last_success_at"] = timestamp
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
                value = deepcopy(self.values.get(provider, self.empty()))
                stale = value["error"] is not None or self.clock() - self.readings.get(provider, float("-inf")) >= self.interval
                status = ("unavailable" if not value["windows"] else "stale" if stale else
                          "partial" if value["unknown"] else "current")
                entries.append(dict(value, provider=provider, status=status))
            return entries
