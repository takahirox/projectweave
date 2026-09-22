# First runtime: minimal design (Issue #4)

Python 3.11+ and the standard library; one synchronous CLI invocation, no task
database. GitHub Projects v2 is canonical. JSON Run output is an execution
receipt, not a second project state store.

## Graph and results

Version 1 graphs contain `nodes`, a nonempty `flow`, and optional `max_steps`
(default 100). Flow entries are node IDs or
`{"if":{"path":"/results/select/data/task","equals":null,"then":[],"else":[]}}`.
Branches may be empty. No expressions, cycles, loops, concurrency or retries.
Every node/control activation counts against max_steps. All declarations and
both branches are validated before any external operation.

Only two node kinds exist. `agent` delegates an instruction to an executor;
`action` invokes a runtime operation: `load`, `select`, `resources`, `execute`,
`writeback`, or `result`. Agent roles belong in instructions. `execute` delegates
work without adding an instruction. Both use the same executor boundary.
Node `inputs` maps names to RFC 6901 pointers into
`{project, resources, results, last, run_id}`. Each node returns
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

Envelope entries are `{unit, available, accounting}`. `accounting` is `reservation`
or `reported`. Units are operator-defined (invocations, tokens, USD, etc.). Every
agent/execute node requires a nonempty `requires` map of positive amounts. All
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

`--project` is a JSON file containing `owner`, `number`, and `owner_type`
(`organization` or `user`). Optional `label` defaults to `projectweave-ready`;
`priority_field`, `status_field` default to Priority and Status; `priority_order`
defaults to `["P0", "P1", "P2"]`; optional `eligible_statuses` further restricts
selection. Read all pages of items, labels, item field values, and project fields.
Only nonarchived open Issues with the label qualify; drafts, PRs, inaccessible
content, and closed Issues are excluded. Rank by configured priority, then oldest
createdAt, then Issue URL and item ID. Missing/unknown priorities sort last.
`select` returns task null for empty work. Writeback posts an ordinary Issue
comment with the Run ID and full structured result, then optionally sets a named
Status option. No automatic semantic interpretation or Issue closure. Validate
status before commenting. Partial writeback is a failure and records completed
mutation references. Lost responses are ambiguous; rerunning can duplicate work
or comments. This release does not claim tasks atomically or reconcile concurrent
edits. Run one coordinator per project.

## Executors and GitWeave lessons

Command executors are argv arrays (no shell). They receive one JSON request on
stdin: `{task, context, resources, allocation, instruction, run_id}`. Exit zero
must emit exactly one common Result as JSON; nonzero is infrastructure failure.
A finite timeout (default 3600 seconds) kills the subprocess group. Instructions
and command configuration are trusted; task content is data. Children inherit
the environment; this is not an OS sandbox.

The GitWeave adapter invokes the public `gitweave run --graph ... --repo ...
--commit ... REQUEST` CLI, with optional `--provenance-remote`. REQUEST embeds the
request JSON as data. It translates the CLI's completed record and terminal
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
