"""Provider-neutral two-pass advisory selection.

The selector never executes, routes, persists, or retries work. A caller supplies an
advisor implementation and decides whether to use the returned advice.
"""

from dataclasses import dataclass
import re
from typing import Protocol, Sequence


_CATEGORY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_CANDIDATE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


class SelectionAdvisor(Protocol):
    """External advisory contract.

    Implementations may call a policy/decision service, but Agent Bridge does not
    open provider sessions itself. Inputs are intentionally limited to a sanitized
    task category and an allowlisted set of candidate identifiers.
    """

    def rank(
        self, *, task_category: str, candidates: tuple[str, ...], top_k: int
    ) -> Sequence[str]: ...

    def verify(
        self, *, task_category: str, candidates: tuple[str, ...]
    ) -> str | None: ...


@dataclass(frozen=True)
class SelectionResult:
    """Normalized advisory result returned to the caller."""

    selected: str | None
    shortlist: tuple[str, ...]
    abstained: bool
    reason: str
    advisor_used: bool


class TwoPassSelector:
    """Rank candidates, then verify the shortlist.

    The second pass may abstain by returning None. Malformed or unavailable advice
    also becomes an abstention; it never triggers a retry or execution.
    """

    def __init__(
        self,
        advisor: SelectionAdvisor,
        *,
        max_candidates: int = 32,
        max_top_k: int = 3,
    ):
        if not 1 <= max_candidates <= 128:
            raise ValueError("max_candidates must be between 1 and 128")
        if not 1 <= max_top_k <= 8:
            raise ValueError("max_top_k must be between 1 and 8")
        self.advisor = advisor
        self.max_candidates = max_candidates
        self.max_top_k = max_top_k

    @staticmethod
    def _category(value: str) -> str:
        if not isinstance(value, str) or not _CATEGORY.fullmatch(value):
            raise ValueError("task_category must be a short machine-readable code")
        return value

    def _candidates(self, values: Sequence[str]) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)):
            raise ValueError("candidates must be a sequence of identifiers")
        unique: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not _CANDIDATE.fullmatch(value):
                raise ValueError("invalid candidate identifier")
            if value not in seen:
                seen.add(value)
                unique.append(value)
        if len(unique) > self.max_candidates:
            raise ValueError("too many candidates")
        return tuple(unique)

    @staticmethod
    def _abstain(
        reason: str, shortlist: tuple[str, ...] = (), *, used: bool
    ) -> SelectionResult:
        return SelectionResult(
            selected=None,
            shortlist=shortlist,
            abstained=True,
            reason=reason,
            advisor_used=used,
        )

    def select(
        self,
        *,
        task_category: str,
        candidates: Sequence[str],
        top_k: int = 3,
    ) -> SelectionResult:
        category = self._category(task_category)
        allowed = self._candidates(candidates)
        if not 1 <= top_k <= self.max_top_k:
            raise ValueError("top_k exceeds configured bound")
        if not allowed:
            return self._abstain("no_candidates", used=False)
        if len(allowed) == 1:
            return SelectionResult(
                selected=allowed[0],
                shortlist=allowed,
                abstained=False,
                reason="single_candidate",
                advisor_used=False,
            )

        effective_top_k = min(top_k, len(allowed))
        try:
            ranked = self.advisor.rank(
                task_category=category,
                candidates=allowed,
                top_k=effective_top_k,
            )
        except Exception:
            return self._abstain("pass1_unavailable", used=True)

        if isinstance(ranked, (str, bytes)) or not isinstance(ranked, Sequence):
            return self._abstain("pass1_invalid", used=True)
        try:
            ranked_values = tuple(ranked)
        except Exception:
            return self._abstain("pass1_invalid", used=True)
        if not ranked_values or len(ranked_values) > effective_top_k:
            return self._abstain("pass1_invalid", used=True)
        if any(not isinstance(item, str) for item in ranked_values):
            return self._abstain("pass1_invalid", used=True)
        if (
            len(set(ranked_values)) != len(ranked_values)
            or any(item not in allowed for item in ranked_values)
        ):
            return self._abstain("pass1_invalid", used=True)

        shortlist = ranked_values
        try:
            selected = self.advisor.verify(
                task_category=category,
                candidates=shortlist,
            )
        except Exception:
            return self._abstain("pass2_unavailable", shortlist, used=True)

        if selected is None:
            return self._abstain("advisor_abstained", shortlist, used=True)
        if not isinstance(selected, str) or selected not in shortlist:
            return self._abstain("pass2_invalid", shortlist, used=True)
        return SelectionResult(
            selected=selected,
            shortlist=shortlist,
            abstained=False,
            reason="verified_selection",
            advisor_used=True,
        )
