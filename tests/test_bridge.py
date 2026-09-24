import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from leadseek.runner import execute_process, outcome, task_from_json, patch_for, build_prompt
from leadseek.workspace import prepare, collect_changes, apply_changes, safe_path, carry_revision


CONFIG = {"timeout_seconds": 600, "result_max_chars": 2500,
          "credentials_path": "/unused", "provider": "deepseek-official",
          "model": "deepseek-flash", "reasoning_effort": "low"}


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.live = self.root / "live"
        self.live.mkdir()
        (self.live / "code.py").write_text("old\n")
        (self.live / "context.txt").write_text("context\n")
        self.run = self.root / "run"
        self.run.mkdir()
        self.task = task_from_json({"goal": "Edit code", "workspace": str(self.live), "mode": "edit",
                                   "read_paths": ["context.txt"], "write_paths": ["code.py", "new.py"]}, CONFIG)

    def stage(self):
        prepare(self.run, self.task)
        (self.run / "task.json").write_text(json.dumps(self.task))

    def complete(self):
        changes, violations = collect_changes(self.run, self.task)
        (self.run / "result.json").write_text(json.dumps({"status": "scope_violation" if violations else "completed"}))
        return changes, violations

    def test_staged_edit_leaves_live_unchanged_and_apply_preserves_unrelated_files(self):
        self.stage()
        (self.run / "workspace/code.py").write_text("new\n")
        self.assertEqual((self.live / "code.py").read_text(), "old\n")
        changes, violations = self.complete()
        self.assertFalse(violations)
        self.assertEqual(len(changes), 1)
        apply_changes(self.run)
        self.assertEqual((self.live / "code.py").read_text(), "new\n")
        self.assertEqual((self.live / "context.txt").read_text(), "context\n")
        with self.assertRaises(ValueError):
            apply_changes(self.run)

    def test_out_of_scope_change_blocks_apply(self):
        self.stage()
        (self.run / "workspace/context.txt").write_text("bad")
        _, violations = self.complete()
        self.assertEqual(violations, ["context.txt"])
        with self.assertRaises(ValueError):
            apply_changes(self.run)
        self.assertEqual((self.live / "context.txt").read_text(), "context\n")

    def test_concurrent_live_change_is_not_overwritten(self):
        self.stage()
        (self.run / "workspace/code.py").write_text("worker")
        self.complete()
        (self.live / "code.py").write_text("another developer")
        with self.assertRaisesRegex(ValueError, "原项目已变化"):
            apply_changes(self.run)
        self.assertEqual((self.live / "code.py").read_text(), "another developer")

    def test_changed_context_also_blocks_apply(self):
        self.stage()
        (self.run / "workspace/code.py").write_text("worker")
        self.complete()
        (self.live / "context.txt").write_text("new context")
        with self.assertRaises(ValueError):
            apply_changes(self.run)

    def test_new_target_collision_is_not_overwritten(self):
        self.stage()
        (self.run / "workspace/new.py").write_text("worker")
        self.complete()
        (self.live / "new.py").write_text("other")
        with self.assertRaises(ValueError):
            apply_changes(self.run)

    def test_modified_staged_result_rejected(self):
        self.stage()
        (self.run / "workspace/code.py").write_text("worker")
        self.complete()
        (self.run / "workspace/code.py").write_text("changed after review")
        with self.assertRaises(ValueError):
            apply_changes(self.run)

    def test_create_and_delete(self):
        self.stage()
        (self.run / "workspace/code.py").unlink()
        (self.run / "workspace/new.py").write_text("new")
        self.complete()
        apply_changes(self.run)
        self.assertFalse((self.live / "code.py").exists())
        self.assertEqual((self.live / "new.py").read_text(), "new")

    def test_explicit_symlink_and_traversal_rejected(self):
        (self.live / "linked").symlink_to(self.root, target_is_directory=True)
        for path in ("linked/file", "../file", "/etc/passwd"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_path(self.live, path)

    def test_worker_symlink_output_rejected(self):
        self.stage()
        (self.run / "workspace/new.py").symlink_to(self.live / "code.py")
        with self.assertRaises(ValueError):
            collect_changes(self.run, self.task)

    def test_directory_copy_omits_credentials(self):
        private_files = (".env", "key.pem", "config.local.json", "config.local.json.tmp")
        for name in private_files:
            (self.live / name).write_text("SECRET")
        self.task["read_paths"] = ["."]
        self.stage()
        for name in private_files:
            self.assertFalse((self.run / "workspace" / name).exists())

    def test_sensitive_output_reported_not_applied(self):
        self.stage()
        (self.run / "workspace/.env").write_text("bad")
        _, violations = self.complete()
        self.assertIn(".env", violations)

    def test_unauthorized_internal_directory_rejected(self):
        self.stage()
        (self.run / "workspace/.git").mkdir()
        with self.assertRaises(ValueError):
            collect_changes(self.run, self.task)

    def test_revision_keeps_worker_changes_without_copying_scope_violations(self):
        self.stage()
        (self.run / "workspace/code.py").write_text("first attempt")
        (self.run / "workspace/context.txt").write_text("unauthorized")
        next_run = self.root / "revision"
        next_run.mkdir()
        prepare(next_run, self.task)
        carry_revision(self.run, next_run, self.task)
        self.assertEqual((next_run / "workspace/code.py").read_text(), "first attempt")
        self.assertEqual((next_run / "workspace/context.txt").read_text(), "context\n")
        self.assertEqual((next_run / "baseline/code.py").read_text(), "old\n")

    def test_revision_refuses_changed_original(self):
        self.stage()
        (self.live / "code.py").write_text("another change")
        next_run = self.root / "revision"
        next_run.mkdir()
        prepare(next_run, self.task)
        with self.assertRaises(ValueError):
            carry_revision(self.run, next_run, self.task)

    def test_directory_to_file_replacement_rejected_before_mutation(self):
        (self.live / "a").mkdir()
        (self.live / "a/x").write_text("keep")
        self.task["write_paths"] = ["a/"]
        self.stage()
        (self.run / "workspace/a/x").unlink()
        (self.run / "workspace/a").rmdir()
        (self.run / "workspace/a").write_text("replacement")
        self.complete()
        with self.assertRaises(ValueError):
            apply_changes(self.run)
        self.assertEqual((self.live / "a/x").read_text(), "keep")


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def execute(self, code, timeout=5, limit=1024*1024, prompt="test"):
        return execute_process([sys.executable, "-c", code], self.directory, os.environ.copy(),
                               prompt, self.directory, timeout, limit)

    def test_stdin_and_raw_logs_not_returned(self):
        result = self.execute("import sys; print(sys.stdin.read()); print('raw-detail', file=sys.stderr)")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((self.directory / "events.jsonl").read_text().strip(), "test")
        self.assertNotIn("raw-detail", str(result))

    def test_timeout_even_if_child_never_reads_large_stdin(self):
        result = self.execute("import time; time.sleep(10)", timeout=0.3, prompt="x" * 300000)
        self.assertEqual(result["interruption"], "timed_out")
        self.assertLess(result["elapsed_seconds"], 4)

    def test_stdin_pipe_closed_after_success_and_timeout(self):
        real_popen = subprocess.Popen
        captured = []

        def capturing_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            captured.append(process.stdin)
            return process

        with mock.patch("leadseek.runner.subprocess.Popen", side_effect=capturing_popen):
            result = self.execute("import sys; sys.stdin.read()")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(len(captured), 1)
            self.assertIsNotNone(captured[-1])
            self.assertTrue(captured[-1].closed)

            captured.clear()
            result = self.execute("import time; time.sleep(10)", timeout=0.3, prompt="x" * 300000)
            self.assertEqual(result["interruption"], "timed_out")
            self.assertEqual(len(captured), 1)
            self.assertIsNotNone(captured[-1])
            self.assertTrue(captured[-1].closed)

    def test_log_limit_stops_process(self):
        result = self.execute("import time; print('x'*100000,flush=True); time.sleep(10)", limit=1000)
        self.assertEqual(result["interruption"], "log_limit_exceeded")

    def test_timeout_kills_process_group(self):
        result = self.execute("import subprocess,time,sys; p=subprocess.Popen([sys.executable,'-c',\"import time,pathlib; time.sleep(2); pathlib.Path('escaped').write_text('bad')\"]); time.sleep(10)", timeout=0.3)
        self.assertEqual(result["interruption"], "timed_out")
        time.sleep(2.1)
        self.assertFalse((self.directory / "escaped").exists())

    def test_final_event_cannot_hide_nonzero_exit_or_failed_turn(self):
        events = {"final_present": True, "turn_reason": "completed", "errors": [], "malformed_lines": 0, "final_text": "done"}
        process = {"exit_code": 1, "interruption": None}
        self.assertEqual(outcome(events, process), "failed")
        process["exit_code"] = 0
        events["turn_reason"] = "error"
        self.assertEqual(outcome(events, process), "failed")
        events["turn_reason"] = "completed"
        events["final_present"] = False
        self.assertEqual(outcome(events, process), "failed")

    def test_blocked_report_is_not_completed(self):
        events = {"final_present": True, "turn_reason": "completed", "errors": [], "malformed_lines": 0}
        for report in ("BLOCKED: missing dependency", "失败原因如下", "阻塞原因：缺少依赖", "状态：失败", "## 阻塞"):
            events["final_text"] = report
            with self.subTest(report=report):
                self.assertEqual(outcome(events, {"exit_code": 0, "interruption": None}), "needs_attention")


class ContractTests(unittest.TestCase):
    def test_prompt_uses_configured_budgets(self):
        task = {"goal": "test", "mode": "research", "read_paths": [], "write_paths": [], "constraints": [], "checks": [], "subagents": 0}
        prompt = build_prompt(task, {"max_tool_calls": 9, "max_search_calls": 1, "max_fetch_calls": 2})
        self.assertIn("最多 9 次工具调用、1 次 web_search、2 次 web_fetch", prompt)

    def test_no_whole_project_write_or_invalid_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            base = {"goal": "test", "workspace": directory}
            for extra in ({"mode": []}, {"mode": "edit"}, {"mode": "research", "write_paths": ["x"]}, {"mode": "research", "subagents": True}):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    task_from_json({**base, **extra}, CONFIG)

    def test_read_only_policy_and_no_alternate_subagent_routes(self):
        patch = {row["id"]: row for row in patch_for({"mode": "research", "subagents": 0}, CONFIG)}
        self.assertEqual(patch["sandbox-policy"]["config"]["mode"], "read-only")
        self.assertEqual(patch["approval"]["config"]["policy"], "never")
        self.assertTrue(patch["tool-subagent"]["disabled"])
        self.assertTrue(patch["tool-subagent-fork"]["disabled"])


if __name__ == "__main__":
    unittest.main()
