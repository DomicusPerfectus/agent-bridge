# Publication checklist

The 0.2.1 source is a candidate for private hosted CI. This checklist does not
authorize a push, visibility change, release, tag or package publication.

## Private validation

- [ ] Obtain successful hosted Ubuntu and Windows runs for Python 3.11 and 3.14
  (the current stable feature series; update the matrix when the supported stable
  series changes). Preserve the exact source revision and CI run links.
- [ ] Full suite, original local regressions, recovery tests, schema check,
  isolated wheel installation and the offline two-clone Git E2E all pass.
- [ ] Inspect the final diff and tracked files for credentials, private mailbox
  messages, deployment identifiers and business data; runtime directories stay ignored.
- [ ] Confirm protocol 0.1 compatibility, explicit push/approval boundaries,
  immutable events, no force push and zero Python runtime dependencies.
- [ ] Complete a final readiness review of the validated source revision.

## Before changing repository visibility to public

- [ ] Verify GitHub Private Vulnerability Reporting is enabled before changing
  visibility to public. GitHub currently provides the feature for public
  repositories, so this check cannot be claimed complete on a private candidate
  that cannot enable it. Keep the candidate private while planning the launch
  sequence; enable and verify reporting immediately at the public transition,
  before announcing or distributing the source. Record the actual verification.
- [ ] Confirm a maintainer monitors GitHub security notifications, SECURITY.md
  directs reporters to **Report a vulnerability**, and public issues are excluded
  as the vulnerability-reporting route. No invented email address is required.
- [ ] Obtain explicit authorization for the visibility change and any later
  release/tag or PyPI publication.

## Launch verification

- [ ] Verify the Security tab's **Report a vulnerability** entry is available
  and Private Vulnerability Reporting is enabled; leave launch incomplete if it
  is unavailable. Do not submit a fake vulnerability to test it.
- [ ] Verify Apache-2.0, NOTICE and packaged license/schema files are present.

[GitHub enablement documentation](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)
documents the public-repository constraint and setting.
