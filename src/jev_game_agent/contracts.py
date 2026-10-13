from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextCase(Contract):
    state: str = Field(min_length=1, max_length=4000)
    instructions: str = Field(min_length=1, max_length=1000)
    criteria: dict[str, str] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def validate_criteria(self) -> "TextCase":
        if any(not key.strip() or not value.strip() for key, value in self.criteria.items()):
            raise ValueError("Criteria need nonempty IDs and descriptions")
        if any(len(key) > 100 or len(value) > 1000 for key, value in self.criteria.items()):
            raise ValueError("Criterion ID or description exceeds the probe limit")
        return self


def default_case() -> TextCase:
    return TextCase(
        state="A notice dialog covers the game menu. Its close button is visible. "
        "The user's goal is to inspect the menu. No battle is in progress.",
        instructions="Choose one next operation for this textual test scenario. "
        "These are hypothetical choices; no game input will be executed.",
        criteria={
            "close_notice": "Click the visible close button to reveal the menu.",
            "wait": "Wait briefly and observe the dialog again.",
            "inspect_notice": "Read the visible notice text before continuing.",
        },
    )


class ProbeRequest(Contract):
    mode: Literal["self_check"] = "self_check"
    repetitions: int = Field(default=0, ge=0, le=10, strict=True)
    text_case: TextCase = Field(default_factory=default_case)


class ProbeError(Contract):
    category: Literal[
        "missing_key", "authentication", "request_format", "permission_denied",
        "network_permission", "dns", "tls", "proxy", "connection", "timeout",
        "rate_limit", "service", "response_invalid", "configuration", "internal",
    ]
    retryable: bool = False
    http_status: int | None = None
    retry_after_seconds: float | None = None


class ProbeSample(Contract):
    index: int
    elapsed_ms: float
    attempts: int
    model: str | None = None
    choice: str | None = None
    probabilities: dict[str, float] | None = None
    confidence: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: ProbeError | None = None


class ProbeReport(Contract):
    stage: Literal["P0"] = "P0"
    status: Literal["LOCAL_READY", "TEXT_PROBE_PASSED", "BLOCKED"]
    service_instance_id: str
    api_key_present: bool
    client_ready: bool
    client_initializations: int
    requested_model: str
    versions: dict[str, str]
    requested_repetitions: int
    completed_repetitions: int = 0
    successful_repetitions: int = 0
    samples: list[ProbeSample] = Field(default_factory=list)
    error: ProbeError | None = None
    # Only the observing host can establish its permission/UI conditions.
    host_approval_verified: bool = False
    ui_binding_verified: bool = False
    p0_accepted: bool = False
    gameplay_enabled: bool = False


Name = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")]
RunId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Phase = Literal["NEED_OBSERVATION", "DECIDING", "AWAITING_EXECUTION", "NEED_VERIFICATION", "COMPLETE", "BLOCKED", "STOPPED"]
HelpCategory = Literal["relocate", "read_text", "explain_mechanism", "add_candidates", "analyze_anomaly", "verify_completion", "reconcile_execution", "clarify_constraints", "split_context", "resolve_knowledge", "identify_game"]


def validate_url(value: str) -> None:
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError:
        raise ValueError("Invalid URL port") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an HTTP(S) URL without embedded credentials")


class CompletionCriterion(Contract):
    id: Name
    description: str = Field(min_length=1, max_length=2000)


class Constraint(Contract):
    id: Name
    description: str = Field(min_length=1, max_length=2000)
    forbidden_tags: list[Name] = Field(default_factory=list)
    requires_clarification: bool = False


class StartRequest(Contract):
    mode: Literal["run"] = "run"
    url: str = Field(min_length=1, max_length=2000)
    game_id: Name = "unknown"
    evidence_mode: Literal["live", "simulation", "reconstructed"] = "live"
    goal: str = Field(min_length=1, max_length=4000)
    completion_criteria: list[CompletionCriterion] = Field(min_length=1, max_length=20)
    constraints: list[Constraint] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_task(self) -> "StartRequest":
        validate_url(self.url)
        for values in (self.completion_criteria, self.constraints):
            if len({item.id for item in values}) != len(values):
                raise ValueError("Task IDs must be unique")
        return self


class FrameBinding(Contract):
    browser_id: str = Field(min_length=1, max_length=200)
    page_id: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    screenshot_ref: str = Field(min_length=1, max_length=2000)
    layout_ref: str = Field(min_length=1, max_length=2000)
    viewport_width: int = Field(gt=0, le=20000, strict=True)
    viewport_height: int = Field(gt=0, le=20000, strict=True)
    device_pixel_ratio: float = Field(gt=0, le=10, allow_inf_nan=False)
    coordinate_space: Literal["viewport_css_pixels"] = "viewport_css_pixels"

    @model_validator(mode="after")
    def validate_frame_url(self) -> "FrameBinding":
        validate_url(self.url)
        return self


class Click(Contract):
    kind: Literal["click"] = "click"
    screenshot_ref: str = Field(min_length=1, max_length=2000)
    x: float = Field(ge=0, allow_inf_nan=False)
    y: float = Field(ge=0, allow_inf_nan=False)
    button: Literal["left", "right", "middle"] = "left"


class Key(Contract):
    kind: Literal["key"] = "key"
    screenshot_ref: str = Field(min_length=1, max_length=2000)
    key: str = Field(min_length=1, max_length=100)


class Drag(Contract):
    kind: Literal["drag"] = "drag"
    screenshot_ref: str = Field(min_length=1, max_length=2000)
    from_x: float = Field(ge=0, allow_inf_nan=False)
    from_y: float = Field(ge=0, allow_inf_nan=False)
    to_x: float = Field(ge=0, allow_inf_nan=False)
    to_y: float = Field(ge=0, allow_inf_nan=False)


class Wait(Contract):
    kind: Literal["wait"] = "wait"
    wait_ms: int = Field(ge=1, le=5000, strict=True)


class Observe(Contract):
    kind: Literal["observe"] = "observe"


class Analyze(Contract):
    kind: Literal["analyze"] = "analyze"
    category: HelpCategory


class Stop(Contract):
    kind: Literal["stop"] = "stop"


Action = Annotated[Click | Key | Drag | Wait | Observe | Analyze | Stop, Field(discriminator="kind")]


class Candidate(Contract):
    id: Name
    description: str = Field(min_length=1, max_length=2000)
    semantic_target: Name
    action: Action
    constraint_tags: list[Name] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    prerequisites_known: bool = True
    known_effects: list[str] = Field(default_factory=list)


class VisibleObject(Contract):
    id: Name
    label: str = Field(min_length=1, max_length=1000)
    state: str = Field(max_length=2000)


class GoalEvidence(Contract):
    criterion_id: Name
    fact_indices: list[int] = Field(min_length=1)
    satisfied: bool


class ResourceReading(Contract):
    value: float = Field(allow_inf_nan=False, strict=True)
    unit: str = Field(min_length=1, max_length=100)
    source_text: str = Field(max_length=1000)


class GameIdentity(Contract):
    game_id: Name
    fact_indices: list[int] = Field(min_length=1)


class KnowledgeRef(Contract):
    document: str = Field(min_length=1, max_length=300, pattern=r"^systems/[A-Za-z0-9_-]+\.md$")
    heading: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n#]+$")


class KnowledgeQuery(Contract):
    terms: list[str] = Field(default_factory=list, max_length=20)
    required_refs: list[KnowledgeRef] = Field(default_factory=list, max_length=20)


class KnowledgeScope(Contract):
    environment: str = Field(min_length=1, max_length=300)
    build: str = Field(min_length=1, max_length=300)
    account_stage: str = Field(min_length=1, max_length=300)
    prerequisites: str = Field(min_length=1, max_length=1000)
    privileges: str = Field(min_length=1, max_length=300)


class KnowledgeDraft(Contract):
    ref: KnowledgeRef
    operation: Literal["append", "replace", "dispute"] = "append"
    expected_document_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    statement: str = Field(min_length=1, max_length=4000)
    visible_checks: str = Field(min_length=1, max_length=2000)
    scope: KnowledgeScope
    status: Literal["observed", "verified"] = "observed"
    evidence_kind: Literal["visible_rule", "verified_result"]
    fact_indices: list[int] = Field(min_length=1)
    admission_reason: Literal["main_story", "new_mechanism", "correction", "prevention"]
    increment_reason: str = Field(min_length=1, max_length=2000)
    reusable: bool = False
    sensitive_content_removed: bool = False
    contains_account_progress: bool = True
    resolution_explanation: str | None = Field(default=None, max_length=2000)


class KnowledgeConflict(Contract):
    ref: KnowledgeRef
    expected_document_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: str = Field(min_length=1, max_length=2000)
    fact_indices: list[int] = Field(min_length=1)


class Observation(Contract):
    frame: FrameBinding
    page_label: str = Field(min_length=1, max_length=1000)
    facts: list[Annotated[str, Field(min_length=1, max_length=4000)]] = Field(max_length=200)
    interpretations: list[str] = Field(default_factory=list, max_length=100)
    unknowns: list[str] = Field(default_factory=list, max_length=100)
    changes: list[str] = Field(default_factory=list, max_length=100)
    objects: list[VisibleObject] = Field(default_factory=list, max_length=100)
    candidates: list[Candidate] = Field(default_factory=list, max_length=50)
    goal_evidence: list[GoalEvidence] = Field(default_factory=list, max_length=20)
    anomalies: list[str] = Field(default_factory=list, max_length=100)
    resources: dict[Name, ResourceReading] = Field(default_factory=dict, max_length=100)
    game_identity: GameIdentity | None = None
    knowledge_query: KnowledgeQuery | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> "Observation":
        for values in (self.objects, self.candidates):
            if len({item.id for item in values}) != len(values):
                raise ValueError("Observation IDs must be unique")
        if len({item.criterion_id for item in self.goal_evidence}) != len(self.goal_evidence):
            raise ValueError("Only one evidence assessment per criterion")
        for evidence in self.goal_evidence:
            if any(index < 0 or index >= len(self.facts) for index in evidence.fact_indices):
                raise ValueError("Completion evidence must reference current facts")
        if self.game_identity and any(index < 0 or index >= len(self.facts) for index in self.game_identity.fact_indices):
            raise ValueError("Game identity must reference current facts")
        for candidate in self.candidates:
            action = candidate.action
            if isinstance(action, (Click, Key, Drag)):
                if action.screenshot_ref != self.frame.screenshot_ref:
                    raise ValueError("UI action must bind to this observation's screenshot")
            points = [(action.x, action.y)] if isinstance(action, Click) else (
                [(action.from_x, action.from_y), (action.to_x, action.to_y)] if isinstance(action, Drag) else []
            )
            if any(x >= self.frame.viewport_width or y >= self.frame.viewport_height for x, y in points):
                raise ValueError("Action coordinates must be inside this viewport")
        return self


class ExecutionFeedback(Contract):
    decision_id: Name
    submission_status: Literal["accepted", "rejected", "unknown"]
    outcome: Literal["success", "failure", "unknown"]
    fact_indices: list[int] = Field(default_factory=list)
    unresponsive: bool = False

    @model_validator(mode="after")
    def validate_result(self) -> "ExecutionFeedback":
        if self.outcome == "success" and (self.submission_status == "rejected" or not self.fact_indices):
            raise ValueError("Success needs visual evidence and cannot follow a rejected submission")
        if self.unresponsive and (self.submission_status != "accepted" or self.outcome != "failure"):
            raise ValueError("Only an accepted, visually failed action can be unresponsive")
        return self


class AnalysisFeedback(Contract):
    category: HelpCategory
    summary: str = Field(min_length=1, max_length=4000)
    semantic_targets: list[Name] = Field(default_factory=list)
    fact_indices: list[int] = Field(min_length=1)


class AdvanceRequest(Contract):
    run_id: RunId
    submission_id: Name
    context_version: int = Field(ge=0, strict=True)
    observation: Observation
    previous_execution: ExecutionFeedback | None = None
    analysis: AnalysisFeedback | None = None
    knowledge_drafts: list[KnowledgeDraft] = Field(default_factory=list, max_length=10)
    knowledge_conflicts: list[KnowledgeConflict] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_feedback(self) -> "AdvanceRequest":
        if self.previous_execution and any(
            index < 0 or index >= len(self.observation.facts) for index in self.previous_execution.fact_indices
        ):
            raise ValueError("Execution feedback must reference current facts")
        if self.analysis and any(index < 0 or index >= len(self.observation.facts) for index in self.analysis.fact_indices):
            raise ValueError("Analysis feedback must reference current facts")
        for item in [*self.knowledge_drafts, *self.knowledge_conflicts]:
            if any(index < 0 or index >= len(self.observation.facts) for index in item.fact_indices):
                raise ValueError("Knowledge evidence must reference current facts")
        return self


class RunRequest(Contract):
    run_id: RunId


class FinishRequest(RunRequest):
    reason: Literal["cancelled", "stopped", "blocked"]
    explanation: str = Field(min_length=1, max_length=2000)
    previous_execution: ExecutionFeedback | None = None
    observation: Observation | None = None
    knowledge_drafts: list[KnowledgeDraft] = Field(default_factory=list, max_length=10)
    knowledge_conflicts: list[KnowledgeConflict] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_final_feedback(self) -> "FinishRequest":
        if self.previous_execution is not None:
            if self.observation is None:
                raise ValueError("Final execution feedback requires a fresh observation")
            if any(index < 0 or index >= len(self.observation.facts) for index in self.previous_execution.fact_indices):
                raise ValueError("Final feedback references invalid facts")
        if self.knowledge_drafts or self.knowledge_conflicts:
            if self.observation is None:
                raise ValueError("Knowledge admission needs current observation evidence")
            for item in [*self.knowledge_drafts, *self.knowledge_conflicts]:
                if any(index < 0 or index >= len(self.observation.facts) for index in item.fact_indices):
                    raise ValueError("Knowledge evidence must reference current facts")
        return self


class RuntimeErrorInfo(Contract):
    code: str
    message: str
    service_error: ProbeError | None = None


class TaskResult(Contract):
    type: Literal["EXECUTE", "OBSERVE", "ANALYZE", "COMPLETE", "BLOCKED"]
    run_id: RunId | None = None
    phase: Phase
    context_version: int = 0
    observation_id: str | None = None
    decision_id: str | None = None
    source: Literal["jev", "runtime"] = "runtime"
    candidate: Candidate | None = None
    binding: FrameBinding | None = None
    wait_ms: int = 0
    help_category: HelpCategory | None = None
    message: str
    error: RuntimeErrorInfo | None = None
    completion_evidence: list[dict[str, Any]] = Field(default_factory=list)
    task: dict[str, Any] = Field(default_factory=dict)
    pending_execution: dict[str, Any] | None = None
    replayed: bool = False
    execute_allowed: bool = False
    export_warning: str | None = None
    context_summary: dict[str, Any] = Field(default_factory=dict)


class RunStatus(Contract):
    run_id: RunId
    phase: Phase
    context_version: int
    task: dict[str, Any]
    pending_execution: dict[str, Any] | None
    last_error: dict[str, Any] | None
    metrics: dict[str, Any]
    ledger_path: str
    exported_state_path: str
    context_summary: dict[str, Any] = Field(default_factory=dict)
