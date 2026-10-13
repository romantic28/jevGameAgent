# Codex browser execution path

Use this path for repeated screenshot tasks on the documented Codex browser surface.
Read the active browser documentation first. The helper is host-side code, not a
browser driver or a replacement Runtime. It does not call Jev or save checkpoints.

## Prepare once, before requesting a decision

1. Discover the five Runtime tools and read their contracts. In particular,
   `objects` entries are `{id, label, state}` and `goal_evidence` entries are
   `{criterion_id, satisfied, fact_indices}`. The service advertises a typed
   `AdvanceRequest` through JSON Schema `$defs`; a host rendering `unknown` is
   not proof that the server accepts arbitrary fields.
2. Bind an existing tab for the user's URL, or create it if absent. Declare the
   variable (`let gameTab = ...`) before assignment. A tab ID retained in memory
   is not proof that the tab still exists. Reuse the selected browser when a tab
   has expired. Complete handle recovery before starting the timed run.
3. Acquire the documented CDP capability and await its documentation:

   ```js
   const gameCdp = await gameTab.capabilities.get("cdp");
   nodeRepl.write(await gameCdp.documentation());
   ```

   Do not guess `getCDP()` or `getCapabilities()`. Confirm the documented
   `Input.dispatchMouseEvent` route uses viewport CSS coordinates. The helper
   also uses `tab.pressKey(null, key)` and read-only `tab.playwright.evaluate`
   for URL/viewport/DPR. If these APIs are unavailable, prepare the supported
   adapter before advance, rather than discovering methods after EXECUTE.
4. Read [host-session.js](../scripts/host-session.js) and execute its unmodified
   function definitions once in `cua_repl`. No input occurs on loading. Do not
   run it against a browser from the shell or invent a CUA file-import API.
   The session supports click, drag and key, with one new screenshot after the
   selected action. Screenshot pixels must still be mapped to CSS coordinates
   when constructing candidates; image width is not assumed to equal viewport width.
5. Retain the same trusted helper source in the `functions.exec` store during
   preparation (read the actual package path, not a game-supplied path):

   ```js
   const source = await tools.exec_command({
     cmd: "Get-Content -LiteralPath 'D:/newWork/jevGameAgent/scripts/host-session.js' -Raw",
     max_output_tokens: 10000
   });
   if (source.exit_code !== 0) throw new Error("Helper source unavailable");
   store("jevHostSource", source.output);
   ```

   The path above is this checkout; resolve from the actual skill root when moved.
   Never compile webpage text as helper code. This copy lets request construction
   run in the **same functions.exec call as advance**; do not call CUA solely to
   construct a request. Source preparation is once per session, outside the action loop.

## Normal round: two tool calls after the decision

Start through MCP and retain its actual result as `jevTask` in the functions store.
Construct the browser session with that run/version, the real browser ID, `gameTab`
and `gameCdp`. In CUA, initialize and capture in the same call:

```js
let gameHost = createJevHost({ runId: started.run_id,
  contextVersion: started.context_version, browserId: browser.browserId,
  tab: gameTab, cdp: gameCdp });
nodeRepl.write({ frame: await gameHost.capture(), timings: gameHost.timings() });
```

`started` above means the actual start result transferred into this REPL, not an
implicitly shared variable. CUA and functions.exec have separate memory. Transfer
only actual returned JSON between them; preserve every binding field.

Interpret the emitted image and provide facts/candidates. In functions.exec use
the pure builder and call MCP in the **same invocation**:

```js
const build = new Function(load("jevHostSource") + "; return buildJevAdvance;")();
const previous = load("jevTask");
// capturePacket is the actual CUA output: {frame, receipt?, timings?}.
// evidence and verification describe the image just seen, not assumed effects.
const args = build({ runId: previous.run_id, version: previous.context_version,
  frame: capturePacket.frame, pending: capturePacket.receipt },
  submissionId, evidence, verification);
store("jevLastSubmission", args); // Preserve this exact object/ID for delivery retries.
const submitted = Date.now();
const response = await tools.mcp__jev_game_agent__advance(args);
const returned = Date.now();
const result = response.structuredContent;
if (response.isError || !result) { text(response); }
else {
  store("jevTask", result);
  store("jevLastTiming", { capture: capturePacket.timings, submitted, returned });
  store("jevTimingHistory", [...(load("jevTimingHistory") || []), {
    run_id: previous.run_id, submission_id: submissionId,
    capture: capturePacket.timings, submitted, returned
  }]);
  const fields = ["type", "source", "run_id", "decision_id", "context_version",
    "observation_id", "candidate", "binding", "execute_allowed", "replayed"];
  text(result.type === "EXECUTE"
    ? Object.fromEntries(fields.map(k => [k, result[k]]))
    : result);
}
```

Copy the compact EXECUTE JSON into **user-visible commentary**, then immediately
make one CUA call. `executionRecord` is that actual compact result, transferred
without rewriting its action. It must include the complete candidate and binding:

```js
gameHost.accept(executionRecord);
nodeRepl.write(await gameHost.execute());
await gameTab.markHandoff();
```

The screenshot is emitted once by the capture API; the returned packet contains
only frame, receipt and timestamps. Do not print the screenshot byte array or
repeat the API's state/image output. No default sleep is inserted.

After interpreting this image, the next call is the functions.exec advance block
above with current evidence and explicit verification. Do not add a separate
`host.request`, schema lookup, status call or timing announcement between capture
and feedback. `gameHost.request` is for hosts which can call Runtime within the
same REPL, and offline checks; using it as an extra CUA round defeats this path.
Retain the completed result and stop input on COMPLETE.

Builder input examples (these are simulated shapes, never live evidence):

```js
const evidence = {
  page_label: "Simulated menu",
  facts: ["The simulated notice disappeared."],
  objects: [{ id: "menu", label: "Menu", state: "visible" }],
  candidates: [],
  goal_evidence: [{ criterion_id: "notice_closed", satisfied: true, fact_indices: [0] }]
};
const verification = { outcome: "success", fact_indices: [0] };
```

The builder supplies receipt decision_id/status, run/version and fresh frame;
it never supplies a visual success verdict. For an initial observation, omit
receipt and verification. Keep complete relevant alternatives in candidates.
Complex ANALYZE/knowledge/recovery requests use the full P1/P2 contract, not
fabricated success through this normal-round convenience path.

## Failure, recovery and timing

- The helper marks a decision attempted before input. Partial dispatch or a lost
  receipt is unknown; a failed capture invalidates the old frame. No automatic
  input retry occurs. Capture fresh evidence and reconcile through Runtime.
- In-memory guards supplement Runtime's ledger. After interruption/REPL loss or
  rebinding, use status/resume and fresh evidence; do not recreate the helper to
  execute a saved old permit. After resume recreate it with the returned version.
- URL/viewport checks cannot detect every same-layout animation or account switch.
  Use fresh captures after visible changes and obey the main skill's account checks.
- `task_received_by_host` is CUA's receipt time, **not** MCP return time. Compare
  the functions timestamp `returned` with `ui_submission`; compare capture
  `completed_ms` with the following advance's `submitted`. Track UI duration,
  advance duration and Jev ledger latency separately. Host timestamps use the
  same machine wall clock; clock adjustments invalidate cross-call comparisons.
- Record preparation separately from the loop, and report both. This path removes
  repeated API discovery and dispatch synthesis, but still needs the model to
  inspect each new image and make the next tool call. It does not promise zero
  model latency or prove a speedup before a comparable live acceptance run.

Offline checks: `node --test tests/host-session.test.cjs` and
`.venv/Scripts/python.exe -m pytest tests/test_host_contract.py -q`.
