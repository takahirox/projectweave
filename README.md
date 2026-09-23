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

The runtime executes JSON graphs with `agent` and `action` nodes, sequential flow, structured result handoff, `if` routing, and explicit post-condition `loop` control bounded by `max_steps`. It runs once from the CLI; concurrent execution is outside this release.

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
Use `python3 -m projectweave` from this checkout, or install with
`python3 -m pip install .` to use the `projectweave` command.

```sh
python3 -m projectweave validate --graph examples/dispatch.json
python3 -m projectweave validate --graph examples/review-fix.json
python3 -m unittest discover -s tests -v
```

A ProjectWeave Project is a GitHub Project whose Tasks are repository Issues
from one or more repositories. For first-time setup, create a Project workspace
directory and select an existing GitHub Project:

```sh
mkdir my-project && cd my-project
projectweave init --project-owner my-team --project-number 7
```

Or explicitly create one with `--create-project "First Run"`. Init writes
human-editable files into the workspace without overwriting existing files; Task
repositories are cloned into its `repos/` only when a Run executes their Issues.
Capacity starts at zero and provider/model configuration remains a human action.
Follow the printed steps to add a chosen Issue to the Project, set its
`AI execution` field to `Ready`, and run. See [initialization and recovery](docs/usage.md#initialize-a-project-workspace)
for prerequisites, compatibility rules, and live Run limitations.

For manual setup, configure the project, resource amounts, and executor paths in
[examples/dispatch.json](examples/dispatch.json),
[examples/project.json](examples/project.json), and
[examples/resources.json](examples/resources.json), then run:

```sh
projectweave run --graph examples/dispatch.json \
  --project examples/project.json --resources examples/resources.json > run.json
```

The example selects the highest-priority eligible open Issue, checks primary
executor capacity, uses an instruction-driven command executor or GitWeave,
and comments the structured outcome on the Issue. The primary executor starts
with zero capacity, so only the GitWeave path needs configuring initially.
Supply a wrapper implementing the JSON contract to enable the primary path.
The runtime never changes provider in response to an executor failure.

See the [minimal runtime design](docs/runtime.md) and
[CLI and executor guide](docs/usage.md) for graph fields, authentication,
resource guarantees, status updates, and failure behavior. Tests use deterministic
external CLI doubles; no live GitHub mutation or AI execution was used to verify
this implementation.
