"""The detect stage: filters raw PaymentEvents to Cases worth acting on, ranked by expected value.

Strategy-agnostic by construction — detect takes only events and the
current time, never a strategy, so every strategy compared later runs
over an identical candidate set. Nothing is silently dropped: anything
excluded comes back as a SkipRecord with a reason, so the denominator is
always reconstructable from cases + skips.

A hard decline is not auto-excluded here: the instrument is dead, but the
customer may still be reachable on a different one, so it stays detected
at a reduced likelihood. It is only dropped when no instrument switch is
viable at all.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .config import (
    BASE_RECOVERY_LIKELIHOOD,
    HARD_DECLINE_LIKELIHOOD_MULTIPLIER,
    MANDATE_EXPIRY_IMMINENT_HOURS,
    MIN_CASE_VALUE_PAISE,
    RECOVERY_WINDOW_HOURS,
)
from .declines import DeclineClass, DeclineInfo, classify
from .models import Case, CaseCategory, EventStatus, PaymentEvent, SkipReason, SkipRecord


@dataclass(frozen=True)
class _Candidate:
    event: PaymentEvent
    decline: DeclineInfo | None
    likelihood: float


def detect(events: Iterable[PaymentEvent], now: datetime) -> tuple[list[Case], list[SkipRecord]]:
    candidates: list[_Candidate] = []
    skipped: list[SkipRecord] = []

    for event in events:
        outcome = _evaluate(event, now)
        if isinstance(outcome, SkipRecord):
            skipped.append(outcome)
        else:
            candidates.append(outcome)

    ranked = sorted(candidates, key=lambda c: c.likelihood * c.event.amount, reverse=True)
    cases = [
        Case(
            case_id=candidate.event.event_id,
            event=candidate.event,
            decline=candidate.decline,
            recovery_likelihood=candidate.likelihood,
            rank=position,
            detected_at=now,
        )
        for position, candidate in enumerate(ranked, start=1)
    ]
    return cases, skipped


def _evaluate(event: PaymentEvent, now: datetime) -> _Candidate | SkipRecord:
    if event.status is EventStatus.RESOLVED:
        return _skip(event, SkipReason.RESOLVED, "payment already resolved through another route")
    if event.status is EventStatus.DISPUTED:
        return _skip(event, SkipReason.DISPUTED, "payment is under active dispute")

    if event.amount < MIN_CASE_VALUE_PAISE:
        return _skip(
            event, SkipReason.BELOW_VALUE_FLOOR, f"amount {event.amount} below floor {MIN_CASE_VALUE_PAISE}"
        )

    age_hours = (now - event.occurred_at).total_seconds() / 3600
    window = RECOVERY_WINDOW_HOURS[event.category]
    if age_hours > window:
        return _skip(
            event,
            SkipReason.STALE,
            f"event is {age_hours:.1f}h old, past the {window}h window for {event.category.value}",
        )

    if event.category is CaseCategory.EXPIRING_MANDATE:
        mandate_skip = _mandate_skip(event, now)
        if mandate_skip is not None:
            return mandate_skip

    decline: DeclineInfo | None = None
    if event.decline_code is not None:
        decline = classify(event.decline_code)
        if decline.decline_class is DeclineClass.HARD and not decline.instrument_switch_may_help:
            return _skip(
                event,
                SkipReason.HARD_DECLINE_NO_INSTRUMENT_SWITCH,
                f"{event.decline_code} is a hard decline with no viable instrument switch",
            )

    return _Candidate(event=event, decline=decline, likelihood=_recovery_likelihood(event.category, decline))


def _mandate_skip(event: PaymentEvent, now: datetime) -> SkipRecord | None:
    expires_at = event.mandate_expires_at
    if expires_at is None:
        return _skip(event, SkipReason.MANDATE_NOT_IMMINENT, "no mandate expiry recorded")
    if expires_at <= now:
        return _skip(event, SkipReason.MANDATE_EXPIRED, f"mandate already expired at {expires_at.isoformat()}")

    hours_to_expiry = (expires_at - now).total_seconds() / 3600
    if hours_to_expiry > MANDATE_EXPIRY_IMMINENT_HOURS:
        return _skip(
            event,
            SkipReason.MANDATE_NOT_IMMINENT,
            f"expires in {hours_to_expiry:.1f}h, beyond the {MANDATE_EXPIRY_IMMINENT_HOURS}h imminent window",
        )
    return None


def _recovery_likelihood(category: CaseCategory, decline: DeclineInfo | None) -> float:
    base = BASE_RECOVERY_LIKELIHOOD[category]
    if decline is not None and decline.decline_class is DeclineClass.HARD:
        return base * HARD_DECLINE_LIKELIHOOD_MULTIPLIER
    return base


def _skip(event: PaymentEvent, reason: SkipReason, detail: str) -> SkipRecord:
    return SkipRecord(event=event, reason=reason, detail=detail)
