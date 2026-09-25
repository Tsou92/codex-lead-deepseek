import assert from 'node:assert/strict';
import {mkdtempSync, readFileSync, rmSync, statSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

import {apply, createObserver, extractText, normalizeUsage, redact} from '../plugins/observe.mjs';

const dirs = [];
function tempDir() {
  const dir = mkdtempSync(join(tmpdir(), 'observe-'));
  dirs.push(dir);
  return dir;
}

function readRecords(path) {
  return readFileSync(path, 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line));
}

function fakeContext() {
  const handlers = new Map();
  const effects = [];
  return {
    handlers,
    effects,
    on(name, handler) {
      handlers.set(name, handler);
    },
    effect(callback) {
      effects.push(callback);
      return () => {};
    },
    fire(name, ...args) {
      const handler = handlers.get(name);
      assert.ok(handler, `no handler registered for ${name}`);
      handler(...args);
    },
  };
}

function runFixture(path, extraConfig = {}) {
  const ctx = fakeContext();
  const observer = apply(ctx, {telemetryPath: path, maxTextChars: 2000, ...extraConfig});
  const root = {id: 'root-session', header: {isSeeded: false}};
  const child = {id: 'child-session', header: {parentSession: 'root-session', origin: 'subagent', delegationDepth: 1}};

  ctx.fire('agent/created', {agent: {id: 'root-session', session: root, options: {model: 'deepseek-flash'}}, source: 'startup'});
  ctx.fire('session/event', root, {type: 'user/message', data: {content: [{type: 'text', text: 'hello root'}]}});
  ctx.fire('session/event', root, {
    type: 'assistant/message',
    data: {
      turn: 1, step: 1,
      message: {content: [{type: 'text', text: 'root answer'}, {type: 'reasoning', text: 'HIDDEN-ROOT-THINKING'}]},
      stream: [{kind: 'reasoning', text: 'HIDDEN-STREAM'}],
      usage: {inputTokens: 100, outputTokens: 40, cacheReadTokens: 7, cacheWriteTokens: 2, totalTokens: 140, reasoningTokens: 999},
    },
  });
  // Same step repeated (retry): usage must still be recorded once.
  ctx.fire('session/event', root, {
    type: 'assistant/message',
    data: {turn: 1, step: 1, message: {content: [{type: 'text', text: 'root answer'}]},
      usage: {inputTokens: 100, outputTokens: 40, totalTokens: 140}},
  });
  ctx.fire('session/event', root, {
    type: 'system/message', data: {turn: 1, step: 1, message: {content: [{type: 'text', text: 'HIDDEN-SYSTEM-PROMPT'}]}},
  });
  ctx.fire('session/event', root, {
    type: 'developer/message', data: {turn: 1, step: 1, message: {content: [{type: 'text', text: 'HIDDEN-DEVELOPER'}]}},
  });
  ctx.fire('session/event', root, {
    type: 'tool/call', data: {turn: 1, step: 1, callId: 'call-1', name: 'read', arguments: '{"path":"a.txt"}'},
  });
  ctx.fire('session/event', root, {
    type: 'tool/result', data: {turn: 1, step: 1, message: {role: 'tool', toolCallId: 'call-1', content: [{type: 'text', text: 'file body'}]}},
  });
  ctx.fire('session/event', root, {
    type: 'user/message',
    data: {content: [{type: 'text', text: 'key sk-abcdefghijklmnop Authorization: Bearer abcdefghijklmnop'}]},
  });

  ctx.fire('agent/created', {agent: {id: 'child-session', session: child, options: {model: 'deepseek-flash'}}, source: 'startup'});
  ctx.fire('subagent/start', {runId: 'run-1', provider: 'spawn', id: 'child-session', local: true});
  ctx.fire('session/event', child, {type: 'user/message', data: {content: [{type: 'text', text: 'do the subtask'}]}});
  ctx.fire('session/event', child, {
    type: 'assistant/message',
    data: {turn: 1, step: 1, message: {content: [{type: 'text', text: 'subtask done'}]},
      usage: {inputTokens: 11, outputTokens: 3, totalTokens: 14}},
  });
  ctx.fire('subagent/end', {runId: 'run-1', provider: 'spawn', id: 'child-session', local: true, stopReason: {kind: 'completed'}});
  ctx.fire('agent/disposed', {agent: {id: 'child-session', session: child}});
  observer.close();
  return readRecords(path);
}

const path = join(tempDir(), 'telemetry.jsonl');
const records = runFixture(path);

// File permissions are owner-only.
assert.equal(statSync(path).mode & 0o777, 0o600);

// Lifecycle and parent linkage from real agent/session metadata.
const created = records.filter(r => r.type === 'agent_created');
assert.deepEqual(created.map(r => r.agent_id), ['root-session', 'child-session']);
assert.equal(created[0].parent_id, null);
assert.equal(created[1].parent_id, 'root-session');
assert.equal(created[0].model, 'deepseek-flash');

const started = records.filter(r => r.type === 'subagent_started');
assert.equal(started.length, 1, 'continuable-capable child observed exactly once');
assert.equal(started[0].agent_id, 'child-session');
assert.equal(started[0].parent_id, 'root-session');

const finished = records.filter(r => r.type === 'subagent_finished');
assert.equal(finished.length, 1);
assert.equal(finished[0].status, 'completed');
const disposed = records.find(r => r.type === 'agent_finished' && r.agent_id === 'child-session');
assert.ok(disposed, 'disposal is recorded');
assert.equal(disposed.status, 'disposed', 'disposal is never claimed as completed');

// Parent and child usage are independent and each step appears exactly once.
const usage = records.filter(r => r.type === 'usage');
assert.equal(usage.length, 2, 'one usage record per agent step, no duplicates');
const rootUsage = usage.find(r => r.agent_id === 'root-session');
const childUsage = usage.find(r => r.agent_id === 'child-session');
assert.equal(rootUsage.usage.inputTokens, 100);
assert.equal(rootUsage.usage.totalTokens, 140);
assert.equal(rootUsage.usage.cacheReadTokens, 7);
assert.equal(childUsage.usage.inputTokens, 11);
assert.equal(childUsage.session_id, 'child-session');
assert.equal('reasoningTokens' in rootUsage.usage, false, 'no hidden reasoning counters');

// Messages and tool traffic for both agents, with no hidden content.
assert.ok(records.some(r => r.type === 'message' && r.role === 'user' && r.text === 'hello root'));
assert.ok(records.some(r => r.type === 'message' && r.role === 'assistant' && r.text === 'root answer'));
assert.ok(records.some(r => r.type === 'message' && r.agent_id === 'child-session'));
const toolCall = records.find(r => r.type === 'tool_call');
assert.equal(toolCall.tool, 'read');
assert.equal(toolCall.call_id, 'call-1');
assert.equal(toolCall.input, '{"path":"a.txt"}');
const toolResult = records.find(r => r.type === 'tool_result');
assert.equal(toolResult.call_id, 'call-1');
assert.equal(toolResult.status, 'ok');

const serialized = JSON.stringify(records);
assert.ok(!serialized.includes('HIDDEN-ROOT-THINKING'));
assert.ok(!serialized.includes('HIDDEN-STREAM'));
assert.ok(!serialized.includes('HIDDEN-SYSTEM-PROMPT'));
assert.ok(!serialized.includes('HIDDEN-DEVELOPER'));
assert.ok(!serialized.includes('sk-abcdefghijklmnop'), 'api keys are redacted');
assert.ok(!serialized.includes('Bearer abcdefghijklmnop'), 'authorization is redacted');

// Every record carries the five contract base fields.
for (const record of records) {
  for (const field of ['timestamp', 'type', 'agent_id', 'parent_id', 'session_id']) {
    assert.ok(Object.prototype.hasOwnProperty.call(record, field), `${record.type} missing ${field}`);
  }
}

// Per-string truncation is flagged without dropping the record.
assert.equal(extractText([{type: 'text', text: 'x'.repeat(50)}], 10), 'x'.repeat(10) + '…[truncated]');
assert.deepEqual(normalizeUsage({inputTokens: 1, totalTokens: 2, reasoningTokens: 3}), {inputTokens: 1, totalTokens: 2});

// JSON-shaped and nested credentials are redacted structurally.
assert.ok(!redact('{"api_key":"supersecretvalue"}').includes('supersecretvalue'));
assert.ok(!redact('{"password": "hunter2", "safe": "keep"}').includes('hunter2'));
assert.ok(!redact('{"nested":{"password":"hunter3"}}').includes('hunter3'));
const nested = redact('{"outer":"{\\"password\\":\\"deep-secret\\"}"}');
assert.ok(!nested.includes('deep-secret'), 'nested JSON string credential is redacted');
assert.deepEqual(JSON.parse(nested), {outer: '{"password":"[redacted]"}'});
assert.ok(!redact("api_key = 'quoted-secret'").includes('quoted-secret'));
assert.ok(!redact("Authorization: 'Bearer xyz12345'").includes('xyz12345'));
assert.equal(redact('{"path":"a.txt"}'), '{"path":"a.txt"}', 'benign JSON keeps its shape');

// A writer failure is isolated and never escapes an event hook.
{
  const throwingWriter = {
    write() {
      throw new Error('disk full');
    },
    close() {
      throw new Error('close failed');
    },
    get stopped() {
      return false;
    },
  };
  const observer = createObserver({telemetryPath: join(tempDir(), 'unused.jsonl')}, {writer: throwingWriter});
  const session = {id: 'io', header: {}};
  assert.doesNotThrow(() => observer.agentCreated({agent: {id: 'io', session, options: {model: 'm'}}, source: 'startup'}));
  assert.doesNotThrow(() => observer.sessionEvent(session, {type: 'user/message', data: {content: [{type: 'text', text: 'hi'}]}}));
  assert.doesNotThrow(() => observer.close());
}

// A running -> idle child surfaces an honest idle finish even without disposal.
{
  const idlePath = join(tempDir(), 'idle.jsonl');
  const idleCtx = fakeContext();
  const idleObserver = apply(idleCtx, {telemetryPath: idlePath});
  const root = {id: 'idle-root', header: {}};
  const child = {id: 'idle-child', header: {parentSession: 'idle-root', origin: 'subagent', delegationDepth: 1}};
  idleCtx.fire('agent/created', {agent: {id: 'idle-root', session: root, options: {model: 'm'}}, source: 'startup'});
  idleCtx.fire('agent/created', {agent: {id: 'idle-child', session: child, options: {model: 'm'}}, source: 'startup'});
  idleCtx.fire('agent/status', {agent: {id: 'idle-child', session: child}, status: 'running'});
  idleCtx.fire('agent/status', {agent: {id: 'idle-child', session: child}, status: 'idle'});
  idleCtx.fire('agent/status', {agent: {id: 'idle-child', session: child}, status: 'idle'});
  idleCtx.fire('agent/status', {agent: {id: 'idle-root', session: root}, status: 'running'});
  idleCtx.fire('agent/status', {agent: {id: 'idle-root', session: root}, status: 'idle'});
  idleObserver.close();
  const idleRecords = readRecords(idlePath);
  const idleFinishes = idleRecords.filter(r => r.type === 'agent_finished' && r.agent_id === 'idle-child');
  assert.equal(idleFinishes.length, 1, 'idle transition recorded exactly once');
  assert.equal(idleFinishes[0].status, 'idle');
  assert.equal(idleFinishes[0].parent_id, 'idle-root');
  assert.equal(idleRecords.filter(r => r.type === 'agent_finished' && r.agent_id === 'idle-root').length, 0,
    'lead agent idle is not reported as a finish');
}

// The 65536-char default keeps full long text but still flags real overrun.
{
  const longPath = join(tempDir(), 'long.jsonl');
  const longCtx = fakeContext();
  const longObserver = apply(longCtx, {telemetryPath: longPath});
  const session = {id: 'long', header: {}};
  longCtx.fire('agent/created', {agent: {id: 'long', session, options: {model: 'm'}}, source: 'startup'});
  const body = 'y'.repeat(10000);
  longCtx.fire('session/event', session, {type: 'user/message', data: {content: [{type: 'text', text: body}]}});
  longCtx.fire('session/event', session, {type: 'user/message', data: {content: [{type: 'text', text: 'z'.repeat(70000)}]}});
  longObserver.close();
  const texts = readRecords(longPath).filter(r => r.type === 'message').map(r => r.text);
  assert.equal(texts[0], body, '10000 chars stay under the raised default cap');
  assert.equal(texts[1].length, 65536 + '…[truncated]'.length);
  assert.ok(texts[1].endsWith('…[truncated]'));
}

// The plugin scope unload closes the descriptor.
{
  const cleanupCtx = fakeContext();
  const cleanupObserver = apply(cleanupCtx, {telemetryPath: join(tempDir(), 'cleanup.jsonl')});
  assert.equal(cleanupCtx.effects.length, 1);
  const dispose = cleanupCtx.effects[0]();
  assert.equal(typeof dispose, 'function');
  assert.doesNotThrow(() => dispose());
  assert.doesNotThrow(() => cleanupObserver.close());
}

// The byte cap keeps the log bounded, leaves one visible truncation notice,
// and a full sink never throws into the execution path.
const capPath = join(tempDir(), 'cap.jsonl');
const capCtx = fakeContext();
const capObserver = apply(capCtx, {telemetryPath: capPath, maxBytes: 1200});
const capSession = {id: 'cap-session', header: {}};
capCtx.fire('agent/created', {agent: {id: 'cap-session', session: capSession, options: {model: 'm'}}, source: 'startup'});
for (let i = 0; i < 200; i++) {
  capCtx.fire('session/event', capSession, {type: 'user/message', data: {content: [{type: 'text', text: `message ${i} `.repeat(20)}]}});
}
for (let i = 0; i < 20; i++) {
  capCtx.fire('session/event', capSession, {type: 'user/message', data: {content: [{type: 'text', text: 'after truncation'}]}});
}
capObserver.close();
assert.ok(statSync(capPath).size <= 1200, 'telemetry file respects the configured cap');
const capRecords = readRecords(capPath);
const notice = capRecords[capRecords.length - 1];
assert.equal(notice.type, 'telemetry_status');
assert.equal(notice.status, 'truncated');
assert.ok(/truncated/i.test(notice.text));

// A sink already near the cap still emits the reserved notice.
{
  const tightPath = join(tempDir(), 'tight.jsonl');
  const tightCtx = fakeContext();
  const tightObserver = apply(tightCtx, {telemetryPath: tightPath, maxBytes: 700});
  const session = {id: 'tight', header: {}};
  tightCtx.fire('agent/created', {agent: {id: 'tight', session, options: {model: 'm'}}, source: 'startup'});
  for (let i = 0; i < 50; i++) {
    tightCtx.fire('session/event', session, {type: 'user/message', data: {content: [{type: 'text', text: 'filler '.repeat(30)}]}});
  }
  tightObserver.close();
  assert.ok(statSync(tightPath).size <= 700);
  const tightNotice = readRecords(tightPath).at(-1);
  assert.equal(tightNotice.status, 'truncated');
  assert.ok(/truncated/i.test(tightNotice.text));
}

for (const dir of dirs) rmSync(dir, {recursive: true, force: true});
console.log('observe tests: passed (parent/child usage, hidden content, redaction, idle finish, IO isolation, 8 MiB cap)');
