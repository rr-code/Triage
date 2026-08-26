"""Tests for the gate stage: guardrails_gate and permissive_gate.

Nothing reaches act without passing through the gate. Each test below
exercises one veto condition in isolation, plus a few checks that the
gate stays out of the way when no rule applies, and that permissive_gate
approves everything guardrails_gate would have blocked.

CaseHistory is built by hand here to stand in for the pipeline's real
audit trail — exactly the point being tested: a Decision cannot carry its
own attempt count or contact history, only the content of its choice.

Written before src/triage/guardrails.py exists; it is written to make
these pass.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.triage.config import (
    MAX_CUSTOMER_CONTACTS_PER_CASE,
    MAX_DISCOUNT_PERCENT,
    MAX_TOTAL_ATTEMPTS_PER_CASE,
)
from src.triage.declines import classify
from src.triage.guardrails import CaseHistory, GuardrailRule, guardrails_gate, permissive_gate
from src.triage.models import Action, Case, CaseCategory, Decision, EventStatus, PaymentEvent

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)

EMPTY_HISTORY = CaseHistory(total_attempts=0, customer_contacts=0, last_attempt_at=None, last_contact_at=None)


def make_case(
    case_id="case-1",
    category=CaseCategory.FAILED_ONE_TIME,
    decline_code=None,
    occurred_at=NOW,
    amount=50_000,
):
    event = PaymentEvent(
        event_id="evt-1",
        occurred_at=occurred_at,
        customer_id="cust-1",
        payment_id="pay-1",
        instrument_id="instr-1",
        amount=amount,
        currency="INR",
        category=category,
        decline_code=decline_code,
        status=EventStatus.OPEN,
        attempt_number=1,
        mandate_expires_at=None,
    )
    decline = classify(decline_code) if decline_code is not None else None
    return Case(
        case_id=case_id, event=event, decline=decline, recovery_likelihood=0.4, rank=1, detected_at=occurred_at
    )


def make_decision(action, discount_percent=None, case_id="case-1"):
    return Decision(
        case_id=case_id,
        action=action,
        reason="test",
        strategy_name="test-strategy",
        decided_at=NOW,
        discount_percent=discount_percent,
    )


class NeverRetryHardDeclineTests(unittest.TestCase):
    def test_retry_same_instrument_on_hard_decline_is_blocked(self):
        case = make_case(decline_code="CARD_EXPIRED")
        decision = make_decision(Action.RETRY_SAME_INSTRUMENT)
        result = guardrails_gate(decision, case, EMPTY_HISTORY, NOW)
        self.assertFalse(result.approved)
        self.assertEqual(result.blocking_rule, GuardrailRule.NEVER_RETRY_HARD_DECLINE.value)

    def test_retry_different_instrument_on_hard_decline_is_allowed(self):
        # The instrument is dead, not the customer — switching lever must
        # still be possible, only same-instrument retry is vetoed.
        case = make_case(decline_code="CARD_EXPIRED")
        decision = make_decision(Action.RETRY_DIFFERENT_INSTRUMENT)
        result = guardrails_gate(decision, case, EMPTY_HISTORY, NOW)
        self.assertTrue(result.approved)


class DeclineBackoffTests(unittest.TestCase):
    def test_retry_before_backoff_elapsed_is_blocked(self):
        # INSUFFICIENT_FUNDS requires 24h; last attempt was 1h ago.
        case = make_case(decline_code="INSUFFICIENT_FUNDS")
        history = CaseHistory(
            total_attempts=1, customer_contacts=0, last_attempt_at=NOW - timedelta(hours=1), last_contact_at=None
        )
        decision = make_decision(Action.RETRY_SAME_INSTRUMENT)
        result = guardrails_gate(decision, case, history, NOW)
        self.assertFalse(result.approved)
        self.assertEqual(result.blocking_rule, GuardrailRule.DECLINE_BACKOFF_NOT_ELAPSED.value)

    def test_retry_after_backoff_elapsed_is_allowed(self):
        case = make_case(decline_code="INSUFFICIENT_FUNDS")
        history = CaseHistory(
            total_attempts=1, customer_contacts=0, last_attempt_at=NOW - timedelta(hours=25), last_contact_at=None
        )
        decision = make_decision(Action.RETRY_SAME_INSTRUMENT)
        result = guardrails_gate(decision, case, history, NOW)
        self.assertTrue(result.approved)


class AttemptCeilingTests(unittest.TestCase):
    def test_attempt_ceiling_exceeded_is_blocked(self):
        case = make_case(decline_code="INSUFFICIENT_FUNDS", occurred_at=NOW - timedelta(hours=100))
        history = CaseHistory(
            total_attempts=MAX_TOTAL_ATTEMPTS_PER_CASE,
            customer_contacts=0,
            last_attempt_at=NOW - timedelta(hours=48),
            last_contact_at=None,
        )
        decision = make_decision(Action.RETRY_SAME_INSTRUMENT)
        result = guardrails_gate(decision, case, history, NOW)
        self.assertFalse(result.approved)
        self.assertEqual(result.blocking_rule, GuardrailRule.ATTEMPT_CEILING_EXCEEDED.value)

    def test_write_off_is_never_blocked_by_attempt_ceiling(self):
        # Otherwise a case that hit the ceiling could never be closed out.
        case = make_case()
        history = CaseHistory(
            total_attempts=MAX_TOTAL_ATTEMPTS_PER_CASE + 10,
            customer_contacts=0,
            last_attempt_at=NOW,
            last_contact_at=None,
        )
        decision = make_decision(Action.WRITE_OFF)
        result = guardrails_gate(decision, case, history, NOW)
        self.assertTrue(result.approved)

    def test_wait_is_never_blocked_by_attempt_ceiling(self):
        # WAIT proposes nothing at all; a strategy correctly holding off
        # for backoff must not be penalized as if it were an attempt.
        case = make_case()
        history = CaseHistory(
            total_attempts=MAX_TOTAL_ATTEMPTS_PER_CASE + 10,
            customer_contacts=0,
            last_attempt_at=NOW,
            last_contact_at=None,
        )
        decision = make_decision(Action.WAIT)
        result = guardrails_gate(decision, case, history, NOW)
        self.assertTrue(result.approved)


class ContactBudgetTests(unittest.TestCase):
    def test_contact_cap_exceeded_is_blocked(self):
        case = make_case()
        history = CaseHistory(
            total_attempts=MAX_CUSTOMER_CONTACTS_PER_CASE,
            customer_contacts=MAX_CUSTOMER_CONTACTS_PER_CASE,
            last_attempt_at=NOW - timedelta(hours=48),
            last_contact_at=NOW - timedelta(hours=48),
        )
        decision = make_decision(Action.SEND_REMINDER)
        result = guardrails_gate(decision, case, history, NOW)
        self.assertFalse(result.approved)
        self.assertEqual(result.blocking_rule, GuardrailRule.CONTACT_CAP_EXCEEDED.value)

    def test_contact_cooldown_active_is_blocked(self):
        case = make_case()
        history = CaseHistory(
            total_attempts=1, customer_contacts=1, last_attempt_at=NOW - timedelta(hours=1), last_contact_at=NOW - timedelta(hours=1)
        )
        decision = make_decision(Action.SEND_REMINDER)
        result = guardrails_gate(decision, case, history, NOW)
        self.assertFalse(result.approved)
        self.assertEqual(result.blocking_rule, GuardrailRule.CONTACT_COOLDOWN_ACTIVE.value)

    def test_silent_retry_does_not_consume_contact_budget(self):
        # Contact budget is already maxed out, but a silent retry is not a
        # contact at all, so it must sail through untouched.
        case = make_case(decline_code="INSUFFICIENT_FUNDS", occurred_at=NOW - timedelta(hours=100))
        history = CaseHistory(
            total_attempts=MAX_CUSTOMER_CONTACTS_PER_CASE,
            customer_contacts=MAX_CUSTOMER_CONTACTS_PER_CASE,
            last_attempt_at=NOW - timedelta(hours=48),
            last_contact_at=NOW - timedelta(hours=1),
        )
        decision = make_decision(Action.RETRY_SAME_INSTRUMENT)
        result = guardrails_gate(decision, case, history, NOW)
        self.assertTrue(result.approved)


class DiscountTests(unittest.TestCase):
    def test_discount_above_ceiling_is_blocked(self):
        case = make_case()
        decision = make_decision(Action.OFFER_DISCOUNT, discount_percent=MAX_DISCOUNT_PERCENT + 5)
        result = guardrails_gate(decision, case, EMPTY_HISTORY, NOW)
        self.assertFalse(result.approved)
        self.assertEqual(result.blocking_rule, GuardrailRule.DISCOUNT_CEILING_EXCEEDED.value)

    def test_offer_discount_with_no_discount_attached_is_blocked(self):
        case = make_case()
        decision = make_decision(Action.OFFER_DISCOUNT, discount_percent=None)
        result = guardrails_gate(decision, case, EMPTY_HISTORY, NOW)
        self.assertFalse(result.approved)
        self.assertEqual(result.blocking_rule, GuardrailRule.OFFER_MISSING_DISCOUNT.value)

    def test_discount_within_ceiling_is_allowed(self):
        case = make_case()
        decision = make_decision(Action.OFFER_DISCOUNT, discount_percent=MAX_DISCOUNT_PERCENT)
        result = guardrails_gate(decision, case, EMPTY_HISTORY, NOW)
        self.assertTrue(result.approved)


class AuditLegibilityTests(unittest.TestCase):
    def test_blocked_result_carries_rule_name_and_human_reason(self):
        case = make_case(decline_code="CARD_EXPIRED")
        decision = make_decision(Action.RETRY_SAME_INSTRUMENT)
        result = guardrails_gate(decision, case, EMPTY_HISTORY, NOW)
        self.assertIsInstance(result.blocking_rule, str)
        self.assertTrue(result.blocking_rule)
        self.assertIsInstance(result.blocked_reason, str)
        self.assertTrue(result.blocked_reason)

    def test_approved_result_has_no_rule_or_reason(self):
        case = make_case()
        decision = make_decision(Action.SEND_REMINDER)
        result = guardrails_gate(decision, case, EMPTY_HISTORY, NOW)
        self.assertTrue(result.approved)
        self.assertIsNone(result.blocking_rule)
        self.assertIsNone(result.blocked_reason)


class PermissiveGateTests(unittest.TestCase):
    def test_permissive_gate_allows_everything_guardrails_would_block(self):
        blocked_scenarios = [
            (make_case(decline_code="CARD_EXPIRED"), make_decision(Action.RETRY_SAME_INSTRUMENT), EMPTY_HISTORY),
            (
                make_case(decline_code="INSUFFICIENT_FUNDS"),
                make_decision(Action.RETRY_SAME_INSTRUMENT),
                CaseHistory(
                    total_attempts=1, customer_contacts=0, last_attempt_at=NOW - timedelta(hours=1), last_contact_at=None
                ),
            ),
            (make_case(), make_decision(Action.OFFER_DISCOUNT, discount_percent=None), EMPTY_HISTORY),
            (make_case(), make_decision(Action.OFFER_DISCOUNT, discount_percent=MAX_DISCOUNT_PERCENT + 50), EMPTY_HISTORY),
        ]
        for case, decision, history in blocked_scenarios:
            self.assertFalse(guardrails_gate(decision, case, history, NOW).approved)
            result = permissive_gate(decision, case, history, NOW)
            self.assertTrue(result.approved)
            self.assertIsNone(result.blocking_rule)
            self.assertIsNone(result.blocked_reason)


if __name__ == "__main__":
    unittest.main()
