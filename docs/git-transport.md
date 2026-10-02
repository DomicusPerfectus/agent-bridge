# Git transport v0.2

## What Git transports

Git carries immutable canonical envelopes and small dependency sidecars.
Application state remains the validated local `.agentbridge/messages/` log.
Git configuration, credentials, branch names and ordering metadata are never
added to protocol envelopes. Protocol version remains **0.1**.

Agent Bridge is a handoff/context protocol with transport/reference code. It is
not a model or executor. An authorized runner decides what to do with a handoff.

## Configuration and identity

Each participant uses an existing trusted local Git repository with exactly one
explicit URL for its selected remote. A separate mailbox repository is useful
when code and shareable context have different access policies.

```sh
agentbridge init --project "Community sample"
agentbridge git init --repo PATH_TO_CLONE --remote origin --branch agentbridge
agentbridge git status
```

The first participant's local `status` supplies the shared project UUID. A peer
joins with `agentbridge init --project-id PROJECT_UUID`, then configures its own
clone of the same mailbox. Existing bridge identity is never overwritten; a
mismatched explicit UUID fails. Project labels may differ between participants.

Initialization protects runtime data in the bridge root's worktree. It verifies
that `.agentbridge/` is untracked and ignored, and appends a scoped anchored rule
to Git's resolved local `info/exclude` only when needed. Existing bytes are
preserved; linked worktrees can share the main worktree's file. No tracked
`.gitignore`, index, branch or Git configuration is edited. Git configuration
also rechecks protection, covering a bridge created before its worktree existed.
With Git available, an unprotectable runtime path stops initialization with a
warning/error. Git-unavailable local mode warns when a worktree marker is found.

`.agentbridge/git.json` records the normalized local repository path, actual Git
directory, selected remote name, pinned endpoint, branch, project ID, object
format and explicit push policy. The file is private/local and Git-ignored.
`examples/git-config.example.json` is a sanitized shape specimen; generate the
real file with `git init`. Credentials are rejected in HTTPS URLs and must use
existing Git authentication. HTTPS, SSH/scp and existing local directories are
supported; HTTP, git://, file:// URLs and external remote-helper protocols are
rejected. Local directory paths support the offline workflow.

Every operation verifies the configured repository/Git directory and remote
endpoint. A substitution or configuration change blocks sync. To change this
binding, review the new repository/endpoint and configure a separate bridge
directory; v0.2 does not silently migrate cached histories. Initialization is
idempotent for identical settings. SHA-1 and SHA-256 object formats are supported
when Git and the remote agree.

## Exact branch layout

The default transport branch is `refs/heads/agentbridge`:

```text
.agentbridge-remote/
  projects/
    <project-uuid>/
      messages/<message-uuid>.json
      links/<message-uuid>.json
```

A message file is the protocol 0.1 envelope, with no added fields. Its immutable
sidecar is:

```json
{
  "format": "agentbridge-git/1",
  "message_id": "22222222-2222-4222-8222-222222222222",
  "after": "11111111-1111-4111-8111-111111111111"
}
```

`after` is the immediately preceding event in the publishing replica's validated
local log, or `null` for its first event. It preserves order that the envelope
does not express directly: receipt before report, human resolution before
dispatch, and successive project state updates. Sidecars contain only format and
IDs. Once published, a sidecar cannot change under that message ID. Existing
sidecars are preserved rather than regenerated on another replica.

Ordering uses a dependency graph: `after`, envelope `in_reply_to` and task
`approval_ids`. Dependencies must be present in the candidate graph or already
known locally. Parents precede children; UUID ordering only breaks ties between
independent ready nodes. Filenames and timestamps never establish causal order.
Concurrent independent branches can merge; contradictory task transitions fail
preflight rather than selecting a winner. Concurrent independent state updates
have no cross-replica business priority; local validated ingestion order governs
the current view, and an agent can publish a new explicit reconciliation state.

Every envelope requires one sidecar in the same committed snapshot. Imported
messages through an app/CLI are assigned a sidecar on their first publication.
Do not manually write into the canonical local log or create unpaired packets.

## Sync algorithm

`agentbridge git sync`:

1. Acquire a separate local sync lock and verify pinned configuration.
2. Inspect the configured remote branch; fetch it into the isolated bare cache.
3. Read committed Git tree/blobs directly. Validate names, modes, sizes, JSON,
   project/message IDs and sidecar pairs. No remote tree is checked out.
4. Check previously accepted remote ancestry and immutable artifacts. Removed or
   changed published packets, rewritten/deleted history and same-ID conflicts
   block sync.
5. Combine the remote snapshot and any unpublished cached snapshot. Resolve the
   graph and preflight the entire candidate batch against the normal lifecycle.
6. Ingest through `Bridge.receive()`; identical messages are idempotent.
7. Snapshot validated local events, add only missing packet pairs to a private
   index, and create/update the cached commit using Git plumbing.
8. Push the explicit transport branch only when requested/configured. A rejected
   push triggers one fetch/reconciliation retry. A second failure reports
   `BLOCKED`; cached envelopes remain available. No force push occurs.

Unrelated remote blobs outside `.agentbridge-remote/` are not interpreted and
are preserved in the base tree. Other correctly named project namespaces are
not ingested. Malformed filenames, symlinks/gitlinks/executable entries anywhere
inside the transport namespace fail closed. Own-project envelope IDs must match
their paths; other project bodies are not read. This policy avoids accidentally
mixing projects and does not silently discard an invalid own-project packet.

The code checkout, code branch, user index and existing staged changes are never
used to build transport commits. Hooks, filters, merges and checkout machinery
are not used. Private cache refs are implementation details:
`refs/heads/agentbridge-local`, `refs/agentbridge/fetched` and
`refs/agentbridge/accepted`. Commit author metadata is a neutral synthetic
identity; summaries, user names and credentials are not used in commit messages.

## Commands and API

| Operation | Network/repository effect | Application effect |
| --- | --- | --- |
| `git init` | Pin existing checkout/remote; initialize private cache | Configure transport only |
| `git status` | Inspect local cache; no network | Read local event count |
| `git fetch` | Fetch configured branch | Validate and ingest |
| `git publish` | Fetch/reconcile; create private transport commit; optional push | Validate and ingest first |
| `git sync` | Same reconciliation/publication as publish | Validate and ingest |

`--push` explicitly authorizes publication; `--no-push` explicitly suppresses it.
`git init --push-by-default` persists authorization for future sync/publish.
Default output is JSON; errors have a nonzero exit code. Fetch errors withhold
raw Git stderr to avoid logging credentials. Check the endpoint, permissions,
authentication and branch locally when Git access fails.

### Local status and known remote delivery

`git status` performs no network operation. Its counts refer to the current
local log/cache and the last accepted remote snapshot (validated fetch or a
successful explicit push):

| Field | Meaning |
| --- | --- |
| `local_event_count` | Validated canonical events in the local application log |
| `local_events_not_cached` | Local events absent from the transport cache |
| `cached_packet_count`, `cached_commit` | Packets and commit present in the private transport cache |
| `accepted_remote_commit`, `accepted_remote_packet_count` | Last accepted remote snapshot and its packet count |
| `remote_snapshot_known` | Whether an accepted branch snapshot exists locally |
| `cached_packets_pending_remote_delivery` | Cached packet IDs absent from that accepted snapshot |
| `local_events_pending_remote_delivery` | Local event IDs absent from that accepted snapshot, including uncached events |

A no-push publication can have zero uncached events while cached packets still
await remote delivery. After a successful push, those packets are accepted and
their pending count is zero. Fetch-only ingestion can produce local events
already accepted remotely but not yet in the local cache. Counts do not assert
that the current server still matches an earlier observation; use fetch/sync
to refresh it. Without an accepted snapshot, remote counts are zero and the
snapshot-known flag is false. See CHANGELOG.md for the pre-public field renames.

```python
from agent_bridge import Bridge

bridge = Bridge.open("/path/to/local-bridge-project")
bridge.configure_git("/path/to/trusted-clone")  # first configuration only
result = bridge.sync(push=False)
for message in bridge.inbox("hermes:worker"):
    if message.kind == "handoff":
        # Inspect scope, record receipt, and call an authorized runner explicitly.
        pass
# bridge.sync(push=True) only after publication is authorized.
```

The existing adapter contract formats the same envelopes for any provider.
Transport `inbox()` retains LocalTransport semantics. Existing local-only
workflows need no Git configuration and do not invoke the Git executable.

## Limits and recovery

Git subprocesses have a 30-second timeout, bounded stdout/stderr and disabled
interactive credential prompts. A project snapshot is limited to 2,048 packets
and 32 MiB of envelope/sidecar data, with 1 MiB per envelope and 4 KiB per sidecar.
Large pack transfers can consume disk before artifact checks; Git transport is
not a sandbox for arbitrary hostile servers or oversized repositories. Use a
reviewed mailbox repository and OS quotas when required.

Timeout/overflow cleanup terminates the spawned process group on POSIX or process
tree on Windows, reaps Git and joins pipe workers with bounded waits. Cleanup
has a short bounded grace period beyond the execution timeout.

A candidate validation error imports nothing from that batch. After preflight,
individual receipts/imports are atomic local events. A concurrent local change,
disk failure or process crash may leave a valid ingested prefix; retry resumes
idempotently. Local commits may precede a failed push and are retained. Readers
can continue using local mode while a remote is unavailable.

If a crash leaves `.agentbridge/.git-sync.lock`, confirm that no sync process is
using the directory before removing that specific file. Never remove a live
writer's lock. The same rule applies to the canonical `.lock`. Do not delete a
cache containing unpublished work to recover a conflict. Preserve it and resolve
the conflicting protocol action with human review; automatic history rewriting
or task arbitration is outside v0.2.

The sync lock wait is bounded to five seconds, and a stale lock is never stolen.
Initialization also uses a five-second bounded local lock beside Git's exclusion
file (`info/exclude.agentbridge.lock`). Only remove a stale lock after confirming
all relevant writers have stopped. An interrupted import can retain a valid
prefix; a rejected push retains cached work. Retry is idempotent after the
underlying problem is resolved.
