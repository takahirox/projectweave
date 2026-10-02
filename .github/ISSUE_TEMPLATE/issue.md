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

Record checks possible only after merge separately, or state that none are required. For a merge-triggered deployment, verify successful publication and the newly published site after merge. Report these checks as pending until performed; do not claim they passed based on pre-merge validation. See the [post-merge verification guidance](https://github.com/takahirox/projectweave/blob/main/docs/development-flow.md#6-verify-after-merge).

## Context

Add any relevant context, examples, logs, or related issues.
