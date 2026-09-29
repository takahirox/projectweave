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
