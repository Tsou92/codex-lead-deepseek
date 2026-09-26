"""Unit tests for ``leadseek activate``.

The tests never start a real monitor or browser: the monitor helper is replaced
with a mock and the ``打开监控.command`` script call is mocked.  They verify the
exact command argument array, stdin isolation, ``CODEX_THREAD_ID`` inheritance
and, most importantly, that no authenticated URL or token can leak into the
receipt or the error path, and that a failing command is never reported as a
success just because a previously running daemon still answers ``status``.
"""

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leadseek import activation  # noqa: E402


def _monitor(status_payload, start_payload=None, start_code=0):
    module = mock.Mock()
    module.status.return_value = (status_payload, 0)
    module.start.return_value = (start_payload or {"status": "running"}, start_code)
    return module


RUNNING_DIRTY = {
    "status": "running", "running": True,
    "address": "http://127.0.0.1:8765/", "port": 8765,
    "url": "http://127.0.0.1:8765/auth?token=SECRET",
}


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / activation.COMMAND_NAME
        self.script.write_text("#!/bin/bash\nexit 0\n")
        patcher = mock.patch.object(activation, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_normal_mode_runs_command_array_with_devnull_and_thread_env(self):
        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            seen.update(kwargs)
            return mock.Mock(returncode=0)

        clean = dict(RUNNING_DIRTY)
        clean["url"] = "http://127.0.0.1:8765/auth?token=SECRET"
        with mock.patch.object(activation.subprocess, "run", fake_run), \
             mock.patch.object(activation, "_monitor", return_value=_monitor(clean)), \
             mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-123"}):
            payload, code = activation.activate()

        self.assertEqual(seen["args"], ["/bin/bash", str(self.script)])
        self.assertIs(seen["stdin"], activation.subprocess.DEVNULL)
        self.assertNotIn("shell", seen)
        self.assertNotIn("capture_output", seen)
        self.assertEqual(seen["env"].get("CODEX_THREAD_ID"), "thread-123")
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["address"], "http://127.0.0.1:8765/")
        self.assertEqual(payload["port"], 8765)
        self.assertNotIn("url", payload)
        self.assertNotIn("SECRET", json.dumps(payload))

    def test_token_from_status_is_never_propagated(self):
        dirty = dict(RUNNING_DIRTY)
        dirty["url"] = "http://127.0.0.1:8765/auth?token=LEAKME"
        with mock.patch.object(activation.subprocess, "run",
                               return_value=mock.Mock(returncode=0)), \
             mock.patch.object(activation, "_monitor", return_value=_monitor(dirty)):
            payload, code = activation.activate()
        text = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(code, 0)
        self.assertNotIn("LEAKME", text)
        self.assertNotIn("/auth", text)
        self.assertNotIn("url", payload)

    def test_no_open_uses_monitor_start_without_browser(self):
        calls = {}

        def fake_start(root=None, no_open=False):
            calls["root"] = root
            calls["no_open"] = no_open
            return {"status": "running", "url": "http://127.0.0.1:8765/auth?token=LEAKME"}, 0

        clean = dict(RUNNING_DIRTY)
        clean["url"] = "http://127.0.0.1:8765/auth?token=LEAKME"
        module = _monitor(clean)
        module.start.side_effect = fake_start
        with mock.patch.object(activation, "_monitor", return_value=module), \
             mock.patch.object(activation.subprocess, "run",
                               side_effect=AssertionError("must not run script")):
            payload, code = activation.activate(root=self.root, no_open=True)

        self.assertTrue(calls["no_open"])
        self.assertEqual(Path(calls["root"]), self.root)
        self.assertEqual(code, 0)
        self.assertNotIn("LEAKME", json.dumps(payload, ensure_ascii=False))

    def test_no_open_start_failure_is_nonzero_without_token(self):
        module = _monitor({"running": False}, start_code=1)
        with mock.patch.object(activation, "_monitor", return_value=module):
            payload, code = activation.activate(root=self.root, no_open=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["status"], "error")
        self.assertNotIn("token", json.dumps(payload).lower())

    def test_no_open_start_exception_is_fixed_error_without_token(self):
        module = _monitor({"running": False})
        module.start.side_effect = RuntimeError(
            "http://127.0.0.1:8765/auth?token=NONCELEAK")
        with mock.patch.object(activation, "_monitor", return_value=module):
            payload, code = activation.activate(root=self.root, no_open=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["status"], "error")
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("NONCELEAK", text)
        self.assertNotIn("auth", text.lower())

    def test_helper_oserror_is_nonzero_and_output_is_discarded(self):
        with mock.patch.object(activation.subprocess, "run",
                               side_effect=OSError("boom http://127.0.0.1:8765/auth?token=LEAKME")), \
             mock.patch.object(activation, "_monitor", return_value=_monitor({"running": False})):
            payload, code = activation.activate(root=self.root)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["status"], "error")
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("LEAKME", text)
        self.assertNotIn("token", text.lower())
        self.assertIn("监控", text)

    def test_helper_timeout_is_nonzero_and_token_free(self):
        with mock.patch.object(
                activation.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(
                    "cmd", 1, output=b"http://127.0.0.1:8765/auth?token=LEAKME")), \
             mock.patch.object(activation, "_monitor", return_value=_monitor({"running": False})):
            payload, code = activation.activate(root=self.root)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["status"], "error")
        self.assertNotIn("LEAKME", json.dumps(payload, ensure_ascii=False))

    def test_command_failure_not_masked_by_running_daemon(self):
        # A stale daemon still answers status as running, but the command that
        # was supposed to (re)start/link this task failed: must not report 0.
        with mock.patch.object(activation.subprocess, "run",
                               return_value=mock.Mock(returncode=1)), \
             mock.patch.object(activation, "_monitor",
                               return_value=_monitor(dict(RUNNING_DIRTY))):
            payload, code = activation.activate(root=self.root)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["status"], "error")
        self.assertFalse(payload["running"])
        self.assertNotIn("SECRET", json.dumps(payload, ensure_ascii=False))

    def test_command_exception_not_masked_by_running_daemon(self):
        with mock.patch.object(activation.subprocess, "run",
                               side_effect=OSError("auth?token=LEAKME")), \
             mock.patch.object(activation, "_monitor",
                               return_value=_monitor(dict(RUNNING_DIRTY))):
            payload, code = activation.activate(root=self.root)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["status"], "error")
        self.assertNotIn("LEAKME", json.dumps(payload, ensure_ascii=False))

    def test_status_exception_becomes_fixed_error(self):
        module = _monitor(dict(RUNNING_DIRTY))
        module.status.side_effect = RuntimeError(
            "http://127.0.0.1:8765/auth?token=NONCELEAK")
        with mock.patch.object(activation.subprocess, "run",
                               return_value=mock.Mock(returncode=0)), \
             mock.patch.object(activation, "_monitor", return_value=module):
            payload, code = activation.activate(root=self.root)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["status"], "error")
        self.assertFalse(payload["running"])
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("NONCELEAK", text)
        self.assertNotIn("auth", text.lower())

    def test_missing_command_script_is_reported_without_running(self):
        self.script.unlink()
        with mock.patch.object(activation.subprocess, "run",
                               side_effect=AssertionError("must not run")):
            payload, code = activation.activate(root=self.root)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["status"], "error")


if __name__ == "__main__":
    unittest.main()
