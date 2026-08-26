"""The naive baseline: retry every failure on a fixed cadence, blind to the decline code.

This is not a strawman. It is deliberately given every advantage that
isn't decline-code knowledge, because the whole pitch behind Triage rests
on this comparison being credible rather than rigged:

- Same candidates: it decides over exactly the Cases detect() produced
  for every other strategy, in the same rank order.
- Same gateway: it executes through the identical act stage and
  mock/real Razorpay gateway as any other strategy.
- Same budget: the guardrails gate enforces the same attempt ceiling,
  contact cap, and cooldown against it as against everyone else. A retry
  it proposes against a hard decline, or before a code's backoff has
  elapsed, gets blocked exactly like it would for any other strategy —
  this strategy has no way to know that, which is the point.
- Same category structure: it routes each case to the action that fits
  its category (retry a failed charge, resend a cold link, ask for
  mandate renewal). A real merchant's dunning system knows that much
  without any payments expertise, so denying it that would be rigging
  the comparison, not testing decline-code awareness.

What it genuinely lacks — the ONLY thing it lacks — is any awareness of
what the decline CODE means. It never reads case.decline. DO_NOT_HONOUR,
CARD_EXPIRED, and STOLEN_CARD get the identical response as
INSUFFICIENT_FUNDS: try the same lever again. It has no soft/hard
distinction, no code-tuned backoff, and no fallback lever of its own — it
just proposes the same action every cycle it's asked, on a fixed cadence,
until the gate or the attempt ceiling stops it. This is genuinely how a
lot of dunning setups run today, which is exactly why it's the baseline
worth beating.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Action, Case, CaseCategory, Decision

# The one piece of structure this strategy is allowed to know: what kind
# of case it's looking at. Every code within a category gets this same
# action — that's the "no decline-code awareness" boundary in code form.
_ACTION_BY_CATEGORY: dict[CaseCategory, Action] = {
    CaseCategory.FAILED_AUTOPAY: Action.RETRY_SAME_INSTRUMENT,
    CaseCategory.FAILED_ONE_TIME: Action.RETRY_SAME_INSTRUMENT,
    CaseCategory.EXPIRING_MANDATE: Action.REQUEST_MANDATE_RENEWAL,
    CaseCategory.COLD_PAYMENT_LINK: Action.SEND_PAYMENT_LINK,
}


class NaiveRetryEverything:
    name = "naive_retry_everything"
    description = "Retries every failed case on a fixed cadence with no decline-code awareness."

    def decide(self, case: Case) -> Decision:
        action = _ACTION_BY_CATEGORY[case.event.category]
        return Decision(
            case_id=case.case_id,
            action=action,
            reason=f"fixed-cadence retry for category={case.event.category.value}; decline code not consulted",
            strategy_name=self.name,
            decided_at=datetime.now(timezone.utc),
            discount_percent=None,
        )
