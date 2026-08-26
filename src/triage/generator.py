"""Synthetic PaymentEvent generator for demos and offline strategy comparison.

Every distribution is a named constant at the top of this file — nothing
is buried inside a random call — specifically so each one can be swept
independently to see how sensitive downstream recovery numbers are to it.

Two properties are deliberate, not incidental:

- DO_NOT_HONOUR is the single most common decline code (DO_NOT_HONOUR_SHARE
  is the largest individual weight among the eight). It's also the least
  informative code in the taxonomy. Skewing the dataset toward it is what
  makes the benchmark appropriately unforgiving, not a softball for a
  decline-code-aware strategy to win against.
- NOISE_SHARE of the dataset is genuinely unrecoverable ON PURPOSE:
  resolved, disputed, stale, or below the value floor. A dataset where
  everything is recoverable would make every strategy's recovery-rate
  metric theatre — there would be nothing to correctly walk away from.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta
from typing import TypeVar

from .config import MANDATE_EXPIRY_IMMINENT_HOURS, MIN_CASE_VALUE_PAISE, RECOVERY_WINDOW_HOURS
from .models import CaseCategory, EventStatus, PaymentEvent

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Category mix — what kind of case each event represents.
# ---------------------------------------------------------------------------

CATEGORY_WEIGHTS: dict[CaseCategory, float] = {
    CaseCategory.FAILED_AUTOPAY: 0.35,  # subscriptions are the highest-volume dunning source
    CaseCategory.FAILED_ONE_TIME: 0.30,  # one-off checkout failures, nearly as common
    CaseCategory.COLD_PAYMENT_LINK: 0.20,  # sent and never completed
    CaseCategory.EXPIRING_MANDATE: 0.15,  # proactive, not reactive — smallest slice by design
}

# ---------------------------------------------------------------------------
# Decline code mix — only applies to FAILED_AUTOPAY / FAILED_ONE_TIME, the
# two categories that represent an actual declined charge.
# ---------------------------------------------------------------------------

# DO_NOT_HONOUR is deliberately the single most common code: real issuer
# feeds are dominated by it, and it's the least informative code in the
# taxonomy — the dataset should be unforgiving, not softballed.
DO_NOT_HONOUR_SHARE = 0.30

# Roughly a quarter of failures are hard declines. Named on its own so it
# can be changed independently to test how sensitive recovery numbers are
# to this one assumption — the rest of the decline mix rescales around it.
HARD_DECLINE_SHARE = 0.25

# Relative split *within* the hard-decline share (must sum to 1.0).
HARD_CODE_RELATIVE_WEIGHTS: dict[str, float] = {
    "CARD_EXPIRED": 0.40,
    "MANDATE_REVOKED": 0.24,
    "ACCOUNT_CLOSED": 0.24,
    "STOLEN_CARD": 0.12,
}

# Relative split within whatever share is left after DO_NOT_HONOUR and the
# hard declines are accounted for (must sum to 1.0).
OTHER_SOFT_CODE_RELATIVE_WEIGHTS: dict[str, float] = {
    "INSUFFICIENT_FUNDS": 0.45,
    "ISSUER_UNAVAILABLE": 0.20,
    "LIMIT_EXCEEDED": 0.35,
}

# ---------------------------------------------------------------------------
# Noise — genuinely unrecoverable events, included on purpose. A dataset
# where every case is recoverable makes the recovery-rate metric theatre.
# ---------------------------------------------------------------------------

NOISE_SHARE = 0.20

NOISE_TYPE_WEIGHTS: dict[str, float] = {
    "resolved": 0.30,  # paid through another channel before Triage got to it
    "disputed": 0.15,  # under active chargeback — must not be touched
    "stale": 0.30,  # already past its category's recovery window
    "below_value_floor": 0.25,  # not worth the operational cost of pursuing
}

# ---------------------------------------------------------------------------
# Attempt number — most cases are first sightings; a minority have already
# been retried once or twice, which is what exercises the repeat-failure
# and cold-link nudge-threshold rules downstream.
# ---------------------------------------------------------------------------

ATTEMPT_NUMBER_WEIGHTS: dict[int, float] = {1: 0.70, 2: 0.20, 3: 0.10}

# ---------------------------------------------------------------------------
# Amounts — order of magnitude for a mid-market Indian merchant, in paise.
# ---------------------------------------------------------------------------

NORMAL_AMOUNT_MIN_PAISE = 50_000  # Rs.500
NORMAL_AMOUNT_MAX_PAISE = 5_000_000  # Rs.50,000

# ---------------------------------------------------------------------------
# Timing — how recent normal events are, and how far past its window a
# deliberately stale event is pushed.
# ---------------------------------------------------------------------------

RECENT_EVENT_MAX_AGE_HOURS = 12  # normal events sit well inside every category's recovery window
STALE_EVENT_EXTRA_HOURS = 24  # how far past the window a "stale" noise event is pushed, unambiguously


def generate_events(count: int, seed: int, now: datetime) -> list[PaymentEvent]:
    rng = random.Random(seed)
    return [_generate_one(index, rng, now) for index in range(count)]


def _generate_one(index: int, rng: random.Random, now: datetime) -> PaymentEvent:
    category = _weighted_choice(rng, CATEGORY_WEIGHTS)
    noise_type = _weighted_choice(rng, NOISE_TYPE_WEIGHTS) if rng.random() < NOISE_SHARE else None

    status = EventStatus.OPEN
    if noise_type == "resolved":
        status = EventStatus.RESOLVED
    elif noise_type == "disputed":
        status = EventStatus.DISPUTED

    window_hours = RECOVERY_WINDOW_HOURS[category]
    if noise_type == "stale":
        age_hours = window_hours + STALE_EVENT_EXTRA_HOURS + rng.uniform(0, RECENT_EVENT_MAX_AGE_HOURS)
    else:
        age_hours = rng.uniform(0, min(RECENT_EVENT_MAX_AGE_HOURS, window_hours - 1))
    occurred_at = now - timedelta(hours=age_hours)

    if noise_type == "below_value_floor":
        amount = rng.randint(1, MIN_CASE_VALUE_PAISE - 1)
    else:
        amount = rng.randint(NORMAL_AMOUNT_MIN_PAISE, NORMAL_AMOUNT_MAX_PAISE)

    decline_code = None
    if category in (CaseCategory.FAILED_AUTOPAY, CaseCategory.FAILED_ONE_TIME):
        decline_code = _weighted_choice(rng, _decline_code_weights())

    mandate_expires_at = None
    if category is CaseCategory.EXPIRING_MANDATE:
        mandate_expires_at = now + timedelta(hours=rng.uniform(1, MANDATE_EXPIRY_IMMINENT_HOURS - 1))

    attempt_number = _weighted_choice(rng, ATTEMPT_NUMBER_WEIGHTS)

    return PaymentEvent(
        event_id=f"evt-{index:06d}",
        occurred_at=occurred_at,
        customer_id=f"cust-{index:06d}",
        payment_id=f"pay-{index:06d}",
        instrument_id=f"instr-{index:06d}",
        amount=amount,
        currency="INR",
        category=category,
        decline_code=decline_code,
        status=status,
        attempt_number=attempt_number,
        mandate_expires_at=mandate_expires_at,
    )


def _decline_code_weights() -> dict[str, float]:
    other_soft_share = 1.0 - DO_NOT_HONOUR_SHARE - HARD_DECLINE_SHARE
    assert 0 <= other_soft_share <= 1, "DO_NOT_HONOUR_SHARE + HARD_DECLINE_SHARE must not exceed 1.0"

    weights: dict[str, float] = {"DO_NOT_HONOUR": DO_NOT_HONOUR_SHARE}
    for code, relative_weight in OTHER_SOFT_CODE_RELATIVE_WEIGHTS.items():
        weights[code] = relative_weight * other_soft_share
    for code, relative_weight in HARD_CODE_RELATIVE_WEIGHTS.items():
        weights[code] = relative_weight * HARD_DECLINE_SHARE
    return weights


def _weighted_choice(rng: random.Random, weights: dict[T, float]) -> T:
    return rng.choices(list(weights.keys()), weights=list(weights.values()), k=1)[0]


def summarize(events: list[PaymentEvent], now: datetime) -> str:
    """A distribution summary so a generated dataset can be sanity-checked by eye."""
    total = len(events)
    lines = [f"Generated {total} events"]

    def section(title: str, counter: Counter, denominator: int) -> None:
        lines.append("")
        lines.append(f"{title}:")
        for key, count in counter.most_common():
            label = key.value if hasattr(key, "value") else str(key)
            share = count / denominator if denominator else 0.0
            lines.append(f"  {label:<22} {count:>6}  ({share:.1%})")

    section("By category", Counter(e.category for e in events), total)
    section("By status", Counter(e.status for e in events), total)

    decline_events = [e for e in events if e.decline_code is not None]
    if decline_events:
        section(
            f"Decline codes (of {len(decline_events)} declined charges)",
            Counter(e.decline_code for e in decline_events),
            len(decline_events),
        )

    below_floor = sum(1 for e in events if e.amount < MIN_CASE_VALUE_PAISE)
    stale = sum(
        1 for e in events if (now - e.occurred_at).total_seconds() / 3600 > RECOVERY_WINDOW_HOURS[e.category]
    )
    resolved = sum(1 for e in events if e.status is EventStatus.RESOLVED)
    disputed = sum(1 for e in events if e.status is EventStatus.DISPUTED)

    lines.append("")
    lines.append("Genuinely unrecoverable, on purpose:")
    lines.append(f"  below value floor      {below_floor:>6}  ({below_floor / total:.1%})" if total else "  (empty dataset)")
    lines.append(f"  stale (past window)    {stale:>6}  ({stale / total:.1%})")
    lines.append(f"  resolved               {resolved:>6}  ({resolved / total:.1%})")
    lines.append(f"  disputed               {disputed:>6}  ({disputed / total:.1%})")

    return "\n".join(lines)
