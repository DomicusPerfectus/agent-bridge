import unittest

from tempfile import TemporaryDirectory

from agent_bridge import Bridge, TwoPassSelector


class FakeAdvisor:
    def __init__(self, ranked=None, selected=None, *, rank_error=False, verify_error=False):
        self.ranked = ranked
        self.selected = selected
        self.rank_error = rank_error
        self.verify_error = verify_error
        self.rank_calls = 0
        self.verify_calls = 0
        self.rank_inputs = []
        self.verify_inputs = []

    def rank(self, *, task_category, candidates, top_k):
        self.rank_calls += 1
        self.rank_inputs.append((task_category, candidates, top_k))
        if self.rank_error:
            raise RuntimeError("offline rank failure")
        return self.ranked

    def verify(self, *, task_category, candidates):
        self.verify_calls += 1
        self.verify_inputs.append((task_category, candidates))
        if self.verify_error:
            raise RuntimeError("offline verify failure")
        return self.selected


class TwoPassSelectionTests(unittest.TestCase):
    def test_two_pass_selects_only_verified_shortlist_member(self):
        advisor = FakeAdvisor(["codex", "hermes", "human:reviewer"], "codex")
        result = TwoPassSelector(advisor).select(
            task_category="code_change",
            candidates=["codex", "hermes", "human:reviewer", "search"],
        )
        self.assertEqual(result.selected, "codex")
        self.assertEqual(result.shortlist, ("codex", "hermes", "human:reviewer"))
        self.assertFalse(result.abstained)
        self.assertEqual(result.reason, "verified_selection")
        self.assertEqual(advisor.rank_calls, 1)
        self.assertEqual(advisor.verify_calls, 1)

    def test_second_pass_can_reject_all(self):
        advisor = FakeAdvisor(["hermes", "codex"], None)
        result = TwoPassSelector(advisor).select(
            task_category="analysis",
            candidates=["codex", "hermes"],
        )
        self.assertIsNone(result.selected)
        self.assertTrue(result.abstained)
        self.assertEqual(result.reason, "advisor_abstained")
        self.assertEqual(advisor.rank_calls, 1)
        self.assertEqual(advisor.verify_calls, 1)

    def test_single_candidate_is_deterministic_without_advisor(self):
        advisor = FakeAdvisor([], None, rank_error=True, verify_error=True)
        result = TwoPassSelector(advisor).select(
            task_category="code_change",
            candidates=["codex"],
        )
        self.assertEqual(result.selected, "codex")
        self.assertFalse(result.advisor_used)
        self.assertEqual(result.reason, "single_candidate")
        self.assertEqual(advisor.rank_calls, 0)
        self.assertEqual(advisor.verify_calls, 0)

    def test_empty_candidates_abstain_without_advisor(self):
        advisor = FakeAdvisor([], None)
        result = TwoPassSelector(advisor).select(
            task_category="analysis",
            candidates=[],
        )
        self.assertTrue(result.abstained)
        self.assertEqual(result.reason, "no_candidates")
        self.assertEqual(advisor.rank_calls, 0)
        self.assertEqual(advisor.verify_calls, 0)

    def test_input_candidates_are_deduplicated_before_advice(self):
        advisor = FakeAdvisor(["codex", "hermes"], "hermes")
        result = TwoPassSelector(advisor).select(
            task_category="code_change",
            candidates=["codex", "codex", "hermes"],
        )
        self.assertEqual(result.selected, "hermes")
        self.assertEqual(
            advisor.rank_inputs,
            [("code_change", ("codex", "hermes"), 2)],
        )

    def test_invalid_pass1_candidate_fails_closed_without_verify(self):
        advisor = FakeAdvisor(["codex", "not-allowed"], "codex")
        result = TwoPassSelector(advisor).select(
            task_category="code_change",
            candidates=["codex", "hermes"],
        )
        self.assertTrue(result.abstained)
        self.assertEqual(result.reason, "pass1_invalid")
        self.assertEqual(advisor.rank_calls, 1)
        self.assertEqual(advisor.verify_calls, 0)

    def test_duplicate_pass1_output_fails_closed(self):
        advisor = FakeAdvisor(["codex", "codex"], "codex")
        result = TwoPassSelector(advisor).select(
            task_category="code_change",
            candidates=["codex", "hermes"],
        )
        self.assertTrue(result.abstained)
        self.assertEqual(result.reason, "pass1_invalid")
        self.assertEqual(advisor.verify_calls, 0)

    def test_invalid_pass2_selection_abstains(self):
        advisor = FakeAdvisor(["codex", "hermes"], "human:reviewer")
        result = TwoPassSelector(advisor).select(
            task_category="analysis",
            candidates=["codex", "hermes", "human:reviewer"],
            top_k=2,
        )
        self.assertTrue(result.abstained)
        self.assertEqual(result.reason, "pass2_invalid")
        self.assertEqual(result.shortlist, ("codex", "hermes"))

    def test_advisor_errors_do_not_retry(self):
        first = FakeAdvisor(rank_error=True)
        result = TwoPassSelector(first).select(
            task_category="analysis",
            candidates=["codex", "hermes"],
        )
        self.assertEqual(result.reason, "pass1_unavailable")
        self.assertEqual(first.rank_calls, 1)
        self.assertEqual(first.verify_calls, 0)

        second = FakeAdvisor(["codex", "hermes"], verify_error=True)
        result = TwoPassSelector(second).select(
            task_category="analysis",
            candidates=["codex", "hermes"],
        )
        self.assertEqual(result.reason, "pass2_unavailable")
        self.assertEqual(second.rank_calls, 1)
        self.assertEqual(second.verify_calls, 1)

    def test_task_category_is_machine_readable_not_raw_text(self):
        selector = TwoPassSelector(FakeAdvisor(["codex", "hermes"], "codex"))
        for value in ("Please fix this", "contains/slash", "", "A_uppercase"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    selector.select(task_category=value, candidates=["codex", "hermes"])

    def test_candidate_and_top_k_bounds(self):
        selector = TwoPassSelector(FakeAdvisor(["codex", "hermes"], "codex"))
        with self.assertRaises(ValueError):
            selector.select(task_category="analysis", candidates=["bad candidate", "codex"])
        with self.assertRaises(ValueError):
            selector.select(task_category="analysis", candidates=["codex", "hermes"], top_k=4)

        bounded = TwoPassSelector(
            FakeAdvisor(["codex"], "codex"),
            max_candidates=2,
        )
        with self.assertRaises(ValueError):
            bounded.select(
                task_category="analysis",
                candidates=["codex", "hermes", "human:reviewer"],
            )

    def test_selection_does_not_mutate_bridge_state(self):
        with TemporaryDirectory() as root:
            bridge = Bridge.initialize(root, "Advisory test")
            before = bridge.status()
            advisor = FakeAdvisor(["codex", "hermes"], "codex")
            result = TwoPassSelector(advisor).select(
                task_category="code_change",
                candidates=["codex", "hermes"],
            )
            self.assertEqual(result.selected, "codex")
            self.assertEqual(bridge.status(), before)


if __name__ == "__main__":
    unittest.main()
