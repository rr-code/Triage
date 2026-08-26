"""The audit log: JSONL, one record per case.

Enough to replay any single case — the decision, the case's decline
classification, and enough of the underlying event to recompute timing —
and enough to PROVE a blocked action was actually blocked: a blocked
record's action_result is always null, because execute() was genuinely
never called for it. That's not asserted here; it's inherited from
CaseOutcome's own invariant (action_result is None exactly when the gate
didn't approve), and this module only ever serializes what the pipeline
already produced. It never reconstructs or infers a verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import ActionResult, CaseOutcome


def write_audit_log(outcomes: list[CaseOutcome], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for outcome in outcomes:
            f.write(json.dumps(to_audit_record(outcome)) + "\n")


def read_audit_log(path: str) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines()]


def to_audit_record(outcome: CaseOutcome) -> dict:
    """The canonical per-case audit record — public so other modules (the
    dashboard included) render exactly what the JSONL log contains,
    instead of re-deriving a slightly different view of the same case."""
    case = outcome.case
    decision = outcome.decision
    gate_result = outcome.gate_result

    return {
        "case_id": case.case_id,
        "category": case.event.category.value,
        "decline_code": case.decline.code if case.decline is not None else None,
        "decline_class": case.decline.decline_class.value if case.decline is not None else None,
        "recovery_likelihood": case.recovery_likelihood,
        "rank": case.rank,
        "event": {
            "event_id": case.event.event_id,
            "occurred_at": case.event.occurred_at.isoformat(),
            "amount": case.event.amount,
            "instrument_id": case.event.instrument_id,
            "attempt_number": case.event.attempt_number,
        },
        "decision": {
            "action": decision.action.value,
            "reason": decision.reason,
            "strategy_name": decision.strategy_name,
            "decided_at": decision.decided_at.isoformat(),
            "discount_percent": decision.discount_percent,
        },
        "gate": {
            "approved": gate_result.approved,
            "blocking_rule": gate_result.blocking_rule,
            "blocked_reason": gate_result.blocked_reason,
            "evaluated_at": gate_result.evaluated_at.isoformat(),
        },
        "action_result": _action_result_record(outcome.action_result),
        "outcome": {
            "recovered": outcome.recovered,
            "recovered_amount": outcome.recovered_amount,
        },
    }


def _action_result_record(action_result: ActionResult | None) -> dict | None:
    if action_result is None:
        return None
    return {
        "succeeded": action_result.succeeded,
        "amount_recovered": action_result.amount_recovered,
        "detail": action_result.detail,
        "executed_at": action_result.executed_at.isoformat(),
    }
