# Agent Bridge

> Stop copying plans, decisions, context and reports manually between AI agents.

Structured context, decisions, tasks and results between AI systems and coding
agents. Agent Bridge 0.2.1 is a local-first Python CLI and library with a
provider-neutral JSON protocol. It has **no runtime dependencies**, requires
Python 3.11+, and needs no API key, paid service, Docker stack or network daemon.

Agent Bridge is a provider-neutral handoff/context protocol plus transport and
reference implementation. It is not an AI model or an agent executor. v0.2 adds
explicit Git/GitHub delivery; the canonical envelope remains protocol **0.1**.

The first reference workflow is ChatGPT ↔ Agent Bridge ↔ Codex. The same protocol
can address Claude, Gemini, Cursor, Aider, Hermes, MindOS or your own agent using
ordinary agent identifiers. The reference adapters format packets; they do not
invoke a provider or fetch conversation histories.

## Quick start

From a fresh checkout, create an environment and install the package.

Windows PowerShell, without activating a script:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\agentbridge.exe init --project "Sample project"
.\.venv\Scripts\python.exe examples/local_demo.py --root .validation/demo
.\.venv\Scripts\agentbridge.exe --root .validation/demo status
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Linux/macOS:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
agentbridge init --project 'Sample project'
python examples/local_demo.py --root .validation/demo
agentbridge --root .validation/demo status
python -m unittest discover -s tests -v
```

The demo runs the actual CLI in separate processes: A creates a task → A stores a
handoff → B reads and acknowledges it → B reports → A reads and acknowledges the
report → task status becomes `completed`. All demo data is synthetic and local.
The demo can be run repeatedly. An actual agent runner supplies execution between
handoff acknowledgment and report creation.

For runtime-only installation, Windows uses
`.\.venv\Scripts\python.exe -m pip install .`; activated Linux/macOS uses
`python -m pip install .`. The optional `test` extra supplies JSON Schema
validation for the test suite. With packaging tools and test dependencies
already available, append `--no-index --no-build-isolation --no-deps` to your
platform's pip command for offline installation. The equivalent module entry
point is `.\.venv\Scripts\python.exe -m agent_bridge` on Windows or
`python -m agent_bridge` in the activated Linux/macOS environment.

## Commands

Windows examples use the venv executables directly. Linux/macOS examples assume
the environment was activated as shown above. The command table uses the short
program name to describe flags independently of the executable path.

Global options precede the command: `agentbridge --root PATH --format markdown status`.
The default output is JSON; errors go to stderr with a nonzero exit status.

| Command | Purpose |
| --- | --- |
| `init --project NAME` | Create a local project identity and message log; safe to repeat |
| `status` | Read current project state, decisions, task statuses and next action |
| `state --from AGENT --summary TEXT --data FILE` | Record a structured JSON state object |
| `decision --from AGENT --to AGENT --summary TEXT --rationale TEXT` | Record a decision |
| `task --from AGENT --to AGENT --title TEXT --description TEXT` | Create a pending task |
| `handoff TASK_ID --from AGENT --to AGENT --instructions TEXT` | Dispatch a task with current structured context |
| `report TASK_ID --from AGENT --result TEXT` | Submit a result to the task creator |
| `inbox --agent AGENT` | Read addressed messages; `--all` includes acknowledged messages |
| `acknowledge MESSAGE_ID --by AGENT` | Record receipt; acknowledging a successful report completes its task |
| `approve MESSAGE_ID --by human:NAME --outcome approved --rationale TEXT` | Resolve a human approval request; outcome can also be `rejected` |
| `show MESSAGE_ID` | Read a canonical envelope |
| `export MESSAGE_ID --adapter codex` | Render a Markdown packet; adapters: `generic`, `codex`, `chatgpt` |
| `import FILE` | Validate and ingest one JSON envelope; `-` reads stdin |

Use `agentbridge COMMAND --help` for complete flags. `state`, `handoff` and
`report` accept repeatable `--blocker`, `--risk` and a `--next-action`.
Reports accept `--status succeeded|failed|blocked` and repeatable `--artifact`.
Artifact references are stored as strings, never executed or opened by the bridge.

Manual flow (replace IDs with the JSON output from the preceding command):

Windows PowerShell:

```powershell
.\.venv\Scripts\agentbridge.exe task --from chatgpt --to codex --title "Check sample" --description "Validate synthetic input"
.\.venv\Scripts\agentbridge.exe handoff TASK_ID --from chatgpt --to codex --instructions "Validate the sample and report"
.\.venv\Scripts\agentbridge.exe inbox --agent codex --kind handoff
.\.venv\Scripts\agentbridge.exe export HANDOFF_MESSAGE_ID --adapter codex
.\.venv\Scripts\agentbridge.exe acknowledge HANDOFF_MESSAGE_ID --by codex
.\.venv\Scripts\agentbridge.exe report TASK_ID --from codex --result "Sample passed" --next-action "Review the result"
.\.venv\Scripts\agentbridge.exe inbox --agent chatgpt --kind report
.\.venv\Scripts\agentbridge.exe export REPORT_MESSAGE_ID --adapter chatgpt
.\.venv\Scripts\agentbridge.exe acknowledge REPORT_MESSAGE_ID --by chatgpt
.\.venv\Scripts\agentbridge.exe status
```

Linux/macOS:

```sh
agentbridge task --from chatgpt --to codex --title "Check sample" --description "Validate synthetic input"
agentbridge handoff TASK_ID --from chatgpt --to codex --instructions "Validate the sample and report"
agentbridge inbox --agent codex --kind handoff
agentbridge export HANDOFF_MESSAGE_ID --adapter codex
agentbridge acknowledge HANDOFF_MESSAGE_ID --by codex
agentbridge report TASK_ID --from codex --result "Sample passed" --next-action "Review the result"
agentbridge inbox --agent chatgpt --kind report
agentbridge export REPORT_MESSAGE_ID --adapter chatgpt
agentbridge acknowledge REPORT_MESSAGE_ID --by chatgpt
agentbridge status
```

Task IDs identify work; message IDs identify specific envelopes. Acknowledgment
requires a **message ID**, not a task ID. The inbox also contains task creation
messages and receipt notifications; use `--kind` to select a workflow step.

## Human approval

Windows PowerShell:

```powershell
.\.venv\Scripts\agentbridge.exe decision --from chatgpt --to human:reviewer --summary "Approve sample work" --rationale "Review the scope" --requires-approval
.\.venv\Scripts\agentbridge.exe task --from chatgpt --to codex --title "Sample work" --description "Synthetic task" --approval DECISION_MESSAGE_ID
.\.venv\Scripts\agentbridge.exe approve DECISION_MESSAGE_ID --by human:reviewer --outcome approved --rationale "Scope reviewed"
.\.venv\Scripts\agentbridge.exe handoff TASK_ID --from chatgpt --to codex --instructions "Run approved work"
```

Linux/macOS:

```sh
agentbridge decision --from chatgpt --to human:reviewer --summary "Approve sample work" --rationale "Review the scope" --requires-approval
agentbridge task --from chatgpt --to codex --title "Sample work" --description "Synthetic task" --approval DECISION_MESSAGE_ID
agentbridge approve DECISION_MESSAGE_ID --by human:reviewer --outcome approved --rationale "Scope reviewed"
agentbridge handoff TASK_ID --from chatgpt --to codex --instructions "Run approved work"
```

Handoff is refused until every attached decision is approved or already accepted.
Only the addressed `human:NAME` identifier may resolve a pending approval.
A receipt does not constitute approval. Identifiers are claimed local identities,
not authenticated accounts; control access to the directory and the integration
that records human input. See [SECURITY.md](SECURITY.md).

## Programmatic integration

```python
from agent_bridge import Bridge
from agent_bridge.adapters import GenericAdapter

bridge = Bridge.open(".")
for message in bridge.inbox("hermes"):
    packet = GenericAdapter().encode(message)
    # Give packet to your authorized runner; inspect and acknowledge a handoff.

# An app/MCP/API integration may submit a JSON envelope after obtaining consent:
# message = GenericAdapter().decode(incoming_json)
# bridge.receive(message)
```

Integrations explicitly feed and consume protocol messages. Future ChatGPT
apps/plugins/MCP integrations can use this interface without reading private
chat histories. Hermes/MindOS can implement `AgentAdapter`, choose their own
agent identifiers, and carry vendor metadata in `extensions`. There is no special
provider dependency or hardcoded list of allowed agents.


## Optional two-pass advisory selection

Agent Bridge includes a provider-neutral `TwoPassSelector` for runners that need
advice among several already-permitted destinations or capabilities. Pass 1 ranks
the bounded allowlist; pass 2 verifies the shortlist and may abstain. The selector
never executes, dispatches, retries or persists advice, and it accepts only a
machine-readable task category rather than raw task text.

A private runner can implement the `SelectionAdvisor` protocol with BuildHub
Decision Service / Jev while keeping provider credentials and audit data outside
the bridge log. Existing task, handoff and human-approval rules remain
authoritative. See [two-pass advisory selection](docs/advisory-selection.md) and
the offline [advisory demo](examples/advisory_demo.py).

## Data and project layout

`agentbridge init` creates `.agentbridge/config.json` and an ordered immutable
JSON log under `.agentbridge/messages/`. Current state and task statuses are
derived from that log. Markdown exports are views, not an additional source of
truth. Existing local Markdown placeholders are preserved and not auto-imported.
When the project is inside a Git worktree, `init` verifies that runtime files
are untracked and locally ignored. If needed, it appends an anchored rule to
Git's resolved `info/exclude`, preserving existing contents. This handles nested
projects and linked worktrees, whose exclusion file can be shared with the main
worktree. It leaves tracked `.gitignore`, Git configuration, branches and the
index untouched. An existing effective ignore rule needs no metadata edit.

If Git can run but protection fails, initialization stops before creating new
runtime state and explains the failure. If Git is unavailable, local mode
continues with a warning when a Git marker is detected; keep runtime data out
of staging and rerun `init` when Git becomes available. Rerun `init` after
moving a project into a checkout or changing ignore rules. Git ignores protect
ordinary staging; a deliberate force-add can still stage ignored files.

| Location | Responsibility |
| --- | --- |
| `src/agent_bridge/protocol/` | Envelopes, JSON Schema and workflow rules |
| `src/agent_bridge/storage/` | Storage contract and atomic filesystem log |
| `src/agent_bridge/transport/` | Transport contract and local inbox routing |
| `src/agent_bridge/adapters/` | Generic, Codex and ChatGPT packet codecs |
| `src/agent_bridge/bridge.py` | Shared application/API operations |
| `src/agent_bridge/cli.py` | CLI |
| `tests/` | Unit and process-level integration tests |
| `docs/`, `examples/` | Specification, architecture, workflows and synthetic samples |

Read the [architecture](docs/architecture.md), [protocol specification](docs/protocol.md),
[adapter guide](docs/adapters.md), [security policy](SECURITY.md) and
[contribution guide](CONTRIBUTING.md). An [example configuration](examples/config.example.json)
shows the generated config shape; use `init` to generate a fresh project identity.

Git is optional. Local-only workflows continue to work with no Git configuration.
For connected delivery, see the [Git transport guide](docs/git-transport.md).

## Git/GitHub delivery

Use an existing trusted Git checkout with a configured remote. The transport
keeps an isolated bare cache in `.agentbridge/`, commits only its packet artifacts
and publishes a dedicated branch (default `agentbridge`). GitHub is one possible
host; no repository is created automatically.

Windows PowerShell:

```powershell
.\.venv\Scripts\agentbridge.exe init --project "Sample project"
.\.venv\Scripts\agentbridge.exe git init --repo PATH_TO_EXISTING_CLONE --remote origin --branch agentbridge
.\.venv\Scripts\agentbridge.exe git status
.\.venv\Scripts\agentbridge.exe git sync
.\.venv\Scripts\agentbridge.exe git publish --push
```

Linux/macOS:

```sh
agentbridge init --project "Sample project"
agentbridge git init --repo PATH_TO_EXISTING_CLONE --remote origin --branch agentbridge
agentbridge git status
agentbridge git sync
agentbridge git publish --push
```

The second agent joins the same project with
`agentbridge init --project-id PROJECT_UUID`, then configures its own existing
clone. Get that stable UUID from the first agent's `status` output. Envelopes,
context and reports are subsequently exchanged automatically through Git.

`git status` reports cached transport state; ordinary `status` reports local
application state. `git fetch` fetches, validates and ingests only. `git publish`
and `git sync` fetch, reconcile, ingest and create transport commits. **Push is
off by default**; use `--push`, or explicitly opt in with `git init
--push-by-default`. `--no-push` overrides that policy for one operation.

Publication exports every recorded local envelope for this project, including
context. Use a separate bridge project containing reviewed, shareable summaries
when your coding repository contains private data. Authentication uses the
machine's existing Git/SSH mechanisms; no tokens belong in bridge config.

Offline E2E, using a new empty scratch directory:

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe examples/git_demo.py --root .validation/git-demo
```

Linux/macOS:

```sh
python examples/git_demo.py --root .validation/git-demo
```

It uses two clones, separate CLI processes and a local bare remote, completes a
task in both replicas and checks repeated sync. No GitHub credentials or internet
are needed. See the [Codex and app integration patterns](docs/adapters.md).

## Release status

This is a 0.2.1 candidate for private hosted CI and a final readiness review.
There is no automatic execution, provider connection, background watcher,
authentication, encryption or
conversation-history access. The local log is intended for small projects.

Git sync is explicit and rejects conflicting histories; it is not a distributed
task scheduler. Concurrent conflicting changes to the same task require review.

Licensed under [Apache License 2.0](LICENSE), with a neutral community notice in
[NOTICE](NOTICE). See [CHANGELOG.md](CHANGELOG.md). No package, public repository,
release or tag has been published by this work.

Public launch requires the [publication checklist](docs/publication-checklist.md),
including Ubuntu CI evidence and private vulnerability-reporting verification.
