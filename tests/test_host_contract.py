"""Cross-language host requests must satisfy the actual MCP input contract."""
import json
from pathlib import Path
import shutil
import subprocess

from mcp import Client
import pytest

from jev_game_agent.config import Settings
from jev_game_agent.contracts import AdvanceRequest
from jev_game_agent.server import create_server


def test_host_requests_match_runtime_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for host JavaScript contract checks")
    script = Path(__file__).with_name("host-session.test.cjs")
    output = subprocess.check_output([node, str(script), "--contracts"], text=True)
    requests = json.loads(output)
    before = AdvanceRequest.model_validate(requests["before"]["request"])
    after = AdvanceRequest.model_validate(requests["after"]["request"])
    assert before.context_version == 0
    assert before.observation.candidates[0].action.screenshot_ref == before.observation.frame.screenshot_ref
    assert after.context_version == 1
    assert after.previous_execution.outcome == "success"
    assert after.observation.goal_evidence[0].satisfied


async def test_mcp_advertises_full_advance_schema_and_rejects_missing_evidence_fields(tmp_path):
    async with Client(create_server(Settings(run_root=tmp_path)), mode="legacy") as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "advance")
        schema = tool.input_schema
        assert schema["properties"]["request"]["$ref"] == "#/$defs/AdvanceRequest"
        assert {"run_id", "submission_id", "context_version", "observation"} <= set(schema["$defs"]["AdvanceRequest"]["required"])
        assert set(schema["$defs"]["VisibleObject"]["properties"]) == {"id", "label", "state"}
        assert "satisfied" in schema["$defs"]["GoalEvidence"]["required"]
        fixture = json.loads((Path(__file__).parent / "fixtures/p1-lifecycle.json").read_text())
        observation = fixture["after"]
        del observation["goal_evidence"][0]["satisfied"]
        result = await client.call_tool("advance", {"request": {
            "run_id": "a" * 32, "submission_id": "invalid", "context_version": 1, "observation": observation,
        }})
        assert result.is_error
        assert not list(tmp_path.iterdir())  # Rejection happens before any run or Jev request.
