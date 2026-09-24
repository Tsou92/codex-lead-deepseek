"""Reduce DSH ``--json`` JSONL event streams into a compact summary.

The reducer reads an event log line by line and returns a small dict with
counters and the last final answer. Verbose payloads (thinking, text, input,
result, ...) are deliberately never returned.
"""

from __future__ import annotations

import json

__all__ = ["reduce_events"]

_ERROR_STATUSES = frozenset({"error", "failed", "failure", "fail"})
_MAX_ERRORS = 5
_MAX_ERROR_CHARS = 300
_DEFAULT_MAX_CHARS = 2500


def _event_type(event):
    """Return the string event type, accepting ``type`` then ``event``."""
    for key in ("type", "event"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    return None


def _as_text(value):
    """Coerce a payload field to text, treating ``None`` as empty."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def reduce_events(path, max_chars=_DEFAULT_MAX_CHARS):
    """Summarize the DSH JSONL file at *path*.

    Args:
        path: Path to the JSONL file. A missing file raises
            :class:`FileNotFoundError`.
        max_chars: Maximum number of characters kept from the last ``final``
            event's text. Must be at least 1.

    Returns:
        A dict with session/final/turn/tool/error/usage statistics.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")

    result = {
        "session_id": None,
        "final_present": False,
        "final_text": "",
        "final_truncated": False,
        "turn_reason": None,
        "tool_counts": {},
        "tool_errors": 0,
        "errors": [],
        "malformed_lines": 0,
        "event_count": 0,
        "usage": {},
    }

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                result["malformed_lines"] += 1
                continue

            if not isinstance(event, dict):
                result["malformed_lines"] += 1
                continue

            result["event_count"] += 1
            event_type = _event_type(event)

            if event_type == "session":
                session_id = event.get("sessionId", event.get("session_id"))
                if isinstance(session_id, str):
                    result["session_id"] = session_id

            elif event_type == "final":
                result["final_present"] = True
                text = _as_text(event.get("text"))
                truncated = len(text) > max_chars
                if truncated:
                    text = text[:max_chars]
                result["final_text"] = text
                result["final_truncated"] = truncated

            elif event_type == "status":
                phase = event.get("phase")
                if phase == "turn_end":
                    reason = event.get("reason")
                    # Harness 0.1.7 emits {kind: "completed"}, not a bare string.
                    if isinstance(reason, dict):
                        reason = reason.get("kind")
                    if reason is not None:
                        result["turn_reason"] = _as_text(reason)
                elif phase == "step_end":
                    usage = event.get("usage")
                    if isinstance(usage, dict):
                        for key, value in usage.items():
                            if isinstance(value, bool) or not isinstance(
                                value, (int, float)
                            ):
                                continue
                            result["usage"][key] = (
                                result["usage"].get(key, 0) + value
                            )

            elif event_type == "tool_call":
                tool = event.get("tool")
                if isinstance(tool, str):
                    result["tool_counts"][tool] = (
                        result["tool_counts"].get(tool, 0) + 1
                    )

            elif event_type == "tool_result":
                status = event.get("status")
                if isinstance(status, str) and status.lower() in _ERROR_STATUSES:
                    result["tool_errors"] += 1

            elif event_type == "error":
                if len(result["errors"]) < _MAX_ERRORS:
                    message = _as_text(event.get("message"))
                    result["errors"].append(message[:_MAX_ERROR_CHARS])

            # Unknown event types are counted above and otherwise ignored.

    return result
