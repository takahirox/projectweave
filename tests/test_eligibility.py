import copy
import unittest
from unittest.mock import patch

from projectweave.contracts import Failure
from projectweave.github import GitHub, validate_project


PROJECT = {"owner": "o", "owner_type": "user", "number": 1}
TASK = {"id": "I", "item_id": "ITEM", "project_id": "P", "state": "OPEN",
        "repository": "o/r", "labels": ["task"], "status": "Todo", "is_archived": False,
        "priority": "P1", "created_at": "2026", "url": "https://github.com/o/r/issues/1"}


class EligibilityTests(unittest.TestCase):
    def test_no_permission_field_and_legacy_values_are_ignored(self):
        backend = GitHub(PROJECT)
        for extra in ({}, {"ai_execution": None}, {"ai_execution": "Not ready"}, {"ai_execution": "Ready"}):
            with self.subTest(extra=extra):
                task = dict(TASK, **extra)
                self.assertIs(backend.select([task]), task)

    def test_existing_issue_and_project_state_exclusions(self):
        for extra in ({"state": "CLOSED"}, {"is_archived": True}, {"status": None},
                      {"status": "In Progress"}, {"status": "Done"}, {"status": "Blocked"}):
            with self.subTest(extra=extra):
                self.assertIsNone(GitHub(PROJECT).select([dict(TASK, **extra)]))
        # eligible_statuses can only narrow Todo; it cannot make a failed or completed Task runnable.
        backend = GitHub(dict(PROJECT, eligible_statuses=["Done", "In Progress"]))
        self.assertIsNone(backend.select([TASK, dict(TASK, status="Done"), dict(TASK, status="In Progress")]))

    def test_optional_repository_and_label_rules(self):
        backend = GitHub(dict(PROJECT, repository="O/R", required_labels=["task", "approved"], excluded_labels=["draft", "hold"]))
        task = dict(TASK, labels=["TASK", "Approved", "other"])
        before = copy.deepcopy(task)
        self.assertIs(backend.select([task]), task)
        self.assertEqual(task, before)
        for extra in ({"repository": "o/other"}, {"labels": []}, {"labels": ["approved"]},
                      {"labels": ["task"]}, {"labels": ["task", "approved", "DRAFT"]},
                      {"labels": ["task", "approved", "hold"]}):
            with self.subTest(extra=extra):
                self.assertIsNone(backend.select([dict(task, **extra)]))

    def test_no_hard_coded_labels_and_independent_rules(self):
        self.assertIsNotNone(GitHub(PROJECT).select([dict(TASK, labels=["draft"])]))
        self.assertIsNotNone(GitHub(dict(PROJECT, required_labels=["custom"])).select([dict(TASK, labels=["custom"])]))
        self.assertIsNone(GitHub(dict(PROJECT, excluded_labels=["hold"])).select([dict(TASK, labels=["hold"])]))
        self.assertIsNotNone(GitHub(dict(PROJECT, excluded_labels=["hold"])).select([dict(TASK, labels=[])]))
        self.assertIsNotNone(GitHub(dict(PROJECT, required_labels=[], excluded_labels=[])).select([TASK]))

    def test_label_configuration_validation(self):
        for field in ("required_labels", "excluded_labels"):
            for value in ("task", None, [""], [" "], [4], ["task", "task"]):
                with self.subTest(field=field, value=value), self.assertRaises(Failure):
                    validate_project(dict(PROJECT, **{field: value}))
        with self.assertRaises(Failure):
            validate_project(dict(PROJECT, required_labels=["task"], excluded_labels=["TASK"]))
        with self.assertRaisesRegex(Failure, "migrate manually"):
            validate_project(dict(PROJECT, label="old"))

    def test_load_excludes_nonissues_closed_and_archived_members(self):
        backend = GitHub(PROJECT)
        backend.project_id = "P"
        issue = {"__typename": "Issue", "id": "I", "number": 1, "title": "Task", "body": "body",
                 "url": TASK["url"], "state": "OPEN", "createdAt": "2026", "repository": {"nameWithOwner": "o/r"}}
        items = [{"id": "ITEM", "isArchived": False, "content": issue},
                 {"id": "closed", "isArchived": False, "content": dict(issue, state="CLOSED")},
                 {"id": "archived", "isArchived": True, "content": issue},
                 {"id": "draft", "isArchived": False, "content": {"__typename": "DraftIssue"}},
                 {"id": "pr", "isArchived": False, "content": {"__typename": "PullRequest"}},
                 {"id": "private", "isArchived": False, "content": None}]

        def pages(identity, typename, field, selection):
            if field == "items":
                self.assertEqual((identity, typename), ("P", "ProjectV2"))
                return iter(items)
            if field == "labels":
                return iter([{"name": "task"}])
            return iter([{"field": {"name": "Status"}, "name": "Todo"}])

        with patch.object(backend, "pages", side_effect=pages):
            tasks = backend.load()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["project_id"], "P")
        self.assertNotIn("ai_execution", tasks[0])
        self.assertIs(backend.select(tasks), tasks[0])
        with self.assertRaisesRegex(Failure, "different project"):
            backend.check_task(dict(TASK, project_id="OTHER"), "status")
