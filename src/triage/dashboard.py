"""The dashboard: a single self-contained HTML file, data injected at build time.

Wiring, exactly as specified: the template lives on disk at
dashboard/template.html and contains the literal string "__TRIAGE_DATA__"
(quotes included) as the value of a JS const. write_dashboard() builds the
report data, writes it to out/comparison.json, reads that file back,
attaches the audit log as a top-level "audit" array (capped so the file
stays openable), and replaces the placeholder with the resulting JSON.
That round-trip through disk is deliberate, not redundant — comparison.json
is a real, inspectable artifact independent of the final HTML.

Every number on the page comes from this module's output. Nothing here
guesses: a percentage against a zero baseline is mathematically undefined,
so it's returned as None (which the template renders as "—"), never 0.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .audit_log import to_audit_record
from .compare import ComparisonReport, ConfigResult
from .config import (
    MAX_CUSTOMER_CONTACTS_PER_CASE,
    MAX_DISCOUNT_PERCENT,
    MAX_TOTAL_ATTEMPTS_PER_CASE,
    MIN_HOURS_BETWEEN_CONTACTS,
)
from .declines import DeclineClass, classify
from .models import Action, CaseOutcome

# Total audit rows embedded in the page, across all three configurations
# combined. A page-weight/legibility limit, not a claim about what
# happened — the decision-log view states shown vs total explicitly.
AUDIT_ROW_CAP = 4000

_PLACEHOLDER = '"__TRIAGE_DATA__"'
DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent / "dashboard" / "template.html"

_ALL_TAXONOMY_CODES = (
    "DO_NOT_HONOUR",
    "INSUFFICIENT_FUNDS",
    "ISSUER_UNAVAILABLE",
    "LIMIT_EXCEEDED",
    "CARD_EXPIRED",
    "MANDATE_REVOKED",
    "ACCOUNT_CLOSED",
    "STOLEN_CARD",
)

_RETRY_ACTIONS = (Action.RETRY_SAME_INSTRUMENT, Action.RETRY_DIFFERENT_INSTRUMENT)


def build_report_data(comparison: ComparisonReport, *, dataset_meta: dict | None = None) -> dict:
    """Everything the dashboard needs except the audit rows (those are
    attached separately, after this dict has round-tripped through
    out/comparison.json — see write_dashboard)."""
    triage_outcomes = list(comparison.triage.report.outcomes)
    baseline_recovered = comparison.baseline.metrics.recovered_amount
    triage_recovered = comparison.triage.metrics.recovered_amount

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": dataset_meta or {},
        "policy": _build_policy(),
        "headline": {
            "recovered_amount": triage_recovered,
            "config_label": comparison.triage.label,
            "baseline_recovered_amount": baseline_recovered,
            "pct_vs_baseline": _safe_ratio(triage_recovered - baseline_recovered, baseline_recovered),
            "wasted_retries": comparison.triage.metrics.dead_instrument_retries,
            "customer_contacts": comparison.triage.metrics.customer_contacts,
            "recovery_per_contact": comparison.triage.recovery_per_contact,
            "exceptions_logged": comparison.triage.metrics.exceptions.total,
        },
        "comparison": _build_comparison(comparison),
        "funnel": _build_funnel(comparison.triage),
        "decline_codes": _build_decline_codes(triage_outcomes),
    }


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    """None when the denominator is zero — a ratio against zero is
    undefined, not zero, and must not be reported as either."""
    if denominator == 0:
        return None
    return numerator / denominator


def _build_policy() -> dict:
    return {
        "max_customer_contacts_per_case": MAX_CUSTOMER_CONTACTS_PER_CASE,
        "min_hours_between_contacts": MIN_HOURS_BETWEEN_CONTACTS,
        "max_total_attempts_per_case": MAX_TOTAL_ATTEMPTS_PER_CASE,
        "max_discount_percent": MAX_DISCOUNT_PERCENT,
    }


def _build_comparison(comparison: ComparisonReport) -> dict:
    return {
        "configs": [
            _config_summary(comparison.baseline, "baseline"),
            _config_summary(comparison.guardrailed_baseline, "guardrailed_baseline"),
            _config_summary(comparison.triage, "triage"),
        ],
        "guardrails_lift": comparison.guardrails_lift,
        "strategy_lift": comparison.strategy_lift,
        "total_lift": comparison.total_lift,
        "contact_delta": comparison.contact_delta,
    }


def _config_summary(config: ConfigResult, key: str) -> dict:
    return {
        "key": key,
        "label": config.label,
        "strategy_name": config.strategy_name,
        "gate_name": config.gate_name,
        "recovered_amount": config.metrics.recovered_amount,
        "recovered_count": config.metrics.recovered_count,
        "customer_contacts": config.metrics.customer_contacts,
        "recovery_per_contact": config.recovery_per_contact,
        "total_cases": config.metrics.total_events,
        "dead_instrument_retries": config.metrics.dead_instrument_retries,
        "exceptions": {
            "blocked_by_guardrail": config.metrics.exceptions.blocked_by_guardrail,
            "declined_by_strategy": config.metrics.exceptions.declined_by_strategy,
            "attempted_and_failed": config.metrics.exceptions.attempted_and_failed,
            "never_detected": config.metrics.exceptions.never_detected,
        },
    }


def _build_funnel(triage: ConfigResult) -> dict:
    metrics = triage.metrics
    detected = metrics.total_events - metrics.exceptions.never_detected
    actioned = detected - metrics.exceptions.blocked_by_guardrail
    return {
        "scanned": metrics.total_events,
        "detected": detected,
        "actioned": actioned,
        "recovered": metrics.recovered_count,
        "exceptions": {
            "blocked_by_guardrail": metrics.exceptions.blocked_by_guardrail,
            "declined_by_strategy": metrics.exceptions.declined_by_strategy,
            "attempted_and_failed": metrics.exceptions.attempted_and_failed,
            "never_detected": metrics.exceptions.never_detected,
        },
    }


def _empty_code_row(code: str) -> dict:
    info = classify(code)
    return {
        "code": code,
        "decline_class": info.decline_class.value,
        "note": info.note,
        "cases": 0,
        "attempted": 0,
        "succeeded": 0,
        "recovered": 0,
        "wasted_retries": 0,
    }


def _build_decline_codes(outcomes: list[CaseOutcome]) -> list[dict]:
    # Every known taxonomy code is seeded at zero first, so a code that
    # simply didn't occur in this run still shows a true zero row rather
    # than vanishing from the table.
    rows: dict[str, dict] = {code: _empty_code_row(code) for code in _ALL_TAXONOMY_CODES}

    for outcome in outcomes:
        decline = outcome.case.decline
        if decline is None:
            continue
        row = rows.setdefault(decline.code, _empty_code_row(decline.code))
        row["cases"] += 1
        if outcome.decision.action in _RETRY_ACTIONS and outcome.action_result is not None:
            row["attempted"] += 1
            if outcome.action_result.succeeded:
                row["succeeded"] += 1
        if outcome.recovered:
            row["recovered"] += 1
        if outcome.decision.action is Action.RETRY_SAME_INSTRUMENT and decline.decline_class is DeclineClass.HARD:
            row["wasted_retries"] += 1

    return list(rows.values())


def _build_audit_rows(comparison: ComparisonReport, cap: int = AUDIT_ROW_CAP) -> list[dict]:
    per_config = {
        "baseline": [dict(to_audit_record(o), config="baseline") for o in comparison.baseline.report.outcomes],
        "guardrailed_baseline": [
            dict(to_audit_record(o), config="guardrailed_baseline") for o in comparison.guardrailed_baseline.report.outcomes
        ],
        "triage": [dict(to_audit_record(o), config="triage") for o in comparison.triage.report.outcomes],
    }
    return _cap_rows_fairly(per_config, cap)


def _audit_meta(comparison: ComparisonReport, shown_count: int) -> dict:
    total_available = (
        len(comparison.baseline.report.outcomes)
        + len(comparison.guardrailed_baseline.report.outcomes)
        + len(comparison.triage.report.outcomes)
    )
    return {"shown": shown_count, "total_available": total_available}


def _cap_rows_fairly(per_config: dict[str, list[dict]], cap: int) -> list[dict]:
    total = sum(len(rows) for rows in per_config.values())
    if total <= cap:
        return [row for rows in per_config.values() for row in rows]

    # Each config gets an even share of whatever budget remains, so a
    # large baseline run can't crowd Triage's own rows out of the log
    # entirely — every filter tab in the UI still has real data to show.
    result: list[dict] = []
    remaining_cap = cap
    keys = list(per_config.keys())
    for index, key in enumerate(keys):
        rows = per_config[key]
        share = remaining_cap // (len(keys) - index)
        take = min(len(rows), share)
        result.extend(rows[:take])
        remaining_cap -= take
    return result


def render_dashboard_html(data: dict, template_path: str | Path | None = None) -> str:
    path = Path(template_path) if template_path is not None else DEFAULT_TEMPLATE_PATH
    template = path.read_text(encoding="utf-8")
    if _PLACEHOLDER not in template:
        raise ValueError(f"template at {path} is missing the {_PLACEHOLDER} placeholder")

    payload = json.dumps(data, default=str)
    # A "</" inside a JSON string value would otherwise terminate the
    # enclosing <script> tag early; this is the standard-safe escape for
    # embedding arbitrary JSON inside inline script content.
    payload = payload.replace("</", "<\\/")
    return template.replace(_PLACEHOLDER, payload)


def write_dashboard(
    html_out_path: str | Path,
    comparison: ComparisonReport,
    *,
    dataset_meta: dict | None = None,
    out_dir: str | Path = "out",
    template_path: str | Path | None = None,
) -> None:
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    comparison_json_path = out_dir_path / "comparison.json"

    data = build_report_data(comparison, dataset_meta=dataset_meta)
    comparison_json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    loaded = json.loads(comparison_json_path.read_text(encoding="utf-8"))
    audit_rows = _build_audit_rows(comparison)
    loaded["audit"] = audit_rows
    loaded["audit_meta"] = _audit_meta(comparison, len(audit_rows))

    html = render_dashboard_html(loaded, template_path=template_path)
    Path(html_out_path).write_text(html, encoding="utf-8")
