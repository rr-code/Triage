"""Tests for the PaymentGateway protocol and its mock Razorpay implementation.

The mock's success model is the most load-bearing assumption in the
project, so these tests pin down what makes it credible rather than
rigged: charge() never receives anything strategy-identifying, a hard
decline never succeeds on the same instrument (not just rarely), a new
instrument is judged independently of the old decline, and timing — not
attempt count — is what moves a soft/ambiguous decline's odds.

Written before src/triage/gateway.py and src/triage/mock_gateway.py
exist; written to make these pass.
"""

from __future__ import annotations

import unittest

from src.triage.declines import classify
from src.triage.gateway import PaymentGateway
from src.triage.mock_gateway import MockRazorpayGateway


class ProtocolConformanceTests(unittest.TestCase):
    def test_mock_gateway_satisfies_payment_gateway_protocol(self):
        self.assertIsInstance(MockRazorpayGateway(), PaymentGateway)


class HardDeclineNeverSucceedsTests(unittest.TestCase):
    def test_hard_decline_same_instrument_never_succeeds(self):
        gateway = MockRazorpayGateway(seed=1)
        decline = classify("CARD_EXPIRED")
        results = [
            gateway.charge(decline=decline, hours_since_decline=h, is_new_instrument=False, amount=50_000).succeeded
            for h in range(0, 500, 5)
        ]
        self.assertFalse(any(results))

    def test_hard_decline_new_instrument_can_succeed(self):
        gateway = MockRazorpayGateway(seed=1)
        decline = classify("CARD_EXPIRED")
        results = [
            gateway.charge(decline=decline, hours_since_decline=0, is_new_instrument=True, amount=50_000).succeeded
            for _ in range(200)
        ]
        self.assertIn(True, results)
        self.assertIn(False, results)  # genuinely probabilistic, not a hidden always-succeed


class NewInstrumentIndependenceTests(unittest.TestCase):
    def test_new_instrument_outcome_ignores_the_decline_entirely(self):
        gw_hard = MockRazorpayGateway(seed=5)
        gw_soft = MockRazorpayGateway(seed=5)
        hard = classify("STOLEN_CARD")
        soft = classify("INSUFFICIENT_FUNDS")
        outcome_hard = gw_hard.charge(decline=hard, hours_since_decline=0, is_new_instrument=True, amount=1_000)
        outcome_soft = gw_soft.charge(decline=soft, hours_since_decline=0, is_new_instrument=True, amount=1_000)
        self.assertEqual(outcome_hard.succeeded, outcome_soft.succeeded)


class TimingMattersTests(unittest.TestCase):
    def test_soft_decline_succeeds_more_often_after_backoff_than_before(self):
        decline = classify("INSUFFICIENT_FUNDS")  # 24h backoff
        trials = 500
        pre_gateway = MockRazorpayGateway(seed=42)
        post_gateway = MockRazorpayGateway(seed=42)
        pre_successes = sum(
            pre_gateway.charge(
                decline=decline, hours_since_decline=1, is_new_instrument=False, amount=50_000
            ).succeeded
            for _ in range(trials)
        )
        post_successes = sum(
            post_gateway.charge(
                decline=decline, hours_since_decline=48, is_new_instrument=False, amount=50_000
            ).succeeded
            for _ in range(trials)
        )
        self.assertGreater(post_successes, pre_successes)

    def test_ambiguous_decline_before_backoff_can_still_occasionally_succeed(self):
        # Rare, not impossible: DO_NOT_HONOUR retried at 1h (backoff is 6h).
        decline = classify("DO_NOT_HONOUR")
        gateway = MockRazorpayGateway(seed=3)
        results = [
            gateway.charge(decline=decline, hours_since_decline=1, is_new_instrument=False, amount=50_000).succeeded
            for _ in range(300)
        ]
        self.assertIn(True, results)


class UnknownDeclineTests(unittest.TestCase):
    def test_same_instrument_retry_with_no_decline_code_is_genuinely_probabilistic(self):
        gateway = MockRazorpayGateway(seed=2)
        results = [
            gateway.charge(decline=None, hours_since_decline=0, is_new_instrument=False, amount=50_000).succeeded
            for _ in range(200)
        ]
        self.assertIn(True, results)
        self.assertIn(False, results)


class ReproducibilityTests(unittest.TestCase):
    def test_same_seed_reproduces_the_same_sequence_of_outcomes(self):
        decline = classify("DO_NOT_HONOUR")

        def run(seed):
            gateway = MockRazorpayGateway(seed=seed)
            return [
                gateway.charge(
                    decline=decline, hours_since_decline=h, is_new_instrument=False, amount=50_000
                ).succeeded
                for h in (1, 3, 5, 7, 10, 20)
            ]

        self.assertEqual(run(99), run(99))

    def test_default_seed_is_deterministic_across_instances(self):
        decline = classify("INSUFFICIENT_FUNDS")
        gw1 = MockRazorpayGateway()
        gw2 = MockRazorpayGateway()
        outcome1 = gw1.charge(decline=decline, hours_since_decline=48, is_new_instrument=False, amount=50_000)
        outcome2 = gw2.charge(decline=decline, hours_since_decline=48, is_new_instrument=False, amount=50_000)
        self.assertEqual(outcome1.succeeded, outcome2.succeeded)


class ChargeOutcomeShapeTests(unittest.TestCase):
    def test_charge_outcome_echoes_the_requested_amount(self):
        gateway = MockRazorpayGateway(seed=1)
        decline = classify("INSUFFICIENT_FUNDS")
        outcome = gateway.charge(decline=decline, hours_since_decline=48, is_new_instrument=False, amount=12_345)
        self.assertEqual(outcome.amount, 12_345)

    def test_outcome_carries_a_reference_and_detail(self):
        gateway = MockRazorpayGateway(seed=1)
        outcome = gateway.charge(decline=None, hours_since_decline=0, is_new_instrument=True, amount=1_000)
        self.assertTrue(outcome.reference)
        self.assertTrue(outcome.detail)


if __name__ == "__main__":
    unittest.main()
