"""Tests for the metrics module.

The central design constraint under test: Metrics bundles the recovered
figures and the exception breakdown in the SAME frozen object, and
compute_metrics() is the only function in the module that produces one —
there is no code path that returns a recovered number without the
exceptions travelling alongside it in the same return value.

Written before src/triage/metrics.py exists; written to make these pass.
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

from src.triage.declines import classify
from src.triage.metrics import compute_metrics
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
    SkipReason,
    SkipRecord,
)

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def make_outcome(
    case_id,
    action,
    *,
    approved=True,
    succeeded=True,
    amount_recovered=0,
    decline_code=None,
):
    event = PaymentEvent(
        event_id=case_id,
        occurred_at=NOW,
        customer_id="cust",
        payment_id="pay",
        instrument_id="instr",
        amount=50_000,
        currency="INR",
        category=CaseCategory.FAILED_ONE_TIME,
        decline_code=decline_code,
        status=EventStatus.OPEN,
        attempt_number=1,
        mandate_expires_at=None,
    )
    decline = classify(decline_code) if decline_code else None
    case = Case(case_id=case_id, event=event, decline=decline, recovery_likelihood=0.4, rank=1, detected_at=NOW)
    decision = Decision(
        case_id=case_id, action=action, reason="r", strategy_name="s", decided_at=NOW, discount_percent=None
    )
    gate_result = GateResult(
        decision=decision,
        approved=approved,
        blocking_rule=None if approved else "some_rule",
        blocked_reason=None if approved else "blocked for test",
        evaluated_at=NOW,
    )
    action_result = None
    if approved:
        action_result = ActionResult(
            case_id=case_id, action=action, succeeded=succeeded, amount_recovered=amount_recovered, detail="d", executed_at=NOW
        )
    recovered = approved and succeeded and amount_recovered > 0
    return CaseOutcome(
        case=case,
        decision=decision,
        gate_result=gate_result,
        action_result=action_result,
        recovered=recovered,
        recovered_amount=amount_recovered if recovered else 0,
    )


def make_skip(case_id="evt-skip", reason=SkipReason.STALE):
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
    return SkipRecord(event=event, reason=reason, detail="test skip")


class RecoveredTests(unittest.TestCase):
    def test_recovered_case_counts_toward_recovered_amount_and_count(self):
        outcomes = [make_outcome("c1", Action.RETRY_SAME_INSTRUMENT, succeeded=True, amount_recovered=50_000)]
        metrics = compute_metrics(outcomes, [])
        self.assertEqual(metrics.recovered_count, 1)
        self.assertEqual(metrics.recovered_amount, 50_000)
        self.assertEqual(metrics.exceptions.total, 0)


class ExceptionClassificationTests(unittest.TestCase):
    def test_blocked_by_guardrail(self):
        outcomes = [make_outcome("c1", Action.RETRY_SAME_INSTRUMENT, approved=False)]
        metrics = compute_metrics(outcomes, [])
        self.assertEqual(metrics.exceptions.blocked_by_guardrail, 1)
        self.assertEqual(metrics.exceptions.total, 1)

    def test_declined_by_strategy_covers_wait_escalate_and_write_off(self):
        outcomes = [
            make_outcome("c1", Action.WAIT, succeeded=True, amount_recovered=0),
            make_outcome("c2", Action.ESCALATE_TO_HUMAN, succeeded=True, amount_recovered=0),
            make_outcome("c3", Action.WRITE_OFF, succeeded=True, amount_recovered=0),
        ]
        metrics = compute_metrics(outcomes, [])
        self.assertEqual(metrics.exceptions.declined_by_strategy, 3)

    def test_attempted_and_failed_covers_failed_retries_and_dispatch_only_actions(self):
        outcomes = [
            make_outcome("c1", Action.RETRY_SAME_INSTRUMENT, succeeded=False, amount_recovered=0),
            make_outcome("c2", Action.SEND_REMINDER, succeeded=True, amount_recovered=0),
        ]
        metrics = compute_metrics(outcomes, [])
        self.assertEqual(metrics.exceptions.attempted_and_failed, 2)

    def test_never_detected_comes_from_the_skipped_list(self):
        metrics = compute_metrics([], [make_skip("evt-1"), make_skip("evt-2")])
        self.assertEqual(metrics.exceptions.never_detected, 2)
        self.assertEqual(metrics.total_events, 2)

    def test_every_outcome_lands_in_exactly_one_bucket(self):
        outcomes = [
            make_outcome("c1", Action.RETRY_SAME_INSTRUMENT, succeeded=True, amount_recovered=50_000),
            make_outcome("c2", Action.RETRY_SAME_INSTRUMENT, approved=False),
            make_outcome("c3", Action.WAIT),
            make_outcome("c4", Action.RETRY_SAME_INSTRUMENT, succeeded=False),
        ]
        metrics = compute_metrics(outcomes, [make_skip()])
        self.assertEqual(metrics.recovered_count + metrics.exceptions.total, len(outcomes) + 1)
        self.assertEqual(metrics.total_events, len(outcomes) + 1)


class CustomerContactsTests(unittest.TestCase):
    def test_counts_only_approved_customer_visible_actions(self):
        outcomes = [
            make_outcome("c1", Action.SEND_REMINDER, approved=True, succeeded=True),
            make_outcome("c2", Action.SEND_REMINDER, approved=False),
            make_outcome("c3", Action.RETRY_SAME_INSTRUMENT, approved=True, succeeded=True, amount_recovered=1),
        ]
        metrics = compute_metrics(outcomes, [])
        self.assertEqual(metrics.customer_contacts, 1)


class DeadInstrumentRetryTests(unittest.TestCase):
    def test_counts_retry_same_instrument_proposed_against_a_hard_decline(self):
        outcomes = [make_outcome("c1", Action.RETRY_SAME_INSTRUMENT, decline_code="CARD_EXPIRED", approved=False)]
        metrics = compute_metrics(outcomes, [])
        self.assertEqual(metrics.dead_instrument_retries, 1)

    def test_counts_even_if_a_permissive_gate_let_it_through(self):
        # The waste is in proposing it at all — the counter tracks the
        # strategy's own blind spot, independent of whether a gate caught it.
        outcomes = [
            make_outcome(
                "c1", Action.RETRY_SAME_INSTRUMENT, decline_code="CARD_EXPIRED", approved=True, succeeded=False
            )
        ]
        metrics = compute_metrics(outcomes, [])
        self.assertEqual(metrics.dead_instrument_retries, 1)

    def test_does_not_count_retry_different_instrument_against_a_hard_decline(self):
        # Switching instrument is the correct, non-wasteful response.
        outcomes = [
            make_outcome(
                "c1", Action.RETRY_DIFFERENT_INSTRUMENT, decline_code="CARD_EXPIRED", approved=True, succeeded=True, amount_recovered=1
            )
        ]
        metrics = compute_metrics(outcomes, [])
        self.assertEqual(metrics.dead_instrument_retries, 0)

    def test_does_not_count_retry_same_instrument_against_a_soft_decline(self):
        outcomes = [
            make_outcome(
                "c1", Action.RETRY_SAME_INSTRUMENT, decline_code="INSUFFICIENT_FUNDS", approved=True, succeeded=False
            )
        ]
        metrics = compute_metrics(outcomes, [])
        self.assertEqual(metrics.dead_instrument_retries, 0)


class StructuralGuaranteeTests(unittest.TestCase):
    def test_metrics_is_frozen_and_carries_recovered_and_exceptions_together(self):
        metrics = compute_metrics(
            [make_outcome("c1", Action.RETRY_SAME_INSTRUMENT, succeeded=True, amount_recovered=1)], []
        )
        self.assertTrue(hasattr(metrics, "recovered_amount"))
        self.assertTrue(hasattr(metrics, "exceptions"))
        with self.assertRaises(FrozenInstanceError):
            metrics.recovered_amount = 999


if __name__ == "__main__":
    unittest.main()
