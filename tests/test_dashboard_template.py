"""Structural tests against the real dashboard/template.html file.

These don't execute the page's JS (no browser in this environment) — they
check the properties that are verifiable from the source text: hard
constraints (no network references), the exact wiring contract (the
literal "__TRIAGE_DATA__" placeholder as a const value), tag balance, and
that all six views / both theme override blocks / the specific CSS
pitfalls called out when this was commissioned (a reused .tag class name)
are actually present and distinct.

Rendering correctness in an actual browser is not verified here — that
part relies on careful authoring, not automated proof.
"""

from __future__ import annotations

import html.parser
import re
import unittest

from src.triage.dashboard import DEFAULT_TEMPLATE_PATH, build_report_data, render_dashboard_html
from src.triage.compare import ComparisonReport
from tests.test_dashboard import build_fixture_comparison


class _BalanceChecker(html.parser.HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self._VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self._VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"mismatched closing tag </{tag}>, stack was {self.stack}")
            return
        self.stack.pop()


def _assert_balanced(text: str) -> None:
    checker = _BalanceChecker()
    checker.feed(text)
    assert not checker.errors, f"HTML tag imbalance: {checker.errors}"
    assert not checker.stack, f"unclosed tags remain: {checker.stack}"


class TemplateFileExistsTests(unittest.TestCase):
    def test_template_file_exists_at_the_documented_path(self):
        self.assertTrue(DEFAULT_TEMPLATE_PATH.exists(), DEFAULT_TEMPLATE_PATH)

    def test_placeholder_is_the_value_of_a_const_named_triage_data(self):
        source = DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertRegex(source, r'const\s+TRIAGE_DATA\s*=\s*"__TRIAGE_DATA__"\s*;')
        # exactly one occurrence of the placeholder token
        self.assertEqual(source.count("__TRIAGE_DATA__"), 1)


class RawTemplateSelfContainmentTests(unittest.TestCase):
    def setUp(self):
        self.source = DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")

    def test_no_network_references(self):
        lowered = self.source.lower()
        for forbidden in ("http://", "https://", "//fonts.", "cdn.", "<script src", 'href="http', "fetch(", "xmlhttprequest"):
            self.assertNotIn(forbidden, lowered, f"found forbidden reference: {forbidden!r}")

    def test_raw_template_is_tag_balanced(self):
        # the raw template still contains the placeholder token as a JS
        # string literal, which is valid markup on its own
        _assert_balanced(self.source)

    def test_defines_full_light_palette_on_bare_root(self):
        # bare :root block (not inside @media or [data-theme]) must define every token
        root_block = re.search(r":root\{(.*?)\}", self.source, re.DOTALL)
        self.assertIsNotNone(root_block)
        for token in ("--bg", "--text", "--accent", "--good", "--bad", "--border"):
            self.assertIn(token, root_block.group(1))

    def test_dark_override_guarded_by_media_query_and_not_light_selector(self):
        self.assertIn('@media (prefers-color-scheme: dark)', self.source)
        self.assertIn(':root:not([data-theme="light"])', self.source)

    def test_explicit_dark_theme_override_present(self):
        self.assertIn(':root[data-theme="dark"]', self.source)

    def test_prefers_reduced_motion_respected(self):
        self.assertIn("prefers-reduced-motion", self.source)

    def test_focus_visible_defined(self):
        self.assertIn(":focus-visible", self.source)

    def test_tabular_nums_used(self):
        self.assertIn("tabular-nums", self.source)

    def test_wide_tables_scroll_in_their_own_container_not_the_body(self):
        self.assertIn("overflow-x:auto", self.source.replace(" ", ""))
        # body itself must not be given horizontal scroll -- check every
        # rule whose selector list includes bare "body" (not "html,body")
        body_rules = re.findall(r"(?:^|[\s,}])body\s*\{([^}]*)\}", self.source, re.MULTILINE)
        self.assertTrue(body_rules)
        self.assertTrue(
            any("overflow-x:hidden" in rule.replace(" ", "") for rule in body_rules),
            "no body{...} rule sets overflow-x:hidden",
        )

    def test_tag_class_is_never_used(self):
        # the exact pitfall named when this was commissioned: a class
        # called .tag reused for two unrelated purposes. This build uses
        # .pill instead, everywhere, and .tag must not appear at all.
        self.assertNotIn(".tag{", self.source.replace(" ", ""))
        self.assertNotIn('class="tag ', self.source)
        self.assertNotIn('class="tag"', self.source)

    def test_overview_stat_cards_are_real_buttons_not_divs_with_onclick(self):
        self.assertIn("el('button', 'stat-card')", self.source)
        self.assertNotIn("onclick", self.source.lower())

    def test_overview_has_the_four_compact_panels_with_dedicated_navigation_targets(self):
        for target in ("comparison", "funnel", "decline-codes", "decision-log"):
            self.assertIn(f'data-goto="{target}"', self.source)
        for panel_id in ("ov-compare-bars", "ov-funnel", "ov-worst-codes", "ov-gate-blocks"):
            self.assertIn(f'id="{panel_id}"', self.source)

    def test_overview_panels_grid_is_two_column_collapsing_to_one(self):
        compact = self.source.replace(" ", "").replace("\n", "")
        self.assertIn(".overview-panels{display:grid;grid-template-columns:1fr1fr;", compact)
        self.assertIn(".overview-panels{grid-template-columns:1fr;}", compact)

    def test_stat_card_has_hover_focus_and_view_affordance_styling(self):
        self.assertIn(".stat-card:hover{", self.source.replace(" ", ""))
        self.assertIn(".stat-card:focus-visible", self.source.replace(" ", ""))
        self.assertIn("stat-view-link", self.source)

    def test_bar_chart_helper_is_defined_once_not_duplicated(self):
        # it was previously defined once inside renderComparison; it must
        # now be hoisted so Overview's mini panel can reuse it too
        self.assertEqual(self.source.count("function barChart("), 1)

    def test_all_six_views_present(self):
        for view_id in (
            "view-overview", "view-comparison", "view-funnel",
            "view-decline-codes", "view-decision-log", "view-how-it-works",
        ):
            self.assertIn(f'id="{view_id}"', self.source)

    def test_nav_groups_match_the_spec(self):
        for heading in ("Results", "Detail", "Reference"):
            self.assertIn(f'<div class="nav-heading">{heading}</div>', self.source)

    def test_localstorage_calls_are_wrapped_in_try_catch(self):
        # every localStorage.setItem/getItem call site must be inside a try block
        for match in re.finditer(r"localStorage\.(setItem|getItem)", self.source):
            preceding = self.source[max(0, match.start() - 120):match.start()]
            self.assertIn("try", preceding, f"localStorage call at {match.start()} not obviously inside a try block")


class RenderedTemplateWithRealDataTests(unittest.TestCase):
    def setUp(self):
        comparison = build_fixture_comparison()
        data = build_report_data(comparison, dataset_meta={"event_count": 5, "seed": 1})
        data["audit"] = []
        data["audit_meta"] = {"shown": 0, "total_available": 0}
        self.html = render_dashboard_html(data, template_path=DEFAULT_TEMPLATE_PATH)

    def test_rendered_output_is_tag_balanced(self):
        _assert_balanced(self.html)

    def test_placeholder_token_is_gone_after_render(self):
        self.assertNotIn("__TRIAGE_DATA__", self.html)

    def test_injected_headline_amount_appears_in_the_const_assignment(self):
        compact = self.html.replace(" ", "").replace("\n", "")
        self.assertIn('"recovered_amount":80000', compact)

    def test_no_network_references_after_render(self):
        lowered = self.html.lower()
        for forbidden in ("http://", "https://", "<script src", "cdn."):
            self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
