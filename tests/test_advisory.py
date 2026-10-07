import unittest
from collections.abc import Sequence

from tempfile import TemporaryDirectory

from agent_bridge import Bridge, TwoPassSelector


class ExplodingSequence(list):
    def __iter__(self):
        raise RuntimeError("offline sequence materialization failure")


class UnhashableString(str):
    __hash__ = None


class ExplodingEqualString(str):
    __hash__ = str.__hash__

    def __eq__(self, other):
        raise RuntimeError("offline string equality failure")


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


class BrokenTypeInspection:
    @property
    def __class__(self):
        raise RuntimeError("malformed type inspection")


class BrokenSequence(Sequence):
    def __len__(self):
        return 2

    def __getitem__(self, index):
        raise RuntimeError("malformed sequence")

    def __iter__(self):
        raise RuntimeError("malformed iteration")


class OversizedSequence(Sequence):
    def __init__(self):
        self.reads = 0

    def __len__(self):
        return 10**9

    def __getitem__(self, index):
        self.reads += 1
        return "codex"


class BrokenHash(str):
    def __hash__(self):
        raise TypeError("malformed hash")


class BrokenEquality(str):
    def __eq__(self, other):
        raise RuntimeError("malformed equality")


class SpoofedEquality(str):
    __hash__ = str.__hash__

    def __eq__(self, other):
        return True

    def __str__(self):
        return "codex"


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

    def test_unhashable_pass1_output_fails_closed_before_dedup(self):
        malformed_values = (
            {"candidate": "codex"},
            ["codex"],
        )
        for malformed in malformed_values:
            with self.subTest(malformed=malformed):
                advisor = FakeAdvisor(["codex", malformed], "codex")
                result = TwoPassSelector(advisor).select(
                    task_category="code_change",
                    candidates=["codex", "hermes"],
                )
                self.assertIsNone(result.selected)
                self.assertTrue(result.abstained)
                self.assertEqual(result.reason, "pass1_invalid")
                self.assertEqual(advisor.rank_calls, 1)
                self.assertEqual(advisor.verify_calls, 0)

    def test_pass1_materialization_failure_fails_closed(self):
        advisor = FakeAdvisor(ExplodingSequence(["codex", "hermes"]), "codex")
        result = TwoPassSelector(advisor).select(
            task_category="code_change",
            candidates=["codex", "hermes"],
        )
        self.assertIsNone(result.selected)
        self.assertTrue(result.abstained)
        self.assertEqual(result.reason, "pass1_invalid")
        self.assertEqual(advisor.rank_calls, 1)
        self.assertEqual(advisor.verify_calls, 0)

    def test_pass1_rejects_string_subclasses_before_hash_or_equality(self):
        malformed_values = (
            UnhashableString("codex"),
            ExplodingEqualString("codex"),
        )
        for malformed in malformed_values:
            with self.subTest(type=type(malformed).__name__):
                advisor = FakeAdvisor([malformed, "hermes"], "hermes")
                result = TwoPassSelector(advisor).select(
                    task_category="code_change",
                    candidates=["codex", "hermes"],
                )
                self.assertIsNone(result.selected)
                self.assertTrue(result.abstained)
                self.assertEqual(result.reason, "pass1_invalid")
                self.assertEqual(advisor.rank_calls, 1)
                self.assertEqual(advisor.verify_calls, 0)

    def test_pass2_rejects_string_subclass_without_custom_equality(self):
        advisor = FakeAdvisor(
            ["codex", "hermes"],
            ExplodingEqualString("codex"),
        )
        result = TwoPassSelector(advisor).select(
            task_category="code_change",
            candidates=["codex", "hermes"],
        )
        self.assertIsNone(result.selected)
        self.assertTrue(result.abstained)
        self.assertEqual(result.reason, "pass2_invalid")
        self.assertEqual(advisor.rank_calls, 1)
        self.assertEqual(advisor.verify_calls, 1)

    def test_local_inputs_reject_string_subclasses_cleanly(self):
        selector = TwoPassSelector(FakeAdvisor(["codex", "hermes"], "codex"))
        with self.assertRaises(ValueError):
            selector.select(
                task_category=UnhashableString("code_change"),
                candidates=["codex", "hermes"],
            )
        with self.assertRaises(ValueError):
            selector.select(
                task_category="code_change",
                candidates=[UnhashableString("codex"), "hermes"],
            )

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

    def assert_invalid_pass1(self, ranked):
        advisor = FakeAdvisor(ranked, "codex")
        result = TwoPassSelector(advisor).select(
            task_category="code_change",
            candidates=["codex", "hermes"],
        )
        self.assertTrue(result.abstained)
        self.assertIsNone(result.selected)
        self.assertEqual(result.shortlist, ())
        self.assertEqual(result.reason, "pass1_invalid")
        self.assertEqual(advisor.rank_calls, 1)
        self.assertEqual(advisor.verify_calls, 0)

    def test_unhashable_dict_pass1_fails_closed(self):
        self.assert_invalid_pass1([{"bad": "object"}, "codex"])

    def test_unhashable_list_pass1_fails_closed(self):
        self.assert_invalid_pass1([["bad", "array"], "codex"])

    def test_other_malformed_pass1_values_fail_closed(self):
        for ranked in ([42, "codex"], [None, "codex"], [{}, []]):
            with self.subTest(ranked=ranked):
                self.assert_invalid_pass1(ranked)

    def test_invalid_pass1_shape_and_bounds_fail_closed(self):
        for ranked in ("codex", b"codex", None, [], ["codex", "hermes", "codex"]):
            with self.subTest(ranked=ranked):
                self.assert_invalid_pass1(ranked)

    def test_broken_external_type_inspection_fails_closed(self):
        self.assert_invalid_pass1(BrokenTypeInspection())

    def test_broken_external_sequence_fails_closed(self):
        self.assert_invalid_pass1(BrokenSequence())

    def test_oversized_external_sequence_reads_only_overflow_bound(self):
        ranked = OversizedSequence()
        self.assert_invalid_pass1(ranked)
        self.assertEqual(ranked.reads, 3)

    def test_external_string_hooks_rejected_without_invocation(self):
        self.assert_invalid_pass1([BrokenHash("codex"), BrokenEquality("hermes")])

    def test_pass1_string_subclass_cannot_spoof_allowlist(self):
        self.assert_invalid_pass1([SpoofedEquality("not-allowed"), "codex"])

    def test_pass2_string_subclass_cannot_spoof_shortlist(self):
        advisor = FakeAdvisor(["codex"], SpoofedEquality("hermes"))
        result = TwoPassSelector(advisor).select(
            task_category="analysis", candidates=["codex", "hermes"], top_k=1
        )
        self.assertTrue(result.abstained)
        self.assertEqual(result.reason, "pass2_invalid")
        self.assertEqual(advisor.verify_calls, 1)

    def test_local_string_subclasses_rejected_before_advice(self):
        advisor = FakeAdvisor(["codex", "hermes"], "codex")
        selector = TwoPassSelector(advisor)
        with self.assertRaises(ValueError):
            selector.select(
                task_category=BrokenHash("analysis"), candidates=["codex", "hermes"]
            )
        with self.assertRaises(ValueError):
            selector.select(
                task_category="analysis",
                candidates=[BrokenHash("codex"), BrokenEquality("hermes")],
            )
        self.assertEqual(advisor.rank_calls, 0)
        self.assertEqual(advisor.verify_calls, 0)

    def test_pass2_malformed_values_and_equality_fail_closed(self):
        for selected in ({}, [], 42, SpoofedEquality("not-allowed")):
            with self.subTest(selected=selected):
                advisor = FakeAdvisor(["codex", "hermes"], selected)
                result = TwoPassSelector(advisor).select(
                    task_category="analysis",
                    candidates=["codex", "hermes"],
                )
                self.assertTrue(result.abstained)
                self.assertEqual(result.reason, "pass2_invalid")
                self.assertEqual(result.shortlist, ("codex", "hermes"))
                self.assertEqual(advisor.rank_calls, 1)
                self.assertEqual(advisor.verify_calls, 1)

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
