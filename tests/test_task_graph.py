import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TaskGraphTests(unittest.TestCase):
    def setUp(self):
        self.graph = json.loads((ROOT / "projectweave/templates/gitweave.json").read_text())
        self.nodes = self.graph["nodes"]
        self.fixtures = json.loads((ROOT / "tests/fixtures/task-results.json").read_text())

    def test_codex_schemas_require_every_property_recursively(self):
        def check(schema, path):
            if schema.get("type") == "object":
                with self.subTest(path=path):
                    self.assertCountEqual(schema.get("required", []), schema["properties"])
                    self.assertIs(schema["additionalProperties"], False)
                for key, child in schema["properties"].items():
                    check(child, f"{path}.{key}")
            if "items" in schema:
                check(schema["items"], f"{path}[]")

        for name, node in self.nodes.items():
            if node.get("provider") == "codex" and "schema" in node:
                check(node["schema"], name)

    def test_assessment_schemas_and_every_handoff_keep_command_baselines(self):
        agents = {"readiness", "diagnose", "review"}
        for name in agents:
            self.assertFalse({"snapshot", "revision"} & self.nodes[name]["schema"]["properties"].keys())
            self.assertNotIn("snapshot exactly unchanged", self.nodes[name]["instruction"])
        for name in ("issue_snapshot", "ask_information", "wait_for_issue_update", "check_issue_open",
                     "review_snapshot", "request_confirmation", "wait_for_confirmation"):
            self.assertIn("revision", self.nodes[name]["schema"]["required"])
        seen = []
        def check(flow):
            for step in flow:
                if isinstance(step, str):
                    self.assertNotIn(step, agents, "Assessment must preserve Command input in another branch")
                    continue
                if "parallel" in step:
                    branches = step["parallel"]
                    self.assertEqual(len(branches), 2)
                    self.assertIn(branches[0][0], agents)
                    seen.append(branches[0][0])
                    identity = branches[1][0]["if"]
                    self.assertEqual(identity["then"], [])
                    self.assertEqual(identity["else"], [])
                elif "if" in step:
                    check(step["if"]["then"])
                    check(step["if"]["else"])
                else:
                    check(step["loop"]["flow"])
        check(self.graph["flow"])
        self.assertCountEqual(seen, ["readiness", "diagnose", "review", "review", "review"])

    def assert_result_matches(self, schema, result):
        # Only the object/string/integer/boolean types used by these result fixtures.
        if schema["type"] == "object":
            self.assertIsInstance(result, dict)
            self.assertTrue(set(schema["required"]).issubset(result))
            if schema["additionalProperties"] is False:
                self.assertTrue(set(result).issubset(schema["properties"]))
            for key, value in result.items():
                self.assert_result_matches(schema["properties"][key], value)
        else:
            types = {"string": str, "integer": int, "boolean": bool}
            self.assertIs(type(result), types[schema["type"]])

    def test_merge_results_include_a_required_string(self):
        for name in ("merge", "close_issue"):
            schema = self.nodes[name]["schema"]
            self.assertEqual(schema["properties"]["merge_commit"]["type"], "string")
            self.assertIn("merge_commit", schema["required"])
            for outcome, fixture in self.fixtures.items():
                with self.subTest(node=name, outcome=outcome):
                    result = dict(fixture)
                    if name == "merge":
                        del result["closed"]
                    else:
                        del result["retry"]
                    self.assert_result_matches(schema, result)
                    if result["merged"]:
                        self.assertRegex(result["merge_commit"], r"^[0-9a-f]{40}$")
                    else:
                        self.assertEqual(result["merge_commit"], "")
                    del result["merge_commit"]
                    with self.assertRaises(AssertionError):
                        self.assert_result_matches(schema, result)
                    result["merge_commit"] = None
                    with self.assertRaises(AssertionError):
                        self.assert_result_matches(schema, result)

    def test_merge_retry_contract_and_loop_condition(self):
        schema = self.nodes["merge"]["schema"]
        self.assertEqual(schema["properties"]["retry"]["type"], "boolean")
        self.assertIn("retry", schema["required"])
        condition = self.graph["flow"][2]["if"]["then"][2]["loop"]["while"]
        self.assertEqual(condition, {"path": "/0/data/retry", "equals": True})
        for outcome, expected_retry in (("successful_merge", False), ("unsuccessful_merge", True),
                                        ("closed_before_merge", False)):
            with self.subTest(outcome=outcome):
                result = dict(self.fixtures[outcome])
                del result["closed"]
                self.assert_result_matches(schema, result)
                self.assertIs(result["retry"], expected_retry)
                del result["retry"]
                with self.assertRaises(AssertionError):
                    self.assert_result_matches(schema, result)

    def test_merge_instructions_guard_issue_and_preserve_github_policy(self):
        instruction = self.nodes["merge"]["instruction"]
        for text in ("Immediately before each attempt to merge", "github_repository and run_input.number",
                     "If the Issue is closed, do not merge the PR", "merged=false, retry=false",
                     "its open state cannot be confirmed, stop without merging",
                     "its head is still inputs[0].data.pr.head_sha", "with a merge commit",
                     "Do not bypass required checks or reviews", "retry=false when merged is true",
                     "retry=true only when the Issue was confirmed open"):
            with self.subTest(text=text):
                self.assertIn(text, instruction)
        self.assertIn("never reopen a closed Issue", self.nodes["close_issue"]["instruction"])

    def test_instructions_and_descriptions_agree_on_merge_commit(self):
        for name in ("merge", "close_issue"):
            node = self.nodes[name]
            for text in (node["instruction"], node["schema"]["properties"]["merge_commit"]["description"]):
                with self.subTest(node=name, text=text):
                    self.assertIn("actual merge commit SHA when merged is true", text)
                    self.assertIn('empty string "" when merged is false', text)
                    self.assertNotIn("when present", text)
                    self.assertNotIn("omit", text)
        self.assertIn("always forward pr, merged and merge_commit from inputs[0].data unchanged",
                      self.nodes["close_issue"]["instruction"])
