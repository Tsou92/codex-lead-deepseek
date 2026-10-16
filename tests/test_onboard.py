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


DEFAULT_CREDENTIALS_PATH = "~/.dsh/.credentials.yaml"


def make_source(root, execution_backend=None, desktop_url=None,
                credentials_path=DEFAULT_CREDENTIALS_PATH):
    """Build a fake checkout; optional keys are only added when requested.

    With every optional argument left at its default the generated
    ``config.json`` is byte-for-byte the legacy one, so old fixtures keep the
    headless behaviour.
    """
    root = Path(root)
    (root / "src" / "leadseek").mkdir(parents=True)
    (root / "src" / "leadseek" / "__init__.py").write_text('__version__ = "1.1.0"\n', encoding="utf-8")
    (root / "src" / "leadseek" / "onboard.py").write_text("x = 1\n", encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin" / "setup").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "bin" / "leadseek").write_text("#!/bin/sh\n", encoding="utf-8")
    config = {"node": "auto"}
    if credentials_path is not None:
        config["credentials_path"] = credentials_path
    if execution_backend is not None:
        config["execution_backend"] = execution_backend
    if desktop_url is not None:
        config["desktop_url"] = desktop_url
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
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
        self.assertIn("已停止后续凭据与 activate 步骤", result["setup_error"])
        self.assertEqual(result["setup_error"],
                         "setup 未通过（退出码 3），已停止后续凭据与 activate 步骤；"
                         "输出已省略以免泄露本机信息。")

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


class DesktopOnboardTests(unittest.TestCase):
    """Desktop backend: no credential flow, no auto Node, legacy path untouched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = make_source(Path(self.tmp.name) / "desktop source",
                                  execution_backend="desktop",
                                  desktop_url="http://127.0.0.1:3080")

    def _dest(self, name="Codex-Lead-DeepSeek"):
        return Path(self.tmp.name) / "Applications" / name

    def _run(self, **kwargs):
        runner = kwargs.pop("runner", FakeRunner())
        destination = kwargs.pop("destination", self._dest())
        result = onboard.onboard(source=self.source, destination=destination,
                                 runner=runner, **kwargs)
        return result, runner

    def _home_tree(self):
        return sorted(path.relative_to(self.home).as_posix() for path in self.home.rglob("*"))

    def test_desktop_skips_credentials_and_never_prompts(self):
        prompter = mock.Mock(side_effect=AssertionError("must not prompt"))
        result, runner = self._run(prompter=prompter, stdin=FakeTty())
        prompter.assert_not_called()
        self.assertEqual(result["execution_backend"], "desktop")
        self.assertEqual(result["credentials"], "managed-by-desktop")
        self.assertIsNone(result["credentials_path"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["steps"], ["setup", "activate"])
        self.assertEqual(len(runner.calls), 2)
        self.assertFalse((self.home / ".dsh").exists())
        self.assertEqual(self._home_tree(), [])
        self.assertIn("桌面模式凭据与模型沿用桌面 DSH 的现有配置：本向导未读取、未创建、未修改任何认证材料。",
                      result["warnings"])

    def test_desktop_keeps_existing_credentials_untouched(self):
        credentials = self.home / ".dsh" / ".credentials.yaml"
        credentials.parent.mkdir(parents=True)
        credentials.write_text('{"version":1,"refs":{"DEEPSEEK_API_KEY":"old"}}\n', encoding="utf-8")
        os.chmod(credentials, 0o600)
        before_tree = self._home_tree()
        before_bytes = credentials.read_bytes()
        prompter = mock.Mock(side_effect=AssertionError("must not prompt"))
        result, _ = self._run(prompter=prompter, stdin=FakeTty())
        prompter.assert_not_called()
        self.assertEqual(result["credentials"], "managed-by-desktop")
        self.assertEqual(credentials.read_bytes(), before_bytes)
        self.assertEqual(stat.S_IMODE(credentials.stat().st_mode), 0o600)
        self.assertEqual(self._home_tree(), before_tree)

    def test_desktop_does_not_forward_portable_node(self):
        result, runner = self._run()
        self.assertTrue(result["ok"])
        self.assertEqual(len(runner.calls), 2)
        setup_command = runner.calls[0]
        self.assertTrue(any(part.endswith("bin/setup") for part in setup_command))
        self.assertNotIn("--node", setup_command)
        self.assertNotIn(str(self._dest() / ".portable" / "node"), setup_command)
        self.assertIn("activate", runner.calls[1])
        # The portable tree is still copied; desktop mode just never selects it.
        self.assertTrue((self._dest() / ".portable" / "node" / "bin" / "node").is_file())

    def test_desktop_forwards_explicit_node(self):
        result, runner = self._run(node="/explicit/node")
        self.assertTrue(result["ok"])
        setup_command = runner.calls[0]
        self.assertEqual(setup_command[setup_command.index("--node") + 1], "/explicit/node")

    def test_desktop_forwards_skip_runtime(self):
        _, runner = self._run(skip_runtime=True)
        self.assertIn("--skip-runtime", runner.calls[0])

    def test_desktop_setup_failure_stops_before_activate(self):
        runner = FakeRunner(setup_code=3)
        prompter = mock.Mock(side_effect=AssertionError("must not prompt"))
        result, _ = self._run(runner=runner, prompter=prompter, stdin=FakeTty())
        prompter.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["steps"], ["setup"])
        self.assertNotIn("activate", result["steps"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(result["execution_backend"], "desktop")
        # The desktop credential keys are set before setup runs, so an early
        # return must not leave them missing.
        self.assertEqual(result["credentials"], "managed-by-desktop")
        self.assertIsNone(result["credentials_path"])
        self.assertFalse((self.home / ".dsh" / ".credentials.yaml").exists())
        self.assertIn("已停止后续 activate 步骤", result["setup_error"])
        self.assertNotIn("凭据", result["setup_error"])
        self.assertEqual(result["setup_error"],
                         "setup 未通过（退出码 3），已停止后续 activate 步骤；"
                         "输出已省略以免泄露本机信息。")
        text = "\n".join(onboard.summary_lines(result, home=self.home))
        self.assertIn("沿用桌面 DSH 配置", text)
        self.assertNotIn("待配置", text)
        self.assertNotIn("尚未配置 DeepSeek API key", text)
        self.assertNotIn("已新建 0600 凭据文件", text)

    def test_desktop_activate_failure_keeps_desktop_credentials(self):
        runner = FakeRunner(setup_code=0, activate_code=2)
        result, _ = self._run(runner=runner)
        self.assertFalse(result["ok"])
        self.assertEqual(result["steps"], ["setup", "activate"])
        self.assertIn("activate_error", result)
        self.assertEqual(result["execution_backend"], "desktop")
        self.assertEqual(result["credentials"], "managed-by-desktop")
        self.assertIsNone(result["credentials_path"])
        text = "\n".join(onboard.summary_lines(result, home=self.home))
        self.assertIn("沿用桌面 DSH 配置", text)
        self.assertNotIn("待配置", text)

    def test_desktop_works_without_credentials_path_key(self):
        source = make_source(Path(self.tmp.name) / "desktop without credentials path",
                             execution_backend="desktop", credentials_path=None)
        result = onboard.onboard(source=source, destination=self._dest("NoCredsPath"),
                                 runner=FakeRunner(), stdin=FakeTty())
        self.assertTrue(result["ok"])
        self.assertEqual(result["execution_backend"], "desktop")
        self.assertEqual(result["credentials"], "managed-by-desktop")
        self.assertEqual([w for w in result["warnings"] if "无法确定凭据路径" in w], [])
        self.assertEqual(self._home_tree(), [])

    def test_desktop_in_place_skips_credentials(self):
        prompter = mock.Mock(side_effect=AssertionError("must not prompt"))
        result = onboard.onboard(source=self.source, in_place=True, runner=FakeRunner(),
                                 stdin=FakeTty(), prompter=prompter)
        prompter.assert_not_called()
        self.assertEqual(result["mode"], "in-place")
        self.assertEqual(result["credentials"], "managed-by-desktop")
        self.assertIsNone(result["credentials_path"])
        self.assertEqual(self._home_tree(), [])

    def test_desktop_summary_lines_are_desktop_specific(self):
        result, _ = self._run()
        text = "\n".join(onboard.summary_lines(result, home=self.home))
        self.assertIn("桌面", text)
        self.assertIn("[2/3] 桌面接入与链接：setup 完成", text)
        self.assertIn("全局 CLI 与 Skill 链接", text)
        self.assertIn("沿用桌面 DSH 配置（未读取、未创建、未修改）", text)
        self.assertIn("凭据、模型与插件由桌面 DSH 管理，本向导未创建或修改认证材料。", text)
        self.assertIn("doctor 只确认安装结构，未验证 DeepSeek API。", text)
        self.assertNotIn("尚未配置 DeepSeek API key", text)
        self.assertNotIn("已新建 0600 凭据文件", text)

    def test_desktop_summary_lines_without_backend_key(self):
        result = {"install_root": "/tmp/install", "mode": "create", "ok": True,
                  "steps": ["setup", "activate"], "activate_exit_code": 0,
                  "credentials": "managed-by-desktop"}
        text = "\n".join(onboard.summary_lines(result, home=self.home))
        self.assertIn("桌面", text)
        self.assertNotIn("尚未配置 DeepSeek API key", text)

    def test_headless_summary_lines_unchanged(self):
        source = make_source(Path(self.tmp.name) / "headless source")
        result = onboard.onboard(source=source, destination=self._dest("Headless"),
                                 non_interactive=True, runner=FakeRunner())
        text = "\n".join(onboard.summary_lines(result, home=self.home))
        self.assertIn("[2/3] 运行时与接入：setup 完成（含 doctor 自检）", text)
        self.assertIn("待配置（非交互，未读取密钥）", text)
        self.assertIn("尚未配置 DeepSeek API key，委派功能未就绪", text)
        self.assertNotIn("桌面", text)

    def test_desktop_never_calls_credential_helpers(self):
        with mock.patch("leadseek.onboard.credentials_path_for",
                        side_effect=AssertionError("must not resolve credentials path")), \
                mock.patch("leadseek.onboard._isatty",
                           side_effect=AssertionError("must not inspect the terminal")), \
                mock.patch("leadseek.onboard.write_credentials",
                           side_effect=AssertionError("must not write credentials")):
            result, _ = self._run(stdin=FakeTty())
        self.assertTrue(result["ok"])
        self.assertEqual(result["credentials"], "managed-by-desktop")

    def test_desktop_never_auto_selects_portable_node(self):
        with mock.patch("leadseek.onboard._portable_node",
                        side_effect=AssertionError("must not auto-select node")):
            result, runner = self._run()
        self.assertTrue(result["ok"])
        self.assertNotIn("--node", runner.calls[0])

    def test_legacy_config_without_backend_still_prompts_and_creates(self):
        source = make_source(Path(self.tmp.name) / "legacy source")
        prompter = mock.Mock(return_value="sk-SECRET-VALUE")
        result = onboard.onboard(source=source, destination=self._dest("Legacy"),
                                 runner=FakeRunner(), stdin=FakeTty(), prompter=prompter)
        self.assertEqual(result["execution_backend"], "headless")
        prompter.assert_called_once_with()
        self.assertEqual(result["credentials"], "created")
        credentials = Path(result["credentials_path"])
        self.assertEqual(credentials.resolve(),
                         (self.home / ".dsh" / ".credentials.yaml").resolve())
        self.assertIn("sk-SECRET-VALUE", credentials.read_text(encoding="utf-8"))
        self.assertNotIn("sk-SECRET-VALUE", json.dumps(result, ensure_ascii=False))

    def test_resolve_backend_normalizes_values(self):
        cases = [
            (None, "headless"),
            ({}, "headless"),
            ({"execution_backend": None}, "headless"),
            ({"execution_backend": "headless"}, "headless"),
            ({"execution_backend": "desktop"}, "desktop"),
            ({"execution_backend": "DESKTOP"}, "desktop"),
            ({"execution_backend": " Desktop "}, "desktop"),
            ("desktop", "headless"),
        ]
        for config, expected in cases:
            with self.subTest(config=config):
                self.assertEqual(onboard.resolve_backend(config), expected)
        for value in ("", "   ", "unknown", 1, ["desktop"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                onboard.resolve_backend({"execution_backend": value})

    def test_effective_config_merges_local_without_resolving_node(self):
        root = Path(self.tmp.name) / "config root"
        root.mkdir(parents=True)
        (root / "config.json").write_text(
            json.dumps({"node": "auto", "execution_backend": "desktop",
                        "desktop_url": "http://127.0.0.1:3080"}), encoding="utf-8")
        (root / "config.local.json").write_text(
            json.dumps({"desktop_url": "http://127.0.0.1:3999", "runtime_version": "1.0.0"}),
            encoding="utf-8")
        merged = onboard.effective_config(root)
        self.assertEqual(merged["execution_backend"], "desktop")
        self.assertEqual(merged["desktop_url"], "http://127.0.0.1:3999")
        self.assertEqual(merged["node"], "auto")
        self.assertEqual(merged["runtime_version"], "1.0.0")
        # No credentials_path anywhere: the helper raises exactly as before.
        with self.assertRaises(ValueError):
            onboard.credentials_path_for(root)

    def test_credentials_path_for_reads_local_override(self):
        root = Path(self.tmp.name) / "override root"
        root.mkdir(parents=True)
        (root / "config.json").write_text(
            json.dumps({"node": "auto", "credentials_path": "~/.dsh/.credentials.yaml"}),
            encoding="utf-8")
        (root / "config.local.json").write_text(
            json.dumps({"credentials_path": "state/local.yaml"}), encoding="utf-8")
        self.assertEqual(onboard.credentials_path_for(root),
                         str((root / "state" / "local.yaml").resolve()))

    def test_missing_config_stops_without_fallback_or_credentials(self):
        source = make_source(Path(self.tmp.name) / "no config source",
                             execution_backend="desktop")
        (source / "config.json").unlink()
        runner = FakeRunner()
        result = onboard.onboard(source=source, destination=self._dest("BrokenConfig"),
                                 non_interactive=True, runner=runner)
        self.assertFalse(result["ok"])
        self.assertEqual(result["steps"], [])
        self.assertIn("无法确定执行后端", result["setup_error"])
        self.assertFalse((self.home / ".dsh" / ".credentials.yaml").exists())


class MakeSourceTests(unittest.TestCase):
    """The default fixture must stay byte-identical to the legacy config.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_default_config_is_legacy_bytes(self):
        root = make_source(Path(self.tmp.name) / "legacy")
        self.assertEqual((root / "config.json").read_text(encoding="utf-8"),
                         '{"node": "auto", "credentials_path": "~/.dsh/.credentials.yaml"}')

    def test_optional_keys_are_added_only_when_requested(self):
        root = make_source(Path(self.tmp.name) / "desktop", execution_backend="desktop",
                           desktop_url="http://127.0.0.1:3080")
        self.assertEqual(json.loads((root / "config.json").read_text(encoding="utf-8")),
                         {"node": "auto", "credentials_path": "~/.dsh/.credentials.yaml",
                          "execution_backend": "desktop",
                          "desktop_url": "http://127.0.0.1:3080"})
        root = make_source(Path(self.tmp.name) / "no-creds", credentials_path=None)
        self.assertEqual(json.loads((root / "config.json").read_text(encoding="utf-8")),
                         {"node": "auto"})


if __name__ == "__main__":
    unittest.main()
