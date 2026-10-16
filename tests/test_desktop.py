"""``leadseek.desktop`` 的单元测试：桌面同进程 DSH 委派后端。

覆盖范围：

* :class:`~leadseek.desktop.PublicJournal` 的 ``(session, seq)`` 去重、父子
  usage／工具结果采集、现代与嵌套 ``callId``、隐藏内容与敏感文本处理；
* 进入暂存目录之前的工具调用范围检测；
* :func:`~leadseek.desktop.read_records` 的跨页完整性、空页与不前进处理；
* :func:`~leadseek.desktop.execute` 的协议下发、原生模型／预设继承、
  ``session/cancel`` 只针对自己的会话，以及只写运行目录的约束。

安全约束（测试自身必须遵守）：只用标准库，不联网，不访问真 3080，不读真
``~/Library/Logs/DSH/server.log``，不启动任何子进程或 headless 运行时——
``DesktopClient``、``server_identity``、``subprocess`` 与 ``time`` 全部被替换为
记录型替身，由此任何真实 I/O 都会让测试失败而不是静默发生。

隐藏角色与非法事件序号的回归用例验证已修复的公开日志边界。
"""

from __future__ import annotations

import json
import re
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    # ``unittest discover -s tests`` 只把 tests/ 放进 sys.path。
    sys.path.insert(0, str(SRC))

from leadseek import desktop, desktop_client
from leadseek.desktop import PublicJournal, build_prompt, read_records

NATIVE_SELECTION = {"model": "deepseek-max", "provider": "deepseek-official", "reasoningEffort": "max"}
CATALOG_DEFAULT = {"model": "deepseek-flash", "provider": "deepseek-official", "reasoningEffort": "low"}
IDENTITY = {"server_pid": 4242, "harness_version": "9.9.9"}
TEST_URL = "http://127.0.0.1:3999"
TRUNCATION = "…[truncated]"
REDACTED = "[已脱敏]"

REASONING_MARKER = "REASONING-MARKER-9f2c"
STREAM_MARKER = "STREAM-MARKER-9f2c"
SYSTEM_MARKER = "SYSTEM-MARKER-9f2c"
DEVELOPER_MARKER = "DEVELOPER-MARKER-9f2c"
INSTRUCTION_MARKER = "INSTRUCTION-MARKER-9f2c"
SECRET = "hunter2-SECRET-9f2c"
API_KEY = "sk-desktoptest0000000000000000"

TOOL_CALL = "tool/call"
TOOL_RESULT = "tool/result"
ASSISTANT = "assistant/message"
USER = "user/message"
TURN_END = "turn/end"
CWD_CHANGE = "working-directory/change"

COMPLETED = {"reason": {"kind": "completed"}}


def event(seq, kind, data):
    return {"event": {"seq": seq, "type": kind, "data": data}}


def message(text, *, role="assistant", extra_blocks=(), turn=1, step=1, usage=None):
    data = {
        "turn": turn,
        "step": step,
        "message": {"role": role, "content": [*extra_blocks, {"type": "text", "text": text}]},
    }
    if usage is not None:
        data["usage"] = usage
    return data


class _Case(unittest.TestCase):
    """临时目录与 JSON/JSONL 读取助手；所有路径都取 realpath。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.directory = self.root / "run"
        self.directory.mkdir()
        self.stage = self.directory / "workspace"
        self.stage.mkdir()
        self.live = self.root / "live"
        self.live.mkdir()
        (self.live / "code.py").write_text("old\n", encoding="utf-8")

    def read_text(self, path):
        return Path(path).read_text(encoding="utf-8")

    def read_json(self, path):
        return json.loads(self.read_text(path))

    def read_lines(self, path):
        return [json.loads(line) for line in self.read_text(path).splitlines() if line.strip()]

    def journal(self, root_session="root", directory=None):
        journal = PublicJournal(directory or self.directory, root_session, self.stage)
        self.addCleanup(journal.finish)
        return journal

    def snapshot(self, directory):
        return {
            str(path.relative_to(directory)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in sorted(Path(directory).rglob("*")) if path.is_file()
        }


# --------------------------------------------------------------------------- 公共替身


class _FakeTime:
    """替换 ``leadseek.desktop`` 里的 ``time`` 模块：不睡眠、时钟可脚本化。

    ``limit`` 是防呆上限：如果被测循环不收敛，这里直接失败而不是挂住整个测试。
    """

    def __init__(self, step=0.0, limit=200):
        self.step = step
        self.limit = limit
        self.calls = 0
        self.sleep_calls = 0

    def sleep(self, _seconds):
        self.sleep_calls += 1
        if self.sleep_calls > self.limit:
            raise AssertionError("execute 轮询没有收敛（sleep 超过 %d 次）" % self.limit)

    def monotonic(self):
        self.calls += 1
        if self.calls > self.limit:
            raise AssertionError("execute 轮询没有收敛（时钟检查超过 %d 次）" % self.limit)
        return (self.calls - 1) * self.step


class _SubprocessSpy:
    """任何子进程调用都立即失败：桌面后端不得启动 lsof/ps/node/headless。"""

    SubprocessError = subprocess.SubprocessError

    def __init__(self):
        self.calls = []

    def run(self, *args, **kwargs):
        self.calls.append(("run", args, kwargs))
        raise AssertionError("桌面后端不得运行外部进程: %r" % (args,))

    def Popen(self, *args, **kwargs):
        self.calls.append(("Popen", args, kwargs))
        raise AssertionError("桌面后端不得启动 headless 进程: %r" % (args,))


class _WriteSpy:
    """记录 ``pathlib`` 上的写操作目标，用于证明只写运行目录。"""

    def __init__(self):
        self.paths = []
        self._patches = []

    def __enter__(self):
        real_open, real_write, real_touch, real_chmod = (
            Path.open, Path.write_text, Path.touch, Path.chmod)

        def spy_open(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if isinstance(mode, str) and any(char in mode for char in "wax+"):
                self.paths.append(Path(path))
            return real_open(path, *args, **kwargs)

        def spy_write_text(path, *args, **kwargs):
            self.paths.append(Path(path))
            return real_write(path, *args, **kwargs)

        def spy_touch(path, *args, **kwargs):
            self.paths.append(Path(path))
            return real_touch(path, *args, **kwargs)

        def spy_chmod(path, *args, **kwargs):
            self.paths.append(Path(path))
            return real_chmod(path, *args, **kwargs)

        for name, replacement in (("open", spy_open), ("write_text", spy_write_text),
                                  ("touch", spy_touch), ("chmod", spy_chmod)):
            patcher = mock.patch.object(Path, name, replacement)
            patcher.start()
            self._patches.append(patcher)
        return self

    def __exit__(self, *_exc):
        for patcher in reversed(self._patches):
            patcher.stop()
        self._patches = []
        return False


class _StubClient:
    """只实现 ``rpc`` 的分页替身，记录每个请求原文。"""

    def __init__(self, projection, page=None):
        self.projection = projection
        self.page = page
        self.calls = []

    def rpc(self, endpoint, args=None):
        self.calls.append((endpoint, args))
        if endpoint == "session/projections":
            return self.projection
        if endpoint == "session/page":
            return self.page(args) if callable(self.page) else self.page
        raise AssertionError("unexpected rpc: %s" % endpoint)

    def page_requests(self):
        return [args["request"] for name, args in self.calls if name == "session/page"]

    def page_calls(self):
        return len(self.page_requests())


class _PagingClient(_StubClient):
    """按真实协议翻页：``throughSeq`` 恒定，``beforeSeq`` 逐页向前滚动。"""

    def __init__(self, projection, records=(), page_size=2, empty_pages=(), stall=False,
                 inclusive=False):
        self.records = list(records)
        self.page_size = page_size
        self.empty_pages = set(empty_pages)
        self.stall = stall
        self.inclusive = inclusive
        self.served = 0
        super().__init__(projection, page=self._serve)

    def _serve(self, args):
        request = args["request"]
        self.served += 1
        limit = min(request["maxMessages"], self.page_size)
        if self.served in self.empty_pages:
            return {"records": [], "hasMore": True}
        if self.stall:
            return {"records": self.records[-limit:], "hasMore": True}
        boundary = request.get("beforeSeq")
        if boundary is None:
            eligible = [r for r in self.records if r["event"]["seq"] <= request["throughSeq"]]
        elif self.inclusive:
            eligible = [r for r in self.records if r["event"]["seq"] <= boundary]
        else:
            eligible = [r for r in self.records if r["event"]["seq"] <= boundary - 1]
        window = eligible[-limit:]
        return {"records": window, "hasMore": len(eligible) > len(window)}


class _FakeDesktopSession:
    def __init__(self, host, kwargs, tag):
        self._host = host
        self.kwargs = kwargs
        self.tag = tag
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True
        return False

    def rpc(self, endpoint, args=None):
        return self._host.dispatch(self, endpoint, args)


class FakeDesktop:
    """脚本化的 ``DesktopClient``：只记录/回放 RPC，绝不打开套接字。"""

    def __init__(self, polls, *, catalog=None, agent_preset="leadseek-preset-native",
                 accepted=True, workspace_id="ws-native", page_size=500):
        self.polls = list(polls)
        self.catalog = {"default": CATALOG_DEFAULT} if catalog is None else catalog
        self.agent_preset = agent_preset
        self.accepted = accepted
        self.workspace_id = workspace_id
        self.page_size = page_size
        self.calls = []
        self.clients = []
        self.sessions = []
        self.session_id = None
        self.poll_index = 0
        self.list_calls = 0

    # -- 由 execute 调用的构造入口 -------------------------------------------------
    def factory(self, **kwargs):
        client = _FakeDesktopSession(self, kwargs, "main" if not self.clients else "cleanup")
        self.clients.append(client)
        self.sessions.append(kwargs)
        return client

    # -- 便捷查询 -----------------------------------------------------------------
    def methods(self):
        return [endpoint for _tag, endpoint, _args in self.calls]

    def args_for(self, endpoint):
        return [args for _tag, name, args in self.calls if name == endpoint]

    def cancel_sessions(self):
        return [args["request"]["sessionId"] for _tag, name, args in self.calls
                if name == "session/cancel"]

    # -- 回放 ---------------------------------------------------------------------
    def _resolve(self, value):
        if isinstance(value, str):
            return value.replace("$root", self.session_id or "$root")
        if isinstance(value, dict):
            # 键也要解析：records/selection/asOfSeq 用 "$root" 作键。
            return {self._resolve(key): self._resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve(item) for item in value]
        return value

    def _poll(self):
        return self.polls[min(self.poll_index, len(self.polls) - 1)]

    def _records(self, poll, session):
        return self._resolve(poll.get("records", {})).get(session, [])

    def dispatch(self, client, endpoint, args):
        self.calls.append((client.tag, endpoint, args))
        if endpoint == "session/modelCatalog":
            return self.catalog
        if endpoint == "workspace/create":
            return {"workspace": {"workspaceId": self.workspace_id, "path": args["request"]["path"]}}
        if endpoint == "session/create":
            self.session_id = args["request"]["sessionId"]
            return {"sessionId": self.session_id, "agentPreset": self.agent_preset}
        if endpoint == "session/rename":
            return {"ok": True}
        if endpoint == "session/prompt":
            return {"accepted": self.accepted}
        if endpoint == "session/cancel":
            return {"cancelled": True}
        if endpoint == "session/list":
            poll = self._poll()
            self.poll_index += 1
            self.list_calls += 1
            return {"items": self._resolve(poll["items"])}
        if endpoint == "session/projections":
            session = args["request"]["sessionId"]
            poll = self._poll()
            records = self._records(poll, session)
            seqs = [record["event"]["seq"] for record in records]
            as_of = self._resolve(poll.get("asOfSeq", {})).get(session, max(seqs) if seqs else 0)
            values = {}
            selection = self._resolve(poll.get("selection", {})).get(session)
            if selection:
                values["modelSelection"] = {"lastUsed": selection}
            return {"kind": "sequenced", "asOfSeq": as_of, "values": values}
        if endpoint == "session/page":
            request = args["request"]
            records = self._resolve(self._poll().get("records", {})).get(
                request["address"].get("sessionId") or request["address"]["childSessionId"], [])
            records = sorted(records, key=lambda record: record["event"]["seq"])
            boundary = request.get("beforeSeq")
            if boundary is None:
                eligible = [r for r in records if r["event"]["seq"] <= request["throughSeq"]]
            else:
                eligible = [r for r in records if r["event"]["seq"] <= boundary - 1]
            limit = min(request["maxMessages"], self.page_size)
            window = eligible[-limit:]
            return {"records": window, "hasMore": len(eligible) > len(window)}
        raise AssertionError("fake 收到白名单外的 RPC: %s" % endpoint)


# --------------------------------------------------------------------------- Journal


class JournalTests(_Case):
    def test_duplicate_and_out_of_order_sequences_project_once(self):
        journal = self.journal()
        duplicate = event(2, TOOL_CALL, {"turn": 1, "step": 2, "name": "read_file", "callId": "call-1"})
        journal.feed("root", [
            event(3, ASSISTANT, message("第三步", turn=1, step=3)),
            event(1, CWD_CHANGE, {"cwd": str(self.stage)}),
            duplicate,
            dict(duplicate),
        ])
        telemetry = self.read_lines(self.directory / "telemetry.jsonl")
        # 乱序到达按 seq 升序投影，重复 seq 只投影一次。
        self.assertEqual([row["type"] for row in telemetry], ["tool_call", "message"])
        self.assertEqual(journal.last_seq, {"root": 3})

        journal.feed("root", [duplicate, event(4, ASSISTANT, message("第四步", turn=1, step=4))])
        telemetry = self.read_lines(self.directory / "telemetry.jsonl")
        self.assertEqual([row.get("text") for row in telemetry if row["type"] == "message"],
                         ["第三步", "第四步"])

        # last_seq 按 session 分开：子会话的同号 seq 仍然处理。
        journal.feed("child-1", [duplicate], parent="root")
        child = self.read_lines(self.directory / "telemetry.jsonl")[-1]
        self.assertEqual((child["type"], child["agent_id"], child["parent_id"]),
                         ("tool_call", "child-1", "root"))
        self.assertEqual(journal.last_seq, {"root": 4, "child-1": 2})

    def test_ignored_event_types_still_advance_the_cursor(self):
        journal = self.journal()
        journal.feed("root", [
            event(4, "assistant/delta", {"text": STREAM_MARKER}),
            event(5, "reasoning/delta", {"text": STREAM_MARKER}),
            event(6, "stream/chunk", {"text": STREAM_MARKER}),
        ])
        self.assertEqual(self.read_lines(self.directory / "telemetry.jsonl"), [])
        self.assertEqual(journal.last_seq, {"root": 6})
        # 游标已推进 => 同一 seq 不会在 resume 时重放。
        journal.feed("root", [event(6, ASSISTANT, message("不应重放"))])
        self.assertEqual(self.read_lines(self.directory / "telemetry.jsonl"), [])
        self.assertNotIn(STREAM_MARKER, self.read_text(self.directory / "events.jsonl"))

    def test_parent_child_usage_and_tool_results_are_collected(self):
        journal = self.journal()
        journal.register("root", model=CATALOG_DEFAULT["model"])
        journal.register("child-1", parent="root", model="deepseek-flash")
        journal.register("child-1", parent="root")
        journal.feed("root", [
            event(1, ASSISTANT, message("父代理", usage={"inputTokens": 10, "outputTokens": 4, "bogus": 1})),
            event(2, TOOL_CALL, {"turn": 1, "step": 2, "name": "read_file", "callId": "call-parent"}),
            event(3, TOOL_RESULT, {"turn": 1, "step": 2, "message": {"content": [
                {"type": "tool-result", "toolCallId": "call-parent", "isError": False,
                 "content": [{"type": "text", "text": "父结果"}]}]}}),
            event(4, TOOL_RESULT, {"turn": 1, "step": 3, "message": {"content": [
                {"type": "tool-result", "toolCallId": "call-error", "isError": True,
                 "content": [{"type": "text", "text": "父错误"}]}]}}),
            event(5, TURN_END, COMPLETED),
        ])
        journal.feed("child-1", [
            event(1, ASSISTANT, message("子代理", usage={"inputTokens": 3, "outputTokens": 2})),
            event(2, TOOL_CALL, {"turn": 1, "step": 2, "name": "read_file", "callId": "call-child"}),
            event(3, TOOL_RESULT, {"turn": 1, "step": 2, "message": {
                "source": {"callId": "call-child"},
                "content": [{"type": "text", "text": "子结果"}]}}),
            event(4, TOOL_RESULT, {"turn": 1, "step": 3, "message": {
                "source": {"callId": "call-nested-error"}, "isError": True,
                "content": [{"type": "text", "text": "子错误"}]}}),
            event(5, TOOL_RESULT, {"turn": 1, "step": 4, "message": {
                "source": {"callId": "call-loose"}, "isError": 1,
                "content": [{"type": "text", "text": "宽松真值"}]}}),
            event(6, TURN_END, COMPLETED),
        ], parent="root")
        journal.finish()

        telemetry = self.read_lines(self.directory / "telemetry.jsonl")
        created = [row for row in telemetry if row["type"] == "agent_created"]
        self.assertEqual(len(created), 2, "重复 register 必须幂等")
        self.assertEqual([row["type"] for row in telemetry].count("subagent_started"), 1)
        usage = {row["agent_id"]: row["usage"] for row in telemetry if row["type"] == "usage"}
        self.assertEqual(usage, {"root": {"inputTokens": 10, "outputTokens": 4},
                                 "child-1": {"inputTokens": 3, "outputTokens": 2}})
        self.assertEqual([row["step_id"] for row in telemetry if row["type"] == "usage"],
                         ["1.1", "1.1"])
        results = {row["call_id"]: (row["status"], row["result"])
                   for row in telemetry if row["type"] == "tool_result"}
        self.assertEqual(results, {
            "call-parent": ("ok", "父结果"), "call-child": ("ok", "子结果"),
            "call-error": ("error", "父错误"), "call-nested-error": ("error", "子错误"),
            # isError 必须是 True 本身：1/"true" 之类不算错误。
            "call-loose": ("ok", "宽松真值")})
        self.assertEqual(journal.tools, {"read_file": 2})

        events = self.read_lines(self.directory / "events.jsonl")
        statuses = [row for row in events if row["type"] == "status"]
        # 只有 root 的 usage 进入 events.jsonl（子会话的 usage 只留在 telemetry）。
        self.assertEqual([row["phase"] for row in statuses], ["step_end", "turn_end"])
        self.assertEqual(statuses[0]["usage"], {"inputTokens": 10, "outputTokens": 4})
        self.assertEqual([row["status"] for row in events if row["type"] == "tool_result"],
                         ["ok", "error", "ok", "error", "ok"])
        finished = {row["agent_id"]: (row["parent_id"], row["status"])
                    for row in telemetry if row["type"] == "agent_finished"}
        self.assertEqual(finished, {"root": (None, "completed"), "child-1": ("root", "completed")})

    def test_hidden_blocks_keys_and_stream_never_reach_logs(self):
        journal = self.journal()
        journal.feed("root", [
            event(1, ASSISTANT, message("公开输出", extra_blocks=[
                {"type": "reasoning", "text": REASONING_MARKER},
                {"type": "thinking", "text": REASONING_MARKER},
            ])),
            event(2, USER, message("用户问题", role="user")),
            event(3, TOOL_CALL, {"turn": 1, "step": 3, "name": "write_file", "callId": "call-1",
                                 "arguments": {"visible": "ok", "thinking": REASONING_MARKER,
                                               "reasoning": REASONING_MARKER,
                                               "system_instructions": INSTRUCTION_MARKER,
                                               "developer_instructions": INSTRUCTION_MARKER,
                                               "nested": {"analysis": REASONING_MARKER, "keep": 1}}}),
            event(4, TOOL_RESULT, {"turn": 1, "step": 3, "message": {"content": [
                {"type": "tool-result", "toolCallId": "call-1", "isError": False, "content": [
                    {"type": "reasoning", "text": REASONING_MARKER},
                    {"type": "text", "text": "工具输出"}]}]}}),
            event(5, "assistant/delta", {"text": STREAM_MARKER}),
        ])
        journal.finish()

        telemetry = self.read_lines(self.directory / "telemetry.jsonl")
        raw = self.read_text(self.directory / "telemetry.jsonl") + self.read_text(self.directory / "events.jsonl")
        for marker in (REASONING_MARKER, INSTRUCTION_MARKER, STREAM_MARKER):
            self.assertNotIn(marker, raw)
        tool_call = next(row for row in telemetry if row["type"] == "tool_call")
        self.assertEqual(tool_call["input"], {"visible": "ok", "nested": {"keep": 1}})
        result = next(row for row in telemetry if row["type"] == "tool_result")
        self.assertEqual(result["result"], "工具输出")
        self.assertIn("公开输出", raw)
        self.assertIn("用户问题", raw)
        final = [row for row in self.read_lines(self.directory / "events.jsonl") if row["type"] == "final"]
        self.assertEqual([row["text"] for row in final], ["公开输出"])

    def test_hidden_role_messages_do_not_reach_logs(self):
        """已知缺陷 D1：隐藏角色的正文会当作公开消息写进日志。

        复现：desktop.py:153-159 只按事件种类推断 ``role``（``assistant/message``
        → assistant，其余 → user），没有调用 ``codex_sessions.is_hidden_role``；
        ``message.role`` 为 ``system``/``developer`` 的正文照样进 telemetry，且
        ``assistant/message`` 的隐藏正文还会成为 ``final``（desktop.py:160-161）。
        无头路径 ``codex_sessions.parse_session`` 会按 ``_PUBLIC_MESSAGE_ROLES``
        过滤，桌面路径与之不一致。修复后本测试应通过（届时移除本标记）。
        """
        journal = self.journal()
        journal.feed("root", [
            event(1, USER, message(SYSTEM_MARKER, role="system")),
            event(2, ASSISTANT, message(DEVELOPER_MARKER, role="developer")),
        ])
        journal.finish()
        raw = (self.read_text(self.directory / "telemetry.jsonl")
               + self.read_text(self.directory / "events.jsonl"))
        self.assertNotIn(SYSTEM_MARKER, raw)
        self.assertNotIn(DEVELOPER_MARKER, raw)

    def test_mixed_sequence_types_do_not_abort_the_feed(self):
        """已知缺陷 D2：非 int seq 的容错分支不可达。

        复现：desktop.py:139 先按 ``event.seq`` 排序，desktop.py:142 才丢弃
        ``type(seq) is not int``；同一页混入字符串 seq 时 ``sorted`` 先抛
        ``TypeError``，整次 feed 中止——在 ``execute`` 里会连带取消整个任务。
        期望：坏记录被跳过，合法 seq 正常投影。修复后移除本标记。
        """
        journal = self.journal()
        journal.feed("root", [
            event(1, TOOL_CALL, {"turn": 1, "step": 1, "name": "read_file", "callId": "call-1"}),
            event("2", TOOL_CALL, {"turn": 1, "step": 2, "name": "read_file", "callId": "call-2"}),
        ])
        telemetry = self.read_lines(self.directory / "telemetry.jsonl")
        self.assertEqual([row["type"] for row in telemetry], ["tool_call"])
        self.assertEqual(journal.last_seq, {"root": 1})

    def test_sensitive_values_are_redacted_from_both_logs(self):
        journal = self.journal()
        journal.feed("root", [
            event(1, ASSISTANT, message("password=%s 与 Authorization: Bearer %s" % (SECRET, API_KEY))),
            event(2, TOOL_CALL, {"turn": 1, "step": 2, "name": "write_file", "callId": "call-1",
                                 "arguments": {"api_key": API_KEY, "password": SECRET,
                                               "path": "tests/test_desktop.py",
                                               "nested": {"token": SECRET}}}),
            event(3, TOOL_RESULT, {"turn": 1, "step": 2, "message": {"content": [
                {"type": "tool-result", "toolCallId": "call-1", "isError": False, "content": [
                    {"type": "text", "text": '{"private_key": "%s"}' % SECRET}]}]}}),
        ])
        journal.finish()

        raw = self.read_text(self.directory / "telemetry.jsonl") + self.read_text(self.directory / "events.jsonl")
        self.assertNotIn(SECRET, raw)
        self.assertNotIn(API_KEY, raw)
        self.assertIn(REDACTED, raw)

        telemetry = self.read_lines(self.directory / "telemetry.jsonl")
        tool_call = next(row for row in telemetry if row["type"] == "tool_call")
        self.assertEqual(tool_call["input"]["api_key"], REDACTED)
        self.assertEqual(tool_call["input"]["password"], REDACTED)
        self.assertEqual(tool_call["input"]["nested"]["token"], REDACTED)
        self.assertEqual(tool_call["input"]["path"], "tests/test_desktop.py")
        result = next(row for row in telemetry if row["type"] == "tool_result")
        self.assertIn(REDACTED, result["result"])
        shown = next(row for row in telemetry if row["type"] == "message")["text"]
        self.assertIn(REDACTED, shown)
        self.assertNotIn(SECRET, shown)

    def test_text_limits_and_finish_are_idempotent(self):
        journal = self.journal()
        long_text = "长" * 20000
        journal.feed("root", [event(1, ASSISTANT, message(long_text))])
        journal.finish()
        journal.finish()  # 幂等：已关闭时不得重复写 final

        observed = next(row for row in self.read_lines(self.directory / "telemetry.jsonl")
                        if row["type"] == "message")["text"]
        self.assertEqual(observed, "长" * 16000 + TRUNCATION)
        finals = [row for row in self.read_lines(self.directory / "events.jsonl") if row["type"] == "final"]
        self.assertEqual([row["text"] for row in finals], ["长" * 12000 + TRUNCATION])

        # 恰好等于上限时不截断（边界是 <=，不是 <）。
        boundary = self.root / "boundary-run"
        boundary.mkdir()
        exact = self.journal(directory=boundary)
        exact.feed("root", [event(1, ASSISTANT, message("短" * 16000))])
        exact.finish()
        shown = next(row for row in self.read_lines(boundary / "telemetry.jsonl")
                     if row["type"] == "message")["text"]
        self.assertEqual(shown, "短" * 16000)

        # 无公开文本时不得产出 final。
        other = self.root / "empty-run"
        other.mkdir()
        quiet = self.journal(directory=other)
        quiet.feed("root", [event(1, ASSISTANT, message("", extra_blocks=[
            {"type": "reasoning", "text": REASONING_MARKER}]))])
        quiet.finish()
        self.assertEqual([row for row in self.read_lines(other / "events.jsonl")
                          if row["type"] == "final"], [])

    def test_usage_whitelist_filters_bool_and_non_finite(self):
        journal = self.journal()
        journal.feed("root", [event(1, ASSISTANT, message("用量", turn=2, step=5, usage={
            "inputTokens": 4, "outputTokens": True, "cacheReadTokens": float("nan"),
            "cacheWriteTokens": float("inf"), "totalTokens": "9", "bogus": 1}))])
        journal.feed("root", [event(2, ASSISTANT, message("零用量", turn=2, step=6, usage={"inputTokens": False}))])
        journal.finish()

        telemetry = self.read_lines(self.directory / "telemetry.jsonl")
        usage = [row for row in telemetry if row["type"] == "usage"]
        self.assertEqual(len(usage), 1, "过滤后为空的 usage 不产生记录")
        self.assertEqual(usage[0]["usage"], {"inputTokens": 4})
        self.assertEqual(usage[0]["step_id"], "2.5")
        self.assertNotIn("NaN", self.read_text(self.directory / "telemetry.jsonl"))

    def test_private_log_files_are_owner_only(self):
        self.journal()
        for name in ("events.jsonl", "telemetry.jsonl"):
            self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o600, name)
        first = self.read_lines(self.directory / "events.jsonl")
        self.assertEqual(first, [{"type": "session", "sessionId": "root"}])


class ScopeTests(_Case):
    def test_tool_call_before_stage_change_marks_scope_error(self):
        journal = self.journal()
        journal.feed("root", [
            event(1, TOOL_CALL, {"turn": 1, "step": 1, "name": "read_file", "callId": "call-1"}),
            event(2, CWD_CHANGE, {"cwd": str(self.stage)}),
        ])
        self.assertTrue(journal.scope_error)
        self.assertTrue(journal.scope_entered)

    def test_working_directory_tool_call_is_exempt_but_does_not_enter_scope(self):
        journal = self.journal()
        journal.feed("root", [event(1, TOOL_CALL, {"turn": 1, "step": 1,
                                                   "name": "working_directory", "callId": "call-1"})])
        self.assertFalse(journal.scope_error, "首个工具是 working_directory 不算越界")
        self.assertFalse(journal.scope_entered, "工具调用本身不代表已切换目录")
        journal.feed("root", [event(2, TOOL_CALL, {"turn": 1, "step": 2,
                                                   "name": "read_file", "callId": "call-2"})])
        self.assertTrue(journal.scope_error)

    def test_leaving_the_stage_reopens_the_scope_check(self):
        journal = self.journal()
        journal.feed("root", [event(1, CWD_CHANGE, {"cwd": str(self.stage)})])
        self.assertTrue(journal.scope_entered)
        journal.feed("root", [event(2, CWD_CHANGE, {"cwd": str(self.live)})])
        self.assertFalse(journal.scope_entered, "scope_entered 是覆盖赋值")
        journal.feed("root", [event(3, TOOL_CALL, {"turn": 1, "step": 3,
                                                   "name": "read_file", "callId": "call-3"})])
        self.assertTrue(journal.scope_error, "离开暂存目录后调用工具必须判越界")

    def test_child_activity_never_enters_root_scope(self):
        journal = self.journal()
        journal.feed("child-1", [event(1, CWD_CHANGE, {"cwd": str(self.stage)})], parent="root")
        journal.feed("child-1", [event(2, TOOL_CALL, {"turn": 1, "step": 1,
                                                      "name": "read_file", "callId": "call-child"})], parent="root")
        self.assertFalse(journal.scope_entered)
        self.assertFalse(journal.scope_error, "子会话的工具调用不能替 root 建立范围")
        self.assertEqual(journal.tools, {"read_file": 1})

    def test_invalid_or_foreign_cwd_is_ignored(self):
        journal = self.journal()
        journal.feed("root", [
            event(1, CWD_CHANGE, {"cwd": 123}),
            event(2, CWD_CHANGE, {"cwd": str(self.live)}),
            event(3, TOOL_CALL, {"turn": 1, "step": 3, "name": "read_file", "callId": "call-1"}),
        ])
        self.assertFalse(journal.scope_entered)
        self.assertTrue(journal.scope_error)


# ----------------------------------------------------------------------- read_records


class ReadRecordsTests(_Case):
    def records(self, seqs):
        return [event(seq, TOOL_CALL, {"turn": seq, "step": 1, "name": "tool-%d" % seq,
                                       "callId": "call-%d" % seq}) for seq in seqs]

    def sequenced(self, as_of):
        return {"kind": "sequenced", "asOfSeq": as_of, "values": {}}

    def test_multiple_pages_are_complete_and_chain_by_before_seq(self):
        client = _PagingClient(self.sequenced(4), self.records(range(5)), page_size=2)
        projection, records = read_records(client, "root", None, -1)

        seqs = [record["event"]["seq"] for record in records]
        self.assertEqual(sorted(seqs), [0, 1, 2, 3, 4], "跨页必须完整")
        self.assertEqual(len(seqs), len(set(seqs)), "协议正确的分页不应重复")
        self.assertEqual(projection["asOfSeq"], 4)

        requests = client.page_requests()
        self.assertEqual(len(requests), 3, "5 条记录 / 每页 2 条 => 3 页")
        self.assertNotIn("beforeSeq", requests[0])
        self.assertEqual([request.get("beforeSeq") for request in requests], [None, 3, 1])
        self.assertTrue(all(request["maxMessages"] == 500 for request in requests))
        self.assertTrue(all(request["throughSeq"] == 4 for request in requests))
        self.assertTrue(all(request["address"] == {"kind": "session", "sessionId": "root"}
                            for request in requests))
        self.assertEqual(client.calls[0], ("session/projections", {"request": {"sessionId": "root"}}))

        # 逐页回放同一个 journal：每个 seq 恰好投影一次。
        journal = self.journal()
        journal.feed("root", records)
        projected = [row["call_id"] for row in self.read_lines(self.directory / "telemetry.jsonl")]
        self.assertEqual(sorted(projected), sorted("call-%d" % seq for seq in range(5)))
        self.assertEqual(journal.last_seq, {"root": 4})

    def test_subagent_address_is_used_for_child_sessions(self):
        client = _PagingClient(self.sequenced(3), self.records([1, 2, 3]), page_size=500)
        read_records(client, "child-1", "root", -1)
        self.assertEqual(client.page_requests()[0]["address"], {
            "kind": "subagent", "parentSessionId": "root",
            "childSessionId": "child-1", "mode": "unknown"})

    def test_overlapping_pages_are_deduplicated_by_the_journal(self):
        client = _PagingClient(self.sequenced(4), self.records(range(5)), page_size=2, inclusive=True)
        _projection, records = read_records(client, "root", None, -1)
        seqs = [record["event"]["seq"] for record in records]
        self.assertEqual(sorted(set(seqs)), [0, 1, 2, 3, 4], "重叠页仍须覆盖全部 seq")
        self.assertGreater(len(seqs), len(set(seqs)), "read_records 自身不去重")

        journal = self.journal()
        journal.feed("root", records)
        call_ids = [row["call_id"] for row in self.read_lines(self.directory / "telemetry.jsonl")]
        self.assertEqual(sorted(call_ids), sorted(set(call_ids)), "去重由 (session, seq) 兜住")
        self.assertEqual(len(call_ids), 5)

    def test_empty_pages_stop_without_spinning(self):
        first_empty = _PagingClient(self.sequenced(4), self.records(range(5)), page_size=2,
                                    empty_pages=[1])
        self.assertEqual(read_records(first_empty, "root", None, -1)[1], [])
        self.assertEqual(first_empty.page_calls(), 1, "空页必须立刻停止")

        later_empty = _PagingClient(self.sequenced(4), self.records(range(5)), page_size=2,
                                    empty_pages=[2])
        _projection, records = read_records(later_empty, "root", None, -1)
        self.assertEqual([record["event"]["seq"] for record in records], [3, 4])
        self.assertEqual(later_empty.page_calls(), 2, "空页后立即停止，不重复请求")

    def test_stalled_pagination_raises(self):
        client = _PagingClient(self.sequenced(9), self.records([1, 2, 3]), page_size=2, stall=True)
        with self.assertRaisesRegex(ValueError, "没有前进"):
            read_records(client, "root", None, -1)
        self.assertEqual(client.page_calls(), 2)

    def test_cursor_reached_skips_page_requests(self):
        client = _PagingClient(self.sequenced(7), self.records(range(8)), page_size=2)
        projection, records = read_records(client, "root", None, 7)
        self.assertEqual(records, [])
        self.assertEqual(projection["asOfSeq"], 7)
        self.assertEqual(client.page_calls(), 0)
        self.assertEqual(len(client.calls), 1, "projection 仍会读一次，page 完全不发")

        # min(seqs) 恰好等于 after 时也必须终止（边界是 <=，不是 <）。
        stale = _StubClient(self.sequenced(5), {"records": self.records([2, 3]), "hasMore": True})
        _projection, records = read_records(stale, "root", None, 2)
        self.assertEqual([record["event"]["seq"] for record in records], [2, 3])
        self.assertEqual(stale.page_calls(), 1, "min(seqs) == after 必须终止分页")

    def test_invalid_payloads_fail_closed(self):
        cases = [
            ("projection 非对象", _StubClient(None, {}), "没有可验证的事件游标", 0),
            ("projection 非 sequenced", _StubClient({"kind": "snapshot", "asOfSeq": 1}, {}),
             "没有可验证的事件游标", 0),
            ("游标缺失", _StubClient({"kind": "sequenced"}, {}), "无效会话游标", 0),
            ("游标是 bool", _StubClient(self.sequenced(True), {}), "无效会话游标", 0),
            ("游标是浮点", _StubClient(self.sequenced(5.0), {}), "无效会话游标", 0),
            ("分页非数组", _StubClient(self.sequenced(3), {"records": {"seq": 1}, "hasMore": False}),
             "无效事件分页", 1),
        ]
        for label, client, message, pages in cases:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, message):
                read_records(client, "root", None, -1)
            self.assertEqual(client.page_calls(), pages, label)


# --------------------------------------------------------------------------- execute


class ExecuteTests(_Case):
    def setUp(self):
        super().setUp()
        self.clock = _FakeTime()
        self.spy = _SubprocessSpy()
        self.identity_urls = []
        # 日志路径必须指向 tmp：绝不允许测试读到真 ~/Library/Logs/DSH/server.log。
        self.log_path = str(self.root / "server.log")

    # -- 场景数据 -----------------------------------------------------------------
    def root_history(self, *, with_change=True, with_end=False):
        records = [
            event(1, CWD_CHANGE, {"cwd": str(self.stage)}),
            event(2, ASSISTANT, message("已切换工作目录", extra_blocks=[
                {"type": "reasoning", "text": REASONING_MARKER}],
                usage={"inputTokens": 11, "outputTokens": 3, "bogus": 99})),
            event(3, TOOL_CALL, {"turn": 1, "step": 2, "name": "working_directory", "callId": "call-cwd"}),
            event(4, TOOL_CALL, {"turn": 1, "step": 3, "name": "read_file", "callId": "call-read",
                                 "arguments": {"path": "src/leadseek/desktop.py", "api_key": API_KEY}}),
            event(5, TOOL_RESULT, {"turn": 1, "step": 3, "message": {"content": [
                {"type": "tool-result", "toolCallId": "call-read", "isError": False,
                 "content": [{"type": "text", "text": "module body"}]}]}}),
            event(6, ASSISTANT, message("最终报告", turn=1, step=4,
                                        usage={"inputTokens": 7, "outputTokens": 2})),
        ]
        if not with_change:
            records = [record for record in records if record["event"]["type"] != CWD_CHANGE]
        if with_end:
            records.append(event(7, TURN_END, COMPLETED))
        return records

    def child_history(self, *, with_end=False, broken=False, started=True):
        records = []
        if started:
            records.append(event(1, ASSISTANT, message("子代理完成",
                                                       usage={"inputTokens": 3, "outputTokens": 2})))
        if broken:
            records.append(event(2, TOOL_CALL, {"turn": 1, "step": 1, "name": "read_file",
                                                "callId": "call-bad"}))
        else:
            records.append(event(2, TOOL_CALL, {"turn": 1, "step": 2, "name": "read_file",
                                                "callId": "call-child"}))
            records.append(event(3, TOOL_RESULT, {"turn": 1, "step": 2, "message": {
                "source": {"callId": "call-child"},
                "content": [{"type": "text", "text": "child body"}]}}))
        if with_end:
            records.append(event(4, TURN_END, COMPLETED))
        return records

    def poll(self, *, root_records, child_records=None, root_running=True, child_running=True,
             selection=None):
        return {
            "items": [
                {"sessionId": "$root", "parentSessionId": None, "running": root_running},
                {"sessionId": "child-1", "parentSessionId": "$root", "running": child_running},
            ],
            "records": {"$root": root_records, "child-1": child_records or []},
            "selection": {"$root": NATIVE_SELECTION} if selection is None else selection,
        }

    def finished_poll(self):
        return self.poll(root_records=self.root_history(with_end=True),
                         child_records=self.child_history(with_end=True),
                         root_running=False, child_running=False)

    # -- 驱动 ---------------------------------------------------------------------
    def make_task(self, **overrides):
        task = {
            "goal": "为桌面同进程 DSH 改造写实质性单元测试",
            "workspace": str(self.live),
            "mode": "edit",
            "read_paths": ["src/leadseek/desktop.py"],
            "write_paths": ["tests/test_desktop.py"],
            "constraints": ["仅测试"],
            "checks": [],
            "subagents": None,
            "timeout_seconds": 30,
        }
        task.update(overrides)
        return task

    def run_execute(self, fake, *, task=None, config=None, identities=None):
        task = task or self.make_task()
        config = {"desktop_url": TEST_URL, "desktop_log_path": self.log_path,
                  "max_log_bytes": 8 * 1024 * 1024, **(config or {})}
        expected = list(identities or [IDENTITY])

        def identity(url):
            self.identity_urls.append(url)
            value = expected[min(len(self.identity_urls) - 1, len(expected) - 1)]
            if isinstance(value, BaseException):
                raise value
            return value

        with mock.patch.object(desktop, "server_identity", new=identity), \
                mock.patch.object(desktop_client, "DesktopClient", new=fake.factory), \
                mock.patch.object(desktop, "subprocess", new=self.spy), \
                mock.patch.object(desktop, "time", new=self.clock):
            return desktop.execute(task, config, self.directory)

    def call_ids(self, endpoint, fake):
        return [args for _tag, name, args in fake.calls if name == endpoint]

    # -- 断言 ---------------------------------------------------------------------
    def test_inherits_native_model_and_preset_without_override_requests(self):
        fake = FakeDesktop([self.finished_poll()])
        result = self.run_execute(fake)

        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["interruption"])
        self.assertEqual(result["execution_backend"], "desktop")
        self.assertEqual(result["server_url"], TEST_URL)
        self.assertEqual(result["server_pid"], IDENTITY["server_pid"])
        self.assertEqual(result["harness_version"], IDENTITY["harness_version"])
        # 模型/思考强度来自桌面会话自身的 lastUsed（Max），不是 leadseek 配置。
        self.assertEqual(result["model_selection"], NATIVE_SELECTION)
        self.assertEqual(result["agent_preset"], "leadseek-preset-native")
        self.assertNotEqual(result["model_selection"], fake.catalog["default"])

        # 只读目录：唯一的模型相关调用是读取目录，且不带任何参数。
        self.assertEqual(self.call_ids("session/modelCatalog", fake), [None])
        # 没有任何 selectModel / 插件禁用 / 权限或沙箱覆盖请求。
        forbidden = re.compile(r"model|preset|plugin|permission|sandbox|approval|config|settings|"
                               r"restart|shutdown|stop|kill|delete|close", re.I)
        for _tag, endpoint, _args in fake.calls:
            if endpoint == "session/modelCatalog":
                continue
            self.assertIsNone(forbidden.search(endpoint), "越权 RPC: %s" % endpoint)
        self.assertEqual(set(fake.methods()), {
            "session/modelCatalog", "workspace/create", "session/create", "session/rename",
            "session/prompt", "session/list", "session/projections", "session/page"})

    def test_protocol_calls_workspace_session_rename_and_prompt(self):
        fake = FakeDesktop([self.finished_poll()])
        goal = "G" * 200
        task = self.make_task(goal=goal)
        result = self.run_execute(fake, task=task)
        session = result["session_id"]

        # 原生工作区：声明的是任务里的原项目目录，不是暂存目录。
        self.assertEqual(self.call_ids("workspace/create", fake), [{"request": {"path": str(self.live)}}])
        created = self.call_ids("session/create", fake)[0]["request"]
        self.assertEqual(set(created), {"workspaceId", "sessionId"}, "不得夹带预设/模型覆盖")
        self.assertEqual(created["workspaceId"], "ws-native")
        self.assertEqual(created["sessionId"], session)

        renamed = self.call_ids("session/rename", fake)[0]["request"]
        self.assertEqual(renamed, {"sessionId": session, "title": "Codex · " + "G" * 65})

        prompts = self.call_ids("session/prompt", fake)
        self.assertEqual(len(prompts), 1, "任务只下发一次，不重发")
        prompt = prompts[0]["request"]
        self.assertEqual(set(prompt), {"requestId", "sessionId", "mode", "content"})
        self.assertEqual(prompt["sessionId"], session)
        self.assertEqual(prompt["mode"], "queue")
        self.assertEqual(uuid.UUID(prompt["requestId"]).version, 4)
        self.assertEqual(len(prompt["content"]), 1)
        self.assertEqual(prompt["content"][0]["type"], "text")
        self.assertIn("working_directory", prompt["content"][0]["text"])
        self.assertEqual(prompt["content"][0]["text"], self.read_text(self.directory / "prompt.txt"))
        self.assertEqual(prompt["content"][0]["text"], build_prompt(task, self.stage))
        self.assertEqual(stat.S_IMODE((self.directory / "prompt.txt").stat().st_mode), 0o600)

        # 轮询协议：session/list 无参数；只读投影按 session；分页按 address 寻址。
        self.assertEqual(self.call_ids("session/list", fake), [{"_request": {}}])
        self.assertEqual(self.call_ids("session/projections", fake),
                         [{"request": {"sessionId": session}}, {"request": {"sessionId": "child-1"}}])
        pages = [args["request"] for args in self.call_ids("session/page", fake)]
        self.assertEqual({page["address"]["kind"] for page in pages}, {"session", "subagent"})
        for page in pages:
            self.assertEqual(page["maxMessages"], 500)
        root_page = next(page for page in pages if page["address"]["kind"] == "session")
        self.assertEqual(root_page["address"], {"kind": "session", "sessionId": session})
        self.assertEqual(root_page["throughSeq"], 7)
        child_page = next(page for page in pages if page["address"]["kind"] == "subagent")
        self.assertEqual(child_page["address"], {"kind": "subagent", "parentSessionId": session,
                                                 "childSessionId": "child-1", "mode": "unknown"})

    def test_prompt_requires_working_directory_first(self):
        prompt = build_prompt(self.make_task(), self.stage)
        self.assertIn("working_directory", prompt)
        self.assertIn("成功前禁止调用其他工具", prompt)
        self.assertIn(json.dumps(str(self.stage), ensure_ascii=False), prompt)
        tail = prompt.split("任务：\n", 1)[1]
        contract = json.loads(tail)
        self.assertEqual(set(contract), {"goal", "mode", "read_paths", "write_paths",
                                         "constraints", "checks", "subagents"})
        self.assertEqual(contract["write_paths"], ["tests/test_desktop.py"])
        self.assertIsNone(contract["subagents"])

    def test_no_headless_process_no_real_desktop_and_single_client(self):
        fake = FakeDesktop([self.finished_poll()])
        self.run_execute(fake)

        self.assertEqual(self.spy.calls, [], "不得启动任何子进程/headless 运行时")
        self.assertEqual(len(fake.clients), 1, "正常路径只建立一个连接")
        self.assertEqual(fake.sessions[0], {"url": TEST_URL, "log_path": self.log_path})
        self.assertTrue(fake.clients[0].closed, "连接必须关闭")
        self.assertEqual(self.identity_urls, [TEST_URL, TEST_URL], "收尾必须复核服务身份")

        # 未配置日志路径时必须原样传 None，不得自行编造或回落到真实日志。
        other = FakeDesktop([self.finished_poll()])
        config = {"desktop_log_path": None}
        self.run_execute(other, config=config)
        self.assertEqual(other.sessions[0], {"url": TEST_URL, "log_path": None})

    def test_missing_server_uses_autostart_then_same_native_execution(self):
        fake = FakeDesktop([self.finished_poll()])
        with mock.patch("leadseek.desktop_start.ensure_running", return_value={**IDENTITY, "desktop_auto_started": True}) as launch:
            result = self.run_execute(fake, identities=[ValueError("absent"), IDENTITY])
        launch.assert_called_once_with(TEST_URL, self.log_path, auto_start=True)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["desktop_auto_started"])
        self.assertEqual(fake.cancel_sessions(), [])
        self.assertEqual(self.spy.calls, [])

    def test_completes_only_after_root_and_child_have_finished(self):
        root_done = self.root_history(with_end=True)
        child_partial = self.child_history()
        child_done = self.child_history(with_end=True)
        fake = FakeDesktop([
            self.poll(root_records=root_done, child_records=child_partial),
            self.poll(root_records=root_done, child_records=child_partial, root_running=False),
            self.poll(root_records=root_done, child_records=child_done,
                      root_running=False, child_running=False),
        ])
        result = self.run_execute(fake)

        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["interruption"])
        self.assertEqual(fake.list_calls, 3, "root 结束后仍须等子会话终结")
        self.assertGreaterEqual(self.clock.sleep_calls, 2, "轮询之间必须有节流（替身不真睡）")
        self.assertEqual(fake.cancel_sessions(), [])
        self.assertEqual(result["elapsed_seconds"], 0.0)

        telemetry = self.read_lines(self.directory / "telemetry.jsonl")
        self.assertEqual({row["agent_id"] for row in telemetry if row["type"] == "agent_finished"},
                         {result["session_id"], "child-1"})

    def test_scope_violation_interrupts_and_cancels_only_own_session(self):
        violating = self.poll(root_records=self.root_history(with_change=False),
                              child_records=self.child_history(), root_running=False,
                              child_running=False)
        fake = FakeDesktop([violating, violating])
        result = self.run_execute(fake)

        self.assertEqual(result["interruption"], "scope_violation")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(fake.list_calls, 1, "越界当轮即中断")
        self.assertEqual(fake.cancel_sessions(), [result["session_id"]])
        self.assertEqual(len(fake.clients), 1, "中断走主连接，不新建客户端")
        self.assertNotIn("child-1", fake.cancel_sessions())
        for _tag, endpoint, _args in fake.calls:
            self.assertNotRegex(endpoint, r"restart|shutdown|stop|kill|delete|close")

    def test_subagent_cap_interrupts_when_child_exceeds_limit(self):
        poll = self.poll(root_records=self.root_history(), child_records=self.child_history(),
                         root_running=True, child_running=True)
        fake = FakeDesktop([poll, poll])
        result = self.run_execute(fake, task=self.make_task(subagents=0), identities=[IDENTITY, IDENTITY])

        self.assertEqual(result["interruption"], "scope_violation")
        self.assertEqual(fake.list_calls, 1)
        self.assertEqual(fake.cancel_sessions(), [result["session_id"]])

    def test_interruption_priority_and_log_limit(self):
        # scope_violation 优先于 subagent 上限、超时与日志上限。
        self.clock.step = 100.0
        scope_fake = FakeDesktop([self.poll(root_records=self.root_history(with_change=False),
                                            child_records=self.child_history())])
        scoped = self.run_execute(scope_fake, task=self.make_task(subagents=0),
                                  config={"max_log_bytes": 1}, identities=[IDENTITY, IDENTITY])
        self.assertEqual(scoped["interruption"], "scope_violation")

        # 日志上限优先于"全部会话已结束"的完成判定。
        self.clock.step = 0.0
        limit_fake = FakeDesktop([self.finished_poll()])
        limited = self.run_execute(limit_fake, config={"max_log_bytes": 1})
        self.assertEqual(limited["interruption"], "log_limit_exceeded")
        self.assertEqual(limited["exit_code"], 1)
        self.assertEqual(limit_fake.cancel_sessions(), [limited["session_id"]])

        # 超时：假时钟第 2 次判断即到期（timeout_seconds=10，每次 +5s）。
        self.clock.step = 5.0
        stall = self.poll(root_records=self.root_history(), child_records=self.child_history())
        timeout_fake = FakeDesktop([stall, stall, stall])
        timed_out = self.run_execute(timeout_fake, task=self.make_task(timeout_seconds=10))
        self.assertEqual(timed_out["interruption"], "timed_out")
        self.assertEqual(timeout_fake.list_calls, 2)
        self.assertEqual(timeout_fake.cancel_sessions(), [timed_out["session_id"]])

    def test_rejected_prompt_cancels_own_session_via_timeout_client(self):
        # accepted 必须是布尔 True；1/"yes" 这类宽松真值不算确认，也不得重发。
        for accepted in (False, 1, "yes"):
            with self.subTest(accepted=accepted):
                fake = FakeDesktop([self.finished_poll()], accepted=accepted)
                with self.assertRaisesRegex(ValueError, "未确认接受任务"):
                    self.run_execute(fake)

                created = self.call_ids("session/create", fake)[0]["request"]["sessionId"]
                self.assertEqual(len(self.call_ids("session/prompt", fake)), 1, "不得重发任务")
                cancels = [(tag, args) for tag, name, args in fake.calls if name == "session/cancel"]
                self.assertEqual([tag for tag, _args in cancels], ["cleanup"],
                                 "清理必须用独立短超时客户端")
                self.assertEqual([args for _tag, args in cancels],
                                 [{"request": {"sessionId": created}}])
                self.assertEqual(fake.sessions[1],
                                 {"url": TEST_URL, "log_path": self.log_path, "timeout": 3})
                self.assertTrue(fake.clients[0].closed, "异常路径也必须关闭主连接")
                self.assertNotIn("child-1", fake.cancel_sessions())

                saved = self.read_json(self.directory / "desktop.json")
                self.assertEqual(saved["session_id"], created)
                self.assertEqual(saved["execution_backend"], "desktop")

    def test_unverifiable_identity_skips_cancellation_and_records_it(self):
        fake = FakeDesktop([self.finished_poll()], accepted=False)
        with self.assertRaisesRegex(ValueError, "未确认接受任务"):
            self.run_execute(fake, identities=[IDENTITY, OSError("lsof 不可用")])

        self.assertEqual(fake.cancel_sessions(), [], "无法确认身份时不得触碰桌面服务")
        self.assertEqual(len(fake.clients), 1)
        self.assertIs(self.read_json(self.directory / "desktop.json")["cancellation_confirmed"], False)

    def test_changed_server_identity_blocks_delivery_without_cancelling(self):
        fake = FakeDesktop([self.finished_poll()])
        changed = {"server_pid": 777, "harness_version": "9.9.9"}
        with self.assertRaisesRegex(ValueError, "服务进程在任务中改变"):
            self.run_execute(fake, identities=[IDENTITY, changed, changed])
        self.assertEqual(fake.cancel_sessions(), [])
        self.assertEqual(len(fake.clients), 1)

    def test_only_run_directory_is_written_and_staged_work_stays_staged(self):
        (self.stage / "code.py").write_text("staged change\n", encoding="utf-8")
        before = self.snapshot(self.live)
        fake = FakeDesktop([self.finished_poll()])
        spy = _WriteSpy()
        with spy:
            result = self.run_execute(fake)
        self.assertEqual(result["exit_code"], 0)

        self.assertEqual(self.snapshot(self.live), before, "原项目文件与权限都不得改变")
        self.assertEqual(self.read_text(self.live / "code.py"), "old\n")
        self.assertEqual(self.read_text(self.stage / "code.py"), "staged change\n",
                         "暂存改动留在暂存区，只有 apply 路径能让它落地")

        self.assertGreaterEqual(len(spy.paths), 6, "写入记录器必须真的观察到落盘")
        for path in spy.paths:
            self.assertIn(self.directory, path.parents, "越界写入: %s" % path)
            self.assertNotIn(self.stage, path.parents, "execute 不得代写暂存工作区")
            self.assertNotIn(self.live, path.parents)

    def test_metadata_budget_and_private_artifacts(self):
        (self.directory / "context.json").write_text(json.dumps(
            {"codex_thread_id": "abc", "previous_run_id": None, "model": "configured",
             "provider": "configured"}), encoding="utf-8")
        (self.directory / "status.json").write_text(json.dumps(
            {"run_id": "r1", "status": "running"}), encoding="utf-8")
        fake = FakeDesktop([self.finished_poll()])
        result = self.run_execute(fake)
        session = result["session_id"]

        desktop_meta = self.read_json(self.directory / "desktop.json")
        self.assertEqual(desktop_meta["execution_backend"], "desktop")
        self.assertEqual(desktop_meta["session_id"], session)
        self.assertEqual(desktop_meta["server_pid"], IDENTITY["server_pid"])
        self.assertEqual(desktop_meta["harness_version"], IDENTITY["harness_version"])
        self.assertEqual(desktop_meta["workspace_id"], "ws-native")
        self.assertEqual(desktop_meta["agent_preset"], "leadseek-preset-native")
        self.assertEqual(desktop_meta["model_selection"], NATIVE_SELECTION)

        context = self.read_json(self.directory / "context.json")
        self.assertEqual(context["codex_thread_id"], "abc", "无关字段必须保留")
        self.assertEqual(context["model"], NATIVE_SELECTION["model"])
        self.assertEqual(context["provider"], NATIVE_SELECTION["provider"])
        self.assertEqual(context["reasoning_effort"], NATIVE_SELECTION["reasoningEffort"])
        self.assertEqual(context["execution_backend"], "desktop")
        self.assertEqual(context["session_id"], session)
        self.assertEqual(self.read_json(self.directory / "status.json")["run_id"], "r1")

        budget = self.read_json(self.directory / "budget.json")
        self.assertEqual(budget["accepted_total"], 3)
        self.assertEqual(budget["accepted_by_tool"], {"working_directory": 1, "read_file": 2})
        self.assertEqual(budget["denied"], 0)
        self.assertEqual(budget["enforcement"], "desktop-native-policy")
        self.assertIsNone(budget["bridge_tool_limit"])
        self.assertEqual(budget["subagents"], "desktop-auto")

        for name in ("desktop.json", "prompt.txt", "events.jsonl", "telemetry.jsonl",
                     "budget.json", "stderr.log", "context.json", "status.json"):
            self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o600, name)

        events = self.read_lines(self.directory / "events.jsonl")
        self.assertEqual([row["phase"] for row in events if row["type"] == "status"],
                         ["step_end", "step_end", "turn_end"])
        self.assertEqual(len([row for row in events if row["type"] == "tool_call"]), 3)
        self.assertEqual(len([row for row in events if row["type"] == "tool_result"]), 2)
        self.assertEqual([row["text"] for row in events if row["type"] == "final"], ["最终报告"])

        telemetry = self.read_lines(self.directory / "telemetry.jsonl")
        results = {row["call_id"] for row in telemetry if row["type"] == "tool_result"}
        self.assertEqual(results, {"call-read", "call-child"}, "现代与嵌套结果都要带 callId")
        usage = {row["agent_id"]: row["usage"] for row in telemetry if row["type"] == "usage"}
        self.assertEqual(usage, {session: {"inputTokens": 7, "outputTokens": 2},
                                 "child-1": {"inputTokens": 3, "outputTokens": 2}})
        raw = self.read_text(self.directory / "telemetry.jsonl") + self.read_text(self.directory / "events.jsonl")
        self.assertNotIn(REASONING_MARKER, raw)
        self.assertNotIn(API_KEY, raw)
        self.assertIn(REDACTED, raw)


# -------------------------------------------------------------------- server_identity


class _CompletedRun:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


class ServerIdentityTests(_Case):
    def setUp(self):
        super().setUp()
        self.commands = []

    def run_identity(self, *, lsof, ps="", url=TEST_URL):
        def fake_run(command, **_kwargs):
            self.commands.append(command)
            return _CompletedRun(lsof if command[0].endswith("lsof") else ps)

        with mock.patch.object(desktop, "subprocess", mock.Mock(run=fake_run)):
            return desktop.server_identity(url)

    def dsh_package(self, name="@deepseek-ai/dsh", version="1.2.3", directory="pkg"):
        package = self.root / directory
        (package / "lib").mkdir(parents=True, exist_ok=True)
        (package / "package.json").write_text(json.dumps({"name": name, "version": version}),
                                              encoding="utf-8")
        entry = package / "lib/bin.js"
        entry.touch()
        return entry

    def test_single_listener_is_reported_without_leaking_argv(self):
        entry = self.dsh_package()
        result = self.run_identity(
            lsof="p4242\n", ps="/usr/local/bin/node %s --token=secret\n" % entry)

        self.assertEqual(result, {"server_pid": 4242, "harness_version": "1.2.3"})
        self.assertEqual(set(result), {"server_pid", "harness_version"})
        serialized = json.dumps(result)
        for leaked in (str(entry), "--token", "secret", "node"):
            self.assertNotIn(leaked, serialized)
        self.assertEqual(self.commands, [
            ["/usr/sbin/lsof", "-nP", "-iTCP:3999", "-sTCP:LISTEN", "-Fp"],
            ["/bin/ps", "-p", "4242", "-o", "args="],
        ])

    def test_ambiguous_or_missing_listener_is_rejected(self):
        for output in ("p1\np2\n", "", "not-a-pid\n"):
            self.commands.clear()
            with self.subTest(lsof=output), self.assertRaisesRegex(ValueError, "唯一的桌面 DSH"):
                self.run_identity(lsof=output)
            self.assertEqual(len(self.commands), 1, "身份不唯一时不得继续读取 argv")

    def test_unconfirmed_dsh_listener_is_rejected(self):
        entry = self.dsh_package(name="some-other-app", directory="other")
        cases = [
            ("非 DSH 包", "p7\n", "/usr/local/bin/node %s\n" % entry),
            ("非绝对脚本路径", "p7\n", "node lib/bin.js\n"),
            ("空 argv", "p7\n", "\n"),
        ]
        for label, lsof, ps in cases:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "不是可确认的桌面 DSH"):
                self.run_identity(lsof=lsof, ps=ps)

    def test_default_port_is_used_when_url_has_none(self):
        entry = self.dsh_package()
        self.run_identity(lsof="p9\n", ps="/usr/local/bin/node %s\n" % entry,
                          url="http://127.0.0.1")
        self.assertIn("-iTCP:80", self.commands[0])

    def test_unquoted_application_support_path_is_recognised(self):
        entry = self.dsh_package(directory="Application Support/DSH/node_modules/dsh")
        result = self.run_identity(lsof="p9\n", ps="/Library/Application Support/node %s --port 3999\n" % entry)
        self.assertEqual(result["harness_version"], "1.2.3")


if __name__ == "__main__":
    unittest.main()
