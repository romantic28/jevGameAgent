"""P2 stdio acceptance: simulated observations, optional real Jev, zero UI input."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters


async def verify(live: bool, output: Path) -> bool:
    project = Path(__file__).resolve().parents[1]
    fixture = json.loads((project / "tests/fixtures/p1-lifecycle.json").read_text(encoding="utf-8"))
    environment = {key: os.environ[key] for key in (
        "TYPESAFE_API_KEY", "JEV_MODEL", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"
    ) if os.environ.get(key)} if live else None
    args = ["-m", "jev_game_agent"] if live else [str(project / "tests/fixtures/p1_stdio_server.py"), "--run-root", str(project / "run")]
    parameters = StdioServerParameters(command=sys.executable, args=args, env=environment, cwd=project)
    calls, checks = [], []
    evidence = {"stage": "P2", "live_jev": live, "visual_evidence": "simulation", "actual_ui_inputs": 0,
                "desktop_host_acceptance": False, "shared_knowledge_writes": 0, "calls": calls, "checks": checks}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with Client(parameters, mode="legacy", read_timeout_seconds=30) as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == {"start", "advance", "resume", "status", "finish"}
            evidence["server"] = client.server_info.model_dump(mode="json")
            checks.append("five_tools_discovered")

            async def call(name, request):
                result = await client.call_tool(name, {"request": request})
                assert not result.is_error and result.structured_content is not None
                calls.append({"tool": name, "request": request, "result": result.structured_content})
                return result.structured_content

            start = dict(fixture["start"], game_id="fengyu", goal="核对公告和迷塔编队条件（模拟观察）",
                         completion_criteria=[{"id": "inspection_complete", "description": "最终核对结果可见"}])
            current = await call("start", start)
            assert current["type"] == "OBSERVE"
            before = fixture["before"]
            before.update(page_label="公告", goal_evidence=[], resources={"gold": {"value": 1000.0, "unit": "枚", "source_text": "金币1000枚（模拟）"}},
                          candidates=[{"id": "observe", "description": "等待新观察", "semantic_target": "notice", "action": {"kind": "observe"}}])
            first_request = {"run_id": current["run_id"], "submission_id": "p2-notice", "context_version": current["context_version"], "observation": before}
            current = await call("advance", first_request)
            assert current["type"] == "OBSERVE" and current["context_summary"]["sent_knowledge_refs"] == []
            checks.append("notice_has_no_tower_guide")
            replay = await call("advance", first_request)
            assert replay["replayed"] and replay["decision_id"] == current["decision_id"] and not replay["execute_allowed"]
            checks.append("duplicate_has_no_call_or_execution")
            tower = json.loads(json.dumps(before))
            tower["frame"].update(screenshot_ref="p2-tower-simulation", layout_ref="p2-tower-layout")
            tower.update(page_label="雾隐迷塔编队", facts=["迷塔编队页显示进入条件（模拟）"], changes=["页面改变，金币减少100枚（模拟）"],
                         resources={"gold": {"value": 900.0, "unit": "枚", "source_text": "金币900枚（模拟）"}},
                         unknowns=["实际挑战解锁条件未知"], candidates=[{"id": "observe", "description": "继续核对编队条件", "semantic_target": "tower_formation", "action": {"kind": "observe"}}])
            current = await call("advance", {"run_id": current["run_id"], "submission_id": "p2-tower", "context_version": current["context_version"], "observation": tower})
            assert current["type"] == "OBSERVE"
            summary = current["context_summary"]
            assert any(ref["document"] == "systems/tower.md" for ref in summary["sent_knowledge_refs"])
            assert summary["resource_changes"][0]["delta"] == -100.0 and summary["input_characters"] <= 12000
            checks += ["tower_retrieves_relevant_sections", "same_unit_resource_delta", "serialized_character_budget"]
            final = json.loads(json.dumps(tower))
            final["frame"].update(screenshot_ref="p2-final-simulation", layout_ref="p2-final-layout")
            final.update(facts=["最终核对结果可见（模拟）"], goal_evidence=[{"criterion_id": "inspection_complete", "fact_indices": [0], "satisfied": True}])
            current = await call("advance", {"run_id": current["run_id"], "submission_id": "p2-complete", "context_version": current["context_version"], "observation": final})
            assert current["type"] == "COMPLETE"
            status = await call("status", {"run_id": current["run_id"]})
            assert status["metrics"]["decision_requests"] == status["metrics"]["decision_responses"] == 2
            evidence["metrics"] = status["metrics"]
            evidence["run_id"] = current["run_id"]
            checks += ["completion_stops_further_decisions", "two_decisions_and_zero_ui_inputs"]
            resumed = await call("resume", {"run_id": current["run_id"]})
            assert resumed["type"] == "COMPLETE"
            checks.append("completed_run_not_reopened")
            evidence["passed"] = True
    except (AssertionError, Exception):
        evidence["passed"] = False
        # Results already contain classified runtime errors; do not print SDK exception bodies.
        evidence["failure"] = "P2 acceptance check failed; inspect recorded structured results"
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: evidence.get(key) for key in ("passed", "live_jev", "actual_ui_inputs", "checks", "metrics", "run_id")}, ensure_ascii=False))
    return evidence["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.live, args.output)) else 1)
