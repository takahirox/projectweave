# CLI and executor guide

## Setup and one Run

Install Python 3.11+, GitHub CLI (`gh`), and the chosen executor. Authenticate `gh`
with access to the project and its Issues: project read access for loading,
project write access for Status updates, and Issue comment permission for
writeback. The runtime targets github.com, including user and organization
Projects v2; classic Projects and enterprise hosts are not supported.

Copy `examples/dispatch.json`, `examples/project.json`, and
`examples/resources.json`, replace the project owner/number, and adjust
Priority and Status names to the actual single-select field values. Values are
case-sensitive. Optional `eligible_statuses` is an additional filter; remove it
to select by open Issue plus ready label alone. Unknown or absent Priority values
sort below all configured values. Priority ordering is explicit, not inferred
from the order returned by GitHub.

Configure the GitWeave graph, local repository, and commit in `dispatch.json`.
Use a GitWeave installation matching its documented v0 `run` CLI contract. The
adapter reads stdout JSON only; it does not import GitWeave. Its graph owns task
instructions, model choices, artifact handling, and any publication actions.
`provenance_remote` optionally maps to GitWeave's `--provenance-remote`; otherwise
GitWeave applies its own destination defaults. All paths (including relative
paths in graph options and argv) resolve from the invoking working directory.

```sh
python3 -m projectweave validate --graph examples/dispatch.json
python3 -m projectweave run --graph examples/dispatch.json \
  --project examples/project.json --resources examples/resources.json > run.json
```

`validate` makes no external calls. `run` emits a JSON receipt on stdout: Run ID,
status, failure (or null), executed steps, per-node results, ordered events, last
result, and remaining/charged resources. A repeated node ID replaces that ID's
entry; events retain every invocation. Exit 0 means normal graph completion
(including no work, exhausted capacity, or a negative task outcome); exit 1 is a
Runtime Failure; exit 2 is invalid input/setup. CLI argument syntax errors also
use exit 2 via argparse. Save receipts for diagnostics if desired; GitHub remains
the project state. Receipts contain selected Issue content and bounded executor
error text, so handle them like project data.

## Node reference

All nodes have `kind` and optional `inputs`, an object mapping names to JSON
pointers into the Run context. No templating or shell evaluation occurs.

| Node | Required configuration/inputs | Output data |
| --- | --- | --- |
| `action: load` | none | `items`: nonarchived open Issues and their metadata |
| `action: select` | `inputs.items` | `task`: highest ranked eligible Issue or null |
| `action: resources` | optional `config.requires` allocation | `resources` snapshot and `available` admission boolean |
| `action: execute` | `executor`, nonempty `requires`, `inputs.task`; optional `inputs.context` | executor's Result data |
| `kind: agent` | same as execute plus `instruction`; omit action | executor's Result data |
| `action: writeback` | `inputs.task`, `inputs.result`; optional `config.status` | updated Status name or null; comment URL in references |
| `action: result` | `config` containing a complete Result | literal Result, useful for terminal no-work branches |

Agent instructions describe project-level thinking such as evaluation, planning,
or review. The runtime simply delivers them to the executor. A command wrapper
must honor the instruction; a GitWeave graph must make use of the supplied request.
There are no specialized planner/reviewer node kinds. Execution nodes accept no
configuration options; omit `config` or use an empty object. Executor Runtime
Failures are preserved in the Run receipt and stop the Run without automatic
GitHub mutations. A valid executor Result is retained in failure details if
accounting fails. GitHub writeback is an explicit graph action. Failure routing
is not supported, so downstream actions do not run after a Runtime Failure.

`resources.config.requires` tests whether an entire allocation can be admitted
without charging it. Actual execution checks again and charges before launching.
An unavailable allocation returns `data.status: "resource_exhausted"`; it is not a
launch failure. No task returns null from select; graphs should branch before
executing, as the example does. Missing pointer targets are Runtime Failures.

To change Project Status, give a writeback node `"config":{"status":"Done"}`.
Use a conditional branch to select that node only when the executor's structured
outcome warrants it. A GitWeave task verdict can be selected at
`/results/gitweave/data/outputs/0/data/approved` if its graph returns that field.
The runtime does not equate GitWeave completion with task approval. The shipped
example comments outcomes without choosing a Status policy. PR URLs can be
returned in Result references and will appear in the Issue comment.

## Review/fix loop

[examples/review-fix.json](../examples/review-fix.json) selects a task and uses
this body, skipping the loop when no eligible task exists:

```json
{
  "loop": {
    "flow": [
      "review",
      {"if": {
        "path": "/results/review/data/approved",
        "equals": false,
        "then": ["fix"],
        "else": []
      }}
    ],
    "while": {"path": "/results/review/data/approved", "equals": false}
  }
}
```

The first review always runs; fixes run only after rejection. The loop checks
the named review result after the conditional fix, so a fix cannot overwrite the
approval being tested through `last`. A rejection leads to another review; an
approval exits without a fix. Configure both wrapper paths and an `ai` resource
envelope before live use. Review must return a boolean `data.approved`; wrappers
receive the selected task and the preceding result as context. This example
performs no writeback.

Validate without invoking wrappers:
`python3 -m projectweave validate --graph examples/review-fix.json`.
The body must be nonempty. Each loop entry consumes one step, each iteration
start (including the first) consumes another, and body nodes/controls consume
their usual steps. All nested controls share `max_steps`; no body executes after
the limit is exhausted. State and resource balances carry forward, with latest
per-node results and all completed invocations in events. Missing condition data
(including an exhausted review allocation that returns no `approved` field) or
any body Runtime Failure stops the Run without retries or continuation.

## Command executor contract

An executor object is either:

```json
{"type":"command","argv":["/path/to/wrapper","--option"],"timeout":3600}
```

or:

```json
{"type":"gitweave","graph":"/path/to/graph.json","repo":"/path/to/repo","commit":"HEAD","timeout":3600}
```

The timeout is positive seconds; default 3600. GitHub requests each have a
120-second timeout. Timeout terminates the direct process group; descendants
that deliberately detach are outside that control. No requests are retried.

Command stdin is one JSON object:

```json
{
  "task": {"title":"Example", "body":"Task description"},
  "context": {},
  "resources": {"api":{"unit":"USD","available":8,"accounting":"reported","charged":2}},
  "allocation": {"api":2},
  "instruction":"Evaluate the selected task",
  "run_id":"opaque-run-id"
}
```

The resources snapshot is after reservation. Action execute sets instruction to
null; agent supplies its declared instruction. The wrapper must emit exactly
one JSON Result to stdout and use stderr for logs. Exit nonzero means Runtime
Failure; ordinary task failure is an exit-zero Result with appropriate data.
For example:

```json
{"message":"Needs revision","data":{"approved":false},"references":[],"usage":{"api":0.7}}
```

Reported usage uses the declared resource unit and is required for every reserved
resource in `reported` mode. All required reports must be valid before any
settlement. Each is settled once with `available += reserved - reported` and
`charged += reported - reserved`. In the example above, reserving 2 and reporting
0.7 leaves 9.3 available and 0.7 charged. Reporting 12 instead leaves -2 available
and 12 charged without a Runtime Failure; later executions requiring `api` cannot
launch. Admission reserves all `requires` atomically or launches nothing.

Usage is optional in `reservation` mode and never adjusts the reserved charge,
even when reported above the reservation. Unreserved usage is ignored for
settlement, whether or not the resource appears in the envelope. All supplied
usage still undergoes common Result validation: an object mapping nonblank
resource names to finite, nonnegative numbers (not booleans). Missing required
reports, invalid Results, or executor Runtime Failures stop the Run and retain
all reservations without partial settlement or inferred refunds.

Units need not be universal: use one GitWeave Run, one request, tokens, or USD as
appropriate. For exact fractional accounting, use integer units such as
microdollars. The runtime uses JSON numbers, not a monetary ledger. GitWeave
supports reservation mode only because aggregate usage is absent from its public
CLI response.

A wrapper can enforce native budgets when its provider offers them. ProjectWeave
reflects reported consumption and constrains subsequent admission; it cannot
prove native consumption or enforce an in-flight spending limit. It performs no
allowance discovery or reset. Graph/resource configuration is trusted and must
declare the resources an executor may consume in `requires`.

## Failures and operational limits

Selection is a snapshot, not an atomic claim. Separate Runs do not coordinate or
share resource balances. Do not run multiple coordinators against the same work
without external coordination. A crash after an external effect may leave GitHub
updated without a receipt; there is no resume or idempotency database.

Writeback preflights the requested Status option, posts the comment, then updates
Status. If the second operation fails, the receipt reports the completed comment
reference and preserves the executor result. If a response is lost, remote effects
are unknown. A new Run can duplicate comments or execute the same task; inspect
GitHub and the receipt before rerunning. Failures do not trigger fallback executors.

## Verification

Run `python3 -m unittest discover -s tests -v`. The suite exercises the real CLI
and subprocess boundary with deterministic fake `gh`, `gitweave`, and command
executables, including pagination of all connections, task filtering, both
execution paths, writeback, failures, and no-work/resource-exhausted cases.
No live executor or GitHub mutation is part of this suite. It verifies the public
contracts against fixtures, not installed GitWeave behavior, actual permissions,
provider accounting, or network behavior. Real GitWeave execution can consume AI
allowance and publish provenance; live validation remains an operator action.
