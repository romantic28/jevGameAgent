"""Validate the real stdio lifecycle; visual evidence is explicitly simulated."""
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
    calls = []
    if live:
        environment = {name: os.environ[name] for name in (
            "TYPESAFE_API_KEY", "JEV_MODEL", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"
        ) if os.environ.get(name)}
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "jev_game_agent"], env=environment, cwd=project)
    else:
        parameters = StdioServerParameters(command=sys.executable,
            args=[str(project / "tests/fixtures/p1_stdio_server.py"), "--run-root", str(project / "run")], cwd=project)
    output.parent.mkdir(parents=True, exist_ok=True)
    async with Client(parameters, mode="legacy", read_timeout_seconds=30) as client:
        listed = await client.list_tools()
        names = {item.name for item in listed.tools}
        if names != {"start", "advance", "resume", "status", "finish"}:
            raise RuntimeError("The five P1 tools were not discovered")

        async def call(name, request):
            result = await client.call_tool(name, {"request": request})
            if result.is_error or result.structured_content is None:
                raise RuntimeError("A lifecycle tool rejected the fixture request")
            calls.append({"tool": name, "request": request, "result": result.structured_content})
            return result.structured_content

        started = await call("start", fixture["start"])
        if started["type"] != "OBSERVE":
            output.write_text(json.dumps({"live": live, "calls": calls}, indent=2), encoding="utf-8")
            print(json.dumps({"passed": False, "status": started["type"], "error": started["error"]}))
            return False
        before = {"run_id": started["run_id"], "submission_id": "fixture-1", "context_version": started["context_version"], "observation": fixture["before"]}
        intent = await call("advance", before)
        if intent["type"] == "BLOCKED" or not intent["decision_id"]:
            output.write_text(json.dumps({"passed": False, "live_jev": live, "visual_evidence": "simulation", "calls": calls}, indent=2), encoding="utf-8")
            print(json.dumps({"passed": False, "status": intent["type"], "error": intent["error"]}))
            return False
        duplicate = await call("advance", before)
        assert duplicate["decision_id"] == intent["decision_id"] and duplicate["replayed"] and not duplicate["execute_allowed"]
        resumed = await call("resume", {"run_id": started["run_id"]})
        after = {"run_id": started["run_id"], "submission_id": "fixture-2", "context_version": resumed["context_version"], "observation": fixture["after"]}
        if intent["type"] == "EXECUTE":
            after["previous_execution"] = {"decision_id": intent["decision_id"], "submission_status": "accepted", "outcome": "success", "fact_indices": [0]}
        completed = await call("advance", after)
        status = await call("status", {"run_id": started["run_id"]})
        assert completed["type"] == "COMPLETE" and status["phase"] == "COMPLETE"
        assert status["metrics"]["decision_requests"] == 1
        assert status["metrics"]["decision_responses"] == 1 and status["metrics"]["models"]
        # Exercise finish on a separate cancelled run, without asserting completion.
        cancelled_start = await call("start", fixture["start"])
        cancelled = await call("finish", {"run_id": cancelled_start["run_id"], "reason": "cancelled", "explanation": "Simulated fixture cancellation check"})
        assert cancelled["phase"] == "STOPPED"
        evidence = {
            "stage": "P1", "passed": True, "harness": "official_python_mcp_client", "desktop_host_acceptance": False,
            "live_jev": live, "visual_evidence": "simulation", "actual_ui_inputs": 0,
            "provenance": fixture["provenance"], "tools": sorted(names),
            "entry": {"command": parameters.command, "args": parameters.args},
            "server": client.server_info.model_dump(mode="json"), "protocol": client.protocol_version,
            "calls": calls, "metrics": status["metrics"],
        }
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "live_jev": live, "visual_evidence": "simulation", "actual_ui_inputs": 0,
                      "tools": sorted(names), "output": str(output.resolve()), "metrics": status["metrics"]}, ensure_ascii=False))
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use the fixed real Jev entry (one small decision); otherwise inject the test client")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.live, args.output)) else 1)
