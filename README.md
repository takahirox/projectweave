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
Then add an Issue to the Project, set its `AI execution` field to `Ready` and its
Status to `Todo`, and run either:

```sh
projectweave run app       # one Task: admission -> claim -> run-task
projectweave coordinate    # all Projects, concurrently, until stopped (--once for one pass)
```

For this repository's shared Project #4, see [Project automation setup](docs/project-automation.md)
to configure automatic task addition and Ready permission from Issue state and labels.

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
  implement → open a PR (`Closes #N`) → review ⇄ fix until approved (including
  conflicts with the current default branch) → merge with a merge commit, going
  back to review and retrying if the merge fails → make sure the Issue is closed
  and comment the outcome on it.
  **It merges without a human review** once the review agent approves; edit it
  if you want a human to merge.

[examples/](examples/) holds specialized feature examples such as the
[review/fix loop](examples/review-fix.json) and a manual
[project.json](examples/project.json); they are not alternative defaults.
The runtime never changes provider in response to an executor failure.

See the [minimal runtime design](docs/runtime.md) and
[CLI and executor guide](docs/usage.md) for graph fields, authentication,
resource guarantees, status updates, and failure behavior. Tests use deterministic
external CLI doubles; no live GitHub mutation or AI execution was used to verify
this implementation.
