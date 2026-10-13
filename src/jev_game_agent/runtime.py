"""Lifecycle, evidence ledger and bounded per-game decision context."""
import hashlib
import json
import math
import sqlite3
from urllib.parse import urlsplit
from uuid import uuid4
from pathlib import Path
from jev_game_agent.context import ContextBuilder
from jev_game_agent.knowledge import KnowledgeLibrary

from jev_game_agent.contracts import (
    AdvanceRequest, Candidate, ExecutionFeedback, FinishRequest, Observation,
    RunStatus, RuntimeErrorInfo, StartRequest, TaskResult,
    ProbeError,
)
from jev_game_agent.decisions.base import DecisionAnswer, DecisionClient, DecisionRequest
from jev_game_agent.decisions.typesafe import classify_error
from jev_game_agent.exports import export_run
from jev_game_agent.store import Store, encode, now


UI_KINDS = {"click", "key", "drag"}


def origin(url: str) -> tuple:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class Runtime:
    def __init__(self, store: Store, client: DecisionClient | None, model: str = "jev-latest", *, knowledge_root=None, context_budget=12000, recent_limit=6):
        self.store, self.client, self.model = store, client, model
        self.owner = uuid4().hex
        self.library = KnowledgeLibrary(knowledge_root or Path(__file__).resolve().parents[2] / "knowledge", store.root)
        self.context_builder = ContextBuilder(self.library, context_budget, recent_limit)

    @staticmethod
    def result(state: dict, kind: str, message: str, **values) -> TaskResult:
        data = {
            "type": kind, "run_id": state["run_id"], "phase": state["phase"],
            "context_version": state["version"], "observation_id": state["observation_id"],
            "task": state["task"], "pending_execution": state["pending"],
            "message": message,
            "context_summary": state.get("context_summary", {}),
        }
        data.update(values)
        return TaskResult(**data)

    def blocked(self, state: dict, code: str, message: str) -> TaskResult:
        return self.result(state, "BLOCKED", message, error=RuntimeErrorInfo(code=code, message=message))

    def publish(self, result: TaskResult) -> TaskResult:
        if result.run_id:
            try:
                export_run(self.store, result.run_id)
            except (OSError, ValueError, sqlite3.Error):
                # Ledger commit is authoritative even if a compatibility export fails.
                result.export_warning = "Compatibility export failed; recover from the SQLite ledger"
        return result

    def start(self, request: StartRequest) -> TaskResult:
        if self.client is None:
            return TaskResult(type="BLOCKED", phase="BLOCKED", message="Decision client is not ready",
                              error=RuntimeErrorInfo(code="client_unavailable", message="Service self-check failed"))
        state = self.store.create(request.model_dump(mode="json"), self.model, self.owner)
        return self.publish(self.result(state, "OBSERVE", "Bind the requested page and provide a fresh screenshot observation"))

    def status(self, run_id: str) -> RunStatus:
        state, metrics = self.store.snapshot(run_id)
        return RunStatus(
            run_id=run_id, phase=state["phase"], context_version=state["version"], task=state["task"],
            pending_execution=state["pending"], last_error=state["last_error"], metrics=metrics,
            ledger_path=str(self.store.path(run_id)), exported_state_path=str(self.store.directory(run_id) / "state.json"),
            context_summary=state.get("context_summary", {}),
        )

    def terminal(self, state: dict) -> TaskResult:
        if state["phase"] == "COMPLETE":
            return self.result(state, "COMPLETE", "The recorded goal is complete", completion_evidence=state["completion_evidence"])
        return self.blocked(state, "run_closed", "This run was ended; it cannot execute further actions")

    def cache(self, db, state: dict, submission_id: str, result: TaskResult) -> TaskResult:
        self.store.save(db, state)
        db.execute("UPDATE submissions SET phase='done',result=? WHERE id=?", (result.model_dump_json(), submission_id))
        return result

    @staticmethod
    def replay(row) -> TaskResult:
        result = TaskResult.model_validate_json(row["result"])
        # Preserve the saved decision, but a repeated delivery is never a new execution permit.
        result.replayed = True
        result.execute_allowed = False
        return result

    def invalidate_inflight(self, db, state: dict, code: str) -> None:
        if state["inflight"]:
            inflight = state["inflight"]
            response = self.blocked(state, code, "The prior request was interrupted or invalidated; do not execute it")
            db.execute("UPDATE submissions SET phase='done',result=? WHERE id=? AND result IS NULL",
                       (response.model_dump_json(), inflight["submission_id"]))
            db.execute("UPDATE decisions SET status='interrupted' WHERE id=? AND status='requesting'", (inflight["decision_id"],))
            self.store.event(db, "request_invalidated", {"decision_id": inflight["decision_id"], "reason": code})
            state["inflight"] = None

    def resume(self, run_id: str) -> TaskResult:
        with self.store.transaction(run_id) as db:
            state = self.store.load(db)
            if state["closed"] or state["phase"] == "COMPLETE":
                return self.terminal(state)
            state["version"] += 1  # Fence all previously issued context/coordinate bindings.
            self.invalidate_inflight(db, state, "resume_required")
            if state["pending"]:
                state["pending"]["status"] = "unknown"
                state["phase"] = "NEED_VERIFICATION"
            else:
                state["phase"] = "NEED_OBSERVATION"
            state["resume_required"] = True
            state["last_error"] = None
            self.store.event(db, "run_resumed", {"pending": state["pending"], "version": state["version"]})
            self.store.save(db, state)
            result = self.result(state, "OBSERVE", "Rebind the page and reconcile a fresh screenshot; never replay a pending action")
        return self.publish(result)

    @staticmethod
    def check_feedback(state: dict, observation: Observation, feedback: ExecutionFeedback | None) -> str | None:
        identity = observation.game_identity
        known_game = state.get("identified_game_id", state["task"]["game_id"])
        if identity and known_game != "unknown" and identity.game_id != known_game:
            return "Observation identifies a different game; start a separate run"
        if feedback and (not state["pending"] or feedback.decision_id != state["pending"]["decision_id"]):
            return "Feedback does not match the single pending decision"
        known = {item["id"] for item in state["task"]["completion_criteria"]}
        if any(item.criterion_id not in known for item in observation.goal_evidence):
            return "Completion evidence references an unknown task criterion"
        if origin(observation.frame.url) != origin(state["task"]["url"]):
            return "Observation is bound to a different URL origin"
        binding = {"browser_id": observation.frame.browser_id, "page_id": observation.frame.page_id}
        if state.get("binding_identity") and binding != state["binding_identity"] and not state["resume_required"]:
            return "Page binding changed without resume and reconciliation"
        return None

    def observe(self, db, state: dict, observation: Observation) -> str:
        if observation.game_identity:
            state["identified_game_id"] = observation.game_identity.game_id
        state["version"] += 1
        state["observation_id"] = "obs-" + uuid4().hex
        state["resume_required"] = False
        state["binding_identity"] = {"browser_id": observation.frame.browser_id, "page_id": observation.frame.page_id}
        state["last_error"] = None
        db.execute("INSERT INTO observations VALUES(?,?,?,?,?)", (
            state["observation_id"], state["version"], observation.frame.screenshot_ref,
            observation.model_dump_json(), now(),
        ))
        self.store.event(db, "observation", {"observation_id": state["observation_id"], "version": state["version"]})
        return state["observation_id"]

    def settle(self, db, state: dict, observation: Observation, feedback: ExecutionFeedback | None) -> bool:
        """Return whether execution is resolved; missing receipts never imply success."""
        if not state["pending"]:
            return True
        pending = state["pending"]
        if feedback is None:
            pending["status"] = "unknown"
            return False
        self.store.event(db, "execution_receipt", {
            "decision_id": feedback.decision_id, "submission_status": feedback.submission_status,
            "observation_id": state["observation_id"],
        })
        state["phase"] = "NEED_VERIFICATION"
        if feedback.submission_status == "rejected":
            self.store.event(db, "execution_rejected", {"decision_id": feedback.decision_id,
                                                       "observation_id": state["observation_id"], "semantic_target": pending["semantic_target"]})
            db.execute("UPDATE decisions SET status='tool_rejected' WHERE id=?", (feedback.decision_id,))
            state["no_progress"] += 1
            state["pending"] = None
            return True
        if feedback.outcome == "unknown":
            pending["status"] = "unknown"
            return False
        proof = [observation.facts[index] for index in feedback.fact_indices]
        if not proof:
            pending["status"] = "unknown"
            return False
        self.store.event(db, "visual_verification", {
            "decision_id": feedback.decision_id, "outcome": feedback.outcome,
            "submission_status": feedback.submission_status, "observation_id": state["observation_id"],
            "semantic_target": pending["semantic_target"], "facts": proof,
            "unresponsive": feedback.unresponsive,
        })
        target = pending["semantic_target"]
        if feedback.outcome == "success":
            state["unresponsive"].pop(target, None)
            state["no_progress"] = 0
        else:
            state["no_progress"] += 1
            if feedback.unresponsive:
                state["unresponsive"][target] = state["unresponsive"].get(target, 0) + 1
        db.execute("UPDATE decisions SET status=? WHERE id=?", ("verified_" + feedback.outcome, feedback.decision_id))
        state["pending"] = None
        return True

    @staticmethod
    def goal_proof(state: dict, observation: Observation) -> list[dict]:
        evidence = {item.criterion_id: item for item in observation.goal_evidence}
        required = state["task"]["completion_criteria"]
        if not all(item["id"] in evidence and evidence[item["id"]].satisfied for item in required):
            return []
        return [{
            "criterion_id": item["id"], "criterion": item["description"],
            "observation_id": state["observation_id"], "screenshot_ref": observation.frame.screenshot_ref,
            "facts": [observation.facts[index] for index in evidence[item["id"]].fact_indices],
            "assessment_source": "host_visual_observation" if state["task"]["evidence_mode"] == "live" else "fixture_or_reconstructed_observation",
            "evidence_mode": state["task"]["evidence_mode"],
        } for item in required]

    def analyze(self, state: dict, category: str, message: str) -> TaskResult:
        state["phase"] = "NEED_VERIFICATION" if state["pending"] else "NEED_OBSERVATION"
        return self.result(state, "ANALYZE", message, help_category=category)

    async def advance(self, request: AdvanceRequest) -> TaskResult:
        return self.publish(await self._advance(request))

    async def _advance(self, request: AdvanceRequest) -> TaskResult:
        digest = hashlib.sha256(encode(request.model_dump(mode="json")).encode()).hexdigest()
        with self.store.transaction(request.run_id) as db:
            state = self.store.load(db)
            cached = db.execute("SELECT * FROM submissions WHERE id=?", (request.submission_id,)).fetchone()
            if cached:
                if cached["input_hash"] != digest:
                    # Normalize old P1 requests with newly added default fields before comparing.
                    normalized = AdvanceRequest.model_validate_json(cached["request"]).model_dump(mode="json")
                    if hashlib.sha256(encode(normalized).encode()).hexdigest() != digest:
                        return self.blocked(state, "submission_conflict", "The submission ID was already used with different input")
                if cached["result"]:
                    return self.replay(cached)
                return self.blocked(state, "request_in_progress", "This submission is still unresolved; resume after interruption")
            if state["closed"] or state["phase"] == "COMPLETE":
                return self.terminal(state)
            if state["inflight"]:
                return self.blocked(state, "run_busy", "Only one decision request may be in flight per run")
            if state["resume_required"] and state["phase"] == "BLOCKED":
                return self.blocked(state, "resume_required", "Resume the interrupted run before providing a new observation")
            if request.context_version != state["version"]:
                return self.blocked(state, "stale_context", "Use the current context version and a fresh screenshot")
            if db.execute("SELECT 1 FROM observations WHERE screenshot_ref=?", (request.observation.frame.screenshot_ref,)).fetchone():
                return self.blocked(state, "stale_observation", "This screenshot reference was already consumed")
            invalid = self.check_feedback(state, request.observation, request.previous_execution)
            if invalid:
                return self.blocked(state, "invalid_feedback_or_binding", invalid)
            db.execute("INSERT INTO submissions VALUES(?,?,?,'preparing',NULL,NULL,?)", (
                request.submission_id, digest, request.model_dump_json(), now(),
            ))
            observation_id = self.observe(db, state, request.observation)
            if request.analysis:
                self.store.event(db, "host_analysis", {
                    "category": request.analysis.category, "summary": request.analysis.summary,
                    "observation_id": observation_id,
                    "facts": [request.observation.facts[index] for index in request.analysis.fact_indices],
                })
                if request.analysis.category == "relocate":
                    for target in request.analysis.semantic_targets:
                        state["analysis_reviewed"][target] = state["unresponsive"].get(target, 0)
                if request.analysis.category == "analyze_anomaly":
                    state["no_progress"] = 0
            if not self.settle(db, state, request.observation, request.previous_execution):
                result = self.analyze(state, "reconcile_execution", "The prior action is unresolved. Inspect its effects; do not repeat it")
                self.cache(db, state, request.submission_id, result)
                immediate = result
            else:
                issues = self.library.maintain(db, self.store, state, request.observation, request.knowledge_drafts,
                                              request.knowledge_conflicts, state["run_id"] + ":" + request.submission_id)
                state.setdefault("context_summary", {})["knowledge_maintenance_issues"] = issues
                proof = self.goal_proof(state, request.observation)
                if proof:
                    state["phase"] = "COMPLETE"
                    state["closed"] = True
                    state["completion_evidence"] = proof
                    self.store.event(db, "goal_complete", {"evidence": proof})
                    immediate = self.cache(db, state, request.submission_id,
                        self.result(state, "COMPLETE", "Current visual evidence satisfies every completion criterion", completion_evidence=proof))
                elif issues:
                    immediate = self.cache(db, state, request.submission_id,
                        self.analyze(state, "resolve_knowledge", "Knowledge maintenance requires evidence or conflict analysis; inspect context_summary"))
                elif any(item["requires_clarification"] for item in state["task"]["constraints"]):
                    immediate = self.cache(db, state, request.submission_id,
                        self.analyze(state, "clarify_constraints", "The task includes unresolved authorization constraints"))
                elif any(count >= 2 and state["analysis_reviewed"].get(target) != count for target, count in state["unresponsive"].items()):
                    immediate = self.cache(db, state, request.submission_id,
                        self.analyze(state, "relocate", "Two visually verified inputs were unresponsive; check positioning and available console logs"))
                elif state["no_progress"] >= 3:
                    immediate = self.cache(db, state, request.submission_id,
                        self.analyze(state, "analyze_anomaly", "Three rounds made no verified progress; provide analysis before continuing"))
                else:
                    immediate = None
            if immediate is None:
                forbidden = {tag for item in state["task"]["constraints"] for tag in item["forbidden_tags"]}
                allowed, excluded = [], []
                for candidate in request.observation.candidates:
                    tags = sorted(forbidden.intersection(candidate.constraint_tags))
                    if tags:
                        excluded.append({"candidate_id": candidate.id, "forbidden_tags": tags})
                    else:
                        allowed.append(candidate)
                if not allowed:
                    immediate = self.cache(db, state, request.submission_id,
                        self.analyze(state, "add_candidates", "No executable alternatives remain; provide feasible candidates"))
                else:
                    built = self.context_builder.build(db, state, request.observation, allowed, excluded,
                                                       request.previous_execution, request.analysis, self.model)
                    state["context_summary"] = built.summary
                    self.store.event(db, "context_built", built.summary)
                    if built.request is None:
                        result = self.analyze(state, built.help_category, built.message)
                        self.cache(db, state, request.submission_id, result)
                        return result
                    decision_request = built.request
                    decision_id = "decision-" + uuid4().hex
                    state["phase"] = "DECIDING"
                    state["inflight"] = {"submission_id": request.submission_id, "decision_id": decision_id, "owner": self.owner}
                    db.execute("UPDATE submissions SET phase='deciding',decision_id=? WHERE id=?", (decision_id, request.submission_id))
                    db.execute("INSERT INTO decisions VALUES(?,?,?,?,NULL,'requesting',?)", (
                        decision_id, observation_id, state["version"], decision_request.model_dump_json(), now(),
                    ))
                    self.store.event(db, "decision_requested", {"decision_id": decision_id, "observation_id": observation_id})
                    self.store.save(db, state)
                    expected_version = state["version"]
        if immediate is not None:
            return immediate
        try:
            if self.client is None:
                answer = DecisionAnswer(index=0, elapsed_ms=0, attempts=0, error=ProbeError(category="missing_key"))
            else:
                answer = await self.client.decide(decision_request)
        except Exception as exc:
            answer = DecisionAnswer(index=0, elapsed_ms=0, attempts=0, error=classify_error(exc))
        except BaseException:
            # Cancellation/transport loss cannot authorize a UI replay on recovery.
            with self.store.transaction(request.run_id) as db:
                current = self.store.load(db)
                if current["inflight"] and current["inflight"]["decision_id"] == decision_id:
                    current["phase"] = "BLOCKED"
                    current["resume_required"] = True
                    current["last_error"] = {"code": "decision_interrupted"}
                    self.invalidate_inflight(db, current, "decision_interrupted")
                    self.store.save(db, current)
            raise
        with self.store.transaction(request.run_id) as db:
            state = self.store.load(db)
            inflight = state["inflight"]
            current = (
                inflight and inflight["decision_id"] == decision_id and inflight["owner"] == self.owner
                and state["version"] == expected_version and state["phase"] == "DECIDING"
            )
            db.execute("UPDATE decisions SET response=?,status=? WHERE id=?", (
                answer.model_dump_json(), "answered" if current else "late", decision_id,
            ))
            self.store.event(db, "decision_response", {"decision_id": decision_id, "late": not bool(current)})
            if not current:
                cached = db.execute("SELECT * FROM submissions WHERE id=?", (request.submission_id,)).fetchone()
                return self.replay(cached) if cached and cached["result"] else self.blocked(state, "stale_decision", "Late answer recorded without execution")
            state["inflight"] = None
            answer_valid = (
                answer.error is None and answer.choice in decision_request.criteria and
                answer.probabilities is not None and set(answer.probabilities) == set(decision_request.criteria) and
                answer.confidence is not None and math.isfinite(answer.confidence) and 0 <= answer.confidence <= 1 and
                all(math.isfinite(p) and 0 <= p <= 1 for p in answer.probabilities.values()) and
                abs(sum(answer.probabilities.values()) - 1) <= 0.02
            )
            if not answer_valid:
                state["phase"] = "BLOCKED"
                state["last_error"] = {"code": "decision_failed", "service_error": answer.error.model_dump(mode="json") if answer.error else None}
                result = self.result(state, "BLOCKED", "Decision service failed or returned an invalid option; no UI input is permitted",
                    decision_id=decision_id, error=RuntimeErrorInfo(code="decision_failed", message="Check the decision service before resuming", service_error=answer.error))
            else:
                candidate = Candidate.model_validate(decision_request.criteria[answer.choice])
                action = candidate.action
                if action.kind in UI_KINDS and not candidate.prerequisites_known:
                    result = self.analyze(state, "read_text", "The selected operation has an unknown prerequisite; gather its evidence first")
                    result.decision_id, result.source = decision_id, "jev"
                elif action.kind in UI_KINDS:
                    state["phase"] = "AWAITING_EXECUTION"
                    state["pending"] = {
                        "decision_id": decision_id, "observation_id": observation_id,
                        "context_version": expected_version, "candidate_id": candidate.id,
                        "description": candidate.description, "semantic_target": candidate.semantic_target,
                        "kind": action.kind, "status": "intent_saved",
                    }
                    self.store.event(db, "execution_intent", {"decision_id": decision_id, "candidate": candidate.model_dump(mode="json"),
                                                             "binding": request.observation.frame.model_dump(mode="json")})
                    result = self.result(state, "EXECUTE", "Execute this exact candidate once, then submit fresh visual feedback",
                        decision_id=decision_id, source="jev", candidate=candidate, binding=request.observation.frame, execute_allowed=True)
                elif action.kind == "analyze":
                    result = self.analyze(state, action.category, "Provide the selected analysis, then return a fresh observation")
                    result.decision_id, result.source = decision_id, "jev"
                else:
                    state["phase"] = "NEED_OBSERVATION"
                    if not request.observation.changes:
                        state["no_progress"] += 1
                    result = self.result(state, "OBSERVE", "Wait within the bound and capture a fresh screenshot" if action.kind == "wait" else
                        "Verify completion visually before stopping" if action.kind == "stop" else "Provide a fresh observation",
                        decision_id=decision_id, source="jev", wait_ms=action.wait_ms if action.kind == "wait" else 0)
            self.cache(db, state, request.submission_id, result)
        return result

    def finish(self, request: FinishRequest) -> TaskResult:
        with self.store.transaction(request.run_id) as db:
            state = self.store.load(db)
            if state["closed"] or state["phase"] == "COMPLETE":
                return self.terminal(state)
            if request.observation:
                invalid = self.check_feedback(state, request.observation, request.previous_execution)
                if invalid:
                    return self.blocked(state, "invalid_feedback_or_binding", invalid)
                if db.execute("SELECT 1 FROM observations WHERE screenshot_ref=?", (request.observation.frame.screenshot_ref,)).fetchone():
                    return self.blocked(state, "stale_observation", "Final feedback requires a new screenshot reference")
                self.observe(db, state, request.observation)
                self.settle(db, state, request.observation, request.previous_execution)
                issues = self.library.maintain(db, self.store, state, request.observation, request.knowledge_drafts,
                                              request.knowledge_conflicts, state["run_id"] + ":finish:" + str(state["version"]))
                state["context_summary"] = {"knowledge_maintenance_issues": issues}
            state["version"] += 1
            state["phase"] = "BLOCKED" if request.reason == "blocked" else "STOPPED"
            state["closed"] = request.reason != "blocked"
            self.invalidate_inflight(db, state, "run_finished")
            if state["pending"]:
                state["pending"]["status"] = "unknown"
            state["last_error"] = {"code": "user_" + request.reason, "message": request.explanation}
            self.store.event(db, "run_finished", {"reason": request.reason, "explanation": request.explanation, "pending": state["pending"]})
            self.store.save(db, state)
            result = self.result(state, "BLOCKED", "Run ended without asserting goal completion",
                                 error=RuntimeErrorInfo(code="user_" + request.reason, message=request.explanation))
        return self.publish(result)
