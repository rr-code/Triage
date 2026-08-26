"""Decline code taxonomy for Triage.

Each code is classified along two independent axes:

- how the SAME instrument is likely to behave on retry
  (decline_class, retry_after_hours)
- whether a DIFFERENT instrument could still recover the payment
  (instrument_switch_may_help)

A dead card does not imply an unreachable customer. Keeping these axes
separate is what lets the gate recover a hard decline by switching lever
instead of writing the case off.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DeclineClass(Enum):
    SOFT = "soft"            # transient; same instrument may work later
    HARD = "hard"             # same instrument is dead; never retry it
    AMBIGUOUS = "ambiguous"   # could be either; treat cautiously


# Backoff applied to a decline code this table has never seen. Real issuer
# feeds surface uncatalogued codes, and the safe default is one slow
# retry, never a retry storm.
UNKNOWN_CODE_RETRY_AFTER_HOURS = 24


@dataclass(frozen=True)
class DeclineInfo:
    code: str
    decline_class: DeclineClass
    retry_after_hours: int | None  # None = never retry this instrument
    instrument_switch_may_help: bool  # can a DIFFERENT instrument still pay?
    note: str


_TAXONOMY: dict[str, DeclineInfo] = {
    "INSUFFICIENT_FUNDS": DeclineInfo(
        code="INSUFFICIENT_FUNDS",
        decline_class=DeclineClass.SOFT,
        retry_after_hours=24,
        instrument_switch_may_help=True,
        note="Funds may replenish; a later retry on the same instrument is often enough.",
    ),
    "ISSUER_UNAVAILABLE": DeclineInfo(
        code="ISSUER_UNAVAILABLE",
        decline_class=DeclineClass.SOFT,
        retry_after_hours=1,
        instrument_switch_may_help=True,
        note="Transient bank-side outage; a short backoff usually resolves it.",
    ),
    "LIMIT_EXCEEDED": DeclineInfo(
        code="LIMIT_EXCEEDED",
        decline_class=DeclineClass.SOFT,
        retry_after_hours=24,
        instrument_switch_may_help=True,
        note="Per-transaction or velocity limit; wait for the limit window to reset.",
    ),
    "DO_NOT_HONOUR": DeclineInfo(
        code="DO_NOT_HONOUR",
        decline_class=DeclineClass.AMBIGUOUS,
        retry_after_hours=6,
        instrument_switch_may_help=True,
        note="Most common, least informative code. One backed-off retry, then switch lever — never grind on it.",
    ),
    "CARD_EXPIRED": DeclineInfo(
        code="CARD_EXPIRED",
        decline_class=DeclineClass.HARD,
        retry_after_hours=None,
        instrument_switch_may_help=True,
        note="Instrument is permanently dead; the customer may still pay with a different card.",
    ),
    "MANDATE_REVOKED": DeclineInfo(
        code="MANDATE_REVOKED",
        decline_class=DeclineClass.HARD,
        retry_after_hours=None,
        instrument_switch_may_help=True,
        note="Customer withdrew authorization; needs a fresh mandate, not a retry.",
    ),
    "ACCOUNT_CLOSED": DeclineInfo(
        code="ACCOUNT_CLOSED",
        decline_class=DeclineClass.HARD,
        retry_after_hours=None,
        instrument_switch_may_help=True,
        note="Underlying account no longer exists; instruments unrelated to it may still work.",
    ),
    "STOLEN_CARD": DeclineInfo(
        code="STOLEN_CARD",
        decline_class=DeclineClass.HARD,
        retry_after_hours=None,
        instrument_switch_may_help=False,
        note="Fraud signal, not just a dead card. Do not auto-switch instrument; route to manual review.",
    ),
}


def classify(code: str) -> DeclineInfo:
    """Look up a decline code's taxonomy entry.

    Unrecognised codes fail closed: AMBIGUOUS, a cautious single backoff,
    and no assumption that switching instrument is safe. The table will
    surface issuer codes it has never seen, and the default must never
    behave as safe-to-retry.
    """
    known = _TAXONOMY.get(code.upper())
    if known is not None:
        return known
    return DeclineInfo(
        code=code,
        decline_class=DeclineClass.AMBIGUOUS,
        retry_after_hours=UNKNOWN_CODE_RETRY_AFTER_HOURS,
        instrument_switch_may_help=False,
        note="Unrecognised decline code — treated as ambiguous with a cautious backoff until catalogued.",
    )
