import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from projectweave.github import GitHub
from projectweave.graph import validate
from projectweave.runtime import Runtime
from projectweave.setup import templates

ROOT = Path(__file__).resolve().parents[1]


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "checkout with spaces"
        self.root.mkdir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        for name in ("git", "gh", "gitweave"):
            script = self.bin / name
            script.write_text(f"#!{sys.executable}\n" + (ROOT / "tests/fake_init_cli.py").read_text())
            script.chmod(0o755)
        self.directory = self.root / ".projectweave"
        self.log = Path(self.tmp.name) / "calls.jsonl"
        self.state = Path(self.tmp.name) / "remote.json"
        self.state.write_text(json.dumps({"label": False, "projects": 0}))
        self.env = dict(os.environ, PATH=str(self.bin), PYTHONPATH=str(ROOT),
                        INIT_ROOT=str(self.root), INIT_LOG=str(self.log), INIT_STATE=str(self.state))

    def invoke(self, *args, mode="success", origin=None):
        self.log.unlink(missing_ok=True)
        env = dict(self.env, INIT_MODE=mode)
        if origin:
            env["INIT_ORIGIN"] = origin
        result = subprocess.run([sys.executable, "-m", "projectweave", "init", "--repo", "o/r", *args],
                                cwd=self.root, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.stderr, "")
        report = json.loads(result.stdout)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        # The strict fixture rejects all execution, issue/field mutation, installation, auth changes and pushes.
        self.assertFalse(any(c["name"] == "gitweave" and c["args"][0] != "validate" for c in calls))
        return result.returncode, report, calls

    def read(self, name):
        return json.loads((self.directory / name).read_text())

    def write(self, name, value):
        self.directory.mkdir(exist_ok=True)
        (self.directory / name).write_text(json.dumps(value))

    def mutations(self, calls):
        return [c for c in calls if c["args"][:2] == ["label", "create"] or
                (c["request"] and c["request"]["query"].startswith("mutation"))]

    def test_first_run_existing_project_and_zero_capacity(self):
        code, report, calls = self.invoke("--project-number", "7")
        self.assertEqual(code, 0, report)
        self.assertTrue(report["initialized"])
        self.assertFalse(report["ready"])
        self.assertEqual(len(report["created"]), 5)
        self.assertEqual(len(self.mutations(calls)), 1)
        self.assertEqual(self.read("project.json"), {"owner": "o", "owner_type": "organization", "number": 7,
                         "repository": "o/r", "label": "projectweave-ready", "priority_order": []})
        graph = validate(self.read("graph.json"))
        executor = graph["nodes"]["execute"]["executor"]
        self.assertEqual(Path(executor["repo"]), self.root)
        self.assertEqual(Path(executor["graph"]), self.directory / "gitweave.json")
        self.assertEqual(self.read("resources.json")["gitweave"]["available"], 0)
        self.assertEqual(self.read("gitweave.json")["nodes"]["work"]["model"], "CONFIGURE_MODEL")
        self.assertTrue(any("item-add 7 --owner o" in c for c in report["next_commands"]))
        self.assertTrue(any("--add-label projectweave-ready" in c for c in report["next_commands"]))
        task = {"id": "I", "item_id": "ITEM", "project_id": "P", "state": "OPEN", "labels": ["projectweave-ready"],
                "priority": None, "status": None, "created_at": "2026", "url": "url", "repository": "o/r"}
        with patch.object(GitHub, "load", return_value=[task]), patch("projectweave.runtime.invoke") as invoke, patch.object(GitHub, "writeback") as writeback:
            record = Runtime(graph, self.read("project.json"), self.read("resources.json")).run()
        self.assertIsNone(record["failure"])
        invoke.assert_not_called()
        writeback.assert_not_called()

    def test_create_user_project_and_rerun_reuses_all_without_overwrite(self):
        self.state.write_text(json.dumps({"label": False, "projects": 0, "owner_type": "User"}))
        code, report, _ = self.invoke("--create-project", "First run", "--project-owner", "team")
        self.assertEqual(code, 0, report)
        self.assertEqual(self.read("project.json")["owner_type"], "user")
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        for flags in ((), ("--create-project", "First run", "--project-owner", "team")):
            code, report, calls = self.invoke(*flags)
            self.assertEqual(code, 0, report)
            self.assertEqual(report["created"], [])
            self.assertEqual(self.mutations(calls), [])
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir()})
        self.assertEqual(json.loads(self.state.read_text())["projects"], 1)

    def test_human_model_capacity_edits_reused_missing_file_restored(self):
        self.invoke("--project-number", "7")
        worker = self.read("gitweave.json")
        worker["nodes"]["work"].update(provider="codex", model="human-selected", effort="medium")
        self.write("gitweave.json", worker)
        capacity = self.read("resources.json")
        capacity["gitweave"]["available"] = 2
        self.write("resources.json", capacity)
        (self.directory / "graph.json").unlink()
        code, report, calls = self.invoke()
        self.assertEqual(code, 0, report)
        self.assertFalse(report["ready"])  # Static checks never certify live provider/access readiness.
        self.assertEqual(report["missing"], [])
        self.assertEqual(self.read("gitweave.json"), worker)
        self.assertEqual(self.read("resources.json"), capacity)
        self.assertEqual(self.mutations(calls), [])

    def test_incompatible_files_stop_before_remote_mutations(self):
        bad_values = {"graph.json": {"version": 999}, "resources.json": {"gitweave": {"available": -1}},
                      "gitweave.json": {"nodes": []}, "project.json": None}
        for name, bad in bad_values.items():
            with self.subTest(name=name):
                self.write(name, bad)
                before = (self.directory / name).read_bytes()
                code, report, calls = self.invoke("--create-project", "New")
                self.assertEqual(code, 2, report)
                self.assertEqual(self.mutations(calls), [])
                self.assertEqual((self.directory / name).read_bytes(), before)
                (self.directory / name).unlink()

    def test_symlinks_rejected(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        self.directory.symlink_to(outside, target_is_directory=True)
        code, _, calls = self.invoke("--project-number", "7")
        self.assertEqual(code, 2)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.mutations(calls), [])
        self.directory.unlink()
        self.directory.mkdir()
        target = outside / "project.json"
        target.write_text("{}")
        (self.directory / "project.json").symlink_to(target)
        code, _, calls = self.invoke("--project-number", "7")
        self.assertEqual(code, 2)
        self.assertEqual(target.read_text(), "{}")
        self.assertEqual(self.mutations(calls), [])

    def test_identity_and_commit_preflight(self):
        for origin in ("git@github.com:other/repo.git", "https://elsewhere/o/r.git", "/local/o/r", "https://github.com/o/r.git.evil"):
            with self.subTest(origin=origin):
                code, report, calls = self.invoke("--create-project", "New", origin=origin)
                self.assertEqual(code, 2, report)
                self.assertFalse(self.directory.exists())
                self.assertTrue(all(c["name"] == "git" for c in calls))
        code, report, calls = self.invoke("--create-project", "New", mode="unborn")
        self.assertEqual(code, 2)
        self.assertIn("first commit", report["failure"]["action"])
        self.assertTrue(all(c["name"] == "git" for c in calls))
        for origin in ("https://github.com/o/r.git", "ssh://git@github.com/o/r.git", "https://github.com/O/R"):
            self.assertEqual(self.invoke("--project-number", "7", origin=origin)[0], 0)

    def test_selection_required_and_conflicts(self):
        code, report, calls = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("--project-number", report["failure"]["message"])
        self.assertEqual(self.mutations(calls), [])
        self.invoke("--project-number", "7")
        for flags in (("--project-number", "8"), ("--project-owner", "team")):
            code, report, calls = self.invoke(*flags)
            self.assertEqual(code, 2)
            self.assertEqual(self.mutations(calls), [])

    def test_tool_auth_access_and_static_validation_failures(self):
        for mode in ("auth", "repo", "project", "invalid_graph", "repo_redirect", "malformed", "owner_type"):
            with self.subTest(mode=mode):
                code, report, calls = self.invoke("--project-number", "7", mode=mode)
                self.assertEqual(code, 2, report)
                self.assertFalse(report["initialized"])
                self.assertFalse(self.directory.exists())
                self.assertEqual(self.mutations(calls), [])
        for tool in ("git", "gitweave", "gh"):
            with self.subTest(tool=tool):
                source = self.bin / tool
                source.rename(self.bin / (tool + ".disabled"))
                code, report, calls = self.invoke("--project-number", "7")
                self.assertEqual(code, 2, report)
                self.assertIn(tool, report["failure"]["message"])
                self.assertEqual(self.mutations(calls), [])
                (self.bin / (tool + ".disabled")).rename(source)

    def test_partial_failures_recover_using_saved_project(self):
        for mode in ("label_read", "label_write", "label_lost"):
            with self.subTest(mode=mode):
                self.state.write_text(json.dumps({"label": False, "projects": 0}))
                if self.directory.exists():
                    for path in self.directory.iterdir():
                        path.unlink()
                code, report, _ = self.invoke("--create-project", "New", mode=mode)
                self.assertEqual(code, 2)
                self.assertEqual(self.read("project.json")["number"], 9)
                self.assertIn("Project o #9", report["created"])
                code, report, calls = self.invoke("--create-project", "New")
                self.assertEqual(code, 0, report)
                self.assertEqual(json.loads(self.state.read_text())["projects"], 1)
                self.assertFalse(any(c["request"] and "createProjectV2(" in c["request"]["query"] for c in calls))

    def test_lost_creation_reports_inspect_before_rerun(self):
        code, report, calls = self.invoke("--create-project", "New", mode="create_lost")
        self.assertEqual(code, 2)
        self.assertIn("gh project list --owner o", report["failure"]["action"])
        self.assertEqual(len(self.mutations(calls)), 1)
        code, report, calls = self.invoke("--project-number", "9")
        self.assertEqual(code, 0, report)
        self.assertEqual(json.loads(self.state.read_text())["projects"], 1)

    def test_local_save_failure_reports_created_identity_for_recovery(self):
        code, report, calls = self.invoke("--create-project", "New", mode="local_save")
        self.assertEqual(code, 2)
        self.assertIn("Project o #9", report["created"])
        self.assertIn("--project-number 9", report["failure"]["action"])
        self.directory.unlink()
        code, report, calls = self.invoke("--project-number", "9")
        self.assertEqual(code, 0, report)
        self.assertEqual(json.loads(self.state.read_text())["projects"], 1)

    def test_no_fields_needed_and_incompatible_policy_is_not_repaired(self):
        code, report, calls = self.invoke("--project-number", "7")
        self.assertEqual(code, 0)
        self.assertTrue(all(not c["request"] or "fields(" not in c["request"]["query"] for c in calls))
        project = self.read("project.json")
        project["eligible_statuses"] = ["Todo"]
        self.write("project.json", project)
        code, report, calls = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("no Status/Priority policy", report["failure"]["message"])
        self.assertEqual(self.mutations(calls), [])

    def test_incompatible_label_case_is_reported(self):
        self.state.write_text(json.dumps({"label": True, "projects": 0, "label_name": "ProjectWeave-Ready"}))
        code, report, calls = self.invoke("--project-number", "7")
        self.assertEqual(code, 2)
        self.assertIn("Incompatible label casing", report["failure"]["message"])
        self.assertEqual(self.mutations(calls), [])

    def test_generated_workflow_executes_and_only_comments_using_external_fixtures(self):
        self.assertEqual(self.invoke("--project-number", "7")[0], 0)
        worker = self.read("gitweave.json")
        worker["nodes"]["work"].update(provider="codex", model="human-selected")
        self.write("gitweave.json", worker)
        capacity = self.read("resources.json")
        capacity["gitweave"]["available"] = 1
        self.write("resources.json", capacity)
        for name in ("gh", "gitweave"):
            (self.bin / name).write_text(f"#!{sys.executable}\n" + (ROOT / "tests/fake_cli.py").read_text())
        self.log.unlink()
        args = [sys.executable, "-m", "projectweave", "run", "--graph", str(self.directory / "graph.json"),
                "--project", str(self.directory / "project.json"), "--resources", str(self.directory / "resources.json")]
        result = subprocess.run(args, cwd=self.bin, env=dict(self.env, FAKE_LOG=str(self.log), FAKE_MODE="success"),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["results"]["comment"]["data"]["status"], None)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        launch = next(c for c in calls if c["command"] == "gitweave")
        self.assertEqual(launch["argv"][2], str(self.directory / "gitweave.json"))
        self.assertEqual(launch["argv"][4], str(self.root))
        mutations = [c for c in calls if c["command"] == "gh" and c["request"]["query"].startswith("mutation")]
        self.assertEqual(len(mutations), 1)
        self.assertIn("addComment(", mutations[0]["request"]["query"])

    def test_model_omission_is_not_accepted_as_native_defaults(self):
        worker = templates(self.root)["gitweave.json"]
        del worker["nodes"]["work"]["model"]
        self.write("gitweave.json", worker)
        code, report, calls = self.invoke("--create-project", "New")
        self.assertEqual(code, 2)
        self.assertIn("gitweave.json", report["failure"]["message"])
        self.assertEqual(self.read("gitweave.json"), worker)
        self.assertEqual(self.mutations(calls), [])

    def test_cross_repository_issue_excluded_and_priority_not_assigned(self):
        backend = GitHub({"owner": "o", "owner_type": "user", "number": 7, "repository": "o/r", "priority_order": []})
        old = {"state": "OPEN", "labels": ["projectweave-ready"], "priority": "P2", "created_at": "2025", "url": "url", "item_id": "1", "repository": "O/R"}
        new = dict(old, priority="P0", created_at="2026")
        foreign = dict(old, created_at="2020", repository="o/other")
        self.assertIs(backend.select([foreign, new, old]), old)

    @unittest.skipUnless(os.environ.get("GITWEAVE_SOURCE"), "Optional installed public GitWeave source validation")
    def test_actual_gitweave_public_validator(self):
        path = self.root / "generated.json"
        path.write_text(json.dumps(templates(self.root)["gitweave.json"]))
        result = subprocess.run([sys.executable, "-m", "gitweave", "validate", "--graph", str(path)],
                                cwd=self.root, env=dict(self.env, PYTHONPATH=os.environ["GITWEAVE_SOURCE"]),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
