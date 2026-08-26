"""Tests for the detect stage.

detect() filters raw PaymentEvents down to Cases worth acting on and ranks
them by expected value. It takes only events and the current time — never
a strategy — so every strategy compared later runs over an identical,
reproducible candidate set. Nothing is silently dropped: anything excluded
comes back as a SkipRecord with a reason, so cases + skips always account
for every input event.

Written before src/triage/detect.py exists; it is written to make these pass.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.triage.config import (
    MANDATE_EXPIRY_IMMINENT_HOURS,
    MIN_CASE_VALUE_PAISE,
    RECOVERY_WINDOW_HOURS,
)
from src.triage.detect import detect
from src.triage.models import CaseCategory, EventStatus, PaymentEvent, SkipReason

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def make_event(
    event_id="evt-1",
    occurred_at=None,
    customer_id="cust-1",
    payment_id="pay-1",
    instrument_id="instr-1",
    amount=50_000,
    currency="INR",
    category=CaseCategory.FAILED_ONE_TIME,
    decline_code=None,
    status=EventStatus.OPEN,
    attempt_number=1,
    mandate_expires_at=None,
):
    return PaymentEvent(
        event_id=event_id,
        occurred_at=occurred_at if occurred_at is not None else NOW,
        customer_id=customer_id,
        payment_id=payment_id,
        instrument_id=instrument_id,
        amount=amount,
        currency=currency,
        category=category,
        decline_code=decline_code,
        status=status,
        attempt_number=attempt_number,
        mandate_expires_at=mandate_expires_at,
    )


class ResolvedAndDisputedTests(unittest.TestCase):
    def test_resolved_event_is_excluded(self):
        cases, skipped = detect([make_event(status=EventStatus.RESOLVED)], now=NOW)
        self.assertEqual(cases, [])
        self.assertEqual(skipped[0].reason, SkipReason.RESOLVED)

    def test_disputed_event_is_excluded(self):
        cases, skipped = detect([make_event(status=EventStatus.DISPUTED)], now=NOW)
        self.assertEqual(cases, [])
        self.assertEqual(skipped[0].reason, SkipReason.DISPUTED)

    def test_open_event_is_included(self):
        cases, skipped = detect([make_event(status=EventStatus.OPEN)], now=NOW)
        self.assertEqual(len(cases), 1)
        self.assertEqual(skipped, [])


class ValueFloorTests(unittest.TestCase):
    def test_below_floor_is_excluded(self):
        cases, skipped = detect([make_event(amount=MIN_CASE_VALUE_PAISE - 1)], now=NOW)
        self.assertEqual(cases, [])
        self.assertEqual(skipped[0].reason, SkipReason.BELOW_VALUE_FLOOR)

    def test_at_floor_is_included(self):
        cases, _ = detect([make_event(amount=MIN_CASE_VALUE_PAISE)], now=NOW)
        self.assertEqual(len(cases), 1)


class StalenessTests(unittest.TestCase):
    def test_event_past_category_window_is_excluded(self):
        window = RECOVERY_WINDOW_HOURS[CaseCategory.FAILED_ONE_TIME]
        event = make_event(occurred_at=NOW - timedelta(hours=window + 1))
        cases, skipped = detect([event], now=NOW)
        self.assertEqual(cases, [])
        self.assertEqual(skipped[0].reason, SkipReason.STALE)

    def test_event_inside_category_window_is_included(self):
        window = RECOVERY_WINDOW_HOURS[CaseCategory.FAILED_ONE_TIME]
        event = make_event(occurred_at=NOW - timedelta(hours=window - 1))
        cases, _ = detect([event], now=NOW)
        self.assertEqual(len(cases), 1)


class ExpiringMandateTests(unittest.TestCase):
    def test_expiry_far_in_the_future_is_excluded(self):
        event = make_event(
            category=CaseCategory.EXPIRING_MANDATE,
            mandate_expires_at=NOW + timedelta(hours=MANDATE_EXPIRY_IMMINENT_HOURS + 1),
        )
        cases, skipped = detect([event], now=NOW)
        self.assertEqual(cases, [])
        self.assertEqual(skipped[0].reason, SkipReason.MANDATE_NOT_IMMINENT)

    def test_expiry_already_passed_is_excluded(self):
        event = make_event(
            category=CaseCategory.EXPIRING_MANDATE,
            mandate_expires_at=NOW - timedelta(hours=1),
        )
        cases, skipped = detect([event], now=NOW)
        self.assertEqual(cases, [])
        self.assertEqual(skipped[0].reason, SkipReason.MANDATE_EXPIRED)

    def test_imminent_unexpired_mandate_is_included(self):
        event = make_event(
            category=CaseCategory.EXPIRING_MANDATE,
            mandate_expires_at=NOW + timedelta(hours=MANDATE_EXPIRY_IMMINENT_HOURS - 1),
        )
        cases, skipped = detect([event], now=NOW)
        self.assertEqual(len(cases), 1)
        self.assertEqual(skipped, [])

    def test_missing_expiry_is_excluded(self):
        event = make_event(category=CaseCategory.EXPIRING_MANDATE, mandate_expires_at=None)
        cases, skipped = detect([event], now=NOW)
        self.assertEqual(cases, [])
        self.assertEqual(skipped[0].reason, SkipReason.MANDATE_NOT_IMMINENT)


class HardDeclineTests(unittest.TestCase):
    def test_hard_decline_with_instrument_switch_stays_detected_at_reduced_likelihood(self):
        soft_cases, _ = detect([make_event(decline_code="INSUFFICIENT_FUNDS")], now=NOW)
        hard_cases, hard_skipped = detect([make_event(decline_code="CARD_EXPIRED")], now=NOW)

        self.assertEqual(len(hard_cases), 1)
        self.assertEqual(hard_skipped, [])
        self.assertLess(hard_cases[0].recovery_likelihood, soft_cases[0].recovery_likelihood)

    def test_hard_decline_with_no_instrument_switch_is_dropped(self):
        cases, skipped = detect([make_event(decline_code="STOLEN_CARD")], now=NOW)
        self.assertEqual(cases, [])
        self.assertEqual(skipped[0].reason, SkipReason.HARD_DECLINE_NO_INSTRUMENT_SWITCH)


class RankingTests(unittest.TestCase):
    def test_cases_are_ranked_by_expected_value_descending(self):
        low_value = make_event(
            event_id="evt-low", amount=MIN_CASE_VALUE_PAISE, category=CaseCategory.COLD_PAYMENT_LINK
        )
        high_value = make_event(
            event_id="evt-high", amount=1_000_000, category=CaseCategory.FAILED_AUTOPAY
        )

        cases, _ = detect([low_value, high_value], now=NOW)

        self.assertEqual([c.event.event_id for c in cases], ["evt-high", "evt-low"])
        self.assertEqual(cases[0].rank, 1)
        self.assertEqual(cases[1].rank, 2)


class AccountingTests(unittest.TestCase):
    def test_every_event_is_accounted_for_as_case_or_skip(self):
        events = [
            make_event(event_id="evt-1", status=EventStatus.RESOLVED),
            make_event(event_id="evt-2", amount=MIN_CASE_VALUE_PAISE - 1),
            make_event(event_id="evt-3"),
        ]
        cases, skipped = detect(events, now=NOW)
        accounted = {c.event.event_id for c in cases} | {s.event.event_id for s in skipped}
        self.assertEqual(accounted, {e.event_id for e in events})
        self.assertEqual(len(cases) + len(skipped), len(events))


class StrategyAgnosticTests(unittest.TestCase):
    def test_detect_is_deterministic_given_only_events_and_now(self):
        events = [
            make_event(event_id="evt-a", amount=100_000),
            make_event(event_id="evt-b", amount=200_000),
        ]
        cases_1, skipped_1 = detect(events, now=NOW)
        cases_2, skipped_2 = detect(events, now=NOW)
        self.assertEqual(cases_1, cases_2)
        self.assertEqual(skipped_1, skipped_2)


if __name__ == "__main__":
    unittest.main()
