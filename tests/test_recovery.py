"""Crash windows and actual stdio subprocess restart, using simulated scenes."""
import json
import sys
from pathlib import Path

import httpx2
import pytest
from mcp import Client, StdioServerParameters

from jev_game_agent.config import Settings
from jev_game_agent.contracts import TaskResult
from jev_game_agent.decisions.typesafe import JevClient
from jev_game_agent.runtime import Runtime
from jev_game_agent.store import Store
from test_runtime import FakeClient, advance, task


class SimulatedPowerLoss(BaseException):
    pass


async def test_response_received_before_commit_is_recovered_as_unresolved(tmp_path, monkeypatch):
    fake = FakeClient()
    runtime = Runtime(Store(tmp_path), fake)
    started = runtime.start(task())
    request = advance(started)

    def lose_power(*args):
        raise SimulatedPowerLoss()

    monkeypatch.setattr(runtime, "cache", lose_power)
    with pytest.raises(SimulatedPowerLoss):
        await runtime.advance(request)
    assert len(fake.requests) == 1
    restarted = Runtime(Store(tmp_path), FakeClient())
    status = restarted.status(started.run_id)
    assert status.phase == "DECIDING"
    assert status.metrics["decision_responses"] == 0
    assert status.metrics["execution_intents"] == 0
    assert (await restarted.advance(request)).error.code == "request_in_progress"
    resumed = restarted.resume(started.run_id)
    assert resumed.type == "OBSERVE" and not resumed.execute_allowed
    # No claim of exactly-once provider charging: the prior external answer was lost.


async def test_intent_committed_before_delivery_does_not_replay(tmp_path, monkeypatch):
    runtime = Runtime(Store(tmp_path), FakeClient())
    started = runtime.start(task())
    request = advance(started)

    def lose_power(*args):
        raise SimulatedPowerLoss()

    monkeypatch.setattr(runtime, "publish", lose_power)
    with pytest.raises(SimulatedPowerLoss):
        await runtime.advance(request)
    restarted = Runtime(Store(tmp_path), FakeClient())
    assert restarted.status(started.run_id).phase == "AWAITING_EXECUTION"
    repeated = await restarted.advance(request)
    assert repeated.replayed and not repeated.execute_allowed
    resumed = restarted.resume(started.run_id)
    assert resumed.pending_execution["status"] == "unknown"
    assert resumed.binding is None and resumed.candidate is None


async def test_actual_process_restart_then_visual_reconciliation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(root / "tests/fixtures/p1_stdio_server.py"), "--run-root", str(tmp_path)], cwd=root)
    async with Client(parameters, mode="legacy") as client:
        result = await client.call_tool("start", {"request": task().model_dump(mode="json")})
        started = TaskResult.model_validate(result.structured_content)
        request = advance(started)
        first = await client.call_tool("advance", {"request": request.model_dump(mode="json")})
        intent = TaskResult.model_validate(first.structured_content)
        assert intent.type == "EXECUTE"
    # A different subprocess reloads the same run's SQLite ledger.
    async with Client(parameters, mode="legacy") as client:
        status = await client.call_tool("status", {"request": {"run_id": started.run_id}})
        assert status.structured_content["phase"] == "AWAITING_EXECUTION"
        duplicate = await client.call_tool("advance", {"request": request.model_dump(mode="json")})
        assert duplicate.structured_content["replayed"] and not duplicate.structured_content["execute_allowed"]
        recovered = await client.call_tool("resume", {"request": {"run_id": started.run_id}})
        resumed = TaskResult.model_validate(recovered.structured_content)
        missing = await client.call_tool("advance", {"request": advance(resumed, "shot-2", "after-restart").model_dump(mode="json")})
        unresolved = TaskResult.model_validate(missing.structured_content)
        assert unresolved.type == "ANALYZE"
        feedback = {"decision_id": intent.decision_id, "submission_status": "unknown", "outcome": "success", "fact_indices": [0]}
        finished = await client.call_tool("advance", {"request": advance(unresolved, "shot-3", "verified", feedback, complete=True).model_dump(mode="json")})
        assert finished.structured_content["type"] == "COMPLETE"
        status = await client.call_tool("status", {"request": {"run_id": started.run_id}})
        assert status.structured_content["metrics"]["decision_requests"] == 1


async def test_real_sdk_adapter_maps_typed_runtime_choice_and_persists_raw_answer(tmp_path):
    outgoing = []

    def handler(request):
        payload = json.loads(request.content)
        outgoing.append(payload)
        criteria = payload["questions"]["next_operation"]["criteria"]
        assert payload["state"]["task"]["goal"] == "Close the notice"
        assert "interpretations" in payload["state"]["observation"]
        return httpx2.Response(200, json={
            "model": "sdk-mock-version", "usage": {"input_tokens": 200, "output_tokens": 20},
            "answers": {"next_operation": {"type": "choice", "choice": "close", "confidence": 0.8,
                       "probabilities": {key: float(key == "close") for key in criteria}}},
        })

    client = JevClient(Settings(api_key="mock-not-real"), transport=httpx2.MockTransport(handler))
    runtime = Runtime(Store(tmp_path), client)
    try:
        result = await runtime.advance(advance(runtime.start(task())))
        assert result.candidate.id == "close" and result.execute_allowed
        status = runtime.status(result.run_id)
        assert status.metrics["models"] == ["sdk-mock-version"]
        db = runtime.store.open(result.run_id, readonly=True)
        try:
            answer = json.loads(db.execute("SELECT response FROM decisions").fetchone()[0])
            assert answer["input_tokens"] == 200 and answer["output_tokens"] == 20
            assert "mock-not-real" not in json.dumps(outgoing)
        finally:
            db.close()
    finally:
        await client.aclose()
