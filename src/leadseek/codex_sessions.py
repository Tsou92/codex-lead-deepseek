"""Read-only access to local Codex session logs plus shared redaction helpers.

The monitor only ever reads sessions that were explicitly referenced (by
``.state/monitor/links.json``, a run's ``context.json`` or a journal decision).
Sessions are located by UUID in :data:`CODEX_HOME`/``sessions`` and the
``session_meta`` id is verified before anything is returned.

Two on-disk usage formats are supported, never mixed:

1. ``response_item`` / ``token_usage_record`` with ``payload.usage``,
   ``payload.turn_token_usage`` and ``payload.thread_token_usage``.  The newest
   ``thread_token_usage`` snapshot is a cumulative value and is therefore taken
   as-is; duplicates are removed by ``response_id`` and never summed.
2. Legacy ``event_msg`` ``token_count`` with ``info.total_token_usage``.  That
   value is a context cumulative counter, so it is labelled as such.

Everything returned is already sanitised: hidden reasoning/thinking, system and
developer instructions, private key material and common credentials are removed
or masked.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "UUID_RE",
    "TEXT_LIMIT",
    "MAX_CODEX_BYTES",
    "normalise_usage",
    "add_usage",
    "redact_text",
    "sanitize",
    "is_hidden_role",
    "find_session_file",
    "parse_session",
    "truncate_text",
]

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

TEXT_LIMIT = 64 * 1024
MAX_CODEX_BYTES = 256 * 1024 * 1024
SESSION_SUBDIR = "sessions"

_REDACTED = "[已脱敏]"

_TOKEN_ALIASES = (
    ("input_tokens", ("input_tokens", "inputTokens", "prompt_tokens")),
    ("output_tokens", ("output_tokens", "outputTokens", "completion_tokens")),
    (
        "cache_read_tokens",
        (
            "cache_read_tokens",
            "cacheReadTokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cached_tokens",
        ),
    ),
    (
        "cache_write_tokens",
        (
            "cache_write_tokens",
            "cacheWriteTokens",
            "cache_creation_input_tokens",
            "cache_write_input_tokens",
        ),
    ),
    ("total_tokens", ("total_tokens", "totalTokens")),
)

_HIDDEN_ROLES = frozenset({"system", "developer", "thinking", "reasoning", "analysis"})
# ``commentary`` is a public progress channel and must stay visible; only true
# internal reasoning channels are hidden.
_HIDDEN_CHANNELS = frozenset({"analysis", "reasoning", "thinking", "internal"})
_PUBLIC_MESSAGE_ROLES = frozenset({"user", "assistant"})
_HIDDEN_KEYS = frozenset(
    {
        "thinking",
        "reasoning",
        "reasoning_content",
        "encrypted_content",
        "internal_reasoning",
        "chain_of_thought",
        "system_instructions",
        "developer_instructions",
        "analysis",
    }
)

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pass",
        "secret",
        "secrets",
        "client_secret",
        "api_key",
        "apikey",
        "api_token",
        "access_token",
        "refresh_token",
        "auth_token",
        "id_token",
        "token",
        "authorization",
        "auth",
        "proxy_authorization",
        "private_key",
        "privatekey",
        "access_key",
        "secret_key",
        "credentials",
        "credential",
        "cookie",
        "set_cookie",
        "session_token",
        "bearer",
    }
)

_CRED_NAME = (
    r"password|passwd|pass|api[_-]?key|apikey|api[_-]?token|secret|secrets|"
    r"client[_-]?secret|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"id[_-]?token|session[_-]?token|authorization|private[_-]?key|privatekey|"
    r"access[_-]?key|secret[_-]?key|credentials?|cookie|set[_-]?cookie|bearer|token"
)


def _redact_quoted(match: "re.Match[str]") -> str:
    return match.group(1) + match.group(2) + _REDACTED + match.group(2)


def _redact_escaped(match: "re.Match[str]") -> str:
    return match.group(1) + '\\"' + _REDACTED + '\\"'


_TEXT_PATTERNS = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.S,
        ),
        "[已脱敏:PRIVATE KEY]",
    ),
    (
        re.compile(
            r"(?i)\b(proxy-authorization|authorization)\s*[:=]\s*"
            r"(?:\bbearer\s+)?\S+"
        ),
        r"\1: " + _REDACTED,
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}"), "Bearer " + _REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), _REDACTED),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), _REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), _REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), _REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), _REDACTED),
    # Credentials embedded in nested / escaped JSON strings:
    #   {\"password\": \"hunter2\"}
    (
        re.compile(
            r'(?i)(\\"(?:%s)\\"\s*:\s*)\\"(?:[^"\\]|\\.)*?\\"' % _CRED_NAME
        ),
        _redact_escaped,
    ),
    # JSON with a quoted credential key and a quoted string value:
    #   "api_key": "sk-..."
    (
        re.compile(
            r'(?i)(["\'](?:%s)["\']\s*[:=]\s*)(["\'])(?:[^"\'\\]|\\.)*?\2'
            % _CRED_NAME
        ),
        _redact_quoted,
    ),
    # Unquoted credential key with a quoted value: password='hunter2'
    (
        re.compile(
            r'(?i)\b(?:%s)\b\s*[:=]\s*(["\'])(?:[^"\'\\]|\\.)*?\1' % _CRED_NAME
        ),
        _REDACTED,
    ),
    # Unquoted credential assignment: token=abcdef123456.  A value made only of
    # digits (a plain token statistic) is deliberately left untouched.
    (
        re.compile(
            r'(?i)\b(?:%s)\b\s*[:=]\s*'
            r'(?=[^\s"\',}]*(?:[A-Za-z_\-/+=]|\.))[^\s"\',}]{4,}' % _CRED_NAME
        ),
        _REDACTED,
    ),
)


def _normalise_key(key: object) -> str:
    return str(key).strip().lower().replace("-", "_").replace(" ", "_")


def redact_text(text: str) -> str:
    """Mask common credential shapes inside free text."""
    if not isinstance(text, str) or not text:
        return text
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize(value: Any, _depth: int = 0) -> Any:
    """Recursively drop hidden fields and redact sensitive values."""
    if _depth > 24:
        return "[截断]"
    if isinstance(value, dict):
        cleaned: Dict[Any, Any] = {}
        for key, item in value.items():
            normalised = _normalise_key(key)
            if normalised in _HIDDEN_KEYS:
                continue
            if normalised in _SENSITIVE_KEYS:
                cleaned[key] = _REDACTED
            else:
                cleaned[key] = sanitize(item, _depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [sanitize(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def is_hidden_role(role: object) -> bool:
    if not isinstance(role, str):
        return False
    return role.strip().lower() in _HIDDEN_ROLES


def _is_hidden_channel(channel: object) -> bool:
    if not isinstance(channel, str):
        return False
    return channel.strip().lower() in _HIDDEN_CHANNELS


def truncate_text(text: str, limit: int = TEXT_LIMIT) -> Tuple[str, bool]:
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _is_number(value: object) -> bool:
    """True for a finite, non-negative int/float (never ``bool``)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, float):
        return math.isfinite(value) and value >= 0
    return False


def normalise_usage(raw: object) -> Optional[Dict[str, Optional[int]]]:
    """Normalise one usage mapping; ``None`` when no recognised number exists."""
    if not isinstance(raw, dict):
        return None
    usage: Dict[str, Optional[int]] = {}
    found = False
    for field, aliases in _TOKEN_ALIASES:
        value: Optional[int] = None
        for alias in aliases:
            candidate = raw.get(alias)
            if _is_number(candidate):
                value = int(candidate)
                break
        usage[field] = value
        if value is not None:
            found = True
    if not found:
        return None
    return usage


def add_usage(
    left: Optional[Dict[str, Optional[int]]],
    right: Optional[Dict[str, Optional[int]]],
) -> Optional[Dict[str, Optional[int]]]:
    """Add two usage dicts field-by-field, keeping unknown values as ``None``."""
    if left is None:
        return right
    if right is None:
        return left
    total: Dict[str, Optional[int]] = {}
    for field in (name for name, _ in _TOKEN_ALIASES):
        a = left.get(field)
        b = right.get(field)
        if a is None:
            total[field] = b
        elif b is None:
            total[field] = a
        else:
            total[field] = a + b
    return total


def _message_text(payload: dict) -> Optional[str]:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    parts: List[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
    if parts:
        return "\n".join(parts)
    text = payload.get("text")
    if isinstance(text, str):
        return text
    return None


def _walk_jsonl(path: Path, limit: int, warnings: List[str]):
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    read_size = min(size, limit)
    if size > limit:
        warnings.append(
            "Codex 会话日志超过读取上限 %d MiB，仅解析开头部分" % (limit // (1024 * 1024))
        )
    with open(str(path), "rb") as handle:
        data = handle.read(read_size)
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if not text.endswith("\n"):
        # A partial tail was never fully written (or the file was truncated).
        lines = lines[:-1]
    for order, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            warnings.append("Codex 会话存在损坏或未写入完成的行，已跳过")
            continue
        if isinstance(obj, dict):
            yield order, obj


def _iter_records(
    records: List[Tuple[int, dict, Optional[str]]]
) -> List[Tuple[int, dict, Optional[str]]]:
    """Deduplicate token records by response_id, keeping the newest copy."""
    deduped: Dict[object, Tuple[int, dict, Optional[str]]] = {}
    for order, payload, turn_id in records:
        response_id = payload.get("response_id")
        key: object = response_id if isinstance(response_id, str) and response_id else ("#", order)
        previous = deduped.get(key)
        if previous is None or order >= previous[0]:
            deduped[key] = (order, payload, turn_id)
    return sorted(deduped.values(), key=lambda item: item[0])


def _session_usage(
    records: List[Tuple[int, dict, Optional[str]]],
    old_counts: List[Tuple[int, dict]],
) -> Tuple[Optional[Dict[str, Optional[int]]], str]:
    if records:
        deduped = _iter_records(records)

        snapshots = []
        for order, payload, _turn_id in deduped:
            usage = normalise_usage(payload.get("thread_token_usage"))
            if usage is not None:
                snapshots.append((order, usage))
        if snapshots:
            snapshots.sort(key=lambda item: item[0])
            return snapshots[-1][1], "thread_token_usage"

        # ``turn_token_usage`` is a cumulative snapshot *inside* one turn, so a
        # turn must be counted once: keep the newest snapshot per turn_id.
        by_turn: Dict[str, Tuple[int, Dict[str, Optional[int]]]] = {}
        unattributed: Optional[Tuple[int, Dict[str, Optional[int]]]] = None
        for order, payload, turn_id in deduped:
            usage = normalise_usage(payload.get("turn_token_usage"))
            if usage is None:
                continue
            if isinstance(turn_id, str) and turn_id:
                previous = by_turn.get(turn_id)
                if previous is None or order >= previous[0]:
                    by_turn[turn_id] = (order, usage)
            elif unattributed is None or order >= unattributed[0]:
                unattributed = (order, usage)

        if by_turn:
            turns: Optional[Dict[str, Optional[int]]] = None
            for _turn_id, (_order, usage) in sorted(
                by_turn.items(), key=lambda item: item[1][0]
            ):
                turns = add_usage(turns, usage)
            return turns, "turn_token_usage"

        per_response = None
        for _order, payload, _turn_id in deduped:
            usage = normalise_usage(payload.get("usage"))
            per_response = add_usage(per_response, usage)
        if per_response is not None:
            return per_response, "response_usage"

        if unattributed is not None:
            # Only cumulative turn snapshots without a turn_id are available;
            # they cannot be safely summed, so surface the last one unchanged.
            return unattributed[1], "unattributed_turn_snapshot"
        return None, "none"

    if old_counts:
        old_counts.sort(key=lambda item: item[0])
        return normalise_usage(old_counts[-1][1]), "event_msg_context_cumulative"
    return None, "none"


def parse_session(path: Path, thread_id: Optional[str] = None) -> Tuple[dict, List[str]]:
    """Parse one Codex session file into a sanitised, bounded structure."""
    warnings: List[str] = []
    meta_id: Optional[str] = None
    model: Optional[str] = None
    status: Optional[str] = None
    started_at: Optional[str] = None
    updated_at: Optional[str] = None
    records: List[Tuple[int, dict, Optional[str]]] = []
    old_counts: List[Tuple[int, dict]] = []
    items: List[dict] = []

    for order, obj in _walk_jsonl(path, MAX_CODEX_BYTES, warnings):
        timestamp = obj.get("timestamp")
        if isinstance(timestamp, str):
            if started_at is None:
                started_at = timestamp
            updated_at = timestamp
        event_type = obj.get("type")
        raw_payload = obj.get("payload")
        payload: dict = raw_payload if isinstance(raw_payload, dict) else {}
        payload_type = payload.get("type")
        turn_id = payload.get("turn_id") or obj.get("turn_id")

        if event_type == "session_meta" or payload_type == "session_meta":
            candidate = payload.get("id") or payload.get("session_id")
            if isinstance(candidate, str):
                meta_id = candidate
            if isinstance(payload.get("model"), str) and not model:
                model = payload["model"]
            if isinstance(payload.get("status"), str):
                status = payload["status"]
            continue

        if event_type == "turn_context" or payload_type == "turn_context":
            if isinstance(payload.get("model"), str):
                model = payload["model"]
            continue

        if event_type == "response_item":
            if payload_type == "message":
                role = payload.get("role")
                if not isinstance(role, str) or role.strip().lower() not in _PUBLIC_MESSAGE_ROLES:
                    continue
                if _is_hidden_channel(payload.get("channel")):
                    continue
                text = _message_text(payload)
                if text is None:
                    continue
                shown, cut = truncate_text(text)
                items.append(
                    {
                        "timestamp": timestamp if isinstance(timestamp, str) else None,
                        "type": "message",
                        "role": role if isinstance(role, str) else None,
                        "text": shown,
                        "truncated": cut,
                        "turn_id": turn_id if isinstance(turn_id, str) else None,
                    }
                )
            elif payload_type in ("function_call", "custom_tool_call"):
                name = payload.get("name")
                argument = payload.get("arguments")
                if argument is None:
                    argument = payload.get("input")
                shown, cut = truncate_text(
                    argument if isinstance(argument, str) else json.dumps(argument, ensure_ascii=False)
                    if argument is not None
                    else ""
                )
                items.append(
                    {
                        "timestamp": timestamp if isinstance(timestamp, str) else None,
                        "type": "tool_call",
                        "tool": name if isinstance(name, str) else None,
                        "call_id": payload.get("call_id"),
                        "input": shown,
                        "truncated": cut,
                        "turn_id": turn_id if isinstance(turn_id, str) else None,
                    }
                )
            elif payload_type in ("function_call_output", "custom_tool_call_output"):
                output = payload.get("output")
                shown, cut = truncate_text(
                    output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
                    if output is not None
                    else ""
                )
                items.append(
                    {
                        "timestamp": timestamp if isinstance(timestamp, str) else None,
                        "type": "tool_result",
                        "call_id": payload.get("call_id"),
                        "result": shown,
                        "truncated": cut,
                        "turn_id": turn_id if isinstance(turn_id, str) else None,
                    }
                )
            elif payload_type == "reasoning":
                continue
            elif payload_type == "token_usage_record":
                records.append(
                    (
                        order,
                        payload,
                        turn_id if isinstance(turn_id, str) and turn_id else None,
                    )
                )
                usage = (
                    normalise_usage(payload.get("thread_token_usage"))
                    or normalise_usage(payload.get("turn_token_usage"))
                    or normalise_usage(payload.get("usage"))
                )
                items.append(
                    {
                        "timestamp": timestamp if isinstance(timestamp, str) else None,
                        "type": "usage",
                        "usage": usage,
                        "turn_id": turn_id if isinstance(turn_id, str) else None,
                    }
                )
            continue

        if event_type == "token_usage_record":
            records.append(
                (
                    order,
                    payload,
                    turn_id if isinstance(turn_id, str) and turn_id else None,
                )
            )
            continue

        if event_type == "event_msg" and payload_type == "token_count":
            info = payload.get("info")
            total = None
            if isinstance(info, dict):
                total = info.get("total_token_usage")
            if total is None:
                total = payload.get("total_token_usage")
            if isinstance(total, dict):
                old_counts.append((order, total))
            continue
        # world_state, compacted, session_meta, reasoning and everything else is
        # intentionally ignored so raw internal state never leaves this module.

    usage, usage_scope = _session_usage(records, old_counts)

    if any(item.get("truncated") for item in items):
        warnings.append("Codex 会话部分内容超过 64 KiB，已截断")
    for position, item in enumerate(items):
        item["index"] = position
        item["agent_id"] = None
        item.setdefault("session_id", thread_id or meta_id)

    available = True
    if thread_id and not meta_id:
        warnings.append("Codex 会话缺少 session_meta，已拒绝返回会话正文与用量")
        available = False
        items = []
        usage = None
        usage_scope = "none"
    elif thread_id and meta_id.lower() != thread_id.lower():
        warnings.append("Codex session_meta ID 与请求的会话不一致，已拒绝")
        available = False
        items = []
        usage = None
        usage_scope = "none"

    data = {
        "thread_id": thread_id or meta_id,
        "model": model,
        "status": status or "unknown",
        "started_at": started_at,
        "updated_at": updated_at,
        "usage": usage,
        "usage_scope": usage_scope,
        "available": available,
        "source_format": (
            "token_usage_record"
            if records
            else ("event_msg_token_count" if old_counts else "none")
        ),
        "items": [sanitize(item) for item in items],
    }
    return data, warnings


def find_session_file(codex_home: Path, thread_id: str) -> Optional[Path]:
    """Locate a session JSONL by UUID inside ``<codex_home>/sessions``."""
    if not thread_id:
        return None
    sessions = Path(codex_home) / SESSION_SUBDIR
    if not sessions.is_dir() or sessions.is_symlink():
        return None
    real_sessions = os.path.realpath(str(sessions))
    matches: List[Path] = []
    wanted = thread_id.lower()
    for dirpath, dirnames, filenames in os.walk(str(sessions)):
        if os.path.islink(dirpath):
            dirnames[:] = []
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if not os.path.islink(os.path.join(dirpath, name))
        ]
        for filename in filenames:
            if not filename.endswith(".jsonl") or wanted not in filename.lower():
                continue
            candidate = Path(dirpath) / filename
            if candidate.is_symlink():
                continue
            real = os.path.realpath(str(candidate))
            if real != real_sessions and not real.startswith(real_sessions + os.sep):
                continue
            matches.append(candidate)
    if not matches:
        return None
    matches.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return matches[0]
