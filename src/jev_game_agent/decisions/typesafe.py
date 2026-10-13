import asyncio
import errno
import math
import socket
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx2
from typesafe_sdk import AsyncTypeSafeClient, Choice, RetryPolicy
from typesafe_sdk import (
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeError,
)

from jev_game_agent.config import Settings
from jev_game_agent.contracts import ProbeError, ProbeSample, TextCase
from jev_game_agent.decisions.base import DecisionAnswer, DecisionRequest


def retry_after_seconds(headers: dict | None) -> float | None:
    if not headers:
        return None
    lowered = {key.lower(): value for key, value in headers.items()}
    try:
        if "retry-after-ms" in lowered:
            value = float(lowered["retry-after-ms"]) / 1000
        elif "retry-after" in lowered:
            raw = lowered["retry-after"]
            try:
                value = float(raw)
            except ValueError:
                date = parsedate_to_datetime(raw)
                value = (date - datetime.now(timezone.utc)).total_seconds()
        else:
            return None
        return max(0.0, value) if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def classify_error(exc: Exception) -> ProbeError:
    """Return safe metadata only: SDK bodies/messages can contain credentials."""
    if isinstance(exc, TypeSafeAPIResponseValidationError):
        return ProbeError(category="response_invalid")
    if isinstance(exc, TypeSafeAPIError):
        status = exc.status
        category = (
            "authentication" if status == 401 else
            "permission_denied" if status == 403 else
            "request_format" if status in {400, 404, 422} else
            "rate_limit" if status == 429 else
            "service" if 500 <= status <= 599 else "request_format"
        )
        return ProbeError(
            category=category,
            retryable=status == 429 or 500 <= status <= 599,
            http_status=status,
            retry_after_seconds=retry_after_seconds(exc.headers),
        )
    if isinstance(exc, (TimeoutError, TypeSafeAPITimeoutError, httpx2.TimeoutException)):
        return ProbeError(category="timeout", retryable=True)
    # Inspect the exception chain's typed causes, without exposing their text.
    causes: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and not any(current is old for old in causes):
        causes.append(current)
        current = current.__cause__ or current.__context__
    for cause in causes:
        if isinstance(cause, PermissionError) or (
            isinstance(cause, OSError) and (
                cause.errno in {errno.EACCES, errno.EPERM} or
                getattr(cause, "winerror", None) == 10013
            )
        ):
            return ProbeError(category="network_permission")
        if isinstance(cause, socket.gaierror):
            return ProbeError(category="dns", retryable=cause.errno == socket.EAI_AGAIN)
        if isinstance(cause, ssl.SSLError):
            return ProbeError(category="tls")
        if isinstance(cause, httpx2.ProxyError):
            return ProbeError(category="proxy")
    if isinstance(exc, (TypeSafeAPIConnectionError, httpx2.NetworkError)):
        return ProbeError(category="connection", retryable=True)
    if isinstance(exc, TypeSafeError):
        return ProbeError(category="configuration")
    return ProbeError(category="internal")


class JevClient:
    """One SDK client per service lifespan; one bounded transient retry at most."""

    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        http_client = httpx2.AsyncClient(
            transport=transport,
            timeout=httpx2.Timeout(settings.connect_timeout_seconds),
        )
        self.sdk = AsyncTypeSafeClient(
            api_key=settings.api_key,
            model=settings.model,
            # Keep the key on the documented origin, regardless of ambient overrides.
            base_url="https://api.typesafe.ai",
            http_client=http_client,
            retry=RetryPolicy(max_retries=0),
        )

    async def aclose(self) -> None:
        await self.sdk.aclose()

    async def evaluate(self, case: TextCase, index: int) -> ProbeSample:
        return await self._evaluate(case.state, case.instructions, case.criteria, index)

    async def decide(self, request: DecisionRequest) -> DecisionAnswer:
        sample = await self._evaluate(request.context, request.instructions, request.criteria, 0)
        return DecisionAnswer.model_validate(sample.model_dump())

    async def _evaluate(self, state, instructions, criteria, index: int) -> ProbeSample:
        started = time.perf_counter()
        attempts = 0
        last_error: ProbeError | None = None
        try:
            async with asyncio.timeout(self.settings.request_budget_seconds):
                for attempt in range(2):
                    attempts += 1
                    try:
                        response = await self.sdk.system_one(
                            state=state,
                            questions={"next_operation": Choice(
                                instructions=instructions, criteria=criteria,
                            )},
                        )
                        answer = response.choices.get("next_operation")
                        valid = (
                            answer is not None and answer.choice in criteria and
                            set(answer.probabilities) == set(criteria) and
                            math.isfinite(answer.confidence) and 0 <= answer.confidence <= 1 and
                            all(math.isfinite(p) and 0 <= p <= 1 for p in answer.probabilities.values()) and
                            abs(sum(answer.probabilities.values()) - 1.0) <= 0.02
                        )
                        if not valid:
                            last_error = ProbeError(category="response_invalid")
                            break
                        return ProbeSample(
                            index=index, elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                            attempts=attempts, model=response.model, choice=answer.choice,
                            probabilities=answer.probabilities, confidence=answer.confidence,
                            input_tokens=response.usage.input_tokens,
                            output_tokens=response.usage.output_tokens,
                        )
                    except Exception as exc:
                        last_error = classify_error(exc)
                        if not last_error.retryable or attempt == 1:
                            break
                        delay = last_error.retry_after_seconds
                        delay = 0.25 if delay is None else delay
                        remaining = self.settings.request_budget_seconds - (time.perf_counter() - started)
                        if delay >= remaining:
                            break
                        await asyncio.sleep(delay)
        except TimeoutError:
            last_error = ProbeError(category="timeout", retryable=True)
        return ProbeSample(
            index=index, elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            attempts=attempts, error=last_error or ProbeError(category="internal"),
        )
