"""LLMStrategy — the same Strategy protocol, wrapped in three constraints.

VALIDATED. The model proposes an action name plus its reasoning
(ModelProposal). Before that proposal becomes a Decision it is checked
against two things: does the name match a real Action, and does the
resulting action contradict what the decline taxonomy already says about
this case (retrying a hard decline, retrying an ambiguous decline that
has already failed once, or retrying before the taxonomy's own backoff
has elapsed). A violation is rejected and LLMStrategy falls back to
`fallback.decide(case)` instead — and the rejection is written straight
into the returned Decision's `reason`, prefixed with REJECTED_MARKER, so
it shows up in the audit log exactly like any other decision. That's the
point: "the validator caught the model trying to retry a dead card" has
to be provable from the same trail every other decision leaves, not a
separate log only the model's defenders get to see.

GATED. LLMStrategy carries no special path through the pipeline. It
returns a plain Decision like every other Strategy; guardrails_gate (or
whatever gate the pipeline was given) evaluates it with zero knowledge of
which strategy produced it — the same property pipeline.run() relies on
for the naive/rules comparison applies here without any extra code.

OPTIONAL. Nothing in this module runs at import time that requires the
`anthropic` package or a network connection — AnthropicLLMClient only
imports `anthropic` lazily, inside propose(), the one method that
actually needs it. make_llm_strategy() is the only sanctioned way to get
a live LLMStrategy: it returns None, without constructing anything, when
ANTHROPIC_API_KEY isn't set. Every other module in this project (cli.py
included) must go through that factory rather than importing
AnthropicLLMClient directly, so a missing key means the LLM strategy
simply isn't offered — not a crash.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

from .declines import DeclineClass
from .models import Action, Case, Decision
from .strategy import Strategy

# A dunning decision is a cheap, high-volume classification task, not one
# that benefits from a large model's depth — default to the fast/cheap
# tier and let a caller override it for experimentation.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Prefixes are the recording mechanism: is_rejected_proposal() below is
# the single source of truth for detecting a rejection, so nothing else
# in the project has to duplicate this string.
REJECTED_MARKER = "[llm-rejected]"
ACCEPTED_MARKER = "[llm]"


@dataclass(frozen=True)
class ModelProposal:
    action_name: str  # raw text from the model — NOT guaranteed to name a real Action
    reasoning: str
    discount_percent: int | None


class RejectionReason(str, Enum):
    UNRECOGNIZED_ACTION = "unrecognized_action"
    RETRY_AGAINST_HARD_DECLINE = "retry_against_hard_decline"
    RETRY_AGAINST_UNSWITCHABLE_HARD_DECLINE = "retry_different_instrument_against_unswitchable_hard_decline"
    RETRY_AMBIGUOUS_ALREADY_FAILED = "retry_ambiguous_decline_already_failed_once"
    RETRY_BEFORE_BACKOFF = "retry_before_taxonomy_backoff_elapsed"


def is_rejected_proposal(decision: Decision) -> bool:
    return decision.strategy_name == LLMStrategy.name and decision.reason.startswith(REJECTED_MARKER)


@runtime_checkable
class LLMClient(Protocol):
    def propose(self, case: Case) -> ModelProposal: ...


class LLMStrategy:
    name = "llm_triage"
    description = "An LLM proposes an action with reasoning, validated against the decline taxonomy and gated identically to every other strategy."

    def __init__(self, client: LLMClient, fallback: Strategy) -> None:
        self._client = client
        self._fallback = fallback

    def decide(self, case: Case) -> Decision:
        now = datetime.now(timezone.utc)
        proposal = self._client.propose(case)
        action, rejection = _validate(proposal.action_name, case, now)

        if rejection is not None:
            fallback_decision = self._fallback.decide(case)
            reason = (
                f"{REJECTED_MARKER} ({rejection.value}): model proposed '{proposal.action_name}' "
                f"— {proposal.reasoning!r}. Falling back to {self._fallback.name}: {fallback_decision.reason}"
            )
            return Decision(
                case_id=case.case_id,
                action=fallback_decision.action,
                reason=reason,
                strategy_name=self.name,
                decided_at=now,
                discount_percent=fallback_decision.discount_percent,
            )

        discount_percent = proposal.discount_percent if action is Action.OFFER_DISCOUNT else None
        return Decision(
            case_id=case.case_id,
            action=action,
            reason=f"{ACCEPTED_MARKER} {proposal.reasoning}",
            strategy_name=self.name,
            decided_at=now,
            discount_percent=discount_percent,
        )


def _validate(action_name: str, case: Case, now: datetime) -> tuple[Action, None] | tuple[None, RejectionReason]:
    try:
        action = Action(action_name.strip().lower())
    except ValueError:
        return None, RejectionReason.UNRECOGNIZED_ACTION

    decline = case.decline
    if decline is None:
        return action, None

    if action is Action.RETRY_SAME_INSTRUMENT:
        if decline.decline_class is DeclineClass.HARD:
            return None, RejectionReason.RETRY_AGAINST_HARD_DECLINE
        if decline.decline_class is DeclineClass.AMBIGUOUS and case.event.attempt_number >= 2:
            return None, RejectionReason.RETRY_AMBIGUOUS_ALREADY_FAILED
        if decline.retry_after_hours is not None:
            elapsed_hours = (now - case.event.occurred_at).total_seconds() / 3600
            if elapsed_hours < decline.retry_after_hours:
                return None, RejectionReason.RETRY_BEFORE_BACKOFF

    if action is Action.RETRY_DIFFERENT_INSTRUMENT:
        if decline.decline_class is DeclineClass.HARD and not decline.instrument_switch_may_help:
            return None, RejectionReason.RETRY_AGAINST_UNSWITCHABLE_HARD_DECLINE

    return action, None


class AnthropicLLMClient:
    """Calls the real Anthropic API. Written to spec; not exercised against
    a live model in this project's own test suite, since doing so would
    require a real key, cost real money, and be non-deterministic. Only
    the validation/rejection/fallback machinery around it is tested here
    — that machinery is what actually matters for safety, and it's
    exercised through FakeLLMClient in tests/test_llm_strategy.py instead.
    """

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        self._api_key = api_key
        self._model = model
        self._client = None

    def propose(self, case: Case) -> ModelProposal:
        client = self._ensure_client()
        response = client.messages.create(
            model=self._model,
            max_tokens=512,
            tools=[_PROPOSE_ACTION_TOOL],
            tool_choice={"type": "tool", "name": "propose_action"},
            messages=[{"role": "user", "content": _build_prompt(case)}],
        )
        tool_use = next(block for block in response.content if block.type == "tool_use")
        payload = tool_use.input
        return ModelProposal(
            action_name=str(payload.get("action", "")),
            reasoning=str(payload.get("reasoning", "")),
            discount_percent=payload.get("discount_percent"),
        )

    def _ensure_client(self):
        if self._client is None:
            import anthropic  # imported here, not at module load, so the package is only ever required when actually used

            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client


_PROPOSE_ACTION_TOOL = {
    "name": "propose_action",
    "description": "Propose the dunning action to take for this payment case.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [a.value for a in Action],
                "description": "The action to take.",
            },
            "reasoning": {"type": "string", "description": "Why this action, in one or two sentences."},
            "discount_percent": {
                "type": ["integer", "null"],
                "description": "Discount percent if action is offer_discount, otherwise null.",
            },
        },
        "required": ["action", "reasoning", "discount_percent"],
    },
}


def _build_prompt(case: Case) -> str:
    now = datetime.now(timezone.utc)
    lines = [
        "You are deciding the next dunning action for one payment case.",
        f"Category: {case.event.category.value}",
        f"Amount: {case.event.amount} paise",
        f"Attempt number: {case.event.attempt_number}",
        f"Hours since this event: {(now - case.event.occurred_at).total_seconds() / 3600:.1f}",
    ]
    if case.decline is not None:
        decline = case.decline
        backoff_text = (
            f"{decline.retry_after_hours} hours"
            if decline.retry_after_hours is not None
            else "never — this code should not be retried on the same instrument"
        )
        lines += [
            f"Decline code: {decline.code} ({decline.decline_class.value})",
            f"Taxonomy note: {decline.note}",
            f"Retry-after (same instrument): {backoff_text}",
            f"A different instrument may help: {decline.instrument_switch_may_help}",
        ]
    else:
        lines.append("No decline code is on record for this case.")
    if case.event.mandate_expires_at is not None:
        lines.append(f"Mandate expires at: {case.event.mandate_expires_at.isoformat()}")
    lines.append("Propose exactly one action via the propose_action tool.")
    return "\n".join(lines)


def make_llm_strategy(fallback: Strategy | None = None) -> LLMStrategy | None:
    """The only sanctioned way to obtain a live LLMStrategy.

    Returns None — without constructing LLMStrategy or AnthropicLLMClient
    at all — when ANTHROPIC_API_KEY isn't set. Callers (the CLI included)
    must treat None as "the LLM strategy isn't available" and carry on;
    that's what keeps the rest of the project runnable and demoable
    offline.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    from .rules_strategy import RulesStrategy

    client = AnthropicLLMClient(api_key=api_key)
    return LLMStrategy(client=client, fallback=fallback or RulesStrategy())
