"""Metrics: the recovered figure and the exceptions that produced it, together.

Metrics bundles a recovered amount with the full exception breakdown in
one frozen object, and compute_metrics() is the only function in this
module that produces a Metrics. There is no second, narrower function
that hands back just a recovered number — honesty about what didn't
convert is enforced by the type, not by remembering to also call
something else.

Tracked deliberately unflattering:
- customer_contacts: an interruption is a cost, not a vanity metric.
- dead_instrument_retries: RETRY_SAME_INSTRUMENT proposed against a case
  whose decline is HARD — the specific waste Triage exists to eliminate.
  Counted at decision time, regardless of whether a gate then caught it,
  because the waste is in a strategy proposing it at all, not only in
  whatever fraction of proposals slip past the safety net.

Every outcome that isn't a recovery lands in exactly one of four
exception buckets:
- never_detected: the event never became a Case (see SkipRecord).
- blocked_by_guardrail: a Decision existed but the gate refused it.
- declined_by_strategy: the gate would have let it through, but the
  strategy itself chose not to attempt recovery this cycle (WAIT,
  ESCALATE_TO_HUMAN, WRITE_OFF).
- attempted_and_failed: everything else — a retry that failed, or a
  message that dispatched but produced no money this pass.
"""

from __future__ import annotations

from dataclasses import dataclass

from .declines import DeclineClass
from .models import CUSTOMER_VISIBLE_ACTIONS, Action, CaseOutcome, SkipRecord

# Actions where the strategy itself declined to attempt recovery this
# cycle, as opposed to attempting one that simply didn't pay off.
_NO_ATTEMPT_ACTIONS = frozenset({Action.WAIT, Action.ESCALATE_TO_HUMAN, Action.WRITE_OFF})


@dataclass(frozen=True)
class ExceptionBreakdown:
    blocked_by_guardrail: int
    declined_by_strategy: int
    attempted_and_failed: int
    never_detected: int

    @property
    def total(self) -> int:
        return self.blocked_by_guardrail + self.declined_by_strategy + self.attempted_and_failed + self.never_detected


@dataclass(frozen=True)
class Metrics:
    total_events: int
    recovered_count: int
    recovered_amount: int
    exceptions: ExceptionBreakdown
    customer_contacts: int
    dead_instrument_retries: int


def compute_metrics(outcomes: list[CaseOutcome], skipped: list[SkipRecord]) -> Metrics:
    recovered_count = 0
    recovered_amount = 0
    blocked_by_guardrail = 0
    declined_by_strategy = 0
    attempted_and_failed = 0
    customer_contacts = 0
    dead_instrument_retries = 0

    for outcome in outcomes:
        if outcome.recovered:
            recovered_count += 1
            recovered_amount += outcome.recovered_amount
        elif not outcome.gate_result.approved:
            blocked_by_guardrail += 1
        elif outcome.decision.action in _NO_ATTEMPT_ACTIONS:
            declined_by_strategy += 1
        else:
            attempted_and_failed += 1

        if outcome.action_result is not None and outcome.decision.action in CUSTOMER_VISIBLE_ACTIONS:
            customer_contacts += 1

        if (
            outcome.decision.action is Action.RETRY_SAME_INSTRUMENT
            and outcome.case.decline is not None
            and outcome.case.decline.decline_class is DeclineClass.HARD
        ):
            dead_instrument_retries += 1

    exceptions = ExceptionBreakdown(
        blocked_by_guardrail=blocked_by_guardrail,
        declined_by_strategy=declined_by_strategy,
        attempted_and_failed=attempted_and_failed,
        never_detected=len(skipped),
    )

    return Metrics(
        total_events=len(outcomes) + len(skipped),
        recovered_count=recovered_count,
        recovered_amount=recovered_amount,
        exceptions=exceptions,
        customer_contacts=customer_contacts,
        dead_instrument_retries=dead_instrument_retries,
    )
