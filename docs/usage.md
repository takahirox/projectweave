# CLI and executor guide

## Default workflow

ProjectWeave has one canonical default workflow, packaged as a pair of files that
init copies and these docs describe:

| File | Layer |
| --- | --- |
| [`projectweave/templates/graph.json`](../projectweave/templates/graph.json) | ProjectWeave graph: how the Project is operated |
| [`projectweave/templates/gitweave.json`](../projectweave/templates/gitweave.json) | GitWeave Task graph: how one selected repository Issue is implemented, reviewed and merged |
| [`projectweave/templates/resources.json`](../projectweave/templates/resources.json) | The subscription stop line the Project graph checks |

The Project graph runs:

```text
load → select ─┬─ no Task → no_work Result
               └─ Task → subscription check ─┬─ below/at stop line or unknown → stop
                                             └─ above → start (Status = In Progress)
                                                        → execute (GitWeave, Issue mode)
                                                        → writeback
```

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
- `close_issue` makes sure the Issue is closed once merged.
- `max_steps: 30` bounds the loop. With `retries: 0`, a failed or exhausted Run
  stops without merging and leaves the PR for a human.

**This merges into the default branch without a human review** once the review
agent approves. Agents push, open PRs and merge with your `gh` and Git
credentials. Edit `gitweave.json` (for example the `merge` instruction) if you
want a human to merge.

Task selection can be changed by editing a copy; the template is the recommended
minimal workflow. [`examples/`](../examples/) contains specialized feature
examples (for example the [review/fix loop](#reviewfix-loop)), not alternative
defaults.

## Initialize a Project workspace

A ProjectWeave Project is one GitHub Project whose Tasks are ordinary repository
Issues from one or more repositories. A single-repository Project is simply the
case where every Task belongs to one repository.

Each Project has one local **workspace**: the directory containing `project.json`.
It holds the Project configuration and graphs, and GitWeave's Run data:

```text
workspace/
├─ project.json  resources.json  graph.json  gitweave.json
├─ .gitweave/repos/OWNER/REPO.git   (GitWeave's shared store per GitHub repository)
└─ repos/OWNER/REPO/                (only for command executors, cloned on demand)
```

Install ProjectWeave, `gh`, and GitWeave on PATH first. From an empty directory
that will be the workspace (it need not be a Git repository and init does not run
`git init`), explicitly select a Project:

```sh
mkdir my-project && cd my-project
projectweave init --project-owner my-team --project-number 7
```

Or explicitly create one (no discovery or automatic creation):

```sh
projectweave init --project-owner my-team --create-project "First Run"
```

`--project-number` and `--create-project TITLE` are mutually exclusive.
`--project-owner` is required unless `project.json` already names the owner.
GitHub determines whether that login is a user or organization; both Projects v2
owner types work. An existing `project.json` is reused, including when rerunning
with `--create-project`. Conflicting explicit selections fail. No candidate is
chosen when selection is missing. Enterprise hosts are unsupported. Init clones
no repository, and names or inspects one only when you pass `--link-repository`.

### Optionally link repositories

Linking shows the Project in a repository's Projects tab and makes adding its
Issues easy. It is not required by the runtime. Opt in with one or more
repositories owned by the Project owner:

```sh
projectweave init --project-owner my-team --create-project "First Run" \
  --link-repository my-team/app --link-repository my-team/api
```

Only the named repositories are linked (nothing is inferred from the current
directory or Project items). Init reads every page of the Project's linked
repositories first; an already-linked repository (compared case-insensitively)
is reported under `existing`, and init never unlinks. A repository with a
different owner is rejected before any GitHub change. Linking does not affect
Task selection, add Issues, mark anything `Ready`, or write anything to the
workspace files; the link lives in GitHub. It needs permission to link the
repository. A failed link is an init failure (exit 2) reported like field
creation: completed pieces stay in the report, and rerunning init with the same
option reuses them. Without the option, init's behavior and output are unchanged.

### Quick start: provider and model

Without further flags, init sets up **Codex with its native default model** (no
`model` entry), so the first Run needs no provider/model/graph/instruction edits.
The quick-start flow is: init → add an Issue to the Project and set
`AI execution` = `Ready` → record `remaining_percent` in `resources.json` →
`projectweave run`. Optional overrides:

```sh
projectweave init --project-owner my-team --project-number 7 --provider claude
projectweave init --project-owner my-team --project-number 7 --model MODEL
```

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

Init creates four ordinary JSON files in the current directory (the workspace).
`graph.json`, `gitweave.json` and `resources.json` are copies of the packaged
templates; only the GitWeave graph path in `graph.json` is made absolute:

| File | Purpose / human input |
| --- | --- |
| `project.json` | Project owner/type/number, standard Priority order `P0`, `P1`, `P2`, `eligible_statuses: ["Todo"]` |
| `resources.json` | One `subscription` entry with `stop_at_remaining_percent: 20` and **no** `remaining_percent`; record the observed remaining usage before each Run |
| `graph.json` | Load, select an eligible Issue whose Status is `Todo` from any repository in the Project (or return `no_work`), check the subscription threshold, set its Status to `In Progress`, run GitWeave in Issue mode for that Issue, comment the result |
| `gitweave.json` | The six-node Task graph (implement → PR → review/fix → merge → close_issue); every agent node uses Codex with its native default model, or the `--provider`/`--model` choices (Claude adds `bypassPermissions`) |

The default workflow retains the standard Priority order **P0, P1, P2**.
Init reads every page of Project fields before deciding Priority is missing. It
reuses a `Priority` single-select field containing each required option name
exactly once (additional options and any display order are allowed), or creates
that field with P0/P1/P2 options when absent. An incompatible type, missing or
ambiguous required options, or ambiguous field name is reported without repair.
Init also verifies that the Project's `Status` field (GitHub's built-in one) is a
single-select with `Todo` and `In Progress` options. It never creates or repairs
Status: a missing field or option is reported for you to add in the Project.
Init verifies/creates the `AI execution` single-select field with `Ready` and
`Not ready` options in the same way (other options are allowed; incompatible
fields are reported, never repaired). Init creates no repository labels.
Init never selects Issues, adds Project items, assigns priorities, marks items
ready, runs AI, installs tools, changes auth, or pushes. Selection at Run time uses
open, nonarchived Project Issues from any repository whose `AI execution` is
`Ready` and whose Status is `Todo`, ranked by P0/P1/P2, then oldest Issue and existing tie-breakers. Unknown
or unset Priority values sort below those three; setting an Issue priority remains
a human choice. Draft Issues are not Tasks. An optional `repository` setting in
`project.json` restricts selection to one repository; init does not emit it but
accepts it in an existing `project.json` as deliberate policy.

Both graphs receive static validation, including the public `gitweave validate`
command, which runs no agents. Static validity is not live execution readiness.
Workspaces created before the quick-start defaults may still contain
`CONFIGURE_PROVIDER`/`CONFIGURE_MODEL`; init reports them in `missing` until you
set a provider and either a model or no `model` entry.

Init prints JSON with `initialized`, `ready`, `created`, `existing`, `missing`,
`failure`, `human_actions`, and `next_commands`. Exit 0 means mechanical setup
completed; exit 2 means incomplete setup. `ready` stays false: shallow checks cannot
certify provider credentials, Issue comment permission, future Issue eligibility,
or provenance publication access. Remaining configuration blockers are listed in
`missing`; access/operational checks are listed in `human_actions`, even after the
remaining usage is recorded. Init requires working `gh` and GitWeave
commands, `gh` authentication and Project read access.
Project or missing field creation needs Project write access. Failure reports give the current check
and a concrete recovery action. A field read failure stops creation; after a field
creation failure (including a lost response), rerun init to inspect all fields and
reuse any compatible field already created. Saved files and Project identity are
retained for recovery. No authentication scopes are changed automatically.

Existing files are never overwritten, including scaffolds from an earlier version
with `priority_order: []`. Init reports those as incompatible; manually set
`priority_order` to `["P0", "P1", "P2"]` in `project.json`, then rerun. Init does
not migrate files or change existing field types/options. The field mutation uses
GitHub's [Projects GraphQL contract](https://docs.github.com/en/graphql/reference/projects#createprojectv2fieldinput)
with explicit single-select option names, neutral colors and empty descriptions.

After installing/authenticating your provider CLI yourself and recording
`remaining_percent`, follow the printed commands. For example:

```sh
# From the workspace; replace 7, my-team, and the Issue URL with your choices.
gitweave validate --graph gitweave.json
projectweave validate --graph graph.json
ISSUE_URL=https://github.com/owner/repo/issues/123
GH_HOST=github.com gh project item-add 7 --owner my-team --url "$ISSUE_URL"
# Then set the item's "AI execution" field to "Ready" in the Project.
projectweave run --graph graph.json --project project.json --resources resources.json
```

Adding an Issue to the Project and setting `AI execution` require Project write
access. Init does neither operation.

The default policy is a subscription stop line. Before each Run, set
`resources.json` `subscription.remaining_percent` to the remaining usage (0–100)
of the provider subscription used by `gitweave.json`, for example 45. The graph
starts a Task only while `remaining_percent > stop_at_remaining_percent` (default
20; edit it deliberately). With the value absent or null (unknown), at or below
the stop line, or with no eligible Issue, this graph neither launches GitWeave nor
posts a comment, and the Task's Status is left unchanged. The name
`subscription` is only a label; ProjectWeave does not
observe provider usage, estimate it, or infer the provider. The file is reloaded
each Run. Renaming the entry (for example to `codex`) or checking several
subscriptions is a custom graph/resources edit that init's compatibility check
reports as incompatible; manage such a setup manually.
Once the threshold check passes, the `start` node sets the Task's Status to
`In Progress` **before** GitWeave launches. A Task is therefore never selected
again, even if the Run then fails or leaves the PR open; set its Status back to
`Todo` to retry it. ProjectWeave never sets `Done`: GitHub's built-in Project
workflows do when the Issue is closed (the default GitWeave Task graph closes it
after a merge). `AI execution` is never changed. If the Status update itself
fails, the Run stops with a `status` Runtime Failure before launching anything.

The GitWeave executor runs `gitweave run --graph gitweave.json --repo OWNER/REPO
--issue N` for the selected Task, with the workspace as its working directory.
In this Issue mode GitWeave fetches the repository's default branch HEAD itself
into one shared bare repository per GitHub repository,
`.gitweave/repos/OWNER/REPO.git` (lowercased). Runs reuse it, so only new objects
are fetched, and each Run's records stay in its own refs and notes there (see
GitWeave's runtime docs). Its nodes receive
`run_input` (`{"kind":"issue","number":N}`) and `github_repository`, and read
the Issue themselves. ProjectWeave clones nothing for GitWeave; push the changes
you want included to the default branch. GitWeave's Git transport and the agents'
pushes use your Git credentials, so for HTTPS run `gh auth setup-git` or use SSH.

For **command executors**, the Run instead resolves the selected Issue's repository
(`task.repository`) to `<workspace>/repos/OWNER/REPO`. The first Task from a
repository clones it with `gh repo clone`; later Runs reuse the directory only if
it is itself a Git checkout (not merely inside another repository) whose `origin`
is that repository on github.com (HTTPS or SSH form, case-insensitive). An existing directory that is not such a checkout is never
overwritten or repurposed: the Run fails. Every execution then runs `git fetch
origin`, and the command receives the checkout path to work against the **remote
default branch tip (`origin/HEAD`)**. If `origin/HEAD` is missing or the
default branch was renamed, run `git remote set-head origin --auto` in the
checkout. Clone,
fetch or origin failures are `checkout` Runtime Failures before the executor
launches, with no Issue comment. The graph file path is absolute; a moved
workspace needs it updated. The default GitWeave agents are instructed to commit
their changes with a concise, human-readable message referencing the Issue; GitWeave
then adds its `GitWeave RUN_ID …` checkpoint commit on top, so the history shows
both what changed and the execution record. If the agent leaves changes
uncommitted, the checkpoint still captures them; only the readable message is
missing. GitWeave pushes provenance refs/notes to the Task's repository during a
live Run. If the workspace is itself a Git repository, ignore `.gitweave/` and
`repos/`. Review your Git
identity, permissions and provenance destination before executing; init neither
runs a probe agent nor promises live Run success.

Rerun `projectweave init` in the workspace to reuse saved Project selection.
Files are never overwritten; missing files are generated. The small compatibility
check accepts this fixed scaffold with edited `remaining_percent` /
`stop_at_remaining_percent` and GitWeave
provider/instruction plus optional `model`, `effort`, `sandbox`, `permission_mode`.
Other graph or policy edits are reported as incompatible with this initializer,
not repaired or treated as invalid for the runtime. Continue managing a customized
setup manually. Symlinked setup files are rejected. In `gitweave.json` the
editable fields are per agent node. A single-node `gitweave.json` from an earlier
template is reported as incompatible: regenerate the workspace (there is no
migration).

Partial failures leave ordinary files and GitHub state in place, with completed
pieces in the report. The Project identity is saved first, before field creation,
so a rerun can reuse it. There is no rollback, retry loop or setup database. If
Project creation succeeds remotely but its response or local save fails, inspect
`GH_HOST=github.com gh project list --owner OWNER` and rerun with `--project-number NUMBER` before
considering another creation. Avoid concurrent init invocations.

### Migrating a single-repository setup

Earlier versions ran `projectweave init --repo owner/repo` inside a checkout and
wrote `.projectweave/` there, with a fixed `repo`/`commit` in the GitWeave
executor. That setup remains conceptually valid: it is a Project whose Tasks all
belong to one repository. To migrate:

1. Create a workspace directory outside the checkout and copy the four files from
   `.projectweave/` into it.
2. In `graph.json`, delete the executor's `repo` and `commit` (now rejected) and
   point `graph` at the workspace's `gitweave.json`.
3. Optionally delete `"repository"` from `project.json` to select Issues from all
   repositories in the Project; keeping it remains a supported filter.
4. Run from the workspace. GitWeave fetches the repository itself (Issue mode);
   the old checkout is no longer used.

Alternatively run `projectweave init --project-owner OWNER --project-number N` in
a new workspace and reapply your provider/model edits. Init now emits the
subscription threshold policy instead of `gitweave` run capacity; an old
`resources.json` with `gitweave` capacity keeps working with its old graph but is
reported as incompatible by init. Likewise a `graph.json` generated before the
canonical template (node names `capacity`/`comment`, no `no_work` branch) keeps
running but is reported as incompatible; replace it with a fresh copy of the
template if you want init to manage it.

### Migrating from the `projectweave-ready` label

Eligibility is now the Project field `AI execution` = `Ready`; labels are ignored
and there is no fallback. For an existing setup:

1. Delete `"label"` from `project.json` (the runtime and init reject it).
2. Rerun `projectweave init` to create or verify the `AI execution` field.
3. For each Project item that had the label, set `AI execution` to `Ready`.
4. Optionally delete the `projectweave-ready` label from each repository.

## Manual setup and one Run

Install Python 3.11+, GitHub CLI (`gh`), and the chosen executor. Authenticate `gh`
with access to the project and its Issues: project read access for loading,
project write access for Status updates, and Issue comment permission for
writeback. The runtime targets github.com, including user and organization
Projects v2; classic Projects and enterprise hosts are not supported.

Copy `projectweave/templates/graph.json`, `gitweave.json`, `resources.json`
and `examples/project.json` into a workspace directory, replace the project
owner/number, and adjust
Priority and Status names to the actual single-select field values. Values are
case-sensitive. Optional `eligible_statuses` is an additional filter; remove it
to select by open Issue plus `AI execution` = `Ready` alone. Unknown or absent Priority values
sort below all configured values. Priority ordering is explicit, not inferred
from the order returned by GitHub.

The template's GitWeave graph path `gitweave.json` is relative, so run from the
workspace (or make it absolute). Init manages only files it generated: in a
hand-copied workspace it reports this relative path as incompatible. The template
uses Codex with its native default model; edit `gitweave.json` for another
provider/model (Claude needs a `permission_mode`, see above), and set
`remaining_percent` in `resources.json`. The workspace is the
directory containing the `--project` file; GitWeave runs there in Issue mode, and
command executors get a checkout under its `repos/`, as described above.
Use a GitWeave installation matching its documented v0 `run` CLI contract. The
adapter reads stdout JSON only; it does not import GitWeave. Its graph owns task
instructions, model choices, artifact handling, and any publication actions.
`provenance_remote` optionally maps to GitWeave's `--provenance-remote`; otherwise
GitWeave applies its own destination defaults. All paths (including relative
paths in graph options and argv) resolve from the invoking working directory.
Command executors receive the resolved checkout path in the request's
`checkout` field.

```sh
python3 -m projectweave validate --graph graph.json
python3 -m projectweave run --graph graph.json \
  --project project.json --resources resources.json > run.json
```

`validate` makes no external calls. `run` emits a JSON receipt on stdout: Run ID,
status, failure (or null), executed steps, per-node results, ordered events, last
result, and remaining/charged resources. A repeated node ID replaces that ID's
entry; events retain every invocation. Exit 0 means normal graph completion
(including no work, exhausted resources, or a negative task outcome); exit 1 is a
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
| `action: resources` | optional `config.requires` allocation and/or `config.subscriptions` names | `resources` snapshot and `available` admission boolean |
| `action: execute` | `executor`, `inputs.task`; optional nonempty `requires`, `inputs.context` | executor's Result data |
| `kind: agent` | same as execute plus `instruction`; omit action | executor's Result data |
| `action: status` | `inputs.task`, `config.status` (an existing Status option) | the Status name set; no comment |
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
launch failure. `resources.config.subscriptions` additionally requires each named
subscription's `remaining_percent` to be known and above its
`stop_at_remaining_percent`; it is checked only by this action, so branch on
`data.available` before executing. No task returns null from select; graphs should branch before
executing, as the canonical template does. Missing pointer targets are Runtime Failures.

To change Project Status, give a writeback node `"config":{"status":"Done"}`.
Use a conditional branch to select that node only when the executor's structured
outcome warrants it. A GitWeave task verdict can be selected at
`/results/execute/data/outputs/0/data/approved` if its graph returns that field.
The runtime does not equate GitWeave completion with task approval. The canonical
template marks the Task `In Progress` with a `status` node before executing and
comments the outcome without setting a final Status. With the default Task
graph, the terminal output's data carries `pr` (number, URL, head), `merged`,
`merge_commit` when merged, and `closed`, so the Issue comment names the pull
request, including when it was left open for a human.

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

The resources snapshot is after reservation; subscription entries appear as
supplied, without `charged`. Action execute sets instruction to
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
are unknown. A new Run can duplicate comments, or execute the same task if its
Status is back to `Todo` (for example after a lost In Progress response or a manual
reset); inspect
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

Init tests use strict external-command fixtures for Git/GitHub/GitWeave, including
first use, reruns, incompatible files, failures and partial recovery. To additionally
validate the generated template with a local GitWeave checkout's public CLI, run
`GITWEAVE_SOURCE=/path/to/gitweave python3 -m unittest discover -s tests -v`.
This optional check is static and performs no AI execution or GitHub mutation.
