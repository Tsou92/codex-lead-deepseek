"""Local, read-only data layer for the task monitor.

``MonitorStore`` exposes the public methods defined in
``docs/monitor-contract.md``: :meth:`overview`, :meth:`run_detail`,
:meth:`run_events`, :meth:`artifact`, :meth:`codex_detail` and
:meth:`link_codex`.  It reads the run directories under
``<root>/.state/runs`` plus ``<root>/.state/decisions.jsonl`` and
``<root>/.state/monitor/links.json`` using the standard library only.

Safety properties enforced here:

* run ids and Codex thread ids are strictly validated; no path component may be
  a symlink and no directory traversal is possible;
* artifacts are a fixed whitelist of files;
* every returned object is sanitised (hidden reasoning/system content removed,
  common credentials masked);
* JSON/JSONL reads are bounded per file, cached by ``mtime_ns``/``size`` and
  surfaced through ``warnings`` instead of silently pretending to be complete.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import codex_sessions as codex

__all__ = ["MonitorStore"]

RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")

MAX_FILE_BYTES = 32 * 1024 * 1024
TEXT_LIMIT = codex.TEXT_LIMIT
ARTIFACT_MAX_BYTES = 512 * 1024
EVENT_LIMIT_DEFAULT = 100
EVENT_LIMIT_MAX = 500
OVERVIEW_DECISIONS = 100
WARNING_LIMIT = 60
GOAL_LIMIT = 2000

_ARTIFACT_FILES = {
    "prompt": "prompt.txt",
    "patch": "changes.patch",
    "stderr": "stderr.log",
}

_ACCEPT_ACTIONS = frozenset(
    {"accept", "accepted", "accept_and_apply", "accepted_and_applied", "applied", "approve", "approved"}
)
_REVISION_ACTIONS = frozenset({"request_revision", "revision_requested", "revise", "revision"})
_REJECT_ACTIONS = frozenset({"reject", "rejected", "deny", "denied"})

_REVIEW_STATUSES = ("pending", "accepted", "revision_requested", "rejected", "unknown")

# A run whose status is in this set has finished executing.  A root agent that
# never emitted an explicit end event must not be reported as if it were still
# starting/running: the end of the task is the only evidence available.
_TERMINAL_RUN_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "needs_attention",
        "cancelled",
        "timed_out",
        "scope_violation",
        "log_limit_exceeded",
    }
)
# Statuses that mean "no explicit lifecycle outcome was recorded yet".
_AGENT_OPEN_STATUSES = frozenset({"unknown", "startup"})
# Root statuses that may be replaced by ``ended`` at task end.
_AGENT_TERMINAL_MARK_STATUSES = frozenset({"unknown", "startup", "running"})
# Statuses that already constitute an explicit per-agent outcome.
_AGENT_ENDED_STATUSES = frozenset(
    {"finished", "ended", "disposed", "completed", "failed", "cancelled", "timed_out"}
)

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
)

_EVENT_FIELDS = (
    "index",
    "timestamp",
    "type",
    "agent_id",
    "session_id",
    "role",
    "tool",
    "call_id",
    "text",
    "input",
    "result",
    "status",
    "usage",
    "turn_id",
    "truncated",
)


def _as_event(event: dict) -> dict:
    """Project a dict onto the contract's public Event fields."""
    return {field: event.get(field) for field in _EVENT_FIELDS}


_DECISION_FIELDS = ("timestamp", "owner", "action", "reason", "run_id", "codex_thread_id", "workspace")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _empty_usage() -> Dict[str, Optional[int]]:
    return {field: None for field in _USAGE_FIELDS}


def _valid_usage_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, float):
        return math.isfinite(value) and value >= 0
    return False


def _usage_obj(usage: Optional[Dict[str, Optional[int]]]) -> Dict[str, Optional[int]]:
    merged = _empty_usage()
    if isinstance(usage, dict):
        for field in _USAGE_FIELDS:
            value = usage.get(field)
            if _valid_usage_number(value):
                merged[field] = int(value)
    return merged


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value: object) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _elapsed_seconds(started: object, finished: object) -> Optional[float]:
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return round((end - start).total_seconds(), 2)


def _clean_warnings(warnings: Iterable[str], limit: int = WARNING_LIMIT) -> List[str]:
    seen: List[str] = []
    seen_set = set()
    for item in warnings:
        if not isinstance(item, str) or not item:
            continue
        if item in seen_set:
            continue
        seen_set.add(item)
        seen.append(item)
        if len(seen) >= limit:
            seen.append("警告过多，其余已省略")
            break
    return seen


def _aggregate_usage(    events: List[dict],
) -> Tuple[Optional[Dict[str, Optional[int]]], Dict[object, dict], set]:
    """Sum usage events, deduplicated by ``(agent_id, step_id)``."""
    total: Optional[Dict[str, Optional[int]]] = None
    per_agent: Dict[object, dict] = {}
    agents_with_usage = set()
    seen = set()
    for event in events:
        if event.get("type") != "usage":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        agent_id = event.get("agent_id")
        step_id = event.get("step_id")
        if step_id is not None:
            key = (agent_id, step_id)
        else:
            key = (agent_id, event.get("call_id") or event.get("index"))
        if key in seen:
            continue
        seen.add(key)
        total = codex.add_usage(total, usage)
        per_agent[agent_id] = codex.add_usage(per_agent.get(agent_id), usage)
        agents_with_usage.add(agent_id)
    return total, per_agent, agents_with_usage


class _FileCache:
    """Bounded cache keyed by ``(mtime_ns, size)`` to avoid reparsing big logs."""

    def __init__(self, max_entries: int = 256) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, tuple] = {}
        self._order: List[str] = []
        self._max = max_entries

    def get(self, path: Path, mtime_ns: int, size: int):
        key = str(path)
        with self._lock:
            item = self._data.get(key)
            if item is not None and item[0] == mtime_ns and item[1] == size:
                return True, item[2]
        return False, None

    def put(self, path: Path, mtime_ns: int, size: int, value: Any) -> None:
        key = str(path)
        with self._lock:
            self._data[key] = (mtime_ns, size, value)
            if key in self._order:
                self._order.remove(key)
            self._order.append(key)
            while len(self._order) > self._max:
                oldest = self._order.pop(0)
                self._data.pop(oldest, None)


class MonitorStore:
    def __init__(self, root, codex_home=None) -> None:
        self.root = Path(root)
        self.codex_home = Path(
            codex_home
            if codex_home is not None
            else os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
        )
        self._cache = _FileCache()

    # ------------------------------------------------------------------ paths
    @staticmethod
    def _check_component(name: object) -> str:
        if not isinstance(name, str) or name in ("", ".", ".."):
            raise ValueError("非法路径片段")
        if "/" in name or "\\" in name or "\x00" in name or name.startswith("~"):
            raise ValueError("非法路径片段")
        return name

    def _state_dir(self, *parts: str) -> Path:
        base = self.root / ".state"
        if base.is_symlink():
            raise ValueError("拒绝符号链接路径")
        current = base
        for part in parts:
            self._check_component(part)
            current = current / part
            if current.is_symlink():
                raise ValueError("拒绝符号链接路径")
        return current

    def _run_dir(self, run_id: object) -> Path:
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            raise ValueError("无效任务编号")
        runs = self.root / ".state" / "runs"
        if runs.is_symlink():
            raise ValueError("拒绝符号链接路径")
        directory = runs / run_id
        if directory.is_symlink():
            raise ValueError("拒绝符号链接路径")
        if not directory.is_dir():
            raise ValueError("找不到任务: " + run_id)
        return directory

    def _list_run_ids(self) -> List[str]:
        runs = self.root / ".state" / "runs"
        if runs.is_symlink() or not runs.is_dir():
            return []
        found: List[str] = []
        try:
            entries = os.listdir(str(runs))
        except OSError:
            return []
        for entry in entries:
            if not RUN_ID_RE.fullmatch(entry):
                continue
            candidate = runs / entry
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            found.append(entry)
        found.sort(reverse=True)
        return found

    # ----------------------------------------------------------------- readers
    def _cached(self, path: Path, producer):
        try:
            stat = os.stat(str(path))
        except OSError:
            return producer()
        hit, value = self._cache.get(path, stat.st_mtime_ns, stat.st_size)
        if hit:
            return value
        value = producer()
        self._cache.put(path, stat.st_mtime_ns, stat.st_size, value)
        return value

    def _read_bytes(
        self, path: Path, limit: int, warnings: List[str], label: str
    ) -> Optional[bytes]:
        try:
            size = os.path.getsize(str(path))
        except OSError:
            return None
        if os.path.islink(str(path)):
            warnings.append(label + " 是符号链接，已拒绝读取")
            return None
        if size > limit:
            warnings.append(
                "%s 超过 %d MiB 读取上限，仅读取开头部分，结果可能不完整"
                % (label, limit // (1024 * 1024))
            )
        try:
            with open(str(path), "rb") as handle:
                return handle.read(min(size, limit))
        except OSError:
            warnings.append(label + " 无法读取")
            return None

    def _json_file(
        self, path: Path, label: str, limit: int = MAX_FILE_BYTES
    ) -> Tuple[Any, List[str]]:
        def produce():
            warnings: List[str] = []
            data = self._read_bytes(path, limit, warnings, label)
            if data is None:
                return None, warnings
            try:
                return json.loads(data.decode("utf-8")), warnings
            except (UnicodeDecodeError, ValueError):
                warnings.append(label + " 损坏或正在写入，无法解析")
                return None, warnings

        return self._cached(path, produce)

    def _jsonl_file(
        self, path: Path, label: str, limit: int = MAX_FILE_BYTES
    ) -> Tuple[List[dict], List[str]]:
        def produce():
            warnings: List[str] = []
            data = self._read_bytes(path, limit, warnings, label)
            if data is None:
                return [], warnings
            text = data.decode("utf-8", errors="replace")
            partial_tail = not text.endswith("\n")
            raw_lines = text.split("\n")
            lines = raw_lines[:-1] if partial_tail else raw_lines
            objects: List[dict] = []
            bad = 0
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    bad += 1
                    continue
                if isinstance(obj, dict):
                    objects.append(obj)
                else:
                    bad += 1
            if bad:
                warnings.append(
                    "%s 有 %d 行损坏或未写入完成，已跳过" % (label, bad)
                )
            elif partial_tail:
                warnings.append(label + " 末尾行尚未写入完成，已跳过")
            return objects, warnings

        return self._cached(path, produce)

    def _read_text_file(
        self, path: Path, label: str, limit: int = TEXT_LIMIT
    ) -> Tuple[str, bool, List[str]]:
        warnings: List[str] = []
        try:
            size = os.path.getsize(str(path))
        except OSError:
            return "", False, warnings
        data = self._read_bytes(path, limit, warnings, label)
        if data is None:
            return "", False, warnings
        return data.decode("utf-8", errors="replace"), size > limit, warnings

    # ------------------------------------------------------------- decisions
    def _load_decisions(self) -> Tuple[List[dict], List[str]]:
        path = self._state_dir("decisions.jsonl")
        raw, warnings = self._jsonl_file(path, "decisions.jsonl")
        decisions: List[dict] = []
        for entry in raw:
            decision = codex.sanitize(
                {field: entry.get(field) for field in _DECISION_FIELDS}
            )
            decisions.append(decision)
        return decisions, warnings

    # ------------------------------------------------------------------ links
    def _links_path(self) -> Path:
        return self._state_dir("monitor", "links.json")

    def _read_links(self) -> Tuple[List[dict], List[str]]:
        path = self._links_path()
        if path.is_symlink():
            raise ValueError("拒绝符号链接路径")
        payload, warnings = self._json_file(path, "links.json")
        links: List[dict] = []
        if isinstance(payload, dict) and isinstance(payload.get("links"), list):
            for item in payload["links"]:
                if not isinstance(item, dict):
                    continue
                thread_id = item.get("thread_id")
                workspace = item.get("workspace")
                if not isinstance(thread_id, str) or not codex.UUID_RE.fullmatch(thread_id):
                    continue
                if not isinstance(workspace, str) or not os.path.isabs(workspace):
                    continue
                links.append(
                    {
                        "thread_id": thread_id.lower(),
                        "workspace": os.path.realpath(workspace),
                        "linked_at": item.get("linked_at"),
                    }
                )
        return links, warnings

    def link_codex(self, thread_id: str, workspace: str) -> dict:
        """Record a Codex session link (local CLI only, never the HTTP API)."""
        if not isinstance(thread_id, str) or not codex.UUID_RE.fullmatch(thread_id):
            raise ValueError("thread_id 必须是 UUID")
        if not isinstance(workspace, str) or not os.path.isabs(workspace):
            raise ValueError("workspace 必须是绝对路径")
        resolved = os.path.realpath(workspace)
        links, _warnings = self._read_links()
        record = {
            "thread_id": thread_id.lower(),
            "workspace": resolved,
            "linked_at": _now_iso(),
        }
        links = [item for item in links if item.get("thread_id") != record["thread_id"]]
        links.append(record)

        monitor = self._state_dir("monitor")
        monitor.mkdir(parents=True, exist_ok=True)
        path = monitor / "links.json"
        if path.is_symlink():
            raise ValueError("拒绝符号链接路径")
        fd, temporary = tempfile.mkstemp(prefix=".links-", dir=str(monitor))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"links": links}, handle, ensure_ascii=False, indent=2)
            os.chmod(temporary, 0o600)
            os.replace(temporary, str(path))
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return record

    def _linked_thread_ids(self) -> List[str]:
        links, _warnings = self._read_links()
        ids = {item["thread_id"] for item in links}
        decisions, _dw = self._load_decisions()
        for decision in decisions:
            thread = decision.get("codex_thread_id")
            if isinstance(thread, str) and codex.UUID_RE.fullmatch(thread):
                ids.add(thread.lower())
        for run_id in self._list_run_ids():
            context, _cw = self._json_file(
                self._run_dir(run_id) / "context.json", "context.json"
            )
            if isinstance(context, dict):
                thread = context.get("codex_thread_id")
                if isinstance(thread, str) and codex.UUID_RE.fullmatch(thread):
                    ids.add(thread.lower())
        return sorted(ids)

    # ------------------------------------------------------------------ codex
    def _parse_codex(self, thread_id: str) -> Tuple[dict, List[str]]:
        path = codex.find_session_file(self.codex_home, thread_id)
        if path is None:
            return (
                {
                    "thread_id": thread_id,
                    "model": None,
                    "status": "unavailable",
                    "started_at": None,
                    "updated_at": None,
                    "usage": None,
                    "usage_scope": "none",
                    "available": False,
                    "items": [],
                },
                ["Codex 会话 %s 未在 CODEX_HOME 中找到" % thread_id],
            )

        def produce():
            return codex.parse_session(path, thread_id)

        return self._cached(path, produce)

    def _codex_summary(self, thread_id: str, warnings: List[str]) -> dict:
        data, extra = self._parse_codex(thread_id)
        warnings.extend(extra)
        return {
            "thread_id": data.get("thread_id") or thread_id,
            "model": data.get("model"),
            "status": data.get("status"),
            "started_at": data.get("started_at"),
            "updated_at": data.get("updated_at"),
            "usage": _usage_obj(data.get("usage")),
            "usage_scope": data.get("usage_scope") or "none",
            "available": bool(data.get("available")),
        }

    def _codex_summaries(self, warnings: List[str]) -> List[dict]:
        summaries = []
        for thread_id in self._linked_thread_ids():
            summaries.append(self._codex_summary(thread_id, warnings))
        return summaries

    def codex_detail(self, thread_id: str, after: int = 0, limit: int = EVENT_LIMIT_DEFAULT) -> dict:
        if not isinstance(thread_id, str) or not codex.UUID_RE.fullmatch(thread_id):
            raise ValueError("thread_id 必须是 UUID")
        normalised = thread_id.lower()
        if normalised not in self._linked_thread_ids():
            raise ValueError("未关联的 Codex 会话")
        warnings: List[str] = []
        data, extra = self._parse_codex(normalised)
        warnings.extend(extra)
        items = data.get("items") if isinstance(data.get("items"), list) else []
        page, next_cursor, has_more, total = self._paginate(items, after, limit)
        return {
            "session": self._codex_summary(normalised, warnings),
            "items": [
                self._trim_event(_as_event(dict(item)), warnings) for item in page
            ],
            "next_cursor": next_cursor,
            "has_more": has_more,
            "total": total,
            "warnings": _clean_warnings(warnings),
        }

    # --------------------------------------------------------------- telemetry
    def _telemetry_events(self, run_dir: Path, warnings: List[str]) -> List[dict]:
        raw, extra = self._jsonl_file(run_dir / "telemetry.jsonl", "telemetry.jsonl")
        warnings.extend(extra)
        events: List[dict] = []
        for obj in raw:
            event_type = obj.get("type")
            role = obj.get("role")
            if codex.is_hidden_role(event_type) or codex.is_hidden_role(role):
                continue
            event = {
                "index": len(events),
                "timestamp": obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None,
                "type": event_type if isinstance(event_type, str) else "unknown",
                "agent_id": obj.get("agent_id"),
                "parent_id": obj.get("parent_id"),
                "session_id": obj.get("session_id"),
                "step_id": obj.get("step_id"),
                "role": role if isinstance(role, str) else None,
                "tool": obj.get("tool"),
                "call_id": obj.get("call_id"),
                "text": obj.get("text"),
                "input": obj.get("input"),
                "result": obj.get("result"),
                "status": obj.get("status"),
                "usage": codex.normalise_usage(obj.get("usage")),
                "model": obj.get("model"),
                "turn_id": obj.get("turn_id"),
            }
            events.append(codex.sanitize(event))
        return events

    _HEADLESS_HIDDEN_TYPES = frozenset(
        {"thinking", "reasoning", "analysis", "internal", "system", "developer"}
    )
    _HEADLESS_KEEP_TYPES = frozenset(
        {"session", "final", "message", "text", "status", "tool_call", "tool_result", "error"}
    )
    _HEADLESS_SUBAGENT_TYPES = frozenset({"subagent_started", "subagent_finished"})

    def _empty_event(self, event_type: str, timestamp, session_id, agent_id) -> dict:
        return {
            "index": 0,
            "timestamp": timestamp,
            "type": event_type,
            "agent_id": agent_id,
            "parent_id": None,
            "session_id": session_id,
            "step_id": None,
            "role": None,
            "tool": None,
            "call_id": None,
            "text": None,
            "input": None,
            "result": None,
            "status": None,
            "usage": None,
            "turn_id": None,
        }

    @staticmethod
    def _call_id(obj: dict):
        return obj.get("callId") or obj.get("call_id") or obj.get("toolCallId")

    def _headless_events(
        self, run_dir: Path, warnings: List[str]
    ) -> Tuple[List[dict], bool]:
        """Return ``(events, subagent_evidence)``.

        Legacy ``events.jsonl`` logs do not carry a reliable per-agent tree, so
        the flag only means "some subagent activity is visible"; it must not be
        turned into a concrete count.
        """
        raw, extra = self._jsonl_file(run_dir / "events.jsonl", "events.jsonl")
        warnings.extend(extra)
        events: List[dict] = []
        current_session = None
        current_agent = None
        root_agent = None
        has_subagent = False
        for obj in raw:
            event_type = obj.get("type") or obj.get("event")
            if not isinstance(event_type, str):
                continue
            event_type = event_type.strip().lower()
            if event_type in self._HEADLESS_HIDDEN_TYPES or codex.is_hidden_role(event_type):
                continue
            timestamp = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None
            role = obj.get("role")
            if codex.is_hidden_role(role):
                continue

            raw_agent = obj.get("agentId") or obj.get("agent_id") or obj.get("agent")
            if isinstance(raw_agent, str) and raw_agent:
                if root_agent is None:
                    root_agent = raw_agent
                elif raw_agent != root_agent:
                    has_subagent = True
            if isinstance(obj.get("parentId") or obj.get("parent_id"), str):
                has_subagent = True

            if event_type == "session":
                session_id = obj.get("sessionId") or obj.get("session_id")
                agent_id = obj.get("agentId") or obj.get("agent_id") or obj.get("agent")
                if isinstance(session_id, str):
                    current_session = session_id
                if isinstance(agent_id, str):
                    if root_agent is None:
                        root_agent = agent_id
                    elif agent_id != root_agent:
                        has_subagent = True
                    current_agent = agent_id
                event = self._empty_event("session", timestamp, current_session, current_agent)
                parent = obj.get("parentId") or obj.get("parent_id")
                if isinstance(parent, str) and parent:
                    event["parent_id"] = parent
                    has_subagent = True
                event["index"] = len(events)
                events.append(codex.sanitize(event))
                continue

            if event_type in self._HEADLESS_SUBAGENT_TYPES:
                has_subagent = True
                continue

            if event_type not in self._HEADLESS_KEEP_TYPES:
                # Unknown/verbose records are skipped entirely instead of
                # emitting empty placeholder events.
                continue

            event = self._empty_event(event_type, timestamp, current_session, current_agent)
            event["index"] = len(events)

            if event_type == "final":
                event["type"] = "message"
                event["role"] = "assistant"
                event["text"] = obj.get("text")
            elif event_type == "text":
                event["type"] = "message"
                event["role"] = role if isinstance(role, str) else "assistant"
                event["text"] = obj.get("text")
            elif event_type == "message":
                if not isinstance(role, str) or role.strip().lower() not in ("user", "assistant"):
                    continue
                event["role"] = role
                event["text"] = obj.get("text")
            elif event_type == "status":
                phase = obj.get("phase")
                reason = obj.get("reason")
                if isinstance(reason, dict):
                    reason = reason.get("kind")
                event["status"] = (
                    reason if isinstance(reason, str) else (phase if isinstance(phase, str) else None)
                )
                for key, target in (
                    ("step_id", "step_id"),
                    ("stepId", "step_id"),
                    ("turn_id", "turn_id"),
                    ("turnId", "turn_id"),
                ):
                    if event.get(target) is None and obj.get(key) is not None:
                        event[target] = obj.get(key)
                event["usage"] = codex.normalise_usage(obj.get("usage"))
                if event["usage"] is None and event["status"] is None and event["turn_id"] is None:
                    continue
            elif event_type == "tool_call":
                # Legacy logs may name the subagent tool as either ``tool`` or
                # ``name``; either way only "subagent activity exists" is known,
                # never a concrete count.
                tool_name = obj.get("tool") if obj.get("tool") is not None else obj.get("name")
                if isinstance(tool_name, str) and tool_name.strip().lower() == "subagent":
                    has_subagent = True
                event["tool"] = tool_name
                event["call_id"] = self._call_id(obj)
                event["input"] = obj.get("input")
            elif event_type == "tool_result":
                event["call_id"] = self._call_id(obj)
                event["result"] = obj.get("result")
                event["status"] = obj.get("status")
            elif event_type == "error":
                event["text"] = obj.get("message")
            events.append(codex.sanitize(event))
        return events, has_subagent

    @staticmethod
    def _headless_usage(events: List[dict]):
        """Aggregate ``status.step_end`` usage, deduplicated by step/index."""
        total: Optional[Dict[str, Optional[int]]] = None
        per_agent: Dict[object, dict] = {}
        seen = set()
        for event in events:
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            key = event.get("step_id")
            if key is None:
                key = event.get("index")
            key = (event.get("agent_id"), key)
            if key in seen:
                continue
            seen.add(key)
            total = codex.add_usage(total, usage)
            agent_id = event.get("agent_id")
            per_agent[agent_id] = codex.add_usage(per_agent.get(agent_id), usage)
        return total, per_agent

    def _trim_event(self, event: dict, warnings: List[str]) -> dict:
        truncated = bool(event.get("truncated"))
        for field in ("text", "input", "result"):
            value = event.get(field)
            if isinstance(value, str) and len(value) > TEXT_LIMIT:
                event[field] = value[:TEXT_LIMIT]
                warnings.append("事件文本超过 64 KiB，已截断")
                truncated = True
        if truncated:
            event["truncated"] = True
        return event

    @staticmethod
    def _paginate(items: List[dict], after: int, limit: int):
        if not _is_int(after) or after < 0:
            after = 0
        if not _is_int(limit) or limit < 1:
            limit = EVENT_LIMIT_DEFAULT
        if limit > EVENT_LIMIT_MAX:
            limit = EVENT_LIMIT_MAX
        total = len(items)
        page = items[after : after + limit]
        consumed = after + len(page)
        next_cursor = consumed if consumed < total else None
        return page, next_cursor, next_cursor is not None, total

    # ------------------------------------------------------------- run record
    def _agents(
        self,
        events: List[dict],
        per_agent: Dict[object, dict],
        run_status: Optional[str] = None,
        warnings: Optional[List[str]] = None,
    ) -> List[dict]:
        agents: Dict[object, dict] = {}
        order: List[object] = []
        finalized = set()
        active = set()
        for event in events:
            agent_id = event.get("agent_id")
            if agent_id is None:
                continue
            agent = agents.get(agent_id)
            if agent is None:
                agent = {
                    "agent_id": agent_id,
                    "parent_id": event.get("parent_id"),
                    "session_id": event.get("session_id"),
                    "status": "unknown",
                    "model": None,
                    "usage": None,
                    "tool_count": 0,
                }
                agents[agent_id] = agent
                order.append(agent_id)
            for field in ("parent_id", "session_id"):
                if agent.get(field) is None and event.get(field) is not None:
                    agent[field] = event.get(field)
            if agent["model"] is None and isinstance(event.get("model"), str):
                agent["model"] = event["model"]
            event_type = event.get("type")
            if event_type == "tool_call":
                agent["tool_count"] += 1
            if event_type in ("agent_status", "agent/status") and isinstance(event.get("status"), str):
                agent["status"] = event["status"]
                if event["status"].strip().lower() in _AGENT_ENDED_STATUSES:
                    finalized.add(agent_id)
            elif event_type in ("agent_finished", "subagent_finished"):
                agent["status"] = (
                    event.get("status") if isinstance(event.get("status"), str) else "finished"
                )
                finalized.add(agent_id)
            elif event_type in ("agent_created", "subagent_started") and agent["status"] == "unknown":
                agent["status"] = (
                    event.get("status") if isinstance(event.get("status"), str) else "running"
                )
            if event_type in ("message", "tool_call", "tool_result", "usage"):
                active.add(agent_id)
                # Public activity is enough to move an agent out of startup, but
                # never enough to declare it successful or finished.
                if agent["status"] in _AGENT_OPEN_STATUSES:
                    agent["status"] = "running"

        terminal = isinstance(run_status, str) and run_status.strip().lower() in _TERMINAL_RUN_STATUSES
        ended_root = False
        for agent_id in order:
            agent = agents[agent_id]
            if agent_id in finalized:
                continue
            if (
                terminal
                and agent.get("parent_id") is None
                and agent["status"] in _AGENT_TERMINAL_MARK_STATUSES
            ):
                # No disposed/end hook fired, but the task itself reached a
                # terminal state: report the root as ended, not still starting.
                agent["status"] = "ended"
                ended_root = True
            elif agent["status"] in _AGENT_OPEN_STATUSES and agent_id in active:
                agent["status"] = "running"
        if ended_root and warnings is not None:
            warnings.append(
                "根代理缺少显式结束事件，按任务结束标记为 ended（表示任务已结束，不代表审核通过）"
            )
        for agent_id in order:
            agents[agent_id]["usage"] = _usage_obj(per_agent.get(agent_id))
        return [agents[agent_id] for agent_id in order]

    def _review_status(
        self,
        run_id: str,
        status: Optional[str],
        applied: bool,
        decisions: List[dict],
    ) -> str:
        for decision in reversed(decisions):
            if decision.get("run_id") != run_id:
                continue
            action = decision.get("action")
            action = action.lower() if isinstance(action, str) else ""
            if action in _ACCEPT_ACTIONS:
                return "accepted"
            if action in _REVISION_ACTIONS:
                return "revision_requested"
            if action in _REJECT_ACTIONS:
                return "rejected"
        if applied:
            return "accepted"
        if status in ("completed", "needs_attention", "scope_violation"):
            return "pending"
        return "unknown"

    def _run_record(
        self, run_id: str, decisions: List[dict], warnings: List[str]
    ) -> dict:
        run_dir = self._run_dir(run_id)
        task, extra = self._json_file(run_dir / "task.json", "task.json")
        warnings.extend(extra)
        status_json, extra = self._json_file(run_dir / "status.json", "status.json")
        warnings.extend(extra)
        result, extra = self._json_file(run_dir / "result.json", "result.json")
        warnings.extend(extra)
        context, extra = self._json_file(run_dir / "context.json", "context.json")
        warnings.extend(extra)
        changes, extra = self._json_file(run_dir / "changes.json", "changes.json")
        warnings.extend(extra)

        task = task if isinstance(task, dict) else {}
        status_json = status_json if isinstance(status_json, dict) else {}
        result = result if isinstance(result, dict) else {}
        context = context if isinstance(context, dict) else {}
        changes = changes if isinstance(changes, list) else []

        for filename, value in (
            ("task.json", task),
            ("status.json", status_json),
            ("result.json", result),
        ):
            if not value and not (run_dir / filename).exists():
                warnings.append(filename + " 缺失，相关字段暂不可用")

        applied_path = run_dir / "applied.json"
        applied = applied_path.exists() and not applied_path.is_symlink()

        status = status_json.get("status") or result.get("status")
        if not isinstance(status, str):
            status = "unknown"

        tool_counts = result.get("tool_counts")
        result_tool_count = 0
        result_subagent_count = 0
        if isinstance(tool_counts, dict):
            result_tool_count = sum(
                value for value in tool_counts.values() if _is_int(value)
            )
            candidate = tool_counts.get("subagent")
            if _is_int(candidate):
                result_subagent_count = candidate

        telemetry_events = self._telemetry_events(run_dir, warnings)
        telemetry_statuses: List[str] = []
        historical_import = False
        for event in telemetry_events:
            if event.get("type") != "telemetry_status":
                continue
            state = str(event.get("status") or "").strip().lower()
            telemetry_statuses.append(state)
            reason = event.get("text")
            reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
            if state == "historical_import":
                historical_import = True
            if state in ("truncated", "partial", "incomplete") and reason:
                warnings.append("telemetry 历史导入不完整（%s）：%s" % (state, reason))
        truncated_telemetry = any(
            state in ("truncated", "partial", "incomplete") for state in telemetry_statuses
        )
        if truncated_telemetry:
            warnings.append(
                "telemetry.jsonl 标记为 truncated/partial，公开事件与用量覆盖不完整（partial）"
            )
        if historical_import:
            warnings.append(
                "telemetry 为 historical_import 历史归档恢复，不代表当前实时完整采集"
            )

        headless_events: List[dict] = []
        headless_usage = None
        headless_subagent = False
        if telemetry_events:
            events = telemetry_events
            usage, per_agent, agents_with_usage = _aggregate_usage(events)
            agents = self._agents(events, per_agent, status, warnings)
            subagent_count: Optional[int] = sum(
                1 for agent in agents if agent.get("parent_id") is not None
            )
        else:
            events = []
            usage = None
            per_agent = {}
            agents_with_usage = set()
            agents = []
            headless_events, headless_subagent = self._headless_events(run_dir, warnings)
            headless_usage, headless_per_agent = self._headless_usage(headless_events)
            if headless_events:
                agents = self._agents(headless_events, headless_per_agent, status, warnings)
            # Legacy logs cannot prove how many subagents ran: subagent evidence
            # in events.jsonl or result.tool_counts.subagent only proves that
            # activity existed, so the count stays null instead of pretending
            # to be zero.
            if headless_subagent or result_subagent_count > 0:
                subagent_count = None
                warnings.append(
                    "旧日志缺少子代理代理树，子代理数量未知；仅能确认存在子代理活动，不按调用次数推断实际子代理数"
                )
            else:
                subagent_count = 0

        parent_usage = result.get("parent_usage_only")
        normalised_parent = codex.normalise_usage(parent_usage)
        if usage is not None:
            subagent_ids = {
                agent["agent_id"] for agent in agents if agent.get("parent_id") is not None
            }
            missing = [agent_id for agent_id in subagent_ids if agent_id not in agents_with_usage]
            if missing or truncated_telemetry:
                coverage = "telemetry_partial"
            elif historical_import:
                # Historical archive recovery is honest but not live full
                # telemetry, so it must not masquerade as complete coverage.
                coverage = "historical_import"
            else:
                coverage = "telemetry"
        elif normalised_parent is not None:
            # Legacy result usage wins; never add headless step usage on top.
            usage = normalised_parent
            coverage = "parent_usage_only"
        elif headless_usage is not None:
            usage = headless_usage
            coverage = "headless_step_usage"
        else:
            coverage = "none"

        if telemetry_events:
            tool_count = sum(
                1 for event in telemetry_events if event.get("type") == "tool_call"
            )
        else:
            headless_tool_count = sum(
                1 for event in headless_events if event.get("type") == "tool_call"
            )
            tool_count = max(headless_tool_count, result_tool_count)

        changed_file_count = result.get("changed_file_count")
        if not _is_int(changed_file_count):
            changed_file_count = len(changes)

        started_at = status_json.get("started_at")
        finished_at = status_json.get("finished_at")
        elapsed = _number(result.get("elapsed_seconds"))
        if elapsed is None:
            elapsed = _elapsed_seconds(started_at, finished_at)

        previous_run_id = result.get("previous_run_id") or context.get("previous_run_id")
        session_id = result.get("session_id") or context.get("session_id")
        model = context.get("model")
        if not isinstance(model, str) or not model:
            model = None
            for agent in agents:
                if (
                    agent.get("parent_id") is None
                    and isinstance(agent.get("model"), str)
                    and agent["model"]
                ):
                    model = agent["model"]
                    break
        codex_thread_id = context.get("codex_thread_id")
        if isinstance(codex_thread_id, str) and not codex.UUID_RE.fullmatch(codex_thread_id):
            warnings.append("context.json 的 codex_thread_id 不是合法 UUID，已忽略")
            codex_thread_id = None

        goal = task.get("goal") or status_json.get("goal") or ""
        if not isinstance(goal, str):
            goal = str(goal)
        goal_truncated = goal[:GOAL_LIMIT]

        workspace = task.get("workspace")
        if not isinstance(workspace, str):
            workspace = None

        run = codex.sanitize(
            {
                "run_id": run_id,
                "goal": goal_truncated,
                "workspace": workspace,
                "mode": task.get("mode"),
                "status": status,
                "review_status": self._review_status(run_id, status, applied, decisions),
                "started_at": started_at,
                "finished_at": finished_at,
                "elapsed_seconds": elapsed,
                "previous_run_id": previous_run_id,
                "session_id": session_id,
                "model": model,
                "changed_file_count": changed_file_count,
                "tool_count": tool_count,
                "subagent_count": subagent_count,
                "usage": _usage_obj(usage),
                "codex_thread_id": codex_thread_id,
            }
        )

        return {
            "run": run,
            "task": codex.sanitize(task),
            "result": codex.sanitize(result),
            "context": codex.sanitize(context),
            "changes": codex.sanitize(changes),
            "applied": applied,
            "telemetry_events": events,
            "headless_events": headless_events,
            "agents": agents,
            "usage": usage,
            "coverage": coverage,
            "run_dir": run_dir,
        }

    # ------------------------------------------------------------------ filter
    @staticmethod
    def _matches(
        run: dict, workspace: Optional[str], query: str, status: str
    ) -> bool:
        if workspace:
            try:
                wanted = os.path.realpath(workspace)
            except (OSError, TypeError):
                return False
            if run.get("workspace") != wanted:
                return False
        if query:
            needle = query.lower()
            haystack = " ".join(
                str(run.get(field) or "")
                for field in ("run_id", "goal", "workspace", "mode", "status")
            ).lower()
            if needle not in haystack:
                return False
        if status:
            wanted = status.strip().lower()
            if wanted == "review_pending":
                wanted = "pending"
            if wanted not in ("pending", "accepted", "revision_requested", "rejected", "unknown"):
                if wanted != str(run.get("status") or "").lower():
                    return False
            elif wanted != (run.get("review_status") or "unknown"):
                return False
        return True

    # --------------------------------------------------------------- overview
    def overview(
        self,
        workspace: Optional[str] = None,
        query: str = "",
        status: str = "",
        offset: int = 0,
        limit: int = EVENT_LIMIT_DEFAULT,
    ) -> dict:
        warnings: List[str] = []
        decisions, extra = self._load_decisions()
        warnings.extend(extra)

        records = []
        for run_id in self._list_run_ids():
            records.append(self._run_record(run_id, decisions, warnings))

        workspaces = sorted(
            {record["run"]["workspace"] for record in records if record["run"]["workspace"]}
        )
        filtered = [
            record
            for record in records
            if self._matches(record["run"], workspace, query or "", status or "")
        ]

        counts = {
            "total": len(filtered),
            "running": sum(
                1 for record in filtered if record["run"]["status"] in ("running", "preparing")
            ),
            "review_pending": sum(
                1 for record in filtered if record["run"]["review_status"] == "pending"
            ),
            "accepted": sum(
                1 for record in filtered if record["run"]["review_status"] == "accepted"
            ),
            "failed": sum(1 for record in filtered if record["run"]["status"] == "failed"),
        }

        if not _is_int(offset) or offset < 0:
            offset = 0
        if not _is_int(limit) or limit < 1:
            limit = EVENT_LIMIT_DEFAULT
        if limit > EVENT_LIMIT_MAX:
            limit = EVENT_LIMIT_MAX
        page = filtered[offset : offset + limit]

        deepseek_usage, deepseek_coverage = self._overview_usage(filtered)
        codex_summaries = self._codex_summaries(warnings)

        return {
            "updated_at": _now_iso(),
            "counts": counts,
            "workspaces": workspaces,
            "runs": [record["run"] for record in page],
            "total": len(filtered),
            "offset": offset,
            "has_more": offset + len(page) < len(filtered),
            "decisions": decisions[-OVERVIEW_DECISIONS:][::-1],
            "usage": {
                "deepseek": {**deepseek_usage, "coverage": deepseek_coverage},
                "codex": {"sessions": codex_summaries},
            },
            "codex_sessions": codex_summaries,
            "warnings": _clean_warnings(warnings),
        }

    def _overview_usage(
        self, records: List[dict]
    ) -> Tuple[Dict[str, Optional[int]], str]:
        total: Optional[Dict[str, Optional[int]]] = None
        present = 0
        partial = False
        for record in records:
            usage = record.get("usage")
            if usage is not None:
                total = codex.add_usage(total, usage)
                present += 1
            else:
                partial = True
            if record.get("coverage") in (
                "telemetry_partial",
                "historical_import",
                "parent_usage_only",
                "headless_step_usage",
            ):
                partial = True
        if present == 0:
            coverage = "none"
        elif partial:
            coverage = "partial"
        else:
            coverage = "telemetry"
        return _usage_obj(total), coverage

    # ------------------------------------------------------------- run detail
    def run_detail(self, run_id: str) -> dict:
        warnings: List[str] = []
        decisions, extra = self._load_decisions()
        warnings.extend(extra)
        record = self._run_record(run_id, decisions, warnings)
        run_dir = record["run_dir"]

        prompt, _truncated, extra = self._read_text_file(
            run_dir / "prompt.txt", "prompt.txt"
        )
        warnings.extend(extra)
        prompt = codex.redact_text(prompt)

        run_decisions = [
            decision for decision in decisions if decision.get("run_id") == run_id
        ]

        thread_ids: List[str] = []
        context_thread = record["context"].get("codex_thread_id")
        if isinstance(context_thread, str):
            thread_ids.append(context_thread.lower())
        for decision in run_decisions:
            thread = decision.get("codex_thread_id")
            if isinstance(thread, str) and codex.UUID_RE.fullmatch(thread):
                if thread.lower() not in thread_ids:
                    thread_ids.append(thread.lower())

        if not record["telemetry_events"]:
            warnings.append(
                "该任务没有 telemetry，仅展示 headless 事件与 parent_usage_only；子代理用量未完整记录"
            )

        return {
            "run": record["run"],
            "task": record["task"],
            "prompt": prompt,
            "result": record["result"],
            "decisions": run_decisions,
            "changes": record["changes"],
            "agents": record["agents"],
            "usage": {**_usage_obj(record["usage"]), "coverage": record["coverage"]},
            "codex_thread_ids": thread_ids,
            "warnings": _clean_warnings(warnings),
        }

    # ------------------------------------------------------------- run events
    def run_events(
        self,
        run_id: str,
        after: int = 0,
        limit: int = EVENT_LIMIT_DEFAULT,
        agent_id: Optional[str] = None,
    ) -> dict:
        warnings: List[str] = []
        decisions, extra = self._load_decisions()
        warnings.extend(extra)
        record = self._run_record(run_id, decisions, warnings)

        events = record["telemetry_events"]
        if events:
            # Internal aggregation fields (step_id/parent_id) stay private; the
            # public page carries exactly the contract's Event fields.
            events = [dict(event) for event in events]
        else:
            events = [dict(event) for event in record.get("headless_events") or []]
            warnings.append(
                "该任务没有 telemetry，使用 headless 历史事件；子代理用量未完整记录"
            )

        if agent_id is not None:
            events = [event for event in events if event.get("agent_id") == agent_id]

        page, next_cursor, has_more, total = self._paginate(events, after, limit)
        page = [self._trim_event(_as_event(event), warnings) for event in page]
        return {
            "items": page,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "total": total,
            "warnings": _clean_warnings(warnings),
        }

    # --------------------------------------------------------------- artifact
    def artifact(self, run_id: str, name: str) -> dict:
        if name not in _ARTIFACT_FILES:
            raise ValueError("仅允许 prompt、patch、stderr")
        run_dir = self._run_dir(run_id)
        path = run_dir / _ARTIFACT_FILES[name]
        if path.is_symlink():
            raise ValueError("拒绝符号链接路径")
        text, truncated, _warnings = self._read_text_file(
            path, name, ARTIFACT_MAX_BYTES
        )
        return {"name": name, "text": codex.redact_text(text), "truncated": truncated}
