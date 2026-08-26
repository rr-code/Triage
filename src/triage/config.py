"""Every tunable policy constant in Triage, in one place.

Rule for the rest of the project: no magic numbers outside this file. If a
stage needs a threshold, it imports it from here.
"""

from __future__ import annotations

from .models import CaseCategory

# ---------------------------------------------------------------------------
# Contact caps — how many customer-visible touches a single case may receive.
# ---------------------------------------------------------------------------

# Past three messages, dunning reads as harassment: opt-outs and spam
# complaints start costing more than the extra attempts recover.
MAX_CUSTOMER_CONTACTS_PER_CASE = 3

# Silent retries cost the customer no attention, but they are not free —
# each one is a real authorization attempt against the issuer, and issuers
# rate-limit and reputation-score merchants who hammer a dead instrument.
MAX_SILENT_RETRIES_PER_CASE = 5

# ---------------------------------------------------------------------------
# Cooldown — minimum spacing between customer-visible touches.
# ---------------------------------------------------------------------------

# One message per day, at most. Any faster and reminders start competing
# with the transactional notifications the customer actually wants.
MIN_HOURS_BETWEEN_CONTACTS = 24

# ---------------------------------------------------------------------------
# Attempt ceiling — total lever pulls (retries + contacts) before a case is
# forced to manual review or write-off, regardless of category.
# ---------------------------------------------------------------------------

# A case that hasn't converted across 6 attempts on every lever we have
# is not going to convert on attempt 7; further spend is pure loss.
MAX_TOTAL_ATTEMPTS_PER_CASE = 6

# ---------------------------------------------------------------------------
# Discount ceiling — largest incentive a strategy may offer without a human
# sign-off.
# ---------------------------------------------------------------------------

# Above 15%, the margin given up on a recovered payment can exceed the
# margin lost by writing the case off outright; that trade needs a human.
MAX_DISCOUNT_PERCENT = 15

# ---------------------------------------------------------------------------
# Minimum case value — cases smaller than this aren't worth the operational
# cost (issuer attempts, support time, customer attention) of pursuing.
# ---------------------------------------------------------------------------

# Rs.100 in paise (Razorpay's smallest currency unit). Below this, expected
# recovery is smaller than the cost of a single support escalation.
MIN_CASE_VALUE_PAISE = 10_000

# ---------------------------------------------------------------------------
# Per-category recovery window — hours after detection during which pursuing
# a case is still worth it. Past this, intent has decayed too far.
# ---------------------------------------------------------------------------

RECOVERY_WINDOW_HOURS: dict[CaseCategory, int] = {
    # Subscription products typically grace-period service for ~3 days
    # before suspending it; that's also the natural recovery deadline.
    CaseCategory.FAILED_AUTOPAY: 72,
    # Mandates need renewing before the *next* billing cycle, not the missed
    # one; a full week gives room without risking that next charge too.
    CaseCategory.EXPIRING_MANDATE: 168,
    # Payment links go cold fast — click-through collapses sharply after
    # the first two days.
    CaseCategory.COLD_PAYMENT_LINK: 48,
    # One-time purchase intent decays fastest of all four categories; past
    # a day the customer has likely bought elsewhere or moved on.
    CaseCategory.FAILED_ONE_TIME: 24,
}

# ---------------------------------------------------------------------------
# Mandate imminence — how close to expiry a mandate must be before it's
# worth pursuing at all.
# ---------------------------------------------------------------------------

# A mandate expiring further out than this isn't urgent yet; contacting the
# customer this early wastes a contact before they have any reason to act.
MANDATE_EXPIRY_IMMINENT_HOURS = 72

# ---------------------------------------------------------------------------
# Hard decline likelihood multiplier — how much to discount the base prior
# when the instrument itself is dead.
# ---------------------------------------------------------------------------

# A hard decline means the instrument won't come back to life, so recovery
# depends entirely on the customer switching instruments — a much weaker
# bet than a soft decline of the same category, but not zero, so the case
# stays detected rather than being written off outright.
HARD_DECLINE_LIKELIHOOD_MULTIPLIER = 0.3

# ---------------------------------------------------------------------------
# Cold link nudges — how many plain resends a cold payment link gets before
# escalating to a discount offer.
# ---------------------------------------------------------------------------

# No instrument ever failed here, so there's no decline signal to reason
# from — only silence. A link that hasn't converted after two plain nudges
# is on a short leash: escalate to the capped offer rather than nudge again.
COLD_LINK_NUDGES_BEFORE_DISCOUNT = 2

# ---------------------------------------------------------------------------
# Base recovery likelihood — category-level prior used to rank cases before
# any decline-code-specific adjustment is applied.
# ---------------------------------------------------------------------------

BASE_RECOVERY_LIKELIHOOD: dict[CaseCategory, float] = {
    # Existing subscriber who already trusted us with recurring billing;
    # historically the highest-recovering category.
    CaseCategory.FAILED_AUTOPAY: 0.55,
    # Still an engaged, paying customer, but recovery needs an active
    # renewal action rather than a passive retry.
    CaseCategory.EXPIRING_MANDATE: 0.45,
    # One-off purchase intent with no ongoing relationship to lean on.
    CaseCategory.FAILED_ONE_TIME: 0.35,
    # Unauthenticated intent that already went cold once; lowest prior of
    # the four categories.
    CaseCategory.COLD_PAYMENT_LINK: 0.20,
}
