"""Exercise the fixed stdio entry point. This is NOT desktop-host acceptance."""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp import Client, StdioServerParameters


async def verify(live: bool, output: Path) -> bool:
    project = Path(__file__).resolve().parents[1]
    env = {"TYPESAFE_API_KEY": os.environ.get("TYPESAFE_API_KEY", "") if live else ""}
    if os.environ.get("JEV_MODEL"):
        env["JEV_MODEL"] = os.environ["JEV_MODEL"]
    # Pass connection settings by name without printing their values.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "jev_game_agent"], cwd=project, env=env,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    async with Client(parameters, mode="legacy", read_timeout_seconds=150) as client:
        tools = await client.list_tools()
        check = await client.call_tool("start", {"request": {"mode": "self_check", "repetitions": 0}})
        if check.is_error or check.structured_content is None:
            raise RuntimeError("The MCP self-check failed")
        calls = check.structured_content
        if live and calls["client_ready"]:
            result = await client.call_tool("start", {"request": {"mode": "self_check", "repetitions": 10}})
            if result.is_error or result.structured_content is None:
                raise RuntimeError("The MCP text probe failed")
            calls = result.structured_content
        evidence = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "harness": "official_python_mcp_client",
            "desktop_host_acceptance": False,
            "live": live,
            "fixed_entry": {"command": sys.executable, "args": parameters.args, "cwd": str(project)},
            "server": client.server_info.model_dump(mode="json") if client.server_info else None,
            "protocol_version": client.protocol_version,
            "tools": [tool.model_dump(mode="json") for tool in tools.tools],
            "local_check": check.structured_content,
            "report": calls,
        }
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "output": str(output.resolve()), "transport": "stdio", "live": live,
        "tools": [tool.name for tool in tools.tools],
        "status": calls["status"], "successes": calls["successful_repetitions"],
        "error": calls["error"], "desktop_host_acceptance": False,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return (calls["successful_repetitions"] == 10) if live else (
        check.structured_content["error"]["category"] == "missing_key" and "start" in {tool.name for tool in tools.tools}
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Make 10 real, small Jev requests")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.live, args.output)) else 1)


if __name__ == "__main__":
    main()
