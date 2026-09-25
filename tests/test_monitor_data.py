"""Tests for the local monitor data layer.

All fixtures are synthetic: no real Codex session, run journal or personal data
is read.  The tests build a throwaway ``.state`` tree and a throwaway
``CODEX_HOME`` under a temporary directory.
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leadseek.monitor_data import MonitorStore, TEXT_LIMIT  # noqa: E402
from leadseek import codex_sessions as codex  # noqa: E402

R1 = "20260101-120000-aaaa1111"
R2 = "20260102-120000-bbbb2222"
R3 = "20260103-120000-cccc3333"
R4 = "20260104-120000-dddd4444"
NO_TELEMETRY_RUN = "20260105-120000-eeee5555"
UUID1 = "11111111-1111-4111-8111-111111111111"
UUID2 = "22222222-2222-4222-8222-222222222222"
UUID3 = "33333333-3333-4333-8333-333333333333"


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def dump_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        self.runs = self.root / ".state" / "runs"
        self.ws = os.path.realpath(str(Path(self._tmp.name) / "workspace"))
        os.makedirs(self.ws)
        self.codex_home = Path(self._tmp.name) / "codex-home"
        self.store = MonitorStore(self.root, self.codex_home)

    def tearDown(self):
        self._tmp.cleanup()

    def make_run(
        self,
        run_id,
        status="completed",
        goal="do the thing",
        result=None,
        context=None,
        changes=None,
        events=None,
        telemetry=None,
        applied=False,
        task_extra=None,
        workspace=None,
        prompt="staged task prompt",
        omit_result=False,
    ):
        run_dir = self.runs / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        task = {
            "goal": goal,
            "workspace": workspace or self.ws,
            "mode": "edit",
            "read_paths": [],
            "write_paths": ["src/"],
            "constraints": [],
            "checks": [],
            "subagents": 0,
        }
        if task_extra:
            task.update(task_extra)
        dump(run_dir / "task.json", task)
        dump(
            run_dir / "status.json",
            {
                "run_id": run_id,
                "status": status,
                "started_at": "2026-01-01T12:00:00Z",
                "finished_at": "2026-01-01T12:00:05Z",
                "goal": goal[:200],
            },
        )
        if not omit_result:
            base_result = {
                "run_id": run_id,
                "status": status,
                "worker_report": "报告",
                "changed_file_count": len(changes or []),
                "session_id": "sess-1",
                "tool_counts": {"read": 2},
            }
            if result:
                base_result.update(result)
            dump(run_dir / "result.json", base_result)
        if context is not None:
            dump(run_dir / "context.json", context)
        if changes is not None:
            dump(run_dir / "changes.json", changes)
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        if events is not None:
            dump_lines(run_dir / "events.jsonl", events)
        if telemetry is not None:
            dump_lines(run_dir / "telemetry.jsonl", telemetry)
        if applied:
            dump(run_dir / "applied.json", {"applied_at": "2026-01-01T13:00:00Z"})
        return run_dir

    def write_decisions(self, entries):
        dump_lines(self.root / ".state" / "decisions.jsonl", entries)

    def write_session(self, thread_id, lines, name=None):
        directory = self.codex_home / "sessions" / "2026" / "01" / "01"
        directory.mkdir(parents=True, exist_ok=True)
        filename = name or ("rollout-2026-01-01T00-00-00-%s.jsonl" % thread_id)
        dump_lines(directory / filename, lines)
        return directory / filename


class OverviewTest(Base):
    def test_completed_without_review_is_pending(self):
        self.make_run(R1)
        data = self.store.overview()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["runs"][0]["review_status"], "pending")
        self.assertEqual(data["counts"]["review_pending"], 1)
        self.assertEqual(data["counts"]["accepted"], 0)
        self.assertEqual(data["counts"]["failed"], 0)
        self.assertEqual(data["workspaces"], [self.ws])

    def test_review_actions_and_applied(self):
        self.make_run(R1)
        self.make_run(R2)
        self.make_run(R3, applied=True)
        self.write_decisions(
            [
                {"timestamp": "t1", "owner": "codex", "action": "accept", "reason": "ok", "run_id": R1},
                {"timestamp": "t2", "owner": "codex", "action": "request_revision", "reason": "redo", "run_id": R2},
            ]
        )
        data = self.store.overview()
        by_id = {run["run_id"]: run for run in data["runs"]}
        self.assertEqual(by_id[R1]["review_status"], "accepted")
        self.assertEqual(by_id[R2]["review_status"], "revision_requested")
        self.assertEqual(by_id[R3]["review_status"], "accepted")
        self.assertEqual(data["counts"]["accepted"], 2)
        self.assertEqual(data["counts"]["review_pending"], 0)

    def test_newest_decision_wins(self):
        self.make_run(R1)
        self.write_decisions(
            [
                {"timestamp": "t1", "owner": "codex", "action": "request_revision", "reason": "redo", "run_id": R1},
                {"timestamp": "t2", "owner": "codex", "action": "accept", "reason": "ok", "run_id": R1},
            ]
        )
        self.assertEqual(self.store.overview()["runs"][0]["review_status"], "accepted")

    def test_global_decision_without_run_id_is_kept(self):
        self.make_run(R1)
        self.write_decisions(
            [
                {"timestamp": "t1", "owner": "codex", "action": "plan", "reason": "integrate", "run_id": None},
            ]
        )
        data = self.store.overview()
        self.assertEqual(len(data["decisions"]), 1)
        self.assertIsNone(data["decisions"][0]["run_id"])

    def test_filters_pagination_and_global_counts(self):
        for index in range(3):
            run_id = "2026010%d-120000-aaaa000%d" % (index + 1, index + 1)
            self.make_run(run_id, goal="alpha" if index == 0 else "beta")
        data = self.store.overview(query="alpha")
        self.assertEqual([run["run_id"] for run in data["runs"]], ["20260101-120000-aaaa0001"])
        self.assertEqual(data["counts"]["total"], 1)

        page = self.store.overview(limit=2)
        self.assertEqual(len(page["runs"]), 2)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["total"], 3)
        second = self.store.overview(limit=2, offset=2)
        self.assertEqual(len(second["runs"]), 1)
        self.assertFalse(second["has_more"])

    def test_running_and_failed_counts(self):
        self.make_run(R1, status="running")
        self.make_run(R2, status="failed")
        data = self.store.overview()
        self.assertEqual(data["counts"]["running"], 1)
        self.assertEqual(data["counts"]["failed"], 1)

    def test_context_fields_surface(self):
        self.make_run(
            R1,
            context={
                "codex_thread_id": UUID1,
                "previous_run_id": R2,
                "model": "gpt-5-codex",
                "provider": "openai",
            },
        )
        run = self.store.overview()["runs"][0]
        self.assertEqual(run["codex_thread_id"], UUID1)
        self.assertEqual(run["previous_run_id"], R2)
        self.assertEqual(run["model"], "gpt-5-codex")
        self.assertEqual(run["elapsed_seconds"], 5.0)


class TelemetryTest(Base):
    def test_missing_run_usage_keeps_aggregate_partial(self):
        self.make_run(R1, telemetry=[
            {"type": "usage", "agent_id": "main", "step_id": "s1",
             "usage": {"inputTokens": 10, "outputTokens": 1, "totalTokens": 11}},
        ])
        self.make_run(R2, status="running", omit_result=True)
        usage = self.store.overview()["usage"]["deepseek"]
        self.assertEqual(usage["total_tokens"], 11)
        self.assertEqual(usage["coverage"], "partial")

    def test_usage_dedup_by_agent_and_step(self):
        self.make_run(
            R1,
            telemetry=[
                {"type": "agent_created", "agent_id": "main", "session_id": "s"},
                {"type": "usage", "agent_id": "main", "step_id": "s1", "usage": {"inputTokens": 10, "outputTokens": 1, "totalTokens": 11}},
                {"type": "usage", "agent_id": "main", "step_id": "s1", "usage": {"inputTokens": 10, "outputTokens": 1, "totalTokens": 11}},
                {"type": "usage", "agent_id": "main", "step_id": "s2", "usage": {"inputTokens": 20, "totalTokens": 20}},
                {"type": "agent_created", "agent_id": "sub", "parent_id": "main", "session_id": "s"},
                {"type": "usage", "agent_id": "sub", "step_id": "s1", "usage": {"inputTokens": 5, "totalTokens": 5}},
            ],
        )
        detail = self.store.run_detail(R1)
        self.assertEqual(detail["usage"]["input_tokens"], 35)
        self.assertEqual(detail["usage"]["total_tokens"], 36)
        self.assertEqual(detail["usage"]["coverage"], "telemetry")
        run = detail["run"]
        self.assertEqual(run["subagent_count"], 1)
        self.assertEqual(len(detail["agents"]), 2)
        subs = [a for a in detail["agents"] if a["parent_id"]]
        self.assertEqual(subs[0]["usage"]["input_tokens"], 5)

    def test_partial_subagent_coverage(self):
        self.make_run(
            R1,
            telemetry=[
                {"type": "agent_created", "agent_id": "main", "session_id": "s"},
                {"type": "usage", "agent_id": "main", "step_id": "s1", "usage": {"inputTokens": 3}},
                {"type": "agent_created", "agent_id": "sub", "parent_id": "main", "session_id": "s"},
            ],
        )
        self.assertEqual(
            self.store.run_detail(R1)["usage"]["coverage"], "telemetry_partial"
        )

    def test_parent_usage_only_for_old_runs(self):
        self.make_run(
            NO_TELEMETRY_RUN,
            result={"parent_usage_only": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}},
            events=[{"type": "final", "text": "done"}],
        )
        detail = self.store.run_detail(NO_TELEMETRY_RUN)
        self.assertEqual(detail["usage"]["coverage"], "parent_usage_only")
        self.assertEqual(detail["usage"]["input_tokens"], 7)
        self.assertEqual(detail["run"]["subagent_count"], 0)
        self.assertEqual(detail["agents"], [])
        self.assertTrue(any("telemetry" in w for w in detail["warnings"]))

    def test_unknown_usage_is_null(self):
        self.make_run(R1, omit_result=False)
        detail = self.store.run_detail(R1)
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
        ):
            self.assertIsNone(detail["usage"][field], field)
        self.assertEqual(detail["usage"]["coverage"], "none")

    def test_telemetry_preferred_over_headless_events(self):
        self.make_run(
            R1,
            telemetry=[
                {"type": "message", "agent_id": "main", "role": "assistant", "text": "from telemetry"},
            ],
            events=[
                {"type": "final", "text": "from headless"},
            ],
        )
        page = self.store.run_events(R1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["text"], "from telemetry")

    def test_hidden_telemetry_filtered(self):
        self.make_run(
            R1,
            telemetry=[
                {"type": "thinking", "agent_id": "main", "text": "secret chain"},
                {"type": "message", "agent_id": "main", "role": "system", "text": "system secret"},
                {"type": "message", "agent_id": "main", "role": "assistant", "text": "visible"},
            ],
        )
        page = self.store.run_events(R1)
        texts = [item["text"] for item in page["items"]]
        self.assertEqual(texts, ["visible"])

    def test_empty_telemetry_falls_back_to_headless(self):
        self.make_run(
            NO_TELEMETRY_RUN,
            telemetry=[],
            events=[
                {"type": "session", "sessionId": "s"},
                {"type": "final", "text": "headless result"},
                {"type": "tool_call", "tool": "read"},
            ],
        )
        page = self.store.run_events(NO_TELEMETRY_RUN)
        self.assertEqual(len(page["items"]), 3)
        self.assertIsNone(page["items"][1]["timestamp"])
        self.assertEqual(page["items"][1]["text"], "headless result")
        self.assertEqual(page["items"][2]["tool"], "read")
        self.assertTrue(any("headless" in w for w in page["warnings"]))

    def test_event_pagination_and_agent_filter(self):
        telemetry = [
            {"type": "message", "agent_id": "main", "text": "m%d" % i, "step_id": "s%d" % i}
            for i in range(5)
        ]
        telemetry.append({"type": "message", "agent_id": "sub", "parent_id": "main", "text": "sub"})
        self.make_run(R1, telemetry=telemetry)
        page = self.store.run_events(R1, limit=2)
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_cursor"], 2)
        self.assertEqual(page["total"], 6)
        tail = self.store.run_events(R1, after=4)
        self.assertEqual(len(tail["items"]), 2)
        self.assertFalse(tail["has_more"])
        only_sub = self.store.run_events(R1, agent_id="sub")
        self.assertEqual(len(only_sub["items"]), 1)
        big = self.store.run_events(R1, limit=1000)
        self.assertEqual(len(big["items"]), 6)

    def test_corrupt_and_partial_jsonl_warns(self):
        run_dir = self.make_run(R1, events=[])
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
            handle.write('{"type": "final", "text": "ok"}\n')
            handle.write('{"type": "final", "text": "half')  # no trailing newline
        page = self.store.run_events(R1)
        self.assertTrue(any("损坏或未写入完成" in w for w in page["warnings"]))
        self.assertEqual([i["text"] for i in page["items"]], ["ok"])

    def test_headless_step_usage_and_tool_count_for_running_run(self):
        self.make_run(
            R1,
            status="running",
            omit_result=True,
            events=[
                {"type": "session", "sessionId": "sess-9", "agentId": "root"},
                {"type": "status", "phase": "step_end", "usage": {"input_tokens": 10, "output_tokens": 2}},
                {"type": "status", "phase": "step_end", "usage": {"input_tokens": 4}},
                {"type": "tool_call", "tool": "read", "callId": "c1"},
                {"type": "final", "text": "partial output"},
            ],
        )
        detail = self.store.run_detail(R1)
        self.assertEqual(detail["usage"]["input_tokens"], 14)
        self.assertEqual(detail["usage"]["output_tokens"], 2)
        self.assertEqual(detail["usage"]["coverage"], "headless_step_usage")
        self.assertEqual(detail["run"]["tool_count"], 1)
        page = self.store.run_events(R1)
        tool_events = [item for item in page["items"] if item["type"] == "tool_call"]
        self.assertEqual(tool_events[0]["call_id"], "c1")
        self.assertEqual(tool_events[0]["session_id"], "sess-9")
        self.assertEqual(tool_events[0]["agent_id"], "root")

    def test_headless_text_and_skip_unknown(self):
        self.make_run(
            R1,
            omit_result=True,
            events=[
                {"type": "session", "sessionId": "s"},
                {"type": "thinking", "text": "secret thinking"},
                {"type": "text", "text": "visible delta"},
                {"type": "unknown_kind", "raw": "ignored"},
            ],
        )
        page = self.store.run_events(R1)
        self.assertEqual([item["type"] for item in page["items"]], ["session", "message"])
        self.assertEqual(page["items"][1]["text"], "visible delta")

    def test_headless_usage_not_added_to_parent_usage_only(self):
        self.make_run(
            R1,
            result={"parent_usage_only": {"input_tokens": 7, "total_tokens": 7}},
            events=[
                {"type": "status", "phase": "step_end", "usage": {"input_tokens": 100, "total_tokens": 100}},
            ],
        )
        detail = self.store.run_detail(R1)
        self.assertEqual(detail["usage"]["input_tokens"], 7)
        self.assertEqual(detail["usage"]["coverage"], "parent_usage_only")

    def test_telemetry_truncated_is_partial(self):
        self.make_run(
            R1,
            telemetry=[
                {"type": "telemetry_status", "status": "truncated", "agent_id": "main"},
                {"type": "usage", "agent_id": "main", "step_id": "s1", "usage": {"inputTokens": 5}},
            ],
        )
        detail = self.store.run_detail(R1)
        self.assertEqual(detail["usage"]["coverage"], "telemetry_partial")
        self.assertTrue(any("truncated" in warning for warning in detail["warnings"]))
        self.assertEqual(self.store.overview()["usage"]["deepseek"]["coverage"], "partial")

    def test_agent_status_is_latest_state(self):
        self.make_run(
            R1,
            telemetry=[
                {"type": "agent_created", "agent_id": "sub", "parent_id": "main"},
                {"type": "agent_status", "agent_id": "sub", "parent_id": "main", "status": "idle"},
            ],
        )
        agents = self.store.run_detail(R1)["agents"]
        self.assertEqual(agents[0]["agent_id"], "sub")
        self.assertEqual(agents[0]["status"], "idle")

    def test_agent_finished_idle_is_not_treated_as_finished(self):
        self.make_run(
            R1,
            telemetry=[
                {"type": "agent_created", "agent_id": "sub", "parent_id": "main"},
                {"type": "agent_finished", "agent_id": "sub", "parent_id": "main", "status": "idle"},
            ],
        )
        agents = self.store.run_detail(R1)["agents"]
        self.assertEqual(agents[0]["status"], "idle")

    def test_headless_subagent_evidence_yields_null_count(self):
        self.make_run(
            R1,
            omit_result=True,
            events=[
                {"type": "session", "sessionId": "s", "agentId": "root"},
                {"type": "subagent_started", "sessionId": "s"},
                {"type": "final", "text": "done"},
            ],
        )
        run = self.store.run_detail(R1)["run"]
        self.assertIsNone(run["subagent_count"])

    def test_telemetry_subagent_count_is_exact(self):
        self.make_run(
            R1,
            telemetry=[
                {"type": "agent_created", "agent_id": "main"},
                {"type": "agent_created", "agent_id": "sub", "parent_id": "main"},
            ],
        )
        self.assertEqual(self.store.run_detail(R1)["run"]["subagent_count"], 1)

    def test_run_model_falls_back_to_root_agent(self):
        self.make_run(
            R1,
            telemetry=[
                {"type": "agent_created", "agent_id": "main", "model": "gpt-5-codex"},
                {"type": "agent_created", "agent_id": "sub", "parent_id": "main", "model": "gpt-5-mini"},
            ],
        )
        self.assertEqual(self.store.run_detail(R1)["run"]["model"], "gpt-5-codex")

    def test_context_model_wins_over_agent_model(self):
        self.make_run(
            R1,
            context={"model": "gpt-5-context"},
            telemetry=[
                {"type": "agent_created", "agent_id": "main", "model": "gpt-5-codex"},
            ],
        )
        self.assertEqual(self.store.run_detail(R1)["run"]["model"], "gpt-5-context")

    def test_overview_headless_step_usage_is_partial(self):
        self.make_run(
            R1,
            omit_result=True,
            events=[
                {"type": "session", "sessionId": "s", "agentId": "root"},
                {"type": "status", "phase": "step_end", "usage": {"input_tokens": 10}},
            ],
        )
        coverage = self.store.overview()["usage"]["deepseek"]["coverage"]
        self.assertEqual(coverage, "partial")

    def test_event_truncation_marker_preserved(self):
        self.make_run(
            R1,
            telemetry=[
                {"type": "message", "agent_id": "main", "role": "assistant", "text": "Z" * (TEXT_LIMIT + 5)},
            ],
        )
        item = self.store.run_events(R1)["items"][0]
        self.assertTrue(item["truncated"])
        self.assertEqual(len(item["text"]), TEXT_LIMIT)

    def test_normalise_usage_rejects_bad_numbers(self):
        usage = codex.normalise_usage(
            {
                "input_tokens": float("nan"),
                "output_tokens": float("inf"),
                "total_tokens": -5,
                "cache_read_tokens": True,
                "cache_write_tokens": 3,
            }
        )
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])
        self.assertIsNone(usage["total_tokens"])
        self.assertIsNone(usage["cache_read_tokens"])
        self.assertEqual(usage["cache_write_tokens"], 3)

    def test_usage_object_ignores_non_finite_values(self):
        self.make_run(
            R1,
            result={"parent_usage_only": {"input_tokens": float("nan"), "total_tokens": 4}},
        )
        detail = self.store.run_detail(R1)
        self.assertIsNone(detail["usage"]["input_tokens"])
        self.assertEqual(detail["usage"]["total_tokens"], 4)

    def test_legacy_subagent_tool_call_yields_null_count(self):
        self.make_run(
            R1,
            omit_result=True,
            events=[
                {"type": "session", "sessionId": "s", "agentId": "root"},
                {"type": "tool_call", "tool": "subagent"},
            ],
        )
        detail = self.store.run_detail(R1)
        self.assertIsNone(detail["run"]["subagent_count"])
        self.assertTrue(
            any("子代理" in w and "未知" in w for w in detail["warnings"])
        )

    def test_result_subagent_tool_count_without_telemetry_yields_null(self):
        self.make_run(
            R1,
            result={"tool_counts": {"subagent": 3}},
            events=[{"type": "final", "text": "done"}],
        )
        detail = self.store.run_detail(R1)
        self.assertIsNone(detail["run"]["subagent_count"])
        self.assertTrue(
            any("子代理" in w and "未知" in w for w in detail["warnings"])
        )

    def test_terminal_run_marks_unfinished_root_agent_ended(self):
        self.make_run(
            R1,
            status="completed",
            telemetry=[
                {"type": "agent_created", "agent_id": "main", "status": "startup"},
                {"type": "usage", "agent_id": "main", "step_id": "s1", "usage": {"inputTokens": 1}},
            ],
        )
        detail = self.store.run_detail(R1)
        main = [a for a in detail["agents"] if a["agent_id"] == "main"][0]
        self.assertEqual(main["status"], "ended")
        self.assertTrue(
            any("ended" in w and "审核" in w for w in detail["warnings"])
        )

    def test_terminal_run_keeps_explicit_finished_status(self):
        self.make_run(
            R1,
            status="completed",
            telemetry=[
                {"type": "agent_created", "agent_id": "main", "status": "startup"},
                {"type": "agent_finished", "agent_id": "main", "status": "finished"},
            ],
        )
        main = [a for a in self.store.run_detail(R1)["agents"] if a["agent_id"] == "main"][0]
        self.assertEqual(main["status"], "finished")

    def test_running_run_activity_updates_startup_to_running(self):
        self.make_run(
            R1,
            status="running",
            omit_result=True,
            telemetry=[
                {"type": "agent_created", "agent_id": "main", "status": "startup"},
                {"type": "message", "agent_id": "main", "role": "assistant", "text": "working"},
            ],
        )
        detail = self.store.run_detail(R1)
        main = [a for a in detail["agents"] if a["agent_id"] == "main"][0]
        self.assertEqual(main["status"], "running")
        self.assertFalse(any("ended" in w for w in detail["warnings"]))

    def test_historical_import_is_not_live_full_coverage(self):
        self.make_run(
            R1,
            telemetry=[
                {"type": "telemetry_status", "status": "historical_import", "text": "archive restore"},
                {"type": "usage", "agent_id": "main", "step_id": "s1", "usage": {"inputTokens": 5}},
            ],
        )
        detail = self.store.run_detail(R1)
        self.assertEqual(detail["usage"]["coverage"], "historical_import")
        self.assertTrue(any("historical_import" in w for w in detail["warnings"]))
        self.assertEqual(self.store.overview()["usage"]["deepseek"]["coverage"], "partial")

    def test_partial_history_reason_and_coverage(self):
        self.make_run(
            R1,
            telemetry=[
                {"type": "telemetry_status", "status": "partial", "text": "子代理 usage 缺失"},
                {"type": "usage", "agent_id": "main", "step_id": "s1", "usage": {"inputTokens": 5}},
            ],
        )
        detail = self.store.run_detail(R1)
        self.assertEqual(detail["usage"]["coverage"], "telemetry_partial")
        self.assertTrue(any("子代理 usage 缺失" in w for w in detail["warnings"]))
        self.assertEqual(self.store.overview()["usage"]["deepseek"]["coverage"], "partial")

    def test_historical_import_parent_usage_not_doubled(self):
        self.make_run(
            R1,
            result={"parent_usage_only": {"input_tokens": 7, "total_tokens": 7}},
            telemetry=[
                {"type": "telemetry_status", "status": "historical_import", "text": "archive restore"},
            ],
        )
        detail = self.store.run_detail(R1)
        self.assertEqual(detail["usage"]["input_tokens"], 7)
        self.assertEqual(detail["usage"]["total_tokens"], 7)
        self.assertEqual(detail["usage"]["coverage"], "parent_usage_only")


class CodexTest(Base):
    def _record(self, response_id, total, input_tokens=100, cache=10, output=5):
        return {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "token_usage_record",
                "response_id": response_id,
                "thread_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cache,
                    "output_tokens": output,
                    "total_tokens": total,
                },
            },
        }

    def test_new_thread_usage_snapshot_not_summed(self):
        self.write_session(
            UUID1,
            [
                {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": UUID1}},
                {"timestamp": "2026-01-01T00:00:00Z", "type": "turn_context", "payload": {"model": "gpt-5-codex"}},
                {"timestamp": "2026-01-01T00:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}},
                {"timestamp": "2026-01-01T00:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}},
                {"timestamp": "2026-01-01T00:00:03Z", "type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": "hidden"}]}},
                {"timestamp": "2026-01-01T00:00:04Z", "type": "response_item", "payload": {"type": "message", "role": "system", "content": [{"type": "input_text", "text": "system secret"}]}},
                {"timestamp": "2026-01-01T00:00:05Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "analysis", "content": [{"type": "text", "text": "hidden analysis"}]}},
                {"timestamp": "2026-01-01T00:00:06Z", "type": "response_item", "payload": {"type": "function_call", "name": "bash", "arguments": "{\"cmd\": \"ls\"}", "call_id": "c1"}},
                {"timestamp": "2026-01-01T00:00:07Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "ok"}},
                self._record("r1", 1050, 1000, 200, 50),
                self._record("r2", 2620, 2500, 500, 120),
            ],
        )
        self.store.link_codex(UUID1, self.ws)
        detail = self.store.codex_detail(UUID1)
        self.assertTrue(detail["session"]["available"])
        self.assertEqual(detail["session"]["model"], "gpt-5-codex")
        self.assertEqual(detail["session"]["usage_scope"], "thread_token_usage")
        self.assertEqual(detail["session"]["usage"]["total_tokens"], 2620)
        self.assertEqual(detail["session"]["usage"]["input_tokens"], 2500)
        self.assertEqual(detail["session"]["usage"]["cache_read_tokens"], 500)
        types = [item["type"] for item in detail["items"]]
        self.assertIn("message", types)
        self.assertIn("tool_call", types)
        self.assertIn("tool_result", types)
        joined = json.dumps(detail, ensure_ascii=False)
        self.assertNotIn("hidden", joined)
        self.assertNotIn("system secret", joined)

    def test_response_id_dedup(self):
        self.write_session(
            UUID1,
            [
                {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": UUID1}},
                self._record("r1", 100, 50),
                self._record("r1", 100, 50),
                self._record("r2", 300, 200),
            ],
        )
        self.store.link_codex(UUID1, self.ws)
        # Cumulative snapshots are not summed: the newest snapshot wins.
        summary = self.store.codex_detail(UUID1)["session"]
        self.assertEqual(summary["usage"]["total_tokens"], 300)
        self.assertEqual(summary["usage_scope"], "thread_token_usage")

    def _token_usage_record(self, response_id, turn_id=None, turn=None, usage=None, thread=None):
        payload = {"type": "token_usage_record", "response_id": response_id}
        if turn_id is not None:
            payload["turn_id"] = turn_id
        if thread is not None:
            payload["thread_token_usage"] = thread
        if turn is not None:
            payload["turn_token_usage"] = turn
        if usage is not None:
            payload["usage"] = usage
        return {"timestamp": "2026-01-01T00:00:01Z", "type": "response_item", "payload": payload}

    def _usage_session(self, thread_id, lines):
        self.write_session(
            thread_id,
            [{"type": "session_meta", "payload": {"id": thread_id}}] + lines,
        )
        self.store.link_codex(thread_id, self.ws)
        return self.store.codex_detail(thread_id)["session"]

    def test_turn_snapshot_latest_response_per_turn_not_summed(self):
        # Real evidence: inside one turn the later response carries a cumulative
        # turn snapshot (37568 then 86474), so only the newest may be counted.
        session = self._usage_session(
            UUID1,
            [
                self._token_usage_record(
                    "r1", turn_id="t1",
                    turn={"input_tokens": 30000, "output_tokens": 7568, "total_tokens": 37568},
                ),
                self._token_usage_record(
                    "r2", turn_id="t1",
                    turn={"input_tokens": 40000, "output_tokens": 8974, "total_tokens": 86474},
                ),
            ],
        )
        self.assertEqual(session["usage_scope"], "turn_token_usage")
        self.assertEqual(session["usage"]["total_tokens"], 86474)
        self.assertEqual(session["usage"]["input_tokens"], 40000)

    def test_turn_snapshots_summed_across_distinct_turns(self):
        session = self._usage_session(
            UUID1,
            [
                self._token_usage_record("r1", turn_id="t1", turn={"input_tokens": 100, "total_tokens": 120}),
                self._token_usage_record("r2", turn_id="t1", turn={"input_tokens": 150, "total_tokens": 200}),
                self._token_usage_record("r3", turn_id="t2", turn={"input_tokens": 50, "total_tokens": 60}),
            ],
        )
        self.assertEqual(session["usage_scope"], "turn_token_usage")
        self.assertEqual(session["usage"]["total_tokens"], 260)
        self.assertEqual(session["usage"]["input_tokens"], 200)

    def test_duplicate_response_id_keeps_newest_turn_snapshot(self):
        session = self._usage_session(
            UUID1,
            [
                self._token_usage_record("r1", turn_id="t1", turn={"total_tokens": 100}),
                self._token_usage_record("r1", turn_id="t1", turn={"total_tokens": 500}),
            ],
        )
        self.assertEqual(session["usage_scope"], "turn_token_usage")
        self.assertEqual(session["usage"]["total_tokens"], 500)

    def test_missing_turn_id_with_independent_usage_dedups(self):
        session = self._usage_session(
            UUID1,
            [
                self._token_usage_record("r1", usage={"input_tokens": 10, "total_tokens": 11}),
                self._token_usage_record("r1", usage={"input_tokens": 10, "total_tokens": 11}),
                self._token_usage_record("r2", usage={"input_tokens": 20, "total_tokens": 22}),
            ],
        )
        self.assertEqual(session["usage_scope"], "response_usage")
        self.assertEqual(session["usage"]["input_tokens"], 30)
        self.assertEqual(session["usage"]["total_tokens"], 33)

    def test_unattributed_turn_snapshot_not_summed(self):
        session = self._usage_session(
            UUID1,
            [
                self._token_usage_record("r1", turn={"total_tokens": 100}),
                self._token_usage_record("r2", turn={"total_tokens": 250}),
            ],
        )
        self.assertEqual(session["usage_scope"], "unattributed_turn_snapshot")
        self.assertEqual(session["usage"]["total_tokens"], 250)

    def test_thread_snapshot_preferred_over_turn_snapshots(self):
        session = self._usage_session(
            UUID1,
            [
                self._token_usage_record("r1", turn_id="t1", turn={"total_tokens": 100}),
                self._token_usage_record(
                    "r2", thread={"input_tokens": 1000, "total_tokens": 1050}
                ),
            ],
        )
        self.assertEqual(session["usage_scope"], "thread_token_usage")
        self.assertEqual(session["usage"]["total_tokens"], 1050)

    def test_legacy_token_count_format(self):
        self.write_session(
            UUID1,
            [
                {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": UUID1}},
                {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}}},
                {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 300, "output_tokens": 60, "total_tokens": 360}}}},
            ],
        )
        self.store.link_codex(UUID1, self.ws)
        summary = self.store.codex_detail(UUID1)["session"]
        self.assertEqual(summary["usage_scope"], "event_msg_context_cumulative")
        self.assertEqual(summary["usage"]["total_tokens"], 360)

    def test_unlinked_session_rejected(self):
        self.write_session(UUID1, [{"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": UUID1}}])
        with self.assertRaises(ValueError):
            self.store.codex_detail(UUID1)
        self.store.link_codex(UUID1, self.ws)
        self.assertEqual(self.store.codex_detail(UUID1)["session"]["available"], None if False else True)

    def test_session_meta_mismatch_rejected(self):
        self.write_session(UUID1, [{"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": UUID2}}])
        self.store.link_codex(UUID1, self.ws)
        detail = self.store.codex_detail(UUID1)
        self.assertFalse(detail["session"]["available"])
        self.assertEqual(detail["items"], [])
        self.assertTrue(any("不一致" in w for w in detail["warnings"]))

    def test_link_validation_and_permissions(self):
        with self.assertRaises(ValueError):
            self.store.link_codex("not-a-uuid", self.ws)
        with self.assertRaises(ValueError):
            self.store.link_codex(UUID1, "relative/path")
        record = self.store.link_codex(UUID1, self.ws)
        self.assertEqual(record["workspace"], self.ws)
        path = self.root / ".state" / "monitor" / "links.json"
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_codex_usage_not_summed_into_deepseek(self):
        self.make_run(R1, result={"parent_usage_only": {"input_tokens": 7}})
        self.write_session(
            UUID1,
            [
                {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": UUID1}},
                self._record("r1", 5000, 4000, 0, 100),
            ],
        )
        self.store.link_codex(UUID1, self.ws)
        data = self.store.overview()
        self.assertEqual(data["usage"]["deepseek"]["input_tokens"], 7)
        self.assertEqual(data["usage"]["codex"]["sessions"][0]["usage"]["total_tokens"], 5000)
        self.assertEqual(len(data["codex_sessions"]), 1)

    def test_commentary_visible_analysis_and_non_chat_hidden(self):
        self.write_session(
            UUID1,
            [
                {"type": "session_meta", "payload": {"id": UUID1}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "commentary", "content": [{"type": "text", "text": "progress note"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "analysis", "content": [{"type": "text", "text": "secret analysis"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "tool", "content": [{"type": "text", "text": "tool text"}]}},
            ],
        )
        self.store.link_codex(UUID1, self.ws)
        detail = self.store.codex_detail(UUID1)
        texts = [item.get("text") for item in detail["items"]]
        self.assertIn("progress note", texts)
        joined = json.dumps(detail, ensure_ascii=False)
        self.assertNotIn("secret analysis", joined)
        self.assertNotIn("tool text", joined)

    def test_session_without_meta_rejected(self):
        self.write_session(
            UUID1,
            [
                {"timestamp": "2026-01-01T00:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "body"}]}},
                self._record("r1", 500),
            ],
        )
        self.store.link_codex(UUID1, self.ws)
        detail = self.store.codex_detail(UUID1)
        self.assertFalse(detail["session"]["available"])
        self.assertEqual(detail["items"], [])
        self.assertIsNone(detail["session"]["usage"]["total_tokens"])
        self.assertTrue(any("session_meta" in warning for warning in detail["warnings"]))


class SecurityTest(Base):
    def test_run_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            self.store.run_detail("../outside")
        with self.assertRaises(ValueError):
            self.store.run_events("..%2f..")
        with self.assertRaises(ValueError):
            self.store.artifact(R1, "task.json")
        with self.assertRaises(ValueError):
            self.store.artifact(R1, "../task.json")

    def test_symlinked_run_dir_rejected(self):
        real = Path(self._tmp.name) / "real-run"
        real.mkdir()
        self.runs.mkdir(parents=True, exist_ok=True)
        (self.runs / R1).symlink_to(real)
        with self.assertRaises(ValueError):
            self.store.run_detail(R1)
        with self.assertRaises(ValueError):
            self.store.run_events(R1)

    def test_missing_run_rejected(self):
        with self.assertRaises(ValueError):
            self.store.run_detail(R1)

    def test_artifact_whitelist(self):
        run_dir = self.make_run(R1)
        (run_dir / "changes.patch").write_text("diff", encoding="utf-8")
        (run_dir / "stderr.log").write_text("err", encoding="utf-8")
        self.assertEqual(self.store.artifact(R1, "prompt")["text"], "staged task prompt")
        self.assertEqual(self.store.artifact(R1, "patch")["text"], "diff")
        self.assertEqual(self.store.artifact(R1, "stderr")["text"], "err")
        self.assertFalse(self.store.artifact(R1, "prompt")["truncated"])

    def test_redaction_of_credentials(self):
        self.make_run(
            R1,
            goal="use Authorization: Bearer abcdefghijklmnop and key sk-abcdefghijklmnop1234",
            task_extra={
                "api_key": "sk-abcdefghijklmnop1234",
                "password": "hunter2",
                "reasoning": "hidden chain",
            },
            result={
                "worker_report": "token=abcdef123456 secret value",
                "parent_usage_only": {"input_tokens": 1},
            },
        )
        overview = json.dumps(self.store.overview(), ensure_ascii=False)
        detail = self.store.run_detail(R1)
        detail_json = json.dumps(detail, ensure_ascii=False)
        self.assertNotIn("hunter2", overview + detail_json)
        self.assertNotIn("sk-abcdefghijklmnop1234", overview + detail_json)
        self.assertNotIn("abcdefghijklmnop", overview + detail_json)
        self.assertNotIn("hidden chain", detail_json)
        self.assertNotIn("reasoning", detail["task"])

    def test_missing_result_file_warns(self):
        self.make_run(R1, omit_result=True)
        detail = self.store.run_detail(R1)
        self.assertTrue(any("result.json" in w and "缺失" in w for w in detail["warnings"]))

    def test_artifact_redacts_quoted_and_nested_json_credentials(self):
        prompt = 'cfg={"api_key": "sk-abcdefghijklmnop1234"} nested={\\"password\\": \\"hunter2\\"}'
        self.make_run(R1, prompt=prompt)
        text = self.store.artifact(R1, "prompt")["text"]
        self.assertNotIn("sk-abcdefghijklmnop1234", text)
        self.assertNotIn("hunter2", text)
        self.assertIn("[已脱敏]", text)

    def test_artifact_symlink_rejected(self):
        run_dir = self.make_run(R1)
        outside = Path(self._tmp.name) / "outside-secret.txt"
        outside.write_text("secret", encoding="utf-8")
        (run_dir / "prompt.txt").unlink()
        (run_dir / "prompt.txt").symlink_to(outside)
        with self.assertRaises(ValueError):
            self.store.artifact(R1, "prompt")

    def test_monitor_dir_symlink_rejected(self):
        self.make_run(R1)
        real = Path(self._tmp.name) / "real-monitor"
        real.mkdir()
        (self.root / ".state" / "monitor").symlink_to(real)
        with self.assertRaises(ValueError):
            self.store.overview()
        with self.assertRaises(ValueError):
            self.store.link_codex(UUID1, self.ws)

    def test_overview_excludes_event_bodies(self):
        self.make_run(
            R1,
            telemetry=[
                {"type": "message", "agent_id": "main", "text": "X" * 5000},
            ],
        )
        overview = json.dumps(self.store.overview(), ensure_ascii=False)
        self.assertNotIn("X" * 100, overview)


if __name__ == "__main__":
    unittest.main()
