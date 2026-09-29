# First runtime: minimal design (Issue #4)

Python 3.11+ and the standard library; no task database. GitHub Projects v2 is
canonical. JSON Run output is an execution receipt, not a second project state
store. The graph runtime below executes one Project graph for one claimed Task
(`run-task`); Project lifecycle (`claim`, `complete`) and the multi-Project
coordinator with shared resource admission are described after it.

## Graph and results

Version 1 graphs contain `nodes`, a nonempty `flow`, and optional `max_steps`
(default 100). Flow entries are node IDs or
`{"if":{"path":"/results/select/data/task","equals":null,"then":[],"else":[]}}`.
Branches may be empty. An explicit post-condition loop has the form
`{"loop":{"flow":["review"],"while":{"path":"/results/review/data/approved","equals":false}}}`.
The nonempty body runs once before `while` is evaluated, then repeats while it
matches. Conditions reuse `if`'s RFC 6901 pointers and type-sensitive equality.
Results, `last`, and remaining/charged resources carry forward across iterations;
`results[node_id]` holds the latest result and `events` retains every completed
invocation. Missing condition data or any body Runtime Failure stops the Run.
There is no retry, continue, or resume behavior.

Every node/control activation counts against `max_steps`. A loop consumes one
step on entry **plus one at each iteration start, including the first**, in
addition to all body node/control activations. Condition evaluation adds no step.
For example, a loop running one node twice consumes five steps: entry, start,
node, start, node. Every activation checks the shared Run budget before executing;
no body or node executes after the budget is exhausted. As with existing node
limits, the denied activation is included in receipt `steps` (`max_steps + 1`)
and produces a `limit` Runtime Failure. Iterations with only empty branches still
consume steps and are bounded.

All declarations, both conditional branches, and loop bodies/condition pointers
are validated before any external operation, including unreachable flows.
Combined `if`/`loop` nesting is limited to 32 levels. No expressions, arbitrary
graph cycles, concurrency, map/fan-out, or retries are supported.

Only two node kinds exist. `agent` delegates an instruction to an executor;
`action` invokes a runtime operation: `load`, `select`, `resources`, `execute`,
`status`, `complete`, `writeback`, or `result`. Agent roles belong in instructions. `execute` delegates
work without adding an instruction. Both use the same executor boundary.
Node `inputs` maps names to RFC 6901 pointers into
`{project, task, resources, results, last, run_id}`; `task` is the claimed Task
passed to `run-task`. The runtime performs no implicit selection, Status change or
completion: only the nodes a graph lists run. Each node returns
`{message, data, references, usage}`; data is a JSON object, references are strings,
usage maps resource names to nonnegative numbers. Results are retained by node ID;
`last` is the last result. Empty branches preserve it. Equality is type-sensitive.
Task outcomes (including rejection) belong in data, never process exit codes.
Missing paths, invalid output, launch, timeout, transport, accounting, and
writeback errors are separate Runtime Failures and terminate the Run. No hidden
fallback or automatic retry. Executor failures never trigger automatic GitHub
writeback. The receipt retains failure details, completed results, and resources;
a valid executor Result is included in failure details if accounting fails.
Failure routing is not supported; subsequent graph operations are not executed.

## Resources

### Shared AI resource admission (coordinator)

Shared provider allowance is admitted by `projectweave run` and
`projectweave coordinate`, not by graphs. The root `projectweave.json` lists, per
Project directory name and provider, `min_remaining_percent` and
`estimated_usage_percent_per_task` (and, per Project, an optional scheduling
`weight`; see Coordinator). Constraints are opt-in: unlisted Projects and
providers are not limited (and not observed).

1. The listed providers are observed read-only without a model call
   (`projectweave/usage.py`): **Claude** via `claude -p --output-format json /usage`,
   giving `100 − N` for each window it reports (`Current session`,
   `Current week (all models)`, and a model-specific weekly window such as
   `Current week (Fable)` when present; session and all-models are required);
   **Codex** via `codex app-server` (experimental) `initialize` → `initialized` →
   `account/rateLimits/read`, giving `primary = 100 − rateLimits.primary.usedPercent`.
2. A Task of Project P is admitted only if for every provider listed for P and
   every observed window: `remaining − reserved(provider) − estimate(P, provider)
   ≥ min(P, provider)`. `reserved` sums the estimates of all running Tasks, across
   Projects, for that provider.
3. Launching reserves P's estimates in memory; when the Task ends (completed,
   failed or crashed) they are released and the providers are observed again.

Any observation failure (launch failure, nonzero exit, timeout, unexpected
output, a missing required line or value, an unknown provider) makes that
provider unknown, which admits nothing for Projects that list it. Estimates are
safety margins adjusted by operators, not accounting: ProjectWeave never
attributes usage to Tasks, estimates from tokens, monitors usage during a Task,
or redeems reset credits. The Claude observer parses human-readable output and
the Codex API is experimental; either may break with a new CLI version, which
then stops safely as unknown.

### Metered resources

Metered entries are `{unit, available, accounting}`. `accounting` is `reservation`
or `reported`. Units are operator-defined (invocations, tokens, USD, etc.). They
remain in the graph runtime for programmatic use with an explicit envelope;
`run-task` supplies none and init emits none. An agent/execute node may declare a nonempty `requires` map of
positive amounts, or omit it when no metered reservation is needed. All
amounts are checked atomically and reserved before launch. Insufficient or absent
resources produce an ordinary `resource_exhausted` result without launching.
`reservation` keeps the full reserved amount charged, regardless of supplied usage.
`reported` first requires valid usage for every reserved reported resource, then
settles each once with `available += reserved - reported` and
`charged += reported - reserved`. Usage above the reservation uses the same
formula and is not a Runtime Failure. Available balances may become negative;
later executions requiring that resource are refused by the admission check.

Usage for unreserved resources is ignored for settlement, including resources
outside the envelope. Common Result validation still requires all usage entries
to have nonblank names and finite, nonnegative numbers. Configuration is
responsible for declaring resources an executor may consume in `requires`.
Missing or invalid required usage, invalid Results, and executor Runtime Failures
stop the Run and keep every reservation charged. No partial settlement or inferred
refunds occur, even when another resource has a valid report or an overrun.

Executors receive the allocation and remaining envelope. This reflects reported
consumption and constrains subsequent admission; it does not guarantee an
in-flight consumption limit. Strict spending limits require executor/provider
controls. No discovery, resets, purchases, or provider switching. Accounting is
scoped to one Run, not concurrent processes or a billing ledger.

## GitHub mapping

A Project's `project.json` contains `owner`, `number`, and `owner_type`
(`organization` or `user`). `priority_field`, `status_field` default to Priority
and Status; `priority_order` defaults to `["P0", "P1", "P2"]`; optional
`eligible_statuses` further restricts selection (AND). Read all pages of items,
labels, item field values, and project fields. Eligibility is the fixed Project
single-select field `AI execution`: only nonarchived open Issues whose value is
`Ready` qualify. Labels play no part and a `label` setting is rejected. Drafts,
PRs, inaccessible content, and closed Issues are excluded. Rank by configured priority, then oldest
createdAt, then Issue URL and item ID. Missing/unknown priorities sort last.
`select` returns task null for empty work. `claim` runs load → select and sets
the selected Task's Status to `In Progress` while holding a local per-Project file
lock (`projects/NAME/.projectweave.lock`), so concurrent claims on one machine
never select the same Task; the lock is released before the Task runs. The
`status` action sets a named Status option (resolved and validated first) without
commenting, and its failure is a `status` Runtime Failure; `complete` (action or
command) sets `Done`. Writeback posts an ordinary Issue
comment with the Run ID and full structured result, then optionally sets a named
Status option. No automatic semantic interpretation or Issue closure. Validate
status before commenting. Partial writeback is a failure and records completed
mutation references. Lost responses are ambiguous; rerunning can duplicate work
or comments. The lock and reservations are single-machine and in-memory; there is
no distributed locking.

## Project workspace and Task checkouts

A Project spans any repositories whose Issues are in the GitHub Project; each
Task's `repository` is its execution location. A Project's `projects/NAME/`
directory is its Project workspace. GitWeave executors run from the
workspace in GitWeave's Issue mode (below), which fetches the repository itself.
Command executors are repository-aware, and **each invocation runs in its own
isolated worktree** (an invariant: concurrent Tasks never share a mutable working
tree). After admission and before launch, the runtime prepares the shared Git
source `<workspace>/repos/OWNER/REPO` for `task.repository`: it clones with `gh repo clone` when the path is
absent, otherwise requires a directory that is itself a Git repository root whose
`origin` is that github.com
repository, then runs `git fetch origin` and checks that `origin/HEAD` resolves.
It then adds a detached worktree at that remote default branch tip under
`<workspace>/worktrees/<run_id>-<node>-<step>/` and passes only that path as the
request's `checkout`; the shared checkout itself is never handed out. The worktree
is removed when the executor exits (success or failure); results that must
survive are published by the executor. No repository list, path mapping, pooling
or background sync exists, and no local branch is changed. Invalid repository
names, an unrelated existing path, and clone/fetch/worktree errors are `checkout`
Runtime Failures before launch (so no writeback); a removal failure is recorded
in the receipt's `cleanup_failures` without changing the outcome. Operations on
the shared checkout (clone, fetch, worktree add/remove) are serialized per
repository by a lock file beside it.

## Coordinator

`projectweave coordinate` repeats: observe the listed providers → repeatedly give
the next launch opportunity to an admitted Project with work, chosen by smooth
weighted round-robin (optional per-Project `weight`, default 1; credit persists
across passes so sort order cannot starve a Project), `claim` a Task there and run
it on its own thread (`run-task`), until no Project can take another Task → wait
until a Task ends (release its reservation) or
`poll_seconds` pass → observe again. Independent Projects and several Tasks of one
Project run concurrently; nothing else limits concurrency. Selection stays
Project-local (no cross-Project ranking or dependency reasoning). A stop request
(SIGINT/SIGTERM) stops launching and waits for running Tasks; `--once` makes one
pass and waits for what it launched. `projectweave run NAME` is the synchronous
single-Task path: admission → claim → run-task.

## Executors and GitWeave lessons

Command executors are argv arrays (no shell). They receive one JSON request on
stdin: `{task, checkout, context, resources, allocation, instruction, run_id}`,
where `checkout` is the invocation's isolated worktree path. Exit zero
must emit exactly one common Result as JSON; nonzero is infrastructure failure.
A finite timeout (default 3600 seconds) kills the subprocess group. Instructions
and command configuration are trusted; task content is data. Children inherit
the environment; this is not an OS sandbox.

The GitWeave adapter invokes the public `gitweave run --graph ... --repo OWNER/REPO
--issue N REQUEST` CLI from the workspace directory, with optional
`--provenance-remote`. The Task must carry a valid `repository` and a positive Issue
`number`; otherwise it is an `input` Runtime Failure before launch. GitWeave fetches
the default branch itself and gives nodes `run_input`/`github_repository`; REQUEST
embeds the ProjectWeave request JSON (without `checkout`) as optional guidance. It translates the CLI's completed record and terminal
outputs into a Result; terminal commit strings become references and original
outputs remain in data. It does not import GitWeave or read its private refs.
GitWeave CLI does not expose aggregate usage, so this adapter supports reservation
accounting only. Completion means graph completion, not task acceptance. Downstream
graph conditions inspect `data.outputs` for task semantics. GitWeave can publish
provenance automatically and its graph may mutate remote state: operators must
review its graph/repository configuration before live use.

Studied GitWeave docs/runtime.md and model.py, graph.py, runtime.py, cli.py in the
local reference checkout before implementation. Transferred the small node model,
structured control, bounded execution, explicit actions, and outcome/failure
separation. Did not transfer worktree ownership or Git provenance as project state.
GitHub operations use native `gh api graphql` authentication and the documented
[Projects API](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects).
