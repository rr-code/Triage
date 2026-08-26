# Triage

A dunning agent that decides which failed payments are worth retrying — and,
just as important, which ones aren't. Built for the Razorpay AI Buildathon
(Track 3, AI Revenue Recovery).

## The problem

Every retry attempt against a payment method costs something even when it
fails — issuer goodwill, rate-limit headroom — and every customer message
costs attention, so retrying blindly is expensive twice over. Most dunning
systems retry every failed payment on a fixed schedule regardless of *why*
it failed, which means they grind forever on cards that are permanently
dead and decline codes that were never going to answer differently on the
fifth attempt. Triage reads the decline code and the case's own history
before deciding whether a payment is worth pursuing at all, and if so,
which lever to pull — retry, switch instrument, nudge, discount, or write
off.

## Headline result

Run against 1,500 synthetic Razorpay test-mode events (`--seed 7`):

| | Recovered | vs. unguarded baseline |
|---|---:|---:|
| Unguarded baseline (retries everything, no safety checks) | ₹35,30,833.77 | — |
| Triage (decline-code-aware strategy + guardrails) | ₹64,15,014.18 | **+81.7%** |

That's **+₹28,84,180.41** total, decomposed into what each piece
contributed on this run:

- **Guardrails alone:** −₹7,70,894.72 (see [Known limitations](#known-limitations) — this is a real, disclosed trade-off, not a rounding error)
- **The strategy alone:** +₹36,55,075.13
- Triage proposed **0** same-instrument retries against a permanently dead
  instrument on this run; the unguarded baseline proposed **180**.
- Both configurations made the same number of customer contacts (**402**),
  so the gain isn't coming from contacting people more.

Reproduce this exactly:

```
python -m src.triage.cli generate --count 1500 --seed 7 --out events.json
python -m src.triage.cli compare --events events.json --seed 7
```

## Quickstart

From the repository root:

```
# 1. generate a synthetic dataset (prints a distribution summary)
python -m src.triage.cli generate --count 1500 --seed 7 --out events.json

# 2. run one strategy through the pipeline
python -m src.triage.cli run --events events.json --strategy rules --gate guardrails

# 3. the three-way comparison (naive+permissive / naive+guardrails / rules+guardrails)
python -m src.triage.cli compare --events events.json --seed 7

# 4. build the self-contained HTML dashboard
python -m src.triage.cli dashboard --events events.json --seed 7 --out dashboard.html --out-dir out
```

Open `dashboard.html` directly from disk — no server, no build step, no
network access required. It also writes `out/comparison.json`, the
intermediate report data the dashboard is built from.

Run the test suite (186 tests, standard library `unittest`, no dependencies
beyond the project itself):

```
python -m unittest discover -s tests
```

`--strategy` accepts `naive` (blind fixed-cadence retry, the honest
baseline) or `rules` (decline-code-aware). `--gate` accepts `guardrails` or
`permissive`. Every command accepts `--seed` to pin the mock gateway's RNG
for reproducible numbers.

## Known limitations

Named here on purpose, not left for someone else to find.

- **Single-pass simulation.** Each call to the pipeline starts every case's
  history at zero — there's no persisted store of past attempts across
  separate runs. The guardrails' attempt ceiling, contact cap, and cooldown
  are correctly wired but structurally inert within one run; only the
  hard-decline and backoff checks (which key off the event's own
  timestamp, not accumulated history) actually bind. A real deployment
  would need a persisted `CaseHistory` store to make the rest of the
  guardrails do anything.
- **Guardrails can show a negative revenue number, and did on the run
  above.** Blocking a premature retry forfeits its small immediate chance
  of success, and a single pass has no later cycle to retry it once
  backoff has actually elapsed at better odds. In a multi-day system that
  blocked retry is deferred, not lost. This run shows the guardrails
  costing ₹7,70,894.72 — real, and it's the strategy's own +₹36,55,075.13
  that makes the net trade worth it, not the guardrails in isolation.
- **The gateway is a seeded mock, not live Razorpay.** It's structurally
  incapable of favoring a strategy — `charge()` never receives a strategy
  identity, only decline-derived facts — but its specific success
  probabilities (soft-decline base rates, new-instrument success rate) are
  documented judgment calls in `mock_gateway.py`, not fitted to real
  issuer data.
- **The synthetic dataset's distributions are assumptions, named as
  such.** Category mix, decline-code mix, the ~25% hard-decline share, and
  the ~20% "genuinely unrecoverable" noise share are constants at the top
  of `generator.py`, chosen to be a believable and unforgiving test case —
  not measured from a real merchant's traffic. Change them and the exact
  rupee figures move; the qualitative shape of the result (naive wastes
  attempts on dead instruments, Triage doesn't) is what should hold.
- **The LLM strategy has never been run against a live model in this
  project.** `LLMStrategy` is fully built, validated, and gated exactly
  like every other strategy, but no `ANTHROPIC_API_KEY` or network access
  was available during development. Only the validation/rejection/fallback
  machinery is tested, against a fake client. There is no empirical
  divergence data yet.
- **Whether the LLM is worth including is an open question, argued
  structurally, not measured.** `decide()` only ever sees a small,
  mostly-categorical signal space (category, decline class, amount,
  attempt number, timing) that `RulesStrategy`'s fixed cascade already
  covers exhaustively, and the validator constrains the model to that same
  legal action space anyway. Until Triage ingests richer input (raw issuer
  text, customer history, novel decline codes), the honest expectation is
  that a decision table does the same job.
- **The dashboard's embedded audit log is capped at ~4,000 rows** across
  all three configurations combined, to keep the file openable. Large
  runs get a fair per-configuration sample rather than the complete trail
  (this run's 3,489 rows all fit under the cap, so nothing was dropped
  here).

## Project layout

Five pipeline stages (`detect` → `decide` → `gate` → `act` → `measure`),
each its own module, wired together in `pipeline.py`. See
[ARCHITECTURE.md](ARCHITECTURE.md) for how they fit together and why.
