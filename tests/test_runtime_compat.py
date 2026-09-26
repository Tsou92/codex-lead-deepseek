import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from leadseek import runtime_compat as rc  # noqa: E402
from leadseek.events import reduce_events  # noqa: E402
from leadseek.runner import outcome  # noqa: E402


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ProbeTests(unittest.TestCase):
    def setUp(self):
        rc.clear_probe_cache()

    def test_modern_help_routes_to_current_json_path(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return FakeCompleted(stdout="Usage: dsh --profile headless [options]\n  --json  stream JSONL")

        mode = rc.probe_headless_mode(["node", "cli.js"], Path("/tmp"), {}, run=run)
        self.assertEqual(mode, rc.MODERN)
        self.assertEqual(calls[0], ["node", "cli.js", "--profile", "headless", "--help"])

    def test_legacy_help_without_json_is_detected(self):
        def run(command, **kwargs):
            return FakeCompleted(stdout="Answer one task and exit", stderr="")

        mode = rc.probe_headless_mode(["node", "cli.js"], Path("/tmp"), {}, run=run)
        self.assertEqual(mode, rc.LEGACY)

    def test_probe_is_cached_per_process(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return FakeCompleted(stdout="--json")

        rc.probe_headless_mode(["node", "cli.js"], Path("/tmp"), {}, run=run)
        rc.probe_headless_mode(["node", "cli.js"], Path("/tmp"), {}, run=run)
        self.assertEqual(len(calls), 1)

    def test_nonzero_help_is_a_clear_failure(self):
        def run(command, **kwargs):
            return FakeCompleted(returncode=2, stdout="", stderr="unknown option")

        with self.assertRaises(rc.CompatibilityError) as caught:
            rc.probe_headless_mode(["node", "cli.js"], Path("/tmp"), {}, run=run)
        self.assertIn("--help", str(caught.exception))

    def test_timeout_is_a_clear_failure(self):
        def run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 15)

        with self.assertRaises(rc.CompatibilityError) as caught:
            rc.probe_headless_mode(["node", "cli.js"], Path("/tmp"), {}, run=run)
        self.assertIn("没有响应", str(caught.exception))

    def test_empty_help_is_not_silently_classified(self):
        def run(command, **kwargs):
            return FakeCompleted(stdout="", stderr="")

        with self.assertRaises(rc.CompatibilityError):
            rc.probe_headless_mode(["node", "cli.js"], Path("/tmp"), {}, run=run)


class LegacyPatchTests(unittest.TestCase):
    def test_patch_disables_startup_and_carries_prompt_out_of_argv(self):
        prompt = "very secret prompt\nwith a --json lookalike"
        patch = rc.legacy_patch(ROOT, prompt, Path("/run"))
        by_id = {row.get("id"): row for row in patch}
        self.assertTrue(by_id["headless-startup"]["disabled"])
        runner = by_id["headless-runner"]
        # The runner must wait for the compat sink before it captures stdout.
        self.assertEqual(runner["inject"], [rc.LEGACY_READY_SERVICE])
        self.assertNotIn("headlessStartup", runner["inject"])
        self.assertEqual(runner["config"]["task"], prompt)
        inserts = [row["insert"] for row in patch if "insert" in row]
        self.assertEqual(len(inserts), 1)
        entry = inserts[0][0]
        self.assertEqual(entry["id"], "leadseek-legacy-headless")
        self.assertTrue(entry["required"])
        self.assertIn("plugins/legacy-headless.mjs", entry["name"])

    def test_legacy_arguments_omit_json_and_prompt(self):
        prompt = "do not leak me"
        arguments = rc.headless_arguments(rc.LEGACY, "/run/patch.json")
        self.assertNotIn("--json", arguments)
        self.assertNotIn(prompt, arguments)
        self.assertEqual(arguments, ["--profile", "headless", "--patch", "/run/patch.json"])
        self.assertEqual(rc.stdin_payload(rc.LEGACY, prompt), "")

    def test_modern_arguments_keep_json_and_stdin_prompt(self):
        arguments = rc.headless_arguments(rc.MODERN, "/run/patch.json")
        self.assertIn("--json", arguments)
        self.assertEqual(rc.stdin_payload(rc.MODERN, "task"), "task")


class ReducerProtocolTests(unittest.TestCase):
    def _reduce(self, events):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            return reduce_events(path)

    def test_projected_legacy_jsonl_satisfies_reducer_and_outcome(self):
        events = self._reduce([
            {"type": "session", "sessionId": "root"},
            {"type": "status", "phase": "turn_end", "reason": {"kind": "completed"}},
            {"type": "status", "phase": "step_end", "usage": {"inputTokens": 3, "outputTokens": 4}},
            {"type": "tool_call", "tool": "read"},
            {"type": "tool_result", "status": "error"},
            {"type": "final", "text": "done"},
        ])
        self.assertEqual(events["session_id"], "root")
        self.assertTrue(events["final_present"])
        self.assertEqual(events["turn_reason"], "completed")
        self.assertEqual(events["usage"], {"inputTokens": 3, "outputTokens": 4})
        self.assertEqual(events["tool_counts"], {"read": 1})
        self.assertEqual(events["tool_errors"], 1)
        self.assertEqual(outcome(events, {"exit_code": 0, "interruption": None}), "completed")

    def test_missing_final_or_failed_turn_is_not_faked_as_completed(self):
        missing_final = self._reduce([
            {"type": "session", "sessionId": "root"},
            {"type": "status", "phase": "turn_end", "reason": {"kind": "completed"}},
        ])
        self.assertEqual(outcome(missing_final, {"exit_code": 0, "interruption": None}), "failed")
        failed_turn = self._reduce([
            {"type": "session", "sessionId": "root"},
            {"type": "status", "phase": "turn_end", "reason": {"kind": "error"}},
            {"type": "final", "text": "partial"},
        ])
        self.assertEqual(outcome(failed_turn, {"exit_code": 0, "interruption": None}), "failed")
        nonzero = self._reduce([
            {"type": "session", "sessionId": "root"},
            {"type": "status", "phase": "turn_end", "reason": {"kind": "completed"}},
            {"type": "final", "text": "done"},
        ])
        self.assertEqual(outcome(nonzero, {"exit_code": 1, "interruption": None}), "failed")


if __name__ == "__main__":
    unittest.main()
