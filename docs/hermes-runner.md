# Local Hermes explicit-handoff runner

`hermes:orchestrator` is an owner-defined canonical Bridge destination introduced
by this integration. It was not discovered as an installed worker. `hermes:auto`
is a model route and is not interchangeable with this destination.

The role is **LOCAL-ONLY EXPLICIT-HANDOFF HERMES RUNNER**.
**ADVISORY_ONLY_ENFORCED: NO**. The audited installed gateway launches Hermes
with configured tools. This integration does not disable those tools or claim
that a prompt, destination name, empty toolset or unverified `none` toolset does
so. A handoff authorizes a scoped runner invocation, not unrestricted product
execution. The integrating owner must separately enforce the permissible tool
and filesystem scope before live use. No live activation is part of this change.

## Identity, registration, configuration, availability and invocation

- Identity is the canonical protocol address, not availability evidence.
- `HERMES_REGISTRATION.preview(synthetic_registry)` validates a proposed
  registration without mutating the supplied registry, starting Hermes, checking
  the gateway or executing a model. There is no production registry writer.
- Runtime configuration supplies a numeric Windows loopback endpoint, a trusted
  local model, timeout and independently reviewed installation-file SHA-256 pins.
  There are no private paths, credentials, provider clients or default endpoint
  in tracked configuration. Existing Codex adapters/runners remain unchanged.
- `HermesGateway.health()` explicitly calls the audited GET `/health` handler.
  Its sanitized result proves health/local-model configuration, not registration
  or that tools are disabled. Construction/import/registration does no I/O.
- `HermesRunner.run(handoff_message_id)` requires an existing, current, addressed
  Bridge handoff and its existing approval/lifecycle gates. It never scans an
  inbox or starts background work. Only handoff `instructions` are forwarded;
  task context, other messages, project history and conversation data are not.

## Audited interface and fail-closed contract

The installed interface is GET `/health` followed by POST `/delegate`. The only
delegation fields are `prompt`, `backend: "wsl"`, and `forceOffline: true`.
Session/resume/history, model overrides, fallback options and extra fields are
rejected. The broader gateway default/native routing is never used.

Before a later operator configures the runner, independently verify and pin:

1. `gateway`: the owned delegation gateway selects only the WSL runner for
   `backend: "wsl"`, forwards offline mode, and has no startup generation.
2. `router`: forceOffline selects the approved offline model, with a one-item
   fallback chain. Explicit model overrides are not sent by this client.
3. `launcher`: the gateway's command builder propagates the local-only policy
   into the WSL child and adds resume only when supplied (this client omits it).
4. `wsl_policy`: the actual installed WSL inference guard checks exact local
   model/provider/base URL before provider setup and dispatch.
5. `wsl_fallback`: the installed WSL fallback configuration filters non-local
   routes under that policy.

Pins must be independently approved after reviewing those facts, not computed
and blindly trusted by the runner. Configure paths to the actual installed
files (including WSL-owned files through a read-only local filesystem view), not
copies that can drift from the running installation. Operator activation must
also prove the endpoint's running process uses that reviewed installation and
that its local backend/tool scope remains permitted. File hashes alone cannot
attest process identity, in-memory code or model availability. This PR does not
provision that proof or alter the gateway, WSL, profile or supervisor.

Missing/unreadable/changed pins, wrong health/model identity, transport failure,
timeout, redirect, malformed JSON, missing terminal completion or non-local
response metadata fail closed. Report verification requires completed status,
zero exit code, WSL backend, local provider/model and one local fallback entry.
Requests use numeric loopback without environment proxies or redirects, bounded
response size and timeout. There is no retry or provider/API fallback.

The acknowledgment is persisted under the existing Store transaction before
dispatch. Completed replay returns the same report; an acknowledgment without
a report blocks redispatch, including after a crash. Timeout is an uncertain
external outcome, not proof that the child stopped: no automatic replay or
recovery occurs. An operator must investigate before authorizing new work.

Reports retain the existing Bridge schema/lifecycle. Raw stderr, session IDs,
private health metadata and arbitrary gateway error strings are discarded.
Common credential-bearing input is rejected and credential patterns in output
are redacted. Pattern filtering is not a general data-loss prevention guarantee;
the explicit handoff and permitted tool scope must themselves be shareable.

## Offline registration preview and API

```python
from agent_bridge.hermes import HERMES_REGISTRATION

synthetic_registry = {"codex:worker": "existing codex runner"}
preview = HERMES_REGISTRATION.preview(synthetic_registry)
# destination: hermes:orchestrator
# registration_valid: True
# runtime_required: hermes local gateway runner
# applied: False
```

After separate operator verification, a private caller may construct
`HermesConfig(endpoint, local_model, contract_files, timeout_seconds)` where
`contract_files` is a tuple of `(role, ContractFile(absolute_path, approved_sha256))`.
Create `HermesGateway(config)` and `HermesRunner(bridge, gateway)`; neither
delegates until `runner.run(explicit_handoff_message_id)` is called.
`HermesAdapter` is only the normal Bridge JSON/Markdown codec.

A temporary Decision Service binding may contain
`{"revision":1,"bindings":{"codex":"codex:worker","hermes":"hermes:orchestrator"}}`.
Syntactic validation and sanitized `bound=true` diagnostics do not prove a live
registered destination. Leave production registries/bindings untouched.

Offline tests use a synthetic loopback HTTP gateway, reviewed fake pin files and
temporary Bridge stores. No Hermes runtime, provider/model or live registry is
invoked. Registration, a two-candidate preflight and any canary require separate
operator actions after independent review; this integration is not activated.
