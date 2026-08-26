"""Tests for the act stage — the only module in Triage permitted side effects.

execute() maps a Decision's Action to exactly one outcome through a single
dispatch table. A FakeGateway stands in for MockRazorpayGateway so these
tests control the charge outcome directly instead of depending on
randomness.

Written before src/triage/execute.py exists; written to make these pass.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.triage.declines import classify
from src.triage.execute import execute
from src.triage.gateway import ChargeOutcome
from src.triage.models import Action, Case, CaseCategory, Decision, EventStatus, PaymentEvent

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


class FakeGateway:
    """Stands in for MockRazorpayGateway: returns a scripted outcome, records what it was called with."""

    def __init__(self, outcome: ChargeOutcome):
        self.calls: list[dict] = []
        self._outcome = outcome

    def charge(self, *, decline, hours_since_decline, is_new_instrument, amount) -> ChargeOutcome:
        self.calls.append(
            {
                "decline": decline,
                "hours_since_decline": hours_since_decline,
                "is_new_instrument": is_new_instrument,
                "amount": amount,
            }
        )
        return self._outcome


def make_case(category=CaseCategory.FAILED_ONE_TIME, decline_code="INSUFFICIENT_FUNDS", occurred_at=None):
    event = PaymentEvent(
        event_id="evt-1",
        occurred_at=occurred_at if occurred_at is not None else NOW - timedelta(hours=48),
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
    return Case(
        case_id="case-1", event=event, decline=decline, recovery_likelihood=0.4, rank=1, detected_at=event.occurred_at
    )


def make_decision(action, discount_percent=None):
    return Decision(
        case_id="case-1",
        action=action,
        reason="test",
        strategy_name="test-strategy",
        decided_at=NOW,
        discount_percent=discount_percent,
    )


class RetrySameInstrumentTests(unittest.TestCase):
    def test_calls_gateway_charge_with_is_new_instrument_false(self):
        gateway = FakeGateway(ChargeOutcome(succeeded=True, amount=50_000, reference="ref-1", detail="ok"))
        case = make_case()
        execute(make_decision(Action.RETRY_SAME_INSTRUMENT), case, gateway, NOW)
        self.assertEqual(len(gateway.calls), 1)
        self.assertFalse(gateway.calls[0]["is_new_instrument"])
        self.assertEqual(gateway.calls[0]["decline"], case.decline)

    def test_success_produces_action_result_with_amount_recovered(self):
        gateway = FakeGateway(ChargeOutcome(succeeded=True, amount=50_000, reference="ref-1", detail="ok"))
        case = make_case()
        result = execute(make_decision(Action.RETRY_SAME_INSTRUMENT), case, gateway, NOW)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.amount_recovered, 50_000)

    def test_failure_produces_action_result_with_zero_amount_recovered(self):
        gateway = FakeGateway(ChargeOutcome(succeeded=False, amount=50_000, reference="ref-1", detail="declined"))
        case = make_case()
        result = execute(make_decision(Action.RETRY_SAME_INSTRUMENT), case, gateway, NOW)
        self.assertFalse(result.succeeded)
        self.assertEqual(result.amount_recovered, 0)


class RetryDifferentInstrumentTests(unittest.TestCase):
    def test_calls_gateway_charge_with_is_new_instrument_true(self):
        gateway = FakeGateway(ChargeOutcome(succeeded=True, amount=50_000, reference="ref-1", detail="ok"))
        case = make_case(decline_code="CARD_EXPIRED")
        execute(make_decision(Action.RETRY_DIFFERENT_INSTRUMENT), case, gateway, NOW)
        self.assertTrue(gateway.calls[0]["is_new_instrument"])


class NonChargeActionsDoNotTouchTheGatewayTests(unittest.TestCase):
    def test_send_reminder_never_calls_gateway_charge(self):
        gateway = FakeGateway(ChargeOutcome(succeeded=True, amount=0, reference="unused", detail="unused"))
        case = make_case()
        result = execute(make_decision(Action.SEND_REMINDER), case, gateway, NOW)
        self.assertEqual(gateway.calls, [])
        self.assertTrue(result.succeeded)

    def test_offer_discount_detail_mentions_the_discount_percent(self):
        gateway = FakeGateway(ChargeOutcome(succeeded=True, amount=0, reference="unused", detail="unused"))
        case = make_case(category=CaseCategory.COLD_PAYMENT_LINK, decline_code=None)
        result = execute(make_decision(Action.OFFER_DISCOUNT, discount_percent=15), case, gateway, NOW)
        self.assertEqual(gateway.calls, [])
        self.assertIn("15", result.detail)

    def test_escalate_write_off_and_wait_all_succeed_without_calling_gateway(self):
        gateway = FakeGateway(ChargeOutcome(succeeded=True, amount=0, reference="unused", detail="unused"))
        case = make_case()
        for action in (Action.ESCALATE_TO_HUMAN, Action.WRITE_OFF, Action.WAIT):
            result = execute(make_decision(action), case, gateway, NOW)
            self.assertTrue(result.succeeded)
            self.assertEqual(result.amount_recovered, 0)
        self.assertEqual(gateway.calls, [])


class DispatchCompletenessTests(unittest.TestCase):
    def test_every_action_has_a_handler(self):
        gateway = FakeGateway(ChargeOutcome(succeeded=True, amount=0, reference="unused", detail="unused"))
        case = make_case()
        for action in Action:
            decision = make_decision(action, discount_percent=10 if action is Action.OFFER_DISCOUNT else None)
            result = execute(decision, case, gateway, NOW)
            self.assertEqual(result.action, action)


if __name__ == "__main__":
    unittest.main()
