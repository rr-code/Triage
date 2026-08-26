# Architecture

## The five stages

Every payment moves through five stages, each its own module, wired
together by `pipeline.run(events, strategy, gateway, gate)`:

| Stage | Module | Does |
|---|---|---|
| detect | `detect.py` | Filters raw `PaymentEvent`s down to `Case`s worth pursuing (excludes resolved, disputed, stale, below the value floor) and ranks them by expected value. Pure: takes only `events` and `now`, never a strategy. |
| decide | `strategy.py` + `naive_strategy.py` / `rules_strategy.py` / `llm_strategy.py` | A strategy proposes one `Action` for a `Case`, with a reason. No side effects. |
| gate | `guardrails.py` | Checks the proposed `Decision` against hard safety rules (`guardrails_gate`) or lets everything through (`permissive_gate`). |
| act | `execute.py` | The only module permitted side effects. Maps each `Action` to exactly one handler through a single dispatch table; only the two retry actions call the gateway. |
| measure | `measure.py`, `metrics.py`, `audit_log.py` | Rolls every case's outcome into a `RunReport`, an exception breakdown (`Metrics`), and a JSONL audit trail. |

`detect` and `decide` produce `Case` and `Decision` objects (`models.py`);
`gate` and `act` produce `GateResult` and `ActionResult`; `measure`
combines all of it into `CaseOutcome`. Every object is a frozen
dataclass — an audit log is only trustworthy if the thing it describes
can't be mutated after the fact.

## Why guardrails are a separate injectable gate, not logic inside a strategy

`guardrails_gate` and `permissive_gate` share one signature —
`(Decision, Case, CaseHistory, datetime) -> GateResult` — which is what
lets `pipeline.run()` accept `gate` as a plain parameter. `pipeline.py`
never imports `guardrails_gate` directly; it has no idea which gate it
was handed. That's deliberate for two reasons:

1. **Safety can't be opted out of, even by accident.** If hard-decline
   checks, backoff enforcement, and contact caps lived inside each
   strategy, every new strategy — including a future one — would have to
   remember to reimplement them, and a strategy that forgot, or that
   reasoned its way around them, would silently lose the guarantee. A
   `Decision` only ever carries the *content* of a strategy's choice
   (`action`, `reason`, `discount_percent`); it never carries a claim
   about compliance. `CaseHistory`, which the gate checks against, is
   built by the pipeline from `ActionResult`s that were actually
   executed — never from anything a strategy reports about itself. A
   strategy can misjudge in its `reason` string; it cannot fabricate
   history.
2. **It makes "what are the guardrails worth" answerable.** Because the
   gate is a swappable parameter and not a hardcoded import, the same
   strategy can run through `guardrails_gate` and `permissive_gate` and
   produce a clean two-point comparison (see `compare.py`) —
   `naive+guardrails` minus `naive+permissive` is a real measurement of
   the guardrails' own contribution, not something asserted.

## Why every strategy shares one protocol

`strategy.py` defines `Strategy` as a `runtime_checkable` `Protocol`:
`name`, `description`, `decide(case) -> Decision`. `NaiveRetryEverything`,
`RulesStrategy`, and `LLMStrategy` satisfy it structurally, not through
inheritance — nothing about the pipeline needs to know which kind of
strategy it's running.

This is what makes a claim like "naive recovered ₹35,30,833.77, Triage
recovered ₹64,15,014.18" structurally fair rather than fair by assertion:

- `detect()` is strategy-agnostic by construction — it takes only
  `events` and `now`, so every strategy decides over the identical,
  identically-ranked candidate set. No strategy can see a friendlier
  subset of cases.
- `decide()` is called once per case with no side effects — it can't
  touch the gateway, the guardrails, or another case's history.
- `gate()` evaluates every `Decision` the same way regardless of which
  strategy produced it — proven by construction, since the gate's
  functions never receive a strategy identity at all, only the `Decision`
  and `Case`.
- `PaymentGateway.charge()` (`gateway.py`) takes `decline`,
  `hours_since_decline`, `is_new_instrument`, `amount` — never a strategy
  identity or a `Decision`. The mock (`mock_gateway.py`) has no way to
  tell naive from Triage even if it wanted to.

There is no code path a strategy could take that skips a stage, sees more
information than another strategy, or gets a private lane through the
gate.

## Where the LLM sits, and why there

`llm_strategy.py`'s `LLMStrategy` implements the identical `Strategy`
protocol as everything else — `decide(case) -> Decision`, nothing more.
It is not a privileged path: `guardrails_gate` evaluates its output
exactly as it would `RulesStrategy`'s or `NaiveRetryEverything`'s.

Inside `decide()`, the model's raw proposal (`ModelProposal`:
`action_name`, `reasoning`, `discount_percent`) is validated against the
decline taxonomy *before* it's allowed to become a `Decision`. Rejected:
an unrecognized action name; a same-instrument retry against a hard
decline; a second same-instrument retry on an already-failed ambiguous
decline; a same-instrument retry proposed before the taxonomy's own
backoff has elapsed; a different-instrument retry against a hard decline
the taxonomy says can't be helped by switching. A rejection falls back to
a reference strategy's decision (`RulesStrategy` by default) and is
recorded in the returned `Decision.reason`, prefixed with a marker
`is_rejected_proposal()` checks for — so "the validator caught the model
proposing a retry against a dead card" is provable from the same audit
trail every other decision leaves, not a claim made in a demo.

`make_llm_strategy()` is the only sanctioned constructor: it returns
`None`, constructing nothing, when `ANTHROPIC_API_KEY` isn't set, and
`AnthropicLLMClient` only imports the `anthropic` package lazily, inside
`propose()` — the one method that actually needs it. The rest of the
project, including the full test suite, imports and runs cleanly with no
key and no network access; at the time this was built, `anthropic` wasn't
even installed in the development environment, and every test still
passed.

`llm_divergence.py` separately measures how often the LLM's *accepted*
decision (i.e., not a rejection that fell back) differs from what
`RulesStrategy` would have chosen for the same case, and whether those
divergences recovered more or less money — reusing the same
`pipeline.run()` + `guardrails_gate` path, not a special comparison mode.

## Swapping the mock gateway for real Razorpay test-mode

`gateway.py` defines `PaymentGateway` as a `Protocol` with one method:

```python
charge(self, *, decline: DeclineInfo | None, hours_since_decline: float,
       is_new_instrument: bool, amount: int) -> ChargeOutcome
```

Nothing outside `mock_gateway.py` and the small set of callers that
construct it (`cli.py`, `compare.py`, `llm_divergence.py`) knows
`MockRazorpayGateway` exists — `pipeline.py`, `execute.py`, and every
strategy depend on the `PaymentGateway` protocol, not the concrete class.

To swap in real Razorpay test-mode: write a class — say,
`RazorpayTestModeGateway` — that implements `charge()` with that same
signature, calling the real test-mode API internally (create the charge
attempt, inspect the response) instead of rolling a seeded die, and
returning a `ChargeOutcome(succeeded, amount, reference, detail)`. Pass an
instance of it wherever `MockRazorpayGateway(...)` is constructed today.
Nothing in `pipeline.py`, `execute.py`, or any strategy needs to change.

The one thing that doesn't carry over: `MockRazorpayGateway`'s `seed`
argument and `config.py`'s `DEFAULT_SEED` exist for reproducibility of a
*simulation* — a real gateway takes credentials, not a seed, and its
results won't be replayable the way the mock's are.
