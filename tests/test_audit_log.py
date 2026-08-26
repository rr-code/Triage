"""Tests for the audit log: JSONL, one record per case.

Enough to replay any single case, and enough to PROVE a blocked action
was actually blocked: a blocked record's action_result must be null, not
just its gate.approved flag false — nothing executed is the proof.

Written before src/triage/audit_log.py exists; written to make these pass.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.triage.audit_log import read_audit_log, write_audit_log
from src.triage.declines import classify
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

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def make_outcome(
    case_id,
    action,
    *,
    approved,
    succeeded=True,
    amount_recovered=0,
    decline_code=None,
    blocking_rule=None,
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
        case_id=case_id, action=action, reason="test reason", strategy_name="test-strategy", decided_at=NOW, discount_percent=None
    )
    gate_result = GateResult(
        decision=decision,
        approved=approved,
        blocking_rule=blocking_rule if not approved else None,
        blocked_reason="blocked for test" if not approved else None,
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


class WriteAuditLogTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.path = str(Path(self._tmpdir) / "audit.jsonl")

    def test_writes_one_line_per_outcome(self):
        outcomes = [
            make_outcome("c1", Action.RETRY_SAME_INSTRUMENT, approved=True, succeeded=True, amount_recovered=50_000),
            make_outcome(
                "c2", Action.RETRY_SAME_INSTRUMENT, approved=False, blocking_rule="never_retry_hard_decline", decline_code="CARD_EXPIRED"
            ),
        ]
        write_audit_log(outcomes, self.path)
        lines = Path(self.path).read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)  # each line must be valid standalone JSON

    def test_blocked_record_has_no_action_result_and_names_the_rule(self):
        outcomes = [
            make_outcome(
                "c1", Action.RETRY_SAME_INSTRUMENT, approved=False, blocking_rule="never_retry_hard_decline", decline_code="CARD_EXPIRED"
            )
        ]
        write_audit_log(outcomes, self.path)
        record = json.loads(Path(self.path).read_text().strip())
        self.assertFalse(record["gate"]["approved"])
        self.assertEqual(record["gate"]["blocking_rule"], "never_retry_hard_decline")
        self.assertIsNone(record["action_result"])  # proof nothing executed
        self.assertFalse(record["outcome"]["recovered"])

    def test_approved_record_carries_the_action_result(self):
        outcomes = [make_outcome("c1", Action.RETRY_SAME_INSTRUMENT, approved=True, succeeded=True, amount_recovered=50_000)]
        write_audit_log(outcomes, self.path)
        record = json.loads(Path(self.path).read_text().strip())
        self.assertTrue(record["gate"]["approved"])
        self.assertIsNotNone(record["action_result"])
        self.assertTrue(record["action_result"]["succeeded"])
        self.assertEqual(record["action_result"]["amount_recovered"], 50_000)
        self.assertTrue(record["outcome"]["recovered"])

    def test_record_carries_the_decision_and_reason(self):
        outcomes = [make_outcome("c1", Action.SEND_REMINDER, approved=True, succeeded=True)]
        write_audit_log(outcomes, self.path)
        record = json.loads(Path(self.path).read_text().strip())
        self.assertEqual(record["decision"]["action"], "send_reminder")
        self.assertEqual(record["decision"]["reason"], "test reason")
        self.assertEqual(record["decision"]["strategy_name"], "test-strategy")

    def test_record_carries_enough_of_the_event_to_replay_the_case(self):
        outcomes = [make_outcome("c1", Action.RETRY_SAME_INSTRUMENT, approved=True, succeeded=True, amount_recovered=50_000)]
        write_audit_log(outcomes, self.path)
        record = json.loads(Path(self.path).read_text().strip())
        self.assertEqual(record["event"]["amount"], 50_000)
        self.assertIn("occurred_at", record["event"])
        self.assertIn("instrument_id", record["event"])


class ReadAuditLogTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.path = str(Path(self._tmpdir) / "audit.jsonl")

    def test_round_trips_case_ids(self):
        outcomes = [
            make_outcome("c1", Action.RETRY_SAME_INSTRUMENT, approved=True, succeeded=True, amount_recovered=50_000),
            make_outcome("c2", Action.WAIT, approved=True, succeeded=True),
        ]
        write_audit_log(outcomes, self.path)
        records = read_audit_log(self.path)
        self.assertEqual([r["case_id"] for r in records], ["c1", "c2"])


if __name__ == "__main__":
    unittest.main()
