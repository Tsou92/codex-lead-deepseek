import assert from 'node:assert/strict';
import {test} from 'node:test';

import {READY_SERVICE, createCompat, installCompat} from '../plugins/legacy-headless.mjs';

function fakeCtx() {
  const handlers = new Map();
  const provided = [];
  const cleanups = [];
  return {
    provided,
    cleanups,
    on(name, fn) {
      if (!handlers.has(name)) handlers.set(name, []);
      handlers.get(name).push(fn);
    },
    emit(name, ...args) {
      for (const fn of handlers.get(name) || []) fn(...args);
    },
    effect(fn) {
      cleanups.push(fn());
    },
    provide(name, value) {
      provided.push([name, value]);
    },
  };
}

function collector() {
  const lines = [];
  return {
    lines,
    write(chunk) {
      lines.push(JSON.parse(chunk));
    },
  };
}

test('projects the root session, usage, tools, turn reason and final', () => {
  const ctx = fakeCtx();
  const internals = {stdout: {write() {}}};
  const output = collector();
  createCompat(ctx, internals, {write: output.write});

  // Real shape: `agent/created` carries `{agent: {id, session}}`.
  ctx.emit('agent/created', {agent: {id: 'root', session: {id: 'root'}}});
  ctx.emit('session/event', {id: 'root'}, {
    type: 'assistant/message', data: {turn: 1, step: 1, usage: {inputTokens: 3, outputTokens: 4}},
  });
  ctx.emit('session/event', {id: 'root'}, {type: 'step/end', data: {turn: 1, step: 1}});
  ctx.emit('session/event', {id: 'root'}, {type: 'tool/call', data: {name: 'read'}});
  ctx.emit('session/event', {id: 'root'}, {type: 'tool/result', data: {message: {isError: true}}});
  ctx.emit('session/event', {id: 'root'}, {type: 'turn/end', data: {reason: {kind: 'completed'}}});
  internals.stdout.write('final answer\n');

  assert.deepEqual(output.lines[0], {type: 'session', sessionId: 'root'});
  assert.deepEqual(
    output.lines.filter(line => line.type === 'status'),
    [
      {type: 'status', phase: 'step_end', usage: {inputTokens: 3, outputTokens: 4}},
      {type: 'status', phase: 'turn_end', reason: {kind: 'completed'}},
    ],
  );
  assert.deepEqual(output.lines.find(line => line.type === 'tool_call'), {type: 'tool_call', tool: 'read'});
  assert.deepEqual(output.lines.find(line => line.type === 'tool_result'), {type: 'tool_result', status: 'error'});
  assert.deepEqual(output.lines.at(-1), {type: 'final', text: 'final answer'});
});

test('reads the legacy nested tool-result shape for the status', () => {
  const ctx = fakeCtx();
  const internals = {stdout: {write() {}}};
  const output = collector();
  createCompat(ctx, internals, {write: output.write});

  ctx.emit('agent/created', {agent: {session: {id: 'root'}}});
  ctx.emit('session/event', {id: 'root'}, {
    type: 'tool/result',
    data: {
      message: {
        source: {kind: 'tool', callId: 'call-1'},
        content: [{type: 'tool-result', toolCallId: 'call-1', content: [{type: 'text', text: 'ok'}], isError: false}],
        role: 'user',
        id: 'message-id',
      },
    },
  });
  ctx.emit('session/event', {id: 'root'}, {
    type: 'tool/result',
    data: {
      message: {
        source: {kind: 'tool', callId: 'call-2'},
        content: [{type: 'tool-result', toolCallId: 'call-2', content: [{type: 'text', text: 'boom'}], isError: true}],
        role: 'user',
        id: 'message-id-2',
      },
    },
  });

  assert.deepEqual(output.lines.filter(line => line.type === 'tool_result'), [
    {type: 'tool_result', status: 'ok'},
    {type: 'tool_result', status: 'error'},
  ]);
});

test('identifies the root from the agent id when the session header omits it', () => {  const ctx = fakeCtx();
  const internals = {stdout: {write() {}}};
  const output = collector();
  createCompat(ctx, internals, {write: output.write});

  ctx.emit('agent/created', {agent: {id: 'session-42', session: {}}});
  assert.deepEqual(output.lines, [{type: 'session', sessionId: 'session-42'}]);
});

test('ignores child sessions and hidden system/developer/reasoning events', () => {
  const ctx = fakeCtx();
  const internals = {stdout: {write() {}}};
  const output = collector();
  createCompat(ctx, internals, {write: output.write});

  ctx.emit('agent/created', {agent: {session: {id: 'child', header: {parentSession: 'root'}}}});
  ctx.emit('session/event', {id: 'child', header: {parentSession: 'root'}}, {type: 'tool/call', data: {name: 'bash'}});
  ctx.emit('agent/created', {agent: {session: {id: 'root'}}});
  ctx.emit('session/event', {id: 'root'}, {type: 'system/message', data: {content: 'secret'}});
  ctx.emit('session/event', {id: 'root'}, {type: 'developer/message', data: {content: 'secret'}});
  ctx.emit('session/event', {id: 'root'}, {type: 'assistant/reasoning', data: {text: 'secret'}});

  assert.deepEqual(output.lines, [{type: 'session', sessionId: 'root'}]);
});

test('does not fabricate a final event when the runner wrote nothing', () => {
  const ctx = fakeCtx();
  const internals = {stdout: {write() {}}};
  const output = collector();
  createCompat(ctx, internals, {write: output.write});

  ctx.emit('agent/created', {agent: {session: {id: 'root'}}});
  ctx.emit('session/event', {id: 'root'}, {type: 'turn/end', data: {reason: {kind: 'completed'}}});

  assert.ok(!output.lines.some(line => line.type === 'final'));
});

test('surfaces a failing turn as an error event without hiding the reason', () => {
  const ctx = fakeCtx();
  const internals = {stdout: {write() {}}};
  const output = collector();
  createCompat(ctx, internals, {write: output.write});

  ctx.emit('agent/created', {agent: {session: {id: 'root'}}});
  ctx.emit('session/event', {id: 'root'}, {
    type: 'turn/end', data: {reason: {kind: 'error', error: {message: 'boom'}}},
  });

  assert.deepEqual(output.lines.at(-1), {type: 'error', message: 'boom'});
  assert.equal(output.lines.find(line => line.type === 'status').reason.kind, 'error');
});

test('swaps internals.stdout without mutating the original sink', () => {
  const ctx = fakeCtx();
  const originalWrite = function write() {};
  const original = {write: originalWrite};
  const internals = {stdout: original};
  const output = collector();
  createCompat(ctx, internals, {write: output.write});

  assert.notEqual(internals.stdout, original);
  // The original object is process.stdout; its write must stay untouched.
  assert.equal(original.write, originalWrite);
  original.write('not a final\n');
  assert.deepEqual(output.lines, []);

  internals.stdout.write('real final\n');
  assert.deepEqual(output.lines, [{type: 'final', text: 'real final'}]);
});

test('installCompat returns undefined, provides ready and registers cleanup', () => {
  const ctx = fakeCtx();
  const internals = {stdout: {write() {}}};
  const output = collector();

  const result = installCompat(ctx, internals, {write: output.write});
  assert.equal(result, undefined);
  assert.deepEqual(ctx.provided, [[READY_SERVICE, {ready: true}]]);
  assert.equal(ctx.cleanups.length, 1);
  assert.equal(typeof ctx.cleanups[0], 'function');
  assert.equal(READY_SERVICE, 'leadseekLegacyReady');

  ctx.emit('agent/created', {agent: {session: {id: 'root'}}});
  internals.stdout.write('answer\n');
  assert.deepEqual(output.lines, [
    {type: 'session', sessionId: 'root'},
    {type: 'final', text: 'answer'},
  ]);

  ctx.cleanups[0]();
  internals.stdout.write('after close\n');
  assert.equal(output.lines.length, 2);
});

test('fails loudly when the official internals export is unavailable', () => {
  assert.throws(() => createCompat(fakeCtx(), null, {write() {}}), /internals/);
  assert.throws(() => createCompat(fakeCtx(), {stdout: {}}, {write() {}}), /internals/);
});
