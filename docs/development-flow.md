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

Require post-merge verification only when the actual change needs it; otherwise state that none is required. Documentation-only work needs the Markdown checks described below, without routine manual approval, physical-device checks, heavyweight model downloads, or deployment gates.

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

Choose validation using the [test and CI principles](#test-and-ci-principles).

A Pull Request should use the normal `Closes #N` reference when it fully implements the Issue and satisfies applicable mandatory pre-merge acceptance. Pending post-merge-only verification does not make an otherwise complete implementation partial.

If the Pull Request intentionally implements only part of the Issue, it must state the remaining scope and must not automatically close the parent Issue. Use a non-closing reference instead.

## 3. Review Before Merge

Every Pull Request should be reviewed before merge.

A central review question is:

> Does this Pull Request address the Issue completely, without adding changes that are not justified by the Issue?

Review must check both directions:

- **No missing scope:** the Pull Request should not leave required parts of the Issue unresolved while claiming completion.
- **No unnecessary scope:** the Pull Request should not introduce unrelated abstractions, frameworks, policies, or complexity beyond what is needed to solve the Issue.

This is especially important for AI-generated changes. AI agents may produce broader or more elaborate designs than the task requires. Prefer the smallest change that fully satisfies the Issue.

Apply the [review guidelines](review-guidelines.md#check-validation-timing): require the implementation and pre-merge acceptance criteria to be satisfied, and confirm that required post-merge verification is recorded separately. Pending checks that can only run after merge do not block pre-merge approval.

Do not request removal of `Closes #N`, keep the source Issue open, or withhold approval solely because post-merge-only verification is pending. ProjectWeave publication adds this reference and its merge/close stages close the Issue; opposing those actions during review causes a repeated Review → Fix → Publish → Review loop without resolving any implementation defect.

## 4. Revise Until Review Passes

If review finds missing requirements, unnecessary scope, correctness problems, or insufficient validation, update the Pull Request and review it again.

When a required pre-merge decision or confirmation can only come from a human, request
the specific result on the Issue and wait for an update. Re-review the response;
an unrelated or incomplete reply leaves the request outstanding. If a code
defect also remains, fix and publish it first, then assess the human check
against the updated PR head. Repeated documentation edits cannot supply a
missing human result. Closure of the source Issue before merge stops the merge path.

The Pull Request should be merged only when the reviewed change is an appropriate and complete response to the Issue.

## 5. Merge and Close the Issue

After review passes and applicable mandatory pre-merge checks, required protections, and genuinely necessary pre-merge human acceptance are satisfied, merge the Pull Request and close the fully implemented source Issue. Do not bypass these requirements. Close the Issue after successful merge, before performing required post-merge verification; closure does not claim that pending checks passed.

## 6. Verify After Merge

Perform any required post-merge verification recorded in the Issue, such as checking the merge-triggered deployment and the newly published site. Record actual results and evidence in the merged PR, closed Issue, or a linked validation report, identifying the affected revision and checks performed. Report unperformed, pending, and environment-blocked checks honestly, including the reason they could not run; merge and Issue closure do not establish that verification passed. An unavailable test environment alone is not evidence of a product defect.

If verification finds an actual defect, check for an existing equivalent Issue first. Use that Issue or create a new linked Issue with reproduction steps, the affected revision, expected and actual behavior, and links to the original Issue and PR. Do not keep or reopen the original Issue solely for post-merge follow-up.

This lifecycle supersedes older instructions to keep a fully implemented Issue open solely for post-merge verification. Preserve historical test outcomes and evidence, including pending or blocked results; do not rewrite them as passed.

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
Merge and close the original Issue
  ↓
Post-merge verification (when required)
```

## Test and CI Principles

**Detect the required failures at the lightest appropriate layer, without redundant execution.** Preserve required behavior, regression confidence, and meaningful integration coverage when redistributing checks.

ProjectWeave currently has Python `unittest` checks, browser-free Node.js layout tests, and Playwright dashboard tests in Chromium at desktop and narrow viewports. The [README](../README.md#run-the-first-runtime) lists test and graph-validation commands; the [verification guide](usage.md#verification) explains deterministic CLI fixtures, optional local GitWeave checks, and the limits of simulated execution. No CI workflow is checked into this repository; the CI guidance below applies to future additions.

1. **Prefer the lightest meaningful layer.** Test calculations, branches, graph contracts, and broad data combinations in Python or browser-free layout tests where appropriate. Use subprocess integration tests for CLI boundaries and Playwright for actual dashboard interaction, rendering, focus, and responsive behavior. Add tests that catch a concrete defect rather than mirror implementation. Live executor/GitHub validation remains an operator action, as described in the verification guide.

2. **Choose combinations deliberately.** Cover distinct executor, provider, graph/control-flow, and dashboard viewport paths with representative cases. Expand combinations exhaustively where interactions require it, rather than multiplying every provider, graph state, and viewport. Preserve required regressions and meaningful boundary checks when moving coverage between layers.

3. **Match validation to the change.** Documentation-only changes need Markdown structure, link/anchor, consistency, and whitespace checks (`git diff --check`). UI changes need relevant layout/browser and dashboard-serving Python checks; core/runtime changes need applicable Python regressions and graph validation when graph contracts change. Build/packaging or future CI changes need applicable configuration, build/install, and affected test checks. Use focused checks for fast feedback and applicable full suites for regression confidence before merge. Keep mandatory pre-merge checks applicable to the change; optional local GitWeave checks remain optional unless the Issue requires them. Record genuinely post-merge-only publication/deployment verification separately under the existing validation-timing guidance, pending until performed.

4. **Reuse work and avoid duplicate execution.** Reuse browser downloads, dependency installation, builds, and fixture setup where safe, with cache keys matching relevant versions/lockfiles and isolation between tests. `npm test` already includes `test:layout`; avoid repeating that work for the same validation. For future CI, avoid equivalent push/PR runs for the same revision, cancel superseded runs when safe, and preserve necessary deployment ordering. Parallelize independent work based on measured resource use; more workers do not necessarily improve feedback time.

5. **Review value and cost together.** Identify the additional defect a new or expanded test detects, whether existing coverage supplies that assurance, and its execution and maintenance cost. Watch repeated browser/server/subprocess setup, large fixtures, long polling simulations, and Cartesian-product growth. Retain meaningful assertions and required coverage when reducing duplication.

6. **Maintain an execution-time budget.** No measured test/CI timing baseline or normal-CI target is currently documented. Establish a repository-appropriate baseline and target during actual test/CI performance work, monitor growth, and investigate overruns. Track elapsed feedback time, total runner/computation time, and queue time separately. Revisit validation structure, setup, and duplication as costs grow while preserving confidence. This guidance adds no benchmark, hardware, or merge gate.

Review question:

> Does this change provide sufficient confidence with the lightest appropriate validation, without redundant execution?
