import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from leadseek import distribution, onboard  # noqa: E402


def make_source(root):
    root = Path(root)
    (root / "src" / "leadseek").mkdir(parents=True)
    (root / "src" / "leadseek" / "__init__.py").write_text('__version__ = "1.1.0"\n', encoding="utf-8")
    (root / "src" / "leadseek" / "onboard.py").write_text("x = 1\n", encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin" / "setup").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "bin" / "leadseek").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "config.json").write_text(
        json.dumps({"node": "auto", "credentials_path": "~/.dsh/.credentials.yaml"}),
        encoding="utf-8")
    (root / "README.md").write_text("r\n", encoding="utf-8")
    (root / "一键安装.command").write_text("#!/bin/bash\n", encoding="utf-8")
    py = root / ".portable" / "python"
    (py / "bin").mkdir(parents=True)
    interpreter = py / "bin" / "python3"
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(interpreter, 0o755)
    os.symlink("python3", str(py / "bin" / "python3.12"))
    node = root / ".portable" / "node"
    (node / "bin").mkdir(parents=True)
    node_bin = node / "bin" / "node"
    node_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(node_bin, 0o755)
    os.symlink("node", str(node / "bin" / "npx"))
    (root / "bundled").mkdir()
    (root / "bundled" / "node-v24.21.0-darwin-arm64.tar.gz").write_bytes(b"x")
    return root


class FakeRunner:
    def __init__(self, setup_code=0, activate_code=0):
        self.setup_code = setup_code
        self.activate_code = activate_code
        self.calls = []

    def __call__(self, command, root, environ):
        command = [str(part) for part in command]
        self.calls.append(command)
        code = self.setup_code if any(part.endswith("bin/setup") for part in command) else self.activate_code
        return subprocess.CompletedProcess(command, code, stdout="{}", stderr="")


class FakeTty:
    def isatty(self):
        return True


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def test_document_shape(self):
        document = onboard.build_credentials_document("k")
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["refs"]["DEEPSEEK_API_KEY"], "k")

    def test_write_is_exclusive_and_private(self):
        path = self.home / ".dsh" / ".credentials.yaml"
        onboard.write_credentials(path, "abc")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         {"version": 1, "refs": {"DEEPSEEK_API_KEY": "abc"}})
        with self.assertRaises(FileExistsError):
            onboard.write_credentials(path, "other")

    def test_write_rejects_bad_key_and_parent_symlink(self):
        with self.assertRaises(ValueError):
            onboard.write_credentials(self.home / "empty.yaml", "")
        with self.assertRaises(ValueError):
            onboard.write_credentials(self.home / "newline.yaml", "a\nb")
        real = self.home / "real"
        real.mkdir()
        link = self.home / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(distribution.UnsafePath):
            onboard.write_credentials(link / ".dsh" / ".credentials.yaml", "abc")

    def test_write_rejects_parent_itself_symlink(self):
        real = self.home / "real-target"
        real.mkdir()
        link = self.home / "parent-link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(distribution.UnsafePath):
            onboard.write_credentials(link / "creds.yaml", "abc")
        self.assertFalse((real / "creds.yaml").exists())

    def test_prompter_rejects_newline(self):
        with mock.patch("leadseek.onboard.getpass.getpass", return_value="a\r\nb"):
            with self.assertRaises(ValueError):
                onboard.default_prompter()


class OnboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = make_source(Path(self.tmp.name) / "source with space")

    def _dest(self, name="Codex-Lead-DeepSeek"):
        return Path(self.tmp.name) / "Applications" / name

    def _run(self, **kwargs):
        runner = kwargs.pop("runner", FakeRunner())
        destination = kwargs.pop("destination", self._dest())
        result = onboard.onboard(source=self.source, destination=destination,
                                 non_interactive=True, runner=runner, **kwargs)
        return result, runner

    def test_installs_with_copied_portable_toolchain(self):
        destination = Path(self.tmp.name) / "with space" / "Codex-Lead-DeepSeek"
        result, runner = self._run(destination=destination)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "create")
        self.assertTrue((destination / distribution.MARKER_NAME).is_file())
        setup_command = runner.calls[0]
        self.assertEqual(setup_command[0], str(destination / ".portable" / "python" / "bin" / "python3"))
        self.assertEqual(setup_command[setup_command.index("--node") + 1],
                         str(destination / ".portable" / "node" / "bin" / "node"))
        activate_command = runner.calls[1]
        self.assertEqual(activate_command[0], str(destination / ".portable" / "python" / "bin" / "python3"))
        self.assertIn("activate", activate_command)
        self.assertTrue((destination / ".portable" / "python" / "bin" / "python3.12").is_symlink())
        self.assertTrue((destination / ".portable" / "node" / "bin" / "npx").is_symlink())

    def test_credentials_created_but_not_leaked(self):
        result = onboard.onboard(source=self.source, destination=self._dest(),
                                 runner=FakeRunner(), stdin=FakeTty(),
                                 prompter=mock.Mock(return_value="sk-SECRET-VALUE"))
        self.assertEqual(result["credentials"], "created")
        credentials = Path(result["credentials_path"])
        self.assertIn("sk-SECRET-VALUE", credentials.read_text(encoding="utf-8"))
        self.assertNotIn("sk-SECRET-VALUE", json.dumps(result, ensure_ascii=False))

    def test_existing_credentials_preserved(self):
        credentials = self.home / ".dsh" / ".credentials.yaml"
        credentials.parent.mkdir(parents=True)
        credentials.write_text('{"version":1,"refs":{"DEEPSEEK_API_KEY":"old"}}\n', encoding="utf-8")
        prompter = mock.Mock(side_effect=AssertionError("must not prompt"))
        result, _ = self._run(prompter=prompter, stdin=FakeTty())
        self.assertEqual(result["credentials"], "exists")
        self.assertIn("old", credentials.read_text(encoding="utf-8"))
        prompter.assert_not_called()

    def test_non_interactive_never_prompts(self):
        prompter = mock.Mock(side_effect=AssertionError("must not prompt"))
        result, _ = self._run(prompter=prompter)
        self.assertEqual(result["credentials"], "pending")
        self.assertFalse((self.home / ".dsh" / ".credentials.yaml").exists())

    def test_setup_failure_stops_before_key_and_activate(self):
        prompter = mock.Mock(side_effect=AssertionError("must not prompt"))
        runner = FakeRunner(setup_code=3)
        result, _ = self._run(runner=runner, prompter=prompter, stdin=FakeTty())
        self.assertFalse(result["ok"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(result["steps"], ["setup"])
        prompter.assert_not_called()

    def test_activate_failure_is_overall_failure(self):
        runner = FakeRunner(setup_code=0, activate_code=2)
        result, _ = self._run(runner=runner)
        self.assertFalse(result["ok"])
        self.assertIn("activate", result["steps"])

    def test_update_preserves_local_state(self):
        destination = self._dest()
        self._run(destination=destination)
        local = destination / "config.local.json"
        local.write_text('{"node":"keep"}\n', encoding="utf-8")
        state = destination / ".state" / "keep.txt"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("keep", encoding="utf-8")
        result, _ = self._run(destination=destination)
        self.assertEqual(result["mode"], "update")
        self.assertEqual(local.read_text(encoding="utf-8"), '{"node":"keep"}\n')
        self.assertEqual(state.read_text(encoding="utf-8"), "keep")

    def test_unknown_nonempty_directory_rejected(self):
        destination = self._dest("NotOurs")
        destination.mkdir(parents=True)
        (destination / "something.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            onboard.onboard(source=self.source, destination=destination,
                            non_interactive=True, runner=FakeRunner())

    def test_marker_list_is_rejected(self):
        destination = self._dest()
        destination.mkdir(parents=True)
        (destination / distribution.MARKER_NAME).write_text("[1, 2]\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            onboard.onboard(source=self.source, destination=destination,
                            non_interactive=True, runner=FakeRunner())

    def test_symlink_destination_and_parent_rejected(self):
        real = Path(self.tmp.name) / "real"
        real.mkdir()
        link = Path(self.tmp.name) / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            onboard.onboard(source=self.source, destination=link,
                            non_interactive=True, runner=FakeRunner())
        with self.assertRaises(ValueError):
            onboard.onboard(source=self.source, destination=link / "child",
                            non_interactive=True, runner=FakeRunner())

    def test_relative_destination_is_absolutized(self):
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        result, _ = self._run(destination="relative-install")
        self.assertTrue(Path(result["install_root"]).is_absolute())

    def test_absolute_destination_preserves_symlink(self):
        real = Path(self.tmp.name) / "real-dest"
        real.mkdir()
        link = Path(self.tmp.name) / "link-dest"
        link.symlink_to(real, target_is_directory=True)
        resolved = onboard._absolute_destination(str(link))
        self.assertEqual(resolved, link)
        self.assertTrue(resolved.is_symlink())

    def test_install_root_equal_source_uses_explicit_node(self):
        link = Path(self.tmp.name) / "bin" / "leadseek"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.source / "bin" / "leadseek")
        runner = FakeRunner()
        result = onboard.onboard(source=self.source, non_interactive=True, runner=runner,
                                 local_bin=link, node="/explicit/node")
        self.assertEqual(result["mode"], "in-place")
        setup_command = runner.calls[0]
        self.assertEqual(setup_command[setup_command.index("--node") + 1], "/explicit/node")
        self.assertNotIn(str(self.source / ".portable" / "node"), setup_command)

    def test_non_executable_portable_node_is_not_selected(self):
        os.chmod(self.source / ".portable" / "node" / "bin" / "node", 0o644)
        runner = FakeRunner()
        result, _ = self._run(runner=runner, node="/explicit/node")
        setup_command = runner.calls[0]
        self.assertEqual(setup_command[setup_command.index("--node") + 1], "/explicit/node")

    def test_bundled_target_symlink_rejected(self):
        dest = self._dest()
        dest.mkdir(parents=True)
        real = dest / "real-bundled"
        real.mkdir()
        (dest / "bundled").symlink_to(real, target_is_directory=True)
        with self.assertRaises(distribution.UnsafePath):
            onboard.copy_portable_runtime(self.source, dest)
        self.assertEqual(list(real.iterdir()), [])

    def test_swap_runtime_rolls_back_and_keeps_foreign_dirs(self):
        src = self.source / ".portable" / "node"
        target = Path(self.tmp.name) / "runtime-target"
        target.mkdir(parents=True)
        (target / "old.txt").write_text("old", encoding="utf-8")
        stale_old = target.parent / (target.name + ".old")
        stale_old.mkdir()
        (stale_old / "keep.txt").write_text("keep", encoding="utf-8")
        stale_new = target.parent / (target.name + ".new-0")
        stale_new.mkdir()
        (stale_new / "keep.txt").write_text("keep", encoding="utf-8")
        real_rename = os.rename
        calls = {"count": 0}

        def flaky(source, destination):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("rename failed")
            return real_rename(source, destination)

        with mock.patch("leadseek.onboard.os.rename", side_effect=flaky):
            with self.assertRaises(OSError):
                onboard._swap_runtime(src, target)
        self.assertTrue((target / "old.txt").is_file())
        self.assertEqual((stale_old / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual((stale_new / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_swap_runtime_success_leaves_no_unique_leftovers(self):
        src = self.source / ".portable" / "node"
        target = Path(self.tmp.name) / "runtime-clean"
        onboard._swap_runtime(src, target)
        self.assertTrue((target / "bin" / "node").is_file())
        leftovers = [p.name for p in target.parent.iterdir()
                     if p.name.startswith(target.name + ".new-")
                     or p.name.startswith(target.name + ".old-")]
        self.assertEqual(leftovers, [])

    def test_in_place_reuses_source(self):
        result = onboard.onboard(source=self.source, in_place=True, non_interactive=True,
                                 runner=FakeRunner())
        self.assertEqual(result["mode"], "in-place")
        self.assertEqual(Path(result["install_root"]).resolve(), self.source.resolve())

    def test_external_runtime_symlink_rejected(self):
        os.symlink("/etc/hosts", str(self.source / ".portable" / "python" / "bin" / "escape"))
        with self.assertRaises(distribution.UnsafePath):
            onboard.copy_portable_runtime(self.source, self._dest())

    def test_installed_runtime_survives_source_removal(self):
        destination = self._dest()
        onboard.copy_portable_runtime(self.source, destination)
        link = destination / ".portable" / "python" / "bin" / "python3.12"
        self.assertTrue(link.is_symlink())
        shutil.rmtree(self.source)
        self.assertTrue(link.exists())


if __name__ == "__main__":
    unittest.main()
