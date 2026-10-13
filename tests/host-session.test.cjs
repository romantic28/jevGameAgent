const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createJevHost } = require('../scripts/host-session.js');

function fixture() {
  const sent = [];
  let geometry = { url: 'http://example.test/game/', viewport_width: 710, viewport_height: 892, device_pixel_ratio: 1 };
  let failInput = false, failCapture = false, counter = 0;
  const tab = {
    id: 'simulation', playwright: { evaluate: async () => ({ ...geometry }) },
    getAXStateAndScreenshot: async () => {
      if (failCapture) throw Error('capture interrupted');
      sent.push('capture'); return { screenshot: new Uint8Array([1]) };
    },
    pressKey: async (...args) => sent.push(args),
  };
  const host = createJevHost({ runId: 'a'.repeat(32), contextVersion: 0,
    browserId: 'simulation', tab, cdp: { send: async (method, params) => {
      sent.push(params); if (failInput) throw Error('receipt lost');
    } }, now: () => ++counter });
  return { host, sent, resize: () => { geometry.viewport_width++; },
    failInput: () => { failInput = true; }, failCapture: () => { failCapture = true; } };
}

async function prepared(kind = 'click') {
  const f = fixture();
  const frame = await f.host.capture();
  const action = kind === 'click' ? { kind, x: 543, y: 125, button: 'left' }
    : kind === 'drag' ? { kind, from_x: 10, from_y: 20, to_x: 30, to_y: 40 }
    : { kind, key: 'Return' };
  const task = { type: 'EXECUTE', source: 'jev', run_id: 'a'.repeat(32), context_version: 1,
    decision_id: 'decision-1', observation_id: 'obs-1', execute_allowed: true, replayed: false,
    candidate: { id: 'close_notice', description: 'Close simulated notice', semantic_target: 'notice',
      action: { ...action, screenshot_ref: frame.screenshot_ref } }, binding: frame,
    pending_execution: { decision_id: 'decision-1' } };
  f.host.accept(task);
  return { ...f, task, frame };
}

async function contractRequests() {
  const f = await prepared();
  // The initial request uses the start version, prior to accepting the decision.
  const initialHost = fixture().host;
  await initialHost.capture();
  const before = initialHost.request('before', {
    page_label: 'Simulated announcement', facts: ['A simulated notice covers the menu.'],
    objects: [{ id: 'notice', label: 'Notice', state: 'visible' }],
    candidates: [{ id: 'close_notice', description: 'Close simulated notice', semantic_target: 'notice',
      action: { kind: 'click', x: 543, y: 125, button: 'left' } }],
  });
  await f.host.execute();
  const after = f.host.request('after', { page_label: 'Simulated menu', facts: ['Simulated notice disappeared.'],
    goal_evidence: [{ criterion_id: 'notice_closed', satisfied: true, fact_indices: [0] }],
  }, { outcome: 'success', fact_indices: [0] });
  return { before, after };
}

if (process.argv.includes('--contracts')) {
  contractRequests().then(r => process.stdout.write(JSON.stringify(r)));
} else {
  test('missing adapter is rejected during preparation, before a decision', () => {
    assert.throws(() => createJevHost({ runId: 'a'.repeat(32), contextVersion: 0,
      browserId: 'simulation', tab: {}, cdp: {} }), /before advance/);
  });
  test('one click and capture; full binding display and explicitly assessed feedback', async () => {
    const { host, frame, task, sent } = await prepared();
    assert.deepEqual(host.decision().binding, task.binding);
    task.candidate.action.x = 0; // A caller mutation cannot change the accepted decision.
    const result = await host.execute();
    assert.equal(sent[1].x, 543);
    assert.deepEqual(sent.slice(1).map(x => x.type || x), ['mousePressed', 'mouseReleased', 'capture']);
    assert.equal(result.receipt.submission_status, 'accepted');
    assert.notEqual(result.frame.screenshot_ref, frame.screenshot_ref);
    assert.throws(() => host.request('after', { page_label: 'x', facts: [] }), /visual assessment/);
    assert.deepEqual(host.timings().map(t => t.event), ['capture', 'task_received_by_host', 'ui_submission', 'ui_receipt', 'capture']);
    await assert.rejects(host.execute(), /pending/);
  });
  for (const change of [{ replayed: true }, { execute_allowed: false }, { type: 'OBSERVE' }]) {
    test(`deny permit ${JSON.stringify(change)}`, async () => {
      const { host, task, sent } = await prepared();
      host.accept({ ...task, ...change });
      await assert.rejects(host.execute(), /permission/);
      assert.equal(sent.length, 1);
    });
  }
  test('reject a changed binding or viewport before input', async () => {
    const { host, task, resize, sent } = await prepared();
    host.accept({ ...task, binding: { ...task.binding, page_id: 'different' } });
    await assert.rejects(host.execute(), /Binding/);
    host.accept(task); resize();
    await assert.rejects(host.execute(), /geometry/);
    assert.equal(sent.length, 1);
  });
  test('partial input is unknown and fenced; feedback needs a new screenshot', async () => {
    const { host, failInput, sent } = await prepared();
    failInput(); await assert.rejects(host.execute(), /receipt lost/);
    await assert.rejects(host.execute(), /pending/);
    assert.equal(sent.length, 2);
    assert.throws(() => host.request('after', { page_label: 'x', facts: [] }), /fresh frame/);
    await host.capture();
    const result = host.request('after', { page_label: 'x', facts: ['Effect uncertain'] }, { outcome: 'unknown' });
    assert.equal(result.request.previous_execution.submission_status, 'unknown');
  });
  test('capture failure after accepted input cannot replay or reuse the old screenshot', async () => {
    const { host, failCapture } = await prepared();
    failCapture(); await assert.rejects(host.execute(), /capture interrupted/);
    await assert.rejects(host.execute(), /pending/);
    assert.throws(() => host.request('after', { page_label: 'x', facts: [] }), /fresh frame/);
  });
  test('duplicate decision remains fenced after a settled receipt', async () => {
    const { host, task } = await prepared();
    await host.execute();
    host.accept({ ...task, pending_execution: null });
    await assert.rejects(host.execute(), /already attempted/);
  });
  test('bad objects, missing satisfied and non-ASCII semantics are rejected locally', async () => {
    const { host } = await prepared();
    const evidence = { page_label: 'x', facts: ['Visible'] };
    for (const extra of [
      { objects: [{ id: 'x', label: 'x', kind: 'button' }] },
      { goal_evidence: [{ criterion_id: 'x', fact_indices: [0] }] },
      { candidates: [{ semantic_target: '关闭按钮' }] },
    ]) assert.throws(() => host.request('x', { ...evidence, ...extra }));
  });
  test('wrong run, stale version and stale candidate screenshot are rejected', async () => {
    const { host, task } = await prepared();
    assert.throws(() => host.accept({ ...task, run_id: 'b'.repeat(32) }), /Wrong run/);
    assert.throws(() => host.accept({ ...task, context_version: 0 }), /stale/);
    assert.throws(() => host.request('x', { page_label: 'x', facts: [], candidates: [{
      id: 'x', semantic_target: 'x', action: { kind: 'click', screenshot_ref: 'old' } }] }), /older screenshot/);
  });
  test('next compact Runtime decision can execute only after a newer version', async () => {
    const { host, task, sent } = await prepared();
    const captured = await host.execute();
    const next = { ...task, context_version: 2, decision_id: 'decision-2', binding: captured.frame,
      candidate: { ...task.candidate, action: { ...task.candidate.action, screenshot_ref: captured.frame.screenshot_ref } } };
    delete next.pending_execution; // The pre-input compact record omits telemetry.
    host.accept(next);
    await host.execute();
    assert.equal(sent.filter(x => x.type === 'mousePressed').length, 2);
  });
  test('drag and key dispatch selected parameters then capture', async () => {
    const drag = await prepared('drag'); await drag.host.execute();
    assert.equal(drag.sent.filter(x => x.type === 'mouseMoved').length, 8);
    assert.equal(drag.sent.at(-2).x, 30); assert.equal(drag.sent.at(-2).y, 40);
    const key = await prepared('key'); await key.host.execute();
    assert.deepEqual(key.sent[1], [null, 'Return']); assert.equal(key.sent.at(-1), 'capture');
  });
}
