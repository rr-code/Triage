"""Tests for the synthetic PaymentEvent generator.

Focus areas: reproducibility, that the named distribution constants
actually drive the output (not just document a coincidence), that
DO_NOT_HONOUR really is the single most common decline code, and that the
"noise" events are genuinely excludable — detect() must actually skip
them for the reasons the generator claims, not just by construction.

Written before src/triage/generator.py exists; written to make these pass.
"""

from __future__ import annotations

import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.triage import generator
from src.triage.config import MANDATE_EXPIRY_IMMINENT_HOURS, MIN_CASE_VALUE_PAISE
from src.triage.detect import detect
from src.triage.generator import generate_events
from src.triage.models import CaseCategory, EventStatus, SkipReason

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


class ReproducibilityTests(unittest.TestCase):
    def test_same_seed_produces_the_same_events(self):
        events_a = generate_events(300, seed=7, now=NOW)
        events_b = generate_events(300, seed=7, now=NOW)
        self.assertEqual(events_a, events_b)

    def test_different_seeds_produce_different_events(self):
        events_a = generate_events(300, seed=7, now=NOW)
        events_b = generate_events(300, seed=8, now=NOW)
        self.assertNotEqual(events_a, events_b)


class CategoryDistributionTests(unittest.TestCase):
    def test_category_shares_are_within_tolerance_of_configured_weights(self):
        events = generate_events(6000, seed=1, now=NOW)
        counts = Counter(e.category for e in events)
        for category, weight in generator.CATEGORY_WEIGHTS.items():
            share = counts[category] / len(events)
            self.assertAlmostEqual(share, weight, delta=0.03)


class DeclineCodeDistributionTests(unittest.TestCase):
    def test_do_not_honour_is_the_single_most_common_decline_code(self):
        events = generate_events(6000, seed=1, now=NOW)
        decline_events = [e for e in events if e.decline_code is not None]
        counts = Counter(e.decline_code for e in decline_events)
        most_common_code, _ = counts.most_common(1)[0]
        self.assertEqual(most_common_code, "DO_NOT_HONOUR")

    def test_hard_decline_share_scales_with_the_named_constant(self):
        # Proves HARD_DECLINE_SHARE actually drives the weights, rather
        # than the weights merely happening to sum to it.
        with patch.object(generator, "HARD_DECLINE_SHARE", 0.10):
            low_weights = generator._decline_code_weights()
        with patch.object(generator, "HARD_DECLINE_SHARE", 0.50):
            high_weights = generator._decline_code_weights()

        low_hard_total = sum(low_weights[c] for c in generator.HARD_CODE_RELATIVE_WEIGHTS)
        high_hard_total = sum(high_weights[c] for c in generator.HARD_CODE_RELATIVE_WEIGHTS)
        self.assertAlmostEqual(low_hard_total, 0.10, places=6)
        self.assertAlmostEqual(high_hard_total, 0.50, places=6)

    def test_only_failure_categories_carry_a_decline_code(self):
        events = generate_events(2000, seed=2, now=NOW)
        for event in events:
            if event.category in (CaseCategory.COLD_PAYMENT_LINK, CaseCategory.EXPIRING_MANDATE):
                self.assertIsNone(event.decline_code)
            elif event.category in (CaseCategory.FAILED_AUTOPAY, CaseCategory.FAILED_ONE_TIME):
                self.assertIsNotNone(event.decline_code)


class NoiseIsGenuinelyUnrecoverableTests(unittest.TestCase):
    def test_below_value_floor_events_exist_and_are_actually_below_it(self):
        events = generate_events(3000, seed=3, now=NOW)
        below_floor = [e for e in events if e.amount < MIN_CASE_VALUE_PAISE]
        self.assertGreater(len(below_floor), 0)

    def test_detect_actually_skips_every_kind_of_noise_this_generator_claims_to_produce(self):
        events = generate_events(3000, seed=4, now=NOW)
        _, skipped = detect(events, now=NOW)
        reasons = {record.reason for record in skipped}
        for expected in (
            SkipReason.RESOLVED,
            SkipReason.DISPUTED,
            SkipReason.STALE,
            SkipReason.BELOW_VALUE_FLOOR,
        ):
            self.assertIn(expected, reasons)

    def test_every_event_is_accounted_for_by_detect(self):
        events = generate_events(1000, seed=5, now=NOW)
        cases, skipped = detect(events, now=NOW)
        self.assertEqual(len(cases) + len(skipped), len(events))


class ExpiringMandateShapeTests(unittest.TestCase):
    def test_mandate_events_have_an_imminent_unexpired_expiry(self):
        events = generate_events(2000, seed=6, now=NOW)
        mandate_events = [e for e in events if e.category is CaseCategory.EXPIRING_MANDATE]
        self.assertGreater(len(mandate_events), 0)
        for event in mandate_events:
            self.assertIsNotNone(event.mandate_expires_at)
            self.assertGreater(event.mandate_expires_at, NOW)
            self.assertLessEqual(event.mandate_expires_at, NOW + timedelta(hours=MANDATE_EXPIRY_IMMINENT_HOURS))


class SummaryTests(unittest.TestCase):
    def test_summarize_mentions_every_category_and_the_total_count(self):
        events = generate_events(200, seed=1, now=NOW)
        text = generator.summarize(events, NOW)
        self.assertIn("200", text)
        for category in CaseCategory:
            self.assertIn(category.value, text)


if __name__ == "__main__":
    unittest.main()
