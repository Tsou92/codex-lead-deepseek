"""Tests for the local observability wiring: run context, journal association,
the observe plugin patch entry, and compatibility of the task JSON contract.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leadseek import journal, runner
from leadseek.journal import effective_codex_thread_id, list_recent, record


THREAD_ID = "ABCDEF01-2345-6789-ABCD-EF0123456789"
NORMALIZED_THREAD_ID = THREAD_ID.lower()

CONFIG = {
    "timeout_seconds": 600,
    "result_max_chars": 2500,
    "credentials_path": "/unused",
    "provider": "deepseek-official",
    "model": "deepseek-flash",
    "reasoning_effort": "low",
    "max_tool_calls": 32,
    "max_search_calls": 3,
    "max_fetch_calls": 5,
    "max_subagent_starts": 0,
    "max_parallel_runs": 1,
    "max_log_bytes": 1024 * 1024,
    "node": "/usr/bin/node",
}

SUCCESSFUL_EVENTS = {
    "final_present": True,
    "turn_reason": "completed",
    "errors": [],
    "malformed_lines": 0,
    "final_text": "done",
    "final_truncated": False,
    "session_id": "session-1",
    "tool_counts": {"read": 1},
    "tool_errors": [],
    "usage": None,
}


class WriteContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name) / "run"
        self.directory.mkdir()

    def test_records_effective_codex_thread_id(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD_ID}, clear=False):
            context = runner.write_context(self.directory, CONFIG, previous_run_id="run-0")
        path = self.directory / "context.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), context)
        self.assertEqual(context["codex_thread_id"], NORMALIZED_THREAD_ID)
        self.assertEqual(context["previous_run_id"], "run-0")
        self.assertEqual(context["model"], "deepseek-flash")
        self.assertEqual(context["provider"], "deepseek-official")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_or_invalid_environment_id_is_null_and_non_fatal(self):
        for value in (None, "", "not-a-uuid", "12345"):
            environ = {} if value is None else {"CODEX_THREAD_ID": value}
            with mock.patch.dict(os.environ, environ, clear=True):
                context = runner.write_context(self.directory, CONFIG)
            self.assertIsNone(context["codex_thread_id"])

    def test_effective_codex_thread_id_pure_helper(self):
        self.assertEqual(effective_codex_thread_id({"CODEX_THREAD_ID": THREAD_ID}), NORMALIZED_THREAD_ID)
        self.assertIsNone(effective_codex_thread_id({}))
        self.assertIsNone(effective_codex_thread_id({"CODEX_THREAD_ID": "  "}))
        self.assertIsNone(effective_codex_thread_id({"CODEX_THREAD_ID": 7}))


class PatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.task = {"mode": "edit", "subagents": 1}

    def test_observe_plugin_is_inserted_with_directory_telemetry_path(self):
        patch = runner.patch_for(self.task, CONFIG, self.directory)
        inserts = [entry for entry in patch if "insert" in entry]
        self.assertEqual(len(inserts), 1)
        plugins = {entry["id"]: entry for entry in inserts[0]["insert"]}
        self.assertIn("leadseek-observe", plugins)
        observe = plugins["leadseek-observe"]
        self.assertEqual(observe["name"], str(runner.ROOT / "plugins/observe.mjs"))
        self.assertTrue(observe["required"])
        self.assertEqual(observe["config"], {"telemetryPath": str(self.directory / "telemetry.jsonl")})
        self.assertIn("leadseek-budget", plugins)
        self.assertEqual(plugins["leadseek-budget"]["config"]["receiptPath"], str(self.directory / "budget.json"))

    def test_observe_plugin_is_absent_without_a_directory(self):
        patch = runner.patch_for(self.task, CONFIG)
        self.assertFalse(any("insert" in entry for entry in patch))

    def test_sandbox_and_subagent_policy_still_present(self):
        patch = runner.patch_for(self.task, CONFIG, self.directory)
        by_id = {entry["id"]: entry for entry in patch if "id" in entry}
        self.assertEqual(by_id["sandbox-policy"]["config"]["mode"], "workspace-write")
        self.assertEqual(by_id["tool-subagent"]["config"]["backgroundMode"], "continuable")


class JournalAssociationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def test_record_attaches_effective_codex_thread_id(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD_ID}, clear=False):
            entry = record(self.root, self.workspace, "deepseek", "execute", "goal", run_id="run-1")
        self.assertEqual(entry["codex_thread_id"], NORMALIZED_THREAD_ID)
        self.assertEqual(list_recent(self.root)[0]["codex_thread_id"], NORMALIZED_THREAD_ID)

    def test_record_without_environment_id_stays_compatible(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            entry = record(self.root, self.workspace, "codex", "plan", "r")
        self.assertIsNone(entry["codex_thread_id"])


class TaskContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir()

    def test_existing_fields_accepted_without_new_keys(self):
        data = {
            "goal": "do it",
            "workspace": str(self.workspace),
            "mode": "edit",
            "read_paths": [],
            "write_paths": ["code.py"],
            "constraints": [],
            "checks": [],
            "subagents": 0,
        }
        task = runner.task_from_json(data, CONFIG)
        self.assertEqual(set(data) - set(task), set())
        self.assertEqual(task["timeout_seconds"], CONFIG["timeout_seconds"])
        self.assertEqual(task["result_max_chars"], CONFIG["result_max_chars"])
        self.assertNotIn("codex_thread_id", task)
        self.assertNotIn("telemetry", task)

    def test_unknown_task_field_rejected(self):
        with self.assertRaises(ValueError):
            runner.task_from_json({"goal": "g", "workspace": str(self.workspace), "codex_thread_id": THREAD_ID}, CONFIG)


class RunTaskContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state = self.root / ".state"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def _fake_execute(self, command, cwd, environment, prompt, directory, timeout, log_limit):
        (Path(directory) / "events.jsonl").write_text('{"type":"final"}\n', encoding="utf-8")
        (Path(directory) / "budget.json").write_text(json.dumps({"denied": 0, "accepted_total": 0}), encoding="utf-8")
        return {"exit_code": 0, "interruption": None, "elapsed_seconds": 0.0}

    def test_run_task_writes_context_and_associates_journal(self):
        data = {
            "goal": "observe me",
            "workspace": str(self.workspace),
            "mode": "edit",
            "read_paths": [],
            "write_paths": ["code.py"],
            "constraints": [],
            "checks": [],
            "subagents": 0,
        }
        with mock.patch.object(runner, "STATE", self.state), \
                mock.patch.object(runner, "ROOT", self.root), \
                mock.patch.object(runner, "load_config", return_value=CONFIG), \
                mock.patch.object(runner, "runtime_command", return_value=["true"]), \
                mock.patch.object(runner, "prepare"), \
                mock.patch.object(runner, "reduce_events", return_value=dict(SUCCESSFUL_EVENTS)), \
                mock.patch.object(runner, "outcome", return_value="completed"), \
                mock.patch.object(runner, "collect_changes", return_value=([], [])), \
                mock.patch.object(runner, "execute_process", side_effect=self._fake_execute), \
                mock.patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD_ID}, clear=False):
            result = runner.run_task(data)

        self.assertEqual(result["status"], "completed")
        run_directory = self.state / "runs" / result["run_id"]
        context = json.loads((run_directory / "context.json").read_text(encoding="utf-8"))
        self.assertEqual(context["codex_thread_id"], NORMALIZED_THREAD_ID)
        self.assertIsNone(context["previous_run_id"])
        self.assertEqual(context["model"], "deepseek-flash")
        self.assertEqual(context["provider"], "deepseek-official")
        self.assertTrue((run_directory / "telemetry.jsonl").parent.is_dir())

        entries = list_recent(self.root)
        self.assertTrue(entries)
        self.assertTrue(all(entry["codex_thread_id"] == NORMALIZED_THREAD_ID for entry in entries))
        self.assertTrue(all(entry["run_id"] == result["run_id"] for entry in entries))


if __name__ == "__main__":
    unittest.main()
