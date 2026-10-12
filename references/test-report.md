# Game Test Report

Turn a completed or interrupted visual game test into a concise, evidence-based Markdown report. Use the active run's `state.json` and, when present, `bugs.json` as the source of truth. Do not replay the game or alter the test state to prepare a report.

## Workflow

1. Read the active run checkpoint and bug findings. Confirm they belong to the requested test and use their recorded run ID and scope.
2. Separate the user's test objective from the outcome. State whether the objective was completed, blocked, or stopped based on the checkpoint's visible completion evidence.
3. Report every recorded finding with its ID, category, confirmation status, trigger, expected behavior and basis, actual behavior, impact on the goal, reproduction results, and relevant console evidence.
4. Distinguish confirmed bugs from suspected issues, tool failures, and transient observations. Do not upgrade uncertainty or infer root cause beyond the recorded evidence.
5. Describe screenshot evidence accurately. Link archived files when available; if screenshots were only displayed during the conversation, state that they were not saved as files.
6. Match the report language to the user's request. Keep this skill and its templates in English; reports may use another language when requested or when it best serves the user.
7. Save the report as `report.md` in the same run directory unless the user specifies another path. Preserve `state.json` and `bugs.json` unchanged.
8. Finish with a brief response linking the report and stating the test outcome.

## Report structure

Use only sections supported by the run data:

```markdown
# Game Test Report

## Outcome
[Objective, completion status, and short result.]

## Test scope
[Target, environment/account labels when useful, and requested destination.]

## Findings
### [ID] [Title]
- Category / status:
- Steps to reproduce:
- Expected (basis):
- Actual:
- Evidence:
- Impact:

## Coverage notes
[What was reached and what was not exercised. Avoid claiming the game is bug-free.]
```

## Reporting rules

- Do not include passwords, tokens, session objects, or unrelated personal information.
- Preserve exact error messages and relevant timestamps; format timestamps in the user's stated timezone when known.
- A console error confirms a bug only when the recorded evidence connects it to the action and the observed failure. Otherwise label the finding suspected.
- If the report contains no findings, say that none were observed on the exercised path; do not claim that no bugs exist.
- Do not present an unarchived screenshot as a downloadable artifact. Identify it as conversation evidence and state that it was not archived.
- Keep recommendations separate from observed facts. Do not assign severity or propose code fixes unless the user asks or the source data provides them.
