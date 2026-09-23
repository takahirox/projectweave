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
        self.root = Path(self.tmp.name).resolve() / "workspace with spaces"
        self.root.mkdir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        for name in ("git", "gh", "gitweave"):
            script = self.bin / name
            script.write_text(f"#!{sys.executable}\n" + (ROOT / "tests/fake_init_cli.py").read_text())
            script.chmod(0o755)
        self.directory = self.root  # The workspace holds configuration directly.
        self.log = Path(self.tmp.name) / "calls.jsonl"
        self.state = Path(self.tmp.name) / "remote.json"
        self.state.write_text(json.dumps({"projects": 0}))
        self.env = dict(os.environ, PATH=str(self.bin), PYTHONPATH=str(ROOT),
                        INIT_ROOT=str(self.root), INIT_LOG=str(self.log), INIT_STATE=str(self.state))

    def invoke(self, *args, mode="success", owner=True):
        self.log.unlink(missing_ok=True)
        env = dict(self.env, INIT_MODE=mode)
        if owner and "--project-owner" not in args:
            args = ("--project-owner", "o", *args)
        result = subprocess.run([sys.executable, "-m", "projectweave", "init", *args],
                                cwd=self.root, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.stderr, "")
        report = json.loads(result.stdout)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        # The strict fixture rejects all execution, issue mutation and unexpected field mutation, installation, auth changes and pushes.
        self.assertFalse(any(c["name"] == "gitweave" and c["args"][0] != "validate" for c in calls))
        self.assertFalse(any(c["name"] == "git" or c["args"][:2] == ["repo", "clone"] for c in calls))
        self.assertFalse((self.root / "repos").exists())
        return result.returncode, report, calls

    def read(self, name):
        return json.loads((self.directory / name).read_text())

    def write(self, name, value):
        self.directory.mkdir(exist_ok=True)
        (self.directory / name).write_text(json.dumps(value))

    def mutations(self, calls):
        return [c for c in calls if c["request"] and c["request"]["query"].startswith("mutation")]

    def test_first_run_existing_project_and_unknown_remaining_usage(self):
        code, report, calls = self.invoke("--project-number", "7")
        self.assertEqual(code, 0, report)
        self.assertTrue(report["initialized"])
        self.assertFalse(report["ready"])
        self.assertEqual(len(report["created"]), 6)
        self.assertEqual(len(self.mutations(calls)), 2)
        self.assertEqual(self.read("project.json"), {"owner": "o", "owner_type": "organization", "number": 7,
                         "priority_order": ["P0", "P1", "P2"]})
        graph = validate(self.read("graph.json"))
        executor = graph["nodes"]["execute"]["executor"]
        self.assertEqual(executor, {"type": "gitweave", "graph": str(self.root / "gitweave.json")})
        self.assertEqual(self.read("resources.json"), {"subscription": {"type": "subscription", "stop_at_remaining_percent": 20}})
        self.assertEqual(graph["nodes"]["subscription"]["config"], {"subscriptions": ["subscription"]})
        # The generated files are the packaged canonical templates; only the GitWeave graph path is materialized.
        canonical = json.loads((ROOT / "projectweave/templates/graph.json").read_text())
        canonical["nodes"]["execute"]["executor"]["graph"] = str(self.root / "gitweave.json")
        self.assertEqual(graph, canonical)
        for name in ("gitweave.json", "resources.json"):
            self.assertEqual(self.read(name), json.loads((ROOT / "projectweave/templates" / name).read_text()))
        self.assertNotIn("requires", graph["nodes"]["execute"])
        self.assertTrue(any("remaining_percent" in entry for entry in report["missing"]))
        self.assertEqual(self.read("gitweave.json")["nodes"]["work"]["model"], "CONFIGURE_MODEL")
        self.assertTrue(any("item-add 7 --owner o" in c for c in report["next_commands"]))
        self.assertIn("projectweave run --graph graph.json --project project.json --resources resources.json", report["next_commands"])
        self.assertIn("AI execution field (Ready/Not ready)", report["created"])
        self.assertFalse(any("label" in c for c in report["next_commands"]))
        self.assertTrue(any("AI execution field to Ready" in a for a in report["human_actions"]))
        task = {"id": "I", "item_id": "ITEM", "project_id": "P", "state": "OPEN", "labels": [], "ai_execution": "Ready",
                "priority": None, "status": None, "created_at": "2026", "url": "url", "repository": "o/r"}
        with patch.object(GitHub, "load", return_value=[task]), patch("projectweave.runtime.invoke") as invoke, patch.object(GitHub, "writeback") as writeback:
            record = Runtime(graph, self.read("project.json"), self.read("resources.json")).run()
        self.assertIsNone(record["failure"])
        invoke.assert_not_called()
        writeback.assert_not_called()
        with patch.object(GitHub, "load", return_value=[]):
            record = Runtime(graph, self.read("project.json"), self.read("resources.json")).run()
        self.assertEqual(record["last"]["data"], {"status": "no_work"})

    def test_create_user_project_and_rerun_reuses_all_without_overwrite(self):
        self.state.write_text(json.dumps({"projects": 0, "owner_type": "User"}))
        code, report, _ = self.invoke("--create-project", "First run", "--project-owner", "team")
        self.assertEqual(code, 0, report)
        self.assertEqual(self.read("project.json")["owner_type"], "user")
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        for flags in ((), ("--create-project", "First run", "--project-owner", "team")):
            code, report, calls = self.invoke(*flags, owner=False)
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
        capacity["subscription"].update(remaining_percent=45, stop_at_remaining_percent=30)
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
        bad_values = {"graph.json": {"version": 999}, "resources.json": {"subscription": {"type": "subscription", "stop_at_remaining_percent": 101}},
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

    def test_graph_generated_before_canonical_template_is_incompatible(self):
        self.invoke("--project-number", "7")
        old = self.read("graph.json")
        old["nodes"]["capacity"] = old["nodes"].pop("subscription")
        old["nodes"]["comment"] = old["nodes"].pop("writeback")
        del old["nodes"]["no_work"]
        old["flow"] = ["load", "select", {"if": {"path": "/results/select/data/task", "equals": None, "then": [],
            "else": ["capacity", {"if": {"path": "/results/capacity/data/available", "equals": True,
            "then": ["execute", "comment"], "else": []}}]}}]
        self.write("graph.json", old)
        code, report, calls = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("Incompatible graph.json", report["failure"]["message"])
        self.assertEqual(self.read("graph.json"), old)
        self.assertEqual(self.mutations(calls), [])

    def test_old_run_capacity_resources_reported_clearly(self):
        old = {"gitweave": {"unit": "runs", "available": 1, "accounting": "reservation"}}
        self.write("resources.json", old)
        code, report, calls = self.invoke("--project-number", "7")
        self.assertEqual(code, 2)
        self.assertIn("not gitweave run capacity", report["failure"]["message"])
        self.assertEqual(self.read("resources.json"), old)
        self.assertEqual(self.mutations(calls), [])

    def test_symlinks_rejected(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        target = outside / "project.json"
        target.write_text("{}")
        (self.directory / "project.json").symlink_to(target)
        code, _, calls = self.invoke("--project-number", "7")
        self.assertEqual(code, 2)
        self.assertEqual(target.read_text(), "{}")
        self.assertEqual(self.mutations(calls), [])

    def test_owner_required_without_repository_or_checkout(self):
        code, report, calls = self.invoke("--project-number", "7", owner=False)
        self.assertEqual(code, 2)
        self.assertIn("--project-owner", report["failure"]["message"])
        self.assertEqual(calls, [])
        # A plain directory (not a Git checkout) is a valid workspace; no repository is named or cloned.
        code, report, calls = self.invoke("--project-number", "7")
        self.assertEqual(code, 0, report)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), sorted(["project.json", "resources.json", "graph.json", "gitweave.json"]))

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
        for mode in ("auth", "project", "invalid_graph", "owner_type"):
            with self.subTest(mode=mode):
                code, report, calls = self.invoke("--project-number", "7", mode=mode)
                self.assertEqual(code, 2, report)
                self.assertFalse(report["initialized"])
                self.assertEqual(list(self.root.iterdir()), [])
                self.assertEqual(self.mutations(calls), [])
        for tool in ("gitweave", "gh"):
            with self.subTest(tool=tool):
                source = self.bin / tool
                source.rename(self.bin / (tool + ".disabled"))
                code, report, calls = self.invoke("--project-number", "7")
                self.assertEqual(code, 2, report)
                self.assertIn(tool, report["failure"]["message"])
                self.assertEqual(self.mutations(calls), [])
                (self.bin / (tool + ".disabled")).rename(source)

    def test_partial_failures_recover_using_saved_project(self):
        for mode in ("field_read", "field_write", "field_lost"):
            with self.subTest(mode=mode):
                self.state.write_text(json.dumps({"projects": 0}))
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
        (self.directory / "project.json").unlink()
        code, report, calls = self.invoke("--project-number", "9")
        self.assertEqual(code, 0, report)
        self.assertEqual(json.loads(self.state.read_text())["projects"], 1)

    def test_incompatible_status_policy_is_not_repaired(self):
        code, report, calls = self.invoke("--project-number", "7")
        self.assertEqual(code, 0)
        project = self.read("project.json")
        project["eligible_statuses"] = ["Todo"]
        self.write("project.json", project)
        code, report, calls = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("no Status policy", report["failure"]["message"])
        self.assertEqual(self.mutations(calls), [])

    def test_compatible_priority_on_later_page_reused_without_changes(self):
        field = {"__typename": "ProjectV2SingleSelectField", "name": "Priority",
                 "dataType": "SINGLE_SELECT", "options": [{"name": n} for n in ("Other", "P2", "P0", "P1")]}
        ready = {"__typename": "ProjectV2SingleSelectField", "name": "AI execution",
                 "dataType": "SINGLE_SELECT", "options": [{"name": n} for n in ("Not ready", "Blocked", "Ready")]}
        state = {"projects": 0, "fields": [field, ready]}
        self.state.write_text(json.dumps(state))
        code, report, calls = self.invoke("--project-number", "7")
        self.assertEqual(code, 0, report)
        self.assertIn("Priority field (P0/P1/P2)", report["existing"])
        self.assertIn("AI execution field (Ready/Not ready)", report["existing"])
        self.assertEqual(self.mutations(calls), [])
        self.assertEqual(json.loads(self.state.read_text()), state)
        reads = [c for c in calls if c["request"] and "fields(first:" in c["request"]["query"]]
        self.assertEqual([c["request"]["variables"]["cursor"] for c in reads], [None, "last"])

    def test_incompatible_priority_never_repaired(self):
        for kind, datatype, options in (("ProjectV2Field", "TEXT", []),
                ("ProjectV2IterationField", "ITERATION", []),
                ("ProjectV2SingleSelectField", "SINGLE_SELECT", ["P0", "P1"]),
                ("ProjectV2SingleSelectField", "SINGLE_SELECT", ["P0", "P1", "p2"]),
                ("ProjectV2SingleSelectField", "SINGLE_SELECT", ["P0", "P1", "P2", "P2"])):
            with self.subTest(kind=kind, options=options):
                state = {"projects": 0, "fields": [{"__typename": kind,
                         "name": "Priority", "dataType": datatype, "options": [{"name": n} for n in options]}]}
                self.state.write_text(json.dumps(state))
                code, report, calls = self.invoke("--project-number", "7")
                self.assertEqual(code, 2)
                self.assertFalse(report["initialized"])
                self.assertIn("Incompatible Priority", report["failure"]["message"])
                self.assertTrue(any("Priority" in entry for entry in report["missing"]))
                self.assertEqual(self.mutations(calls), [])
                self.assertEqual(json.loads(self.state.read_text()), state)

    def test_ambiguous_priority_across_pages_not_accepted(self):
        field = {"__typename": "ProjectV2SingleSelectField", "name": "Priority",
                 "dataType": "SINGLE_SELECT", "options": [{"name": n} for n in ("P0", "P1", "P2")]}
        state = {"projects": 0, "first_fields": [field], "fields": [field]}
        self.state.write_text(json.dumps(state))
        code, report, calls = self.invoke("--project-number", "7")
        self.assertEqual(code, 2)
        self.assertIn("ambiguous", report["failure"]["message"])
        self.assertEqual(self.mutations(calls), [])
        self.assertEqual(json.loads(self.state.read_text()), state)

    def test_priority_failure_and_recovery_without_duplicate_creation(self):
        for mode in ("field_read", "field_page", "field_write", "field_lost"):
            with self.subTest(mode=mode):
                self.state.write_text(json.dumps({"projects": 0}))
                code, report, calls = self.invoke("--project-number", "7", mode=mode)
                self.assertEqual(code, 2, report)
                self.assertFalse(report["initialized"])
                self.assertIn("Rerun init", report["failure"]["action"])
                self.assertTrue(any("Priority" in entry for entry in report["missing"]))
                if mode in ("field_read", "field_page"):
                    self.assertEqual(self.mutations(calls), [])
                before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
                code, report, calls = self.invoke()
                self.assertEqual(code, 0, report)
                # Priority is created first; a lost Priority response leaves only AI execution to create.
                self.assertEqual(len(self.mutations(calls)), 1 if mode == "field_lost" else 2)
                self.assertEqual(len(json.loads(self.state.read_text())["fields"]), 2)
                self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir()})
                code, report, calls = self.invoke()
                self.assertEqual(code, 0, report)
                self.assertEqual(self.mutations(calls), [])

    def test_old_empty_priority_configuration_not_overwritten(self):
        self.invoke("--project-number", "7")
        project = self.read("project.json")
        project["priority_order"] = []
        self.write("project.json", project)
        code, report, calls = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("Priority order P0/P1/P2", report["failure"]["message"])
        self.assertEqual(self.read("project.json"), project)
        self.assertEqual(self.mutations(calls), [])

    def test_second_field_lost_creation_recovers_without_duplicate(self):
        priority = {"__typename": "ProjectV2SingleSelectField", "name": "Priority", "dataType": "SINGLE_SELECT",
                    "options": [{"name": n} for n in ("P0", "P1", "P2")]}
        self.state.write_text(json.dumps({"projects": 0, "fields": [priority]}))
        code, report, calls = self.invoke("--project-number", "7", mode="field_lost")
        self.assertEqual(code, 2, report)
        self.assertTrue(any("AI execution" in entry for entry in report["missing"]))
        self.assertEqual(len(self.mutations(calls)), 1)
        code, report, calls = self.invoke()
        self.assertEqual(code, 0, report)
        self.assertEqual(self.mutations(calls), [])
        self.assertIn("AI execution field (Ready/Not ready)", report["existing"])
        self.assertEqual([f["name"] for f in json.loads(self.state.read_text())["fields"]], ["Priority", "AI execution"])

    def test_incompatible_ai_execution_field_never_repaired(self):
        for kind, datatype, options in (("ProjectV2Field", "TEXT", []),
                ("ProjectV2SingleSelectField", "SINGLE_SELECT", ["Ready"]),
                ("ProjectV2SingleSelectField", "SINGLE_SELECT", ["ready", "Not ready"])):
            with self.subTest(kind=kind, options=options):
                state = {"projects": 0, "fields": [
                    {"__typename": "ProjectV2SingleSelectField", "name": "Priority", "dataType": "SINGLE_SELECT",
                     "options": [{"name": n} for n in ("P0", "P1", "P2")]},
                    {"__typename": kind, "name": "AI execution", "dataType": datatype,
                     "options": [{"name": n} for n in options]}]}
                self.state.write_text(json.dumps(state))
                code, report, calls = self.invoke("--project-number", "7")
                self.assertEqual(code, 2)
                self.assertIn("Incompatible AI execution", report["failure"]["message"])
                self.assertTrue(any("AI execution" in entry for entry in report["missing"]))
                self.assertEqual(self.mutations(calls), [])
                self.assertEqual(json.loads(self.state.read_text()), state)

    def test_saved_label_configuration_reports_migration(self):
        self.invoke("--project-number", "7")
        project = dict(self.read("project.json"), label="projectweave-ready")
        self.write("project.json", project)
        code, report, calls = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("Label eligibility was removed", report["failure"]["message"])
        self.assertEqual(self.read("project.json"), project)
        self.assertEqual(self.mutations(calls), [])

    def test_generated_workflow_executes_and_only_comments_using_external_fixtures(self):
        self.assertEqual(self.invoke("--project-number", "7")[0], 0)
        worker = self.read("gitweave.json")
        worker["nodes"]["work"].update(provider="codex", model="human-selected")
        self.write("gitweave.json", worker)
        capacity = self.read("resources.json")
        capacity["subscription"]["remaining_percent"] = 21
        self.write("resources.json", capacity)
        for name in ("gh", "gitweave", "git"):
            (self.bin / name).write_text(f"#!{sys.executable}\n" + (ROOT / "tests/fake_cli.py").read_text())
        self.log.unlink()
        args = [sys.executable, "-m", "projectweave", "run", "--graph", str(self.directory / "graph.json"),
                "--project", str(self.directory / "project.json"), "--resources", str(self.directory / "resources.json")]
        result = subprocess.run(args, cwd=self.bin, env=dict(self.env, FAKE_LOG=str(self.log), FAKE_MODE="success"),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["results"]["writeback"]["data"]["status"], None)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        launch = next(c for c in calls if c["command"] == "gitweave")
        self.assertEqual(launch["argv"][2], str(self.directory / "gitweave.json"))
        self.assertEqual(launch["argv"][4:7], [str(self.root / "repos" / "o" / "r"), "--commit", "origin/HEAD"])
        mutations = [c for c in calls if c["command"] == "gh" and c["request"] and c["request"]["query"].startswith("mutation")]
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

    def test_multi_repository_selection_and_optional_saved_filter(self):
        self.assertEqual(self.invoke("--project-number", "7")[0], 0)
        backend = GitHub(self.read("project.json"))
        old = {"state": "OPEN", "ai_execution": "Ready", "priority": "P2", "created_at": "2025", "url": "url", "item_id": "1", "repository": "O/R"}
        new = dict(old, priority="P0", created_at="2026")
        foreign = dict(old, priority="P0", created_at="2020", repository="o/other")
        middle = dict(old, priority="P1")
        unknown = dict(old, priority="other", created_at="2020")
        absent = dict(unknown, priority=None)
        tasks = [foreign, new, old, middle, unknown, absent]
        before = json.dumps(tasks)
        self.assertIs(backend.select(tasks), foreign)  # Any repository in the Project is eligible by default.
        self.assertIs(backend.select([old, middle, unknown, absent]), middle)
        self.assertIs(backend.select([old, unknown, absent]), old)
        self.assertEqual(json.dumps(tasks), before)
        # An explicit repository filter from an older setup is still accepted as optional policy.
        self.write("project.json", dict(self.read("project.json"), repository="o/r"))
        code, report, calls = self.invoke()
        self.assertEqual(code, 0, report)
        self.assertIs(GitHub(self.read("project.json")).select(tasks), new)

    @unittest.skipUnless(os.environ.get("GITWEAVE_SOURCE"), "Optional installed public GitWeave source validation")
    def test_actual_gitweave_public_validator(self):
        path = self.root / "generated.json"
        path.write_text(json.dumps(templates(self.root)["gitweave.json"]))
        result = subprocess.run([sys.executable, "-m", "gitweave", "validate", "--graph", str(path)],
                                cwd=self.root, env=dict(self.env, PYTHONPATH=os.environ["GITWEAVE_SOURCE"]),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
