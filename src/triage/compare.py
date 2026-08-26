"""The comparison harness — the most important output in the project.

Three configurations, not two:

    naive + permissive gate  -> what an unguarded cron job does today
    naive + full guardrails  -> the gap here is what the GUARDRAILS are worth
    rules + full guardrails  -> the gap from above is what the STRATEGY is worth

Running the baseline behind the guardrails would quietly hand it the
single best idea in this project (never retry a dead instrument) and
then report the remainder as the strategy's improvement — understating
the guardrails' contribution and misattributing the rest. Three
configurations decompose the lift instead of declaring a winner:

    guardrails_lift = naive+guardrails  - naive+permissive
    strategy_lift   = rules+guardrails  - naive+guardrails
    total_lift      = guardrails_lift + strategy_lift  (== rules+guardrails - naive+permissive)

Every configuration runs against the same events and a gateway built from
the same seed, so no configuration gets luckier than another. (The mock's
RNG still advances per charge() call, and different strategies make
different numbers of calls before reaching any given case — so "same
seed" guarantees no config starts from a better draw, not that a specific
case's odds line up bit-for-bit across configs. That residual noise is
inherent to comparing strategies that behave differently, not a bug.)

Lifts and the contact count are reported with their sign, unmodified —
a negative lift or a strategy that contacts customers MORE than the
baseline is reported exactly as plainly as a flattering number would be.
"""

from __future__ import annotations

from dataclasses import dataclass

from .gateway import PaymentGateway
from .guardrails import guardrails_gate, permissive_gate
from .metrics import Metrics, compute_metrics
from .mock_gateway import DEFAULT_SEED, MockRazorpayGateway
from .models import PaymentEvent, RunReport
from .naive_strategy import NaiveRetryEverything
from .pipeline import run as run_pipeline
from .rules_strategy import RulesStrategy


@dataclass(frozen=True)
class ConfigResult:
    label: str
    strategy_name: str
    gate_name: str
    report: RunReport
    metrics: Metrics

    @property
    def recovery_per_contact(self) -> float | None:
        if self.metrics.customer_contacts == 0:
            return None
        return self.metrics.recovered_amount / self.metrics.customer_contacts


@dataclass(frozen=True)
class ComparisonReport:
    baseline: ConfigResult  # naive + permissive gate
    guardrailed_baseline: ConfigResult  # naive + full guardrails
    triage: ConfigResult  # rules + full guardrails

    @property
    def guardrails_lift(self) -> int:
        """What guardrails alone are worth: same strategy, gate varies."""
        return self.guardrailed_baseline.metrics.recovered_amount - self.baseline.metrics.recovered_amount

    @property
    def strategy_lift(self) -> int:
        """What the decline-code-aware strategy alone is worth: same gate, strategy varies."""
        return self.triage.metrics.recovered_amount - self.guardrailed_baseline.metrics.recovered_amount

    @property
    def total_lift(self) -> int:
        return self.triage.metrics.recovered_amount - self.baseline.metrics.recovered_amount

    @property
    def contact_delta(self) -> int:
        """Triage's customer contacts minus the unguarded baseline's. Positive means Triage contacts MORE."""
        return self.triage.metrics.customer_contacts - self.baseline.metrics.customer_contacts


def run_comparison(events: list[PaymentEvent], gateway_seed: int = DEFAULT_SEED) -> ComparisonReport:
    baseline = _run_config("naive + permissive gate", NaiveRetryEverything(), permissive_gate, "permissive", events, gateway_seed)
    guardrailed_baseline = _run_config(
        "naive + full guardrails", NaiveRetryEverything(), guardrails_gate, "guardrails", events, gateway_seed
    )
    triage = _run_config("rules + full guardrails", RulesStrategy(), guardrails_gate, "guardrails", events, gateway_seed)
    return ComparisonReport(baseline=baseline, guardrailed_baseline=guardrailed_baseline, triage=triage)


def _run_config(label: str, strategy, gate, gate_name: str, events: list[PaymentEvent], gateway_seed: int) -> ConfigResult:
    gateway: PaymentGateway = MockRazorpayGateway(seed=gateway_seed)
    report, outcomes, skipped = run_pipeline(events, strategy, gateway, gate)
    metrics = compute_metrics(outcomes, skipped)
    return ConfigResult(label=label, strategy_name=strategy.name, gate_name=gate_name, report=report, metrics=metrics)


def summarize_comparison(comparison: ComparisonReport) -> str:
    lines = ["Three configurations, same events, same gateway seed:", ""]

    for config in (comparison.baseline, comparison.guardrailed_baseline, comparison.triage):
        rpc = config.recovery_per_contact
        rpc_text = f"{rpc:,.0f} paise/contact" if rpc is not None else "n/a (no contacts)"
        lines.append(
            f"  {config.label:<24} recovered {config.metrics.recovered_amount:>14,} paise   "
            f"contacts {config.metrics.customer_contacts:>4}   {rpc_text}"
        )

    lines.append("")
    lines.append(f"Guardrails changed recovered amount by {comparison.guardrails_lift:+,} paise "
                 f"(naive+guardrails vs naive+permissive)")
    lines.append(f"The strategy changed recovered amount by {comparison.strategy_lift:+,} paise "
                 f"(rules+guardrails vs naive+guardrails)")
    lines.append(f"Total lift over the unguarded baseline: {comparison.total_lift:+,} paise")

    lines.append("")
    contact_delta = comparison.contact_delta
    if contact_delta > 0:
        lines.append(
            f"Honest trade-off: Triage makes {contact_delta} MORE customer contacts than the "
            "unguarded baseline, not fewer."
        )
    elif contact_delta < 0:
        lines.append(f"Triage makes {-contact_delta} fewer customer contacts than the unguarded baseline.")
    else:
        lines.append("Triage makes the same number of customer contacts as the unguarded baseline.")

    all_contacts = {
        comparison.baseline.metrics.customer_contacts,
        comparison.guardrailed_baseline.metrics.customer_contacts,
        comparison.triage.metrics.customer_contacts,
    }
    if len(all_contacts) == 1:
        lines.append(
            "All three configurations land on the exact same contact total here — that's not a "
            "counting bug, it's how this build's strategies behave: only cold-link and mandate cases "
            "ever produce a customer-visible action, and every strategy contacts 100% of those cases "
            "regardless of which specific message it sends. Retry-based categories never contact a "
            "customer at all, silent or not. Contact caps and cooldowns are also inert within a single "
            "pass (each run starts every case's history empty — see README's Known Limitations), so "
            "switching gates can't move this number either. It would only move if a strategy's own "
            "logic chose not to contact some of these cases."
        )

    return "\n".join(lines)
