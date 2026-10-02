# Changelog

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
