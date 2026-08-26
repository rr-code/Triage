"""A mock Razorpay test-mode gateway, driven entirely by the decline code.

This mock's success model is the most load-bearing assumption in the
project: every recovery number Triage claims ultimately traces back to
whether charge() here returns succeeded=True. It is built to be argued
with, not hidden behind randomness:

- charge() receives only decline-derived facts (see PaymentGateway) —
  never a strategy name, a Decision, or anything else that would let it
  tell naive from Triage. Both get charged against the exact same table
  for the exact same facts; the signature makes that structurally true.
- A hard decline never succeeds on the same instrument. Not a low
  probability rolled against the RNG — the branch below returns before
  the RNG is even touched. "A closed account reopens because you asked
  twice" isn't a probability, it's a category error, and the code says so.
- A new instrument is modeled as an entirely fresh, healthy charge,
  independent of whatever killed the old one — because it is one.
- A soft/ambiguous decline's odds depend on whether the taxonomy's
  backoff has elapsed, not on how many times it's been tried. That gap is
  what makes retry TIMING the lever worth optimizing, not attempt count.

Seeded with its own `random.Random` instance, not the global `random`
module, so a demo run is exactly reproducible from a fixed seed and two
gateway instances never share hidden state.
"""

from __future__ import annotations

import random

from .declines import DeclineClass, DeclineInfo
from .gateway import ChargeOutcome

# A fresh instrument carries none of the old instrument's problem — this
# is modeled as an ordinary healthy charge, not conditioned on the prior
# decline at all. Comparable to a typical first-attempt success rate.
NEW_INSTRUMENT_SUCCESS_RATE = 0.65

# A same-instrument retry with no decline code on record (should be rare
# in practice) has no taxonomy signal to condition on; treated as a coin
# flip rather than assumed to lean either way.
UNKNOWN_DECLINE_SUCCESS_RATE = 0.40

# Soft/ambiguous decline, retried AFTER the taxonomy's backoff has
# elapsed: a real, meaningfully-better-than-even shot — deliberately
# close to this project's category-level base recovery priors. Timing
# gives the underlying condition (funds, an outage, a rate limit) room to
# resolve, but it's not a guarantee.
POST_BACKOFF_SUCCESS_RATE = 0.55

# Soft/ambiguous decline, retried BEFORE the taxonomy's backoff has
# elapsed: still possible — a card gets topped up mid-cycle, an outage
# clears early — but rare enough that it should never look like a
# reasonable bet. The gap between this and POST_BACKOFF_SUCCESS_RATE is
# what makes timing, not attempt count, the thing worth optimizing.
PRE_BACKOFF_SUCCESS_RATE = 0.08

# Fixed so a demo run is exactly reproducible; change it and every run
# after tells a different, equally valid, but different story.
DEFAULT_SEED = 20260824


class MockRazorpayGateway:
    def __init__(self, seed: int = DEFAULT_SEED) -> None:
        self._random = random.Random(seed)
        self._next_reference = 1

    def charge(
        self, *, decline: DeclineInfo | None, hours_since_decline: float, is_new_instrument: bool, amount: int
    ) -> ChargeOutcome:
        if is_new_instrument:
            succeeded = self._roll(NEW_INSTRUMENT_SUCCESS_RATE)
            return self._outcome(succeeded, amount, "new instrument charge, judged independently of the prior decline")

        if decline is None:
            succeeded = self._roll(UNKNOWN_DECLINE_SUCCESS_RATE)
            return self._outcome(succeeded, amount, "same-instrument retry with no decline code on record")

        if decline.decline_class is DeclineClass.HARD:
            return self._outcome(False, amount, f"{decline.code} is a hard decline; the instrument cannot recover")

        backoff_elapsed = decline.retry_after_hours is not None and hours_since_decline >= decline.retry_after_hours
        rate = POST_BACKOFF_SUCCESS_RATE if backoff_elapsed else PRE_BACKOFF_SUCCESS_RATE
        succeeded = self._roll(rate)
        timing = "past" if backoff_elapsed else "before"
        return self._outcome(succeeded, amount, f"same-instrument retry, {timing} {decline.code}'s taxonomy backoff")

    def _roll(self, success_rate: float) -> bool:
        return self._random.random() < success_rate

    def _outcome(self, succeeded: bool, amount: int, detail: str) -> ChargeOutcome:
        reference = f"mock_rzp_{self._next_reference:06d}"
        self._next_reference += 1
        return ChargeOutcome(succeeded=succeeded, amount=amount, reference=reference, detail=detail)
