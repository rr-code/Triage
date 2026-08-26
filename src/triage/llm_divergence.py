"""Divergence analysis: how often LLMStrategy's final decision differs from
what RulesStrategy would have chosen for the same case, and whether those
divergences recovered more or less money.

A rejected LLM proposal is NOT counted as a divergence: LLMStrategy falls
back to the rules decision on rejection, so its final action is identical
to rules' by construction. Only an accepted, taxonomy-valid proposal
whose action differs from what rules would have proposed is a genuine
divergence — that's the only case where the model actually changed the
outcome rather than just being overruled.

Both strategies run through the identical guardrails_gate, over the same
events, against a gateway built from the same seed — the same fairness
discipline compare.py uses for the naive/rules decomposition. The same
caveat applies too: the mock's RNG advances per charge() call, and rules
and the LLM will generally make different numbers of calls before
reaching any given case once they've diverged on an earlier one, so a
specific case's draw isn't guaranteed to line up bit-for-bit between the
two runs. That's inherent to comparing strategies that behave
differently, not a bug in this harness.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .gateway import PaymentGateway
from .guardrails import guardrails_gate
from .mock_gateway import DEFAULT_SEED, MockRazorpayGateway
from .models import PaymentEvent
from .pipeline import run as run_pipeline
from .rules_strategy import RulesStrategy
from .strategy import Strategy


@dataclass(frozen=True)
class DivergenceReport:
    total_cases: int
    rejected_proposals: int
    divergent_cases: int
    llm_total_recovered_amount: int
    rules_total_recovered_amount: int
    divergent_llm_recovered_amount: int
    divergent_rules_recovered_amount: int

    @property
    def divergence_rate(self) -> float | None:
        return self.divergent_cases / self.total_cases if self.total_cases else None

    @property
    def divergence_recovered_delta(self) -> int:
        """Positive means the LLM's divergent decisions recovered MORE than rules would have."""
        return self.divergent_llm_recovered_amount - self.divergent_rules_recovered_amount


def analyze_divergence(
    events: list[PaymentEvent],
    llm_strategy: Strategy,
    *,
    gateway_seed: int = DEFAULT_SEED,
    make_gateway: Callable[[], PaymentGateway] | None = None,
) -> DivergenceReport:
    make_gateway = make_gateway or (lambda: MockRazorpayGateway(seed=gateway_seed))

    rules_report, rules_outcomes, _ = run_pipeline(events, RulesStrategy(), make_gateway(), guardrails_gate)
    llm_report, llm_outcomes, _ = run_pipeline(events, llm_strategy, make_gateway(), guardrails_gate)

    rules_by_case = {outcome.case.case_id: outcome for outcome in rules_outcomes}
    llm_by_case = {outcome.case.case_id: outcome for outcome in llm_outcomes}

    # Import here, not at module top, to avoid a hard dependency on the
    # module that defines the rejection marker for callers who only need
    # the divergence counting and not the LLM strategy itself.
    from .llm_strategy import is_rejected_proposal

    rejected_proposals = sum(1 for outcome in llm_outcomes if is_rejected_proposal(outcome.decision))

    divergent_case_ids = [
        case_id
        for case_id, llm_outcome in llm_by_case.items()
        if case_id in rules_by_case and llm_outcome.decision.action != rules_by_case[case_id].decision.action
    ]

    return DivergenceReport(
        total_cases=len(llm_outcomes),
        rejected_proposals=rejected_proposals,
        divergent_cases=len(divergent_case_ids),
        llm_total_recovered_amount=llm_report.total_recovered_amount,
        rules_total_recovered_amount=rules_report.total_recovered_amount,
        divergent_llm_recovered_amount=sum(llm_by_case[cid].recovered_amount for cid in divergent_case_ids),
        divergent_rules_recovered_amount=sum(rules_by_case[cid].recovered_amount for cid in divergent_case_ids),
    )


def summarize_divergence(report: DivergenceReport) -> str:
    lines = [
        f"{report.total_cases} cases, {report.rejected_proposals} rejected proposals, "
        f"{report.divergent_cases} genuine divergences "
        f"({report.divergence_rate:.1%} of cases)" if report.total_cases else "no cases",
        "",
        f"LLM total recovered:   {report.llm_total_recovered_amount:>14,} paise",
        f"Rules total recovered: {report.rules_total_recovered_amount:>14,} paise",
        "",
        f"On the {report.divergent_cases} divergent cases specifically:",
        f"  LLM recovered:   {report.divergent_llm_recovered_amount:>14,} paise",
        f"  Rules would have: {report.divergent_rules_recovered_amount:>14,} paise",
    ]

    delta = report.divergence_recovered_delta
    if delta > 0:
        lines.append(f"  Divergences recovered {delta:+,} paise MORE than sticking with rules would have.")
    elif delta < 0:
        lines.append(f"  Divergences recovered {delta:+,} paise LESS than sticking with rules would have.")
    else:
        lines.append("  Divergences recovered exactly as much as sticking with rules would have.")

    return "\n".join(lines)
