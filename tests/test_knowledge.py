"""Knowledge maintenance is exercised only against temporary fixture libraries."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from jev_game_agent.contracts import AdvanceRequest, KnowledgeDraft, KnowledgeConflict
from jev_game_agent.contracts import FinishRequest
from jev_game_agent.context import ContextBuilder
from jev_game_agent.knowledge import KnowledgeLibrary, KnowledgeError, digest
from jev_game_agent.runtime import Runtime
from jev_game_agent.store import Store
from test_runtime import FakeClient, task, observation, advance


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "knowledge"
    game = root / "fengyu"
    (game / "systems").mkdir(parents=True)
    (game / "maintenance.md").write_bytes((ROOT / "knowledge/fengyu/maintenance.md").read_bytes())
    client = FakeClient(["observe"] * 30)
    runtime = Runtime(Store(tmp_path / "run"), client, knowledge_root=root)
    # live is a contract fixture declaration; there is no real UI or production KB here.
    started = runtime.start(task(game_id="fengyu", evidence_mode="live"))
    return runtime, client, started, game


def draft(path, **changes):
    values = dict(ref={"document": "systems/tower.md", "heading": "迷塔的进入条件"},
                  expected_document_hash=digest(path.read_bytes() if path.exists() else b""),
                  statement="挑战前在编队页核对开放条件。", visible_checks="编队页显示开放条件提示。",
                  scope={key: "未知" for key in ("environment", "build", "account_stage", "prerequisites", "privileges")},
                  status="observed", evidence_kind="visible_rule", fact_indices=[0],
                  admission_reason="new_mechanism", increment_reason="补充此前缺失的入口条件判断点。",
                  reusable=True, sensitive_content_removed=True, contains_account_progress=False)
    values.update(changes)
    return KnowledgeDraft.model_validate(values)


def req(state, item=None, ref="shot-1", submission="step-1", **changes):
    values = advance(state, ref, submission, page_label="迷塔编队", facts=["编队页显示开放条件提示。"], changes=["new evidence"]).model_dump(mode="json")
    values.update(changes)
    if item:
        values["knowledge_drafts"] = [item.model_dump(mode="json")]
    return AdvanceRequest.model_validate(values)


async def test_admit_portable_entry_no_extra_jev_and_duplicate(setup):
    runtime, client, started, game = setup
    path = game / "systems/tower.md"
    first = await runtime.advance(req(started, draft(path)))
    text = path.read_text(encoding="utf-8")
    assert "已观察" in text and "特权=未知" in text and "最后核实日期" in text
    assert started.run_id not in text and "shot-1" not in text and len(client.requests) == 1
    before = path.read_bytes()
    await runtime.advance(req(first, draft(path), "shot-2", "step-2"))
    assert path.read_bytes() == before and len(client.requests) == 2


@pytest.mark.parametrize("changes", [
    {"reusable": False}, {"contains_account_progress": True}, {"sensitive_content_removed": False},
    {"statement": "密码 password=secret"}, {"statement": "点 (543,125)"}, {"visible_checks": "D:/newWork/run/shot.jpg"},
    {"admission_reason": "main_story"}, {"evidence_kind": "verified_result", "status": "verified"},
])
async def test_invalid_admission_requests_analysis_without_write(setup, changes):
    runtime, client, started, game = setup
    path = game / "systems/tower.md"
    result = await runtime.advance(req(started, draft(path, **changes)))
    assert result.help_category == "resolve_knowledge" and not path.exists() and not client.requests


async def test_simulation_cannot_update_shared_knowledge(setup):
    runtime, client, started, game = setup
    simulated = runtime.start(task(game_id="fengyu"))
    path = game / "systems/tower.md"
    result = await runtime.advance(req(simulated, draft(path)))
    assert result.context_summary["knowledge_maintenance_issues"][0]["code"] == "non_live_evidence"
    assert not path.exists()


async def test_conflict_is_disputed_then_explicitly_resolved(setup):
    runtime, client, started, game = setup
    path = game / "systems/tower.md"
    first = await runtime.advance(req(started, draft(path)))
    conflicting = draft(path, statement="入口条件在主城核对，编队页不显示。")
    disputed = await runtime.advance(req(first, conflicting, "shot-2", "step-2"))
    assert disputed.help_category == "resolve_knowledge" and len(client.requests) == 1
    assert "有争议" in path.read_text(encoding="utf-8")
    questions = (game / "questions.md").read_text(encoding="utf-8")
    assert "旧结论" in questions and "新观察" in questions and started.run_id not in questions
    blocked = await runtime.advance(req(disputed, None, "shot-3", "step-3"))
    assert blocked.help_category == "resolve_knowledge" and len(client.requests) == 1
    correction = draft(path, operation="replace", admission_reason="correction", resolution_explanation="核对当前构建的界面说明，保留编队页条件核验并补充适用范围。", scope={key: "当前开发构建" if key == "build" else "未知" for key in ("environment", "build", "account_stage", "prerequisites", "privileges")})
    fixed = await runtime.advance(req(blocked, correction, "shot-4", "step-4"))
    assert fixed.type == "OBSERVE" and "有争议" not in path.read_text(encoding="utf-8")
    assert "已分析修订" in (game / "questions.md").read_text(encoding="utf-8")
    assert len(client.requests) == 2


async def test_explicit_conflict_contract_and_stale_concurrent_edit(setup):
    runtime, client, started, game = setup
    path = game / "systems/tower.md"
    one = await runtime.advance(req(started, draft(path)))
    stale = draft(path, operation="replace", admission_reason="correction", resolution_explanation="已分析当前说明。")
    path.write_text(path.read_text(encoding="utf-8") + "\n其他会话补充\n", encoding="utf-8")
    before = path.read_bytes()
    blocked = await runtime.advance(req(one, stale, "shot-2", "step-2"))
    assert blocked.help_category == "resolve_knowledge" and path.read_bytes() == before
    conflict = KnowledgeConflict(ref=stale.ref, expected_document_hash=digest(before), summary="当前提示与旧结论的适用范围冲突。", fact_indices=[0])
    result = await runtime.advance(req(blocked, None, "shot-3", "step-3", knowledge_conflicts=[conflict.model_dump(mode="json")]))
    assert result.help_category == "resolve_knowledge"
    assert "其他会话补充" in path.read_text(encoding="utf-8")


async def test_policy_change_disables_writes_but_not_retrieval(setup):
    runtime, client, started, game = setup
    maintenance = game / "maintenance.md"
    maintenance.write_text("# New rules\n", encoding="utf-8")
    path = game / "systems/tower.md"
    result = await runtime.advance(req(started, draft(path)))
    assert result.help_category == "resolve_knowledge" and not path.exists()


def test_policy_newlines_and_cross_game_index_isolation(setup):
    runtime, client, started, game = setup
    rules = game / "maintenance.md"
    rules.write_bytes(rules.read_bytes().replace(b"\r\n", b"\n"))
    assert runtime.library.check_policy("fengyu") == runtime.library.POLICY_HASH
    for game_id, statement in [("fengyu", "本游戏条件"), ("other", "另一个游戏的秘密规则")]:
        directory = runtime.library.root / game_id / "systems"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "tower.md").write_text(f"# 迷塔\n\n### 进入条件\n\n- 状态：已观察。\n- 结论：{statement}\n", encoding="utf-8")
    obs = observation(page_label="迷塔")
    found = runtime.library.search("fengyu", obs, obs.candidates)
    assert found.items and "另一个" not in str(found.items)


def test_hypothesis_remains_labeled_and_cache_rebuilds(setup):
    runtime, client, started, game = setup
    path = game / "systems/tower.md"
    path.write_text("# 迷塔\n\n### 入口条件\n\n- 状态：推测。\n- 结论：未核实。\n", encoding="utf-8")
    obs = observation(page_label="迷塔")
    assert runtime.library.search("fengyu", obs, obs.candidates).items[0]["status"] == "hypothesis"
    path.write_text(path.read_text(encoding="utf-8").replace("推测", "已验证"), encoding="utf-8")
    assert runtime.library.search("fengyu", obs, obs.candidates).items[0]["status"] == "verified"


async def test_verified_result_requires_current_visual_success(setup):
    runtime, client, started, game = setup
    client.choices = iter(["close", "observe"])
    selected = await runtime.advance(advance(started))
    path = game / "systems/tower.md"
    data = req(selected, draft(path, status="verified", evidence_kind="verified_result"), "shot-2", "step-2").model_dump(mode="json")
    data["previous_execution"] = {"decision_id": selected.decision_id, "submission_status": "accepted", "outcome": "success", "fact_indices": [0]}
    result = await runtime.advance(AdvanceRequest.model_validate(data))
    assert result.type == "OBSERVE" and "已验证" in path.read_text(encoding="utf-8")


def test_journal_recovers_partial_two_file_publication(setup):
    runtime, client, started, game = setup
    path = game / "systems/tower.md"
    target = "# 迷塔\n\n### 入口\n\n- 状态：有争议。\n"
    questions = "# 待分析\n"
    changes = [{"document": "systems/tower.md", "before_hash": digest(b""), "content": target},
               {"document": "questions.md", "before_hash": digest(b""), "content": questions}]
    path.write_text(target, encoding="utf-8", newline="\n")  # Crash after first file, before questions publication.
    directory = runtime.store.root / "knowledge-journal/fengyu"
    directory.mkdir(parents=True)
    job = {"game_id": "fengyu", "changes": changes, "evidence": {}, "status": "prepared"}
    journal = directory / "fixture.json"
    journal.write_text(json.dumps(job), encoding="utf-8")
    runtime.library.recover_jobs("fengyu")
    assert (game / "questions.md").read_text(encoding="utf-8") == questions
    assert json.loads(journal.read_text(encoding="utf-8"))["status"] == "published"
    runtime.library.recover_jobs("fengyu")
    assert path.read_text(encoding="utf-8") == target


async def test_publication_recovers_sqlite_rollback_without_duplicate(setup):
    runtime, client, started, game = setup
    path = game / "systems/tower.md"
    item = draft(path)
    obs = observation()
    with pytest.raises(RuntimeError):
        with runtime.store.transaction(started.run_id) as db:
            state = runtime.store.load(db)
            runtime.observe(db, state, obs)
            assert not runtime.library.maintain(db, runtime.store, state, obs, [item], [], started.run_id + ":retry")
            raise RuntimeError("fixture crash before SQLite commit")
    before = path.read_bytes()
    with runtime.store.transaction(started.run_id) as db:
        state = runtime.store.load(db)
        runtime.observe(db, state, obs)
        assert not runtime.library.maintain(db, runtime.store, state, obs, [item], [], started.run_id + ":retry")
    assert path.read_bytes() == before


async def test_optional_knowledge_trimmed_whole_required_never_truncated(setup):
    runtime, client, started, game = setup
    path = game / "systems/tower.md"
    path.write_text("# 迷塔\n\n### 超长规则\n\n- 状态：已观察。\n" + "这是历史规则。" * 3000, encoding="utf-8")
    result = await runtime.advance(req(started))
    assert result.type == "OBSERVE" and not client.requests[-1].context["knowledge"]
    assert result.context_summary["trimmed"][0]["kind"] == "optional_knowledge"
    data = req(result, None, "shot-2", "step-2").model_dump(mode="json")
    data["observation"]["knowledge_query"] = {"required_refs": [{"document": "systems/tower.md", "heading": "超长规则"}]}
    blocked = await runtime.advance(AdvanceRequest.model_validate(data))
    assert blocked.help_category == "split_context" and len(client.requests) == 1


def test_bad_ref_and_invalid_evidence_are_rejected(setup):
    runtime, client, started, game = setup
    with pytest.raises(KnowledgeError):
        runtime.library.document_path("fengyu", "../other/systems/tower.md")
    with pytest.raises(ValidationError):
        req(started, draft(game / "systems/tower.md", fact_indices=[50]))


def test_finish_admits_draft_and_reports_no_goal_success(setup):
    runtime, client, started, game = setup
    path = game / "systems/tower.md"
    result = runtime.finish(FinishRequest(run_id=started.run_id, reason="stopped", explanation="fixture stop", observation=observation(), knowledge_drafts=[draft(path)]))
    assert result.phase == "STOPPED" and result.type == "BLOCKED" and not result.completion_evidence
    assert path.exists() and not client.requests
    report = json.loads((runtime.store.directory(started.run_id) / "report.json").read_text(encoding="utf-8"))
    assert report["stage"] == "P2" and report["state"]["status"] == "stopped"


def test_prepared_journal_preserves_external_edits(setup):
    runtime, client, started, game = setup
    path = game / "systems/tower.md"
    path.write_text("external concurrent edit", encoding="utf-8")
    job = {"game_id": "fengyu", "status": "prepared", "changes": [{"document": "systems/tower.md", "before_hash": digest(b""), "content": "proposed replacement"}], "evidence": {}}
    journal = runtime.store.root / "recovery-fixture.json"
    journal.write_text(json.dumps(job), encoding="utf-8")
    with pytest.raises(KnowledgeError):
        runtime.library.apply_job(journal, job)
    assert path.read_text(encoding="utf-8") == "external concurrent edit"
    assert json.loads(journal.read_text(encoding="utf-8"))["status"] == "prepared"
