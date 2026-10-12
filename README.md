# gameAgent

A portable skill package for visual game operation. The skill identifier is `game-agent`; its entry point is [SKILL.md](SKILL.md).

## Package contents

- `SKILL.md`: observation, decisions, individual actions, verification, checkpoints, and bug reporting.
- `AGENTS.md`: bundled instructions for game operations and recovery.
- `knowledge/overview.md`: the game index. Each game maintains its own gameplay, main-story guidance, and knowledge inclusion rules.
- `references/test-report.md`: instructions for preparing feedback from recorded run evidence.
- `run/`: runtime checkpoints, screenshots, reports, and bug evidence, grouped by run ID. The agent creates this directory when absent.

## Reuse in another project

Copy the entire `gameAgent` folder into the target project. Ask the agent to use it, for example:

```text
Use gameAgent/SKILL.md and its bundled AGENTS.md to operate the game.
Project root: <target project directory>
Game URL: <preview URL>
Goal: <requested outcome>
```

`SKILL.md` explicitly loads the bundled `AGENTS.md` for game operations. A nested `AGENTS.md` may only be discovered automatically for work inside its directory, so invoke the skill explicitly when operating a browser from elsewhere. A project-wide instruction file is optional; if needed, add a reference to this skill to the target project's existing instructions.

Resolve package resources relative to the folder containing `SKILL.md` (`<skill-root>`). The package may be placed in another directory; update the invocation path accordingly. `<project-root>` identifies the project being tested and is independent of the package location.

## Runtime requirements and data

The agent needs browser observation, screenshot, and input tools, plus project file access. This package does not supply browser tools, account credentials, or external services. Screenshots are the primary evidence for game state.

Keep account progress, checkpoints, screenshots, reports, and bug evidence under `<skill-root>/run/<run-id>/`. Before reading or writing run data, the agent creates `run/` if it does not exist, and creates a unique subdirectory for each new test. Establish each new test's starting state from the live game, even when copied run records are present. To reuse only the skill and knowledge, omit `run/` from the copy; the agent will recreate it. Follow the selected game's `maintenance.md` before updating knowledge; routine operations using existing guidance do not require new entries.
