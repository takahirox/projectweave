# First runtime: minimal design (Issue #4)

Python 3.11+ and the standard library; no task database. GitHub Projects v2 is
canonical. JSON Run output is an execution receipt, not a second project state
store. The graph runtime below executes one Project graph for a claimed Task
(`run-task`) or independently of Tasks (`run-graph`); Project lifecycle (`claim`, `complete`) and the multi-Project
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
`{project, task, resources, results, last, run_id, input}`; `task` is the claimed Task
passed to `run-task`, or null for `run-graph`. `input` is optional operator text
from `run-graph --input`, or null. The runtime performs no implicit selection, Status change or
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

`run-graph PROJECT --graph PATH` loads and validates `projects/PROJECT/project.json`
and the selected Project graph before executing it once using this interpreter.
The path must be relative to that Project workspace and stay inside it after
symlink resolution; absolute paths, Windows drive paths and backslashes are
rejected. It does not load the fixed `graph.json`, require root coordinator
configuration, admit or claim a Task, or change Status implicitly. Repetition
remains graph-defined through `loop` and bounded by `max_steps`.

Command agent/execute nodes may omit `inputs.task`. These Taskless invocations
run with the Project workspace as their working directory and receive `task: null`,
`project`, and `input` in the executor request, with the normal context/resources/
allocation/instruction/Run ID fields and no `checkout`. There is no implicit
repository selection or temporary repository worktree; Project files persist.
An explicit `inputs.task` still requires a Task object and retains the existing
isolated worktree behavior. GitWeave Issue-mode nodes still require a Task input
with a valid repository and positive Issue number. Status, complete and writeback
still require a selected Task belonging to the configured GitHub Project.

## GitWeave graph routes

`project.json` may contain an ordered `graph_routes` array of objects with
exactly `label` and `graph` nonblank strings. Labels are unique and compared
case-insensitively. For every GitWeave invocation, the first configured label
in the Task's existing `labels` chooses its graph; unmatched Tasks retain that
node's configured default. Command executors are unaffected. Routing policies
are independent per Project and have no built-in label/provider/model meanings.

Route paths are Project-workspace-relative and may not escape the directory,
including through symlinks. For Projects with routes, setup validates all route
graphs and GitWeave executor defaults using the public `gitweave validate` CLI
before claiming work. The selected graph is checked again before launch.
Static failures do not claim Tasks or launch agents. `init-project` preserves
and validates human-edited routes when reusing a workspace.

Run receipts include `executions` with each invocation's `execution_id` and
selected `config`. Observers receive the same configuration in
`executor_started`; dashboard execution state retains `config`, `graph_path`
and the selected graph's progress view. No routes (or an empty array) keeps
existing executor selection and setup behavior.

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
`run-task` and `run-graph` supply none and init emits none. An agent/execute node may declare a nonempty `requires` map of
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
`eligible_statuses` further restricts selection (AND), but only Status `Todo`
is runnable even when that setting is absent or lists other statuses. Read all
pages of items, labels, item field values, and project fields. Only nonarchived
open repository Issues in the configured Project qualify. Optional `repository`
filters by owner/name (case-insensitive). Optional `required_labels` and
`excluded_labels` are arrays of nonblank label names: all required labels must
be present, and no excluded label may be present (case-insensitive). Defaults
impose no repository or label restriction. Labels are freshly loaded at each
selection, including for existing members; Auto-add controls membership only.
No permission field is required or read for eligibility; an existing
`AI execution` field is ignored. The legacy singular `label` setting is rejected
with instructions to migrate manually to `required_labels`. Draft Project items,
PRs, inaccessible content, closed Issues, and archived items are excluded.
Rank by configured priority, then oldest
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
Command executors with a Task are repository-aware, and **each invocation runs in its own
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
A finite timeout (default 3600 seconds) kills the subprocess group. Explicit
`timeout: null` disables the outer timeout for either executor; the packaged
GitWeave Project graph uses it for resident readiness waits. Instructions
and command configuration are trusted; task content is data. Children inherit
the environment; this is not an OS sandbox.

The GitWeave adapter invokes the public `gitweave run --graph ... --repo OWNER/REPO
--issue N REQUEST` CLI from the workspace directory, with optional
`--provenance-remote`. The Task must carry a valid `repository` and a positive Issue
`number`; otherwise it is an `input` Runtime Failure before launch. GitWeave fetches
the default branch itself and gives nodes `run_input`/`github_repository`; REQUEST
embeds the ProjectWeave request JSON (without `checkout`) as optional guidance. It translates the CLI's completed record and terminal
outputs into a Result; terminal commit strings become references and original
outputs remain in data. It does not import GitWeave. The optional dashboard can
read the returned Run's local provenance refs for progress (see below).
GitWeave CLI does not expose aggregate usage, so this adapter supports reservation
accounting only. Completion means graph completion, not task acceptance. Downstream
graph conditions inspect `data.outputs` for task semantics. GitWeave can publish
provenance automatically and its graph may mutate remote state: operators must
review its graph/repository configuration before live use.

The packaged GitWeave Task graph first runs the deterministic `issue_route`
command. It reads the source Issue's labels through `gh api`, trims and
case-folds names, and returns sorted unique `labels` plus a `route`: the exact
`bug` label selects `bug`; missing or unrelated labels select `default`.
The ordered `LABEL_ROUTES` mapping in `projectweave/routing.py` defines label
precedence explicitly. Classification uses no AI model. GitHub failures stop
the Run rather than silently choosing a route. An `if` on `/0/data/route`
selects one pre-implementation loop for the Run; label-only changes during
the Run do not re-route it.

Each loop captures the Issue's title/body, external comments and GitHub revision
metadata. The default route runs the readiness agent; the bug route runs
`diagnose`, which inspects
the repository and relevant tests, reproduces the failure where practical,
and returns evidence, likely cause, fix approach and validation in a
`diagnosis` string. An undiagnosed bug returns `needs_information` with
blocking questions instead of approving implementation. A deterministic command
posts missing questions once per assessed revision and waits for relevant content
changes or closure. Polls consume no model calls or additional graph steps:
60 seconds for the first hour, 300 seconds until 24 hours, then 3600 seconds
indefinitely. Automation markers identify its own comments; the authenticated
account's unmarked replies still count. GraphQL Issue/comment `lastEditedAt`,
the latest title rename event ID, and external comment IDs identify relevant
changes; Issue `updatedAt` and full-body comparison/hashing are never detectors.
Unchanged polls read metadata only, apart from newly added/edited comment bodies
needed to classify automation. Initial assessment and relevant updates fetch full
content. Existing GitWeave parallel control flow carries each original Command
result in a branch containing a no-op conditional, independently of the agent
assessment. Request/guard commands receive the agent result at `inputs[0]` and the
Command-captured baseline at `inputs[1]`. Agents return neither snapshots nor
authoritative revision metadata. Commands preserve that baseline through posting
and waiting, so concurrent replies are not missed. No shared files or global
baselines are used; separate Runs and Issues remain isolated, and serialized
checkpoint inputs preserve the same baseline on command retry. A deterministic
guard rechecks closure and revision immediately before implementation.
For an unchanged, open Issue it forwards the approved technical diagnosis to
implementation. Content changes discard that diagnosis and repeat the selected
review phase. Both routes converge on the same implement/publish/review/fix/merge
flow. Closure exits with schema-validated `{status: "closed", questions: [], snapshot, revision}`
data and no PR. This is a completed graph receipt, with no implementation.
The resident wait retains the Task's coordinator reservation, and shutdown or
`--once` waits for it; resume after process/host restart is out of scope.
Repeated meaningful updates share `max_steps: 30` with the remaining workflow;
exhaustion produces GitWeave's normal step-limit failure and stops further work.

After publication, `review_snapshot` captures Issue content and revision metadata
and forwards `pr`. Review returns its assessment and the current head it actually
reviewed. Its schema adds `status`, `questions` and `retry_review`
to the existing `pr`, `approved` and `findings` fields. `status` is `approved`
only when no blockers remain, `needs_fixes` for agent-fixable findings (including
when human checks also remain), or `needs_confirmation` for human-only blockers.
`retry_review` is true for either pending outcome and false on approval.
The fix/publish path snapshots and reviews the updated PR head before requesting
remaining human checks. The merge/open-Issue/head/check safeguards are unchanged.

`request_confirmation` and `wait_for_confirmation` use
`projectweave issue-review comment|wait`, preserving `pr.number`, `pr.url`, and
`pr.head_sha`. The request identifies that head and asks concrete questions with
expected evidence. Deduplication includes the relevant revision metadata, PR
identity and questions.
An intervening content change returns `updated` without posting stale questions;
posting retains the original baseline to catch a concurrent reply. Both
`<!-- projectweave:issue-readiness:` and `<!-- projectweave:issue-review:` comments
are excluded from both phases' relevant revisions. Unrelated metadata changes
do not wake a wait. Polling uses the same cadence and resident lifetime as readiness, with no
AI invocation or additional graph step per poll. An update returns the changed
snapshot, revision and PR identity to review; it never sets approval. An
insufficient or unrelated response causes another request/wait, without fix/publication unless
review identifies a new agent-fixable defect.

Command statuses are `review` (initial snapshot), `needs_confirmation` (posted
request), `updated` (content changed), or `closed`. `retry_review` is false for
closure. Closure at snapshot/request/wait exits the review loop, and
`review_closed` returns the same `{pr, merged: false, retry: false,
merge_commit: ""}` contract as a skipped merge. It invokes no merge agent;
the existing terminal `close_issue` reports the skipped outcome and preserves
the closed Issue. Review waiting retains the Task reservation and waits through
coordinator shutdown/`--once`; it does not implement resource-releasing
suspension, a new Runtime pause mechanism, capacity retries or restart recovery.

Immediately before every merge attempt, the merge agent reads the source Issue
from `github_repository` and `run_input.number`. A closed Issue produces
`merged: false`, `retry: false`, and `merge_commit: ""`, without merging.
A successful merge also returns `retry: false`; an open Issue's failed or
blocked merge returns `retry: true`. The outer review/merge loop repeats only
on `retry: true`, so closure ends it cleanly. If the Issue cannot be read or its
open state cannot be confirmed, execution stops without merging. The reviewed
head SHA, merge-commit strategy, and required GitHub checks/reviews still apply.

After implementation, the graph returns terminal `close_issue` data with
required `pr`, `merged`, `merge_commit`, and `closed` properties. Both `merge`
and `close_issue` require `merge_commit` to be a string: the actual merge commit
SHA when `merged` is true, or `""` when it is false. `close_issue` always forwards
`pr`, `merged`, and `merge_commit` unchanged from `merge`; it ensures the Issue
is closed after a successful merge and otherwise preserves its current state,
never reopening an Issue closed before merge. It comments the outcome in both
cases. Omitting `merge_commit` is invalid. All object properties in the packaged
Codex schemas are required, including nested PR properties. See the README's
[existing workspace migration](../README.md#repairing-existing-workspace-graphs)
before dispatching Tasks with a previously generated graph.

Studied GitWeave docs/runtime.md and model.py, graph.py, runtime.py, cli.py in the
local reference checkout before implementation. Transferred the small node model,
structured control, bounded execution, explicit actions, and outcome/failure
separation. Did not transfer worktree ownership or Git provenance as project state.
GitHub operations use native `gh api graphql` authentication and the documented
[Projects API](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects).

## Local execution dashboard

`projectweave coordinate --web` serves a read-only dashboard at
`http://127.0.0.1:8765/` alongside the coordinator. `--web-port PORT` changes the
port; `--web-port 0` chooses an available port. The actual URL is printed to
stderr as `{"dashboard_url": "..."}`. `--long-running-seconds N` sets the elapsed
threshold (default 3600 seconds). The server binds only to IPv4 loopback, uses no
external assets or dependencies, and closes when coordination exits, including
`--once`, errors, and graceful SIGINT/SIGTERM shutdown. During graceful shutdown
it remains available while already launched Tasks finish.

The top page lists every managed Project, including idle or misconfigured
Projects, with compact Running, Long running, Failed, and Completed execution
counts and the most relevant Task's elapsed time and observed position.
Long-running and running Tasks appear ahead of failed and completed executions. Project links
open a split task/run explorer; Task links select a run while keeping the Project
and task list in view. Run details include identity, status, times, failure
details, executor information, and a GitWeave workflow graph when configured.
Graph nodes can be selected with a click or Enter/Space to inspect their status
and recent attributed output. Selection remains during refresh; "Follow current
node" resumes following observed active/latest nodes and brings them into view.
Narrow layouts stack the
task list and detail, and graphs scroll within their own region. The dashboard
uses the browser's light/dark preference. Durations and views refresh every second.
Running means a Task actually launched by this coordinator and not yet finished; a GitHub Project
item's In Progress status does not determine this count. Long running means
Running with elapsed time strictly greater than the threshold. Failed counts
failed executions, including worker exceptions. Completed counts runs known to
the current registry whose final status is `completed`; these runs no longer
count as Running or Long running. All counts reset when ProjectWeave restarts.
Terminal executions remain visible for this process's lifetime. Setup/claim
problems remain in the normal coordinator reports and are not counted as failed
Task executions.

The optional thread-safe registry holds this state directly in memory. Nothing
is recovered from earlier coordinator processes, and no database or history file
is written. Each Task retains at most 500 recent log entries of at most 2000
characters each, including captured executor stdout/stderr. The execution trace
shows timestamps, stream and available node attribution, separates lifecycle
events from output, and can filter either category. It follows new output while
at the bottom; scrolling up preserves your position across refreshes, and "Jump
to latest" resumes following. Graph scrolling and keyboard focus also survive
refreshes. Command executors keep their normal single-JSON-result stdout contract and have a useful generic
view without requiring a GitWeave graph. Without `--web`, executor collection and
coordinator behavior use the original paths.

For GitWeave, ProjectWeave loads each executor's configured graph relative to
its Project workspace (absolute graph paths also work). Structured flow is
rendered as node edges, including parallel branches, conditionals, maps, and
loops. Multiple executor invocations have separate graphs and GitWeave Run IDs;
the ProjectWeave Run ID is displayed separately. On completion or failure,
returned terminal outputs identify known completed nodes. When the returned
repository, run ref, and notes ref are locally available, ProjectWeave reads
`run.json` and attempt notes using read-only Git commands to show intermediate
completed/failed nodes too. Missing provenance never changes the Task outcome.
Only refs belonging to the returned Run are consulted; other Runs are not
scanned or recovered.

The currently supported GitWeave CLI emits only a final Run summary. Thus active
nodes and internal agent/command logs are unknown until an executor supplies
live events. The dashboard adapter also consumes optional JSON-lines events on
GitWeave stderr alongside ordinary text, while stdout remains the final Run
JSON. Supported flat event objects use `type` (or `event`): `run_started` with
`run_id`; `node_started`, `node_completed`, and `node_failed` with `node_id` and
optional `instance_id`/`run_id`; and `agent_output`, `command_stdout`,
`command_stderr`, or `log` with `text` (also accepting `message` or `output`).
Output events may also supply `node_id` and `instance_id`; these are retained
alongside the executor invocation identity for selected-node output. Unattributed
stdout/stderr remains visible in the run trace; its node is never guessed.
For example:

```json
{"type":"node_started","run_id":"abc","node_id":"implement","instance_id":"implement-1"}
{"type":"command_stdout","text":"Running checks"}
{"type":"node_completed","node_id":"implement","instance_id":"implement-1"}
```

Such events update active nodes and logs immediately without waiting for the
executor to finish. Concurrent instances are tracked separately. Progress is
observed rather than guessed: branches may never execute, and active nodes that
lack a terminal event become unknown when the executor ends. A future GitWeave
event protocol can be translated at this adapter boundary without moving UI
code into GitWeave.

The same registry is available through read-only JSON endpoints: `/api/state`,
`/api/projects`, `/api/projects/NAME`, and `/api/runs/ID` (the last uses the
process-local execution ID supplied by the API). All responses disable caching.
`/api/state` also includes `subscription_usage`, an empty list by default. When
[dashboard usage providers are configured](usage.md#shared-ai-resources), each
entry has `provider`, `windows` (remaining percentages, or null), `updated_at`
(last successful UTC timestamp, or null), `error` (latest read failure, or null),
and `status` (`current`, `stale`, or `unavailable`). These entries come from the
shared usage cache; HTTP requests never run provider checks.
