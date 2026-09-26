// Shared normalization for the public `tool/result` message across Harness
// versions. Kept separate so observe.mjs (call id + text + status) and
// legacy-headless.mjs (status only) agree without importing each other.
//
// Two public shapes are understood, and only these:
//
//   modern (0.1.7-alpha surface):
//     {role:'tool', toolCallId, content:[{type:'text',text}], isError}
//
//   legacy (public "latest" 0.1.5-rc.3 shape):
//     {role:'user', id, source:{kind:'tool', callId},
//      content:[{type:'tool-result', toolCallId,
//                content:[{type:'text',text}], isError}]}
//
// Only the public tool-result text is exposed: `content` in the result is the
// block list that text extraction may scan (`type:'text'` only), never an
// arbitrary walk of unknown blocks. Hidden reasoning/system/developer content
// is deliberately not reachable from here.

function idOf(value) {
  return value === null || value === undefined ? null : String(value);
}

/**
 * Normalize one `tool/result` message. Returns `{callId, isError, content}`
 * where `content` is the public block list to extract text from, or `null` when
 * the message is not a recognizable tool result.
 */
export function normalizeToolResult(message) {
  if (!message || typeof message !== 'object') return null;

  const source = message.source;
  if (source && typeof source === 'object' && source.kind === 'tool') {
    if (!Array.isArray(message.content)) return null;
    const block = message.content.find(item => item && item.type === 'tool-result');
    if (!block) return null;
    return {
      // The nested block is authoritative; source.callId is the fallback.
      callId: idOf(block.toolCallId !== undefined ? block.toolCallId : source.callId),
      isError: block.isError === true,
      content: Array.isArray(block.content) ? block.content : [],
    };
  }

  // Modern top-level shape. Require a tool marker so an unrelated message is
  // never misread as a tool result.
  if (message.role !== 'tool' && message.toolCallId === undefined) return null;
  return {
    callId: idOf(message.toolCallId),
    isError: message.isError === true,
    content: Array.isArray(message.content) ? message.content : [],
  };
}
