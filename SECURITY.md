# Security policy

## Trust boundary

Local mode is a coordination tool for cooperating agents and users with access
to a trusted project directory. Local mode performs no network calls, has no telemetry,
does not execute commands from payloads, and does not read project files or
conversation histories automatically. CLI state/context/import files are read
only when explicitly supplied. Artifact references are inert strings.

v0.2 optionally exchanges packets through Git. Configured fetch/sync invokes
Git; explicit or configured opt-in permits push. See the Git threat model below.

Agent names and `human:<name>` are claimed identifiers. The bridge checks that
a message follows its recorded routing and workflow, but does **not authenticate
who wrote it**. Anyone with filesystem/API access can claim a human identifier
or create an ungated task. Approval gates prevent accidental workflow bypass;
they are not a security boundary against a malicious local writer. A future
connected integration must authenticate principals, preserve user consent and
obtain human approval through a trusted UI.

Stored payloads and Markdown exports are untrusted input. Consuming agents must
apply their own policies and review embedded instructions, links and artifacts.
Backtick-aware fences keep JSON within its Markdown data block, but do not make
payload content trustworthy. The bridge never automatically acts on it.

## Keeping data private

- `.agentbridge/`, `.validation/`, environments, caches and `.env*` files are
  ignored by the supplied `.gitignore`. Review staged files before committing.
- In external worktrees, `init` checks for tracked runtime files, verifies an
  effective ignore rule and, if needed, appends a scoped rule to Git's resolved
  local `info/exclude`. Existing bytes are preserved; linked worktrees can share
  this file. Tracked `.gitignore`, configuration, index and branches are unchanged.
  With working Git, failed protection stops initialization with a warning/error.
  Without Git, local mode warns when a worktree marker is present. Rerun `init`
  after moving into a repository or changing ignore rules. Force-add bypasses
  ignore rules; do not force-add runtime data.
- Do not put credentials, tokens, raw private chat logs or unnecessary project
  data into messages. The bridge does not redact secrets or encrypt the log.
- Prefer small structured summaries. Default handoff context includes the latest
  recorded state and current decisions; use `--context FILE` to minimize sharing.
- Exporting a packet shares its contents. Review it before passing it to a
  provider, Git remote or another person. Git ignore is not a data-loss control
  for manually exported or explicitly force-added files.
- Protect the project directory with operating-system access controls. Files
  are private by default where supported by the OS; review Windows ACLs when
  sharing the project. Do not place active bridge data on untrusted network shares.

Examples and tests use synthetic data. No credentials or private provider data
are necessary for installation, the demo, or the runtime CLI.

## Storage safeguards and recovery

The runtime rejects unknown fields, unsupported protocol versions, duplicate
JSON keys, invalid identifiers, non-finite numbers, oversized/deep messages,
cross-project envelopes and invalid workflow transitions. It rejects symlinks
and Windows junctions in storage paths. These checks assume a trusted local
filesystem; hostile races replacing directory entries are outside v0.1 scope.

An exclusive lock serializes readers/writers. File fsync and atomic replacement
avoid publishing partial messages. Directory fsync, tamper signatures,
encryption, automatic backup and distributed coordination are not implemented.
Do not edit published event files in place or copy new files directly into the
log. Import canonical messages through the validated interface.

If a crash leaves `.agentbridge/.lock`, the bridge times out rather than taking
over automatically. Stop all bridge writers, inspect the PID in the lock, and
confirm that no live process is using this directory. Only then remove that
specific lock file. A PID can be reused, so checking existence alone is
insufficient. Do not remove a live writer's lock.

An unpublished `.pending-*` file is ignored and may be removed after confirming
no writer is active. For a corrupt published event or missing sequence number,
preserve the log and restore a known-good copy; the bridge does not silently
repair, discard or reorder canonical local events.

## Git transport threat model

The trusted components are the local OS/filesystem, installed Git executable,
user/system Git authentication configuration, SSH/credential agents, and the
authorized integration/runner. Treat remote Git trees, messages and claimed
identities as untrusted. Git login authenticates access to a repository; it does
**not** authenticate the `source` or `human:<name>` written inside an envelope.
There are no message signatures, per-agent ACLs or cryptographic attribution.

| Threat | v0.2 behavior and remaining responsibility |
| --- | --- |
| Malicious remote content | Read committed blobs directly; never checkout, execute, import code, use filters or run client repository hooks. Apply strict schema, dependency and lifecycle checks before ingestion. |
| Forged identities or human approvals | Routing checks validate consistency only. Restrict mailbox write access and have the integrating app authenticate human input. A malicious authorized writer can impersonate an identifier. |
| Compromised credentials | No tokens/SSH keys/PATs in envelopes or bridge config. Use existing Git authentication, least repository privilege and rotation outside Agent Bridge. A compromised writer can inject valid-looking work. |
| Message tampering | Envelopes/sidecars are immutable by ID; changed or removed accepted packets and rewritten/deleted accepted history fail closed. The first observed snapshot has no independent provenance guarantee. |
| Duplicate/replay attacks | Identical replay adds no event; conflicting content under an ID is rejected. Receipts are idempotent. Runners must not execute an unacknowledged delivery twice. |
| Path traversal and malicious filenames | Fixed UUID namespace, ASCII paths and exact filename matching; IDs never become arbitrary paths or command flags. Unsafe transport entries fail closed. No remote path is written into the user checkout. |
| Oversized payloads | Check Git tree blob sizes before reading content; 1 MiB envelope, 4 KiB sidecar, 2,048 packets and 32 MiB per project, with bounded process output/time. Fetch packs can still consume disk; use a trusted small mailbox and OS quotas. |
| Repository substitution | Pin normalized repository path, Git directory, remote URL, project ID and object format. Recheck on operation; reject binding changes and non-descendant accepted history. This is trust on first use, not server identity pinning. |
| Poisoned Git configuration | Remote `.gitconfig` is inert. Fetch/push use an isolated bare cache, not clone-local helpers or URL rewrites. Cache config keys are allowlisted; inherited Git repository/index/config override environment variables are removed. Client hooks, fsmonitor, signing and auto maintenance are disabled. |
| Symlinks | Reject transport symlink/gitlink/executable modes and local symlinks/junctions at storage/cache/repository boundaries. Hostile local filesystem races or deep object-store mutation are outside the cooperating-local-writer model. |
| Concurrent writers | Separate bounded sync lock and canonical store lock; Git ref updates are atomic. Normal push only; one fetch/reconcile retry after rejection. Conflicting task branches or repeated failure block, preserving cached work. |
| Secrets and private context publication | Push requires explicit/configured consent and exports all project envelopes. The bridge does not redact/classify data. Use a separate shareable bridge directory, review summaries/context and verify the selected mailbox before publishing. |

Global/system Git settings and explicitly supplied SSH/askpass mechanisms remain
trusted executable authentication policy. Malicious global credential helpers,
SSH commands or URL rewrites can execute code or redirect connections; review
the machine configuration outside the bridge. HTTPS redirects are disabled.
The selected local remote's filesystem/server configuration must also be trusted;
Git transport is not a sandbox for an arbitrary malicious Git server or local
repository RPC implementation. No remote-helper protocol is accepted.

Each Git subprocess has a 30-second timeout, bounded stdout/stderr and disabled
interactive credential prompts. Raw Git stderr is withheld from bridge errors
to avoid credential leaks. Remote branch writes can invoke the server's own
authorized hooks; no client-side bridge message controls those policies.
Timeout/output-limit cleanup terminates the spawned process group on POSIX, or
the spawned process tree using Windows taskkill, then reaps Git and joins pipe
workers with bounded cleanup waits. It never targets unrelated processes.
Caller interruption uses the same cleanup and reaps Git before propagating the interrupt.

The code checkout, active branch and user index are never used for transport
commits. Only deterministic packet pairs are added to a private index. Existing
unrelated remote files are preserved, not interpreted or staged from local code.
Local `.agentbridge/` data, config, cache and locks remain ignored by Git.

Whole-batch preflight prevents malformed candidate batches from importing a
prefix. A crash/disk error or concurrent local mutation after preflight can leave
a valid prefix; retry resumes idempotently. A failed push can leave a local
transport commit containing unpublished envelopes. Preserve that cache when
investigating a conflict. Do not force-push, discard packets or delete the cache
to hide a conflict. Confirm no sync is active before removing a stale
`.agentbridge/.git-sync.lock`, using the same care as for the canonical lock.
Sync waits up to five seconds for that lock and never removes a lock it did not
acquire. Initialization's local exclusion lock has the same five-second bound.
Its path is Git's resolved `info/exclude.agentbridge.lock`. Confirm all relevant
writers have stopped before manually removing a stale lock; PID reuse means
existence alone does not prove ownership or safety. Retry preserves valid events
and unpublished cached packets; it never rewrites history to recover.

## Reporting vulnerabilities

Security vulnerabilities must not be filed as public issues. GitHub Private
Vulnerability Reporting is the primary reporting route at public launch. The
public repository will have it enabled before/at launch, and maintainers monitor
GitHub security notifications. Reports should use the repository's Security tab
and GitHub's **Report a vulnerability** flow, with a minimal reproduction and no
unnecessary tokens or private logs. See [GitHub's reporting instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately).

Until the project is public, no external vulnerability-reporting route is
advertised because the source is not distributed publicly. No email address is
provided. Private collaborators use their existing private coordination channel.

GitHub currently exposes this feature for public repositories. The
[publication checklist](docs/publication-checklist.md) requires verification of
reporting enablement before a visibility change; if the private candidate cannot
enable it yet, that gate remains open and the launch sequence must enable and
verify it immediately at the public transition before announcements or distribution.
Do not claim an unverified reporting route is active.
