---
name: Issue
about: Report a problem or propose a change
title: ""
labels: ""
assignees: ""
---

## Problem

Describe the problem.

## Expected outcome

Describe what should be true when the issue is resolved.

Default to completion criteria an AI agent can execute and verify. Require human checks only when necessary; explain why and the expected result, and distinguish optional validation from mandatory criteria. See the [development guidance](https://github.com/takahirox/projectweave/blob/main/docs/development-flow.md#1-start-with-an-issue).

### Pre-merge acceptance criteria

List mandatory acceptance criteria that are achievable and verifiable before merge. Checks possible only after merge must not be prerequisites for pre-merge PR approval. Preserve all implementation requirements and applicable pre-merge tests.

For a merge-triggered deployment, validate the code and configuration, local builds, and applicable automated tests before merge.

## Required post-merge verification

Record checks needed for this change that are possible only after merge, or state that none are required. Documentation-only work does not require routine post-merge gates. For a merge-triggered deployment, verify successful publication and the newly published site after merge. Report pending or environment-blocked checks honestly; do not claim they passed based on pre-merge validation or Issue closure.

Follow the [development lifecycle](https://github.com/takahirox/projectweave/blob/main/docs/development-flow.md#5-merge-and-close-the-issue): merge and close the fully implemented Issue after pre-merge acceptance, then record required post-merge evidence. Track discovered defects in a new linked Issue after checking for an equivalent one; do not reopen this Issue solely for follow-up. See the [evidence and follow-up guidance](https://github.com/takahirox/projectweave/blob/main/docs/development-flow.md#6-verify-after-merge).

## Context

Add any relevant context, examples, logs, or related issues.
