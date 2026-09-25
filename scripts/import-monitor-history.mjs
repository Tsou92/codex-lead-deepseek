#!/usr/bin/env node
// Offline recovery of saved Harness sessions into monitor telemetry.jsonl.
//
// This tool never calls a network, model or the live Harness.  It reads the
// project-local compressed session store under
// `<root>/.state/harness-home/sessions/<workspace-key>/<session-id>/session.v4.jsonl.zstd`,
// rebuilds the public event stream through the existing
// `plugins/observe.mjs` createObserver (so normalization/filtering/redaction
// stays in one place), and atomically writes `<run>/telemetry.jsonl`.
//
// Only finished runs with a recorded session_id are recovered.  Sessions are
// attributed to a run strictly through `header.parentSession`; unrelated
// sessions are never written.
import {
  chmodSync, closeSync, constants, existsSync, lstatSync, mkdirSync, openSync,
  readdirSync, readFileSync, renameSync, rmSync, statSync, unlinkSync,
  writeFileSync, writeSync,
} from 'node:fs';
import {zstdDecompressSync} from 'node:zlib';
import {createObserver} from '../plugins/observe.mjs';

// O_NOFOLLOW is a POSIX extension; fall back to 0 where it is unavailable.
const NOFOLLOW = constants.O_NOFOLLOW || 0;
const OPEN_EXCL_NOFOLLOW = constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | NOFOLLOW;
const OPEN_APPEND_NOFOLLOW = constants.O_WRONLY | constants.O_APPEND | constants.O_CREAT | NOFOLLOW;

export const COMPRESSED_LIMIT = 32 * 1024 * 1024;
export const DECOMPRESSED_LIMIT = 32 * 1024 * 1024;
export const MAX_SCAN_SESSIONS = 4096;
const HEADER_FRAME_LIMIT = 4 * 1024 * 1024;
const TELEMETRY_MAX_BYTES = DECOMPRESSED_LIMIT;
const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
// Only these explicit finish states may be recovered. Anything else (running,
// preparing, queued, unknown) is skipped so an unfinished run is never frozen
// into a misleading historical telemetry.
const FINISHED_STATUSES = new Set([
  'completed', 'failed', 'needs_attention', 'cancelled', 'timed_out',
  'scope_violation', 'log_limit_exceeded',
]);
const PUBLIC_EVENT_TYPES = new Set(['user/message', 'assistant/message', 'tool/call', 'tool/result']);
const FILE_NAME = 'session.v4.jsonl.zstd';
const TELEMETRY_NAME = 'telemetry.jsonl';

export class ImportError extends Error {}

function nowIso() {
  return new Date().toISOString();
}

function lstatOrNull(path) {
  try {
    return lstatSync(path);
  } catch {
    return null;
  }
}

function relInside(root, target) {
  if (!target.startsWith(root)) throw new ImportError(`路径越界: ${target}`);
  const rel = target.slice(root.length).replace(/^[\\/]+/, '');
  if (rel.split(/[\\/]+/).some(part => part === '..')) throw new ImportError(`路径越界: ${target}`);
  return rel;
}

/** Reject any symlink on the way from `root` down to `target`. */
export function assertNoSymlink(root, target, label = 'path') {
  const rel = relInside(root, target);
  let current = root.replace(/\/+$/, '');
  for (const part of rel.split(/[\\/]+/)) {
    if (!part || part === '.') continue;
    current = `${current}/${part}`;
    const info = lstatOrNull(current);
    if (info && info.isSymbolicLink()) {
      throw new ImportError(`拒绝符号链接: ${label} (${current})`);
    }
  }
}

function assertPlainRoot(root) {
  const info = lstatOrNull(root);
  if (!info) throw new ImportError(`根目录不存在: ${root}`);
  if (info.isSymbolicLink()) throw new ImportError(`拒绝符号链接根目录: ${root}`);
  if (!info.isDirectory()) throw new ImportError(`根目录不是目录: ${root}`);
}

function decompressFrame(data, maxOutputLength) {
  const parsed = zstdDecompressSync(data, {info: true, maxOutputLength});
  return {buffer: parsed.buffer, written: parsed.engine.bytesWritten};
}

/** Decode every concatenated zstd frame, not just the first one. */
export function decompressAllFrames(buf, limits = {}) {
  const compressedLimit = limits.compressedLimit ?? COMPRESSED_LIMIT;
  const decompressedLimit = limits.decompressedLimit ?? DECOMPRESSED_LIMIT;
  if (buf.length > compressedLimit) {
    throw new ImportError(`压缩文件超过 ${compressedLimit} 字节上限`);
  }
  const parts = [];
  let offset = 0;
  let total = 0;
  while (offset < buf.length) {
    const remaining = decompressedLimit - total;
    if (remaining <= 0) throw new ImportError('解压后内容超过上限');
    let result;
    try {
      result = decompressFrame(buf.subarray(offset), remaining);
    } catch (error) {
      if (/larger than|too large|maxOutputLength/i.test(error.message)) {
        throw new ImportError('解压后内容超过上限');
      }
      throw new ImportError(`zstd 帧损坏或不完整（偏移 ${offset}）: ${error.message}`);
    }
    if (!Number.isSafeInteger(result.written) || result.written <= 0) {
      throw new ImportError('zstd 帧未推进，文件可能损坏');
    }
    total += result.buffer.length;
    if (total > decompressedLimit) throw new ImportError('解压后内容超过上限');
    parts.push(result.buffer);
    offset += result.written;
  }
  if (parts.length === 0) throw new ImportError('空压缩文件');
  return {buffer: Buffer.concat(parts), frames: parts.length, compressedBytes: buf.length};
}

/** Read only the first frame to index one session header cheaply. */
export function readHeader(filePath) {
  const info = statSync(filePath);
  if (info.size > COMPRESSED_LIMIT) throw new ImportError('压缩文件超过上限');
  const buf = readFileSync(filePath);
  let result;
  try {
    result = decompressFrame(buf, HEADER_FRAME_LIMIT);
  } catch (error) {
    throw new ImportError(`首个 zstd 帧损坏: ${error.message}`);
  }
  const first = result.buffer.toString('utf8').split('\n').find(line => line.trim() !== '');
  if (first === undefined) throw new ImportError('首个 zstd 帧没有 JSON 行');
  try {
    return JSON.parse(first);
  } catch (error) {
    throw new ImportError(`会话 header 不是有效 JSON: ${error.message}`);
  }
}

function extractModel(event) {
  const data = event && event.data;
  if (!data || typeof data !== 'object') return null;
  if (typeof data.model === 'string' && data.model) return data.model;
  if (typeof data.provider === 'string' && data.provider) return data.provider;
  return null;
}

function isChildHeader(header) {
  if (!header) return false;
  if (header.parentSession) return true;
  if (header.origin === 'subagent') return true;
  return typeof header.delegationDepth === 'number' && header.delegationDepth > 0;
}

function eventTimeMs(event, fallback) {
  const value = event && event.time;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    if (!Number.isNaN(parsed)) return parsed;
  }
  const created = fallback ? Date.parse(fallback) : Number.NaN;
  return Number.isNaN(created) ? Date.now() : created;
}

function sessionsRoot(root) {
  return `${root}/.state/harness-home/sessions`;
}

function runsRoot(root) {
  return `${root}/.state/runs`;
}

/** Build a header index across the project-local session store. */
export function indexSessions(root, warnings) {
  const base = sessionsRoot(root);
  const index = new Map();
  if (!existsSync(base)) return index;
  assertNoSymlink(root, base, 'harness sessions');
  let scanned = 0;
  for (const workspace of readdirSync(base)) {
    const workspacePath = `${base}/${workspace}`;
    const workspaceInfo = lstatOrNull(workspacePath);
    if (!workspaceInfo || !workspaceInfo.isDirectory()) continue;
    assertNoSymlink(root, workspacePath, 'workspace key');
    let dirs;
    try {
      dirs = readdirSync(workspacePath);
    } catch (error) {
      warnings.push(`无法读取会话目录 ${workspace}: ${error.message}`);
      continue;
    }
    for (const dir of dirs) {
      if (scanned >= MAX_SCAN_SESSIONS) {
        warnings.push(`扫描会话数达到上限 ${MAX_SCAN_SESSIONS}，其余未索引`);
        return index;
      }
      const sessionDir = `${workspacePath}/${dir}`;
      const sessionInfo = lstatOrNull(sessionDir);
      if (!sessionInfo || !sessionInfo.isDirectory()) continue;
      const file = `${sessionDir}/${FILE_NAME}`;
      if (!existsSync(file)) continue;
      scanned += 1;
      try {
        assertNoSymlink(root, sessionDir, 'session dir');
        assertNoSymlink(root, file, 'session file');
        const header = readHeader(file);
        const id = header && header.id ? String(header.id) : dir;
        index.set(id, {id, header, file, dir: sessionDir});
      } catch (error) {
        warnings.push(`会话 ${dir} header 无法读取: ${error.message}`);
        if (!index.has(dir)) index.set(dir, {id: dir, header: null, file, dir: sessionDir, error: error.message});
      }
    }
  }
  return index;
}

/** Collect the root session plus every descendant linked by parentSession. */
export function collectTree(index, rootId, warnings) {
  const children = new Map();
  for (const entry of index.values()) {
    const parent = entry.header && entry.header.parentSession ? String(entry.header.parentSession) : null;
    if (!parent) continue;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(entry.id);
  }
  const ordered = [];
  const seen = new Set();
  const queue = [rootId];
  while (queue.length > 0) {
    const id = queue.shift();
    if (seen.has(id)) continue;
    seen.add(id);
    const entry = index.get(id);
    if (!entry) {
      warnings.push(`会话 ${id} 不在本机 session store 中`);
      continue;
    }
    ordered.push(entry);
    for (const child of children.get(id) || []) queue.push(child);
  }
  return ordered;
}

function toEvents(buffer) {
  const events = [];
  let dropped = 0;
  for (const line of buffer.toString('utf8').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const value = JSON.parse(trimmed);
      if (value && value.type === 'session') continue; // header is not replayed
      if (value && typeof value.type === 'string') events.push(value);
    } catch {
      dropped += 1;
    }
  }
  return {events, dropped};
}

function replaySession(observer, entry, events, warnings, state, limits) {
  const header = entry.header || {};
  const session = {id: entry.id, header};
  const model = events.reduce(
    (acc, event) => acc || (event.type === 'request/context' ? extractModel(event) : null), null);
  const createdAt = new Date(eventTimeMs(events[0] || {}, header.createdAt)).toISOString();
  state.currentTime = createdAt;
  observer.agentCreated({agent: {session, options: model ? {model} : {}}, source: 'history-import'});
  const child = isChildHeader(header);
  if (child) observer.agentStatus({agent: {session}, status: 'running'});
  for (const event of events) {
    state.currentTime = new Date(eventTimeMs(event, header.createdAt)).toISOString();
    if (!PUBLIC_EVENT_TYPES.has(event.type)) continue; // hidden/boundary events never replayed
    observer.sessionEvent(session, event);
  }
  if (events.length > 0) {
    state.currentTime = new Date(eventTimeMs(events[events.length - 1], header.createdAt)).toISOString();
  }
  if (child) {
    observer.agentStatus({agent: {session}, status: 'idle'});
    observer.subagentEnd({id: entry.id, stopReason: {kind: 'ended'}});
  } else {
    observer.agentDisposed({agent: {session}});
  }
}

function writeStatusFd(fd, sessionId, status, text, time) {
  writeSync(fd, JSON.stringify({
    timestamp: time,
    type: 'telemetry_status',
    agent_id: null,
    parent_id: null,
    session_id: sessionId,
    status,
    text,
  }) + '\n');
}

function appendStatus(path, sessionId, status, text, time) {
  const fd = openSync(path, OPEN_APPEND_NOFOLLOW, 0o600);
  try {
    chmodSync(path, 0o600);
    writeStatusFd(fd, sessionId, status, text, time);
  } finally {
    closeSync(fd);
  }
}

function acquireLock(root, runId) {
  const dir = `${root}/.state/monitor/locks`;
  assertNoSymlink(root, dir, 'monitor locks');
  mkdirSync(dir, {recursive: true, mode: 0o700});
  assertNoSymlink(root, dir, 'monitor locks');
  const lockPath = `${dir}/${runId}.lock`;
  try {
    assertNoSymlink(root, lockPath, 'lock file');
    const fd = openSync(lockPath, OPEN_EXCL_NOFOLLOW, 0o600);
    writeSync(fd, JSON.stringify({run_id: runId, pid: process.pid, at: nowIso()}) + '\n');
    closeSync(fd);
    chmodSync(lockPath, 0o600);
    return lockPath;
  } catch (error) {
    if (error && error.code === 'EEXIST') throw new ImportError('同 run 的导入正在进行中');
    throw error;
  }
}

function releaseLock(lockPath) {
  try {
    unlinkSync(lockPath);
  } catch {
    // lock already gone
  }
}

function readResult(root, runId) {
  const runDir = `${runsRoot(root)}/${runId}`;
  assertNoSymlink(root, runDir, 'run dir');
  const resultPath = `${runDir}/result.json`;
  if (!existsSync(resultPath)) return {runDir, result: null};
  assertNoSymlink(root, resultPath, 'result.json');
  let raw;
  try {
    raw = JSON.parse(readFileSync(resultPath, 'utf8'));
  } catch (error) {
    throw new ImportError(`result.json 无法解析: ${error.message}`);
  }
  if (!raw || typeof raw !== 'object') throw new ImportError('result.json 不是对象');
  return {runDir, result: raw};
}

function importOneRun(root, runId, index, options = {}) {
  const warnings = [];
  const entry = {run_id: runId, status: 'failed', reason: null, warnings, sessions: []};
  const telemetryMaxBytes = Number.isSafeInteger(options.telemetryMaxBytes) && options.telemetryMaxBytes > 0
    ? options.telemetryMaxBytes : TELEMETRY_MAX_BYTES;
  const globalWarnings = Array.isArray(options.globalWarnings) ? options.globalWarnings : [];
  if (globalWarnings.length > 0) {
    // A global index warning can hide an unlinked child; never claim complete.
    entry.partial = true;
    warnings.push(`会话索引告警 ${globalWarnings.length} 条，可能有子会话未被发现: ${globalWarnings.join(' | ').slice(0, 800)}`);
  }
  let lockPath = null;
  let tempPath = null;
  try {
    if (!RUN_ID_PATTERN.test(runId)) throw new ImportError(`非法 run id: ${runId}`);
    const {runDir, result} = readResult(root, runId);
    if (!result) {
      entry.status = 'skipped';
      entry.reason = '尚无 result.json，任务可能仍在执行，未恢复';
      return entry;
    }
    const sessionId = result.session_id ? String(result.session_id) : null;
    const status = typeof result.status === 'string' ? result.status.toLowerCase() : '';
    if (!sessionId) {
      entry.status = 'skipped';
      entry.reason = '没有 session_id';
      return entry;
    }
    if (!FINISHED_STATUSES.has(status)) {
      entry.status = 'skipped';
      entry.reason = `不在结束状态白名单 (status=${result.status ?? '缺失'})`;
      return entry;
    }
    const telemetryPath = `${runDir}/${TELEMETRY_NAME}`;
    const existing = lstatOrNull(telemetryPath);
    if (existing && existing.isSymbolicLink()) throw new ImportError('拒绝符号链接 telemetry.jsonl');
    if (existing && existing.size > 0) {
      entry.status = 'skipped';
      entry.reason = 'telemetry.jsonl 已存在且非空，不覆盖真实采集';
      return entry;
    }

    const rootEntry = index.get(sessionId);
    if (!rootEntry) {
      entry.reason = `根会话 ${sessionId} 不在本机 session store 中`;
      return entry;
    }
    if (!rootEntry.header) {
      entry.reason = `根会话 ${sessionId} header 无法读取: ${rootEntry.error || '未知'}`;
      return entry;
    }
    if (rootEntry.header.isSeeded === true) {
      entry.status = 'skipped';
      entry.reason = '根会话 isSeeded=true 且缺少继承边界，跳过以防生成空 telemetry 替代父历史 usage';
      return entry;
    }
    const tree = collectTree(index, sessionId, warnings);

    lockPath = acquireLock(root, runId);
    tempPath = `${runDir}/.telemetry-import-${process.pid}-${Date.now()}.tmp`;
    assertNoSymlink(root, tempPath, 'temp telemetry');
    writeFileSync(tempPath, '', {mode: 0o600, flag: OPEN_EXCL_NOFOLLOW});

    const state = {currentTime: nowIso()};
    const observer = createObserver(
      {telemetryPath: tempPath, maxBytes: telemetryMaxBytes},
      {now: () => state.currentTime},
    );

    // First visible record: this is a recovered saved session, not a live capture.
    appendStatus(tempPath, sessionId, 'historical_import',
      `历史保存会话恢复：由 Harness 压缩会话导入，导入时间 ${nowIso()}；事件时间为原始时间，不宣称为精确账单`,
      nowIso());

    let importedSessions = 0;
    for (const sessionEntry of tree) {
      const header = sessionEntry.header;
      if (!header) {
        warnings.push(`会话 ${sessionEntry.id} header 缺失，未导入`);
        entry.partial = true;
        continue;
      }
      if (header.isSeeded === true) {
        warnings.push(`会话 ${sessionEntry.id} isSeeded=true 且缺少继承边界，跳过以防重复计入父历史 usage`);
        entry.partial = true;
        continue;
      }
      let decoded;
      try {
        assertNoSymlink(root, sessionEntry.file, 'session file');
        const info = statSync(sessionEntry.file);
        if (info.size > COMPRESSED_LIMIT) throw new ImportError('压缩文件超过上限');
        decoded = decompressAllFrames(readFileSync(sessionEntry.file));
      } catch (error) {
        if (sessionEntry.id === sessionId) {
          throw new ImportError(`根会话解压失败: ${error.message}`);
        }
        warnings.push(`子会话 ${sessionEntry.id} 解压失败，已跳过: ${error.message}`);
        entry.partial = true;
        continue;
      }
      const {events, dropped} = toEvents(decoded.buffer);
      if (dropped > 0) {
        warnings.push(`会话 ${sessionEntry.id} 有 ${dropped} 行 JSONL 损坏，已忽略`);
        entry.partial = true;
      }
      replaySession(observer, sessionEntry, events, warnings, state, telemetryMaxBytes);
      importedSessions += 1;
      entry.sessions.push({session_id: sessionEntry.id, events: events.length, frames: decoded.frames});
    }
    observer.close();

    if (observer.truncated) {
      entry.partial = true;
      warnings.push(`telemetry 输出超过 ${telemetryMaxBytes} 字节上限，后续事件被截断`);
    }
    if (importedSessions === 0) {
      entry.status = 'skipped';
      entry.reason = '没有可导入的会话；不生成空 telemetry 替代父历史流';
      return entry;
    }

    const finalStatus = entry.partial === true ? 'partial' : 'complete';
    const warningBlock = warnings.length > 0
      ? `；缺失/损坏(${warnings.length}): ${warnings.join(' | ').slice(0, 1500)}`
      : '';
    const finalText = `历史保存会话恢复${finalStatus === 'complete' ? '完成' : '为部分结果'}，`
      + `共 ${importedSessions} 个会话；用量来自保存事件，不能宣称总账单精确${warningBlock}`;
    appendStatus(tempPath, sessionId, finalStatus, observer.redact(finalText), nowIso());

    assertNoSymlink(root, telemetryPath, 'telemetry.jsonl');
    const finalInfo = lstatOrNull(telemetryPath);
    if (finalInfo && finalInfo.isSymbolicLink()) throw new ImportError('拒绝符号链接 telemetry.jsonl');
    if (finalInfo && finalInfo.size > 0) {
      entry.status = 'skipped';
      entry.reason = 'telemetry.jsonl 在导入期间被创建，不覆盖';
      return entry;
    }
    renameSync(tempPath, telemetryPath);
    tempPath = null;
    entry.status = 'imported';
    entry.partial = entry.partial === true;
    entry.telemetry_path = telemetryPath;
    entry.sessions_count = importedSessions;
    return entry;
  } catch (error) {
    entry.status = 'failed';
    entry.reason = error instanceof ImportError ? error.message : `${error.name}: ${error.message}`;
    return entry;
  } finally {
    if (tempPath) {
      try {
        rmSync(tempPath, {force: true});
      } catch {
        // best effort cleanup
      }
    }
    if (lockPath) releaseLock(lockPath);
  }
}

/** Import finished historical runs and return the JSON-serializable stats. */
export function importHistory(options = {}) {
  const root = (options.root || process.cwd()).replace(/\/+$/, '') || '/';
  const runIdFilter = options.runId ? String(options.runId) : null;
  const stats = {root, generated_at: nowIso(), imported: 0, skipped: 0, failed: 0, runs: [], warnings: []};
  try {
    assertPlainRoot(root);
  } catch (error) {
    stats.failed = 1;
    stats.runs.push({run_id: runIdFilter || null, status: 'failed', reason: error.message, warnings: []});
    return stats;
  }
  const runsDir = runsRoot(root);
  let runIds = [];
  if (runIdFilter) {
    runIds = [runIdFilter];
  } else if (existsSync(runsDir)) {
    try {
      assertNoSymlink(root, runsDir, 'runs dir');
      runIds = readdirSync(runsDir).filter(name => RUN_ID_PATTERN.test(name)).sort();
    } catch (error) {
      stats.failed = 1;
      stats.runs.push({run_id: null, status: 'failed', reason: error.message, warnings: []});
      return stats;
    }
  }
  const index = indexSessions(root, stats.warnings);
  for (const runId of runIds) {
    const entry = importOneRun(root, runId, index, {
      globalWarnings: stats.warnings,
      telemetryMaxBytes: options.telemetryMaxBytes,
    });
    stats.runs.push(entry);
    if (entry.status === 'imported') stats.imported += 1;
    else if (entry.status === 'skipped') stats.skipped += 1;
    else stats.failed += 1;
  }
  return stats;
}

/** Persist the last import summary under `.state/monitor/history-import.json` (0600). */
export function persistStats(root, stats) {
  const base = String(root).replace(/\/+$/, '') || '/';
  const dir = `${base}/.state/monitor`;
  assertNoSymlink(base, dir, 'monitor dir');
  mkdirSync(dir, {recursive: true, mode: 0o700});
  assertNoSymlink(base, dir, 'monitor dir');
  const target = `${dir}/history-import.json`;
  assertNoSymlink(base, target, 'history-import.json');
  const temp = `${dir}/.history-import-${process.pid}-${Date.now()}.tmp`;
  assertNoSymlink(base, temp, 'history-import temp');
  writeFileSync(temp, JSON.stringify(stats, null, 2) + '\n', {mode: 0o600, flag: OPEN_EXCL_NOFOLLOW});
  chmodSync(temp, 0o600);
  renameSync(temp, target);
  chmodSync(target, 0o600);
}

function parseArgs(argv) {
  const options = {root: process.cwd(), runId: null, help: false};
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--help' || arg === '-h') options.help = true;
    else if (arg === '--root') options.root = argv[++index];
    else if (arg === '--run-id') options.runId = argv[++index];
    else if (arg.startsWith('--root=')) options.root = arg.slice('--root='.length);
    else if (arg.startsWith('--run-id=')) options.runId = arg.slice('--run-id='.length);
  }
  return options;
}

export async function main(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (options.help) {
    process.stdout.write('用法: node scripts/import-monitor-history.mjs [--root ROOT] [--run-id RUN_ID]\n');
    return 0;
  }
  if (!options.root) {
    process.stderr.write('缺少 --root\n');
    return 2;
  }
  const stats = importHistory({root: options.root, runId: options.runId});
  try {
    persistStats(options.root, stats);
  } catch {
    // best effort; stats still go to stdout
  }
  process.stdout.write(JSON.stringify(stats) + '\n');
  return stats.failed > 0 ? 1 : 0;
}

const invokedDirectly = process.argv[1]
  && import.meta.url === (await import('node:url')).pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  main().then(code => {
    process.exitCode = code;
  }).catch(error => {
    process.stderr.write(`导入失败: ${error.message}\n`);
    process.exitCode = 3;
  });
}
