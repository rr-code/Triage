"""Tests for the comparison harness — the project's most important output.

Two layers are tested separately on purpose:

- ComparisonReport's arithmetic and summarize_comparison's honesty are
  tested against hand-built ConfigResults with known numbers, so these
  assertions don't depend on the mock gateway's randomness at all.
- run_comparison itself is tested against the real generator/pipeline for
  correct labeling, reproducibility, and the algebraic identity that must
  hold regardless of what the random draws happened to be.

Written before src/triage/compare.py exists; written to make these pass.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.triage.compare import ComparisonReport, ConfigResult, run_comparison, summarize_comparison
from src.triage.generator import generate_events
from src.triage.metrics import ExceptionBreakdown, Metrics
from src.triage.models import RunReport

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def make_config_result(label, strategy_name, gate_name, recovered_amount, customer_contacts):
    metrics = Metrics(
        total_events=10,
        recovered_count=1 if recovered_amount else 0,
        recovered_amount=recovered_amount,
        exceptions=ExceptionBreakdown(0, 0, 0, 0),
        customer_contacts=customer_contacts,
        dead_instrument_retries=0,
    )
    report = RunReport(
        run_id="r",
        strategy_name=strategy_name,
        started_at=NOW,
        finished_at=NOW,
        outcomes=(),
        total_cases=10,
        total_recovered_amount=recovered_amount,
        total_customer_contacts=customer_contacts,
    )
    return ConfigResult(label=label, strategy_name=strategy_name, gate_name=gate_name, report=report, metrics=metrics)


class RecoveryPerContactTests(unittest.TestCase):
    def test_computes_recovered_amount_divided_by_contacts(self):
        config = make_config_result("x", "rules", "guardrails", 150_000, 15)
        self.assertAlmostEqual(config.recovery_per_contact, 10_000)

    def test_is_none_when_there_were_no_contacts(self):
        config = make_config_result("x", "rules", "guardrails", 0, 0)
        self.assertIsNone(config.recovery_per_contact)


class LiftDecompositionTests(unittest.TestCase):
    def test_guardrails_lift_is_guardrailed_baseline_minus_baseline(self):
        comparison = ComparisonReport(
            baseline=make_config_result("A", "naive", "permissive", 100_000, 10),
            guardrailed_baseline=make_config_result("B", "naive", "guardrails", 120_000, 10),
            triage=make_config_result("C", "rules", "guardrails", 200_000, 10),
        )
        self.assertEqual(comparison.guardrails_lift, 20_000)
        self.assertEqual(comparison.strategy_lift, 80_000)
        self.assertEqual(comparison.total_lift, 100_000)
        self.assertEqual(comparison.total_lift, comparison.guardrails_lift + comparison.strategy_lift)

    def test_a_lift_can_be_negative_and_stays_negative(self):
        # Guardrails blocking a premature retry outright can cost more in
        # a single pass than it saves — the report must not hide that.
        comparison = ComparisonReport(
            baseline=make_config_result("A", "naive", "permissive", 150_000, 10),
            guardrailed_baseline=make_config_result("B", "naive", "guardrails", 100_000, 10),
            triage=make_config_result("C", "rules", "guardrails", 200_000, 10),
        )
        self.assertEqual(comparison.guardrails_lift, -50_000)
        text = summarize_comparison(comparison)
        self.assertIn("-50,000", text)


class ContactDirectionHonestyTests(unittest.TestCase):
    def test_summary_says_more_when_triage_contacts_more_than_baseline(self):
        comparison = ComparisonReport(
            baseline=make_config_result("naive + permissive gate", "naive", "permissive", 100_000, 10),
            guardrailed_baseline=make_config_result("naive + full guardrails", "naive", "guardrails", 90_000, 10),
            triage=make_config_result("rules + full guardrails", "rules", "guardrails", 150_000, 15),
        )
        self.assertEqual(comparison.contact_delta, 5)
        text = summarize_comparison(comparison)
        self.assertIn("more", text.lower())

    def test_summary_says_fewer_when_triage_contacts_less_than_baseline(self):
        comparison = ComparisonReport(
            baseline=make_config_result("naive + permissive gate", "naive", "permissive", 100_000, 20),
            guardrailed_baseline=make_config_result("naive + full guardrails", "naive", "guardrails", 90_000, 20),
            triage=make_config_result("rules + full guardrails", "rules", "guardrails", 150_000, 12),
        )
        self.assertEqual(comparison.contact_delta, -8)
        text = summarize_comparison(comparison)
        self.assertIn("fewer", text.lower())

    def test_summary_never_omits_the_contact_comparison(self):
        # Even when contacts tie, the summary must still say so rather
        # than silently dropping the line.
        comparison = ComparisonReport(
            baseline=make_config_result("naive + permissive gate", "naive", "permissive", 100_000, 10),
            guardrailed_baseline=make_config_result("naive + full guardrails", "naive", "guardrails", 90_000, 10),
            triage=make_config_result("rules + full guardrails", "rules", "guardrails", 150_000, 10),
        )
        self.assertEqual(comparison.contact_delta, 0)
        text = summarize_comparison(comparison)
        self.assertIn("contact", text.lower())

    def test_explains_when_all_three_configs_land_on_the_exact_same_contact_total(self):
        # This is the scenario that looks like a measurement bug (and was
        # reported as one) but is a real consequence of current strategy
        # behavior plus the single-pass history limitation -- the summary
        # must say so explicitly rather than let the coincidence look silent.
        comparison = ComparisonReport(
            baseline=make_config_result("naive + permissive gate", "naive", "permissive", 100_000, 421),
            guardrailed_baseline=make_config_result("naive + full guardrails", "naive", "guardrails", 90_000, 421),
            triage=make_config_result("rules + full guardrails", "rules", "guardrails", 150_000, 421),
        )
        text = summarize_comparison(comparison)
        self.assertIn("not a counting bug", text)
        self.assertIn("Contact caps and cooldowns", text)

    def test_does_not_explain_when_contacts_merely_tie_pairwise_not_across_all_three(self):
        # Baseline and Triage tie (contact_delta == 0), but the guardrailed
        # baseline differs -- not all three are equal, so the "exact same
        # total" caveat must not fire.
        comparison = ComparisonReport(
            baseline=make_config_result("naive + permissive gate", "naive", "permissive", 100_000, 10),
            guardrailed_baseline=make_config_result("naive + full guardrails", "naive", "guardrails", 90_000, 15),
            triage=make_config_result("rules + full guardrails", "rules", "guardrails", 150_000, 10),
        )
        self.assertEqual(comparison.contact_delta, 0)
        text = summarize_comparison(comparison)
        self.assertNotIn("not a counting bug", text)


class SummaryContentTests(unittest.TestCase):
    def test_mentions_all_three_labels_and_per_contact_figures(self):
        comparison = ComparisonReport(
            baseline=make_config_result("naive + permissive gate", "naive", "permissive", 100_000, 10),
            guardrailed_baseline=make_config_result("naive + full guardrails", "naive", "guardrails", 90_000, 10),
            triage=make_config_result("rules + full guardrails", "rules", "guardrails", 150_000, 12),
        )
        text = summarize_comparison(comparison)
        for label in ("naive + permissive gate", "naive + full guardrails", "rules + full guardrails"):
            self.assertIn(label, text)
        self.assertIn("paise/contact", text)


class RunComparisonIntegrationTests(unittest.TestCase):
    def test_produces_three_correctly_labeled_configurations(self):
        events = generate_events(200, seed=3, now=NOW)
        comparison = run_comparison(events, gateway_seed=42)
        self.assertEqual(comparison.baseline.strategy_name, "naive_retry_everything")
        self.assertEqual(comparison.baseline.gate_name, "permissive")
        self.assertEqual(comparison.guardrailed_baseline.strategy_name, "naive_retry_everything")
        self.assertEqual(comparison.guardrailed_baseline.gate_name, "guardrails")
        self.assertEqual(comparison.triage.strategy_name, "triage_rules")
        self.assertEqual(comparison.triage.gate_name, "guardrails")

    def test_same_seed_and_events_are_fully_reproducible(self):
        events = generate_events(200, seed=3, now=NOW)
        first = run_comparison(events, gateway_seed=42)
        second = run_comparison(events, gateway_seed=42)
        self.assertEqual(first.baseline.metrics.recovered_amount, second.baseline.metrics.recovered_amount)
        self.assertEqual(
            first.guardrailed_baseline.metrics.recovered_amount, second.guardrailed_baseline.metrics.recovered_amount
        )
        self.assertEqual(first.triage.metrics.recovered_amount, second.triage.metrics.recovered_amount)

    def test_total_lift_equals_sum_of_decomposed_lifts(self):
        events = generate_events(200, seed=3, now=NOW)
        comparison = run_comparison(events, gateway_seed=42)
        self.assertEqual(comparison.total_lift, comparison.guardrails_lift + comparison.strategy_lift)


if __name__ == "__main__":
    unittest.main()
