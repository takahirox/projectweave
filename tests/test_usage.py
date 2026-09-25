import os
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

    def test_claude_takes_smallest_applicable_limit(self):
        self.env(FAKE_CLAUDE_USED="10,20,90")
        self.assertEqual(usage.claude(uses_fable=True), 10)  # Weekly Fable is 90% used.
        self.assertEqual(usage.claude(uses_fable=False), 80)  # Session 90% left, week 80% left.

    def test_claude_failures_are_unknown(self):
        for mode in ("claude_failure", "claude_no_week"):
            with self.subTest(mode=mode):
                self.env(FAKE_USAGE=mode)
                with self.assertRaises(Failure):
                    usage.claude(uses_fable=False)

    def test_codex_primary_window(self):
        self.env(FAKE_CODEX_USED="95")
        self.assertEqual(usage.codex(), 5)
        self.env(FAKE_USAGE="codex_server_request")
        self.assertEqual(usage.codex(), 5)  # The server's own request with id 2 is skipped.

    def test_codex_failures_are_unknown(self):
        for mode in ("codex_error", "codex_no_primary", "codex_hang", "codex_exit"):
            with self.subTest(mode=mode):
                self.env(FAKE_USAGE=mode)
                with self.assertRaises(Failure):
                    usage.codex(timeout=1)

    def test_fable_rule_and_observe_per_provider(self):
        self.assertTrue(usage.fable({"provider": "claude"}))  # No model: native default is unknown.
        self.assertTrue(usage.fable({"provider": "claude", "model": "claude-FABLE-5-1"}))
        self.assertFalse(usage.fable({"provider": "claude", "model": "opus"}))
        self.env(FAKE_CLAUDE_USED="10,20,90", FAKE_CODEX_USED="40")
        nodes = [{"provider": "codex"}, {"provider": "claude", "model": "opus"}, {"provider": "claude", "model": "opus"}]
        self.assertEqual(usage.observe(nodes), {"claude": {"remaining_percent": 80}, "codex": {"remaining_percent": 60}})
        observed = usage.observe([{"provider": "claude", "model": "opus"}, {"provider": "claude"}])
        self.assertEqual(observed, {"claude": {"remaining_percent": 10}})  # Any Fable node includes the Fable limit.
        self.env(FAKE_USAGE="codex_exit")
        observed = usage.observe([{"provider": "codex"}, {"provider": "claude", "model": "opus"}])
        self.assertIn("exited without a rate limit response", observed["codex"]["error"])  # Real reason recorded.
        self.assertEqual(observed["claude"], {"remaining_percent": 80})  # Other providers are unaffected.
        self.assertIsInstance(observed["claude"]["remaining_percent"], int)
        observed = usage.observe([{"provider": "gemini"}])
        self.assertIn("error", observed["gemini"])
        self.assertIn("error", usage.observe([])["(none)"])
