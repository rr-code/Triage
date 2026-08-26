"""Tests for LLMStrategy — same Strategy protocol, wrapped in three constraints:

- VALIDATED: the proposal must name a real Action and must not contradict
  the decline taxonomy (hard-decline retry, a second ambiguous retry, or
  retrying before the taxonomy's backoff). A violation is rejected and
  falls back to the rules decision, and the rejection is RECORDED in the
  returned Decision's reason — that's the "caught the model" demo moment.
- GATED: LLMStrategy is just another Strategy; it carries no bypass and
  goes through the same guardrails_gate as everyone else (already proven
  by the guardrails test suite treating every Decision identically
  regardless of which strategy produced it).
- OPTIONAL: make_llm_strategy() returns None — never constructing
  LLMStrategy at all — when no API key is present.

A FakeLLMClient stands in for AnthropicLLMClient throughout, so nothing
here makes a network call or requires the `anthropic` package.

Written before src/triage/llm_strategy.py exists; written to make these pass.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.triage.declines import classify
from src.triage.llm_strategy import (
    LLMStrategy,
    ModelProposal,
    RejectionReason,
    is_rejected_proposal,
    make_llm_strategy,
)
from src.triage.models import Action, Case, CaseCategory, EventStatus, PaymentEvent
from src.triage.rules_strategy import RulesStrategy
from src.triage.strategy import Strategy

NOW = datetime.now(timezone.utc)


class FakeLLMClient:
    def __init__(self, proposal: ModelProposal):
        self.proposal = proposal
        self.calls = 0

    def propose(self, case: Case) -> ModelProposal:
        self.calls += 1
        return self.proposal


def make_case(
    category=CaseCategory.FAILED_ONE_TIME,
    decline_code=None,
    occurred_at=None,
    attempt_number=1,
    case_id="case-1",
):
    event = PaymentEvent(
        event_id="evt-1",
        occurred_at=occurred_at if occurred_at is not None else NOW,
        customer_id="cust-1",
        payment_id="pay-1",
        instrument_id="instr-1",
        amount=50_000,
        currency="INR",
        category=category,
        decline_code=decline_code,
        status=EventStatus.OPEN,
        attempt_number=attempt_number,
        mandate_expires_at=None,
    )
    decline = classify(decline_code) if decline_code is not None else None
    return Case(case_id=case_id, event=event, decline=decline, recovery_likelihood=0.4, rank=1, detected_at=NOW)


class ProtocolConformanceTests(unittest.TestCase):
    def test_llm_strategy_satisfies_strategy_protocol(self):
        client = FakeLLMClient(ModelProposal(action_name="wait", reasoning="r", discount_percent=None))
        strategy = LLMStrategy(client=client, fallback=RulesStrategy())
        self.assertIsInstance(strategy, Strategy)


class AcceptedProposalTests(unittest.TestCase):
    def test_valid_action_matching_taxonomy_is_accepted(self):
        case = make_case(category=CaseCategory.COLD_PAYMENT_LINK)
        client = FakeLLMClient(ModelProposal(action_name="send_payment_link", reasoning="nudge them", discount_percent=None))
        strategy = LLMStrategy(client=client, fallback=RulesStrategy())
        decision = strategy.decide(case)
        self.assertEqual(decision.action, Action.SEND_PAYMENT_LINK)
        self.assertFalse(is_rejected_proposal(decision))
        self.assertIn("nudge them", decision.reason)
        self.assertEqual(decision.strategy_name, strategy.name)

    def test_offer_discount_carries_the_proposed_percent(self):
        case = make_case(category=CaseCategory.COLD_PAYMENT_LINK, attempt_number=3)
        client = FakeLLMClient(ModelProposal(action_name="offer_discount", reasoning="gone cold twice", discount_percent=10))
        strategy = LLMStrategy(client=client, fallback=RulesStrategy())
        decision = strategy.decide(case)
        self.assertEqual(decision.action, Action.OFFER_DISCOUNT)
        self.assertEqual(decision.discount_percent, 10)


class RejectedProposalTests(unittest.TestCase):
    def test_unrecognized_action_name_is_rejected(self):
        case = make_case()
        client = FakeLLMClient(ModelProposal(action_name="give_them_a_call", reasoning="seems nice", discount_percent=None))
        fallback = RulesStrategy()
        strategy = LLMStrategy(client=client, fallback=fallback)
        decision = strategy.decide(case)
        self.assertTrue(is_rejected_proposal(decision))
        self.assertEqual(decision.action, fallback.decide(case).action)
        self.assertIn("give_them_a_call", decision.reason)

    def test_retry_same_instrument_against_hard_decline_is_rejected(self):
        case = make_case(decline_code="CARD_EXPIRED")
        client = FakeLLMClient(ModelProposal(action_name="retry_same_instrument", reasoning="worth a shot", discount_percent=None))
        fallback = RulesStrategy()
        strategy = LLMStrategy(client=client, fallback=fallback)
        decision = strategy.decide(case)
        self.assertTrue(is_rejected_proposal(decision))
        self.assertIn(RejectionReason.RETRY_AGAINST_HARD_DECLINE.value, decision.reason)
        self.assertEqual(decision.action, fallback.decide(case).action)
        self.assertNotEqual(decision.action, Action.RETRY_SAME_INSTRUMENT)

    def test_retry_same_instrument_against_ambiguous_decline_already_failed_once_is_rejected(self):
        case = make_case(decline_code="DO_NOT_HONOUR", attempt_number=2, occurred_at=NOW - timedelta(hours=10))
        client = FakeLLMClient(ModelProposal(action_name="retry_same_instrument", reasoning="try again", discount_percent=None))
        strategy = LLMStrategy(client=client, fallback=RulesStrategy())
        decision = strategy.decide(case)
        self.assertTrue(is_rejected_proposal(decision))
        self.assertIn(RejectionReason.RETRY_AMBIGUOUS_ALREADY_FAILED.value, decision.reason)

    def test_retry_same_instrument_before_backoff_elapsed_is_rejected(self):
        case = make_case(decline_code="INSUFFICIENT_FUNDS", occurred_at=NOW - timedelta(hours=1))
        client = FakeLLMClient(ModelProposal(action_name="retry_same_instrument", reasoning="try now", discount_percent=None))
        strategy = LLMStrategy(client=client, fallback=RulesStrategy())
        decision = strategy.decide(case)
        self.assertTrue(is_rejected_proposal(decision))
        self.assertIn(RejectionReason.RETRY_BEFORE_BACKOFF.value, decision.reason)

    def test_retry_different_instrument_against_unswitchable_hard_decline_is_rejected(self):
        case = make_case(decline_code="STOLEN_CARD")
        client = FakeLLMClient(
            ModelProposal(action_name="retry_different_instrument", reasoning="try another card", discount_percent=None)
        )
        strategy = LLMStrategy(client=client, fallback=RulesStrategy())
        decision = strategy.decide(case)
        self.assertTrue(is_rejected_proposal(decision))

    def test_valid_action_on_a_soft_decline_after_backoff_is_not_rejected(self):
        # Sanity check that validation isn't over-eager: a same-instrument
        # retry that genuinely obeys the taxonomy must be accepted.
        case = make_case(decline_code="INSUFFICIENT_FUNDS", occurred_at=NOW - timedelta(hours=25))
        client = FakeLLMClient(ModelProposal(action_name="retry_same_instrument", reasoning="backoff has passed", discount_percent=None))
        strategy = LLMStrategy(client=client, fallback=RulesStrategy())
        decision = strategy.decide(case)
        self.assertFalse(is_rejected_proposal(decision))
        self.assertEqual(decision.action, Action.RETRY_SAME_INSTRUMENT)


class OptionalConstructionTests(unittest.TestCase):
    def test_make_llm_strategy_returns_none_without_an_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(make_llm_strategy())

    def test_make_llm_strategy_returns_a_strategy_with_an_api_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True):
            strategy = make_llm_strategy()
        self.assertIsNotNone(strategy)
        self.assertIsInstance(strategy, Strategy)


if __name__ == "__main__":
    unittest.main()
