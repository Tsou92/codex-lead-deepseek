"""Tests for events.reduce_events against the DSH --json JSONL contract."""

import json
import os
import tempfile
import unittest

from leadseek.events import reduce_events


EXPECTED_KEYS = {
    "session_id",
    "final_present",
    "final_text",
    "final_truncated",
    "turn_reason",
    "tool_counts",
    "tool_errors",
    "errors",
    "malformed_lines",
    "event_count",
    "usage",
}


class ReduceEventsTests(unittest.TestCase):
    def test_real_harness_object_turn_reason(self):
        path = self.write_events([{"type": "status", "phase": "turn_end", "reason": {"kind": "completed"}},
                                  {"type": "final", "text": "完成"}])
        self.assertEqual(reduce_events(path)["turn_reason"], "completed")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def write_text(self, text, name="events.jsonl"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def write_events(self, events, name="events.jsonl"):
        return self.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n", name=name
        )

    def assert_keys(self, result):
        self.assertEqual(set(result), EXPECTED_KEYS)

    # 1. Full summary
    def test_full_result(self):
        path = self.write_events(
            [
                {"type": "session", "sessionId": "sess-1"},
                {"type": "tool_call", "tool": "bash"},
                {"type": "tool_call", "tool": "bash"},
                {"type": "tool_call", "tool": "read"},
                {"type": "tool_result", "status": "error"},
                {"type": "tool_result", "status": "ok"},
                {"type": "error", "message": "boom"},
                {"type": "status", "phase": "turn_end", "reason": "max_tokens"},
                {"type": "final", "text": "all done"},
            ]
        )
        result = reduce_events(path)
        self.assert_keys(result)
        self.assertEqual(result["session_id"], "sess-1")
        self.assertIs(result["final_present"], True)
        self.assertEqual(result["final_text"], "all done")
        self.assertIs(result["final_truncated"], False)
        self.assertEqual(result["turn_reason"], "max_tokens")
        self.assertEqual(result["tool_counts"], {"bash": 2, "read": 1})
        self.assertEqual(result["tool_errors"], 1)
        self.assertEqual(result["errors"], ["boom"])
        self.assertEqual(result["malformed_lines"], 0)
        self.assertEqual(result["event_count"], 9)
        self.assertEqual(result["usage"], {})

    # 2. Truncation and reset
    def test_truncation_and_later_short_final_resets(self):
        long_path = self.write_events(
            [{"type": "final", "text": "abcdefghij"}], name="long.jsonl"
        )
        result = reduce_events(long_path, max_chars=5)
        self.assertEqual(result["final_text"], "abcde")
        self.assertIs(result["final_truncated"], True)

        reset_path = self.write_events(
            [{"type": "final", "text": "abcdefghij"}, {"type": "final", "text": "hi"}],
            name="reset.jsonl",
        )
        result = reduce_events(reset_path, max_chars=5)
        self.assertEqual(result["final_text"], "hi")
        self.assertIs(result["final_truncated"], False)

        short_path = self.write_events(
            [{"type": "final", "text": "hey"}], name="short.jsonl"
        )
        result = reduce_events(short_path, max_chars=5)
        self.assertEqual(result["final_text"], "hey")
        self.assertIs(result["final_truncated"], False)

    def test_truncation_default_max_chars(self):
        path = self.write_events([{"type": "final", "text": "x" * 3000}])
        result = reduce_events(path)
        self.assertEqual(len(result["final_text"]), 2500)
        self.assertEqual(result["final_text"], "x" * 2500)
        self.assertIs(result["final_truncated"], True)

    # 3. Empty file defaults
    def test_empty_file_defaults(self):
        path = self.write_text("")
        result = reduce_events(path)
        self.assert_keys(result)
        self.assertIsNone(result["session_id"])
        self.assertIs(result["final_present"], False)
        self.assertEqual(result["final_text"], "")
        self.assertIs(result["final_truncated"], False)
        self.assertIsNone(result["turn_reason"])
        self.assertEqual(result["tool_counts"], {})
        self.assertEqual(result["tool_errors"], 0)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["malformed_lines"], 0)
        self.assertEqual(result["event_count"], 0)
        self.assertEqual(result["usage"], {})

    # 4. A text event must not impersonate a final
    def test_text_event_is_not_a_final(self):
        path = self.write_events(
            [
                {"type": "thinking", "text": "considering"},
                {"type": "text", "text": "partial output"},
            ]
        )
        result = reduce_events(path)
        self.assertIs(result["final_present"], False)
        self.assertEqual(result["final_text"], "")
        self.assertEqual(result["event_count"], 2)

    # 5. Failed turn reason
    def test_failed_turn_reason(self):
        path = self.write_events(
            [{"type": "status", "phase": "turn_end", "reason": "error"}]
        )
        result = reduce_events(path)
        self.assertEqual(result["turn_reason"], "error")
        self.assertEqual(result["event_count"], 1)

    # 6. Malformed lines and blank lines
    def test_malformed_and_blank_lines(self):
        path = self.write_text(
            "\n".join(
                [
                    '{"type": "session", "sessionId": "s1"}',
                    "not json at all",
                    "[1, 2, 3]",
                    "",
                    "   ",
                    "null",
                    '"just a string"',
                    '{"type": "final", "text": "ok"}',
                ]
            )
            + "\n"
        )
        result = reduce_events(path)
        self.assert_keys(result)
        self.assertEqual(result["malformed_lines"], 4)
        self.assertEqual(result["event_count"], 2)
        self.assertEqual(result["session_id"], "s1")
        self.assertEqual(result["final_text"], "ok")

    # 7. Usage summing
    def test_usage_summing_ignores_bool_and_non_numeric(self):
        path = self.write_events(
            [
                {
                    "type": "status",
                    "phase": "step_end",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
                {
                    "type": "status",
                    "phase": "step_end",
                    "usage": {
                        "input_tokens": 2,
                        "cache_read": 3.5,
                        "flag": True,
                        "other": False,
                        "label": "nope",
                        "none_value": None,
                    },
                },
                {"type": "status", "phase": "step_end", "usage": "not a dict"},
                {"type": "status", "phase": "step_end", "usage": [1, 2]},
            ]
        )
        result = reduce_events(path)
        self.assertEqual(
            result["usage"],
            {"input_tokens": 12, "output_tokens": 5, "cache_read": 3.5},
        )
        self.assertEqual(result["event_count"], 4)

    # 8. Verbose content must not leak
    def test_verbose_content_not_returned(self):
        path = self.write_events(
            [
                {"type": "thinking", "text": "secret thinking"},
                {"type": "text", "text": "secret text"},
                {"type": "input", "text": "secret input"},
                {"type": "result", "text": "secret result"},
                {"type": "unknown_kind", "raw": "secret raw"},
            ]
        )
        result = reduce_events(path)
        self.assert_keys(result)
        self.assertEqual(result["event_count"], 5)
        self.assertIs(result["final_present"], False)
        self.assertEqual(result["final_text"], "")
        for key, value in result.items():
            self.assertNotIn("secret", str(value), msg="leaked via %s" % key)

    # 9. Invalid max_chars raises ValueError even before reading
    def test_invalid_max_chars_raises_value_error(self):
        path = self.write_events([{"type": "final", "text": "ok"}])
        with self.assertRaises(ValueError):
            reduce_events(path, max_chars=0)
        with self.assertRaises(ValueError):
            reduce_events(path, max_chars=-5)

    # 10. Unknown types counted, content ignored
    def test_unknown_event_type_counted_but_ignored(self):
        path = self.write_events(
            [
                {"type": "confabulate", "text": "should be ignored"},
                {"type": "confabulate", "reason": "also ignored"},
                {"type": "final", "text": "real"},
            ]
        )
        result = reduce_events(path)
        self.assertEqual(result["event_count"], 3)
        self.assertEqual(result["final_text"], "real")
        self.assertIsNone(result["turn_reason"])
        self.assertEqual(result["errors"], [])

    # Additional: tool_result status matching
    def test_tool_error_status_matching(self):
        path = self.write_events(
            [
                {"type": "tool_result", "status": "error"},
                {"type": "tool_result", "status": "FAILED"},
                {"type": "tool_result", "status": "failure"},
                {"type": "tool_result", "status": "Fail"},
                {"type": "tool_result", "status": "ok"},
                {"type": "tool_result", "status": "success"},
                {"type": "tool_result", "status": "completed"},
                {"type": "tool_result"},
                {"type": "tool_result", "status": 500},
            ]
        )
        result = reduce_events(path)
        self.assertEqual(result["tool_errors"], 4)

    # Additional: errors cap of 5
    def test_errors_capped_at_five(self):
        path = self.write_events(
            [{"type": "error", "message": "err-%d" % i} for i in range(8)]
        )
        result = reduce_events(path)
        self.assertEqual(
            result["errors"], ["err-0", "err-1", "err-2", "err-3", "err-4"]
        )

    # Additional: error messages truncated to 300 chars
    def test_error_message_truncated_to_300(self):
        messages = ["m%d:" % i + ("x" * 400) for i in range(3)]
        path = self.write_events(
            [{"type": "error", "message": m} for m in messages]
        )
        result = reduce_events(path)
        self.assertEqual(len(result["errors"]), 3)
        for entry, original in zip(result["errors"], messages):
            self.assertEqual(len(entry), 300)
            self.assertEqual(entry, original[:300])

    def test_non_string_turn_reason_converted(self):
        path = self.write_events(
            [{"type": "status", "phase": "turn_end", "reason": 42}]
        )
        result = reduce_events(path)
        self.assertEqual(result["turn_reason"], "42")

    def test_missing_path_raises_file_not_found(self):
        missing = os.path.join(self.dir, "does-not-exist.jsonl")
        with self.assertRaises(FileNotFoundError):
            reduce_events(missing)


if __name__ == "__main__":
    unittest.main()
