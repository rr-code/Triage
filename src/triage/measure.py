"""The measure stage: aggregates a batch of CaseOutcomes into a RunReport."""

from __future__ import annotations

from datetime import datetime

from .models import CUSTOMER_VISIBLE_ACTIONS, CaseOutcome, RunReport


def measure(
    outcomes: list[CaseOutcome], *, run_id: str, strategy_name: str, started_at: datetime, finished_at: datetime
) -> RunReport:
    total_recovered_amount = sum(outcome.recovered_amount for outcome in outcomes)
    # A contact only happened if the gate actually approved it — a
    # blocked customer-visible decision never reached the customer, so it
    # must not count against the contact budget it never spent.
    total_customer_contacts = sum(
        1
        for outcome in outcomes
        if outcome.action_result is not None and outcome.decision.action in CUSTOMER_VISIBLE_ACTIONS
    )
    return RunReport(
        run_id=run_id,
        strategy_name=strategy_name,
        started_at=started_at,
        finished_at=finished_at,
        outcomes=tuple(outcomes),
        total_cases=len(outcomes),
        total_recovered_amount=total_recovered_amount,
        total_customer_contacts=total_customer_contacts,
    )
