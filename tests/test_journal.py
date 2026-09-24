import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from leadseek.journal import list_recent, record


class JournalTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ws = Path(self._tmp.name) / "workspace"
        self.ws.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    @property
    def log(self):
        return self.root / ".state" / "decisions.jsonl"

    def test_record_appends_and_writes_secure_file(self):
        result = record(self.root, self.ws, "codex", "plan", "initial plan")
        self.assertEqual(result["owner"], "codex")
        self.assertEqual(result["action"], "plan")
        self.assertEqual(result["reason"], "initial plan")
        self.assertEqual(result["workspace"], os.path.realpath(str(self.ws)))
        self.assertIsNone(result["run_id"])
        self.assertTrue(self.log.exists())
        mode = stat.S_IMODE(os.stat(self.log).st_mode)
        self.assertEqual(mode, 0o600)

        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed, result)
        self.assertTrue(parsed["timestamp"].endswith("Z"))

    def test_record_stores_run_id(self):
        record(self.root, self.ws, "deepseek", "fix", "bug", run_id="run-7")
        entry = list_recent(self.root)[0]
        self.assertEqual(entry["run_id"], "run-7")

    def test_record_creates_nested_state_dir(self):
        record(self.root, self.ws, "codex", "a", "r")
        self.assertTrue((self.root / ".state").is_dir())

    def test_list_recent_newest_first(self):
        for i in range(3):
            record(self.root, self.ws, "codex", "step-%d" % i, "reason")
        actions = [e["action"] for e in list_recent(self.root)]
        self.assertEqual(actions, ["step-2", "step-1", "step-0"])

    def test_list_recent_limit_bounds(self):
        for i in range(5):
            record(self.root, self.ws, "codex", "step-%d" % i, "reason")
        self.assertEqual(len(list_recent(self.root, limit=3)), 3)
        self.assertEqual(list_recent(self.root, limit=3)[0]["action"], "step-4")
        self.assertEqual(len(list_recent(self.root, limit=100)), 5)

    def test_list_recent_filter_by_workspace(self):
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        record(self.root, self.ws, "codex", "mine", "reason")
        record(self.root, other, "deepseek", "theirs", "reason")
        result = list_recent(self.root, workspace=self.ws)
        self.assertEqual([e["action"] for e in result], ["mine"])
        self.assertEqual(len(list_recent(self.root, workspace=other)), 1)

    def test_list_recent_missing_file(self):
        self.assertEqual(list_recent(self.root), [])
        self.assertEqual(list_recent(self.root, limit=1), [])

    def test_list_recent_skips_blank_and_corrupt_lines(self):
        record(self.root, self.ws, "codex", "good", "reason")
        with open(self.log, "a", encoding="utf-8") as handle:
            handle.write("\n")
            handle.write("{not valid json\n")
            handle.write('"just a string"\n')
            handle.write("[1, 2, 3]\n")
        record(self.root, self.ws, "codex", "good2", "reason")
        result = list_recent(self.root)
        self.assertEqual([e["action"] for e in result], ["good2", "good"])

    def test_invalid_owner(self):
        for owner in ("", "Codex", "other", None, 3, [], {}):
            with self.assertRaises(ValueError):
                record(self.root, self.ws, owner, "a", "r")

    def test_empty_action_or_reason(self):
        with self.assertRaises(ValueError):
            record(self.root, self.ws, "codex", "", "r")
        with self.assertRaises(ValueError):
            record(self.root, self.ws, "codex", "a", "")
        with self.assertRaises(ValueError):
            record(self.root, self.ws, "codex", None, "r")
        with self.assertRaises(ValueError):
            record(self.root, self.ws, "codex", "a", 5)

    def test_whitespace_only_action_or_reason(self):
        for blank in (" ", "   ", "\n", "\t", " \n\t "):
            with self.assertRaises(ValueError):
                record(self.root, self.ws, "codex", blank, "r")
            with self.assertRaises(ValueError):
                record(self.root, self.ws, "codex", "a", blank)
        result = record(self.root, self.ws, "codex", "  plan  ", "  reason  ")
        self.assertEqual(result["action"], "  plan  ")
        self.assertEqual(result["reason"], "  reason  ")

    def test_reason_too_long(self):
        record(self.root, self.ws, "codex", "a", "x" * 2000)
        with self.assertRaises(ValueError):
            record(self.root, self.ws, "codex", "a", "x" * 2001)

    def test_workspace_must_be_absolute(self):
        with self.assertRaises(ValueError):
            record(self.root, "relative/path", "codex", "a", "r")

    def test_invalid_limit(self):
        for limit in (True, False, 0, 101, -1, "5", 2.5, None):
            with self.assertRaises(ValueError):
                list_recent(self.root, limit=limit)

    def test_concurrent_append_produces_intact_lines(self):
        threads = []
        count = 8
        per_thread = 10

        def worker(n):
            for i in range(per_thread):
                record(
                    self.root,
                    self.ws,
                    "deepseek",
                    "t%d-%d" % (n, i),
                    "concurrent",
                )

        for n in range(count):
            thread = threading.Thread(target=worker, args=(n,))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()

        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), count * per_thread)
        for line in lines:
            entry = json.loads(line)
            self.assertEqual(entry["owner"], "deepseek")
            self.assertEqual(entry["workspace"], os.path.realpath(str(self.ws)))
        self.assertEqual(len(list_recent(self.root, limit=100)), count * per_thread)


if __name__ == "__main__":
    unittest.main()
