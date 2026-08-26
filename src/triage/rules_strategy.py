"""RulesStrategy — Triage proper.

Organising principle: the decline code says whether the INSTRUMENT can
work again; the category says which LEVER is available. Those two facts
together determine the action — nothing else does. decide() is a strict
priority cascade, checked in this order, so a reader can follow it
top to bottom exactly as it executes:

1. Pre-failure (expiring mandate). Nothing has broken yet, so a single
   well-timed re-auth prompt beats anything available after a failure —
   this is the highest-value thing the system does, and it's checked
   first regardless of what decline code (if any) happens to be present.
2. Hard declines. The instrument is dead. Switch instrument; never retry
   the one that just failed.
3. Cold links. There was no instrument failure at all, only silence —
   nudge on a short leash, then escalate to a capped offer.
4. Soft and ambiguous declines. Retry, but only once the taxonomy's
   backoff has actually elapsed; before that, WAIT rather than fire a
   retry the gate would just block anyway. An ambiguous decline
   (DO_NOT_HONOUR and its kin) that has already failed once does not get
   retried again on the same instrument at all: an identical second
   request buys no new information, so the lever switches instead.

Anything that reaches decide() without matching one of the four (a
failure-category case with no decline code on record at all) is a data
gap, not a judgment call, and is escalated rather than guessed at.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config import COLD_LINK_NUDGES_BEFORE_DISCOUNT, MAX_DISCOUNT_PERCENT
from .declines import DeclineClass
from .models import Action, Case, CaseCategory, Decision


class RulesStrategy:
    name = "triage_rules"
    description = "Decline-code-aware dunning: instrument health from the code, lever choice from the category."

    def decide(self, case: Case) -> Decision:
        now = datetime.now(timezone.utc)

        if case.event.category is CaseCategory.EXPIRING_MANDATE:
            return self._expiring_mandate(case, now)

        if case.decline is not None and case.decline.decline_class is DeclineClass.HARD:
            return self._hard_decline(case, now)

        if case.event.category is CaseCategory.COLD_PAYMENT_LINK:
            return self._cold_link(case, now)

        if case.decline is not None and case.decline.decline_class in (DeclineClass.SOFT, DeclineClass.AMBIGUOUS):
            return self._soft_or_ambiguous_decline(case, now)

        return self._no_decline_info(case, now)

    # 1. Pre-failure ----------------------------------------------------

    def _expiring_mandate(self, case: Case, now: datetime) -> Decision:
        return self._decision(
            case,
            now,
            Action.REQUEST_MANDATE_RENEWAL,
            "mandate is expiring and nothing has failed yet — one well-timed re-auth "
            "prompt now beats any lever available after it lapses into a failed charge",
        )

    # 2. Hard declines ----------------------------------------------------

    def _hard_decline(self, case: Case, now: datetime) -> Decision:
        decline = case.decline
        assert decline is not None
        if decline.instrument_switch_may_help:
            return self._decision(
                case,
                now,
                Action.RETRY_DIFFERENT_INSTRUMENT,
                f"{decline.code} is a hard decline — the instrument is dead, so retrying it is "
                "pointless, but the taxonomy says a different instrument may still work",
            )
        # detect() drops hard declines with no viable switch before they
        # ever become a Case; this is a defensive fallback, not the
        # expected path.
        return self._decision(
            case,
            now,
            Action.ESCALATE_TO_HUMAN,
            f"{decline.code} is a hard decline with no viable instrument switch — nothing left "
            "for this strategy to try, so this goes to a human rather than being guessed at",
        )

    # 3. Cold links ----------------------------------------------------

    def _cold_link(self, case: Case, now: datetime) -> Decision:
        if case.event.attempt_number <= COLD_LINK_NUDGES_BEFORE_DISCOUNT:
            return self._decision(
                case,
                now,
                Action.SEND_PAYMENT_LINK,
                f"cold link, nudge {case.event.attempt_number} of {COLD_LINK_NUDGES_BEFORE_DISCOUNT} — "
                "no decline signal exists here, only silence, so resend before spending the discount lever",
            )
        return self._decision(
            case,
            now,
            Action.OFFER_DISCOUNT,
            f"cold link still unanswered after {COLD_LINK_NUDGES_BEFORE_DISCOUNT} plain nudges — short "
            f"leash means escalating straight to the capped {MAX_DISCOUNT_PERCENT}% offer, not nudging again",
            discount_percent=MAX_DISCOUNT_PERCENT,
        )

    # 4. Soft and ambiguous declines --------------------------------------

    def _soft_or_ambiguous_decline(self, case: Case, now: datetime) -> Decision:
        decline = case.decline
        assert decline is not None

        if decline.decline_class is DeclineClass.AMBIGUOUS and case.event.attempt_number >= 2:
            return self._decision(
                case,
                now,
                Action.RETRY_DIFFERENT_INSTRUMENT,
                f"{decline.code} already failed once and is ambiguous by definition — an identical "
                "second request buys no new information, so the lever switches instead of repeating it",
            )

        if decline.retry_after_hours is None:
            # Every SOFT/AMBIGUOUS entry in the current taxonomy carries a
            # backoff; a future one that doesn't gives nothing to time a
            # retry against, so this fails closed rather than guessing.
            return self._decision(
                case,
                now,
                Action.ESCALATE_TO_HUMAN,
                f"{decline.code} is classified {decline.decline_class.value} but specifies no retry "
                "timing — nothing to schedule against, so this needs a human call",
            )

        elapsed_hours = (now - case.event.occurred_at).total_seconds() / 3600
        if elapsed_hours >= decline.retry_after_hours:
            return self._decision(
                case,
                now,
                Action.RETRY_SAME_INSTRUMENT,
                f"{elapsed_hours:.1f}h have passed since the decline, past {decline.code}'s "
                f"{decline.retry_after_hours}h backoff — a retry now has a real chance",
            )
        return self._decision(
            case,
            now,
            Action.WAIT,
            f"only {elapsed_hours:.1f}h have passed since the decline; {decline.code} needs "
            f"{decline.retry_after_hours}h before a retry means anything — scheduling, not firing early",
        )

    # Fallback ----------------------------------------------------------

    def _no_decline_info(self, case: Case, now: datetime) -> Decision:
        return self._decision(
            case,
            now,
            Action.ESCALATE_TO_HUMAN,
            f"{case.event.category.value} case has no decline code on record — nothing in the "
            "taxonomy to reason from, so this needs a human look rather than a blind retry",
        )

    @staticmethod
    def _decision(
        case: Case, now: datetime, action: Action, reason: str, *, discount_percent: int | None = None
    ) -> Decision:
        return Decision(
            case_id=case.case_id,
            action=action,
            reason=reason,
            strategy_name=RulesStrategy.name,
            decided_at=now,
            discount_percent=discount_percent,
        )
