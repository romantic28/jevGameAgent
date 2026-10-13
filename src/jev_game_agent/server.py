import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.metadata import version
from uuid import uuid4
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from jev_game_agent import __version__
from jev_game_agent.config import Settings
from jev_game_agent.contracts import (
    AdvanceRequest, FinishRequest, ProbeError, ProbeReport, ProbeRequest,
    RunRequest, RunStatus, RuntimeErrorInfo, StartRequest, TaskResult,
)
from jev_game_agent.decisions.typesafe import JevClient, classify_error
from jev_game_agent.runtime import Runtime
from jev_game_agent.store import Store, StoreError


@dataclass
class ProbeService:
    settings: Settings
    client: JevClient | None = None
    startup_error: ProbeError | None = None
    runtime: Runtime | None = None
    instance_id: str = field(default_factory=lambda: str(uuid4()))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def check(self, request: ProbeRequest) -> ProbeReport:
        report = ProbeReport(
            status="LOCAL_READY", service_instance_id=self.instance_id,
            api_key_present=bool(self.settings.api_key), client_ready=self.client is not None,
            client_initializations=int(self.client is not None), requested_model=self.settings.model,
            versions={name: version(name) for name in ("mcp", "typesafe-sdk", "pydantic", "httpx2")},
            requested_repetitions=request.repetitions,
        )
        if self.startup_error or self.client is None:
            report.status = "BLOCKED"
            report.error = self.startup_error or ProbeError(category="missing_key")
            return report
        # Serialize probe calls on the same service/client.
        async with self.lock:
            for index in range(1, request.repetitions + 1):
                sample = await self.client.evaluate(request.text_case, index)
                report.samples.append(sample)
                report.completed_repetitions += 1
                if sample.error:
                    report.status = "BLOCKED"
                    report.error = sample.error
                    return report
                report.successful_repetitions += 1
        if request.repetitions:
            report.status = "TEXT_PROBE_PASSED"
        return report


def create_server(settings: Settings | None = None, client_factory=JevClient) -> MCPServer:
    settings = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[ProbeService]:
        service = ProbeService(settings=settings)
        if settings.api_key:
            try:
                service.client = client_factory(settings)
            except Exception as exc:
                service.startup_error = classify_error(exc)
        try:
            service.runtime = Runtime(Store(settings.run_root), service.client, settings.model, knowledge_root=settings.knowledge_root, context_budget=settings.context_character_budget, recent_limit=settings.recent_verified_results)
        except (OSError, StoreError):
            service.startup_error = ProbeError(category="configuration")
        try:
            yield service
        finally:
            if service.client is not None:
                await service.client.aclose()

    server = MCPServer(
        "jev-game-agent", version=__version__, lifespan=lifespan,
        instructions="P2 decision runtime; the host owns screenshots and UI input. start accepts run or self_check. "
        "Use advance with the returned context_version and a fresh screenshot_ref. Execute only EXECUTE results "
        "with execute_allowed=true once per decision_id. After input submit fresh visual feedback. "
        "Duplicate deliveries never permit execution. resume invalidates old bindings and requires reconciliation; "
        "never repeat an unresolved action. Never send credentials. Context is bounded to 12000 characters; knowledge is isolated by game. Knowledge drafts require current evidence and maintenance checks. ANALYZE requests must be resolved before proceeding. P3 host workflow migration is not implemented.",
    )

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True,
    ))
    async def start(request: ProbeRequest | StartRequest, ctx: Context[ProbeService]) -> dict[str, Any]:
        """Start a run with URL, goal, completion criteria and constraints, or run the compatible P0 self-check."""
        service = ctx.request_context.lifespan_context
        if isinstance(request, ProbeRequest):
            return (await service.check(request)).model_dump(mode="json")
        if service.startup_error or service.runtime is None:
            return TaskResult(type="BLOCKED", phase="BLOCKED", message="Runtime self-check failed",
                              error=RuntimeErrorInfo(code="startup_failed", message="Runtime self-check failed", service_error=service.startup_error)).model_dump(mode="json")
        return service.runtime.start(request).model_dump(mode="json")

    def runtime(ctx: Context[ProbeService]) -> Runtime:
        value = ctx.request_context.lifespan_context.runtime
        if value is None:
            raise ToolError("Runtime storage is unavailable")
        return value

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True))
    async def advance(request: AdvanceRequest, ctx: Context[ProbeService]) -> TaskResult:
        """Verify prior input, record fresh evidence, and persist exactly one next task.

        Pass an object under request with run_id, submission_id, context_version,
        observation. observation requires frame, page_label, facts. frame requires
        browser_id, page_id, url, screenshot_ref, layout_ref, viewport_width,
        viewport_height, device_pixel_ratio; coordinate_space=viewport_css_pixels.
        objects entries contain only id, label, state. Candidate id and
        semantic_target are ASCII identifiers; action.kind selects click/key/drag/
        wait/observe/analyze/stop. UI actions need this frame's screenshot_ref.
        goal_evidence entries require criterion_id, satisfied (boolean),
        fact_indices (zero-based current facts). previous_execution, when needed,
        contains decision_id, submission_status (accepted/rejected/unknown),
        outcome (success/failure/unknown), fact_indices, optional unresponsive.
        Input acceptance is not visual success. Full input schema is in tools/list;
        docs/p1-contracts.md and docs/p2-contracts.md cover optional analysis/knowledge.
        """
        try:
            return await runtime(ctx).advance(request)
        except StoreError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False))
    def resume(request: RunRequest, ctx: Context[ProbeService]) -> TaskResult:
        """Load the run, invalidate old bindings and request fresh evidence without replaying UI input."""
        try:
            return runtime(ctx).resume(request.run_id)
        except StoreError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def status(request: RunRequest, ctx: Context[ProbeService]) -> RunStatus:
        """Read the ledger state and telemetry without contacting Jev or modifying the run."""
        try:
            return runtime(ctx).status(request.run_id)
        except StoreError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False))
    def finish(request: FinishRequest, ctx: Context[ProbeService]) -> TaskResult:
        """End, cancel or block a run and export a report; never assert goal success without visual proof."""
        try:
            return runtime(ctx).finish(request)
        except StoreError as exc:
            raise ToolError(str(exc)) from None

    return server
