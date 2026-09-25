// Synthetic-fixture tests for the offline Harness history importer.
//
// The fixtures are real zstd files built with Node's zlib (no mocking of the
// decompressor), with multiple concatenated frames and original event times.
import assert from 'node:assert/strict';
import {existsSync, mkdirSync, mkdtempSync, readFileSync, statSync, symlinkSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {zstdCompressSync} from 'node:zlib';
import {decompressAllFrames, importHistory, persistStats} from '../scripts/import-monitor-history.mjs';

const SESSION_FILE = 'session.v4.jsonl.zstd';
const t = base => new Date(base).toISOString();

function makeRoot() {
  return mkdtempSync(path.join(tmpdir(), 'monitor-history-'));
}

function runDir(root, runId) {
  const dir = path.join(root, '.state', 'runs', runId);
  mkdirSync(dir, {recursive: true});
  return dir;
}

function writeRun(root, runId, result) {
  const dir = runDir(root, runId);
  writeFileSync(path.join(dir, 'result.json'), JSON.stringify(result) + '\n');
  return dir;
}

function buildSessionBuffer(header, events, frameCount = 2) {
  const lines = [JSON.stringify(header), ...events.map(event => JSON.stringify(event))];
  const perFrame = Math.max(1, Math.ceil(lines.length / frameCount));
  const chunks = [];
  for (let index = 0; index < lines.length; index += perFrame) {
    chunks.push(zstdCompressSync(Buffer.from(lines.slice(index, index + perFrame).join('\n') + '\n')));
  }
  return Buffer.concat(chunks);
}

function writeSession(root, workspaceKey, sessionId, header, events, frameCount = 2) {
  const dir = path.join(root, '.state', 'harness-home', 'sessions', workspaceKey, sessionId);
  mkdirSync(dir, {recursive: true});
  const file = path.join(dir, SESSION_FILE);
  writeFileSync(file, buildSessionBuffer(header, events, frameCount));
  return file;
}

const headerOf = (id, extra = {}) => ({
  type: 'session', version: 4, id, createdAt: t(1_700_000_000_000), cwd: '/tmp/work',
  isSeeded: false, delegationDepth: 0, ...extra,
});

const requestContext = (time, provider, model) => ({
  type: 'request/context', seq: 1, time, data: {provider, model, messages: [{role: 'system', content: 'SECRET'}]},
});
const userMessage = (time, text, seq = 2) => ({
  type: 'user/message', seq, time, data: {content: [{type: 'text', text}]},
});
const assistantMessage = (time, text, usage, turn, step) => ({
  type: 'assistant/message', seq: 3, time,
  data: {message: {content: [{type: 'text', text}]}, usage, turn, step},
});
const toolCall = (time, name, args, callId, turn, step) => ({
  type: 'tool/call', seq: 4, time, data: {turn, step, name, arguments: args, callId},
});
const toolResult = (time, content, callId, isError, turn, step) => ({
  type: 'tool/result', seq: 5, time,
  data: {turn, step, message: {content: [{type: 'text', text: content}], toolCallId: callId, isError}},
});

function readTelemetry(file) {
  return readFileSync(file, 'utf8').split('\n').filter(line => line.trim()).map(line => JSON.parse(line));
}

const tests = [];
const test = (name, fn) => tests.push({name, fn});

test('父子会话：多帧、原始时间、独立 usage、无关会话不导入', () => {
  const root = makeRoot();
  try {
    const rootId = 'root-aaa';
    const childId = 'child-bbb';
    const otherId = 'other-ccc';
    const rootFile = writeSession(root, 'wk1', rootId, headerOf(rootId), [
      requestContext(1_700_000_100_000, 'deepseek', 'deepseek-chat'),
      userMessage(1_700_000_101_000, 'hello-root'),
      assistantMessage(1_700_000_102_000, 'root-answer',
        {inputTokens: 10, outputTokens: 5, cacheReadTokens: 1, cacheWriteTokens: 2, totalTokens: 15}, 1, 1),
      toolCall(1_700_000_103_000, 'bash', {command: 'ls'}, 'call-1', 1, 1),
      toolResult(1_700_000_104_000, 'file.txt', 'call-1', false, 1, 1),
    ], 3);
    const rootBytes = readFileSync(rootFile);
    writeSession(root, 'wk1', childId,
      headerOf(childId, {parentSession: rootId, delegationDepth: 1, origin: 'subagent'}), [
        requestContext(1_700_000_200_000, 'deepseek', 'deepseek-chat'),
        userMessage(1_700_000_201_000, 'child-task'),
        assistantMessage(1_700_000_202_000, 'child-answer',
          {inputTokens: 7, outputTokens: 3, totalTokens: 10}, 1, 1),
      ], 2);
    writeSession(root, 'wk1', otherId, headerOf(otherId, {parentSession: 'unrelated-parent'}), [
      userMessage(1_700_000_300_000, 'should-not-appear'),
    ], 1);
    writeRun(root, 'run-1', {session_id: rootId, status: 'completed'});

    const stats = importHistory({root});
    assert.equal(stats.imported, 1, JSON.stringify(stats.runs));
    const entry = stats.runs[0];
    assert.equal(entry.status, 'imported');
    assert.equal(entry.sessions.find(item => item.session_id === rootId).frames, 3);
    assert.deepEqual(readFileSync(rootFile), rootBytes, '原压缩文件不得改变');

    const telemetryPath = path.join(root, '.state', 'runs', 'run-1', 'telemetry.jsonl');
    const records = readTelemetry(telemetryPath);
    assert.equal(records[0].type, 'telemetry_status');
    assert.equal(records[0].status, 'historical_import');
    assert.equal(records.at(-1).type, 'telemetry_status');
    assert.equal(records.at(-1).status, 'complete');

    const agents = records.filter(record => record.type === 'agent_created');
    assert.deepEqual(agents.map(record => record.session_id).sort(), [childId, rootId].sort());
    for (const agent of agents) assert.equal(agent.model, 'deepseek-chat');
    assert.ok(!records.some(record => record.session_id === otherId), '无关会话不得写入');

    const userRoot = records.find(record => record.type === 'message' && record.text === 'hello-root');
    assert.equal(userRoot.timestamp, t(1_700_000_101_000), '用户消息使用原始时间');
    assert.equal(userRoot.role, 'user');
    const tool = records.find(record => record.type === 'tool_call');
    assert.equal(tool.tool, 'bash');
    assert.equal(tool.call_id, 'call-1');
    const result = records.find(record => record.type === 'tool_result');
    assert.equal(result.result, 'file.txt');
    assert.equal(result.status, 'ok');

    const usages = records.filter(record => record.type === 'usage');
    assert.equal(usages.length, 2, '父子各自一条 usage');
    assert.equal(usages.find(record => record.session_id === rootId).usage.inputTokens, 10);
    assert.equal(usages.find(record => record.session_id === childId).usage.inputTokens, 7);
    assert.equal(usages.find(record => record.session_id === childId).parent_id, rootId, 'parent_id 保留');

    assert.equal(statSync(telemetryPath).mode & 0o777, 0o600, 'telemetry 权限 0600');
  } finally {
    // temp dirs are left for the OS to reap; no assertion depends on them
  }
});

test('已有非空 telemetry.jsonl 一律跳过且不覆盖', () => {
  const root = makeRoot();
  const rootId = 'root-keep';
  writeSession(root, 'wk1', rootId, headerOf(rootId), [userMessage(1_700_000_101_000, 'hello')], 1);
  const dir = writeRun(root, 'run-2', {session_id: rootId, status: 'completed'});
  const telemetryPath = path.join(dir, 'telemetry.jsonl');
  writeFileSync(telemetryPath, '{"real":true}\n');
  const stats = importHistory({root});
  assert.equal(stats.skipped, 1);
  assert.equal(stats.imported, 0);
  assert.match(stats.runs[0].reason, /已存在且非空/);
  assert.equal(readFileSync(telemetryPath, 'utf8'), '{"real":true}\n');
});

test('运行中或缺 session_id 的 run 跳过', () => {
  const root = makeRoot();
  const rootId = 'root-live';
  writeSession(root, 'wk1', rootId, headerOf(rootId), [userMessage(1_700_000_101_000, 'x')], 1);
  writeRun(root, 'run-live', {session_id: rootId, status: 'running'});
  writeRun(root, 'run-no-session', {status: 'completed'});
  runDir(root, 'run-not-finished');
  const stats = importHistory({root});
  assert.equal(stats.imported, 0);
  assert.equal(stats.skipped, 3);
  assert.ok(stats.runs.every(entry => entry.status === 'skipped'));
});

test('符号链接会话文件被拒绝', () => {
  const root = makeRoot();
  const rootId = 'root-link';
  const realFile = path.join(root, 'outside-session.zstd');
  writeFileSync(realFile, buildSessionBuffer(headerOf(rootId), [userMessage(1_700_000_101_000, 'x')], 1));
  const linkDir = path.join(root, '.state', 'harness-home', 'sessions', 'wk1', rootId);
  mkdirSync(linkDir, {recursive: true});
  symlinkSync(realFile, path.join(linkDir, SESSION_FILE));
  writeRun(root, 'run-3', {session_id: rootId, status: 'completed'});
  const stats = importHistory({root});
  assert.equal(stats.imported, 0);
  assert.equal(stats.failed, 1);
  assert.match(stats.runs[0].reason, /符号链接/);
  assert.ok(!existsSync(path.join(root, '.state', 'runs', 'run-3', 'telemetry.jsonl')));
});

test('损坏根压缩：该 run 失败且不写 telemetry', () => {
  const root = makeRoot();
  const rootId = 'root-bad';
  const file = writeSession(root, 'wk1', rootId, headerOf(rootId), [userMessage(1_700_000_101_000, 'x')], 1);
  writeFileSync(file, Buffer.concat([readFileSync(file), Buffer.from('not-a-zstd-frame')]));
  writeRun(root, 'run-4', {session_id: rootId, status: 'failed'});
  const stats = importHistory({root});
  assert.equal(stats.failed, 1);
  assert.match(stats.runs[0].reason, /损坏|不完整/);
  assert.ok(!existsSync(path.join(root, '.state', 'runs', 'run-4', 'telemetry.jsonl')));
});

test('损坏子会话：根仍导入但标记 partial 并给出 warning', () => {
  const root = makeRoot();
  const rootId = 'root-partial';
  const childId = 'child-bad';
  writeSession(root, 'wk1', rootId, headerOf(rootId), [
    userMessage(1_700_000_101_000, 'root-ok'),
    assistantMessage(1_700_000_102_000, 'root-answer', {inputTokens: 4, outputTokens: 1, totalTokens: 5}, 1, 1),
  ], 1);
  const childFile = writeSession(root, 'wk1', childId,
    headerOf(childId, {parentSession: rootId, delegationDepth: 1}), [userMessage(1_700_000_201_000, 'child')], 1);
  writeFileSync(childFile, Buffer.concat([readFileSync(childFile), Buffer.from('garbage')]));
  writeRun(root, 'run-5', {session_id: rootId, status: 'completed'});
  const stats = importHistory({root});
  const entry = stats.runs[0];
  assert.equal(entry.status, 'imported');
  assert.equal(entry.partial, true);
  assert.ok(entry.warnings.some(warning => warning.includes(childId) && warning.includes('跳过')));
  const records = readTelemetry(path.join(root, '.state', 'runs', 'run-5', 'telemetry.jsonl'));
  assert.equal(records.at(-1).status, 'partial');
  assert.ok(records.at(-1).text.includes(childId) && records.at(-1).text.includes('解压失败'),
    'partial 的 telemetry_status 必须写出具体缺失原因');
  assert.ok(records.some(record => record.type === 'message' && record.text === 'root-ok'));
  assert.ok(!records.some(record => record.session_id === childId));
});

test('isSeeded 子会话标 warning 且不计父历史 usage', () => {
  const root = makeRoot();
  const rootId = 'root-seed';
  const childId = 'child-seed';
  const sharedUsage = {inputTokens: 100, outputTokens: 40, totalTokens: 140};
  writeSession(root, 'wk1', rootId, headerOf(rootId), [
    userMessage(1_700_000_101_000, 'root-msg'),
    assistantMessage(1_700_000_102_000, 'root-answer', sharedUsage, 1, 1),
  ], 1);
  writeSession(root, 'wk1', childId,
    headerOf(childId, {parentSession: rootId, delegationDepth: 1, isSeeded: true}), [
      userMessage(1_700_000_101_000, 'root-msg'),
      assistantMessage(1_700_000_102_000, 'inherited-answer', sharedUsage, 1, 1),
      userMessage(1_700_000_201_000, 'new-child-msg'),
    ], 1);
  writeRun(root, 'run-6', {session_id: rootId, status: 'completed'});
  const stats = importHistory({root});
  const entry = stats.runs[0];
  assert.equal(entry.status, 'imported');
  assert.equal(entry.partial, true);
  assert.ok(entry.warnings.some(warning => warning.includes('isSeeded')));
  const records = readTelemetry(path.join(root, '.state', 'runs', 'run-6', 'telemetry.jsonl'));
  const usages = records.filter(record => record.type === 'usage');
  assert.equal(usages.length, 1, 'isSeeded 会话的继承 usage 不得重复计入');
  assert.equal(usages[0].session_id, rootId);
  assert.ok(!records.some(record => record.session_id === childId));
});

test('同一 step 的重复 assistant 事件 usage 只计一次', () => {
  const root = makeRoot();
  const rootId = 'root-dup';
  const usage = {inputTokens: 3, outputTokens: 1, totalTokens: 4};
  writeSession(root, 'wk1', rootId, headerOf(rootId), [
    assistantMessage(1_700_000_101_000, 'attempt-1', usage, 2, 3),
    assistantMessage(1_700_000_102_000, 'attempt-2', usage, 2, 3),
  ], 1);
  writeRun(root, 'run-7', {session_id: rootId, status: 'completed'});
  const stats = importHistory({root});
  assert.equal(stats.imported, 1);
  const records = readTelemetry(path.join(root, '.state', 'runs', 'run-7', 'telemetry.jsonl'));
  assert.equal(records.filter(record => record.type === 'usage').length, 1);
  assert.equal(records.filter(record => record.type === 'message' && record.role === 'assistant').length, 2);
});

test('读取上限与多帧解码在单元层生效', () => {
  const frame = text => zstdCompressSync(Buffer.from(text));
  const buffer = Buffer.concat([frame('a\n'), frame('b\n'), frame('c\n')]);
  const decoded = decompressAllFrames(buffer);
  assert.equal(decoded.frames, 3);
  assert.equal(decoded.buffer.toString('utf8'), 'a\nb\nc\n');
  assert.throws(() => decompressAllFrames(buffer, {compressedLimit: 4}), /压缩文件超过/);
  assert.throws(() => decompressAllFrames(buffer, {decompressedLimit: 3}), /解压后内容超过/);
});

test('persistStats 写入 0600 的 history-import.json', () => {
  const root = makeRoot();
  const stats = {imported: 2, skipped: 1, failed: 0, runs: []};
  persistStats(root, stats);
  const file = path.join(root, '.state', 'monitor', 'history-import.json');
  assert.ok(existsSync(file));
  assert.deepEqual(JSON.parse(readFileSync(file, 'utf8')), stats);
  assert.equal(statSync(file).mode & 0o777, 0o600);
});

test('白名单外的状态（准备中/未知）一律跳过', () => {
  const root = makeRoot();
  const rootId = 'root-prep';
  writeSession(root, 'wk1', rootId, headerOf(rootId), [userMessage(1_700_000_101_000, 'x')], 1);
  writeRun(root, 'run-prep', {session_id: rootId, status: 'preparing'});
  const stats = importHistory({root});
  assert.equal(stats.imported, 0);
  assert.equal(stats.skipped, 1);
  assert.match(stats.runs[0].reason, /白名单/);
  assert.ok(!existsSync(path.join(root, '.state', 'runs', 'run-prep', 'telemetry.jsonl')));
});

test('根会话 isSeeded 跳过且不生成空 telemetry', () => {
  const root = makeRoot();
  const rootId = 'root-seeded-root';
  writeSession(root, 'wk1', rootId, headerOf(rootId, {isSeeded: true}),
    [userMessage(1_700_000_101_000, 'inherited')], 1);
  writeRun(root, 'run-seed-root', {session_id: rootId, status: 'completed'});
  const stats = importHistory({root});
  assert.equal(stats.imported, 0);
  assert.equal(stats.skipped, 1);
  assert.match(stats.runs[0].reason, /isSeeded/);
  assert.ok(!existsSync(path.join(root, '.state', 'runs', 'run-seed-root', 'telemetry.jsonl')));
});

test('全局会话索引告警使导入标 partial 并写入 telemetry', () => {
  const root = makeRoot();
  const rootId = 'root-gw';
  writeSession(root, 'wk1', rootId, headerOf(rootId), [userMessage(1_700_000_101_000, 'root-gw-msg')], 1);
  const badDir = path.join(root, '.state', 'harness-home', 'sessions', 'wk1', 'broken-one');
  mkdirSync(badDir, {recursive: true});
  writeFileSync(path.join(badDir, SESSION_FILE), Buffer.from('not-a-zstd-frame'));
  writeRun(root, 'run-gw', {session_id: rootId, status: 'completed'});
  const stats = importHistory({root});
  const entry = stats.runs[0];
  assert.equal(entry.status, 'imported');
  assert.equal(entry.partial, true);
  const records = readTelemetry(path.join(root, '.state', 'runs', 'run-gw', 'telemetry.jsonl'));
  assert.equal(records.at(-1).status, 'partial');
  assert.match(records.at(-1).text, /索引告警/);
});

test('.state/monitor 为符号链接时拒绝锁与统计写入', () => {
  const root = makeRoot();
  const rootId = 'root-monlink';
  writeSession(root, 'wk1', rootId, headerOf(rootId), [userMessage(1_700_000_101_000, 'x')], 1);
  writeRun(root, 'run-monlink', {session_id: rootId, status: 'completed'});
  const outside = path.join(root, 'outside-monitor');
  mkdirSync(outside, {recursive: true});
  symlinkSync(outside, path.join(root, '.state', 'monitor'));
  const stats = importHistory({root});
  assert.equal(stats.imported, 0);
  assert.equal(stats.failed, 1);
  assert.match(stats.runs[0].reason, /符号链接/);
  assert.ok(!existsSync(path.join(root, '.state', 'runs', 'run-monlink', 'telemetry.jsonl')));
  assert.throws(() => persistStats(root, {imported: 0}), /符号链接/);
});

test('telemetry 输出截断时标 partial 并保留 warning', () => {
  const root = makeRoot();
  const rootId = 'root-trunc';
  const events = [userMessage(1_700_000_101_000, 'first')];
  for (let index = 0; index < 40; index += 1) {
    events.push(userMessage(1_700_000_102_000 + index, 'x'.repeat(200), index + 2));
  }
  writeSession(root, 'wk1', rootId, headerOf(rootId), events, 1);
  writeRun(root, 'run-trunc', {session_id: rootId, status: 'completed'});
  const stats = importHistory({root, telemetryMaxBytes: 2048});
  const entry = stats.runs[0];
  assert.equal(entry.status, 'imported');
  assert.equal(entry.partial, true);
  assert.ok(entry.warnings.some(warning => warning.includes('截断')));
  const records = readTelemetry(path.join(root, '.state', 'runs', 'run-trunc', 'telemetry.jsonl'));
  assert.equal(records.at(-1).status, 'partial');
});

let failures = 0;
for (const {name, fn} of tests) {
  try {
    fn();
    process.stdout.write(`ok - ${name}\n`);
  } catch (error) {
    failures += 1;
    process.stdout.write(`not ok - ${name}\n  ${error && error.stack ? error.stack.split('\n').slice(0, 4).join('\n  ') : error}\n`);
  }
}
process.stdout.write(`\n${tests.length - failures}/${tests.length} passed\n`);
if (failures > 0) process.exitCode = 1;
