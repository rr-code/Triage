"""The PaymentGateway protocol.

Sits behind this Protocol specifically so swapping the mock for real
Razorpay test-mode is a one-file change: nothing outside a concrete
implementation of PaymentGateway needs to change when that swap happens.

The signature of charge() is deliberately narrow. It takes only the facts
that a real payment gateway would actually be reacting to — the decline's
taxonomy classification, how long it's been since the decline, whether
this is a new instrument, and the amount — and nothing about who decided
to call it. There is no `strategy_name`, no `Decision`, no `Case`. That
absence is structural, not a promise: no implementation of this protocol
can be strategy-aware even if it wanted to be, because the information
simply never arrives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .declines import DeclineInfo


@dataclass(frozen=True)
class ChargeOutcome:
    succeeded: bool
    amount: int  # the amount attempted, echoed back regardless of outcome
    reference: str
    detail: str


@runtime_checkable
class PaymentGateway(Protocol):
    def charge(
        self, *, decline: DeclineInfo | None, hours_since_decline: float, is_new_instrument: bool, amount: int
    ) -> ChargeOutcome: ...
