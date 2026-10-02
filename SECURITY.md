# Security policy

## Trust boundary

v0.1 is a local coordination tool for cooperating agents and users with access
to a trusted project directory. It performs no network calls, has no telemetry,
does not execute commands from payloads, and does not read project files or
conversation histories automatically. CLI state/context/import files are read
only when explicitly supplied. Artifact references are inert strings.

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
repair, discard or reorder events.

## Reporting vulnerabilities

Before publishing, the maintainer should enable the hosting provider's private
vulnerability reporting and document its contact route here. No public remote
or maintainer contact has been selected yet. Do not put exploit details, tokens
or private logs into a public issue. Share a minimal synthetic reproduction with
the maintainer through an established private channel when available.
