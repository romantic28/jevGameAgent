"""Preview or install only the jev-game-agent MCP entry. Never writes an API key."""
import argparse
import json
import os
import tomllib
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    executable = project / ".venv" / "Scripts" / "python.exe"
    if not executable.is_file():
        raise SystemExit("The project virtual environment is missing")
    codex_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    target = codex_root / "config.toml"
    entry = {
        "command": str(executable), "args": ["-m", "jev_game_agent"], "cwd": str(project),
        "env_vars": ["TYPESAFE_API_KEY", "JEV_MODEL", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"],
        "startup_timeout_sec": 30, "tool_timeout_sec": 150,
    }
    original = target.read_bytes() if target.exists() else b""
    config = tomllib.loads(original.decode("utf-8-sig"))
    existing = config.get("mcp_servers", {}).get("jev-game-agent")
    if existing is not None and existing != entry:
        raise SystemExit("An existing jev-game-agent entry differs; refusing to overwrite it")
    print(json.dumps({"target": str(target), "entry": entry, "apply": args.apply, "already_present": existing is not None}, indent=2))
    if not args.apply or existing is not None:
        return
    lines = ["", "[mcp_servers.jev-game-agent]"]
    lines.extend(f"{key} = {json.dumps(value)}" for key, value in entry.items())
    updated = original + ("\n".join(lines) + "\n").encode("utf-8")
    tomllib.loads(updated.decode("utf-8-sig"))
    # Refuse a concurrent edit rather than replacing another tool's changes.
    if target.exists() and target.read_bytes() != original:
        raise SystemExit("Configuration changed during preparation; retry")
    temporary = target.with_name("config.toml.jev-game-agent.tmp")
    with temporary.open("xb") as handle:
        handle.write(updated)
    temporary.replace(target)
    print("Registered jev-game-agent. Desktop tool discovery still requires host reload verification.")


if __name__ == "__main__":
    main()
