// Legacy (@deepseek-ai/dsh-headless "latest") compatibility adapter.
//
// The installed official one-shot runner only accepts a positional task,
// streams reasoning to stderr and prints the final assistant text to stdout; it
// has no `--json` projection.  The bridge needs the same JSONL event protocol
// the modern `--json` profile emits, so this required plugin:
//   * replaces the runner's public `internals.stdout` *object* with a
//     `{"type":"final"}` JSONL sink, and
//   * projects the root agent's `session/event` feed into the reducer shape
//     (session, status turn_end/step_end, tool_call/tool_result, error).
// It never fabricates a final event or a turn reason: both come from the real
// runner output and the durable session log.  Hidden content (system/developer
// messages, reasoning) is never projected.
//
// Ordering is a service dependency, not a write hijack: the official runner
// captures `internals.stdout` when it mounts, so the patch makes it inject the
// `leadseekLegacyReady` service this plugin provides after the swap.  The
// previous sink object is never mutated (it is `process.stdout`), which would
// otherwise intercept every process write as a fake final.
import {normalizeToolResult} from './tool-result.mjs';

const HEADLESS_MODULE = '../runtime/node_modules/@deepseek-ai/dsh-headless/lib/index.js';

export const name = 'leadseek-legacy-headless';
// Apply as early as possible: runners that list READY_SERVICE wait for us.
export const inject = [];

/** Service the patched official runner injects, releasing it after the swap. */
export const READY_SERVICE = 'leadseekLegacyReady';

function isChildSession(session) {
  const header = session && session.header;
  if (!header) return false;
  if (header.parentSession) return true;
  if (header.origin === 'subagent') return true;
  return typeof header.delegationDepth === 'number' && header.delegationDepth > 0;
}

function sessionIdOf(session) {
  return session && session.id !== undefined && session.id !== null
    ? String(session.id) : null;
}

function toText(value) {
  if (typeof value === 'string') return value;
  if (value === undefined || value === null) return '';
  return Buffer.isBuffer(value) ? value.toString('utf8') : String(value);
}

/**
 * Wire the JSONL projection. Exported for tests; `apply` supplies the real
 * official `internals`. Throws when the public stdout interface is missing so a
 * wrong runtime fails loudly instead of losing the completion signal.
 *
 * Only `internals.stdout` is reassigned. The original object's `write` is left
 * byte-for-byte untouched.
 */
export function createCompat(ctx, internals, options = {}) {
  if (!internals || !internals.stdout || typeof internals.stdout.write !== 'function') {
    throw new Error('legacy-headless: official internals.stdout is unavailable; refusing to run without JSONL final events');
  }
  // Bind the real sink before any wrapping so emitted events never recurse.
  const write = options.write || process.stdout.write.bind(process.stdout);
  let closed = false;
  let rootSessionId = null;
  const usageByStep = new Map();

  function emitFinal(text) {
    emit({type: 'final', text: text.endsWith('\n') ? text.slice(0, -1) : text});
  }

  function emit(event) {
    if (closed) return;
    write(JSON.stringify(event) + '\n');
  }

  const sink = {write(chunk) { emitFinal(toText(chunk)); return true; }};
  // Swap the public stdout target only. Never touch the previous sink's
  // `write`: `internals.stdout` is `process.stdout`, so patching it would
  // hijack the whole process and disguise every log line as a final event.
  internals.stdout = sink;

  function onAgentCreated(payload) {
    const agent = payload && payload.agent;
    const session = agent && agent.session;
    if (!session || isChildSession(session)) return;
    const id = sessionIdOf(session)
      || (agent && agent.id !== undefined && agent.id !== null ? String(agent.id) : null);
    if (!id || rootSessionId !== null) return;
    rootSessionId = id;
    emit({type: 'session', sessionId: id});
  }

  function onSessionEvent(session, event) {
    if (!rootSessionId || !session || sessionIdOf(session) !== rootSessionId) return;
    if (!event || typeof event.type !== 'string') return;
    const data = event.data || {};
    switch (event.type) {
      case 'turn/end':
        emit({type: 'status', phase: 'turn_end', reason: data.reason});
        if (data.reason && data.reason.kind === 'error') {
          const error = data.reason.error || {};
          emit({type: 'error', message: toText(error.message || data.reason.error || 'turn error')});
        }
        return;
      case 'step/end': {
        const usage = data.usage || usageByStep.get(`${data.turn}.${data.step}`);
        if (usage) emit({type: 'status', phase: 'step_end', usage});
        return;
      }
      case 'assistant/message': {
        const usage = data.usage;
        if (usage && typeof usage === 'object') {
          usageByStep.set(`${data.turn}.${data.step}`, usage);
        }
        return;
      }
      case 'tool/call':
        emit({type: 'tool_call', tool: typeof data.name === 'string' ? data.name : undefined});
        return;
      case 'tool/result': {
        const message = data.message || {};
        const normalized = normalizeToolResult(message);
        const isError = normalized ? normalized.isError : message.isError === true;
        emit({type: 'tool_result', status: isError ? 'error' : 'ok'});
        return;
      }
      case 'error':
        emit({type: 'error', message: toText(data.message || event.message)});
        return;
      default:
        // system/message, developer/message, reasoning and boundary events are
        // intentionally not projected.
        return;
    }
  }

  ctx.on('agent/created', payload => { onAgentCreated(payload); });
  ctx.on('session/event', (session, event) => { onSessionEvent(session, event); });

  return {
    emitFinal,
    close() { closed = true; },
    get rootSessionId() { return rootSessionId; },
  };
}

/**
 * Install the adapter and publish {@link READY_SERVICE}. Returns `undefined`:
 * an async Cordis `apply` must not resolve to an object, or the loader treats
 * the resolved value as an effect and fails at `safeCollect`.
 */
export function installCompat(ctx, internals, options = {}) {
  const compat = createCompat(ctx, internals, options);
  if (typeof ctx.effect === 'function') {
    ctx.effect(() => () => compat.close());
  } else if (typeof ctx.on === 'function') {
    ctx.on('dispose', () => compat.close());
  }
  if (typeof ctx.provide !== 'function') {
    throw new Error('leadseek-legacy-headless: ctx.provide 不可用，无法保证 runner 在 internals.stdout 替换之后才挂载');
  }
  ctx.provide(READY_SERVICE, {ready: true});
  return undefined;
}

/** Cordis entry: load the official internals export and install the adapter. */
export async function apply(ctx, config) {
  let headless;
  try {
    headless = await import(HEADLESS_MODULE);
  } catch (error) {
    throw new Error('leadseek-legacy-headless: 无法加载官方 headless internals（不静默降级）: '
      + (error && error.message ? error.message : String(error)));
  }
  if (!headless || !headless.internals) {
    throw new Error('leadseek-legacy-headless: 官方 headless 未导出 internals，无法保证 JSONL 终止事件');
  }
  installCompat(ctx, headless.internals, {});
  // Returns undefined on purpose; see installCompat.
}
