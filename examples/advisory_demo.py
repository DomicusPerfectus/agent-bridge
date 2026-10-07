"""Offline two-pass advisory selection demo.

This uses a deterministic local advisor. It makes no provider or network calls and
does not create or dispatch any Agent Bridge task.
"""

import json

from agent_bridge import TwoPassSelector


class DemoAdvisor:
    def rank(self, *, task_category, candidates, top_k):
        preferred = {
            "code_change": ("codex", "hermes", "human:reviewer"),
            "fresh_research": ("hermes", "human:reviewer", "codex"),
        }.get(task_category, ())
        ordered = [item for item in preferred if item in candidates]
        ordered.extend(item for item in candidates if item not in ordered)
        return ordered[:top_k]

    def verify(self, *, task_category, candidates):
        if task_category == "unknown":
            return None
        return candidates[0] if candidates else None


def main():
    selector = TwoPassSelector(DemoAdvisor())
    result = selector.select(
        task_category="code_change",
        candidates=["codex", "hermes", "human:reviewer"],
    )
    print(json.dumps({
        "selected": result.selected,
        "shortlist": list(result.shortlist),
        "abstained": result.abstained,
        "reason": result.reason,
        "advisor_used": result.advisor_used,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
