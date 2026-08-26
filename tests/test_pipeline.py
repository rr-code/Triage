"""Tests for pipeline.run — the detect -> decide -> gate -> act -> measure wiring.

The central property under test: run() must not know or care which gate
function it was handed. A stub that approves everything and a stub that
blocks everything must both work purely because they satisfy the same
call signature (Decision, Case, CaseHistory, datetime) -> GateResult —
proving the gate is a genuine injected seam, not something pipeline.py
imports and hardcodes.

Written before src/triage/pipeline.py exists; written to make these pass.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.triage.gateway import ChargeOutcome
from src.triage.models import Action, CaseCategory, Decision, EventStatus, GateResult, PaymentEvent
from src.triage.pipeline import run

NOW = datetime.now(timezone.utc)


class FakeGateway:
    def __init__(self, succeed: bool):
        self.calls = 0
        self._succeed = succeed

    def charge(self, *, decline, hours_since_decline, is_new_instrument, amount):
        self.calls += 1
        return ChargeOutcome(succeeded=self._succeed, amount=amount, reference="ref", detail="d")


class FixedActionStrategy:
    name = "fixed"
    description = "always proposes the same action"

    def __init__(self, action, discount_percent=None):
        self._action = action
        self._discount_percent = discount_percent

    def decide(self, case):
        return Decision(
            case_id=case.case_id,
            action=self._action,
            reason="fixed",
            strategy_name=self.name,
            decided_at=datetime.now(timezone.utc),
            discount_percent=self._discount_percent,
        )


def always_approve(decision, case, history, now):
    return GateResult(decision=decision, approved=True, blocking_rule=None, blocked_reason=None, evaluated_at=now)


def always_block(decision, case, history, now):
    return GateResult(
        decision=decision, approved=False, blocking_rule="stub_block", blocked_reason="blocked for test", evaluated_at=now
    )


def make_event(
    event_id="evt-1",
    category=CaseCategory.FAILED_ONE_TIME,
    decline_code=None,
    amount=50_000,
    status=EventStatus.OPEN,
    occurred_at=None,
):
    return PaymentEvent(
        event_id=event_id,
        occurred_at=occurred_at or NOW,
        customer_id="cust-1",
        payment_id="pay-1",
        instrument_id="instr-1",
        amount=amount,
        currency="INR",
        category=category,
        decline_code=decline_code,
        status=status,
        attempt_number=1,
        mandate_expires_at=None,
    )


class GateIsInjectedTests(unittest.TestCase):
    def test_approving_gate_lets_the_action_execute(self):
        events = [make_event()]
        gateway = FakeGateway(succeed=True)
        strategy = FixedActionStrategy(Action.RETRY_SAME_INSTRUMENT)
        report, outcomes, skipped = run(events, strategy, gateway, always_approve)
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].gate_result.approved)
        self.assertIsNotNone(outcomes[0].action_result)
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(report.total_recovered_amount, 50_000)

    def test_blocking_gate_prevents_execution_entirely(self):
        events = [make_event()]
        gateway = FakeGateway(succeed=True)
        strategy = FixedActionStrategy(Action.RETRY_SAME_INSTRUMENT)
        report, outcomes, skipped = run(events, strategy, gateway, always_block)
        self.assertEqual(len(outcomes), 1)
        self.assertFalse(outcomes[0].gate_result.approved)
        self.assertIsNone(outcomes[0].action_result)
        self.assertEqual(gateway.calls, 0)
        self.assertEqual(report.total_recovered_amount, 0)

    def test_same_events_and_strategy_different_gate_different_outcome(self):
        # This is the property that matters: swapping only the gate
        # changes the result even though nothing else about the run did.
        events = [make_event()]
        strategy = FixedActionStrategy(Action.RETRY_SAME_INSTRUMENT)
        approved_report, _, _ = run(events, strategy, FakeGateway(succeed=True), always_approve)
        blocked_report, _, _ = run(events, strategy, FakeGateway(succeed=True), always_block)
        self.assertNotEqual(approved_report.total_recovered_amount, blocked_report.total_recovered_amount)


class SkippedCasesTests(unittest.TestCase):
    def test_resolved_event_is_skipped_not_decided(self):
        events = [make_event(status=EventStatus.RESOLVED)]
        strategy = FixedActionStrategy(Action.RETRY_SAME_INSTRUMENT)
        report, outcomes, skipped = run(events, strategy, FakeGateway(succeed=True), always_approve)
        self.assertEqual(outcomes, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(report.total_cases, 0)


class FailedChargeTests(unittest.TestCase):
    def test_failed_charge_is_not_counted_as_recovered(self):
        events = [make_event()]
        strategy = FixedActionStrategy(Action.RETRY_SAME_INSTRUMENT)
        report, outcomes, skipped = run(events, strategy, FakeGateway(succeed=False), always_approve)
        self.assertFalse(outcomes[0].recovered)
        self.assertEqual(report.total_recovered_amount, 0)


class ReportMetadataTests(unittest.TestCase):
    def test_report_carries_strategy_name_and_a_run_id(self):
        events = [make_event()]
        strategy = FixedActionStrategy(Action.RETRY_SAME_INSTRUMENT)
        report, _, _ = run(events, strategy, FakeGateway(succeed=True), always_approve)
        self.assertEqual(report.strategy_name, strategy.name)
        self.assertTrue(report.run_id)


if __name__ == "__main__":
    unittest.main()
