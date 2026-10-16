"""Submit bounded tasks to the *running desktop* DSH through its native API.

No Harness child process, model override, or global plugin patch is installed.
The desktop owns Agent composition, permissions, model choice and subagents.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shlex
import subprocess
import time
from urllib.parse import urlsplit
import uuid

from .codex_sessions import redact_text, sanitize


def server_identity(url):
    """Read only the owner of the loopback listener; never echo process argv."""
    from .desktop_client import normalize_url
    url = normalize_url(url)
    port = urlsplit(url).port or 80
    found = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-Fp"],
        capture_output=True, text=True, timeout=5,
    )
    pids = {int(line[1:]) for line in found.stdout.splitlines()
            if line.startswith("p") and line[1:].isdigit()}
    if len(pids) != 1:
        raise ValueError("没有找到唯一的桌面 DSH 服务；请打开 DSH.app 后重试")
    pid = pids.pop()
    result = {"server_pid": pid, "harness_version": None}
    command = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "args="],
                             capture_output=True, text=True, timeout=5)
    try:
        args = shlex.split(command.stdout)
    except ValueError:
        args = []
    # macOS ps does not quote argv containing spaces (Application Support, etc.).
    # Reconstruct only existing file paths; never return argv, flags or env.
    entries = set()
    for begin in range(1, min(len(args), 32)):
        if not args[begin].startswith("/"):
            continue
        for end in range(begin + 1, min(len(args) + 1, begin + 32)):
            candidate = Path(" ".join(args[begin:end]))
            if candidate.is_file():
                entries.add(candidate.resolve())
    for entry in entries:
        if entry.name not in {"bin.js", "dsh"}:
            continue
        for parent in entry.parents:
            package = parent / "package.json"
            if not package.is_file():
                continue
            try:
                info = json.loads(package.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if info.get("name") == "@deepseek-ai/dsh":
                result["harness_version"] = info.get("version")
                break
        if result["harness_version"] is not None:
            break
    if result["harness_version"] is None:
        raise ValueError("端口监听者不是可确认的桌面 DSH 后台，未发送认证或任务")
    return result


def _write_private(path, value):
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.write_text(data, encoding="utf-8")
    path.chmod(0o600)


def _save_metadata(directory, metadata):
    _write_private(directory / "desktop.json", metadata)
    for name in ("context.json", "status.json"):
        path = directory / name
        if not path.is_file():
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        value.update({key: metadata.get(key) for key in
                      ("execution_backend", "session_id", "server_pid", "harness_version")})
        selection = metadata.get("model_selection") or {}
        value.update({key: selection[key] for key in ("model", "provider") if key in selection})
        value["reasoning_effort"] = selection.get("reasoningEffort")
        _write_private(path, value)


def _text(blocks, limit=16000):
    if not isinstance(blocks, list):
        return ""
    value = "\n".join(b["text"] for b in blocks
                      if isinstance(b, dict) and b.get("type") == "text"
                      and isinstance(b.get("text"), str))
    value = redact_text(value)
    return value if len(value) <= limit else value[:limit] + "…[truncated]"


def _usage(value):
    if not isinstance(value, dict):
        return {}
    return {key: number for key, number in value.items()
            if key in {"inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "totalTokens"}
            and type(number) in (int, float) and math.isfinite(number)}


class PublicJournal:
    """Project only public messages, tool records and usage into local logs."""

    def __init__(self, directory, root_session, stage):
        self.directory = Path(directory)
        self.root_session = root_session
        self.stage = Path(stage).resolve()
        self.last_seq = {}
        self.final = {}
        self.reasons = {}
        self.tools = {}
        self.agents = set()
        self.scope_entered = False
        self.scope_error = False
        self.closed = False
        self.events = (self.directory / "events.jsonl").open("w", encoding="utf-8")
        self.telemetry = (self.directory / "telemetry.jsonl").open("w", encoding="utf-8")
        for name in ("events.jsonl", "telemetry.jsonl"):
            (self.directory / name).chmod(0o600)
        self.emit({"type": "session", "sessionId": root_session})

    def emit(self, value):
        self.events.write(json.dumps(sanitize(value), ensure_ascii=False) + "\n")
        self.events.flush()

    def observe(self, kind, session, parent=None, **fields):
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "type": kind,
                  "agent_id": session, "session_id": session, "parent_id": parent, **fields}
        self.telemetry.write(json.dumps(sanitize(record), ensure_ascii=False) + "\n")
        self.telemetry.flush()

    def register(self, session, parent=None, model=None):
        if session in self.agents:
            return
        self.agents.add(session)
        self.observe("agent_created", session, parent, model=model, status="running")
        if parent:
            self.observe("subagent_started", session, parent, status="started")

    def feed(self, session, records, parent=None):
        valid = [r for r in records if isinstance(r, dict)
                 and isinstance(r.get("event"), dict)
                 and type(r["event"].get("seq")) is int]
        for record in sorted(valid, key=lambda r: r["event"]["seq"]):
            event = record.get("event", {})
            seq = event.get("seq")
            if type(seq) is not int or seq <= self.last_seq.get(session, -1):
                continue
            self.last_seq[session] = seq
            kind, data = event.get("type"), event.get("data", {})
            if not isinstance(data, dict):
                continue
            step = "%s.%s" % (data.get("turn"), data.get("step"))
            if kind == "working-directory/change" and session == self.root_session:
                path = data.get("cwd")
                if isinstance(path, str):
                    self.scope_entered = Path(path).resolve() == self.stage
            elif kind in {"user/message", "assistant/message"}:
                message = data.get("message", {})
                if not isinstance(message, dict):
                    continue
                role = message.get("role", "assistant" if kind == "assistant/message" else "user")
                channel = message.get("channel", data.get("channel", ""))
                if not isinstance(role, str) or role.strip().lower() not in {"user", "assistant"}:
                    continue
                if isinstance(channel, str) and channel.strip().lower() in {"analysis", "reasoning", "thinking", "internal"}:
                    continue
                content = message.get("content", data.get("content", []))
                text = _text(content)
                if text:
                    self.observe("message", session, parent,
                                 role="assistant" if kind == "assistant/message" else "user", text=text)
                    if kind == "assistant/message":
                        self.final[session] = _text(content, 12000)
                usage = _usage(data.get("usage"))
                if kind == "assistant/message" and usage:
                    self.observe("usage", session, parent, step_id=step, usage=usage)
                    if session == self.root_session:
                        self.emit({"type": "status", "phase": "step_end", "usage": usage})
            elif kind == "tool/call":
                name = data.get("name")
                if not isinstance(name, str):
                    continue
                self.tools[name] = self.tools.get(name, 0) + 1
                if session == self.root_session and not self.scope_entered and name != "working_directory":
                    self.scope_error = True
                self.observe("tool_call", session, parent, step_id=step, tool=name,
                             call_id=data.get("callId"), input=sanitize(data.get("arguments")))
                self.emit({"type": "tool_call", "tool": name})
            elif kind == "tool/result":
                message = data.get("message", {})
                blocks = message.get("content", [])
                wrapped = next((b for b in blocks if isinstance(b, dict) and b.get("type") == "tool-result"), None) if isinstance(blocks, list) else None
                result = wrapped if wrapped is not None else message
                status = "error" if result.get("isError") is True else "ok"
                call_id = result.get("toolCallId") or message.get("source", {}).get("callId")
                self.observe("tool_result", session, parent, step_id=step, call_id=call_id,
                             result=_text(result.get("content", [])), status=status)
                self.emit({"type": "tool_result", "status": status})
            elif kind == "turn/end":
                reason = data.get("reason", {})
                self.reasons[session] = reason
                self.observe("agent_status", session, parent, status="idle")
                if session == self.root_session:
                    self.emit({"type": "status", "phase": "turn_end", "reason": reason})

    def finish(self):
        if self.closed:
            return
        if self.final.get(self.root_session):
            self.emit({"type": "final", "text": self.final[self.root_session]})
        for session in self.agents:
            self.observe("agent_finished", session, None if session == self.root_session else self.root_session,
                         status="completed" if self.reasons.get(session, {}).get("kind") == "completed" else "unknown")
        self.events.close()
        self.telemetry.close()
        self.closed = True


def read_records(client, session, parent, after=-1):
    projection = client.rpc("session/projections", {"request": {"sessionId": session}})
    if not isinstance(projection, dict) or projection.get("kind") != "sequenced":
        raise ValueError("DSH 会话没有可验证的事件游标，未继续任务")
    cursor = projection.get("asOfSeq")
    if type(cursor) is not int:
        raise ValueError("DSH 返回了无效会话游标")
    address = ({"kind": "session", "sessionId": session} if parent is None else
               {"kind": "subagent", "parentSessionId": parent, "childSessionId": session, "mode": "unknown"})
    records, before = [], None
    while cursor > after:
        request = {"address": address, "throughSeq": cursor, "maxMessages": 500}
        if before is not None:
            request["beforeSeq"] = before
        page = client.rpc("session/page", {"request": request})
        if not isinstance(page, dict):
            raise ValueError("DSH 返回了无效事件分页")
        batch = page.get("records", [])
        if not isinstance(batch, list):
            raise ValueError("DSH 返回了无效事件分页")
        records.extend(r for r in batch if isinstance(r, dict))
        seqs = [r["event"].get("seq") for r in batch
                if isinstance(r, dict) and isinstance(r.get("event"), dict)]
        seqs = [s for s in seqs if type(s) is int]
        if not page.get("hasMore") or not seqs or min(seqs) <= after:
            break
        following = min(seqs)
        if before is not None and following >= before:
            raise ValueError("DSH 事件分页没有前进")
        before = following
    return projection, records


def build_prompt(task, stage):
    return (
        "你正在桌面 DSH 中执行 Codex 委派任务。继承此 DSH 的模型、思考强度、Agent 预设、插件及权限设置，"
        "由你自主判断调用已启用的插件、工作流和子 agent；不需要为了使用子 agent 询问用户。"
        "按需使用最少必要工具；简单任务直接完成，只有确实能降低工作量时才创建子 agent，避免重复审查和无界等待。"
        "Codex 负责需求、关键决策、审核与验收。\n"
        "重要：会话属于原项目，但任务输入已复制到暂存目录。第一步必须先调用 working_directory 工具，"
        "将 cd 设置为 " + json.dumps(str(stage), ensure_ascii=False) + "。成功前禁止调用其他工具。"
        "之后只在该暂存目录读写，子 agent 也必须使用此目录。不要访问原项目、上级目录或私人资料。"
        "只允许修改 write_paths；inspect/research/process 模式不写文件。遵循任务 constraints 和检查要求。"
        "不得自行扩大范围、安装软件、提交推送、部署或启动长期服务。你不是唯一执行者，不撤销他人修改。"
        "普通可逆选择自主处理，真正缺少信息时说明阻塞。必须等待所有子 agent 完成后交付。"
        "subagents 为 null 表示完全沿用 DSH 的自动子代理策略；只有明确给出整数时才限制启动数量。"
        "隐藏推理不回传；最终中文报告做了什么、文件或来源、实际检查和剩余问题。"
        "process 模式只返回所需成品；不得虚构完成或测试通过。\n任务：\n"
        + json.dumps({k: task[k] for k in ("goal", "mode", "read_paths", "write_paths", "constraints", "checks", "subagents")}, ensure_ascii=False, indent=2)
    )


def execute(task, config, directory):
    from .desktop_client import DesktopClient

    start = time.monotonic()
    directory = Path(directory)
    url = config.get("desktop_url", "http://127.0.0.1:3080")
    try:
        identity = {**server_identity(url), "desktop_auto_started": False}
    except ValueError:
        from .desktop_start import ensure_running
        identity = ensure_running(url, config.get("desktop_log_path"),
                                  auto_start=config.get("desktop_auto_start", True))
    session = "session-" + str(uuid.uuid4())
    journal = PublicJournal(directory, session, directory / "workspace")
    metadata = {"execution_backend": "desktop", "server_url": url,
                "session_id": session, **identity}
    _save_metadata(directory, metadata)
    interruption, completed = None, False
    try:
        with DesktopClient(url=url, log_path=config.get("desktop_log_path")) as client:
            catalog = client.rpc("session/modelCatalog")
            metadata["model_selection"] = catalog.get("default")
            workspace = client.rpc("workspace/create", {"request": {"path": task["workspace"]}})["workspace"]
            created = client.rpc("session/create", {"request": {"workspaceId": workspace["workspaceId"], "sessionId": session}})
            if created.get("sessionId") != session:
                raise ValueError("DSH 未创建预期的新任务会话，未发送任务")
            metadata.update(workspace_id=workspace["workspaceId"], agent_preset=created.get("agentPreset"))
            _save_metadata(directory, metadata)
            journal.register(session, model=(metadata["model_selection"] or {}).get("model"))
            title = "Codex · " + task["goal"].splitlines()[0].strip()[:65]
            client.rpc("session/rename", {"request": {"sessionId": session, "title": title}})
            prompt = build_prompt(task, directory / "workspace")
            (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
            (directory / "prompt.txt").chmod(0o600)
            accepted = client.rpc("session/prompt", {"request": {
                "requestId": str(uuid.uuid4()), "sessionId": session, "mode": "queue",
                "content": [{"type": "text", "text": prompt}],
            }})
            if accepted.get("accepted") is not True:
                raise ValueError("DSH 未确认接受任务，未重发")
            while True:
                listed = client.rpc("session/list", {"_request": {}}).get("items", [])
                tree = {session: None}
                for _ in range(len(listed)):
                    added = False
                    for row in listed:
                        sid, parent = row.get("sessionId"), row.get("parentSessionId")
                        if parent in tree and sid not in tree:
                            tree[sid] = parent
                            added = True
                    if not added:
                        break
                for sid, parent in tree.items():
                    projection, records = read_records(client, sid, parent, journal.last_seq.get(sid, -1))
                    selection = projection.get("values", {}).get("modelSelection", {}).get("lastUsed")
                    journal.register(sid, parent, (selection or {}).get("model"))
                    journal.feed(sid, records, parent)
                    if sid == session and selection:
                        metadata["model_selection"] = selection
                _save_metadata(directory, metadata)
                running = {r.get("sessionId") for r in listed if r.get("running") is True}
                if journal.scope_error:
                    interruption = "scope_violation"
                elif task.get("subagents") is not None and len(tree) - 1 > task["subagents"]:
                    interruption = "scope_violation"
                elif time.monotonic() - start >= task["timeout_seconds"]:
                    interruption = "timed_out"
                elif sum(p.stat().st_size for p in (directory / "events.jsonl", directory / "telemetry.jsonl")) > config["max_log_bytes"]:
                    interruption = "log_limit_exceeded"
                if interruption:
                    client.rpc("session/cancel", {"request": {"sessionId": session}})
                    break
                if all(sid in journal.reasons for sid in tree) and not running.intersection(tree):
                    completed = True
                    break
                time.sleep(0.8)
            if server_identity(url)["server_pid"] != identity["server_pid"]:
                raise ValueError("DSH 服务进程在任务中改变，未确认交付")
    except BaseException:
        # A lost HTTP response must not leave this task running unnoticed.
        # Never restart the Host or cancel any other Session.
        try:
            if server_identity(url)["server_pid"] == identity["server_pid"]:
                with DesktopClient(url=url, log_path=config.get("desktop_log_path"), timeout=3) as cleanup:
                    cleanup.rpc("session/cancel", {"request": {"sessionId": session}})
        except (OSError, ValueError, subprocess.SubprocessError):
            metadata["cancellation_confirmed"] = False
        raise
    finally:
        journal.finish()
        _save_metadata(directory, metadata)
        _write_private(directory / "budget.json", {
            "accepted_total": sum(journal.tools.values()), "attempted_total": sum(journal.tools.values()),
            "accepted_by_tool": journal.tools, "denied": 0,
            "enforcement": "desktop-native-policy", "bridge_tool_limit": None,
            "subagents": "desktop-auto" if task.get("subagents") is None else task["subagents"],
        })
    (directory / "stderr.log").touch(mode=0o600)
    return {"exit_code": 0 if completed else 1, "interruption": interruption,
            "elapsed_seconds": round(time.monotonic() - start, 2), **metadata}
