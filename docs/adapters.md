# Adapter and integration guide

## Reference adapters

The `AgentAdapter` typing protocol requires `name`, `encode(Message) -> str`,
`decode(str) -> Message`, and `render(Message) -> str`. `GenericAdapter` implements
canonical JSON and safe fenced Markdown views. `CodexAdapter` and `ChatGPTAdapter`
inherit that codec and add reference workflow instructions.

These are formatting adapters. They do not open provider sessions, access chat
history, call an LLM, watch files, or execute a task. JSON messages can be passed
through a future app/plugin/MCP server/API or through files. An authorized runner
uses `Bridge` to consume and produce envelopes.

```python
from agent_bridge import Bridge
from agent_bridge.adapters import GenericAdapter

class HermesAdapter(GenericAdapter):
    name = "hermes"

    def render(self, message):
        return super().render(message) + "\nReturn a structured result to the bridge.\n"

bridge = Bridge.open(".")
adapter = HermesAdapter()
for handoff in bridge.inbox("hermes"):
    if handoff.kind != "handoff":
        continue
    bridge.acknowledge(handoff.message_id, actor="hermes")
    packet = adapter.encode(handoff)
    # Supply packet to your runner under its own authorization rules.
    # When authorized execution finishes:
    # bridge.report(handoff.task_id, source="hermes", result=result_text)
```

Provider-neutral agent identifiers do not have to match adapter names. You can
address `hermes:worker-1` and use a generic codec, or define `mindos:planner` with
a custom codec. Optional metadata belongs in an `extensions` namespace such as
`org.example.mindos`. The built-in CLI registry is deliberately small; a new
CLI codec can be registered in `adapters/__init__.py`. Dynamic plugin loading is
outside v0.1.

## Producing an external report

An integration receives an existing handoff envelope, records its receipt, then
creates a **new** report envelope with the same project/task/correlation and the
handoff as parent:

```python
from agent_bridge import Message
from agent_bridge.adapters import GenericAdapter

report = Message.create(
    project_id=handoff.project_id,
    kind="report",
    source=handoff.destination,
    destination=handoff.source,
    task_id=handoff.task_id,
    correlation_id=handoff.correlation_id,
    in_reply_to=handoff.message_id,
    summary="Synthetic result",
    status="succeeded",
    payload={
        "result": "Sample validation passed; include execution evidence here.",
        "artifacts": [],
        "blockers": [],
        "risks": [],
        "next_action": "Task creator should review and acknowledge.",
    },
    extensions={"org.example.runner": {"mode": "synthetic"}},
)
incoming_json = GenericAdapter().encode(report)
bridge.receive(GenericAdapter().decode(incoming_json))
```

Incoming parents must exist locally, in causal order. The source/destination
must agree with the assigned agent and task creator. Identical replay is safe;
a conflicting ID, stale report, unknown project or pending approval is rejected.

For file-based use, save the JSON and run `agentbridge import report.json`.
`agentbridge show MESSAGE_ID` emits canonical JSON. `export` emits Markdown
and is intended for reading in an agent session. The example
`examples/chatgpt-handoff.json` is a synthetic format specimen with fixed IDs;
it is not a standalone import into a newly initialized project.

## ChatGPT ↔ Codex reference workflow

1. A user or future ChatGPT integration creates a structured task addressed to
   `codex`. ChatGPT can propose decisions and structured state separately.
2. A handoff captures the latest project state, decision summaries and target
   task. Export a `codex` Markdown packet or supply the canonical JSON through an
   authorized integration.
3. The coding agent inspects repository instructions and scope, records receipt,
   performs the authorized work and writes a result through the CLI or API.
4. The task creator reads the report as JSON or a `chatgpt` packet, reviews the
   result and records receipt to complete a successful task.

Manual packets are a useful fallback, while machine integration can eliminate
conversation copying entirely. No unsupported direct access to ChatGPT history
is attempted. Human approval records must originate from actual human input in
the integrating application; the local bridge cannot verify that provenance.

## Future transport contracts

`Bridge(store, transport)` accepts objects implementing `Store` and `Transport`.
The Store contract exposes transaction, config, read and append. `Bridge`
validates workflow rules before appending. A transaction must serialize the
complete read/validate/write operation. `Transport.inbox()` projects addressed
messages from a validated sequence; future remote ingestion should enter through
`Bridge.receive()` rather than writing files directly.

An HTTP/MCP integration needs its own delivery/authentication layer around
these operations. Preserve project identity, message IDs and parent order;
choose conflict handling before supporting concurrent remote writers. An
optional Git workflow can version sanitized exported packets without making
Git mandatory for local use. Raw `.agentbridge/` data is ignored by default.

## v0.2 Git workflow for Codex

Use a configured shared-project bridge and an authorized Codex session:

```sh
agentbridge git sync --no-push
agentbridge inbox --agent codex:worker --kind handoff
agentbridge export HANDOFF_MESSAGE_ID --adapter codex
agentbridge acknowledge HANDOFF_MESSAGE_ID --by codex:worker
# The authorized session performs the scoped work and validation here.
agentbridge report TASK_ID --from codex:worker --result "Describe verified results" --next-action "Creator should review"
agentbridge git publish --push
```

`python examples/codex_inbox.py --root PATH --agent codex:worker` combines sync
and packet display. It does not acknowledge, execute instructions or publish.
Execution and report claims stay with the authorized runner. Human approval
decisions remain governed by the existing task lifecycle.

## ChatGPT app/MCP boundary

An authorized integration can use the generic Python API:

```python
bridge = Bridge.open("/path/to/local-shareable-bridge-project")
bridge.sync(push=False)
task = bridge.task(source="chatgpt:planner", destination="codex:worker",
                   title="Sample task", description="Shareable scoped work")
handoff = bridge.handoff(task.task_id, source="chatgpt:planner", destination="codex:worker",
                        instructions="Perform the authorized task and report")
bridge.sync(push=True)  # Requires actual authorization to publish this context.

# On a later explicitly authorized request:
bridge.sync(push=False)
for report in bridge.inbox("chatgpt:planner"):
    if report.kind == "report":
        packet = GenericAdapter().encode(report)
        # Present packet to the reviewer. After review/approval:
        # bridge.acknowledge(report.message_id, actor="chatgpt:planner")
        # bridge.sync(push=True)
```

The integration owns session authentication, consent, trusted human approvals
and data minimization. Incoming externally prepared JSON still uses
`GenericAdapter.decode()` followed by `Bridge.receive()`. No history access,
OpenAI credentials or private endpoint is hardcoded. MCP scheduling and provider
connections are future integration work, not automatic behavior of this library.

## Hermes, MindOS and private organizations

The owner-defined canonical Hermes destination now has an opt-in local runner
and codec. See [Local Hermes runner](hermes-runner.md) for registration preview,
explicit invocation, local-only policy proof and the boundary that configured
Hermes tools remain enabled. The generic examples below do not install workers.

Use generic identifiers such as `chatgpt:planner`, `codex:worker`,
`hermes:orchestrator` and `mindos:planner`. These are sanitized examples, not
an internal organization configuration. Keep private deployment mappings and
secrets in your own repository/configuration. No private integration code is
required in the public project.

```python
bridge.sync(push=False)
for handoff in bridge.inbox("hermes:worker"):
    if handoff.kind == "handoff":
        packet = GenericAdapter().encode(handoff)
        # Inspect, acknowledge, call an authorized runner, then bridge.report().
        # Reuse AgentAdapter; no provider-specific protocol fields are needed.
```

MindOS follows the same pattern with a different address or adapter. See the
[Git transport guide](git-transport.md) for first-time identity joining and
explicit publication boundaries.
