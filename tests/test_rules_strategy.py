"""Tests for RulesStrategy — Triage proper.

Organising principle under test: the decline code says whether the
INSTRUMENT can work again; the category says which LEVER is available.
One test class per branch, in the same priority order the strategy
itself is structured around:

1. Pre-failure (expiring mandate) — highest priority, fires before
   anything decline-related is even considered.
2. Hard declines — instrument is dead, switch it, never retry it.
3. Cold links — no decline signal at all, short leash to a capped offer.
4. Soft/ambiguous declines — backoff-gated retry, with the explicit rule
   that a second ambiguous failure switches lever instead of repeating.

Written before src/triage/rules_strategy.py exists; it is written to make
these pass.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.triage.config import COLD_LINK_NUDGES_BEFORE_DISCOUNT, MAX_DISCOUNT_PERCENT
from src.triage.declines import classify
from src.triage.models import Action, Case, CaseCategory, EventStatus, PaymentEvent
from src.triage.rules_strategy import RulesStrategy
from src.triage.strategy import Strategy

# RulesStrategy reads the wall clock internally (decide() takes only a
# Case, per the Strategy protocol), so backoff-relative fixtures below are
# built from the real current time rather than a fixed timestamp.
NOW = datetime.now(timezone.utc)


def make_case(
    category,
    decline_code=None,
    occurred_at=NOW,
    attempt_number=1,
    mandate_expires_at=None,
    case_id="case-1",
):
    event = PaymentEvent(
        event_id="evt-1",
        occurred_at=occurred_at,
        customer_id="cust-1",
        payment_id="pay-1",
        instrument_id="instr-1",
        amount=50_000,
        currency="INR",
        category=category,
        decline_code=decline_code,
        status=EventStatus.OPEN,
        attempt_number=attempt_number,
        mandate_expires_at=mandate_expires_at,
    )
    decline = classify(decline_code) if decline_code is not None else None
    return Case(case_id=case_id, event=event, decline=decline, recovery_likelihood=0.4, rank=1, detected_at=occurred_at)


class ProtocolConformanceTests(unittest.TestCase):
    def test_rules_strategy_satisfies_strategy_protocol(self):
        self.assertIsInstance(RulesStrategy(), Strategy)

    def test_rules_strategy_has_name_and_description(self):
        strategy = RulesStrategy()
        self.assertTrue(strategy.name)
        self.assertTrue(strategy.description)


class ExpiringMandateTests(unittest.TestCase):
    def test_expiring_mandate_requests_renewal(self):
        case = make_case(CaseCategory.EXPIRING_MANDATE, mandate_expires_at=NOW + timedelta(hours=24))
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.REQUEST_MANDATE_RENEWAL)
        self.assertTrue(decision.reason)

    def test_expiring_mandate_takes_priority_over_a_hard_decline_on_the_same_event(self):
        # Defensive priority check: rule 1 is checked before rule 2, no
        # matter what decline code happens to be attached.
        case = make_case(
            CaseCategory.EXPIRING_MANDATE, decline_code="CARD_EXPIRED", mandate_expires_at=NOW + timedelta(hours=24)
        )
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.REQUEST_MANDATE_RENEWAL)


class HardDeclineTests(unittest.TestCase):
    def test_hard_decline_switches_to_different_instrument(self):
        case = make_case(CaseCategory.FAILED_ONE_TIME, decline_code="CARD_EXPIRED")
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.RETRY_DIFFERENT_INSTRUMENT)
        self.assertTrue(decision.reason)

    def test_hard_decline_never_retries_same_instrument(self):
        for code in ("CARD_EXPIRED", "MANDATE_REVOKED", "ACCOUNT_CLOSED"):
            case = make_case(CaseCategory.FAILED_ONE_TIME, decline_code=code)
            decision = RulesStrategy().decide(case)
            self.assertNotEqual(decision.action, Action.RETRY_SAME_INSTRUMENT, code)

    def test_hard_decline_with_no_viable_instrument_switch_escalates(self):
        # STOLEN_CARD has instrument_switch_may_help=False. detect() would
        # normally drop this case before it ever reached decide(); this
        # pins the strategy's own defensive fallback if it somehow didn't.
        case = make_case(CaseCategory.FAILED_ONE_TIME, decline_code="STOLEN_CARD")
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.ESCALATE_TO_HUMAN)


class ColdLinkTests(unittest.TestCase):
    def test_cold_link_within_nudge_budget_resends_the_link(self):
        case = make_case(CaseCategory.COLD_PAYMENT_LINK, attempt_number=1)
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.SEND_PAYMENT_LINK)

    def test_cold_link_at_nudge_threshold_still_resends(self):
        case = make_case(CaseCategory.COLD_PAYMENT_LINK, attempt_number=COLD_LINK_NUDGES_BEFORE_DISCOUNT)
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.SEND_PAYMENT_LINK)

    def test_cold_link_past_nudge_threshold_escalates_to_capped_discount(self):
        case = make_case(CaseCategory.COLD_PAYMENT_LINK, attempt_number=COLD_LINK_NUDGES_BEFORE_DISCOUNT + 1)
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.OFFER_DISCOUNT)
        self.assertEqual(decision.discount_percent, MAX_DISCOUNT_PERCENT)
        self.assertTrue(decision.reason)


class SoftDeclineTests(unittest.TestCase):
    def test_before_backoff_elapsed_waits_instead_of_firing(self):
        # INSUFFICIENT_FUNDS backoff is 24h; only 1h has passed.
        case = make_case(CaseCategory.FAILED_ONE_TIME, decline_code="INSUFFICIENT_FUNDS", occurred_at=NOW - timedelta(hours=1))
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.WAIT)
        self.assertTrue(decision.reason)

    def test_after_backoff_elapsed_retries_same_instrument(self):
        case = make_case(CaseCategory.FAILED_ONE_TIME, decline_code="INSUFFICIENT_FUNDS", occurred_at=NOW - timedelta(hours=25))
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.RETRY_SAME_INSTRUMENT)


class AmbiguousDeclineTests(unittest.TestCase):
    def test_first_failure_before_backoff_waits(self):
        # DO_NOT_HONOUR backoff is 6h; only 1h has passed, first failure.
        case = make_case(
            CaseCategory.FAILED_ONE_TIME, decline_code="DO_NOT_HONOUR", occurred_at=NOW - timedelta(hours=1), attempt_number=1
        )
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.WAIT)

    def test_first_failure_after_backoff_retries_same_instrument_once(self):
        case = make_case(
            CaseCategory.FAILED_ONE_TIME, decline_code="DO_NOT_HONOUR", occurred_at=NOW - timedelta(hours=7), attempt_number=1
        )
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.RETRY_SAME_INSTRUMENT)

    def test_second_failure_never_retries_same_instrument_again(self):
        # The explicit rule: an identical second DO_NOT_HONOUR buys no new
        # information, so this must switch lever even though backoff has
        # long since elapsed.
        case = make_case(
            CaseCategory.FAILED_ONE_TIME, decline_code="DO_NOT_HONOUR", occurred_at=NOW - timedelta(hours=7), attempt_number=2
        )
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.RETRY_DIFFERENT_INSTRUMENT)
        self.assertTrue(decision.reason)

    def test_second_failure_switches_lever_even_before_backoff_would_allow_a_retry(self):
        # Proves this is a hard "never again" rule, not just another
        # backoff wait — the switch fires regardless of elapsed time.
        case = make_case(
            CaseCategory.FAILED_ONE_TIME, decline_code="DO_NOT_HONOUR", occurred_at=NOW - timedelta(hours=1), attempt_number=2
        )
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.RETRY_DIFFERENT_INSTRUMENT)


class NoDeclineInfoFallbackTests(unittest.TestCase):
    def test_failure_category_with_no_decline_code_escalates(self):
        case = make_case(CaseCategory.FAILED_ONE_TIME, decline_code=None)
        decision = RulesStrategy().decide(case)
        self.assertEqual(decision.action, Action.ESCALATE_TO_HUMAN)
        self.assertTrue(decision.reason)


class DecisionMetadataTests(unittest.TestCase):
    def test_decision_carries_the_strategy_name(self):
        strategy = RulesStrategy()
        case = make_case(CaseCategory.FAILED_ONE_TIME, decline_code="INSUFFICIENT_FUNDS", occurred_at=NOW - timedelta(hours=25))
        decision = strategy.decide(case)
        self.assertEqual(decision.strategy_name, strategy.name)

    def test_only_offer_discount_decisions_carry_a_discount(self):
        case = make_case(CaseCategory.FAILED_ONE_TIME, decline_code="INSUFFICIENT_FUNDS", occurred_at=NOW - timedelta(hours=25))
        decision = RulesStrategy().decide(case)
        self.assertIsNone(decision.discount_percent)


if __name__ == "__main__":
    unittest.main()
