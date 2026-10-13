"""P2 context fixtures: no game interaction and no shared knowledge writes."""
import hashlib
import json
from pathlib import Path

import pytest
import httpx2
from pydantic import ValidationError

from jev_game_agent.context import ContextBuilder, execution_memory, payload_characters
from jev_game_agent.contracts import AdvanceRequest, Observation
from jev_game_agent.knowledge import KnowledgeLibrary
from jev_game_agent.runtime import Runtime
from jev_game_agent.store import Store, encode
from jev_game_agent.config import Settings
from jev_game_agent.decisions.typesafe import JevClient
from test_runtime import FakeClient, task, observation, advance


ROOT = Path(__file__).resolve().parents[1]


def library(tmp_path):
    return KnowledgeLibrary(ROOT / "knowledge", tmp_path / "work")


def seed(tmp_path, budget=12000, game="unknown"):
    client = FakeClient(["observe"] * 20)
    runtime = Runtime(Store(tmp_path / "run"), client, context_budget=budget)
    return runtime, client, runtime.start(task(game_id=game))


async def test_current_replaces_facts_missing_resource_is_historical(tmp_path):
    runtime, client, started = seed(tmp_path)
    one = await runtime.advance(advance(started, resources={"gold": {"value": 10.0, "unit": "枚", "source_text": "金币10枚"}}))
    await runtime.advance(advance(one, "shot-2", "step-2", page_label="menu", facts=["Menu is visible"], changes=["page changed"]))
    context = client.requests[-1].context
    assert context["observation"]["facts"] == ["Menu is visible"]
    assert context["observation"]["resources"] == {}
    assert context["historical_resources"][0]["reading"]["value"] == 10.0
    assert context["historical_resources"][0]["visibility"] == "historical_not_reobserved"
    assert context["historical_known"][0]["visibility"] == "historical_not_current"


@pytest.mark.parametrize("unit,delta", [("枚", -3.0), ("万枚", None)])
async def test_resource_delta_requires_same_unit(tmp_path, unit, delta):
    runtime, client, started = seed(tmp_path)
    one = await runtime.advance(advance(started, resources={"gold": {"value": 10.0, "unit": "枚", "source_text": "10枚"}}))
    await runtime.advance(advance(one, "shot-2", "step-2", resources={"gold": {"value": 7.0, "unit": unit, "source_text": "7" + unit}}, changes=["resource changed"]))
    context = client.requests[-1].context
    if delta is None:
        assert not context["resource_changes"]
        assert context["historical_resources"][0]["visibility"] == "historical_unit_changed"
    else:
        assert context["resource_changes"][0]["delta"] == delta


def test_failure_survives_six_window_and_success_releases(tmp_path):
    runtime, client, started = seed(tmp_path)
    with runtime.store.transaction(started.run_id) as db:
        runtime.store.event(db, "visual_verification", {"semantic_target": "tower", "outcome": "failure", "facts": ["locked"]})
        for n in range(9):
            runtime.store.event(db, "visual_verification", {"semantic_target": "other", "outcome": "success", "facts": [str(n)]})
        recent, blockers = execution_memory(db, 6)
        assert len(recent) == 6 and blockers[0]["latest_result"]["facts"] == ["locked"]
        runtime.store.event(db, "visual_verification", {"semantic_target": "tower", "outcome": "success", "facts": ["unlocked"]})
        assert execution_memory(db, 6)[1] == []


async def test_tool_rejection_is_retained_and_p1_fallback(tmp_path):
    runtime, client, started = seed(tmp_path)
    client.choices = iter(["close", "observe"])
    selected = await runtime.advance(advance(started))
    result = await runtime.advance(advance(selected, "shot-2", "step-2", {"decision_id": selected.decision_id, "submission_status": "rejected", "outcome": "unknown"}))
    assert client.requests[-1].context["active_blockers"][0]["kind"] == "tool_rejected"
    with runtime.store.transaction(started.run_id) as db:
        row = db.execute("SELECT seq,data FROM events WHERE kind='execution_rejected'").fetchone()
        data = json.loads(row[1]); data.pop("semantic_target")
        db.execute("UPDATE events SET data=? WHERE seq=?", (encode(data), row[0]))
        assert execution_memory(db, 6)[1][0]["semantic_target"] == "notice"


def test_announcement_ignores_global_goal_and_tower_is_local(tmp_path):
    kb = library(tmp_path)
    assert not kb.search("fengyu", observation(page_label="公告"), observation().candidates).items
    tower = observation(page_label="雾隐迷塔编队", unknowns=["挑战前置条件未知"])
    found = kb.search("fengyu", tower, tower.candidates)
    assert found.items and all(item["game_id"] == "fengyu" for item in found.items)
    assert any(item["ref"]["document"] == "systems/tower.md" for item in found.items)
    assert len(found.items) <= 6


def test_namespace_and_required_missing(tmp_path):
    kb = library(tmp_path)
    obs = observation(page_label="雾隐迷塔", knowledge_query={"required_refs": [{"document": "systems/tower.md", "heading": "不存在"}]})
    assert kb.search("fengyu", obs, obs.candidates).missing_refs
    assert not kb.search("another_game", obs, obs.candidates).items
    assert kb.search("unknown", obs, obs.candidates).errors


async def test_unknown_identity_requires_evidence_and_cannot_switch(tmp_path):
    runtime, client, started = seed(tmp_path)
    result = await runtime.advance(advance(started, knowledge_query={"terms": ["迷塔"]}))
    assert result.help_category == "identify_game" and not client.requests
    identified = await runtime.advance(advance(result, "shot-2", "step-2", game_identity={"game_id": "fengyu", "fact_indices": [0]}, page_label="公告"))
    assert client.requests[-1].context["game_identity"] == "fengyu"
    changed = await runtime.advance(advance(identified, "shot-3", "step-3", game_identity={"game_id": "other", "fact_indices": [0]}))
    assert changed.error.code == "invalid_feedback_or_binding"


async def test_protected_context_over_budget_never_calls_jev(tmp_path):
    runtime, client, started = seed(tmp_path)
    obs = {"facts": ["当前事实" * 900] * 4, "unknowns": ["必要未知信息"], "interpretations": []}
    result = await runtime.advance(advance(started, **obs))
    assert result.type == "ANALYZE" and result.help_category == "split_context"
    assert result.context_summary["required_characters"] > 12000 and not client.requests
    saved = json.loads((runtime.store.directory(started.run_id) / "state.json").read_text(encoding="utf-8"))
    assert saved["context_summary"] == result.context_summary


async def test_exact_unicode_budget_boundary_and_saved_context(tmp_path):
    runtime, client, started = seed(tmp_path)
    result = await runtime.advance(advance(started, facts=["当前界面：公告"] , unknowns=["账号阶段未知"]))
    request = client.requests[0]
    size = payload_characters(request, runtime.model)
    assert request.input_characters == size == result.context_summary["input_characters"]
    assert len(encode(request.context).encode("utf-8")) > len(encode(request.context))
    with runtime.store.transaction(started.run_id) as db:
        state = runtime.store.load(db)
        obs = observation(facts=["当前界面：公告"], unknowns=["账号阶段未知"])
        built = ContextBuilder(runtime.library, size).build(db, state, obs, obs.candidates, [], None, None, runtime.model)
        assert built.request is not None
        over = ContextBuilder(runtime.library, size - 1).build(db, state, obs, obs.candidates, [], None, None, runtime.model)
        assert over.request is None and over.help_category == "split_context"


async def test_old_p1_submission_defaults_still_replay(tmp_path):
    runtime, client, started = seed(tmp_path)
    req = advance(started)
    selected = await runtime.advance(req)
    old = req.model_dump(mode="json")
    for key in ("knowledge_drafts", "knowledge_conflicts"):
        old.pop(key)
    for key in ("resources", "knowledge_query", "game_identity"):
        old["observation"].pop(key)
    with runtime.store.transaction(started.run_id) as db:
        db.execute("UPDATE submissions SET request=?,input_hash=?", (encode(old), hashlib.sha256(encode(old).encode()).hexdigest()))
    replay = await runtime.advance(req)
    assert replay.replayed and replay.decision_id == selected.decision_id and len(client.requests) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_resource_nonfinite_rejected(value):
    with pytest.raises(ValidationError):
        observation(resources={"gold": {"value": value, "unit": "枚", "source_text": "bad"}})


async def test_budget_matches_actual_sdk_payload(tmp_path):
    runtime, fake, started = seed(tmp_path)
    await runtime.advance(advance(started, facts=["当前公告"] ))
    built = fake.requests[0]
    wire = []

    def handler(request):
        payload = json.loads(request.content)
        wire.append(payload)
        assert len(encode(payload)) == built.input_characters
        assert len(request.content.decode("utf-8")) == built.input_characters
        assert payload["state"]["observation"]["facts"] == ["当前公告"]
        return httpx2.Response(200, json={"model": "fixture", "usage": {"input_tokens": 1, "output_tokens": 1},
            "answers": {"next_operation": {"type": "choice", "choice": "observe", "confidence": 1.0,
                                           "probabilities": {key: float(key == "observe") for key in built.criteria}}}})

    client = JevClient(Settings(api_key="test-not-a-real-key"), transport=httpx2.MockTransport(handler))
    try:
        answer = await client.decide(built)
        assert answer.error is None and len(wire) == 1
    finally:
        await client.aclose()


async def test_old_active_failure_cannot_be_trimmed_for_budget(tmp_path):
    runtime, client, started = seed(tmp_path)
    with runtime.store.transaction(started.run_id) as db:
        runtime.store.event(db, "visual_verification", {"semantic_target": "locked_route", "outcome": "failure", "facts": ["必须保留的阻塞证据" * 1600]})
        for n in range(10):
            runtime.store.event(db, "visual_verification", {"semantic_target": "other", "outcome": "success", "facts": [str(n)]})
    result = await runtime.advance(advance(started))
    assert result.help_category == "split_context" and not client.requests
    assert result.context_summary["active_blocker_count"] == 1
    assert result.context_summary["recent_verified_count"] == 6
