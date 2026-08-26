"""Smoke tests for the CLI: each subcommand runs end-to-end without error.

Not a substitute for the module-level tests (pipeline, generator, gate,
etc. are tested there in depth) — this just proves the argparse wiring,
file I/O, and command dispatch actually work together.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src.triage.cli import main


class CliSmokeTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.events_path = str(Path(self._tmpdir) / "events.json")

    def _run(self, argv: list[str]) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = main(argv)
        self.assertEqual(exit_code, 0)
        return buffer.getvalue()

    def test_generate_writes_events_and_prints_a_summary(self):
        output = self._run(["generate", "--count", "50", "--seed", "1", "--out", self.events_path])
        self.assertIn("Wrote 50 events", output)
        self.assertIn("By category", output)

        data = json.loads(Path(self.events_path).read_text())
        self.assertEqual(len(data), 50)

    def test_run_reports_on_a_generated_dataset(self):
        self._run(["generate", "--count", "80", "--seed", "1", "--out", self.events_path])
        output = self._run(["run", "--events", self.events_path, "--strategy", "rules", "--gate", "guardrails"])
        self.assertIn("strategy          triage_rules", output)
        self.assertIn("cases considered", output)

    def test_compare_prints_the_three_config_lift_decomposition(self):
        self._run(["generate", "--count", "80", "--seed", "1", "--out", self.events_path])
        output = self._run(["compare", "--events", self.events_path])
        for label in ("naive + permissive gate", "naive + full guardrails", "rules + full guardrails"):
            self.assertIn(label, output)
        self.assertIn("Guardrails changed recovered amount", output)
        self.assertIn("strategy changed recovered amount", output)
        self.assertIn("Total lift", output)

    def test_dashboard_writes_comparison_json_and_a_self_contained_html_file(self):
        self._run(["generate", "--count", "80", "--seed", "1", "--out", self.events_path])
        dashboard_path = str(Path(self._tmpdir) / "dashboard.html")
        out_dir = str(Path(self._tmpdir) / "out")
        output = self._run(["dashboard", "--events", self.events_path, "--out", dashboard_path, "--out-dir", out_dir])
        self.assertIn(f"Wrote {out_dir}/comparison.json", output)
        self.assertIn(f"Wrote {dashboard_path}", output)
        self.assertIn("naive + permissive gate", output)  # summarize_comparison() also printed to stdout

        comparison_json = Path(out_dir) / "comparison.json"
        self.assertTrue(comparison_json.exists())
        data = json.loads(comparison_json.read_text(encoding="utf-8"))
        self.assertNotIn("audit", data)  # audit is attached only to the HTML's copy, not this artifact

        content = Path(dashboard_path).read_text(encoding="utf-8")
        self.assertTrue(content.strip().lower().startswith("<!doctype html>"))
        self.assertNotIn("http://", content.lower())
        self.assertNotIn("https://", content.lower())
        self.assertIn("const TRIAGE_DATA", content)
        self.assertNotIn("__TRIAGE_DATA__", content)


if __name__ == "__main__":
    unittest.main()
