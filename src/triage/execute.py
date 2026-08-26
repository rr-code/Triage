"""The act stage: the only module in Triage permitted side effects.

Every Action maps to exactly one handler, through exactly one dispatch
table (_HANDLERS, below) — this is the single place to look when asking
"what does OFFER_DISCOUNT actually do." Only the two retry actions touch
the gateway: they are the only actions this simulation has an interesting
probability model for. A message, an escalation, a write-off, and a wait
are all treated as deterministic records of what was chosen, since this
project has no independent model of message-delivery failure — Triage's
whole probabilistic story lives in whether a charge succeeds, and that
story belongs to the gateway alone (see mock_gateway.py).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .gateway import ChargeOutcome, PaymentGateway
from .models import Action, ActionResult, Case, Decision


def execute(decision: Decision, case: Case, gateway: PaymentGateway, now: datetime) -> ActionResult:
    return _HANDLERS[decision.action](decision, case, gateway, now)


def _retry_same_instrument(decision: Decision, case: Case, gateway: PaymentGateway, now: datetime) -> ActionResult:
    return _from_charge(decision, now, _charge(case, gateway, now, is_new_instrument=False))


def _retry_different_instrument(
    decision: Decision, case: Case, gateway: PaymentGateway, now: datetime
) -> ActionResult:
    return _from_charge(decision, now, _charge(case, gateway, now, is_new_instrument=True))


def _charge(case: Case, gateway: PaymentGateway, now: datetime, *, is_new_instrument: bool) -> ChargeOutcome:
    hours_since_decline = (now - case.event.occurred_at).total_seconds() / 3600
    return gateway.charge(
        decline=case.decline,
        hours_since_decline=hours_since_decline,
        is_new_instrument=is_new_instrument,
        amount=case.event.amount,
    )


def _send_reminder(decision: Decision, case: Case, gateway: PaymentGateway, now: datetime) -> ActionResult:
    return _dispatched(decision, now, "reminder sent")


def _send_payment_link(decision: Decision, case: Case, gateway: PaymentGateway, now: datetime) -> ActionResult:
    return _dispatched(decision, now, "payment link resent")


def _offer_discount(decision: Decision, case: Case, gateway: PaymentGateway, now: datetime) -> ActionResult:
    return _dispatched(decision, now, f"{decision.discount_percent}% discount offer sent")


def _request_mandate_renewal(decision: Decision, case: Case, gateway: PaymentGateway, now: datetime) -> ActionResult:
    return _dispatched(decision, now, "mandate renewal request sent")


def _escalate_to_human(decision: Decision, case: Case, gateway: PaymentGateway, now: datetime) -> ActionResult:
    return _record(decision, now, "escalated to human review")


def _write_off(decision: Decision, case: Case, gateway: PaymentGateway, now: datetime) -> ActionResult:
    return _record(decision, now, "case written off")


def _wait(decision: Decision, case: Case, gateway: PaymentGateway, now: datetime) -> ActionResult:
    return _record(decision, now, "no action taken this cycle")


def _dispatched(decision: Decision, now: datetime, detail: str) -> ActionResult:
    return _record(decision, now, detail)


def _record(decision: Decision, now: datetime, detail: str) -> ActionResult:
    return ActionResult(
        case_id=decision.case_id,
        action=decision.action,
        succeeded=True,
        amount_recovered=0,
        detail=detail,
        executed_at=now,
    )


def _from_charge(decision: Decision, now: datetime, outcome: ChargeOutcome) -> ActionResult:
    return ActionResult(
        case_id=decision.case_id,
        action=decision.action,
        succeeded=outcome.succeeded,
        amount_recovered=outcome.amount if outcome.succeeded else 0,
        detail=outcome.detail,
        executed_at=now,
    )


_HANDLERS: dict[Action, Callable[[Decision, Case, PaymentGateway, datetime], ActionResult]] = {
    Action.RETRY_SAME_INSTRUMENT: _retry_same_instrument,
    Action.RETRY_DIFFERENT_INSTRUMENT: _retry_different_instrument,
    Action.SEND_REMINDER: _send_reminder,
    Action.SEND_PAYMENT_LINK: _send_payment_link,
    Action.OFFER_DISCOUNT: _offer_discount,
    Action.REQUEST_MANDATE_RENEWAL: _request_mandate_renewal,
    Action.ESCALATE_TO_HUMAN: _escalate_to_human,
    Action.WRITE_OFF: _write_off,
    Action.WAIT: _wait,
}
