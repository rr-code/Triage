"""Wires detect -> decide -> gate -> act -> measure into one pipeline run.

The gate is an injected parameter, not an import. run() has no idea
whether it was handed guardrails_gate, permissive_gate, or anything else
that satisfies the same (Decision, Case, CaseHistory, datetime) ->
GateResult shape — that ignorance is deliberate. It's what makes "same
strategy, with and without guardrails" an honest comparison rather than
one this module could quietly bias by knowing which gate is "the real
one."

Each call to run() is a single pass over one batch of events: detect()
runs once, and every resulting case gets exactly one decide/gate/act
cycle. CaseHistory starts empty for every case on every call — this
pipeline does not persist attempts across separate calls to run(), so the
guardrails' attempt ceiling, contact cap, and cooldown are inert within a
single run and only bind once something outside this module replays run()
against a persisted history store. The hard-decline and backoff checks,
which don't depend on history, still apply normally.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

from .detect import detect
from .execute import execute
from .gateway import PaymentGateway
from .guardrails import CaseHistory
from .measure import measure
from .models import Case, CaseOutcome, Decision, GateResult, PaymentEvent, RunReport, SkipRecord
from .strategy import Strategy

GateFunc = Callable[[Decision, Case, CaseHistory, datetime], GateResult]

# Every case in a single run() call starts with no recorded history — see
# the module docstring for why the budget-tracking guardrail checks are
# inert (though still correctly wired) within one call.
_EMPTY_HISTORY = CaseHistory(total_attempts=0, customer_contacts=0, last_attempt_at=None, last_contact_at=None)


def run(
    events: list[PaymentEvent], strategy: Strategy, gateway: PaymentGateway, gate: GateFunc
) -> tuple[RunReport, list[CaseOutcome], list[SkipRecord]]:
    started_at = datetime.now(timezone.utc)
    cases, skipped = detect(events, now=started_at)

    outcomes: list[CaseOutcome] = []
    for case in cases:
        decision = strategy.decide(case)
        gate_result = gate(decision, case, _EMPTY_HISTORY, started_at)

        action_result = None
        recovered = False
        recovered_amount = 0
        if gate_result.approved:
            action_result = execute(decision, case, gateway, started_at)
            recovered = action_result.succeeded and action_result.amount_recovered > 0
            recovered_amount = action_result.amount_recovered

        outcomes.append(
            CaseOutcome(
                case=case,
                decision=decision,
                gate_result=gate_result,
                action_result=action_result,
                recovered=recovered,
                recovered_amount=recovered_amount,
            )
        )

    finished_at = datetime.now(timezone.utc)
    report = measure(
        outcomes, run_id=uuid.uuid4().hex, strategy_name=strategy.name, started_at=started_at, finished_at=finished_at
    )
    return report, outcomes, skipped
