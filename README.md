# ProjectWeave

ProjectWeave is a project-level orchestration foundation for coordinating AI-driven work within a single project.

It operates one level above individual task execution. Rather than defining how an agent implements a task internally, ProjectWeave focuses on how project work is selected, prepared, dispatched to executors, constrained by available AI resources, and connected to subsequent decisions.

## Why ProjectWeave

AI agents are increasingly capable of completing well-defined tasks such as coding, research, review, and documentation.

A project has a broader coordination problem:

- which work should be processed next
- how work should be prepared before execution
- which executor should handle it
- how limited AI resources should constrain execution
- how results should feed into the next project-level action

ProjectWeave provides infrastructure for expressing and executing that control logic without hard-coding a single project-management strategy.

## Project-level workflows

A ProjectWeave workflow may be as simple as:

```text
Task
  ↓
Executor
```

or include additional project-level processing:

```text
Task
  ↓
Analyze
  ↓
Execute
  ↓
Evaluate
```

The important property is that the workflow is explicit and configurable.

The runtime executes JSON graphs with `agent` and `action` nodes, sequential flow, structured result handoff, `if` routing, and explicit post-condition `loop` control bounded by `max_steps`. Each `run-task` executes one graph for one claimed Task; `projectweave coordinate` runs claimed Tasks concurrently across and within Projects, each Task still one bounded graph Run.

## AI resources as constraints

ProjectWeave treats AI resources as first-class inputs and constraints.

Examples include:

- subscription usage limits
- per-model quotas
- API budgets
- time-window-based usage limits

A project may receive a resource envelope such as:

```text
Codex: 20% of weekly allowance
Claude: 30% of weekly allowance
API budget: $25
```

These limits may be enforced as hard constraints.

Resource state can also participate in project-level decision making. For example, a workflow may choose work suitable for an executor with abundant remaining capacity while preserving a scarce resource for tasks where it is more valuable.

ProjectWeave provides the resource visibility and enforcement primitives; the allocation strategy itself belongs to workflows, policies, AI systems, humans, or other external control logic.

## Executor-independent

ProjectWeave is not tied to a specific agent or execution system.

Possible executors include:

- Codex
- Claude
- other AI agents
- multi-agent execution systems
- API-backed executors
- local executors

ProjectWeave connects project-level work to executors rather than reimplementing their internal task-solving capabilities.

## Reuse existing project infrastructure

ProjectWeave should reuse existing project and source-control systems wherever practical instead of rebuilding them.

For a project hosted on GitHub, existing primitives may already provide:

```text
Issue         → Task
Priority      → Work priority
Project field → Eligibility (`AI execution`)
Label         → Classification
Pull Request  → Proposed result
Git           → Artifacts and history
```

ProjectWeave should stay focused on the control layer between project-management infrastructure, AI resource constraints, and executors.

## Intelligence and infrastructure are separate

ProjectWeave is not itself an autonomous project-manager AI.

Questions such as which task to prioritize, which executor to use, how to allocate resources, when to create additional work, or which workflow to run may be decided by:

- humans
- AI systems
- policies
- workflows
- external software

ProjectWeave provides the infrastructure needed to observe state, apply constraints, execute project-level workflows, dispatch work, and observe results.

## Long-term direction

The long-term goal is to make continuous project-operation loops possible:

```text
Observe
  ↓
Decide
  ↓
Execute
  ↓
Observe
  ↺
```

ProjectWeave does not need to decide the optimal loop itself. It should provide a small, understandable, extensible foundation for building such loops on top of project state, available work, resource constraints, and heterogeneous executors.

With appropriate intelligence and policy layered on top, ProjectWeave should make it possible to build systems that continuously advance a project without requiring a human to manually direct every individual AI task.

## Design principles

- Operate at the project level, not inside individual task execution.
- Keep project-level workflows explicit and configurable.
- Treat graphs as a strong candidate for workflow representation.
- Treat AI resources as first-class inputs and constraints.
- Allow resource limits to be enforced as hard limits.
- Keep resource-allocation strategy outside the core.
- Remain independent of specific executors.
- Reuse existing Git and project-management infrastructure where practical.
- Support both human and AI-driven control logic.
- Separate intelligence from infrastructure.
- Keep the core small, understandable, and extensible.

See [Issue #1](https://github.com/takahirox/projectweave/issues/1) for the full vision and design discussion.


## Run the first runtime

Requires Python 3.11+ on Linux or macOS. No Python runtime dependencies.

For developing or dogfooding ProjectWeave from a checkout, use an editable
install in a virtual environment:

```sh
cd /path/to/projectweave
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
projectweave --help
```

- `python3 -m MODULE` runs the named module with that Python interpreter
  (for example `python3 -m projectweave` works from the checkout without installing).
- `pip install -e .` is an editable install: the `projectweave` command uses the
  checkout's source, so pulled or edited code takes effect without reinstalling
  in the normal case.
- Reinstall (`python3 -m pip install -e .`) after packaging or dependency
  metadata changes, such as edits to `pyproject.toml`.
- The virtual environment keeps ProjectWeave out of your system Python; run
  `source .venv/bin/activate` in new shells before using `projectweave`.

A non-editable `python3 -m pip install .` copies the current state and needs
reinstalling after each update.

Validate the canonical and example graphs and run the tests:

```sh
python3 -m projectweave validate --graph projectweave/templates/graph.json
gitweave validate --graph projectweave/templates/gitweave.json
python3 -m projectweave validate --graph examples/review-fix.json
python3 -m unittest discover -s tests -v
```

A ProjectWeave Project is a GitHub Project whose Tasks are repository Issues
from one or more repositories. One root workspace manages any number of Projects
under `projects/<name>/`:

```sh
mkdir workspace && cd workspace
projectweave init
projectweave init-project app --project-owner my-team --project-number 7
projectweave init-project api --project-owner my-team --create-project "API" --resource codex:20:5
```

`init-project` also takes `--link-repository OWNER/REPO` (repeatable) and
`--provider claude` (which also enables Claude's `bypassPermissions` mode: the
agent edits files and runs commands without asking) or `--model MODEL`; the
default is Codex with its native default model. Init never overwrites files.
Choose how Issues enter the Project and become executable:

- **Manual:** add an open Issue, then manually set Status to `Todo` and
  `AI execution = Ready`.
- **GitHub built-in auto-add:** use one Project per repository and configure one
  auto-add workflow in each Project for its repository. Auto-add handles
  membership; manually set Status to `Todo` and `AI execution = Ready` afterward.

See [Issue onboarding](docs/usage.md#onboard-issues) for setup and verification.
Init does not configure Project workflows or mark Issues Ready. Labels and Issue
closure do not automatically synchronize `AI execution`; after completion you
can manually set it to `Not ready`. Closed Issues are not executable, even if
`AI execution` remains `Ready`. Verify the Project's built-in **Item closed**
workflow sets Status to `Done`, or use `projectweave complete` explicitly.

ProjectWeave never sets Ready itself: `coordinate` selects open, Todo, Ready
Issues under the configured resource policy. No Pending Status is needed.
Not ready does not stop an active execution; cancellation and pre-merge
eligibility gates are deferred.

After onboarding and verification, run either:

```sh
projectweave run app       # one Task: admission -> claim -> run-task
projectweave coordinate    # all Projects, concurrently, until stopped (--once for one pass)
```

ProjectWeave owns Project lifecycle: `claim` selects one runnable Task and sets it
`In Progress` under a per-Project lock, `run-task` runs the Project graph for a
claimed Task, and `complete` sets `Done` explicitly (by default GitHub does that
when the Issue closes). Shared AI usage is observed directly (Claude `/usage`,
Codex app-server) and admitted per Project with **opt-in** limits in the root
`projectweave.json` (`init-project --resource PROVIDER:MIN:ESTIMATE`); a Project
without limits is not constrained. See [the CLI guide](docs/usage.md) for
prerequisites, admission rules, and live Run limitations.

Each Project workspace holds two packaged templates:

- [projectweave/templates/graph.json](projectweave/templates/graph.json), the
  required **Project graph**: here just run GitWeave for the claimed Task
  (`--repo OWNER/REPO --issue N`). ProjectWeave posts no comment itself.
- [projectweave/templates/gitweave.json](projectweave/templates/gitweave.json),
  the **GitWeave Task graph** (how one selected Issue is carried to a merge):
  review Issue readiness → ask for missing information and wait for meaningful
  Issue updates until ready → implement → open a PR (`Closes #N`) → review ⇄ fix
  until approved (including conflicts with the current default branch) → merge with a merge commit, going
  back to review and retrying if the merge fails → make sure the Issue is closed
  and comment the outcome on it.
  **It merges without a human review** once the review agent approves; edit it
  if you want a human to merge.

Readiness uses `github_repository` and `run_input.number`. Command nodes read
the Issue, post deduplicated questions, and poll without invoking an AI model:
every minute for the first hour, every five minutes until 24 hours, then hourly
without a cutoff. Title/body changes and external comment additions, edits or
deletions trigger another readiness review; the automation's marked comments
and metadata-only updates do not. The reviewed snapshot is preserved so replies
during review are not missed. An open-state check immediately before implementation
also re-reviews any intervening content changes. Closure before implementation
ends the graph with schema-validated `status: "closed"`, `questions`, and
`snapshot` data, with no PR. The resident waiting command holds the Task's
resource reservation; coordinator shutdown and `--once` wait for it. Restart
recovery is out of scope. The generated Project executor uses `timeout: null`
to allow this wait; omitted executor timeouts still default to 3600 seconds.

After implementation, the Task graph's terminal `close_issue` data always
includes `pr`, `merged`,
`merge_commit`, and `closed`. `merge_commit` is a required string: the actual
merge commit SHA when `merged` is true, or `""` when it is false. `close_issue`
forwards `pr`, `merged`, and `merge_commit` unchanged from `merge` and reports
whether the Issue is closed. Every object property in the packaged Codex output
schemas is required.

### Repairing existing workspace graphs

Init never overwrites existing files, so updating ProjectWeave or rerunning
`init-project` does not repair an existing `projects/weave/gitweave.json`.
For the readiness scaffold, regenerate both `graph.json` and `gitweave.json`
from the current packaged templates, materialize the GitWeave graph path in
`graph.json`, and reapply your provider/model/instruction choices. Preserve
`project.json` and root resource policies. Init reports older scaffolds as
incompatible and never rewrites them. Ensure the Project executor has
`timeout: null` and the waiting command has no GitWeave node or graph timeout;
a finite timeout would terminate the wait.

For older merge result schemas, edit that file (or the equivalent path for
your Project) before dispatching Tasks, preserving your other settings:

1. In both `nodes.merge.schema.required` and
   `nodes.close_issue.schema.required`, add `"merge_commit"`. Keep
   `properties.merge_commit.type` as `"string"`.
2. In both `merge_commit` schema descriptions and in the `merge` instruction,
   require the actual merge commit SHA when `merged` is true and the empty
   string `""` when it is false. Remove any wording permitting omission.
3. In the `close_issue` instruction, replace the conditional forwarding of
   `merge_commit` with always forwarding `pr`, `merged`, and `merge_commit`
   unchanged, including the empty string on an unsuccessful merge.
4. Run `gitweave validate --graph projects/weave/gitweave.json` and inspect
   each Codex output schema recursively: every object property's name must be
   in `required`. Static GitWeave validation alone does not check this Codex
   requirement. The packaged regression tests check it without live AI calls
   or GitHub mutations.

Keep the repair Task Not ready until the bootstrap workspace graph is fixed,
or implement the repair through a separate manual development flow. Do not
repeatedly dispatch the unchanged graph. Failed Tasks stay In Progress and are
not automatically retried. Before retrying ProjectWeave #61, GitWeave #100, or
ProjectWeave #63, inspect their existing PRs and checkpoints and reuse existing
work as appropriate to avoid duplicate PRs. Retrying these Tasks is a separate
operational follow-up.

[examples/](examples/) holds specialized feature examples such as the
[review/fix loop](examples/review-fix.json) and a manual
[project.json](examples/project.json); they are not alternative defaults.
The runtime never changes provider in response to an executor failure.

See the [minimal runtime design](docs/runtime.md) and
[CLI and executor guide](docs/usage.md) for graph fields, authentication,
resource guarantees, status updates, and failure behavior. Tests use deterministic
external CLI doubles; no live GitHub mutation or AI execution was used to verify
this implementation.
