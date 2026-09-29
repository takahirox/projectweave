# CLI and executor guide

## Overview

ProjectWeave operates **Projects** (GitHub Projects whose Tasks are repository
Issues) and shares AI provider capacity between them. GitWeave carries out one
Task. The responsibilities are:

- **ProjectWeave:** discovers managed Projects, observes shared AI usage, admits
  work under each Project's resource policy, **claims** Tasks (select one and set
  it `In Progress`), runs each claimed Task's Project graph, and runs independent
  Tasks concurrently when resources permit.
- **GitWeave:** the workflow inside one Task (implement → PR → review/fix →
  merge → close), defined by the Project's `gitweave.json`.

Canonical state stays where it already lives: GitHub Projects (Task state), Git
and GitWeave (execution and provenance), and the providers (usage). ProjectWeave
keeps only in-memory state (running Tasks, locks, reservations); there is no
database.

## Root workspace and Projects

```text
workspace/
├─ projectweave.json          (shared resource policy, per Project)
└─ projects/
   ├─ app/                    (a Project workspace; "app" is its key)
   │  ├─ project.json  graph.json  gitweave.json
   │  ├─ .gitweave/repos/OWNER/REPO.git   (GitWeave's shared store per repository)
   │  └─ repos/OWNER/REPO/                (only for command executors, cloned on demand)
   └─ api/
```

Managed Projects are auto-discovered: every `projects/<name>/` holding a
`project.json`. The directory name is the Project key. External paths are not
supported. Run every command below from the root workspace.

| Command | What it does |
| --- | --- |
| `projectweave init` | Create (or verify) `projectweave.json` and `projects/`. Never overwrites. |
| `projectweave init-project NAME …` | Create (or verify) `projects/NAME/` for one GitHub Project (see below). |
| `projectweave claim NAME` | Under the Project's lock, select one runnable Task, set it `In Progress`, and print it (or `null`). |
| `projectweave run-task NAME --task FILE\|-` | Run the Project's `graph.json` for an already-claimed Task (JSON from a file or stdin) and print the Run receipt. |
| `projectweave complete NAME --task FILE\|-` | Explicitly set the claimed Task's Project item to `Done`. |
| `projectweave run NAME` | One Task, synchronously: resource admission → `claim` → `run-task` → wait. |
| `projectweave coordinate [--once] [--poll-seconds N]` | The multi-Project loop (below). |
| `projectweave validate --graph FILE` | Static graph validation; no external calls. |

## Task lifecycle

- **claim** selects one runnable Task (open, nonarchived Issue with
  `AI execution` = `Ready` and Status `Todo`, ranked P0/P1/P2 then oldest) and
  sets its Status to `In Progress`, holding a local per-Project file lock
  (`projects/NAME/.projectweave.lock`) only around select → mark. Manual `claim`
  and `coordinate` share the lock, so concurrent claims on one machine never pick
  the same Task. Only `Todo` Tasks are claimed, even if `project.json` sets no
  `eligible_statuses`. The printed Task shows its new Status, `In Progress`.
  Another claim may start while an earlier Task is still running.
- **run-task** runs the Project graph with the claimed Task at `/task`. The graph
  does only what it says: no implicit selection, Status change or completion.
- **complete** sets `Done` explicitly. The default path to `Done` is GitHub's
  built-in Project workflow when the Issue closes (the default GitWeave graph
  closes it after a merge); a custom Project graph can call the `complete`
  action under its own condition.

A Task that fails, or whose PR is left open, stays `In Progress`: it is not
retried automatically and never moved back to `Todo`. Set its Status to `Todo`
yourself to retry. `AI execution` is never changed.

## Shared AI resources

Resource constraints are **opt-in** per Project and provider, in the root
`projectweave.json`:

```json
{
  "projects": {
    "app": {"weight": 2, "resources": {"codex": {"min_remaining_percent": 20, "estimated_usage_percent_per_task": 15}}},
    "api": {"resources": {"codex": {"min_remaining_percent": 20, "estimated_usage_percent_per_task": 5}}}
  },
  "poll_seconds": 300
}
```

- Only listed providers are observed and constrained. **A Project or provider
  that is not listed is not limited**: `coordinate` may start all of that
  Project's eligible Tasks at once. Omission does not forbid a provider.
- Usage is observed read-only, without a model call: Claude via
  `claude -p --output-format json /usage` (the session, weekly all-models and any
  model-specific weekly window), Codex via `codex app-server`
  `account/rateLimits/read` (the `primary` window).
- A candidate Task is admitted only if, for every listed provider and **every**
  observed window: `remaining − reservations of running Tasks −
  estimated_usage_percent_per_task ≥ min_remaining_percent`. Reservations are
  shared across Projects, since the provider allowance is shared.
- Starting a Task reserves its estimates in memory; when it ends the reservation
  is released and usage is observed again. Estimates are conservative safety
  margins that you tune by experience, not accounting.
- An observation failure (not installed, not signed in, timeout, unexpected
  output) makes that provider unknown and blocks Projects that list it.
- ProjectWeave never chooses a provider for a GitWeave node; the Task graph
  declares its own.

## Coordinator

`projectweave coordinate` runs in the foreground:

```text
observe listed providers → next admitted Project (weighted round-robin): claim and launch a Task
        ↑                        (repeat until no Project can take another Task)    │
        └──────────── a Task ends (release, re-observe) or poll_seconds pass ───────┘
```

Independent Projects progress concurrently, and one Project can run several
Tasks at once while admission allows. **Which Project gets the next launch
opportunity** follows a smooth weighted round-robin over the Projects that are
admitted and still have work: each gets credit equal to its optional `weight`
(default 1, a positive integer) per opportunity, and the one with the most
credit goes next. Credit persists across passes, so a Project that sorts first
cannot take all newly available capacity, every runnable Project gets recurring
turns, and a weight-2 Project gets about twice as many as a weight-1 Project
when capacity is scarce. This only orders launches: admission still decides
whether a Task may start, and no capacity is partitioned per Project. Fairness
applies among Projects admitted right now: a Project whose estimate is larger
than the remaining headroom waits while Projects with smaller estimates keep
fitting, so choose estimates with that in mind. Task selection within a Project
stays Project-local; there is no cross-Project Issue ranking. Each finished Task
is reported on stderr as one JSON line; on exit a summary (last observations,
reservations, runs) is printed. SIGINT or SIGTERM stops launching new work and
waits for running Tasks. `--once` makes a single pass, waits for what it
launched, and exits (useful for tests or cron). Exit status 1 means a Task, a
claim, or a Project's setup check failed; a Project whose
`project.json`/`graph.json` is invalid is skipped before claiming, so no Task is
left `In Progress` by a broken setup. The root config and the set of Projects
are read at start: restart `coordinate` after editing `projectweave.json` or
adding a Project. Any unexpected error also waits for the Tasks already running
before `coordinate` exits.

## Default Project workflow

A Project workspace holds two packaged templates:

| File | Layer |
| --- | --- |
| [`graph.json`](../projectweave/templates/graph.json) | Project graph, required: here just `execute` (GitWeave in Issue mode for `/task`) |
| [`gitweave.json`](../projectweave/templates/gitweave.json) | GitWeave Task graph: implement, review and merge one Issue |

The GitWeave Task graph runs, entirely inside GitWeave:

```text
implement → publish PR → review ─┬─ approved → merge → close_issue
                ▲                └─ findings → fix ─┐
                └───────────────────────────────────┘
```

- `implement` implements the Issue and commits with a human-readable message.
- `publish` opens (or updates) a PR that says `Closes #N`.
- `review` checks the PR against the Issue for missing and unnecessary scope,
  correctness and tests.
- `fix` addresses the findings, and the PR is updated again.
- `merge` merges only the reviewed head, **with a merge commit** so GitWeave's
  checkpoint notes stay in the branch history, and never bypasses required checks.
- `close_issue` makes sure the Issue is closed once merged, and comments the
  outcome on the Issue whether or not the PR was merged (in whatever form the
  repository's conventions suggest), when the Run reaches it.
- `max_steps: 30` bounds the loop. With `retries: 0`, a failed or exhausted Run
  stops without merging and leaves the PR for a human.

**This merges into the default branch without a human review** once the review
agent approves. Agents push, open PRs and merge with your `gh` and Git
credentials. Edit `gitweave.json` (for example the `merge` instruction) if you
want a human to merge. The Project graph can be extended with explicit
Project-level steps (for example a `complete` action);
[`examples/`](../examples/) contains specialized feature examples such as the
[review/fix loop](#reviewfix-loop).

## Initialize

Install ProjectWeave, `gh`, and GitWeave on PATH first.

```sh
mkdir workspace && cd workspace
projectweave init
projectweave init-project app --project-owner my-team --project-number 7
projectweave init-project api --project-owner my-team --create-project "API" \
  --resource codex:20:5
```

`init-project NAME` works like a Project-level init in `projects/NAME/`:

- `--project-number N` or `--create-project TITLE` (mutually exclusive) selects
  the GitHub Project; `--project-owner` is required unless `project.json`
  already names the owner. User and organization Projects v2 work; enterprise
  hosts do not. No candidate is chosen when selection is missing.
- `--resource PROVIDER:MIN:ESTIMATE` (repeatable) opts the Project in to shared
  resource admission by adding `projects.NAME.resources.PROVIDER` to the root
  `projectweave.json`. Only missing entries are added; a different existing
  entry is reported as a conflict and never rewritten. Without it the Project is
  unconstrained, and init never invents a policy.
- `--link-repository OWNER/REPO` (repeatable) links same-owner repositories to
  the GitHub Project (see below).
- `--provider codex|claude` and `--model MODEL` shape a new `gitweave.json`
  (below).

It writes three files into `projects/NAME/`:

| File | Purpose / human input |
| --- | --- |
| `project.json` | Project owner/type/number, standard Priority order `P0`, `P1`, `P2`, `eligible_statuses: ["Todo"]` |
| `graph.json` | The Project graph: `execute` GitWeave in Issue mode for the claimed Task; only the GitWeave graph path is made absolute |
| `gitweave.json` | The six-node Task graph (implement → PR → review/fix → merge → close_issue); every agent node uses Codex with its native default model, or the `--provider`/`--model` choices (Claude adds `bypassPermissions`) |

### Provider and model

Without flags, every agent node uses **Codex with its native default model** (no
`model` entry), so the first Run needs no provider/model/graph/instruction edits.
These choices apply to **every agent node** of the GitWeave Task graph.

- `--provider codex|claude` selects the GitWeave provider (default `codex`).
- `--model MODEL` writes an explicit model; without it no model is written and
  the provider's native default applies.
- **`--provider claude` also writes `"permission_mode": "bypassPermissions"`.**
  Non-interactive Claude otherwise cannot edit files or run commands, so the Run
  could not implement the Issue. With it, the agent edits files and runs commands
  in its GitWeave worktree without asking; init's output repeats this. Remove the
  setting from `gitweave.json` if you want to restrict Claude. Codex runs with
  GitWeave's default Codex sandbox (`danger-full-access`).

Install and authenticate the chosen provider CLI yourself. Flags only shape a new
`gitweave.json`: if one exists with a different provider or model, init reports
the conflict and leaves it unchanged. Without flags, an existing file's provider,
model and permission edits are reused.

### Project fields

The default workflow retains the standard Priority order **P0, P1, P2**.
Init reads every page of Project fields before deciding Priority is missing. It
reuses a `Priority` single-select field containing each required option name
exactly once (additional options and any display order are allowed), or creates
that field with P0/P1/P2 options when absent. An incompatible type, missing or
ambiguous required options, or ambiguous field name is reported without repair.
Init also verifies that the Project's `Status` field (GitHub's built-in one) is a
single-select with `Todo`, `In Progress` and `Done` options (claim sets
`In Progress`; `complete` sets `Done`). It never creates or repairs
Status: a missing field or option is reported for you to add in the Project.
Init verifies/creates the `AI execution` single-select field with `Ready` and
`Not ready` options in the same way (other options are allowed; incompatible
fields are reported, never repaired). Init creates no repository labels.
Init never selects Issues, adds Project items, assigns priorities, marks items
ready, runs AI, installs tools, changes auth, or pushes. Unknown or unset
Priority values sort below P0/P1/P2; setting an Issue priority remains a human
choice. Draft Issues are not Tasks. An optional `repository` setting in
`project.json` restricts selection to one repository; init does not emit it but
accepts it in an existing `project.json` as deliberate policy.

### Optionally link repositories

Linking shows the Project in a repository's Projects tab and makes adding its
Issues easy. It is not required by the runtime. Only the named repositories are
linked (nothing is inferred from the current directory or Project items). Init
reads every page of the Project's linked repositories first; an already-linked
repository (compared case-insensitively) is reported under `existing`, and init
never unlinks. A repository with a different owner is rejected before any GitHub
change. Linking does not affect Task selection, add Issues, mark anything
`Ready`, or write anything to the workspace files. A failed link is an init
failure (exit 2): completed pieces stay in the report, and rerunning init with
the same option reuses them.

### Report, reruns and failures

Both graphs receive static validation, including the public `gitweave validate`
command, which runs no agents. Static validity is not live execution readiness.
Workspaces created before the quick-start defaults may still contain
`CONFIGURE_PROVIDER`/`CONFIGURE_MODEL`; init reports them in `missing` until you
set a provider and either a model or no `model` entry.

Init prints JSON with `initialized`, `ready`, `created`, `existing`, `missing`,
`failure`, `human_actions`, and `next_commands`. Exit 0 means mechanical setup
completed; exit 2 means incomplete setup. `ready` stays false: shallow checks
cannot certify provider credentials, Issue comment permission, future Issue
eligibility, or provenance publication access. Init requires working `gh` and
GitWeave commands, `gh` authentication and Project read access. Project or
missing field creation needs Project write access. Failure reports give the
current check and a concrete recovery action; argument errors name the argument.
After a field creation failure (including a lost response), rerun init to
inspect all fields and reuse any compatible field already created.

Rerun `projectweave init-project NAME` to reuse the saved Project selection.
Files are never overwritten; missing files are generated. The small
compatibility check accepts this fixed scaffold with an edited GitWeave
provider/instruction plus optional `model`, `effort`, `sandbox`,
`permission_mode`, per agent node. Other graph or policy edits are reported as
incompatible with this initializer, not repaired or treated as invalid for the
runtime; continue managing a customized setup manually. Symlinked setup files are
rejected. Partial failures leave ordinary files and GitHub state in place, with
completed pieces in the report; the Project identity is saved first, before
field creation, so a rerun can reuse it. If Project creation succeeds remotely
but its response or local save fails, inspect
`GH_HOST=github.com gh project list --owner OWNER` and rerun with
`--project-number NUMBER` before considering another creation. Avoid concurrent
init invocations.

There is no migration from earlier layouts (a single workspace with
`resources.json`, a Project graph that selected and marked Tasks itself,
`projectweave-ready` labels, and so on): create a root workspace and run
`init-project` again, then reapply provider/model edits.

## Running a Task

After installing/authenticating your provider CLI, add an Issue to the GitHub
Project and mark it ready:

```sh
# From the root workspace; replace 7, my-team, app, and the Issue URL.
ISSUE_URL=https://github.com/owner/repo/issues/123
GH_HOST=github.com gh project item-add 7 --owner my-team --url "$ISSUE_URL"
# Then set the item's "AI execution" field to "Ready" and its Status to "Todo".
projectweave run app          # or: projectweave coordinate
```

`run` prints `{"status", "project", "task", "record", "observations"}`: `status`
is `not_admitted` (with `reason`), `no_work`, or the Run's `completed`/`failed`.
`run-task` prints the Run receipt: Run ID, status, failure (or null), executed
steps, per-node results, ordered events, last result, and resources. Exit 0 means
normal completion (including no work or not admitted); exit 1 is a Runtime
Failure; exit 2 is invalid input/setup or a failed claim. Receipts contain
selected Issue content and bounded executor error text, so handle them like
project data. Adding an Issue to the Project and setting fields require Project
write access.

The GitWeave executor runs `gitweave run --graph gitweave.json --repo OWNER/REPO
--issue N` for the claimed Task, with the Project workspace as its working
directory. In this Issue mode GitWeave fetches the repository's default branch
HEAD itself into one shared bare repository per GitHub repository,
`.gitweave/repos/OWNER/REPO.git` (lowercased). Runs reuse it, so only new objects
are fetched, and each Run's records stay in its own refs and notes there (see
GitWeave's runtime docs). Its nodes receive `run_input`
(`{"kind":"issue","number":N}`) and `github_repository`, and read the Issue
themselves. ProjectWeave clones nothing for GitWeave; push the changes you want
included to the default branch. GitWeave's Git transport and the agents' pushes
use your Git credentials, so for HTTPS run `gh auth setup-git` or use SSH. The
default GitWeave agents are instructed to commit their changes with a concise,
human-readable message referencing the Issue; GitWeave then adds its
`GitWeave RUN_ID …` checkpoint commit on top, so the history shows both what
changed and the execution record. If the agent leaves changes uncommitted, the
checkpoint still captures them; only the readable message is missing. GitWeave
pushes provenance refs/notes to the Task's repository during a live Run. If the
workspace is itself a Git repository, ignore `.gitweave/`, `repos/` and
`*.lock`.

For **command executors**, the Run instead resolves the claimed Task's repository
(`task.repository`) to `<Project workspace>/repos/OWNER/REPO`. The first Task
from a repository clones it with `gh repo clone`; later Runs reuse the directory
only if it is itself a Git checkout (not merely inside another repository) whose
`origin` is that repository on github.com (HTTPS or SSH form, case-insensitive).
An existing directory that is not such a checkout is never overwritten or
repurposed: the Run fails. Every execution then runs `git fetch origin`, and the
command receives the checkout path to work against the **remote default branch
tip (`origin/HEAD`)**. If `origin/HEAD` is missing or the default branch was
renamed, run `git remote set-head origin --auto` in the checkout. Clone, fetch or
origin failures are `checkout` Runtime Failures before the executor launches.
Checkout preparation (clone/fetch) is serialized per repository with a lock file
beside it, but concurrent command-executor Tasks of one repository then share the
same checkout directory: make such wrappers safe for that (for example by
working in their own worktree), or do not run them concurrently.

Authenticate `gh` with Project read access (claim), Project write access (Status
updates) and Issue comment permission (the GitWeave graph's `close_issue`
comments the outcome; a custom writeback node also comments). The runtime
targets github.com, including user and organization Projects v2; classic
Projects and enterprise hosts are not supported. A Project workspace can also be
written by hand from the templates and `examples/project.json`; init manages
only files it generated. The GitWeave adapter reads stdout JSON only and does not
import GitWeave; `provenance_remote` optionally maps to GitWeave's
`--provenance-remote`.

## Node reference

All nodes have `kind` and optional `inputs`, an object mapping names to JSON
pointers into the Run context `{project, task, resources, results, last, run_id}`,
where `task` is the claimed Task given to `run-task`. No templating or shell
evaluation occurs. A graph runs only what it lists; the canonical graph is just
`execute` with `inputs.task = "/task"`.

| Node | Required configuration/inputs | Output data |
| --- | --- | --- |
| `action: load` | none | `items`: nonarchived open Issues and their metadata (custom graphs; `claim` does selection by default) |
| `action: select` | `inputs.items` | `task`: highest ranked eligible Issue or null |
| `action: resources` | optional `config.requires` allocation | `resources` snapshot and `available` admission boolean |
| `action: execute` | `executor`, `inputs.task`; optional nonempty `requires`, `inputs.context` | executor's Result data |
| `kind: agent` | same as execute plus `instruction`; omit action | executor's Result data |
| `action: status` | `inputs.task`, `config.status` (an existing Status option) | the Status name set; no comment |
| `action: complete` | `inputs.task`; no config | sets the Task's Status to `Done`; no comment |
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
launch failure. `run-task` supplies no metered envelope, so these metered checks
apply only when a graph is run through the `Runtime` API with one; shared AI
usage is admitted by the coordinator instead (see Shared AI resources). No task
returns null from select; graphs that select should branch before executing.
Missing pointer targets are Runtime Failures.

To change Project Status after execution, add a writeback node with
`"config":{"status":"Done"}`.
Use a conditional branch to select that node only when the executor's structured
outcome warrants it. A GitWeave task verdict can be selected at
`/results/execute/data/outputs/0/data/approved` if its graph returns that field.
The runtime does not equate GitWeave completion with task approval. The canonical
Project graph only executes (the Task was already marked `In Progress` by
`claim`) and has **no writeback or complete node**: the GitWeave Task graph's `close_issue` node comments
the outcome on the Issue, and Status needs no final update (a merged PR closes
the Issue and GitHub sets `Done`; otherwise the Task stays `In Progress`). A custom
graph can still add a writeback node after `execute`; with the default Task graph
the terminal output's data carries `pr` (number, URL, head), `merged`,
`merge_commit` when merged, and `closed`. Writeback posts the full JSON Result,
which can include local paths such as GitWeave's store, so consider that before
using it on public Issues.

## Review/fix loop

[examples/review-fix.json](../examples/review-fix.json) is a Project graph for a
claimed Task (both agents take `inputs.task = "/task"`) whose whole flow is this
loop:

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
approval exits without a fix. Configure both wrapper paths before live use.
Review must return a boolean `data.approved`; wrappers receive the claimed Task
and the preceding result as context. This example
performs no writeback.

Validate without invoking wrappers:
`python3 -m projectweave validate --graph examples/review-fix.json`.
The body must be nonempty. Each loop entry consumes one step, each iteration
start (including the first) consumes another, and body nodes/controls consume
their usual steps. All nested controls share `max_steps`; no body executes after
the limit is exhausted. State and resource balances carry forward, with latest
per-node results and all completed invocations in events. Missing condition data
or any body Runtime Failure stops the Run without retries or continuation.

## Command executor contract

An executor object is either:

```json
{"type":"command","argv":["/path/to/wrapper","--option"],"timeout":3600}
```

or:

```json
{"type":"gitweave","graph":"/path/to/graph.json","timeout":3600}
```

GitWeave receives the selected Task's `OWNER/REPO` as `--repo` and its Issue
number as `--issue`, and runs from the workspace; `repo` is not configurable.

The timeout is positive seconds; default 3600. GitHub requests each have a
120-second timeout. Timeout terminates the direct process group; descendants
that deliberately detach are outside that control. No requests are retried.

Command stdin is one JSON object:

```json
{
  "task": {"title":"Example", "body":"Task description", "repository":"owner/repo"},
  "checkout": "/path/to/workspace/repos/owner/repo",
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

Claims are serialized per Project by a local file lock, so claims on one machine
never select the same Task. The lock and the coordinator's reservations are
single-machine and in-memory: do not run coordinators on several machines (or
several coordinators with separate resource budgets) against the same Projects. A crash after an external effect may leave GitHub
updated without a receipt; there is no resume or idempotency database.

Writeback preflights the requested Status option, posts the comment, then updates
Status. If the second operation fails, the receipt reports the completed comment
reference and preserves the executor result. If a response is lost, remote effects
are unknown. A new Run can duplicate comments, or execute the same task if its
Status is back to `Todo` (for example after a lost In Progress response or a manual
reset); inspect
GitHub and the receipt before rerunning. Failures do not trigger fallback executors.

## Verification

Run `python3 -m unittest discover -s tests -v`. The suite exercises the real CLI
and subprocess boundary with deterministic fake `gh`, `gitweave`, and command
executables (including fake `claude`/`codex` usage observers), covering
pagination, claiming, both execution paths, writeback, failures, resource
admission, and the coordinator's concurrency and reservations.
No live executor or GitHub mutation is part of this suite. It verifies the public
contracts against fixtures, not installed GitWeave behavior, actual permissions,
provider accounting, or network behavior. Real GitWeave execution can consume AI
allowance and publish provenance; live validation remains an operator action.

Init tests use strict external-command fixtures for Git/GitHub/GitWeave, including
first use, reruns, incompatible files, failures and partial recovery. To additionally
validate the generated template with a local GitWeave checkout's public CLI, run
`GITWEAVE_SOURCE=/path/to/gitweave python3 -m unittest discover -s tests -v`.
This optional check is static and performs no AI execution or GitHub mutation.
