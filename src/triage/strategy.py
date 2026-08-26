"""The decide stage's shared contract.

Every strategy compared in Triage implements this one Protocol and runs
through the identical detect -> decide -> gate -> act -> measure pipeline.
Structural typing (not inheritance) is deliberate: an LLM-backed strategy,
a rules-based strategy, and the naive baseline all satisfy Strategy the
same way, so nothing about the pipeline needs to know which kind it's
running — that's what keeps the comparison between them fair.

decide() takes only a Case and returns a Decision. No side effects: it
must not touch the gateway, the guardrails, or any case's history: those
belong to the gate and act stages. A strategy can misjudge in `reason`,
but it cannot spend money, send a message, or see anything beyond the
Case the pipeline handed it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Case, Decision


@runtime_checkable
class Strategy(Protocol):
    name: str
    description: str

    def decide(self, case: Case) -> Decision: ...
