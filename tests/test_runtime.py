"""All visual observations here are simulated fixtures, never game evidence."""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from mcp import Client
from pydantic import ValidationError

from jev_game_agent.config import Settings
from jev_game_agent.contracts import (
    AdvanceRequest, FinishRequest, Observation, ProbeError, StartRequest,
    AnalysisFeedback,
)
from jev_game_agent.decisions.base import DecisionAnswer
from jev_game_agent.runtime import Runtime
from jev_game_agent.server import create_server
from jev_game_agent.store import Store, StoreError


class FakeClient:
    def __init__(self, choices=None, error=None):
        self.choices = iter(choices or [])
        self.requests = []
        self.error = error

    async def decide(self, request):
        self.requests.append(request)
        if self.error:
            return DecisionAnswer(index=0, attempts=1, elapsed_ms=1, error=self.error)
        choice = next(self.choices, next(iter(request.criteria)))
        return DecisionAnswer(index=0, attempts=1, elapsed_ms=2, model="simulated-jev",
                              choice=choice, confidence=0.2,
                              probabilities={key: float(key == choice) for key in request.criteria})

    async def aclose(self):
        pass


def task(**changes):
    value = {"mode": "run", "evidence_mode": "simulation", "url": "http://example.test/game/", "goal": "Close the notice",
             "completion_criteria": [{"id": "notice_closed", "description": "The notice is absent and the menu is visible"}]}
    value.update(changes)
    return StartRequest.model_validate(value)


def observation(ref="shot-1", complete=False, candidates=None, **changes):
    value = {
        "frame": {"browser_id": "browser-test", "page_id": "page-test", "url": "http://example.test/game/?scene=mock",
                  "screenshot_ref": ref, "layout_ref": ref, "viewport_width": 710, "viewport_height": 892, "device_pixel_ratio": 1},
        "page_label": "simulated notice" if not complete else "simulated menu",
        "facts": ["Notice close button is visible"] if not complete else ["Notice is absent; menu is visible"],
        "unknowns": ["Account progression is unknown"],
        "interpretations": ["Closing the notice may reveal the menu"],
        "candidates": candidates if candidates is not None else [
            {"id": "close", "description": "Click the visible close button", "semantic_target": "notice",
             "action": {"kind": "click", "screenshot_ref": ref, "x": 543, "y": 125}},
            {"id": "observe", "description": "Capture another view", "semantic_target": "page", "action": {"kind": "observe"}},
        ],
        "goal_evidence": [{"criterion_id": "notice_closed", "fact_indices": [0], "satisfied": True}] if complete else [],
    }
    value.update(changes)
    return Observation.model_validate(value)


def advance(started, ref="shot-1", submission="step-1", feedback=None, complete=False, **obs_changes):
    return AdvanceRequest(run_id=started.run_id, submission_id=submission, context_version=started.context_version,
                          observation=observation(ref, complete, **obs_changes), previous_execution=feedback)


@pytest.fixture
def setup(tmp_path):
    client = FakeClient()
    runtime = Runtime(Store(tmp_path / "run"), client)
    return runtime, client, runtime.start(task())


async def test_intent_receipt_verification_and_completion_are_separate(setup):
    runtime, client, started = setup
    selected = await runtime.advance(advance(started))
    assert selected.type == "EXECUTE" and selected.execute_allowed
    assert selected.source == "jev" and selected.phase == "AWAITING_EXECUTION"
    assert runtime.status(started.run_id).metrics["execution_receipts"] == 0
    feedback = {"decision_id": selected.decision_id, "submission_status": "accepted", "outcome": "success", "fact_indices": [0]}
    result = await runtime.advance(advance(selected, "shot-2", "step-2", feedback, complete=True))
    assert result.type == "COMPLETE" and result.completion_evidence[0]["facts"]
    assert len(client.requests) == 1  # Completed evidence stops further decisions.
    status = runtime.status(started.run_id)
    assert status.metrics["execution_intents"] == status.metrics["execution_receipts"] == status.metrics["visual_verifications"] == 1
    report = json.loads((runtime.store.directory(started.run_id) / "report.json").read_text(encoding="utf-8"))
    assert report["state"]["status"] == "complete"
    with runtime.store.open(started.run_id, readonly=True) as db:
        kinds = [row[0] for row in db.execute("SELECT kind FROM events ORDER BY seq")]
    assert kinds.index("execution_intent") < kinds.index("execution_receipt") < kinds.index("visual_verification") < kinds.index("goal_complete")


async def test_duplicate_is_saved_and_never_permits_a_second_execution(setup):
    runtime, client, started = setup
    request = advance(started)
    first = await runtime.advance(request)
    duplicate = await runtime.advance(request)
    assert duplicate.decision_id == first.decision_id
    assert duplicate.candidate == first.candidate
    assert duplicate.replayed and not duplicate.execute_allowed
    assert len(client.requests) == 1
    conflict = await runtime.advance(advance(started, "different-shot"))
    assert conflict.error.code == "submission_conflict"
    assert runtime.status(started.run_id).context_version == first.context_version


async def test_new_observation_without_receipt_cannot_repeat_or_complete(setup):
    runtime, client, started = setup
    intent = await runtime.advance(advance(started))
    result = await runtime.advance(advance(intent, "shot-2", "step-2", complete=True))
    assert result.type == "ANALYZE" and result.help_category == "reconcile_execution"
    assert result.pending_execution["decision_id"] == intent.decision_id
    assert result.pending_execution["status"] == "unknown"
    assert len(client.requests) == 1


async def test_receipt_unknown_can_be_resolved_by_fresh_visual_success(setup):
    runtime, client, started = setup
    intent = await runtime.advance(advance(started))
    feedback = {"decision_id": intent.decision_id, "submission_status": "unknown", "outcome": "success", "fact_indices": [0]}
    result = await runtime.advance(advance(intent, "shot-2", "step-2", feedback, complete=True))
    assert result.type == "COMPLETE" and result.pending_execution is None


async def test_rejected_input_is_not_counted_as_unresponsive(setup):
    runtime, client, started = setup
    intent = await runtime.advance(advance(started))
    feedback = {"decision_id": intent.decision_id, "submission_status": "rejected", "outcome": "failure"}
    result = await runtime.advance(advance(intent, "shot-2", "step-2", feedback))
    assert result.type == "EXECUTE"  # A new Jev choice, not an automatic retry.
    with runtime.store.open(started.run_id, readonly=True) as db:
        assert runtime.store.load(db)["unresponsive"] == {}
    assert runtime.status(started.run_id).metrics["visual_verifications"] == 0
    assert runtime.status(started.run_id).metrics["tool_rejections"] == 1


async def test_unknown_effects_remain_pending_after_report_and_restart(setup):
    runtime, client, started = setup
    intent = await runtime.advance(advance(started))
    ended = runtime.finish(FinishRequest(run_id=intent.run_id, reason="blocked", explanation="Lost tool receipt"))
    assert ended.phase == "BLOCKED" and ended.pending_execution["status"] == "unknown"
    restarted = Runtime(Store(runtime.store.root), FakeClient())
    resumed = restarted.resume(intent.run_id)
    assert resumed.type == "OBSERVE" and resumed.phase == "NEED_VERIFICATION"
    assert resumed.candidate is None and resumed.binding is None and not resumed.execute_allowed
    assert "x" not in resumed.pending_execution and "action" not in resumed.pending_execution
    unresolved = await restarted.advance(advance(resumed, "shot-2", "after-restart"))
    assert unresolved.type == "ANALYZE"


async def test_stale_versions_screenshots_and_wrong_decisions_rejected(setup):
    runtime, client, started = setup
    intent = await runtime.advance(advance(started))
    old = await runtime.advance(advance(started, "shot-2", "stale"))
    assert old.error.code == "stale_context"
    screenshot = await runtime.advance(advance(intent, "shot-1", "stale-shot"))
    assert screenshot.error.code == "stale_observation"
    wrong = await runtime.advance(advance(intent, "shot-2", "wrong", {"decision_id": "wrong", "submission_status": "accepted", "outcome": "success", "fact_indices": [0]}))
    assert wrong.error.code == "invalid_feedback_or_binding"
    assert len(client.requests) == 1


async def test_resume_fences_cached_intents_and_export_edits_are_ignored(setup):
    runtime, client, started = setup
    request = advance(started)
    intent = await runtime.advance(request)
    (runtime.store.directory(started.run_id) / "state.json").write_text('{"status":"complete"}', encoding="utf-8")
    fresh_runtime = Runtime(Store(runtime.store.root), client)
    assert fresh_runtime.status(started.run_id).phase == "AWAITING_EXECUTION"
    resumed = fresh_runtime.resume(started.run_id)
    replay = await fresh_runtime.advance(request)
    assert not replay.execute_allowed and replay.replayed
    assert resumed.context_version > intent.context_version
    assert fresh_runtime.status(started.run_id).phase == "NEED_VERIFICATION"


class PausedClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def decide(self, request):
        self.entered.set()
        await self.release.wait()
        return await super().decide(request)


@pytest.mark.parametrize("invalidation", ["resume", "finish"])
async def test_late_answer_is_archived_without_execution(tmp_path, invalidation):
    client = PausedClient()
    runtime = Runtime(Store(tmp_path), client)
    started = runtime.start(task())
    running = asyncio.create_task(runtime.advance(advance(started)))
    await client.entered.wait()
    # A second Runtime simulates a competing service/recovery process.
    other = Runtime(Store(tmp_path), FakeClient())
    if invalidation == "resume":
        other.resume(started.run_id)
    else:
        other.finish(FinishRequest(run_id=started.run_id, reason="cancelled", explanation="User cancelled"))
    client.release.set()
    result = await running
    assert not result.execute_allowed and result.type == "BLOCKED"
    status = other.status(started.run_id)
    assert status.pending_execution is None
    assert status.metrics["late_responses"] == 1
    assert status.metrics["execution_intents"] == 0


async def test_concurrent_duplicate_and_new_request_make_one_call(tmp_path):
    client = PausedClient()
    runtime = Runtime(Store(tmp_path), client)
    started = runtime.start(task())
    request = advance(started)
    running = asyncio.create_task(runtime.advance(request))
    await client.entered.wait()
    other = Runtime(Store(tmp_path), FakeClient())
    duplicate = await other.advance(request)
    assert duplicate.error.code == "request_in_progress"
    next_request = await other.advance(advance(started, "shot-2", "step-2"))
    assert next_request.error.code == "run_busy"
    client.release.set()
    await running
    assert len(client.requests) == 1 and other.status(started.run_id).metrics["decision_requests"] == 1


async def test_cancellation_requires_resume_and_never_reissues_submission(tmp_path):
    client = PausedClient()
    runtime = Runtime(Store(tmp_path), client)
    started = runtime.start(task())
    request = advance(started)
    running = asyncio.create_task(runtime.advance(request))
    await client.entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert runtime.status(started.run_id).phase == "BLOCKED"
    duplicate = await runtime.advance(request)
    assert duplicate.replayed and not duplicate.execute_allowed
    replacement = Runtime(Store(tmp_path), FakeClient())
    resumed = replacement.resume(started.run_id)
    assert resumed.type == "OBSERVE"
    result = await replacement.advance(advance(resumed, "shot-2", "fresh-request"))
    assert result.type == "EXECUTE"


async def test_restart_before_request_response_is_not_assumed_failed(setup):
    runtime, client, started = setup
    # Explicit crash fixture: request was committed but process died before storing an answer.
    with runtime.store.transaction(started.run_id) as db:
        state = runtime.store.load(db)
        state["phase"] = "DECIDING"
        state["inflight"] = {"submission_id": "crashed", "decision_id": "crashed-decision", "owner": "dead-process"}
        db.execute("INSERT INTO submissions VALUES('crashed','hash','{}','deciding',NULL,'crashed-decision',?)", ("mock-time",))
        db.execute("INSERT INTO decisions VALUES('crashed-decision','obs',0,'{}',NULL,'requesting','mock-time')")
        runtime.store.save(db, state)
    replacement = Runtime(Store(runtime.store.root), FakeClient())
    assert (await replacement.advance(advance(started))).error.code == "run_busy"
    resumed = replacement.resume(started.run_id)
    assert resumed.type == "OBSERVE" and resumed.pending_execution is None
    assert replacement.status(started.run_id).metrics["decision_responses"] == 0


@pytest.mark.parametrize("error", ["authentication", "network_permission", "service", "timeout"])
async def test_service_failure_blocks_without_codex_takeover(tmp_path, error):
    client = FakeClient(error=ProbeError(category=error))
    runtime = Runtime(Store(tmp_path), client)
    started = runtime.start(task())
    result = await runtime.advance(advance(started))
    assert result.type == "BLOCKED" and result.error.service_error.category == error
    assert runtime.status(started.run_id).metrics["execution_intents"] == 0
    assert runtime.status(started.run_id).pending_execution is None


async def test_invalid_option_never_becomes_tool_parameters(tmp_path):
    runtime = Runtime(Store(tmp_path), FakeClient(["unlisted"]))
    result = await runtime.advance(advance(runtime.start(task())))
    assert result.type == "BLOCKED" and not result.execute_allowed


async def test_wait_analysis_and_stop_are_control_branches(tmp_path):
    client = FakeClient(["wait", "analyze", "stop"])
    runtime = Runtime(Store(tmp_path), client)
    current = runtime.start(task())
    candidates = [
        {"id": "wait", "description": "Wait briefly", "semantic_target": "page", "action": {"kind": "wait", "wait_ms": 5000}},
        {"id": "analyze", "description": "Explain the mechanism", "semantic_target": "page", "action": {"kind": "analyze", "category": "explain_mechanism"}},
        {"id": "stop", "description": "Check whether the task is complete", "semantic_target": "task", "action": {"kind": "stop"}},
    ]
    waiting = await runtime.advance(advance(current, candidates=candidates))
    assert waiting.type == "OBSERVE" and waiting.wait_ms == 5000
    analysis = await runtime.advance(advance(waiting, "shot-2", "step-2", candidates=candidates))
    assert analysis.type == "ANALYZE" and analysis.help_category == "explain_mechanism"
    stopped = await runtime.advance(advance(analysis, "shot-3", "step-3", candidates=candidates))
    assert stopped.type == "OBSERVE" and stopped.phase != "COMPLETE"
    assert runtime.status(current.run_id).metrics["execution_intents"] == 0


async def test_unknown_prerequisite_is_not_discarded_but_cannot_execute(tmp_path):
    client = FakeClient()
    runtime = Runtime(Store(tmp_path), client)
    started = runtime.start(task())
    obs = observation()
    obs.candidates[0].prerequisites_known = False
    result = await runtime.advance(AdvanceRequest(run_id=started.run_id, submission_id="step-1", context_version=0, observation=obs))
    assert "close" in client.requests[0].criteria
    assert result.type == "ANALYZE" and not result.execute_allowed


async def test_explicit_constraint_exclusion_is_recorded(tmp_path):
    client = FakeClient()
    runtime = Runtime(Store(tmp_path), client)
    started = runtime.start(task(constraints=[{"id": "no-spend", "description": "No currency spending", "forbidden_tags": ["spend"]}]))
    obs = observation()
    obs.candidates[0].constraint_tags = ["spend"]
    result = await runtime.advance(AdvanceRequest(run_id=started.run_id, submission_id="step-1", context_version=0, observation=obs))
    assert result.type == "OBSERVE"
    assert "close" not in client.requests[0].criteria
    assert client.requests[0].context["excluded_candidates"] == [{"candidate_id": "close", "forbidden_tags": ["spend"]}]


async def test_no_candidates_requests_analysis_without_calling_jev(setup):
    runtime, client, started = setup
    result = await runtime.advance(advance(started, candidates=[]))
    assert result.type == "ANALYZE" and len(client.requests) == 0


async def test_two_unresponsive_inputs_request_review_then_return_to_jev(setup):
    runtime, client, current = setup
    intent = await runtime.advance(advance(current))
    feedback = {"decision_id": intent.decision_id, "submission_status": "accepted", "outcome": "failure", "fact_indices": [0], "unresponsive": True}
    intent = await runtime.advance(advance(intent, "first-failure", "first-failure", feedback))
    feedback["decision_id"] = intent.decision_id
    current = await runtime.advance(advance(intent, "second-failure", "second-failure", feedback))
    assert current.type == "ANALYZE" and current.help_category == "relocate"
    request = advance(current, "review-shot", "review")
    request.analysis = AnalysisFeedback(category="relocate", summary="Reidentified the close button and checked available logs", semantic_targets=["notice"], fact_indices=[0])
    reviewed = await runtime.advance(request)
    assert reviewed.type == "EXECUTE" and len(client.requests) == 3


def test_finish_cancel_is_not_success_and_status_is_read_only(setup):
    runtime, client, started = setup
    state_before = runtime.store.path(started.run_id).read_bytes()
    runtime.status(started.run_id)
    assert runtime.store.path(started.run_id).read_bytes() == state_before
    ended = runtime.finish(FinishRequest(run_id=started.run_id, reason="cancelled", explanation="User stopped this mock test"))
    assert ended.phase == "STOPPED" and ended.type == "BLOCKED"
    assert runtime.resume(started.run_id).error.code == "run_closed"
    assert not client.requests


@pytest.mark.parametrize("bad", [
    {"frame": {"browser_id": "b", "page_id": "p", "url": "http://example.test", "screenshot_ref": "s", "layout_ref": "s", "viewport_width": 0, "viewport_height": 2, "device_pixel_ratio": 1}},
    {"goal_evidence": [{"criterion_id": "notice_closed", "fact_indices": [99], "satisfied": True}]},
    {"candidates": [{"id": "close", "description": "Close", "semantic_target": "notice", "action": {"kind": "click", "screenshot_ref": "old-shot", "x": 1, "y": 1}}]},
    {"candidates": [{"id": "close", "description": "Close", "semantic_target": "notice", "action": {"kind": "click", "screenshot_ref": "shot-1", "x": 710, "y": 1}}]},
    {"candidates": [{"id": "wait", "description": "Wait", "semantic_target": "notice", "action": {"kind": "wait", "wait_ms": 5001}}]},
])
def test_observation_contract_rejects_invalid_bindings_and_evidence(bad):
    with pytest.raises(ValidationError):
        observation(**bad)


def test_unknown_runs_and_path_traversal_never_create_ledgers(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(StoreError):
        store.snapshot("f" * 32)
    with pytest.raises(StoreError):
        store.directory("../outside")
    assert list(tmp_path.iterdir()) == []


async def test_five_tools_have_a_protocol_driven_mock_closed_loop(tmp_path):
    fake = FakeClient()
    server = create_server(Settings(api_key="fake-test-key", run_root=tmp_path), client_factory=lambda settings: fake)
    async with Client(server, mode="legacy") as client:
        listed = await client.list_tools()
        assert {item.name for item in listed.tools} == {"start", "advance", "resume", "status", "finish"}
        result = await client.call_tool("start", {"request": task().model_dump(mode="json")})
        assert not result.is_error
        run_id = result.structured_content["run_id"]
        from jev_game_agent.contracts import TaskResult
        started = TaskResult.model_validate(result.structured_content)
        result = await client.call_tool("advance", {"request": advance(started).model_dump(mode="json")})
        assert not result.is_error and result.structured_content["type"] == "EXECUTE"
        status = await client.call_tool("status", {"request": {"run_id": run_id}})
        assert status.structured_content["phase"] == "AWAITING_EXECUTION"
        resumed = await client.call_tool("resume", {"request": {"run_id": run_id}})
        assert resumed.structured_content["type"] == "OBSERVE" and not resumed.structured_content["execute_allowed"]
        finished = await client.call_tool("finish", {"request": {"run_id": run_id, "reason": "cancelled", "explanation": "Mock loop ended"}})
        assert finished.structured_content["phase"] == "STOPPED"
        unknown = await client.call_tool("status", {"request": {"run_id": "f" * 32}})
        assert unknown.is_error


async def test_page_rebinding_requires_resume(setup):
    runtime, client, started = setup
    intent = await runtime.advance(advance(started))
    request = advance(intent, "shot-2", "new-page")
    request.observation.frame.page_id = "different-page"
    result = await runtime.advance(request)
    assert result.error.code == "invalid_feedback_or_binding"
    resumed = runtime.resume(started.run_id)
    request.context_version = resumed.context_version
    result = await runtime.advance(request)
    assert result.type == "ANALYZE" and result.help_category == "reconcile_execution"


async def test_three_waits_request_analysis_without_infinite_polling(tmp_path):
    client = FakeClient()
    runtime = Runtime(Store(tmp_path), client)
    current = runtime.start(task())
    candidates = [{"id": "wait", "description": "Wait briefly", "semantic_target": "page", "action": {"kind": "wait", "wait_ms": 100}}]
    for index in range(3):
        current = await runtime.advance(advance(current, f"s{index}", f"step-{index}", candidates=candidates))
        assert current.type == "OBSERVE"
    result = await runtime.advance(advance(current, "s3", "step-3", candidates=candidates))
    assert result.type == "ANALYZE" and result.help_category == "analyze_anomaly"
    assert len(client.requests) == 3


async def test_failed_export_does_not_lose_committed_execution_intent(setup, monkeypatch):
    import jev_game_agent.runtime as module
    runtime, client, started = setup

    def unavailable(*args):
        raise PermissionError("Cannot write export")

    monkeypatch.setattr(module, "export_run", unavailable)
    result = await runtime.advance(advance(started))
    assert result.type == "EXECUTE" and result.export_warning
    assert runtime.status(started.run_id).pending_execution["decision_id"] == result.decision_id


async def test_finish_records_last_feedback_without_claiming_completion(setup):
    runtime, client, started = setup
    intent = await runtime.advance(advance(started))
    finished = runtime.finish(FinishRequest(
        run_id=started.run_id, reason="stopped", explanation="Stop after reconciling the last operation",
        observation=observation("shot-2", complete=True),
        previous_execution={"decision_id": intent.decision_id, "submission_status": "accepted", "outcome": "success", "fact_indices": [0]},
    ))
    assert finished.phase == "STOPPED" and not finished.completion_evidence
    assert runtime.status(started.run_id).pending_execution is None
    assert runtime.status(started.run_id).metrics["visual_verifications"] == 1


async def test_partial_goal_evidence_and_other_origin_do_not_complete(setup):
    runtime, client, started = setup
    request = advance(started)
    request.observation.frame.url = "http://other.test/game/"
    assert (await runtime.advance(request)).error.code == "invalid_feedback_or_binding"
    started = runtime.start(task(completion_criteria=[{"id": "notice_closed", "description": "Notice absent"}, {"id": "menu_open", "description": "Menu open"}]))
    request = advance(started, complete=True)
    result = await runtime.advance(request)
    assert result.type != "COMPLETE"


def test_missing_client_does_not_create_a_run(tmp_path):
    runtime = Runtime(Store(tmp_path), None)
    result = runtime.start(task())
    assert result.type == "BLOCKED" and result.run_id is None
    assert list(tmp_path.iterdir()) == []


async def test_restart_without_key_keeps_read_only_access_but_blocks_decision(setup):
    runtime, client, started = setup
    replacement = Runtime(Store(runtime.store.root), None)
    assert replacement.status(started.run_id).phase == "NEED_OBSERVATION"
    result = await replacement.advance(advance(started))
    assert result.type == "BLOCKED" and result.error.service_error.category == "missing_key"
    assert replacement.status(started.run_id).metrics["http_attempts"] == 0


@pytest.mark.parametrize("action", [
    {"kind": "key", "screenshot_ref": "shot-1", "key": "Escape"},
    {"kind": "drag", "screenshot_ref": "shot-1", "from_x": 20, "from_y": 40, "to_x": 40, "to_y": 50},
])
async def test_other_typed_ui_operations_preserve_exact_parameters(setup, action):
    runtime, client, started = setup
    result = await runtime.advance(advance(started, candidates=[{
        "id": "operation", "description": "Fixture operation", "semantic_target": "page", "action": action,
    }]))
    assert result.type == "EXECUTE" and result.execute_allowed
    assert result.candidate.action.model_dump() == action
