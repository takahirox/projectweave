import copy
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from projectweave import usage
from projectweave.contracts import Failure
from projectweave.coordinator import Admission

ROOT = Path(__file__).resolve().parents[1]


class UsageTests(unittest.TestCase):
    """Built-in observers against fake claude/codex executables; no account or model is touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        for name in ("claude", "codex"):
            path = directory / name
            path.write_text(f"#!{sys.executable}\n" + (ROOT / "tests/fake_cli.py").read_text())
            path.chmod(0o755)
        env = {"PATH": str(directory) + os.pathsep + os.environ["PATH"], "FAKE_LOG": str(directory / "log")}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, **values):
        patcher = patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_claude_reports_every_window(self):
        self.env(FAKE_CLAUDE_USED="10,20,90")
        self.assertEqual(usage.claude()["windows"], {"session": 90, "week": 80, "fable": 10})

    def test_claude_failures_are_unknown(self):
        for mode in ("claude_failure", "claude_unsupported", "claude_fetch_failure",
                     "claude_exit", "claude_hang", "claude_init_error", "claude_unsupported_request", "claude_malformed"):
            with self.subTest(mode=mode):
                self.env(FAKE_USAGE=mode)
                with self.assertRaisesRegex(Failure, "Claude subscription limits unavailable") as caught:
                    usage.claude(timeout=0.2)
                self.assertIn("claude auth status", str(caught.exception))
                self.assertIn("2.1.312", str(caught.exception))

    def test_claude_process_and_pipes_are_closed_on_success_and_failure(self):
        launch = subprocess.Popen
        for mode in ("", "claude_hang", "claude_malformed"):
            with self.subTest(mode=mode):
                children = []

                def spawn(*args, **kwargs):
                    child = launch(*args, **kwargs)
                    children.append(child)
                    return child

                self.env(FAKE_USAGE=mode)
                with patch("projectweave.usage.subprocess.Popen", side_effect=spawn):
                    if mode:
                        with self.assertRaises(Failure):
                            usage.claude(timeout=0.2)
                    else:
                        usage.claude()
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].poll())
                self.assertTrue(children[0].stdin.closed)
                self.assertTrue(children[0].stdout.closed)

    def test_claude_structured_limits_and_control_only_protocol(self):
        self.env(FAKE_CLAUDE_DATA=(ROOT / "tests/fixtures/claude-usage.json").read_text())
        self.assertEqual(usage.claude()["windows"], {"session": 90, "week": 80, "opus": 64.5, "fable": 10})
        calls = [json.loads(line) for line in Path(os.environ["FAKE_LOG"]).read_text().splitlines()]
        self.assertEqual(calls[0]["argv"], usage.CLAUDE_ARGV[1:])
        self.assertEqual([c["request"] for c in calls[1:]], [
            {"subtype": "initialize"}, {"subtype": "get_usage", "skip_behaviors": True}])

    def test_successful_print_mode_cost_fixture_is_unknown_not_unused_allowance(self):
        self.env(FAKE_USAGE="claude_cost_only",
                 FAKE_CLAUDE_DATA=(ROOT / "tests/fixtures/claude-cost-only.json").read_text())
        observed = usage.observe(["claude"])["claude"]
        self.assertNotIn("windows", observed)
        self.assertIn("session cost/token statistics, not plan limits", observed["error"])
        self.assertIn("claude auth status", observed["error"])

    def test_observed_weekly_only_response_and_control_only_protocol(self):
        self.env(FAKE_CLAUDE_DATA=(ROOT / "tests/fixtures/claude-usage-null-session.json").read_text())
        observed = usage.observe(["claude"])["claude"]
        self.assertEqual(observed["windows"], {"week": 75, "fable": 100})
        self.assertTrue(usage.complete(observed))
        self.assertEqual(observed["absent"], ["session"])
        self.assertFalse(observed["window_details"]["fable"]["is_active"])
        self.assertEqual(observed["window_details"]["week"]["label"], "Current week (all models)")
        controls = [json.loads(line) for line in Path(os.environ["FAKE_LOG"]).read_text().splitlines()]
        self.assertEqual([c["request"] for c in controls[1:]], [
            {"subtype": "initialize"}, {"subtype": "get_usage", "skip_behaviors": True}])

    def test_required_plan_field_diagnostics_distinguish_missing_null_and_malformed(self):
        valid = json.loads((ROOT / "tests/fixtures/claude-usage.json").read_text())
        for key in ("five_hour", "seven_day"):
            for state, value in (("missing", None), ("null", None), ("a string", "private-account-value"),
                                 ("an array", []), ("a boolean", False)):
                with self.subTest(key=key, state=state):
                    record = copy.deepcopy(valid)
                    if state == "missing":
                        del record["rate_limits"][key]
                    else:
                        record["rate_limits"][key] = value
                    observed = usage.claude_windows(record)
                    self.assertFalse(usage.complete(observed))
                    name = {"five_hour": "session", "seven_day": "week"}[key]
                    self.assertNotIn(name, observed["windows"])
                    self.assertIn(f"rate_limits.{key} is {state}", observed["unknown"][name])
                    self.assertNotIn("private-account-value", json.dumps(observed))
            for state, value in (("missing", {}), ("null", {"utilization": None}),
                                 ("a string", {"utilization": "private-account-value"}),
                                 ("a boolean", {"utilization": True}), ("a number", {"utilization": 101})):
                with self.subTest(key=key, utilization=state):
                    record = copy.deepcopy(valid)
                    record["rate_limits"][key] = value
                    observed = usage.claude_windows(record)
                    reason = observed["unknown"][{"five_hour": "session", "seven_day": "week"}[key]]
                    self.assertIn(f"rate_limits.{key}.utilization is {state}", reason)
                    self.assertIn("finite percentage from 0 to 100", reason)
                    self.assertNotIn("private-account-value", reason)
        for state, value in (("missing", None), ("null", None), ("an array", [])):
            record = copy.deepcopy(valid)
            if state == "missing":
                del record["rate_limits"]
            else:
                record["rate_limits"] = value
            with self.subTest(rate_limits=state), self.assertRaisesRegex(
                    Failure, f"Plan limit retrieval failed: Claude rate_limits is {state}"):
                usage.claude_windows(record)
        for record, reason in (([], "expected an object"), ({}, "availability is unknown"),
                               ({"rate_limits_available": None}, "availability is unknown"),
                               ({"rate_limits_available": False}, "not available for this authentication")):
            with self.subTest(record=record), self.assertRaisesRegex(Failure, reason):
                usage.claude_windows(record)

    def test_malformed_measurements_preserve_other_windows_but_never_admit(self):
        valid = json.loads((ROOT / "tests/fixtures/claude-usage.json").read_text())
        invalid = [None, [], {}, {"rate_limits_available": False},
                   {"rate_limits_available": "true", "rate_limits": valid["rate_limits"]},
                   {"rate_limits_available": True, "rate_limits": None}]
        for key in ("five_hour", "seven_day", "seven_day_opus"):
            for value in ({}, [], "25", {"utilization": None}, {"utilization": True},
                          {"utilization": "0"}, {"utilization": -1}, {"utilization": 101}):
                record = copy.deepcopy(valid)
                record["rate_limits"][key] = value
                invalid.append(record)
        for value in (None, {}, [None], [{}], [{"display_name": "Fable"}],
                      [{"display_name": "", "utilization": 0}],
                      [{"display_name": "Fable", "utilization": 0}, {"display_name": "Fable", "utilization": 1}]):
            record = copy.deepcopy(valid)
            record["rate_limits"]["model_scoped"] = value
            invalid.append(record)
        for record in invalid:
            with self.subTest(record=record):
                self.env(FAKE_CLAUDE_DATA=json.dumps(record))
                observed = usage.observe(["claude"])["claude"]
                self.assertFalse(usage.complete(observed))
                self.assertTrue(observed.get("error") or observed.get("unknown"))
        # Zero is a real quota measurement only when reported as plan utilization.
        self.env(FAKE_CLAUDE_DATA=json.dumps({"rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": 0}, "seven_day": {"utilization": 100}}}))
        self.assertEqual(usage.claude()["windows"], {"session": 100, "week": 0})

    def test_invalid_numbers_and_missing_cli_are_actionable(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.env(FAKE_CLAUDE_DATA=json.dumps({"rate_limits_available": True, "rate_limits": {
                    "five_hour": {"utilization": value}, "seven_day": {"utilization": 0}}}))
                observed = usage.observe(["claude"])["claude"]
                self.assertFalse(usage.complete(observed))
                self.assertTrue(observed.get("error") or observed.get("unknown"))
        with patch("projectweave.usage.subprocess.Popen", side_effect=FileNotFoundError("claude")):
            self.assertIn("claude auth status", usage.observe(["claude"])["claude"]["error"])

    def test_failed_claude_observation_keeps_dashboard_success_but_blocks_admission(self):
        cache = usage.UsageCache()
        cache.configure(["claude"])
        admission = Admission({"app": {"claude": {
            "min_remaining_percent": 0, "estimated_usage_percent_per_task": 0}}},
            observe=lambda providers: cache.collect(providers, usage.observe))
        admission.refresh(["app"])
        self.assertTrue(admission.admits("app")[0])
        first = cache.snapshot()[0]
        self.assertEqual(first["status"], "current")
        for mode in ("claude_failure", "claude_fetch_failure", "claude_cost_only"):
            with self.subTest(mode=mode):
                fixture = {"claude_cost_only": "claude-cost-only.json", "null_session": "claude-usage-null-session.json"}.get(mode)
                self.env(FAKE_USAGE=mode, FAKE_CLAUDE_DATA=(
                    (ROOT / "tests/fixtures" / fixture).read_text() if fixture else ""))
                admission.refresh(["app"])
                admitted, reason = admission.admits("app")
                self.assertFalse(admitted)
                self.assertIn("usage unknown", reason)
                stale = cache.snapshot()[0]
                self.assertEqual(stale["windows"], first["windows"])
                self.assertEqual(stale["updated_at"], first["updated_at"])
                self.assertEqual(stale["status"], "stale")
                self.assertIn("unavailable", stale["error"])
                unavailable = usage.UsageCache()
                unavailable.configure(["claude"])
                unavailable.record(admission.observations)
                self.assertEqual(unavailable.snapshot()[0]["status"], "unavailable")
                self.assertIsNone(unavailable.snapshot()[0]["updated_at"])
                self.assertIsNone(unavailable.snapshot()[0]["windows"])

    def test_codex_primary_window(self):
        self.env(FAKE_CODEX_USED="95")
        self.assertEqual(usage.codex(), {"primary": 5})
        self.env(FAKE_USAGE="codex_server_request")
        self.assertEqual(usage.codex(), {"primary": 5})  # The server's own request with id 2 is skipped.

    def test_codex_failures_are_unknown(self):
        for mode in ("codex_error", "codex_no_primary", "codex_hang", "codex_exit"):
            with self.subTest(mode=mode):
                self.env(FAKE_USAGE=mode)
                with self.assertRaises(Failure):
                    usage.codex(timeout=1)

    def test_observe_named_providers(self):
        self.env(FAKE_CLAUDE_USED="10,20,90", FAKE_CODEX_USED="40")
        observed = usage.observe({"codex", "claude"})
        self.assertEqual(observed["claude"]["windows"], {"session": 90, "week": 80, "fable": 10})
        self.assertTrue(usage.complete(observed["claude"]))
        self.assertEqual(observed["codex"], {"windows": {"primary": 60}})
        self.env(FAKE_USAGE="codex_exit")
        observed = usage.observe(["codex", "claude"])
        self.assertIn("exited without a rate limit response", observed["codex"]["error"])  # Real reason recorded.
        self.assertIn("windows", observed["claude"])  # Other providers are unaffected.
        self.assertIsInstance(observed["claude"]["windows"]["week"], int)
        self.assertIn("No usage observer", usage.observe(["gemini"])["gemini"]["error"])
        self.assertEqual(usage.observe([]), {})


class ClaudeWindowTests(unittest.TestCase):
    """Synthetic server rows exercise applicability independently of account access."""

    def row(self, kind="weekly_all", percent=37, active=True, scope=None):
        return {"kind": kind, "group": "session" if kind == "session" else "weekly",
                "percent": percent, "is_active": active, "scope": scope,
                "resets_at": "2030-02-03T04:05:06Z", "severity": "normal"}

    def read(self, rows, **legacy):
        return usage.claude_windows({"rate_limits_available": True,
                                    "rate_limits": {"limits": rows, **legacy}})

    def admits(self, seen, minimum=0, estimate=0):
        admission = Admission({"app": {"claude": {"min_remaining_percent": minimum,
                                                   "estimated_usage_percent_per_task": estimate}}},
                              observe=lambda providers: {"claude": seen})
        admission.refresh(["app"])
        return admission.admits("app")[0]

    def test_enumerated_week_only_and_legacy_fallback_have_different_applicability(self):
        week = {"utilization": 30}
        seen = self.read([self.row(percent=30)], five_hour=None, seven_day=week)
        self.assertEqual(seen["windows"], {"week": 70})
        self.assertEqual(seen["absent"], ["session"])
        self.assertTrue(self.admits(seen, minimum=60, estimate=10))
        self.assertFalse(self.admits(seen, minimum=60, estimate=11))
        for rows in (None, "invalid"):
            seen = self.read(rows, five_hour=None, seven_day=week)
            self.assertEqual(seen["windows"], {"week": 70})
            self.assertIn("session", seen["unknown"])
            self.assertFalse(self.admits(seen))
            self.assertEqual(seen["absent"], [])
        for rows in ([], None):
            seen = self.read(rows)
            self.assertFalse(self.admits(seen))  # No vacuous success on an empty measurement set.

    def test_session_and_scoped_rows_are_deduplicated_and_inactive_rows_constrain(self):
        scoped = self.row("weekly_scoped", 83, False, {"model": {"display_name": "Fable"}})
        seen = self.read([self.row("session", 12), self.row(), scoped],
                         five_hour={"utilization": 12}, seven_day={"utilization": 37},
                         model_scoped=[{"display_name": "Fable", "utilization": 83}])
        self.assertEqual(seen["windows"], {"session": 88, "week": 63, "fable": 17})
        self.assertTrue(usage.complete(seen))
        self.assertFalse(seen["window_details"]["fable"]["is_active"])
        self.assertFalse(self.admits(seen, minimum=18))
        self.assertTrue(self.admits(seen, minimum=17))
        # Equivalent timestamps and legacy/model aliases identify the same weekly bucket.
        seen = self.read([self.row("weekly_scoped", 20, scope={"model": {"display_name": "Opus"}})],
                         seven_day_opus={"utilization": 20, "resets_at": "2030-02-03T05:05:06+01:00"},
                         model_scoped=[{"display_name": "opus", "utilization": 20}])
        self.assertEqual(seen["windows"], {"opus": 80})
        self.assertTrue(usage.complete(seen))

    def test_surface_scopes_and_reserved_model_names_do_not_overwrite_base_windows(self):
        rows = [self.row(), self.row("weekly_scoped", 25, scope={"surface": {"display_name": "Chat"}}),
                self.row("weekly_scoped", 90, scope={"model": {"display_name": "week"}})]
        seen = self.read(rows)
        self.assertEqual(seen["windows"], {"week": 63, "surface_chat": 75, "model_week": 10})
        self.assertFalse(self.admits(seen, minimum=11))

    def test_conflicting_equivalents_never_hide_a_constraint(self):
        for legacy in ({"seven_day": {"utilization": 38}},
                       {"seven_day": {"utilization": 37, "resets_at": "2031-02-03T04:05:06Z"}},
                       {"seven_day": {"utilization": None}}):
            with self.subTest(legacy=legacy):
                seen = self.read([self.row(), self.row("session", 10)], **legacy)
                self.assertEqual(seen["windows"], {"session": 90})
                self.assertIn("week", seen["unknown"])
                self.assertFalse(self.admits(seen))
        seen = self.read([self.row(), self.row(percent=38)])
        self.assertIn("week", seen["unknown"])
        self.assertEqual(seen["windows"], {})
        self.assertTrue(usage.complete(self.read([self.row(), self.row()])))
        self.assertTrue(usage.complete(self.read([self.row(scope={})])))
        self.assertTrue(usage.complete(self.read([self.row(scope={"model": None, "surface": None})])))

    def test_malformed_or_unsupported_rows_preserve_known_data_but_block_even_zero_minimum(self):
        bad_rows = [None, {}, self.row("future_meter"), self.row(percent=None),
                    self.row(percent=True), self.row(percent=-1), self.row(percent=101),
                    self.row(percent="secret-value"), self.row(active=None),
                    self.row("weekly_scoped", scope={}),
                    self.row("weekly_scoped", scope={"model": {"display_name": ""}}),
                    self.row("weekly_scoped", scope={"model": {}, "surface": {}})]
        for value in (float("nan"), float("inf"), float("-inf")):
            bad_rows.append(self.row(percent=value))
        for reset in (10, False, "not-a-date", "2030-02-03T04:05:06"):
            bad_rows.append(dict(self.row(), resets_at=reset))
        for row in bad_rows:
            with self.subTest(row=row):
                seen = self.read([self.row("session", 11), row])
                self.assertEqual(seen["windows"]["session"], 89)
                self.assertTrue(seen["unknown"])
                self.assertFalse(self.admits(seen))
                self.assertNotIn("secret-value", json.dumps(seen))

    def test_top_level_errors_do_not_echo_private_payload_or_publish_percentages(self):
        for key in ("error", "errors", "rate_limits_error"):
            with self.subTest(key=key), self.assertRaises(Failure) as caught:
                usage.claude_windows({"rate_limits_available": True,
                                      "rate_limits": {"limits": [self.row()]},
                                      key: {"secret": "private-auth-payload"}})
            self.assertNotIn("private-auth-payload", str(caught.exception))


class UsageCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.start = datetime(2026, 10, 4, tzinfo=timezone.utc)
        self.cache = usage.UsageCache(clock=lambda: self.now,
                                     wall_clock=lambda: self.start + timedelta(seconds=self.now))

    def test_opt_in_interval_and_shared_policy_observation(self):
        calls = []

        def observe(providers):
            calls.append(set(providers))
            return {provider: {"windows": {"primary": 63}} for provider in providers}

        self.cache.refresh(observe)
        self.assertEqual(calls, [])
        self.assertEqual(self.cache.snapshot(), [])
        self.cache.configure(["codex", "codex"])
        self.cache.refresh(observe)
        self.assertEqual(calls, [{"codex"}])
        self.now = 299
        self.cache.refresh(observe)
        self.assertEqual(len(calls), 1)
        self.now = 300
        self.cache.refresh(observe)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.cache.next_refresh(), 600)
        self.now = 600
        self.cache.collect({"codex", "claude"}, observe)
        self.cache.refresh(observe, force=True, observed={"codex", "claude"})
        self.assertEqual(len(calls), 3)
        self.assertEqual([entry["provider"] for entry in self.cache.snapshot()], ["codex"])

    def test_failure_preserves_windows_and_success_time_until_recovery(self):
        self.cache.configure(["claude", "codex"])
        self.cache.record({"claude": {"windows": {"session": 72, "week": 48, "fable": 85}},
                           "codex": {"error": "not logged in"}})
        first, unavailable = self.cache.snapshot()
        self.assertEqual(first["status"], "current")
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertIsNone(unavailable["updated_at"])
        self.assertIsNone(unavailable["windows"])
        self.now = 30
        self.cache.record({"claude": {"error": "timed out"}})
        stale = self.cache.snapshot()[0]
        self.assertEqual(stale["windows"], first["windows"])
        self.assertEqual(stale["updated_at"], first["updated_at"])
        self.assertEqual((stale["status"], stale["error"]), ("stale", "timed out"))
        stale["windows"]["week"] = 0
        self.assertEqual(self.cache.snapshot()[0]["windows"]["week"], 48)
        self.now = 40
        self.cache.record({"claude": {"windows": {"session": 71, "week": 47}}})
        recovered = self.cache.snapshot()[0]
        self.assertEqual(recovered["status"], "current")
        self.assertIsNone(recovered["error"])
        self.assertNotEqual(recovered["updated_at"], first["updated_at"])
        self.assertNotIn("fable", recovered["windows"])
        self.now = 340
        self.assertEqual(self.cache.snapshot()[0]["status"], "stale")

    def test_unexpected_read_failure_is_cached_and_throttled(self):
        self.cache.configure(["codex"])
        calls = []

        def broken(providers):
            calls.append(providers)
            raise RuntimeError("unavailable")

        self.cache.refresh(broken)
        self.cache.refresh(broken)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.cache.snapshot()[0]["error"], "unavailable")

    def test_partial_reading_has_fresh_measurement_time_but_no_complete_success(self):
        self.cache.configure(["claude"])
        partial = {"windows": {"week": 62}, "unknown": {"session": "Missing session measurement"},
                   "absent": [], "window_details": {"week": {"label": "Current week (all models)"}}}
        self.cache.record({"claude": partial})
        first = self.cache.snapshot()[0]
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["updated_at"], self.start.isoformat())
        self.assertIsNone(first["last_success_at"])
        self.assertNotIn("claude", self.cache.successes)
        self.cache.refresh(lambda providers: self.fail("partial attempts must also be throttled"))
        self.now = 10
        self.cache.record({"claude": {"windows": {"session": 80, "week": 61}}})
        complete = self.cache.snapshot()[0]
        self.assertEqual(complete["status"], "current")
        self.assertEqual(complete["updated_at"], complete["last_success_at"])
        self.now = 20
        self.cache.record({"claude": partial})
        second = self.cache.snapshot()[0]
        self.assertEqual(second["status"], "partial")
        self.assertNotIn("session", second["windows"])
        self.assertEqual(second["last_success_at"], complete["last_success_at"])
        self.assertNotEqual(second["updated_at"], complete["updated_at"])
        self.now = 320
        self.assertEqual(self.cache.snapshot()[0]["status"], "stale")
        self.cache.record({"claude": {"error": "fetch failed"}})
        failed = self.cache.snapshot()[0]
        self.assertEqual(failed["windows"], second["windows"])
        self.assertEqual(failed["unknown"], second["unknown"])
        self.assertEqual(failed["updated_at"], second["updated_at"])
        self.assertNotEqual(failed["attempted_at"], second["attempted_at"])
