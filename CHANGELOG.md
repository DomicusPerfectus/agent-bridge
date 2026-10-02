# Changelog

## 0.2.1 — unreleased candidate

- Protect runtime data during initialization and Git configuration using Git's
  resolved local exclusion file. Preserve existing contents, support linked and
  nested worktrees, reject tracked/overridden runtime protection and warn when
  Git is unavailable. No tracked ignore/config/index/branch edits or opt-out.
- Fix Windows command examples and adopt GitHub Private Vulnerability Reporting
  as the public-launch policy, with a publication checklist and platform constraint.
- Prepare Windows/Ubuntu CI for Python 3.11 and 3.14, verbose recovery tests,
  installed-wheel verification and the full offline Git demo. Ubuntu PASS remains
  pending a real hosted run.
- Reap timed-out/interrupted Git process trees/groups and pipe workers with bounded cleanup;
  test real push rejection/reconciliation, blocked retry, preserved cached work,
  interruption replay and manual stale-lock recovery.
- Pre-public API refinement: Git status `unpublished_local_messages` becomes
  `local_events_not_cached`; `local_message_count` becomes `local_event_count`.
  `cached_message_count` becomes `cached_packet_count` in status and sync results.
  Add accepted-remote counts, snapshot-known state and separate cached/local
  pending-delivery counts. Status remains local and uses the last accepted snapshot.
- Canonical protocol/envelopes remain version 0.1; Python runtime dependencies remain empty.

## 0.2.0 — unreleased

- Add Git/GitHub packet transport using an existing checkout/remote and isolated
  bare cache; local filesystem mode remains the default until configured.
- Add `git init`, `git status`, `git fetch`, `git publish` and `git sync`, with
  explicit push policy and machine-readable output.
- Preserve protocol 0.1 envelopes; dependency sidecars preserve causal order,
  receipts, human approval resolution and successive state events.
- Add whole-batch preflight, immutable artifact/conflict checks, pinned repository
  binding, bounded Git subprocesses and one push-race reconciliation retry.
- Add explicit project-ID joining, generic `Bridge.sync()` API, Codex inbox
  helper, sanitized integration examples and offline two-clone E2E demo/tests.
- Add Apache License 2.0 and a neutral contributor notice.
- Document security, trust boundaries and public/private separation.

## 0.1.0 — local foundation

- Provider-neutral canonical JSON, local append-only event storage, CLI/API,
  reference adapters, approval gates and task lifecycle.
- 34 unit/integration tests and offline local handoff demo.

No release/tag or package publication has been created by these changes.
