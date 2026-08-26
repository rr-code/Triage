"""Tests for the naive baseline strategy and its conformance to Strategy.

The whole point of this baseline is that it's a fair opponent: same
categories routed sensibly, zero awareness of the decline code itself.
These tests pin down both halves — that routing works, and that nothing
about the outcome changes when only the decline code changes.

Written before src/triage/naive_strategy.py exists; it is written to make
these pass.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.triage.declines import classify
from src.triage.models import Action, Case, CaseCategory, EventStatus, PaymentEvent
from src.triage.naive_strategy import NaiveRetryEverything
from src.triage.strategy import Strategy

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def make_case(category, decline_code=None, case_id="case-1"):
    event = PaymentEvent(
        event_id="evt-1",
        occurred_at=NOW,
        customer_id="cust-1",
        payment_id="pay-1",
        instrument_id="instr-1",
        amount=50_000,
        currency="INR",
        category=category,
        decline_code=decline_code,
        status=EventStatus.OPEN,
        attempt_number=1,
        mandate_expires_at=None,
    )
    decline = classify(decline_code) if decline_code is not None else None
    return Case(case_id=case_id, event=event, decline=decline, recovery_likelihood=0.4, rank=1, detected_at=NOW)


class ProtocolConformanceTests(unittest.TestCase):
    def test_naive_strategy_satisfies_strategy_protocol(self):
        self.assertIsInstance(NaiveRetryEverything(), Strategy)

    def test_naive_strategy_has_name_and_description(self):
        strategy = NaiveRetryEverything()
        self.assertIsInstance(strategy.name, str)
        self.assertTrue(strategy.name)
        self.assertIsInstance(strategy.description, str)
        self.assertTrue(strategy.description)


class CategoryRoutingTests(unittest.TestCase):
    def test_failed_autopay_retries_same_instrument(self):
        decision = NaiveRetryEverything().decide(make_case(CaseCategory.FAILED_AUTOPAY))
        self.assertEqual(decision.action, Action.RETRY_SAME_INSTRUMENT)

    def test_failed_one_time_retries_same_instrument(self):
        decision = NaiveRetryEverything().decide(make_case(CaseCategory.FAILED_ONE_TIME))
        self.assertEqual(decision.action, Action.RETRY_SAME_INSTRUMENT)

    def test_expiring_mandate_requests_renewal(self):
        decision = NaiveRetryEverything().decide(make_case(CaseCategory.EXPIRING_MANDATE))
        self.assertEqual(decision.action, Action.REQUEST_MANDATE_RENEWAL)

    def test_cold_payment_link_resends_link(self):
        decision = NaiveRetryEverything().decide(make_case(CaseCategory.COLD_PAYMENT_LINK))
        self.assertEqual(decision.action, Action.SEND_PAYMENT_LINK)


class DeclineCodeBlindnessTests(unittest.TestCase):
    def test_decision_is_identical_regardless_of_decline_code(self):
        # The whole point of the baseline: DO_NOT_HONOUR, a soft decline,
        # a hard decline with no instrument switch, and no decline code at
        # all must produce the exact same action for the same category.
        codes = ["INSUFFICIENT_FUNDS", "DO_NOT_HONOUR", "CARD_EXPIRED", "STOLEN_CARD", None]
        strategy = NaiveRetryEverything()
        actions = {
            code: strategy.decide(make_case(CaseCategory.FAILED_ONE_TIME, decline_code=code)).action
            for code in codes
        }
        self.assertEqual(len(set(actions.values())), 1)


class NoSideEffectsTests(unittest.TestCase):
    def test_decide_does_not_mutate_the_case(self):
        case = make_case(CaseCategory.FAILED_ONE_TIME, decline_code="INSUFFICIENT_FUNDS")
        before = case
        NaiveRetryEverything().decide(case)
        self.assertEqual(case, before)

    def test_decision_strategy_name_matches_strategy(self):
        strategy = NaiveRetryEverything()
        decision = strategy.decide(make_case(CaseCategory.FAILED_ONE_TIME))
        self.assertEqual(decision.strategy_name, strategy.name)


if __name__ == "__main__":
    unittest.main()
