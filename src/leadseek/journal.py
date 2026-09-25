"""Small decision/assignment journal.

Appends one JSON object per line to ``<root>/.state/decisions.jsonl`` and reads
the most recent records back without loading the whole file into memory.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union

__all__ = ["record", "list_recent", "effective_codex_thread_id"]

_CODEX_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_OWNERS = frozenset({"codex", "deepseek"})
_REASON_MAX = 2000
_STATE_DIR = ".state"
_LOG_NAME = "decisions.jsonl"
_LIMIT_MIN = 1
_LIMIT_MAX = 100


def _log_path(root: Union[str, Path]) -> Path:
    return Path(root) / _STATE_DIR / _LOG_NAME


def _canonical_workspace(workspace: Union[str, Path]) -> str:
    if not isinstance(workspace, (str, Path)):
        raise ValueError("workspace must be a path string")
    path = Path(workspace)
    if not path.is_absolute():
        raise ValueError("workspace must be an absolute path")
    return os.path.realpath(str(path))


def effective_codex_thread_id(environ=None) -> Optional[str]:
    """Return the environment's valid Codex thread UUID, or ``None``.

    Only the effective ``CODEX_THREAD_ID`` string is consulted; anything that
    is absent, blank, or not a canonical UUID is treated as "not recorded".
    """
    if environ is None:
        environ = os.environ
    value = environ.get("CODEX_THREAD_ID")
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _CODEX_UUID.fullmatch(candidate):
        return None
    return candidate.lower()


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError("%s must be a non-empty string" % field)
    return value


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - defensive
            raise OSError("short write while appending journal record")
        view = view[written:]


def record(
    root: Union[str, Path],
    workspace: Union[str, Path],
    owner: str,
    action: str,
    reason: str,
    run_id: Optional[str] = None,
) -> dict:
    """Append one decision record and return it.

    Raises ``ValueError`` for invalid input.
    """
    canonical_workspace = _canonical_workspace(workspace)
    if not isinstance(owner, str) or owner not in _OWNERS:
        raise ValueError("owner must be one of: codex, deepseek")
    action = _require_text(action, "action")
    reason = _require_text(reason, "reason")
    if len(reason) > _REASON_MAX:
        raise ValueError("reason must be at most %d characters" % _REASON_MAX)

    entry = {
        "timestamp": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "workspace": canonical_workspace,
        "owner": owner,
        "action": action,
        "reason": reason,
        "run_id": run_id,
        "codex_thread_id": effective_codex_thread_id(),
    }

    path = _log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")

    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            _write_all(fd, payload)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return entry


def list_recent(
    root: Union[str, Path],
    workspace: Optional[Union[str, Path]] = None,
    limit: int = 20,
) -> List[dict]:
    """Return up to ``limit`` records, newest first.

    Blank and corrupt lines are skipped. A missing log file yields ``[]``.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer between 1 and 100")
    if limit < _LIMIT_MIN or limit > _LIMIT_MAX:
        raise ValueError("limit must be an integer between 1 and 100")

    target = None
    if workspace is not None:
        target = _canonical_workspace(workspace)

    path = _log_path(root)
    if not path.exists():
        return []

    recent: "deque" = deque(maxlen=limit)
    with open(str(path), "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(entry, dict):
                continue
            if target is not None and entry.get("workspace") != target:
                continue
            recent.append(entry)

    return list(reversed(recent))
