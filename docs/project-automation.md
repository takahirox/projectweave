# Shared Project automation

[Issue #61](https://github.com/takahirox/projectweave/issues/61) adds
[`sync-task-project.yml`](../.github/workflows/sync-task-project.yml) for this
repository's Issues in [takahirox Project #4](https://github.com/users/takahirox/projects/4).
It runs on opening, reopening, closing, labeling and unlabeling, once the
workflow is on the default branch. It does not backfill existing Issues.

| Current Issue | Project action |
| --- | --- |
| Open, `task`, no `draft` | Add if absent; set `AI execution = Ready`; initialize unset Status to `Todo` |
| Closed, missing `task`, or has `draft` | Set `AI execution = Not ready` only if already registered |

Removing `draft` or re-adding `task` restores Ready if eligible. Repeated events
preserve membership, any set Status (including `In Progress` and `Done`), Priority,
and other fields. Pull requests are ignored. No `Pending` option is introduced.
Actions only synchronizes Project fields; the continuously running ProjectWeave
`coordinate` process admits open, Todo, Ready Tasks under its resource policy.
Not ready prevents future admission; it does not cancel running work, gate merges,
or make GitWeave read Project permissions.

Runs are serialized per Issue without canceling active writes. Each run reads
current state and all label pages instead of trusting the triggering event,
paginates Project fields and Issue Project membership, and checks state again
before and after writes. Changed snapshots cause reconciliation again; after
five unstable passes it leaves existing membership Not ready and fails with a
rerun instruction. GitHub reads and mutations are not atomic: a concurrent Issue
edit can briefly precede its permission update. A later queued run reads current
state even if GitHub delivers events out of order or replaces a pending run.
Set Status values are read immediately before initialization; external Project
writers are not covered by the Actions concurrency lock.

## Owner setup

Automation is **inactive until `ADD_TO_PROJECT_PAT` is configured**. A missing
secret fails with an actionable error. Provisioning is a separate owner action;
never put a token in repository files, Issue text, logs, or a PR.

1. As the Project owner, open GitHub **Settings → Developer settings → Personal
   access tokens → Tokens (classic) → Generate new token (classic)**. Choose an
   expiration and the **`project`** scope (user-Project read/write). This is the
   documented classic PAT option for these public repositories; no additional
   private-repository scope is needed here. The ordinary `GITHUB_TOKEN` cannot
   access Projects. See GitHub's [Actions authentication guide](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions)
   and [Project API guide](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects).
2. Register the new token as the **Actions repository secret**
   `ADD_TO_PROJECT_PAT` in **both** `takahirox/projectweave` and
   `takahirox/gitweave`: repository **Settings → Secrets and variables → Actions
   → New repository secret**. The other repository's workflow is a separate task.
   Renew the expiring token and update both secrets before expiration.
3. In Project #4, check single-select `AI execution` has `Ready` and `Not ready`,
   and `Status` has `Todo`, `In Progress`, and `Done`. Existing fields/options are
   used; this workflow does not create them.
4. In Project #4's **Workflows**, enable the built-in [**Item closed** workflow](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-built-in-automations)
   and confirm it sets Status to **Done** for closed Issues. This Actions workflow
   preserves Status on closure and retains membership. `Done` alone does not
   prove implementation: inspect the Issue's **Completed / Not planned** reason.
   An unregistered closed Issue is never added by this workflow.

## Live verification after provisioning

Use disposable Issues and pause the coordinator or use a controlled resource
policy during these checks: an eligible test Issue is automatically executable.
Wait for each Actions run before inspecting Project #4.

1. Create open Issues with `task`, with `task` + `draft`, and without `task`.
   Only the first should appear, exactly once, with Todo and Ready. Close an
   unregistered ineligible Issue with `task` + `draft`, then remove `draft` while
   closed: it must remain absent.
2. Remove `draft` from the open draft Task: it must appear once, Todo and Ready.
   Trigger repeated matching events (e.g. add/remove an unrelated label) and
   verify one item, unchanged Status and Priority.
3. On registered items, test each Status (`Todo`, `In Progress`, `Done`) with a
   set Priority. Remove/re-add `task`, then add/remove `draft`. Permission must
   alternate Not ready/Ready while Status and Priority stay unchanged. Clear
   Status on an eligible item and trigger a matching event: it becomes Todo.
4. Close registered Tasks with each closure reason. Permission becomes Not ready,
   membership remains, and the built-in workflow sets Done. Reopen an eligible
   Task: Ready returns but Done is preserved by this Actions workflow.
5. Rapidly add/remove both labels; rerun an older Actions run after the final
   change. Final permission must match current eligibility, without duplicates
   or resetting Status. Inspect any separate Project automation if Status changes.

Record local validation and these live results (or provisioning blockers) in the
PR for #61 before review. Do not treat mocked checks as live verification.
