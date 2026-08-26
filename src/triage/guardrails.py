"""The gate stage: the only place a Decision may become executable.

Nothing reaches act without passing through here, and no strategy can
soften a check. A Decision carries only the content of its own choice
(which action, what discount if any) — every budget, timing, and history
fact the gate checks against comes from CaseHistory, which the pipeline
builds from actual executed ActionResults, never from anything a strategy
reports about itself. A strategy can lie in `reason`; it cannot rewrite
history.

Two gates share one signature so they're interchangeable in the pipeline:
- `guardrails_gate` enforces every hard limit below.
- `permissive_gate` allows everything, unconditionally — built now, not
  retrofitted later, so it can be swapped in to measure what the
  guardrails themselves are worth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .config import (
    MAX_CUSTOMER_CONTACTS_PER_CASE,
    MAX_DISCOUNT_PERCENT,
    MAX_TOTAL_ATTEMPTS_PER_CASE,
    MIN_HOURS_BETWEEN_CONTACTS,
)
from .declines import DeclineClass
from .models import CUSTOMER_VISIBLE_ACTIONS, Action, Case, Decision, GateResult


class GuardrailRule(str, Enum):
    NEVER_RETRY_HARD_DECLINE = "never_retry_hard_decline"
    DECLINE_BACKOFF_NOT_ELAPSED = "decline_backoff_not_elapsed"
    ATTEMPT_CEILING_EXCEEDED = "attempt_ceiling_exceeded"
    CONTACT_CAP_EXCEEDED = "contact_cap_exceeded"
    CONTACT_COOLDOWN_ACTIVE = "contact_cooldown_active"
    OFFER_MISSING_DISCOUNT = "offer_missing_discount"
    DISCOUNT_CEILING_EXCEEDED = "discount_ceiling_exceeded"


# Actions that spend no attempt budget: WRITE_OFF/ESCALATE_TO_HUMAN close a
# case out instead of attempting recovery, and WAIT proposes nothing at
# all. None should count against, or be blocked by, the attempt ceiling —
# otherwise a maxed-out case could never be closed, and a strategy that
# correctly held off would be penalized for holding off.
_NON_ATTEMPT_ACTIONS = frozenset({Action.ESCALATE_TO_HUMAN, Action.WRITE_OFF, Action.WAIT})

_Verdict = tuple[GuardrailRule, str] | None


@dataclass(frozen=True)
class CaseHistory:
    """The authoritative record of what has already happened to a case.

    Built by the pipeline from actually executed ActionResults — never
    from anything a strategy reports about itself.
    """

    total_attempts: int
    customer_contacts: int
    last_attempt_at: datetime | None
    last_contact_at: datetime | None


def guardrails_gate(decision: Decision, case: Case, history: CaseHistory, now: datetime) -> GateResult:
    for check in (
        _check_no_hard_decline_retry,
        _check_decline_backoff,
        _check_attempt_ceiling,
        _check_contact_cap,
        _check_contact_cooldown,
        _check_discount_offer,
    ):
        verdict = check(decision, case, history, now)
        if verdict is not None:
            rule, reason = verdict
            return GateResult(
                decision=decision, approved=False, blocking_rule=rule.value, blocked_reason=reason, evaluated_at=now
            )
    return GateResult(decision=decision, approved=True, blocking_rule=None, blocked_reason=None, evaluated_at=now)


def permissive_gate(decision: Decision, case: Case, history: CaseHistory, now: datetime) -> GateResult:
    """Allows every decision, unconditionally — for measuring what guardrails_gate is worth."""
    return GateResult(decision=decision, approved=True, blocking_rule=None, blocked_reason=None, evaluated_at=now)


def _check_no_hard_decline_retry(decision: Decision, case: Case, history: CaseHistory, now: datetime) -> _Verdict:
    if decision.action is not Action.RETRY_SAME_INSTRUMENT:
        return None
    if case.decline is not None and case.decline.decline_class is DeclineClass.HARD:
        return (
            GuardrailRule.NEVER_RETRY_HARD_DECLINE,
            f"{case.decline.code} is a hard decline; the instrument is dead and retrying it recovers nothing",
        )
    return None


def _check_decline_backoff(decision: Decision, case: Case, history: CaseHistory, now: datetime) -> _Verdict:
    if decision.action is not Action.RETRY_SAME_INSTRUMENT:
        return None
    if case.decline is None:
        return None
    if case.decline.retry_after_hours is None:
        return (
            GuardrailRule.DECLINE_BACKOFF_NOT_ELAPSED,
            f"{case.decline.code} specifies no viable retry timing for the same instrument",
        )
    reference = history.last_attempt_at or case.event.occurred_at
    elapsed_hours = (now - reference).total_seconds() / 3600
    if elapsed_hours < case.decline.retry_after_hours:
        return (
            GuardrailRule.DECLINE_BACKOFF_NOT_ELAPSED,
            f"only {elapsed_hours:.1f}h elapsed since the last attempt; {case.decline.code} requires "
            f"{case.decline.retry_after_hours}h before a retry is meaningful",
        )
    return None


def _check_attempt_ceiling(decision: Decision, case: Case, history: CaseHistory, now: datetime) -> _Verdict:
    if decision.action in _NON_ATTEMPT_ACTIONS:
        return None
    if history.total_attempts >= MAX_TOTAL_ATTEMPTS_PER_CASE:
        return (
            GuardrailRule.ATTEMPT_CEILING_EXCEEDED,
            f"case has already used {history.total_attempts} attempts, at the ceiling of {MAX_TOTAL_ATTEMPTS_PER_CASE}",
        )
    return None


def _check_contact_cap(decision: Decision, case: Case, history: CaseHistory, now: datetime) -> _Verdict:
    if decision.action not in CUSTOMER_VISIBLE_ACTIONS:
        return None
    if history.customer_contacts >= MAX_CUSTOMER_CONTACTS_PER_CASE:
        return (
            GuardrailRule.CONTACT_CAP_EXCEEDED,
            f"case has already used {history.customer_contacts} customer contacts, at the cap of "
            f"{MAX_CUSTOMER_CONTACTS_PER_CASE}",
        )
    return None


def _check_contact_cooldown(decision: Decision, case: Case, history: CaseHistory, now: datetime) -> _Verdict:
    if decision.action not in CUSTOMER_VISIBLE_ACTIONS:
        return None
    if history.last_contact_at is None:
        return None
    hours_since_contact = (now - history.last_contact_at).total_seconds() / 3600
    if hours_since_contact < MIN_HOURS_BETWEEN_CONTACTS:
        return (
            GuardrailRule.CONTACT_COOLDOWN_ACTIVE,
            f"last customer contact was {hours_since_contact:.1f}h ago; cooldown requires "
            f"{MIN_HOURS_BETWEEN_CONTACTS}h",
        )
    return None


def _check_discount_offer(decision: Decision, case: Case, history: CaseHistory, now: datetime) -> _Verdict:
    if decision.action is not Action.OFFER_DISCOUNT:
        return None
    if decision.discount_percent is None or decision.discount_percent <= 0:
        return (GuardrailRule.OFFER_MISSING_DISCOUNT, "OFFER_DISCOUNT decision carries no actual discount")
    if decision.discount_percent > MAX_DISCOUNT_PERCENT:
        return (
            GuardrailRule.DISCOUNT_CEILING_EXCEEDED,
            f"{decision.discount_percent}% exceeds the discount ceiling of {MAX_DISCOUNT_PERCENT}%",
        )
    return None
