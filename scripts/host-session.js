// Paste this file once into the documented browser REPL; it performs no input on load.
// Node's export below exists only for offline tests. Do not run UI from a shell.
function createJevHost({ runId, contextVersion, browserId, tab, cdp, now = Date.now }) {
  const copy = value => JSON.parse(JSON.stringify(value));
  const requireValue = (ok, message) => { if (!ok) throw new Error(message); };
  requireValue(typeof tab?.playwright?.evaluate === "function" &&
    typeof tab?.getAXStateAndScreenshot === "function" && typeof tab?.pressKey === "function" &&
    typeof cdp?.send === "function", "Prepare the documented browser adapter before advance");
  const frameKeys = ["browser_id", "page_id", "url", "screenshot_ref", "layout_ref",
    "viewport_width", "viewport_height", "device_pixel_ratio", "coordinate_space"];
  const geometryKeys = ["url", "viewport_width", "viewport_height", "device_pixel_ratio"];
  const seen = new Set();
  const timings = [];
  let version = contextVersion, task, frame, pending, sequence = 0, busy = false;

  async function geometry() {
    return tab.playwright.evaluate(() => ({
      url: location.href, viewport_width: innerWidth, viewport_height: innerHeight,
      device_pixel_ratio: devicePixelRatio,
    }));
  }

  async function capture() {
    const started = now();
    frame = undefined;
    const before = await geometry();
    // This documented API emits the image itself. Never print its returned bytes.
    const visual = await tab.getAXStateAndScreenshot();
    const after = await geometry();
    frame = undefined;
    requireValue(visual.screenshot?.length, "No screenshot; obtain fresh visual evidence");
    requireValue(geometryKeys.every(k => before[k] === after[k]), "Layout changed during capture; observe again");
    requireValue(Number.isInteger(after.viewport_width) && after.viewport_width > 0 &&
      Number.isInteger(after.viewport_height) && after.viewport_height > 0 &&
      Number.isFinite(after.device_pixel_ratio) && after.device_pixel_ratio > 0,
    "Invalid viewport geometry");
    const ref = `cua:${runId}:${browserId}:${tab.id}:${now()}:${++sequence}`;
    frame = { browser_id: browserId, page_id: tab.id, ...after,
      screenshot_ref: `${ref}:image`, layout_ref: `${ref}:layout`, coordinate_space: "viewport_css_pixels" };
    timings.push({ event: "capture", started_ms: started, completed_ms: now() });
    return copy(frame);
  }

  function accept(result) {
    requireValue(!busy, "An operation is still in progress");
    requireValue(result.run_id === runId && Number.isInteger(result.context_version) &&
      result.context_version >= version, "Wrong run or stale context version");
    if (result.context_version > version && result.type === "EXECUTE" &&
        result.decision_id !== pending?.decision_id) pending = undefined;
    task = copy(result);
    version = task.context_version;
    if (task.pending_execution === null) pending = undefined;
    timings.push({ event: "task_received_by_host", at_ms: now() });
    return decision();
  }

  function decision() {
    requireValue(task, "Accept the actual Runtime result first");
    return copy(Object.fromEntries(["type", "source", "run_id", "decision_id", "context_version",
      "observation_id", "candidate", "binding", "execute_allowed", "replayed"].map(k => [k, task[k]])));
  }

  async function execute() {
    requireValue(!busy && !pending, "Input is pending; submit fresh feedback or reconcile");
    requireValue(task?.type === "EXECUTE" && task.execute_allowed === true && task.replayed === false,
      "No fresh execution permission");
    requireValue(task.decision_id && !seen.has(task.decision_id), "Decision already attempted");
    requireValue(frame && frameKeys.every(k => frame[k] === task.binding?.[k]), "Binding changed; resume and reconcile");
    const action = copy(task.candidate.action);
    requireValue(["click", "drag", "key"].includes(action.kind), "Unsupported UI action; prepare an adapter before advance");
    requireValue(action.screenshot_ref === frame.screenshot_ref, "Stale action screenshot");
    const points = action.kind === "click" ? [[action.x, action.y]] : action.kind === "drag"
      ? [[action.from_x, action.from_y], [action.to_x, action.to_y]] : [];
    requireValue(points.every(([x, y]) => Number.isFinite(x) && Number.isFinite(y) &&
      x >= 0 && y >= 0 && x < frame.viewport_width && y < frame.viewport_height), "Coordinates outside viewport");
    requireValue(action.kind !== "click" || ["left", "right", "middle"].includes(action.button), "Invalid click button");
    requireValue(action.kind !== "key" || (typeof action.key === "string" && action.key.length > 0), "Invalid key");
    busy = true;
    try {
      const current = await geometry();
      requireValue(geometryKeys.every(k => current[k] === frame[k]), "Page geometry changed; resume and reconcile");
      // Fence BEFORE any awaited input; partial input or lost receipts must never be replayed.
      seen.add(task.decision_id);
      pending = { decision_id: task.decision_id, submission_status: "unknown" };
      frame = undefined;
      timings.push({ event: "ui_submission", at_ms: now() });
      if (action.kind === "key") {
        await tab.pressKey(null, action.key);
      } else {
        const drag = action.kind === "drag";
        const x = drag ? action.from_x : action.x, y = drag ? action.from_y : action.y;
        const button = drag ? "left" : action.button;
        await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button, clickCount: 1 });
        if (drag) {
          for (let step = 1; step <= 8; step++) {
            await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", button: "left", buttons: 1,
              x: x + (action.to_x - x) * step / 8, y: y + (action.to_y - y) * step / 8 });
          }
        }
        await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", button, clickCount: 1,
          x: drag ? action.to_x : x, y: drag ? action.to_y : y });
      }
      pending.submission_status = "accepted";
      timings.push({ event: "ui_receipt", at_ms: now() });
      const nextFrame = await capture();
      return { receipt: copy(pending), frame: nextFrame, timings: copy(timings.slice(-3)) };
    } finally {
      busy = false;
    }
  }

  function request(submissionId, evidence, verification) {
    requireValue(!busy, "An operation is still in progress");
    return buildJevAdvance({ runId, version, frame, pending }, submissionId, evidence, verification);
  }

  return { capture: async () => {
    requireValue(!busy, "An operation is still in progress");
    return capture();
  }, accept, decision, execute, request, timings: () => copy(timings) };
}

function buildJevAdvance({ runId, version, frame, pending }, submissionId, evidence, verification) {
    const copy = value => JSON.parse(JSON.stringify(value));
    const requireValue = (ok, message) => { if (!ok) throw new Error(message); };
    requireValue(frame, "Capture a fresh frame first");
    const observation = { ...copy(evidence), frame: copy(frame) };
    requireValue(Array.isArray(observation.facts) && observation.page_label, "Provide current facts and page_label");
    const indicesValid = indices => Array.isArray(indices) && indices.length > 0 &&
      indices.every(i => Number.isInteger(i) && i >= 0 && i < observation.facts.length);
    for (const obj of observation.objects || []) {
      requireValue(typeof obj.state === "string" && Object.keys(obj).every(k => ["id", "label", "state"].includes(k)),
        "objects use only id, label, state");
    }
    for (const item of observation.goal_evidence || []) {
      requireValue(typeof item.satisfied === "boolean" && indicesValid(item.fact_indices),
        "goal_evidence requires satisfied and current fact_indices");
    }
    for (const candidate of observation.candidates || []) {
      requireValue([candidate.id, candidate.semantic_target].every(value =>
        typeof value === "string" && /^[A-Za-z0-9_.:-]+$/.test(value)),
      "Candidate id and semantic_target must be ASCII identifiers");
      if (["click", "drag", "key"].includes(candidate.action.kind)) {
        requireValue(!candidate.action.screenshot_ref || candidate.action.screenshot_ref === frame.screenshot_ref,
          "Candidate references an older screenshot");
        candidate.action.screenshot_ref = frame.screenshot_ref;
      }
    }
    const result = { run_id: runId, submission_id: submissionId, context_version: version, observation };
    if (pending) {
      requireValue(verification && ["success", "failure", "unknown"].includes(verification.outcome),
        "Supply a visual assessment; input acceptance is not success");
      requireValue(["accepted", "rejected", "unknown"].includes(pending.submission_status), "Invalid receipt status");
      requireValue(verification.outcome !== "success" || pending.submission_status !== "rejected", "Rejected input cannot prove success");
      requireValue(!verification.fact_indices || (Array.isArray(verification.fact_indices) &&
        (verification.fact_indices.length === 0 || indicesValid(verification.fact_indices))), "Feedback references invalid facts");
      requireValue(verification.outcome !== "success" || indicesValid(verification.fact_indices), "Success needs current visual facts");
      requireValue(!verification.unresponsive || (pending.submission_status === "accepted" && verification.outcome === "failure"),
        "Unresponsive requires accepted input and visual failure");
      result.previous_execution = { ...pending, outcome: verification.outcome,
        fact_indices: verification.fact_indices || [], unresponsive: verification.unresponsive || false };
    } else {
      requireValue(!verification, "No local receipt; use the recovery contract for pending Runtime input");
    }
    return { request: result };
  }


if (typeof module !== "undefined") module.exports = { createJevHost, buildJevAdvance };
