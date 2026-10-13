from typing import Any, Protocol

from pydantic import Field

from jev_game_agent.contracts import Contract, ProbeSample


class DecisionRequest(Contract):
    context: dict[str, Any]
    instructions: str
    criteria: dict[str, dict[str, Any]] = Field(min_length=1)
    input_characters: int = 0


class DecisionAnswer(ProbeSample):
    """Actual provider answer and telemetry; no generated rationale."""


class DecisionClient(Protocol):
    async def decide(self, request: DecisionRequest) -> DecisionAnswer: ...
