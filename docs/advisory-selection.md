# Two-pass advisory selection

Agent Bridge can optionally use an external decision service to advise which
eligible destination or capability best fits a task. The bridge itself remains
provider-neutral: it does not call a model, open a provider session, execute a
candidate, or persist advisory input/output.

The pattern is inspired by TypeSafe Jev's published Hermes "Skill suggestion"
cookbook: a first pass ranks eligible candidates and a second pass verifies the
shortlist and may reject all of them.

## Boundary

Use `TwoPassSelector` only after the runner has already derived:

- a short machine-readable task category such as `code_change` or
  `fresh_research`;
- a bounded allowlist of eligible candidate identifiers.

Do **not** send a raw user message, files, email, commands, logs, secrets,
credentials, customer content, or arbitrary paths through this interface.

The selector returns advice only. A selected candidate is not permission to
execute, dispatch, acknowledge, approve, publish, or bypass any existing Agent
Bridge workflow rule.

## Flow

```text
local task classification
  -> local eligible-candidate allowlist
  -> pass 1: rank candidates
  -> shortlist (normally top 3)
  -> pass 2: verify shortlist or abstain
  -> SelectionResult
  -> caller applies existing approval/routing rules
```

For two or more candidates the selector invokes exactly one `rank` call and, if
pass 1 is valid, exactly one `verify` call. It never retries. Provider failure,
malformed output, an out-of-allowlist answer, or an explicit `None` becomes an
abstention.

A single eligible candidate is returned deterministically without consulting the
advisor. No candidates produces an abstention without consulting the advisor.

## Jev / BuildHub Decision Service integration

A private or product-specific runner can implement `SelectionAdvisor` using
BuildHub Decision Service and Jev:

1. Pass only the sanitized task category and server-owned candidate IDs.
2. Ask the decision service to rank the eligible set.
3. Submit only the returned top candidates for verification.
4. Map reject-all to `None`.
5. Record provider/model/audit data in the decision service, not in the Agent
   Bridge protocol log.
6. Do not let Jev advice create a task or handoff automatically.

This is particularly useful for Agent Bridge when several permitted destinations
or capabilities are valid, for example choosing among a coding agent, Hermes,
research, or a human-review path. It can also be reused for adapter/capability
selection by an authorized runner.

Agent Bridge deliberately does not contain a TypeSafe API client. That keeps the
public package dependency-free and prevents provider credentials from entering
bridge configuration or canonical envelopes.

## Example

```python
from agent_bridge import TwoPassSelector

class DecisionServiceAdvisor:
    def rank(self, *, task_category, candidates, top_k):
        # Call your authorized policy service here.
        return candidates[:top_k]

    def verify(self, *, task_category, candidates):
        # Return one candidate or None to abstain.
        return candidates[0] if candidates else None

selector = TwoPassSelector(DecisionServiceAdvisor())
advice = selector.select(
    task_category="code_change",
    candidates=["codex", "hermes", "human:reviewer"],
)

if advice.selected:
    # Existing Agent Bridge task/handoff and approval rules still apply.
    print(advice.selected)
```

## Safety properties

- provider-neutral core;
- no runtime dependency added;
- no raw task text accepted as a task category;
- candidate IDs are bounded and deduplicated;
- pass 1 cannot introduce a new candidate;
- pass 2 cannot select outside the shortlist;
- reject-all is first-class;
- no retries or paid fallback;
- no automatic execution or bridge mutation;
- no protocol/schema change.

A product integration should add its own labeled replay set and compare baseline
routing with two-pass advisory routing before enabling live Jev calls.
