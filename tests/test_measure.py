"""Tests for the measure stage."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.triage.measure import measure
from src.triage.models import (
    Action,
    ActionResult,
    Case,
    CaseCategory,
    CaseOutcome,
    Decision,
    EventStatus,
    GateResult,
    PaymentEvent,
)

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def make_outcome(case_id, action, succeeded, amount_recovered, approved=True):
    event = PaymentEvent(
        event_id=case_id,
        occurred_at=NOW,
        customer_id="cust",
        payment_id="pay",
        instrument_id="instr",
        amount=50_000,
        currency="INR",
        category=CaseCategory.FAILED_ONE_TIME,
        decline_code=None,
        status=EventStatus.OPEN,
        attempt_number=1,
        mandate_expires_at=None,
    )
    case = Case(case_id=case_id, event=event, decline=None, recovery_likelihood=0.4, rank=1, detected_at=NOW)
    decision = Decision(
        case_id=case_id, action=action, reason="r", strategy_name="s", decided_at=NOW, discount_percent=None
    )
    gate_result = GateResult(
        decision=decision,
        approved=approved,
        blocking_rule=None if approved else "x",
        blocked_reason=None if approved else "blocked",
        evaluated_at=NOW,
    )
    action_result = None
    if approved:
        action_result = ActionResult(
            case_id=case_id, action=action, succeeded=succeeded, amount_recovered=amount_recovered, detail="d", executed_at=NOW
        )
    return CaseOutcome(
        case=case,
        decision=decision,
        gate_result=gate_result,
        action_result=action_result,
        recovered=succeeded and amount_recovered > 0,
        recovered_amount=amount_recovered if succeeded else 0,
    )


class MeasureTests(unittest.TestCase):
    def test_totals_recovered_amount_across_outcomes(self):
        outcomes = [
            make_outcome("c1", Action.RETRY_SAME_INSTRUMENT, True, 50_000),
            make_outcome("c2", Action.RETRY_SAME_INSTRUMENT, False, 0),
        ]
        report = measure(outcomes, run_id="run-1", strategy_name="s", started_at=NOW, finished_at=NOW)
        self.assertEqual(report.total_recovered_amount, 50_000)
        self.assertEqual(report.total_cases, 2)

    def test_counts_only_approved_customer_visible_actions_as_contacts(self):
        outcomes = [
            make_outcome("c1", Action.SEND_REMINDER, True, 0, approved=True),
            make_outcome("c2", Action.SEND_REMINDER, True, 0, approved=False),
            make_outcome("c3", Action.RETRY_SAME_INSTRUMENT, True, 50_000, approved=True),
        ]
        report = measure(outcomes, run_id="run-1", strategy_name="s", started_at=NOW, finished_at=NOW)
        self.assertEqual(report.total_customer_contacts, 1)

    def test_report_carries_run_metadata(self):
        report = measure([], run_id="run-42", strategy_name="triage_rules", started_at=NOW, finished_at=NOW)
        self.assertEqual(report.run_id, "run-42")
        self.assertEqual(report.strategy_name, "triage_rules")
        self.assertEqual(report.total_cases, 0)


if __name__ == "__main__":
    unittest.main()
