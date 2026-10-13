# jevGameAgent

A screenshot-based game testing skill with a Jev MCP decision runtime. The skill identifier is `game-agent`; begin with [SKILL.md](SKILL.md) and its bundled [AGENTS.md](AGENTS.md).

**Experimental branch; overall workflow acceptance failed (2026-10-13).** Jev integration and Runtime component checks passed, but live single-step runs retained seconds of host handoff delay and did not establish continuous AI gameplay/testing or an advantage over the original visual workflow. This work is archived on `experiment/jev-runtime-workflow-20261013`, with no merge into main. See the [development and outcome record](docs/workflow-experiment-20261013.md) and [timing evidence](docs/workflow-experiment-20261013-evidence.json). Earlier phase acceptance below describes component checks, not overall product acceptance.

P0, P1 and P2 have passed their recorded acceptance, including 109 automated checks and desktop MCP verification. This P3 update migrates the three host instruction files to the accepted Runtime contracts. P3's real screenshot task, tower challenge and building relocation acceptance remain pending. See [development plan](docs/development-plan.md), [architecture](docs/architecture.md) and [P2 acceptance](docs/p2-acceptance.md).

## How the host loop works

The host captures and interprets a fresh screenshot, supplies relevant complete candidates to advance, displays the returned task, executes one permitted operation, and sends fresh visual feedback. Runtime supplies Jev with the fixed task, current situation, action memory and per-game knowledge, and saves all decisions and execution records.

- start creates a run with URL, goal, completion criteria and constraints; self_check remains available for deployment checks.
- advance records the current observation, settles prior execution and returns EXECUTE, OBSERVE, ANALYZE, COMPLETE or BLOCKED.
- resume fences old bindings and requests fresh reconciliation; status reads the recorded state.
- finish explicitly cancels, stops or blocks a run without asserting success. Goal completion is proved through advance.

Only EXECUTE with execute_allowed=true and replayed=false permits its exact candidate, once per decision_id. Duplicate delivery and recovery never permit replaying unknown UI input. The host does not call Jev through shell scripts, choose a substitute action, write intermediate input JSON or maintain a parallel checkpoint. Details: [P1 lifecycle contracts](docs/p1-contracts.md) and [P2 context/knowledge additions](docs/p2-contracts.md).

Repeated Codex browser rounds can use the tested [host fast path](references/host-fast-path.md): a reusable session helper checks bindings and duplicate decisions, dispatches click/drag/key input and captures the result within one CUA call. Its pure request builder runs inside the same call as MCP advance. It records host timestamps and requires an explicit visual assessment before reporting success. This reduces repeated tool setup and parameter construction; real-game latency improvement still requires live measurement. Node is needed only for the host helper's offline tests; the Python Runtime has no new dependency.

The MCP service already publishes the full AdvanceRequest JSON Schema. Its tool description now also spells out frequently missed input fields for hosts that render the nested request as unknown. Existing connected hosts need to reload the MCP service to receive the updated description; source changes do not replace a running process.

## Setup and reuse

Python 3.12 or later, the installed project package, the registered MCP service with TYPESAFE_API_KEY, and the platform's screenshot/input capability are required. The Runtime does not provide browser control or account credentials. The host must read its active browser documentation and bind to the user's exact preview URL.

For a Windows checkout, install from this folder and preview the service registration:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e .
.venv/Scripts/python.exe scripts/register_mcp.py
```

The fixed service entry is the project's virtual-environment Python with `-m jev_game_agent`; configuration examples and deployment evidence are described in [P0 setup](docs/p0-acceptance.md). Configure the key in the service environment, never in skill text, run evidence or committed configuration. The registration script's --apply option writes the host's Codex config and must obey its filesystem permissions; it refuses a conflicting existing entry. Reload the MCP host after changing its entry and confirm the active service/tool contracts. No setup command is executed merely by reading this README.

When moving or copying the package, recreate the environment, install from the new path and update the service entry accordingly; confirm status.ledger_path points to the intended package. An existing registration may still refer to the earlier checkout. Other hosts need compatible stdio MCP plus their own documented screenshot and input tools.

Example invocation:

```text
Use <package-path>/SKILL.md and its bundled AGENTS.md to operate the game.
Project root: <target project directory>
Game URL: <preview URL>
Goal: <requested outcome>
```

## Package resources and records

- SKILL.md and AGENTS.md define the host procedure, scope, recovery and findings rules.
- src/jev_game_agent and the installed entry provide the Runtime; docs/p1-contracts.md and docs/p2-contracts.md define tool fields.
- knowledge/ is isolated by game. Runtime retrieves relevant Markdown sections; maintenance.md governs qualifying evidence-based drafts and conflict analysis.
- run/<run_id>/runtime.sqlite3 is authoritative. Runtime creates the run and exports state.json/report.json; the host must not edit these exports. It can separately save screenshots, bugs.json and report.md through supported tools.
- references/test-report.md describes human-readable QA reports. For Runtime runs, use ledger-backed status and exports; legacy manual checkpoints remain historical evidence.

Runtime normally retains the last six verified outcomes and active failures, marks old facts/resources as historical, and protects essential information under a default 12,000-character budget. Required content over budget returns ANALYZE. Shared knowledge cannot inherit account progress or authorize operations. Write a draft only for a reusable increment, with current evidence and scope; routine completion requires no knowledge update.

Keep the exact run_id for continuation. A new independent goal starts a new run and determines account state from current screenshots. To distribute only the package and knowledge, omit run/ and .venv/ and reinstall at the destination; Runtime recreates its storage. Do not treat copied records as the new account's progress.
