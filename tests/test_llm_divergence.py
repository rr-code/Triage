"""Tests for the divergence harness: how often LLMStrategy's final decision
differs from what RulesStrategy would have chosen for the same case, and
whether those divergences recovered more or less money.

A rejected proposal that fell back to the rules decision is NOT a
divergence (the action ends up identical to rules' by construction) — the
whole point of this suite is pinning down that rejected != divergent.

A DeterministicGateway (not the real seeded mock) is injected so every
assertion here is hand-verifiable arithmetic, not a statistical claim.

Written before src/triage/llm_divergence.py exists; written to make these pass.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.triage.declines import DeclineClass
from src.triage.gateway import ChargeOutcome
from src.triage.llm_divergence import DivergenceReport, analyze_divergence, summarize_divergence
from src.triage.llm_strategy import LLMStrategy, ModelProposal
from src.triage.models import CaseCategory, EventStatus, PaymentEvent
from src.triage.rules_strategy import RulesStrategy

NOW = datetime.now(timezone.utc)


class DeterministicGateway:
    """Always succeeds, except a same-instrument charge against a hard decline — matching
    the one guarantee the real mock makes structurally, without any randomness."""

    def charge(self, *, decline, hours_since_decline, is_new_instrument, amount):
        if not is_new_instrument and decline is not None and decline.decline_class is DeclineClass.HARD:
            return ChargeOutcome(succeeded=False, amount=amount, reference="det", detail="hard decline")
        return ChargeOutcome(succeeded=True, amount=amount, reference="det", detail="ok")


class ScriptedLLMClient:
    def __init__(self, proposals: dict[str, ModelProposal]):
        self._proposals = proposals

    def propose(self, case):
        return self._proposals[case.case_id]


def make_event(event_id, decline_code, occurred_at=None, amount=50_000):
    return PaymentEvent(
        event_id=event_id,
        occurred_at=occurred_at if occurred_at is not None else NOW - timedelta(hours=48),
        customer_id="cust",
        payment_id="pay",
        instrument_id="instr",
        amount=amount,
        currency="INR",
        category=CaseCategory.FAILED_AUTOPAY,  # 72h recovery window — enough room for a 24h-old event to still be live
        decline_code=decline_code,
        status=EventStatus.OPEN,
        attempt_number=1,
        mandate_expires_at=None,
    )


class DivergenceAccountingTests(unittest.TestCase):
    def setUp(self):
        # evt-a: HARD decline. rules -> retry_different_instrument (recovers).
        #        llm proposes escalate_to_human (valid, no taxonomy issue) -> a genuine divergence, recovers nothing.
        # evt-b: SOFT decline, backoff long elapsed. rules -> retry_same_instrument.
        #        llm proposes the identical action -> agreement, not a divergence.
        # evt-c: HARD decline again. llm proposes retry_same_instrument (a taxonomy violation)
        #        -> rejected, falls back to rules' retry_different_instrument -> matches rules, not a divergence,
        #        but IS a rejection.
        self.events = [
            make_event("evt-a", "CARD_EXPIRED"),
            make_event("evt-b", "INSUFFICIENT_FUNDS"),
            make_event("evt-c", "CARD_EXPIRED"),
        ]
        proposals = {
            "evt-a": ModelProposal(action_name="escalate_to_human", reasoning="risky case", discount_percent=None),
            "evt-b": ModelProposal(action_name="retry_same_instrument", reasoning="agree with rules", discount_percent=None),
            "evt-c": ModelProposal(action_name="retry_same_instrument", reasoning="worth trying", discount_percent=None),
        }
        client = ScriptedLLMClient(proposals)
        self.llm_strategy = LLMStrategy(client=client, fallback=RulesStrategy())

    def test_counts_cases_rejections_and_divergences_correctly(self):
        report = analyze_divergence(self.events, self.llm_strategy, make_gateway=DeterministicGateway)
        self.assertEqual(report.total_cases, 3)
        self.assertEqual(report.rejected_proposals, 1)  # evt-c
        self.assertEqual(report.divergent_cases, 1)  # evt-a only

    def test_totals_recovered_amount_for_each_strategy(self):
        report = analyze_divergence(self.events, self.llm_strategy, make_gateway=DeterministicGateway)
        # rules: 50k (evt-a, different instrument) + 50k (evt-b) + 50k (evt-c, different instrument) = 150k
        self.assertEqual(report.rules_total_recovered_amount, 150_000)
        # llm: 0 (evt-a, escalated, no charge) + 50k (evt-b) + 50k (evt-c, rejected -> same as rules) = 100k
        self.assertEqual(report.llm_total_recovered_amount, 100_000)

    def test_divergent_subset_shows_the_llm_did_worse_here(self):
        report = analyze_divergence(self.events, self.llm_strategy, make_gateway=DeterministicGateway)
        self.assertEqual(report.divergent_llm_recovered_amount, 0)
        self.assertEqual(report.divergent_rules_recovered_amount, 50_000)
        self.assertEqual(report.divergence_recovered_delta, -50_000)

    def test_divergence_rate(self):
        report = analyze_divergence(self.events, self.llm_strategy, make_gateway=DeterministicGateway)
        self.assertAlmostEqual(report.divergence_rate, 1 / 3)


class SummaryHonestyTests(unittest.TestCase):
    def test_reports_when_divergences_recovered_less(self):
        report = DivergenceReport(
            total_cases=10,
            rejected_proposals=1,
            divergent_cases=3,
            llm_total_recovered_amount=500_000,
            rules_total_recovered_amount=600_000,
            divergent_llm_recovered_amount=50_000,
            divergent_rules_recovered_amount=150_000,
        )
        text = summarize_divergence(report)
        self.assertIn("less", text.lower())
        self.assertIn("-100,000", text)

    def test_reports_when_divergences_recovered_more(self):
        report = DivergenceReport(
            total_cases=10,
            rejected_proposals=1,
            divergent_cases=3,
            llm_total_recovered_amount=700_000,
            rules_total_recovered_amount=600_000,
            divergent_llm_recovered_amount=250_000,
            divergent_rules_recovered_amount=150_000,
        )
        text = summarize_divergence(report)
        self.assertIn("more", text.lower())
        self.assertIn("+100,000", text)


if __name__ == "__main__":
    unittest.main()
