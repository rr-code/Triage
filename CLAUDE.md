# Triage

A dunning agent that decides which failed payments are worth retrying.
Built for the Razorpay AI Buildathon (Track 3, AI Revenue Recovery).

## Domain

- Four case categories: `failed_autopay`, `expiring_mandate`,
  `cold_payment_link`, `failed_one_time`.
- The soft vs hard decline distinction is the core of the product. Never
  retry a hard decline — it recovers nothing, costs an attempt, and risks
  the merchant's issuer reputation.
- `DO_NOT_HONOUR` is the most common and least informative decline code.
  One backed-off retry, then switch lever (e.g. payment link, channel).
  Never grind on it.
- Retry timing matters more than retry count.

## Architecture

- Five stages, each its own module: `detect -> decide -> gate -> act ->
  measure`.
- Guardrails are an independent module, never folded into a strategy —
  the safety layer must be impossible for a strategy or an LLM to route
  around.
- All strategies implement one shared protocol and run through the same
  pipeline, so comparisons across strategies are structurally fair, not
  asserted.
- The payment gateway sits behind a protocol so swapping the mock for
  real Razorpay test-mode is a one-file change.
- `config.py` holds every policy constant, commented. No magic numbers
  anywhere else.
- Only the `act`/execute module may have side effects. `detect`,
  `decide`, `gate`, and `measure` are pure.

## Code style

- Python 3.10+, standard library first. No framework unless explicitly
  requested.
- Type hints on all public functions; dataclasses for domain objects.
- Tests before implementation for anything with real logic.
