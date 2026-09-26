import contextlib
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leadseek import setup


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_dsh():
    loader = SourceFileLoader("bin_dsh", str(REPO_ROOT / "bin" / "dsh"))
    spec = importlib.util.spec_from_loader("bin_dsh", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def make_root(self, name="install", credentials="~/.dsh/.credentials.yaml", with_runtime=True):
        root = Path(self.temp.name) / name
        (root / "bin").mkdir(parents=True)
        bindir = root / "fake-node"
        bindir.mkdir()
        node = bindir / "node"
        node.write_text("#!/bin/sh\n")
        node.chmod(0o755)
        npm = bindir / "npm"
        npm.write_text("#!/bin/sh\n")
        npm.chmod(0o755)
        if with_runtime:
            runtime = root / "runtime/node_modules/@deepseek-ai/dsh/lib"
            runtime.mkdir(parents=True)
            (runtime / "bin.js").write_text("// fake\n", encoding="utf-8")
            for name in setup.runtime_install.REQUIRED_PACKAGES:
                package = root / "runtime/node_modules" / name
                package.mkdir(parents=True)
                (package / "package.json").write_text(
                    json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8")
        (root / "config.json").write_text(json.dumps({
            "node": str(node),
            "credentials_path": credentials,
            "model": "deepseek-flash",
            "timeout_seconds": 600,
        }), encoding="utf-8")
        return root, node, {"PATH": str(bindir)}

    def make_runner(self, failing=None):
        calls = []

        def fake(command, **kwargs):
            calls.append((command, kwargs))
            joined = " ".join(str(part) for part in command)
            if "--version" in command:
                return subprocess.CompletedProcess(command, 0, stdout="v24.1.0\n", stderr="")
            if failing and failing in joined:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="stderr: " + failing + " 未就绪")
            return subprocess.CompletedProcess(command, 0, stdout="{}\n", stderr="")

        return calls, fake

    def names(self, calls):
        return [" ".join(str(part) for part in command) for command, _ in calls]

    def test_default_flow_writes_local_and_runs_steps_in_order(self):
        root, node, environ = self.make_root()
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, environ=environ)
        local_path = root / "config.local.json"
        local = json.loads(local_path.read_text(encoding="utf-8"))
        self.assertEqual(local["node"], str(node.resolve()))
        self.assertEqual(local["credentials_path"], str(Path.home() / ".dsh/.credentials.yaml"))
        self.assertEqual(local_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result["steps"], ["setup-runtime", "install-codex"])
        joined = self.names(calls)
        self.assertLess(next(i for i, n in enumerate(joined) if n.endswith("setup-runtime")),
                        next(i for i, n in enumerate(joined) if n.endswith("install-codex")))
        self.assertTrue(any(n.endswith("doctor") for n in joined))
        self.assertTrue(all(kwargs.get("shell") is not True for _, kwargs in calls))

    def test_repeat_setup_keeps_other_local_keys(self):
        root, node, environ = self.make_root()
        (root / "config.local.json").write_text(json.dumps({
            "node": str(node), "credentials_path": "~/.dsh/custom.yaml", "custom_key": 7,
        }), encoding="utf-8")
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            setup.configure(root=root, environ=environ, skip_runtime=True)
        local = json.loads((root / "config.local.json").read_text(encoding="utf-8"))
        self.assertEqual(local["custom_key"], 7)
        self.assertEqual(local["credentials_path"], str(Path.home() / ".dsh/custom.yaml"))
        joined = self.names(calls)
        self.assertFalse(any(n.endswith("setup-runtime") for n in joined))
        self.assertTrue(any(n.endswith("install-codex") for n in joined))

    def test_explicit_overrides_are_recorded_absolute(self):
        root, node, environ = self.make_root()
        other = root / "fake-node/second-node"
        other.write_text("#!/bin/sh\n")
        other.chmod(0o755)
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            setup.configure(root=root, node=str(other), credentials="~/.config/dsh/creds.yaml",
                            environ=environ, skip_runtime=True)
        local = json.loads((root / "config.local.json").read_text(encoding="utf-8"))
        self.assertEqual(local["node"], str(other.resolve()))
        self.assertEqual(local["credentials_path"], str(Path.home() / ".config/dsh/creds.yaml"))

    def test_relative_credentials_resolve_against_root(self):
        root, node, environ = self.make_root(credentials="state/creds.yaml")
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            setup.configure(root=root, environ=environ, skip_runtime=True)
        local = json.loads((root / "config.local.json").read_text(encoding="utf-8"))
        self.assertEqual(local["credentials_path"], str((root / "state/creds.yaml").resolve()))
        self.assertTrue(Path(local["credentials_path"]).is_absolute())

    def test_skip_runtime_does_not_install_but_links(self):
        root, node, environ = self.make_root()
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, environ=environ, skip_runtime=True)
        self.assertEqual(result["steps"], ["install-codex"])
        joined = self.names(calls)
        self.assertFalse(any("setup-runtime" in n for n in joined))

    def test_skip_runtime_missing_runtime_does_not_write_global(self):
        root, node, environ = self.make_root(with_runtime=False)
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            with self.assertRaises(ValueError) as raised:
                setup.configure(root=root, environ=environ, skip_runtime=True)
        self.assertIn("运行时", str(raised.exception))
        self.assertFalse((root / "config.local.json").exists())
        self.assertFalse(any("install-codex" in n for n in self.names(calls)))

    def test_missing_npm_does_not_write_config_or_global(self):
        root = Path(self.temp.name) / "nopkg"
        (root / "bin").mkdir(parents=True)
        bindir = root / "fake-node"
        bindir.mkdir()
        node = bindir / "node"
        node.write_text("#!/bin/sh\n")
        node.chmod(0o755)
        (root / "config.json").write_text(json.dumps({
            "node": str(node), "credentials_path": "~/.dsh/x.yaml",
        }), encoding="utf-8")
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            with self.assertRaises(ValueError):
                setup.configure(root=root, environ={"PATH": ""})
        self.assertFalse((root / "config.local.json").exists())
        self.assertFalse(any("install-codex" in n for n in self.names(calls)))

    def test_missing_node_does_not_write_any_config(self):
        root, node, environ = self.make_root()
        with self.assertRaises(ValueError):
            setup.configure(root=root, node=str(root / "absent-node"), environ=environ)
        self.assertFalse((root / "config.local.json").exists())

    def test_runtime_failure_reports_stderr_and_skips_global(self):
        root, node, environ = self.make_root()
        calls, fake = self.make_runner(failing="setup-runtime")
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            with self.assertRaises(ValueError) as raised:
                setup.configure(root=root, environ=environ)
        self.assertIn("setup-runtime", str(raised.exception))
        self.assertIn("未就绪", str(raised.exception))
        self.assertFalse(any("install-codex" in n for n in self.names(calls)))

    def test_doctor_failure_is_reported_nonzero(self):
        root, node, environ = self.make_root()
        calls, fake = self.make_runner(failing="doctor")
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, environ=environ)
        self.assertNotEqual(result["doctor_exit_code"], 0)
        self.assertIn("未就绪", result["doctor_stderr"])

    def test_root_path_with_spaces(self):
        root, node, environ = self.make_root(name="install dir with spaces")
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            setup.configure(root=root, environ=environ, skip_runtime=True)
        local = json.loads((root / "config.local.json").read_text(encoding="utf-8"))
        self.assertTrue((root / "config.local.json").is_file())
        self.assertTrue(Path(local["node"]).is_absolute())
        self.assertTrue(any(str(root) in n for n in self.names(calls)))

    def test_runtime_source_recorded_and_doctor_actual_checked(self):
        root, node, environ = self.make_root()
        calls, fake = self.make_runner()

        def writing_fake(command, **kwargs):
            text = " ".join(str(part) for part in command)
            if text.endswith("setup-runtime"):
                path = root / "config.local.json"
                data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
                data["runtime_version"] = "2.2.2"
                data["runtime_source"] = "latest"
                path.write_text(json.dumps(data), encoding="utf-8")
            if text.endswith("doctor"):
                health = {"version_matches": True, "skill_linked": True, "cli_linked": True,
                          "expected": "latest", "actual": "2.2.2", "version_policy": "latest"}
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(health), stderr="")
            return fake(command, **kwargs)

        with mock.patch.object(setup.subprocess, "run", side_effect=writing_fake):
            result = setup.configure(root=root, environ=environ)
        self.assertEqual(result["runtime_version"], "2.2.2")
        self.assertTrue(result["runtime_recorded_matches"])

    def test_main_rejects_recorded_version_mismatch(self):
        result = self.base_result()
        result["runtime_recorded_matches"] = False
        with mock.patch.object(setup, "configure", return_value=result):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(setup.main([]), 1)

    def test_missing_required_package_fails_before_global_links(self):
        root, node, environ = self.make_root()
        shutil.rmtree(root / "runtime/node_modules/@deepseek-ai/dsh-headless")
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            with self.assertRaises(ValueError) as raised:
                setup.configure(root=root, environ=environ)
        self.assertIn("关键包", str(raised.exception))
        self.assertIn("@deepseek-ai/dsh-headless", str(raised.exception))
        self.assertFalse(any("install-codex" in n for n in self.names(calls)))

    def test_symlinked_local_config_is_rejected(self):
        root, node, environ = self.make_root()
        target = root / "other.json"
        target.write_text('{"node": "keep"}', encoding="utf-8")
        (root / "config.local.json").symlink_to(target)
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            with self.assertRaises(ValueError):
                setup.configure(root=root, environ=environ, skip_runtime=True)
        self.assertEqual(target.read_text(encoding="utf-8"), '{"node": "keep"}')
        self.assertFalse(any("install-codex" in n for n in self.names(calls)))

    def test_local_config_write_is_0600_without_predictable_tmp(self):
        root, node, environ = self.make_root()
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            setup.configure(root=root, environ=environ, skip_runtime=True)
        path = root / "config.local.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        leftovers = [p.name for p in root.iterdir()
                     if p.name != "config.local.json" and p.name.startswith("config.local")]
        self.assertEqual(leftovers, [])

    def test_python_version_guard(self):
        setup.python_ok((3, 9))
        with self.assertRaises(ValueError):
            setup.python_ok((3, 8))

    def test_node_major_parsing(self):
        self.assertEqual(setup.node_major("v24.15.0"), 24)
        self.assertEqual(setup.node_major("25.1.0"), 25)
        with self.assertRaises(ValueError):
            setup.node_major("unknown")

    def base_result(self, doctor_exit_code=0):
        return {
            "root": "/tmp/root", "node": "/tmp/node", "node_version": "v24.1.0",
            "credentials_path": "/tmp/creds.yaml", "local_config": "/tmp/config.local.json",
            "local_keys": ["node"], "steps": ["install-codex"],
            "doctor": json.dumps({"version_matches": True, "skill_linked": True, "cli_linked": True,
                                  "credentials_file_exists": False}),
            "doctor_stderr": "", "doctor_exit_code": doctor_exit_code,
            "runtime_version": "0.1.7-alpha.2",
        }

    def test_main_returns_zero_on_success(self):
        with mock.patch.object(setup, "configure", return_value=self.base_result()):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.main([]), 0)

    def test_main_returns_nonzero_when_doctor_fails(self):
        result = self.base_result(doctor_exit_code=1)
        result["doctor"] = ""
        result["doctor_stderr"] = "runtime missing"
        with mock.patch.object(setup, "configure", return_value=result):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(setup.main([]), 1)

    def test_main_rejects_doctor_false_checks_even_with_zero_exit(self):
        for field in ("version_matches", "skill_linked", "cli_linked"):
            result = self.base_result()
            health = json.loads(result["doctor"])
            health[field] = False
            result["doctor"] = json.dumps(health)
            with self.subTest(field=field), mock.patch.object(setup, "configure", return_value=result):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(setup.main([]), 1)

    def test_main_rejects_unreadable_doctor_response(self):
        result = self.base_result()
        result["doctor"] = "not JSON"
        with mock.patch.object(setup, "configure", return_value=result):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(setup.main([]), 1)

    def test_main_returns_nonzero_when_configure_fails(self):
        with mock.patch.object(setup, "configure", side_effect=ValueError("没有运行时")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(setup.main([]), 1)


@unittest.skipUnless((REPO_ROOT / "bin" / "dsh").is_file(), "bin/dsh 不在当前快照中，由其他执行者维护")
class DshWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "clone with spaces"
        self.config = {"node": "/tmp/fake/node",
                       "credentials_path": str(self.root / "state/.credentials.yaml")}
        self.dsh = load_dsh()

    def test_credentials_patch_uses_configured_store(self):
        patch_path = self.dsh.write_credentials_patch(self.root, self.config)
        self.assertEqual(patch_path, self.root / ".state" / "dsh-credentials-patch.json")
        data = json.loads(patch_path.read_text(encoding="utf-8"))
        self.assertEqual(data, [{"id": "credentials",
                                 "config": {"path": self.config["credentials_path"], "watch": False}}])
        self.assertEqual(patch_path.stat().st_mode & 0o777, 0o600)

    def test_build_command_injects_patch_pointing_at_config(self):
        patch_path = self.dsh.write_credentials_patch(self.root, self.config)
        command = self.dsh.build_command(self.root, self.config, patch_path, ["web"])
        self.assertIn("--patch", command)
        self.assertEqual(command[command.index("--patch") + 1], str(patch_path))
        self.assertEqual(command[-1], "web")
        self.assertEqual(command[0], self.config["node"])

    def test_build_command_keeps_explicit_user_patch(self):
        patch_path = self.root / "auto.json"
        command = self.dsh.build_command(self.root, self.config, patch_path, ["web", "--patch", "mine.json"])
        self.assertEqual(command.count("--patch"), 1)
        self.assertIn("mine.json", command)

    def test_interactive_entry_keeps_state_local_and_telemetry_disabled(self):
        runtime = self.root / setup.configuration.RUNTIME_CLI
        runtime.parent.mkdir(parents=True)
        runtime.write_text("placeholder")
        with mock.patch.object(self.dsh, "root", self.root), \
             mock.patch.object(self.dsh, "load_config", return_value=self.config), \
             mock.patch.object(self.dsh.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(self.dsh.main(["--version"]), 0)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["DSH_HOME"], str(self.root / ".state/harness-home"))
        self.assertEqual(env["TMPDIR"], str(self.root / ".state/tmp"))
        self.assertEqual(env["DSH_TELEMETRY_DISABLED"], "1")
        self.assertEqual(env["DSH_TELEMETRY_MODE"], "DISABLED")


if __name__ == "__main__":
    unittest.main()
