"""Tests for the dashboard data layer and the template-injection mechanics.

Two layers, tested separately:

- build_report_data() / the audit-row builder are tested against a
  hand-built ComparisonReport with known outcomes, so every number is
  independently hand-verified arithmetic.
- render_dashboard_html() is tested against a throwaway fake template (not
  the real dashboard/template.html) so its mechanics — literal
  "__TRIAGE_DATA__" replacement, </script> escaping, a clear error when
  the placeholder is missing — are isolated from the real template's
  content. A separate suite (test_dashboard_template.py) exercises the
  real template file once it exists.

Written before src/triage/dashboard.py's new version exists; written to
make these pass.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.triage.compare import ComparisonReport, ConfigResult
from src.triage.config import (
    MAX_CUSTOMER_CONTACTS_PER_CASE,
    MAX_DISCOUNT_PERCENT,
    MAX_TOTAL_ATTEMPTS_PER_CASE,
    MIN_HOURS_BETWEEN_CONTACTS,
)
from src.triage.dashboard import build_report_data, render_dashboard_html, write_dashboard
from src.triage.declines import classify
from src.triage.measure import measure
from src.triage.metrics import compute_metrics
from src.triage.models import (
    Action,
    ActionResult,
    Case,
    CaseCategory,
    CaseOutcome,
    Decision,
    EventStatus,
    GateResult,
    PaymentEvent,
)

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def make_outcome(
    case_id,
    category,
    decline_code,
    action,
    *,
    approved=True,
    succeeded=True,
    amount_recovered=0,
    amount=50_000,
    blocking_rule=None,
    reason="test reason",
):
    event = PaymentEvent(
        event_id=case_id, occurred_at=NOW, customer_id="cust", payment_id="pay", instrument_id="instr",
        amount=amount, currency="INR", category=category, decline_code=decline_code,
        status=EventStatus.OPEN, attempt_number=1, mandate_expires_at=None,
    )
    decline = classify(decline_code) if decline_code else None
    case = Case(case_id=case_id, event=event, decline=decline, recovery_likelihood=0.4, rank=1, detected_at=NOW)
    decision = Decision(case_id=case_id, action=action, reason=reason, strategy_name="s", decided_at=NOW, discount_percent=None)
    gate_result = GateResult(
        decision=decision, approved=approved,
        blocking_rule=blocking_rule if not approved else None,
        blocked_reason="blocked" if not approved else None,
        evaluated_at=NOW,
    )
    action_result = None
    if approved:
        action_result = ActionResult(case_id=case_id, action=action, succeeded=succeeded, amount_recovered=amount_recovered, detail="d", executed_at=NOW)
    recovered = approved and succeeded and amount_recovered > 0
    return CaseOutcome(
        case=case, decision=decision, gate_result=gate_result, action_result=action_result,
        recovered=recovered, recovered_amount=amount_recovered if recovered else 0,
    )


def make_config_result(label, strategy_name, gate_name, outcomes):
    report = measure(list(outcomes), run_id="r-" + strategy_name, strategy_name=strategy_name, started_at=NOW, finished_at=NOW)
    metrics = compute_metrics(list(outcomes), [])
    return ConfigResult(label=label, strategy_name=strategy_name, gate_name=gate_name, report=report, metrics=metrics)


def build_fixture_comparison():
    triage_outcomes = [
        make_outcome("c1", CaseCategory.FAILED_ONE_TIME, "INSUFFICIENT_FUNDS", Action.RETRY_SAME_INSTRUMENT, succeeded=True, amount_recovered=50_000, amount=50_000),
        make_outcome("c2", CaseCategory.FAILED_ONE_TIME, "CARD_EXPIRED", Action.RETRY_DIFFERENT_INSTRUMENT, succeeded=True, amount_recovered=30_000, amount=30_000),
        make_outcome("c3", CaseCategory.FAILED_ONE_TIME, "CARD_EXPIRED", Action.ESCALATE_TO_HUMAN, succeeded=True, amount_recovered=0, amount=20_000),
        make_outcome("c4", CaseCategory.COLD_PAYMENT_LINK, None, Action.SEND_PAYMENT_LINK, succeeded=True, amount_recovered=0, amount=10_000),
    ]
    triage = make_config_result("rules + full guardrails", "triage_rules", "guardrails", triage_outcomes)

    guardrailed_baseline_outcomes = [
        make_outcome("b1", CaseCategory.FAILED_ONE_TIME, "CARD_EXPIRED", Action.RETRY_SAME_INSTRUMENT, approved=False, blocking_rule="never_retry_hard_decline", amount=40_000),
        make_outcome("b2", CaseCategory.FAILED_ONE_TIME, "INSUFFICIENT_FUNDS", Action.RETRY_SAME_INSTRUMENT, succeeded=True, amount_recovered=15_000, amount=15_000),
    ]
    guardrailed_baseline = make_config_result("naive + full guardrails", "naive_retry_everything", "guardrails", guardrailed_baseline_outcomes)

    baseline_outcomes = [
        make_outcome("p1", CaseCategory.FAILED_ONE_TIME, "CARD_EXPIRED", Action.RETRY_SAME_INSTRUMENT, succeeded=False, amount=40_000),
    ]
    baseline = make_config_result("naive + permissive gate", "naive_retry_everything", "permissive", baseline_outcomes)

    return ComparisonReport(baseline=baseline, guardrailed_baseline=guardrailed_baseline, triage=triage)


class BuildReportDataTests(unittest.TestCase):
    def setUp(self):
        self.comparison = build_fixture_comparison()
        self.data = build_report_data(self.comparison, dataset_meta={"event_count": 5, "seed": 1})

    def test_headline_figures(self):
        h = self.data["headline"]
        self.assertEqual(h["recovered_amount"], 80_000)
        self.assertEqual(h["baseline_recovered_amount"], 0)
        self.assertIsNone(h["pct_vs_baseline"])  # baseline recovered 0 -> undefined percentage, must not guess
        self.assertEqual(h["wasted_retries"], 0)
        self.assertEqual(h["customer_contacts"], 1)
        self.assertEqual(h["exceptions_logged"], 2)  # c3 declined_by_strategy, c4 attempted_and_failed (no skips in this fixture)

    def test_pct_vs_baseline_is_computed_when_baseline_nonzero(self):
        comparison = build_fixture_comparison()
        # give baseline a nonzero recovered amount by swapping in a config where p1 succeeded
        outcomes = [
            make_outcome("p1", CaseCategory.FAILED_ONE_TIME, "CARD_EXPIRED", Action.RETRY_SAME_INSTRUMENT, succeeded=True, amount_recovered=40_000, amount=40_000)
        ]
        baseline = make_config_result("naive + permissive gate", "naive_retry_everything", "permissive", outcomes)
        comparison = ComparisonReport(baseline=baseline, guardrailed_baseline=comparison.guardrailed_baseline, triage=comparison.triage)
        data = build_report_data(comparison, dataset_meta={"event_count": 5, "seed": 1})
        # triage recovered 80_000, baseline recovered 40_000 -> +100%
        self.assertAlmostEqual(data["headline"]["pct_vs_baseline"], 1.0)

    def test_comparison_configs_carry_stable_keys_and_dead_instrument_retries(self):
        configs = self.data["comparison"]["configs"]
        keys = [c["key"] for c in configs]
        self.assertEqual(keys, ["baseline", "guardrailed_baseline", "triage"])
        # guardrailed_baseline had b1: RETRY_SAME_INSTRUMENT against CARD_EXPIRED (hard), blocked -> still counted as proposed
        guardrailed = configs[1]
        self.assertEqual(guardrailed["dead_instrument_retries"], 1)
        self.assertEqual(configs[2]["dead_instrument_retries"], 0)

    def test_lift_decomposition(self):
        comp = self.data["comparison"]
        self.assertEqual(comp["guardrails_lift"], 15_000)
        self.assertEqual(comp["strategy_lift"], 65_000)
        self.assertEqual(comp["total_lift"], 80_000)

    def test_funnel_and_its_exception_breakdown(self):
        funnel = self.data["funnel"]
        self.assertEqual(funnel["scanned"], 4)
        self.assertEqual(funnel["detected"], 4)
        self.assertEqual(funnel["actioned"], 4)
        self.assertEqual(funnel["recovered"], 2)
        self.assertEqual(funnel["exceptions"]["declined_by_strategy"], 1)
        self.assertEqual(funnel["exceptions"]["attempted_and_failed"], 1)

    def test_decline_codes_table_includes_every_taxonomy_code_even_at_zero(self):
        codes = {row["code"]: row for row in self.data["decline_codes"]}
        self.assertIn("STOLEN_CARD", codes)  # never appeared in this fixture -- must still be present at zero
        self.assertEqual(codes["STOLEN_CARD"]["cases"], 0)
        self.assertEqual(codes["STOLEN_CARD"]["recovered_amount"], 0)
        self.assertTrue(codes["STOLEN_CARD"]["note"])  # human-readable description present

    def test_decline_codes_table_counts_correctly(self):
        codes = {row["code"]: row for row in self.data["decline_codes"]}
        soft = codes["INSUFFICIENT_FUNDS"]
        self.assertEqual(soft["cases"], 1)
        self.assertEqual(soft["attempted"], 1)
        self.assertEqual(soft["succeeded"], 1)
        self.assertEqual(soft["recovered"], 1)
        self.assertEqual(soft["recovered_amount"], 50_000)  # c1
        self.assertEqual(soft["wasted_retries"], 0)

        hard = codes["CARD_EXPIRED"]
        self.assertEqual(hard["cases"], 2)  # c2, c3
        self.assertEqual(hard["attempted"], 1)  # only c2 was a retry action (different instrument)
        self.assertEqual(hard["succeeded"], 1)
        self.assertEqual(hard["recovered"], 1)
        self.assertEqual(hard["recovered_amount"], 30_000)  # c2 only -- c3 (escalated) recovered nothing
        self.assertEqual(hard["wasted_retries"], 0)  # neither used retry_same_instrument

    def test_decline_codes_recovered_amount_is_exact_not_derived_from_a_capped_sample(self):
        # The whole point of computing this in Python over the full outcome
        # list: it must be correct regardless of any downstream audit cap.
        codes = {row["code"]: row for row in self.data["decline_codes"]}
        total_recovered_amount = sum(row["recovered_amount"] for row in codes.values())
        self.assertEqual(total_recovered_amount, self.data["headline"]["recovered_amount"])

    def test_policy_block_matches_config_constants(self):
        policy = self.data["policy"]
        self.assertEqual(policy["max_customer_contacts_per_case"], MAX_CUSTOMER_CONTACTS_PER_CASE)
        self.assertEqual(policy["min_hours_between_contacts"], MIN_HOURS_BETWEEN_CONTACTS)
        self.assertEqual(policy["max_total_attempts_per_case"], MAX_TOTAL_ATTEMPTS_PER_CASE)
        self.assertEqual(policy["max_discount_percent"], MAX_DISCOUNT_PERCENT)

    def test_no_audit_key_present_before_it_is_attached(self):
        self.assertNotIn("audit", self.data)


class RenderMechanicsTests(unittest.TestCase):
    """Uses a throwaway fake template so these tests are independent of the real one."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.template_path = Path(self.tmpdir) / "fake_template.html"

    def test_replaces_the_literal_placeholder_with_json(self):
        self.template_path.write_text('<html><script>const TRIAGE_DATA = "__TRIAGE_DATA__";</script></html>', encoding="utf-8")
        html = render_dashboard_html({"a": 1}, template_path=self.template_path)
        self.assertIn('const TRIAGE_DATA = {"a": 1};', html)
        self.assertNotIn("__TRIAGE_DATA__", html)

    def test_raises_a_clear_error_when_placeholder_is_missing(self):
        self.template_path.write_text("<html>no placeholder here</html>", encoding="utf-8")
        with self.assertRaises(ValueError):
            render_dashboard_html({"a": 1}, template_path=self.template_path)

    def test_escapes_forward_slash_after_less_than_so_script_tag_cannot_break_out(self):
        self.template_path.write_text('<script>const TRIAGE_DATA = "__TRIAGE_DATA__";</script>', encoding="utf-8")
        html = render_dashboard_html({"reason": "</script><script>alert(1)</script>"}, template_path=self.template_path)
        self.assertNotIn("<script>alert(1)</script>", html)
        # round-trips back through JSON correctly
        start = html.index("{")
        end = html.rindex("}") + 1
        parsed = json.loads(html[start:end])
        self.assertEqual(parsed["reason"], "</script><script>alert(1)</script>")


class WriteDashboardTests(unittest.TestCase):
    def test_writes_comparison_json_and_html_with_capped_tagged_audit(self):
        comparison = build_fixture_comparison()
        tmpdir = tempfile.mkdtemp()
        html_path = str(Path(tmpdir) / "dashboard.html")
        out_dir = str(Path(tmpdir) / "out")
        fake_template = Path(tmpdir) / "template.html"
        fake_template.write_text('<script>const TRIAGE_DATA = "__TRIAGE_DATA__";</script>', encoding="utf-8")

        write_dashboard(
            html_path, comparison, dataset_meta={"event_count": 5, "seed": 1}, out_dir=out_dir, template_path=fake_template
        )

        comparison_json_path = Path(out_dir) / "comparison.json"
        self.assertTrue(comparison_json_path.exists())
        written = json.loads(comparison_json_path.read_text(encoding="utf-8"))
        self.assertNotIn("audit", written)  # comparison.json itself has no audit array

        html = Path(html_path).read_text(encoding="utf-8")
        start = html.index("{")
        end = html.rindex("}") + 1
        injected = json.loads(html[start:end])
        self.assertIn("audit", injected)
        configs_present = {row["config"] for row in injected["audit"]}
        self.assertEqual(configs_present, {"baseline", "guardrailed_baseline", "triage"})
        self.assertEqual(len(injected["audit"]), 4 + 2 + 1)  # triage(4) + guardrailed_baseline(2) + baseline(1)

        self.assertEqual(injected["audit_meta"]["shown"], 7)
        self.assertEqual(injected["audit_meta"]["total_available"], 7)


if __name__ == "__main__":
    unittest.main()
