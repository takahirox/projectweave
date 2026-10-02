# Development Flow

This document defines the default development flow for ProjectWeave, with particular emphasis on AI-assisted development.

## 1. Start with an Issue

Work should begin with an Issue.

The Issue should clearly state:

- the problem
- the expected outcome
- relevant context

The Issue defines the scope of the work. If the scope is unclear, clarify the Issue before implementation instead of inventing requirements during the change.

By default, completion criteria should be executable and verifiable by an AI agent. Require human checks, such as physical-device testing, subjective evaluation, or external approval, only when there is a necessary reason to do so.

When human work is required, state why it is necessary and what result is expected. Distinguish optional additional validation from mandatory completion criteria.

Mandatory pre-merge acceptance criteria must be achievable and verifiable before merge. Record required checks possible only after merge separately as post-merge verification; they must not be prerequisites for pre-merge PR approval. This distinction preserves all implementation requirements and applicable pre-merge tests.

For example, when merge triggers a deployment, validate the code and configuration, local builds, and applicable automated tests before merge. Verify successful publication and the newly published site after merge. Report required post-merge verification as pending until performed.

Use the [Issue template](../.github/ISSUE_TEMPLATE/issue.md) to record the two stages separately.

## 2. Create a Pull Request for the Issue

Implementation should be proposed through a Pull Request associated with the Issue.

The Pull Request should explain:

- what changed
- what outcome the change produces
- how the change was validated
- which Issue it addresses

Report completed pre-merge validation and pending required post-merge verification separately. Include the checks and results actually obtained; do not present pending verification as passed or the deployed outcome as verified.

A Pull Request should only claim to close an Issue when it fully addresses that Issue.

If the Pull Request intentionally implements only part of the Issue, it should state that clearly and should not present the Issue as fully resolved.

## 3. Review Before Merge

Every Pull Request should be reviewed before merge.

A central review question is:

> Does this Pull Request address the Issue completely, without adding changes that are not justified by the Issue?

Review must check both directions:

- **No missing scope:** the Pull Request should not leave required parts of the Issue unresolved while claiming completion.
- **No unnecessary scope:** the Pull Request should not introduce unrelated abstractions, frameworks, policies, or complexity beyond what is needed to solve the Issue.

This is especially important for AI-generated changes. AI agents may produce broader or more elaborate designs than the task requires. Prefer the smallest change that fully satisfies the Issue.

Apply the [review guidelines](review-guidelines.md#check-validation-timing): require the implementation and pre-merge acceptance criteria to be satisfied, and confirm that required post-merge verification is recorded separately. Pending checks that can only run after merge do not block pre-merge approval.

## 4. Revise Until Review Passes

If review finds missing requirements, unnecessary scope, correctness problems, or insufficient validation, update the Pull Request and review it again.

When a required pre-merge decision or confirmation can only come from a human, request
the specific result on the Issue and wait for an update. Re-review the response;
an unrelated or incomplete reply leaves the request outstanding. If a code
defect also remains, fix and publish it first, then assess the human check
against the updated PR head. Repeated documentation edits cannot supply a
missing human result. Closure of the source Issue stops the merge path.

The Pull Request should be merged only when the reviewed change is an appropriate and complete response to the Issue.

## 5. Merge

After review passes, merge the Pull Request.

## 6. Verify After Merge

Perform any required post-merge verification recorded in the Issue, such as checking the merge-triggered deployment and the newly published site. Keep each check pending until performed, then report its actual result and evidence. Report failed checks and any required follow-up accurately; merge alone does not establish that verification passed.

The normal flow is therefore:

```text
Issue
  ↓
Implementation
  ↓
Pull Request
  ↓
Review
  ↓
Revision if needed
  ↓
Merge
  ↓
Post-merge verification (when required)
```
