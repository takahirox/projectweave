import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from projectweave import usage
from projectweave.contracts import Failure

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
        for mode in ("claude_failure", "claude_no_week"):
            with self.subTest(mode=mode):
                self.env(FAKE_USAGE=mode)
                with self.assertRaises(Failure):
                    usage.claude()

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
