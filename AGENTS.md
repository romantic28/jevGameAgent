# Project instructions

These instructions are bundled with the gameAgent skill. `<skill-root>` is the directory containing this file and `SKILL.md`; `<project-root>` is the project being tested. Resolve package resources and run data relative to `<skill-root>`.

## Visual game testing

These instructions apply when operating or testing the game through its browser preview. They do not initiate game actions during unrelated project work.

- Before starting or resuming game operations, including after context compaction or interruption, read `<skill-root>/SKILL.md` from disk. A shortened task summary does not replace the skill's workflow requirements.
- Before reading or writing run data, create `<skill-root>/run/` if it does not exist. For a continuation, load the exact corresponding `<skill-root>/run/<run-id>/state.json` and capture a fresh screenshot to reconcile progress and pending actions. For an independent new test, create a separate run directory as specified by the skill; do not inherit another run's progress.
- Preserve the user's overall goal, completion condition, and constraints. Stop when that goal is visually verified; newly unlocked tasks do not extend the scope.
- Read `<skill-root>/knowledge/overview.md` when starting or resuming game testing. Identify the current game from the user request and live page, then read only that game's `<skill-root>/knowledge/<game-id>/overview.md` and system knowledge relevant to the current subgoal. Maintain knowledge under that game's directory using its `maintenance.md`; do not apply Fengyu knowledge to another game. Shared knowledge must not transfer account progress or override current observations, user constraints, or the skill.
- Before every game input, output a structured decision JSON in commentary based on the latest screenshot. Then execute one action using the tool's documented arguments, obtain a fresh screenshot to verify the result, and update the run checkpoint. Natural-language updates and previous decisions do not replace this action's JSON.
- During observation and verification, actively check interaction, state, display, and flow anomalies, including nonblocking issues. Record and report findings under the skill's bug workflow; after two verified unresponsive clicks, inspect available console logs before further attempts.
- On handoff or compaction, retain the skill path, exact run checkpoint path, user goal, pending verification, and the requirement to restore this procedure before further game input.

Keep detailed game procedures in the skill and test progress in the run checkpoint; do not accumulate run-specific progress in this file.
