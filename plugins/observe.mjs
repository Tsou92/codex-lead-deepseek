// Real Harness observability for the lead agent and every subagent.
//
// Sources (verified against the pinned 0.1.7-alpha.2 type surface under
// `.monitor-context/runtime/`):
//   - `agent/created` / `agent/disposed`  (dsh-agent runtime-types): lifecycle
//     plus `agent.options.model`. Agent.id IS the SessionId.
//   - `agent/status`                      (dsh-agent runtime-types): payload
//     `{agent, status}` where status is 'idle' | 'running'; disposal is not a
//     third status, so a continuable child that finishes a turn but is never
//     disposed becomes observable as an honest `idle` finish.
//   - `subagent/start` / `subagent/end`   (dsh-subagent types): subagent
//     lifecycle pair keyed by runId; continuable children also surface through
//     `agent/created`, so they remain observable even without a SubagentRun.
//   - `session/event`                     (dsh-session index): durable
//     `user/message`, `assistant/message` (carries the step usage),
//     `tool/call`, `tool/result` for every session, child sessions included.
//
// Hidden content is never read: `system/message` and `developer/message` are
// ignored outright, and reasoning/thinking/stream fields are never touched.
import {closeSync, fchmodSync, mkdirSync, openSync, statSync, writeSync} from 'node:fs';
import {dirname} from 'node:path';

export const name = 'leadseek-observe';

const DEFAULT_MAX_BYTES = 8 * 1024 * 1024;
const DEFAULT_MAX_TEXT_CHARS = 65536;
const TRUNCATION_MARK = '…[truncated]';
const REDACTION_MARK = '[redacted]';
// Room kept free so the final "truncated" notice always fits under the cap.
const NOTICE_RESERVE_BYTES = 512;
const SENSITIVE_KEY = /^(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|authorization|password|passwd|secret|client[_-]?secret|token)$/i;

const SECRET_PATTERNS = [
  /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
  /\bsk-[A-Za-z0-9_-]{12,}/g,
  /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/gi,
  /\bAuthorization\b["']?\s*[:=]\s*("[^"\n]*"|'[^'\n]*'|[^\n,;}]+)/gi,
  /\b(?:api[_-]?key|password|passwd|secret|token)\b["']?\s*[:=]\s*("[^"\n]*"|'[^'\n]*'|[^\s,;}]+)/gi,
];

function redactText(value) {
  let text = value;
  for (const pattern of SECRET_PATTERNS) text = text.replace(pattern, REDACTION_MARK);
  return text;
}

function looksLikeJson(text) {
  return (text.startsWith('{') && text.endsWith('}')) || (text.startsWith('[') && text.endsWith(']'));
}

/**
 * Structurally redact a parsed JSON value: sensitive object keys are replaced
 * wholesale, every other string is scrubbed, and JSON embedded inside a string
 * is parsed again so nested payloads cannot smuggle a credential through.
 */
function redactJson(value, depth = 0) {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (depth < 6 && looksLikeJson(trimmed)) {
      try {
        return JSON.stringify(redactJson(JSON.parse(value), depth + 1));
      } catch {
        // not valid JSON after all; fall through to text scrubbing
      }
    }
    return redactText(value);
  }
  if (Array.isArray(value)) return value.map(item => redactJson(item, depth + 1));
  if (value && typeof value === 'object') {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      out[key] = SENSITIVE_KEY.test(key) ? REDACTION_MARK : redactJson(item, depth + 1);
    }
    return out;
  }
  return value;
}

/** Replace common credential shapes; not a general secret detector. */
export function redact(value) {
  if (typeof value !== 'string' || value === '') return value;
  const trimmed = value.trim();
  if (looksLikeJson(trimmed)) {
    try {
      return JSON.stringify(redactJson(JSON.parse(value)));
    } catch {
      // plain-text fallback below
    }
  }
  return redactText(value);
}

/** Join the public text blocks of one message, ignoring every other block kind. */
export function extractText(content, maxChars = DEFAULT_MAX_TEXT_CHARS) {
  if (!Array.isArray(content)) return '';
  const parts = [];
  for (const block of content) {
    if (block && block.type === 'text' && typeof block.text === 'string') parts.push(block.text);
  }
  let text = redact(parts.join('\n'));
  if (typeof maxChars === 'number' && maxChars > 0 && text.length > maxChars) {
    text = text.slice(0, maxChars) + TRUNCATION_MARK;
  }
  return text;
}

function clip(value, maxChars) {
  if (typeof value !== 'string') return value;
  const text = redact(value);
  if (typeof maxChars === 'number' && maxChars > 0 && text.length > maxChars) {
    return text.slice(0, maxChars) + TRUNCATION_MARK;
  }
  return text;
}

function idOf(value) {
  return value === null || value === undefined ? null : String(value);
}

function sessionIdOf(session) {
  return session && session.id !== undefined && session.id !== null ? String(session.id) : null;
}

function parentIdOf(session) {
  const header = session && session.header;
  if (!header) return null;
  return idOf(header.parentSession);
}

function isChildSession(session) {
  const header = session && session.header;
  if (!header) return false;
  if (header.parentSession) return true;
  if (header.origin === 'subagent') return true;
  return typeof header.delegationDepth === 'number' && header.delegationDepth > 0;
}

/** Keep only the five native usage counters named by the monitor contract. */
export function normalizeUsage(usage) {
  if (!usage || typeof usage !== 'object' || Array.isArray(usage)) return null;
  const allowed = ['inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens', 'totalTokens'];
  const out = {};
  let present = false;
  for (const key of allowed) {
    const value = usage[key];
    if (typeof value === 'number' && Number.isFinite(value)) {
      out[key] = value;
      present = true;
    }
  }
  return present ? out : null;
}

function createWriter(telemetryPath, maxBytes, nowFn) {
  mkdirSync(dirname(telemetryPath) || '.', {recursive: true});
  const fd = openSync(telemetryPath, 'a', 0o600);
  fchmodSync(fd, 0o600);
  let bytes = 0;
  try {
    bytes = statSync(telemetryPath).size;
  } catch {
    bytes = 0;
  }
  let stopped = false;
  return {
    get size() {
      return bytes;
    },
    get stopped() {
      return stopped;
    },
    write(record) {
      if (stopped) return false;
      const line = JSON.stringify(record) + '\n';
      const size = Buffer.byteLength(line, 'utf8');
      // Reserve headroom so the truncation notice itself always still fits.
      if (bytes + size > maxBytes - NOTICE_RESERVE_BYTES) {
        const notice = {
          timestamp: nowFn(),
          type: 'telemetry_status',
          agent_id: null,
          parent_id: null,
          session_id: record.session_id === undefined ? null : record.session_id,
          status: 'truncated',
          text: `telemetry truncated: reached the ${maxBytes}-byte limit; later public events were not recorded`,
        };
        const noticeLine = JSON.stringify(notice) + '\n';
        const noticeSize = Buffer.byteLength(noticeLine, 'utf8');
        if (bytes + noticeSize <= maxBytes) {
          writeSync(fd, noticeLine);
          bytes += noticeSize;
        }
        stopped = true;
        return false;
      }
      writeSync(fd, line);
      bytes += size;
      return true;
    },
    close() {
      try {
        closeSync(fd);
      } catch {
        // already closed
      }
    },
  };
}

function baseRecord(type, nowFn, session, extra = {}) {
  const record = {
    timestamp: nowFn(),
    type,
    agent_id: sessionIdOf(session),
    parent_id: parentIdOf(session),
    session_id: sessionIdOf(session),
  };
  for (const [key, value] of Object.entries(extra)) {
    if (value !== undefined) record[key] = value;
  }
  return record;
}

/**
 * Build one observer. `config.telemetryPath` is required; `config.maxBytes`
 * defaults to 8 MiB and `config.maxTextChars` caps each public string.
 */
export function createObserver(config, options = {}) {
  if (!config || typeof config.telemetryPath !== 'string' || config.telemetryPath === '') {
    throw new Error('leadseek-observe requires a non-empty telemetryPath');
  }
  const maxBytes = Number.isSafeInteger(config.maxBytes) && config.maxBytes > 0
    ? config.maxBytes : DEFAULT_MAX_BYTES;
  const maxTextChars = Number.isSafeInteger(config.maxTextChars) && config.maxTextChars > 0
    ? config.maxTextChars : DEFAULT_MAX_TEXT_CHARS;
  const nowFn = options.now || (() => new Date().toISOString());
  const writer = options.writer || createWriter(config.telemetryPath, maxBytes, nowFn);

  const modelBySession = new Map();
  const parentBySession = new Map();
  const childSessions = new Set();
  const statusBySession = new Map();
  const startedSubagents = new Set();
  const finishedSubagents = new Set();
  const usageSteps = new Set();

  // Observation must never break the observed execution: sink failures are
  // swallowed here instead of propagating out of an event hook.
  function emit(record) {
    try {
      writer.write(record);
    } catch {
      // ignore telemetry write failures
    }
  }

  function guard(handler) {
    return (...args) => {
      try {
        return handler(...args);
      } catch {
        return undefined;
      }
    };
  }

  function remember(session) {
    const id = sessionIdOf(session);
    if (id) {
      const parent = parentIdOf(session);
      if (parent) parentBySession.set(id, parent);
      if (isChildSession(session)) childSessions.add(id);
    }
    return id;
  }

  function markStarted(session) {
    const id = sessionIdOf(session);
    if (!id || startedSubagents.has(id)) return;
    startedSubagents.add(id);
    emit(baseRecord('subagent_started', nowFn, session, {status: 'started'}));
  }

  function agentCreated(payload) {
    const agent = payload && payload.agent;
    const session = agent && agent.session;
    if (!session) return;
    const id = remember(session);
    if (agent && agent.options && typeof agent.options.model === 'string') {
      modelBySession.set(id, agent.options.model);
    }
    const model = modelBySession.get(id);
    emit(baseRecord('agent_created', nowFn, session, {
      status: payload && payload.source ? String(payload.source) : undefined,
      model,
    }));
    if (isChildSession(session)) markStarted(session);
  }

  function agentDisposed(payload) {
    const agent = payload && payload.agent;
    const session = agent && agent.session;
    if (!session) return;
    const id = remember(session);
    statusBySession.delete(id);
    // Disposal is only that; it is never claimed as successful completion.
    emit(baseRecord('agent_finished', nowFn, session, {status: 'disposed'}));
  }

  // `idle` means no driver remains. A continuable child that completes work
  // without being disposed therefore surfaces an honest idle/completed finish
  // instead of staying invisible until (or unless) it is disposed.
  function agentStatus(payload) {
    const agent = payload && payload.agent;
    const session = agent && agent.session;
    if (!session) return;
    const id = remember(session);
    const status = payload && typeof payload.status === 'string' ? payload.status : null;
    if (!id || status === null) return;
    const previous = statusBySession.get(id);
    if (previous === status) return;
    statusBySession.set(id, status);
    if (status === 'idle' && previous === 'running' && childSessions.has(id)) {
      emit(baseRecord('agent_finished', nowFn, session, {status: 'idle'}));
    }
  }

  function subagentStart(info) {
    const session = info && info.id !== undefined && info.id !== null
      ? {id: info.id, header: {parentSession: parentBySession.get(String(info.id))}}
      : null;
    if (!session) return;
    markStarted(session);
  }

  function subagentEnd(info) {
    const id = info && info.id !== undefined && info.id !== null ? String(info.id) : null;
    if (!id || finishedSubagents.has(id)) return;
    finishedSubagents.add(id);
    const reason = info && info.stopReason;
    const status = reason && typeof reason === 'object' ? (reason.kind || 'ended') : 'ended';
    const session = {id, header: {parentSession: parentBySession.get(id)}};
    emit(baseRecord('subagent_finished', nowFn, session, {status: String(status)}));
  }

  function emitMessage(session, role, content) {
    emit(baseRecord('message', nowFn, session, {role, text: extractText(content, maxTextChars)}));
  }

  function sessionEvent(session, event) {
    if (!session || !event || typeof event.type !== 'string') return;
    const data = event.data || {};
    switch (event.type) {
      case 'user/message':
        emitMessage(session, 'user', data.content);
        break;
      case 'assistant/message': {
        const model = modelBySession.get(sessionIdOf(session));
        emit(baseRecord('message', nowFn, session, {
          role: 'assistant',
          text: extractText(data.message && data.message.content, maxTextChars),
          model,
        }));
        const usage = normalizeUsage(data.usage);
        if (usage) {
          const stepId = `${data.turn}.${data.step}`;
          const key = `${sessionIdOf(session)}:${data.turn}:${data.step}`;
          if (!usageSteps.has(key)) {
            usageSteps.add(key);
            emit(baseRecord('usage', nowFn, session, {step_id: stepId, usage, model}));
          }
        }
        break;
      }
      case 'tool/call':
        emit(baseRecord('tool_call', nowFn, session, {
          step_id: `${data.turn}.${data.step}`,
          tool: typeof data.name === 'string' ? data.name : undefined,
          call_id: idOf(data.callId),
          input: clip(data.arguments, maxTextChars),
        }));
        break;
      case 'tool/result': {
        const message = data.message || {};
        emit(baseRecord('tool_result', nowFn, session, {
          step_id: `${data.turn}.${data.step}`,
          call_id: idOf(message.toolCallId),
          result: extractText(message.content, maxTextChars),
          status: message.isError ? 'error' : 'ok',
        }));
        break;
      }
      default:
        // system/message, developer/message, request/header, assistant/attempt,
        // reasoning, and every boundary event are intentionally not recorded.
        break;
    }
  }

  return {
    agentCreated: guard(agentCreated),
    agentDisposed: guard(agentDisposed),
    agentStatus: guard(agentStatus),
    subagentStart: guard(subagentStart),
    subagentEnd: guard(subagentEnd),
    sessionEvent: guard(sessionEvent),
    close: () => {
      try {
        writer.close();
      } catch {
        // ignore telemetry close failures
      }
    },
    get truncated() {
      return writer.stopped;
    },
    redact,
  };
}

/** Cordis plugin entry: wire the real hooks into one telemetry sink. */
export function apply(ctx, config) {
  const observer = createObserver(config);
  const safe = handler => (...args) => {
    try {
      return handler(...args);
    } catch {
      return undefined;
    }
  };
  ctx.on('agent/created', safe(payload => observer.agentCreated(payload)));
  ctx.on('agent/disposed', safe(payload => observer.agentDisposed(payload)));
  ctx.on('agent/status', safe(payload => observer.agentStatus(payload)));
  ctx.on('subagent/start', safe(info => observer.subagentStart(info)));
  ctx.on('subagent/end', safe(info => observer.subagentEnd(info)));
  ctx.on('session/event', safe((session, event) => observer.sessionEvent(session, event)));
  // Close the descriptor when the plugin scope unloads.
  if (typeof ctx.effect === 'function') {
    ctx.effect(() => () => observer.close());
  } else if (typeof ctx.on === 'function') {
    ctx.on('dispose', () => observer.close());
  }
  return observer;
}
