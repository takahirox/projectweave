import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TaskGraphTests(unittest.TestCase):
    def setUp(self):
        self.nodes = json.loads((ROOT / "projectweave/templates/gitweave.json").read_text())["nodes"]
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
            if node["provider"] == "codex" and "schema" in node:
                check(node["schema"], name)

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
