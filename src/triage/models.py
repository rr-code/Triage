"""Domain objects for Triage.

All dataclasses are frozen: an audit log is only trustworthy if the thing
it describes can't be mutated after the fact. Each object corresponds to
the output of one pipeline stage (detect -> decide -> gate -> act ->
measure), so a CaseOutcome can be reconstructed and re-audited from its
parts alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .declines import DeclineInfo


class CaseCategory(Enum):
    FAILED_AUTOPAY = "failed_autopay"
    EXPIRING_MANDATE = "expiring_mandate"
    COLD_PAYMENT_LINK = "cold_payment_link"
    FAILED_ONE_TIME = "failed_one_time"


class EventStatus(Enum):
    OPEN = "open"  # still failed and unresolved — the default candidate state
    RESOLVED = "resolved"  # paid through another route; nothing left to recover
    DISPUTED = "disputed"  # chargeback/dispute in progress; must not be contacted


class Action(Enum):
    RETRY_SAME_INSTRUMENT = "retry_same_instrument"
    RETRY_DIFFERENT_INSTRUMENT = "retry_different_instrument"
    SEND_REMINDER = "send_reminder"
    SEND_PAYMENT_LINK = "send_payment_link"
    OFFER_DISCOUNT = "offer_discount"
    REQUEST_MANDATE_RENEWAL = "request_mandate_renewal"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    WRITE_OFF = "write_off"
    WAIT = "wait"  # propose nothing this cycle; schedule rather than fire early


# Deliberately separate from Action itself: a silent retry against an
# instrument costs the customer nothing, but a message costs them
# attention. Guardrails must budget those two costs independently instead
# of counting every action as one interchangeable "attempt".
CUSTOMER_VISIBLE_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.SEND_REMINDER,
        Action.SEND_PAYMENT_LINK,
        Action.OFFER_DISCOUNT,
        Action.REQUEST_MANDATE_RENEWAL,
    }
)


@dataclass(frozen=True)
class PaymentEvent:
    """A raw record as received from the gateway/issuer feed, before Triage interprets it."""

    event_id: str
    occurred_at: datetime
    customer_id: str
    payment_id: str | None
    instrument_id: str
    amount: int  # smallest currency unit (paise)
    currency: str
    category: CaseCategory
    decline_code: str | None
    status: EventStatus
    attempt_number: int
    mandate_expires_at: datetime | None  # only meaningful for EXPIRING_MANDATE


@dataclass(frozen=True)
class Case:
    """A PaymentEvent that detect judged worth tracking: decline classified, priority ranked."""

    case_id: str
    event: PaymentEvent
    decline: DeclineInfo | None  # None when the event has no decline code to classify
    recovery_likelihood: float
    rank: int
    detected_at: datetime


class SkipReason(Enum):
    RESOLVED = "resolved"
    DISPUTED = "disputed"
    BELOW_VALUE_FLOOR = "below_value_floor"
    STALE = "stale"
    MANDATE_NOT_IMMINENT = "mandate_not_imminent"
    MANDATE_EXPIRED = "mandate_expired"
    HARD_DECLINE_NO_INSTRUMENT_SWITCH = "hard_decline_no_instrument_switch"


@dataclass(frozen=True)
class SkipRecord:
    """Why detect excluded a PaymentEvent — kept so cases never silently vanish from the denominator."""

    event: PaymentEvent
    reason: SkipReason
    detail: str


@dataclass(frozen=True)
class Decision:
    """What decide's strategy chose to do about a Case, and why."""

    case_id: str
    action: Action
    reason: str
    strategy_name: str
    decided_at: datetime
    discount_percent: int | None  # only meaningful for Action.OFFER_DISCOUNT


@dataclass(frozen=True)
class GateResult:
    """Whether guardrails let a Decision proceed."""

    decision: Decision
    approved: bool
    blocking_rule: str | None  # name of the rule that fired; None when approved
    blocked_reason: str | None  # human-readable reason; None when approved
    evaluated_at: datetime


@dataclass(frozen=True)
class ActionResult:
    """What actually happened when act executed an approved Decision."""

    case_id: str
    action: Action
    succeeded: bool
    amount_recovered: int
    detail: str
    executed_at: datetime


@dataclass(frozen=True)
class CaseOutcome:
    """The complete, immutable record of one case's path through the pipeline."""

    case: Case
    decision: Decision
    gate_result: GateResult
    action_result: ActionResult | None  # None when the gate blocked the decision
    recovered: bool
    recovered_amount: int


@dataclass(frozen=True)
class RunReport:
    """Aggregate results for one strategy over one batch of cases."""

    run_id: str
    strategy_name: str
    started_at: datetime
    finished_at: datetime
    outcomes: tuple[CaseOutcome, ...]
    total_cases: int
    total_recovered_amount: int
    total_customer_contacts: int
