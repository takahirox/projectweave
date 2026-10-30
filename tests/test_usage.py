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
        self.assertEqual(usage.claude(), {"session": 90, "week": 80, "fable": 10})

    def test_claude_failures_are_unknown(self):
        for mode in ("claude_failure", "claude_no_week", "claude_unsupported", "claude_fetch_failure",
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
        self.assertEqual(usage.claude(), {"session": 90, "week": 80, "opus": 64.5, "fable": 10})
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

    def test_malformed_or_missing_plan_utilization_never_returns_partial_windows(self):
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
                      [{"display_name": "week", "utilization": 0}],
                      [{"display_name": "Fable", "utilization": 0}] * 2):
            record = copy.deepcopy(valid)
            record["rate_limits"]["model_scoped"] = value
            invalid.append(record)
        for record in invalid:
            with self.subTest(record=record):
                self.env(FAKE_CLAUDE_DATA=json.dumps(record))
                observed = usage.observe(["claude"])["claude"]
                self.assertIn("unavailable", observed["error"])
                self.assertNotIn("windows", observed)
        # Zero is a real quota measurement only when reported as plan utilization.
        self.env(FAKE_CLAUDE_DATA=json.dumps({"rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": 0}, "seven_day": {"utilization": 100}}}))
        self.assertEqual(usage.claude(), {"session": 100, "week": 0})

    def test_invalid_numbers_and_missing_cli_are_actionable(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.env(FAKE_CLAUDE_DATA=json.dumps({"rate_limits_available": True, "rate_limits": {
                    "five_hour": {"utilization": value}, "seven_day": {"utilization": 0}}}))
                self.assertIn("unavailable", usage.observe(["claude"])["claude"]["error"])
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
                self.env(FAKE_USAGE=mode, FAKE_CLAUDE_DATA=(
                    (ROOT / "tests/fixtures/claude-cost-only.json").read_text() if mode == "claude_cost_only" else ""))
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
        self.assertEqual(usage.observe({"codex", "claude"}), {
            "claude": {"windows": {"session": 90, "week": 80, "fable": 10}}, "codex": {"windows": {"primary": 60}}})
        self.env(FAKE_USAGE="codex_exit")
        observed = usage.observe(["codex", "claude"])
        self.assertIn("exited without a rate limit response", observed["codex"]["error"])  # Real reason recorded.
        self.assertIn("windows", observed["claude"])  # Other providers are unaffected.
        self.assertIsInstance(observed["claude"]["windows"]["week"], int)
        self.assertIn("No usage observer", usage.observe(["gemini"])["gemini"]["error"])
        self.assertEqual(usage.observe([]), {})


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
