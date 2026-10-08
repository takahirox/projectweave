import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from projectweave.contracts import Failure, result
from projectweave.execution import ExecutionRegistry
from projectweave.github import validate_project
from projectweave.graph_routes import select_executor
from projectweave.runtime import Runtime
from projectweave.workspace import check_project, claim, run_task

ROOT = Path(__file__).resolve().parents[1]
PROJECT = {"owner": "o", "owner_type": "user", "number": 1}
ROUTES = [{"label": "arbitrary / label", "graph": "graphs/first.json"},
          {"label": "second", "graph": "second.json"}]
TASK = {"number": 1, "repository": "o/r", "labels": []}


class RouteConfigTests(unittest.TestCase):
    def test_routes_are_optional_and_project_specific(self):
        for routes in ([], ROUTES, [dict(ROUTES[0], base_branch="experiment/foo")]):
            project = dict(PROJECT, graph_routes=copy.deepcopy(routes))
            self.assertIs(validate_project(project), project)
        self.assertEqual(validate_project(PROJECT), PROJECT)

    def test_invalid_configuration(self):
        invalid = [None, {}, "routes", 1,
                   [None], [{}], [{"label": "a"}], [{"graph": "a.json"}],
                   [{"label": "a", "graph": "a.json", "extra": True}],
                   [{"label": "a", "graph": "a.json"}, {"label": "A", "graph": "b.json"}]]
        # Exercise each bad value individually, not as a multi-entry route list.
        invalid.extend([{"label": label, "graph": "a.json"}] for label in (None, 1, "", " "))
        invalid.extend([{"label": "a", "graph": graph}] for graph in (None, 1, "", " "))
        invalid.extend([{"label": "a", "graph": "a.json", "base_branch": branch}]
                       for branch in (None, True, 1, [], {}, "", " \t\n", "bad\0branch"))
        invalid.extend([{"label": "a", "graph": graph}] for graph in
                       ("/tmp/a.json", "../a.json", "graphs/../../a.json", "C:/a.json", "\\\\host\\a.json", "a\0.json"))
        for routes in invalid:
            with self.subTest(routes=routes), self.assertRaises(Failure):
                validate_project(dict(PROJECT, graph_routes=routes))

    def test_invalid_branch_error_names_route_field(self):
        with self.assertRaisesRegex(Failure, "graph_routes base_branch must be a nonblank string"):
            validate_project(dict(PROJECT, graph_routes=[dict(ROUTES[0], base_branch=" ")]))


class GraphRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name).resolve() / "project with spaces"
        self.directory.mkdir()
        self.project = dict(PROJECT, graph_routes=copy.deepcopy(ROUTES))
        self.graph = json.loads((ROOT / "projectweave/templates/graph.json").read_text())
        self.config = self.graph["nodes"]["execute"]["executor"]
        self.config["graph"] = "custom-default.json"
        self.worker = {"version": 1, "nodes": {"work": {"kind": "command"}}, "flow": ["work"]}
        for name in ("custom-default.json", "graphs/first.json", "second.json"):
            path = self.directory / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(self.worker))
        (self.directory / "project.json").write_text(json.dumps(self.project))
        (self.directory / "graph.json").write_text(json.dumps(self.graph))
        validator = patch("projectweave.graph_routes.process", return_value="valid")
        self.validator = validator.start()
        self.addCleanup(validator.stop)

    def test_first_configured_match_wins_independent_of_issue_label_order(self):
        cases = [([], "custom-default.json"), (["unrelated"], "custom-default.json"),
                 (["second"], "second.json"), (["arbitrary / label"], "graphs/first.json"),
                 (["second", "arbitrary / label"], "graphs/first.json"),
                 (["ARBITRARY / LABEL", "second"], "graphs/first.json")]
        original = copy.deepcopy(self.config)
        for labels, expected in cases:
            with self.subTest(labels=labels):
                config = select_executor(self.config, self.project, dict(TASK, labels=labels), self.directory)
                self.assertEqual(config, dict(original, graph=expected))
        self.assertEqual(self.config, original)
        self.project["graph_routes"].reverse()
        self.assertEqual(select_executor(self.config, self.project,
                                        dict(TASK, labels=["arbitrary / label", "second"]),
                                        self.directory)["graph"], "second.json")

    def test_no_routes_and_command_executors_keep_existing_config(self):
        for project, config in ((PROJECT, self.config), (dict(PROJECT, graph_routes=[]), self.config),
                                (self.project, {"type": "command", "argv": ["worker"]})):
            self.assertIs(select_executor(config, project, TASK, self.directory), config)
        self.validator.assert_not_called()

    def test_branch_is_selected_with_first_matching_graph_and_does_not_leak(self):
        self.project["graph_routes"][0]["base_branch"] = "experiment/foo"
        original_config, original_project = copy.deepcopy(self.config), copy.deepcopy(self.project)
        cases = [(["second", "ARBITRARY / LABEL"], "graphs/first.json", "experiment/foo"),
                 (["second"], "second.json", None), ([], "custom-default.json", None),
                 (["unrelated"], "custom-default.json", None)]
        for labels, graph, branch in cases:
            with self.subTest(labels=labels):
                selected = select_executor(self.config, self.project, dict(TASK, labels=labels), self.directory)
                expected = dict(original_config, graph=graph)
                if branch is not None:
                    expected["base_branch"] = branch
                self.assertEqual(selected, expected)
        self.assertEqual(self.config, original_config)
        self.assertEqual(self.project, original_project)
        self.project["graph_routes"].reverse()
        selected = select_executor(self.config, self.project,
                                   dict(TASK, labels=["arbitrary / label", "second"]), self.directory)
        self.assertEqual(selected["graph"], "second.json")
        self.assertNotIn("base_branch", selected)

    def test_all_routes_and_fallback_are_statically_validated_in_workspace(self):
        check_project(self.directory)
        paths = {Path(call.args[0][3]) for call in self.validator.call_args_list}
        self.assertEqual(paths, {self.directory / name for name in
                                ("graphs/first.json", "second.json", "custom-default.json")})
        for call in self.validator.call_args_list:
            self.assertEqual(call.args[0][:3], ["gitweave", "validate", "--graph"])
            self.assertEqual(call.kwargs["cwd"], self.directory)
            self.assertEqual(call.args[2], 30)

    def test_missing_and_invalid_graphs_fail_before_claim_with_injected_backend(self):
        backend = Mock()
        backend.select.return_value = TASK
        (self.directory / "second.json").unlink()
        with self.assertRaisesRegex(Failure, "second.json"):
            claim(self.directory, backend)
        backend.set_status.assert_not_called()
        (self.directory / "second.json").write_text("{}")
        self.validator.side_effect = Failure("transport", "static graph rejected")
        with self.assertRaisesRegex(Failure, "static graph rejected"):
            claim(self.directory, backend)
        backend.set_status.assert_not_called()
        backend.load.assert_not_called()

    def test_fallback_missing_also_fails_setup(self):
        (self.directory / "custom-default.json").unlink()
        with self.assertRaisesRegex(Failure, "custom-default.json"):
            check_project(self.directory)

    def test_symlink_file_and_directory_cannot_escape_workspace(self):
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text(json.dumps(self.worker))
        path = self.directory / "second.json"
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(Failure, "escapes"):
            check_project(self.directory)
        with self.assertRaisesRegex(Failure, "escapes"):
            select_executor(self.config, self.project, dict(TASK, labels=["second"]), self.directory)
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(Failure, "does not exist"):
            check_project(self.directory)
        path.rmdir()
        path.symlink_to(Path(self.tmp.name), target_is_directory=True)
        self.project["graph_routes"][1]["graph"] = "second.json/outside.json"
        (self.directory / "project.json").write_text(json.dumps(self.project))
        with self.assertRaisesRegex(Failure, "escapes"):
            check_project(self.directory)

    def test_runtime_receipt_and_dashboard_use_selected_config_and_graph(self):
        self.project["graph_routes"][0]["base_branch"] = "experiment/foo"
        (self.directory / "project.json").write_text(json.dumps(self.project))
        registry = ExecutionRegistry()
        identity = registry.start("p", TASK, self.directory)
        executor = Mock(return_value=result("finished", {}))
        task = dict(TASK, labels=["second", "arbitrary / label"])
        record = run_task(self.directory, task, executor=executor,
                          observer=lambda event: registry.event(identity, event))
        self.assertEqual(record["status"], "completed", record)
        selected = executor.call_args.args[0]
        self.assertEqual(selected["graph"], "graphs/first.json")
        self.assertEqual(selected["base_branch"], "experiment/foo")
        self.assertEqual(record["executions"][0]["config"], selected)
        execution = registry.snapshot()["runs"][0]["executions"][0]
        self.assertEqual(execution["config"], selected)
        self.assertEqual(execution["graph_path"], "graphs/first.json")
        self.assertEqual(execution["graph"]["nodes"][0]["id"], "work")
        self.assertIsNone(execution["graph_error"])
        # The next Task uses the original fallback; Project routing policies stay separate.
        executor.reset_mock()
        record = Runtime(self.graph, PROJECT, task=task, workspace=str(self.directory), executor=executor).run()
        self.assertEqual(record["status"], "completed")
        self.assertEqual(executor.call_args.args[0]["graph"], "custom-default.json")
        self.assertNotIn("base_branch", executor.call_args.args[0])

    def test_static_failure_prevents_runtime_launch_and_executor_event(self):
        self.validator.side_effect = Failure("transport", "graph rejected")
        executor, observer = Mock(), Mock()
        record = Runtime(self.graph, self.project, task=dict(TASK, labels=["second"]),
                         workspace=str(self.directory), executor=executor, observer=observer).run()
        self.assertEqual(record["failure"]["kind"], "setup")
        self.assertEqual(record["executions"], [])
        executor.assert_not_called()
        self.assertEqual([call.args[0]["type"] for call in observer.call_args_list], ["run_started"])
