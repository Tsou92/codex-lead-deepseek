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
        # macOS exposes the temp dir as /var -> /private/var; compare real paths.
        self.assertEqual(Path(local["node"]).resolve(), node.resolve())
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
        # macOS exposes the temp dir as /var -> /private/var; compare real paths.
        self.assertEqual(Path(local["node"]).resolve(), other.resolve())
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


DESKTOP_DOCTOR_STATUS = {
    "execution_backend": "desktop",
    "server_pid": None,
    "harness_version": "0.2.0",
    "model_selection": {"provider": "deepseek", "model": "deepseek-flash"},
    "plugin_policy": "inherit-desktop",
    "subagents": "desktop-auto",
    "api_tested_by_doctor": False,
    "skill_linked": True,
    "cli_linked": True,
    "desktop_app_installed": True,
}


class DesktopSetupTests(unittest.TestCase):
    """Desktop backend: Python + global links + doctor, and nothing else.

    Every external command is mocked, so no node/npm lookup, no bin/setup-runtime,
    no config.local.json write and no real connection to 127.0.0.1:3080 happens.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def status(self, **overrides):
        status = dict(DESKTOP_DOCTOR_STATUS)
        status.update(overrides)
        return status

    def make_root(self, name="desktop install", config=None, local=None, with_broken_runtime=False):
        root = Path(self.temp.name) / name
        (root / "bin").mkdir(parents=True)
        document = {
            "execution_backend": "desktop",
            "desktop_url": setup.DEFAULT_DESKTOP_URL,
            "credentials_path": "~/.dsh/.credentials.yaml",
            "model": "deepseek-flash",
        }
        if config:
            document.update(config)
        (root / "config.json").write_text(json.dumps(document), encoding="utf-8")
        if local is not None:
            (root / "config.local.json").write_text(json.dumps(local, indent=2), encoding="utf-8")
        if with_broken_runtime:
            # An empty node_modules would fail the headless required-package check.
            (root / "runtime/node_modules").mkdir(parents=True)
        return root

    def make_legacy_root(self):
        """Old-style fixture without execution_backend: node, npm and runtime/ present."""
        root = Path(self.temp.name) / "legacy"
        (root / "bin").mkdir(parents=True)
        bindir = root / "fake-node"
        bindir.mkdir()
        node = bindir / "node"
        node.write_text("#!/bin/sh\n")
        node.chmod(0o755)
        npm = bindir / "npm"
        npm.write_text("#!/bin/sh\n")
        npm.chmod(0o755)
        runtime = root / "runtime/node_modules/@deepseek-ai/dsh/lib"
        runtime.mkdir(parents=True)
        (runtime / "bin.js").write_text("// fake\n", encoding="utf-8")
        for package_name in setup.runtime_install.REQUIRED_PACKAGES:
            package = root / "runtime/node_modules" / package_name
            package.mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps({"name": package_name, "version": "1.0.0"}), encoding="utf-8")
        (root / "config.json").write_text(json.dumps({
            "node": str(node),
            "credentials_path": "~/.dsh/.credentials.yaml",
            "model": "deepseek-flash",
        }), encoding="utf-8")
        return root, node, {"PATH": str(bindir)}

    def make_runner(self, status=None, doctor_stdout=None, doctor_exit_code=0, doctor_stderr=""):
        calls = []
        if doctor_stdout is None:
            doctor_stdout = json.dumps(self.status() if status is None else status)

        def fake(command, **kwargs):
            calls.append((command, kwargs))
            parts = [str(part) for part in command]
            if "--version" in parts:
                return subprocess.CompletedProcess(command, 0, stdout="v24.1.0\n", stderr="")
            if parts and parts[-1] == "doctor":
                return subprocess.CompletedProcess(command, doctor_exit_code,
                                                   stdout=doctor_stdout, stderr=doctor_stderr)
            return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

        return calls, fake

    def names(self, calls):
        return [" ".join(str(part) for part in command) for command, _ in calls]

    def run_main(self, result):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(setup, "configure", return_value=result):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = setup.main([])
        return code, out.getvalue(), err.getvalue()

    def base_result(self, status=None, **overrides):
        status = self.status() if status is None else status
        result = {
            "root": "/tmp/root", "execution_backend": "desktop", "python_version": "3.9",
            "local_config": "/tmp/root/config.local.json", "local_keys": ["model"],
            "local_config_written": False, "steps": ["install-codex"],
            "doctor": json.dumps(status), "doctor_stderr": "", "doctor_exit_code": 0,
            "doctor_status": status, "notes": [setup.DESKTOP_NOTES],
            "node": None, "node_version": None, "credentials_path": None,
            # Legacy headless values are deliberately present but must never be used.
            "runtime_version": "0.1.7-alpha.2", "runtime_recorded_matches": False,
            "desktop_url": setup.DEFAULT_DESKTOP_URL,
            "connection": "connected" if status.get("server_pid") else "pending-start",
            "desktop_connected": bool(status.get("server_pid")),
            "server_pid": status.get("server_pid"),
            "harness_version": status.get("harness_version"),
            "model_selection": status.get("model_selection"),
            "plugin_policy": status.get("plugin_policy"),
            "subagents": status.get("subagents"),
            "api_tested_by_doctor": status.get("api_tested_by_doctor"),
            "desktop_app_installed": status.get("desktop_app_installed"),
        }
        result.update(overrides)
        return result

    # ---- configure: no Node/npm/runtime, no local write -------------------

    def test_desktop_needs_no_node_or_npm(self):
        root = self.make_root()
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, environ={"PATH": ""})
        names = self.names(calls)
        self.assertEqual(result["execution_backend"], "desktop")
        self.assertEqual(result["python_version"], "%d.%d" % sys.version_info[:2])
        self.assertEqual(result["steps"], ["install-codex"])
        self.assertFalse(any("--version" in name for name in names))
        self.assertFalse(any("setup-runtime" in name for name in names))
        self.assertLess(next(i for i, n in enumerate(names) if n.endswith("install-codex")),
                        next(i for i, n in enumerate(names) if n.endswith("doctor")))
        self.assertIsNone(result["node"])
        self.assertIsNone(result["node_version"])
        self.assertIsNone(result["runtime_version"])
        self.assertIsNone(result["runtime_recorded_matches"])
        self.assertTrue(all(kwargs.get("shell") is not True for _, kwargs in calls))

    def test_desktop_never_resolves_node_runtime_or_local_config(self):
        root = self.make_root(with_broken_runtime=True)
        calls, fake = self.make_runner()
        forbidden = [
            mock.patch.object(setup.configuration, "resolve_node",
                              side_effect=AssertionError("must not resolve node")),
            mock.patch.object(setup.configuration, "find_node",
                              side_effect=AssertionError("must not find node")),
            mock.patch.object(setup, "check_node",
                              side_effect=AssertionError("must not run node --version")),
            mock.patch.object(setup, "find_npm",
                              side_effect=AssertionError("must not look for npm")),
            mock.patch.object(setup.runtime_install, "missing_required_packages",
                              side_effect=AssertionError("must not check runtime packages")),
            mock.patch.object(setup, "write_local_config",
                              side_effect=AssertionError("must not write config.local.json")),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in forbidden:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(setup.subprocess, "run", side_effect=fake))
            result = setup.configure(root=root, environ={"PATH": ""})
        self.assertEqual(result["steps"], ["install-codex"])
        self.assertFalse(result["local_config_written"])

    def test_desktop_does_not_write_local_config(self):
        root = self.make_root()
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, environ={"PATH": ""})
        self.assertFalse((root / "config.local.json").exists())
        self.assertFalse(result["local_config_written"])
        self.assertEqual(result["local_keys"], [])
        self.assertEqual(result["local_config"], str(root.resolve() / "config.local.json"))

    def test_desktop_keeps_existing_local_config_byte_identical(self):
        local_document = {
            "node": "/opt/homebrew/bin/node",
            "credentials_path": "~/.dsh/custom.yaml",
            "model": "deepseek-pro",
            "plugins": ["alpha"],
            "custom_key": 7,
        }
        root = self.make_root(local=local_document)
        local_path = root / "config.local.json"
        before_bytes = local_path.read_bytes()
        before_mode = local_path.stat().st_mode & 0o777
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, environ={"PATH": ""})
        self.assertEqual(local_path.read_bytes(), before_bytes)
        self.assertEqual(local_path.stat().st_mode & 0o777, before_mode)
        self.assertFalse(result["local_config_written"])
        self.assertEqual(result["local_keys"], sorted(local_document))
        self.assertIsNone(result["node"])
        self.assertIsNone(result["credentials_path"])
        self.assertEqual([p.name for p in root.iterdir() if p.name.startswith(".leadseek-")], [])

    def test_desktop_ignores_explicit_node_and_credentials(self):
        root = self.make_root()
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, node=str(root / "absent-node"),
                                     credentials="~/.config/other/creds.yaml", environ={"PATH": ""})
        self.assertEqual(result["steps"], ["install-codex"])
        self.assertIsNone(result["node"])
        self.assertIsNone(result["credentials_path"])
        self.assertFalse((root / "config.local.json").exists())
        self.assertFalse(any("--version" in name for name in self.names(calls)))
        self.assertTrue(result["notes"])
        self.assertTrue(all(isinstance(note, str) for note in result["notes"]))
        self.assertTrue(any("忽略" in note for note in result["notes"]))

    def test_desktop_skip_runtime_is_noop_without_local_runtime(self):
        root = self.make_root()
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, environ={"PATH": ""}, skip_runtime=True)
        self.assertEqual(result["steps"], ["install-codex"])
        self.assertFalse(any("setup-runtime" in name for name in self.names(calls)))
        self.assertFalse((root / "config.local.json").exists())

    # ---- configure: doctor state -----------------------------------------

    def test_desktop_pending_start_when_desktop_not_running(self):
        status = self.status(server_pid=None)
        root = self.make_root()
        calls, fake = self.make_runner(status=status)
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, environ={"PATH": ""})
        self.assertEqual(result["doctor_status"], status)
        self.assertEqual(result["connection"], "pending-start")
        self.assertFalse(result["desktop_connected"])
        self.assertIsNone(result["server_pid"])
        self.assertEqual(result["harness_version"], "0.2.0")
        self.assertEqual(result["model_selection"], {"provider": "deepseek", "model": "deepseek-flash"})
        self.assertEqual(result["plugin_policy"], "inherit-desktop")
        self.assertEqual(result["subagents"], "desktop-auto")
        self.assertFalse(result["api_tested_by_doctor"])
        self.assertTrue(result["desktop_app_installed"])

    def test_desktop_connected_when_server_pid_present(self):
        status = self.status(server_pid=4242)
        root = self.make_root()
        calls, fake = self.make_runner(status=status)
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, environ={"PATH": ""})
        self.assertEqual(result["connection"], "connected")
        self.assertTrue(result["desktop_connected"])
        self.assertEqual(result["server_pid"], 4242)

    def test_desktop_missing_doctor_fields_stay_unknown(self):
        status = {"execution_backend": "desktop", "skill_linked": True, "cli_linked": True}
        root = self.make_root()
        calls, fake = self.make_runner(status=status)
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, environ={"PATH": ""})
        self.assertIsNone(result["server_pid"])
        self.assertEqual(result["connection"], "pending-start")
        self.assertIsNone(result["harness_version"])
        self.assertIsNone(result["model_selection"])
        self.assertIsNone(result["plugin_policy"])
        self.assertIsNone(result["subagents"])
        self.assertIsNone(result["api_tested_by_doctor"])
        self.assertIsNone(result["desktop_app_installed"])

    def test_desktop_app_signals_are_tri_state(self):
        cases = [
            ({"desktop_app_installed": True}, True),
            ({"desktop_app_installed": False}, False),
            ({"desktop_app_installed": None}, None),
            ({"desktop_app_path": "/Applications/DSH.app"}, True),
            ({"desktop_app_path": "   "}, False),
            # An explicit boolean wins over the path hint.
            ({"desktop_app_installed": True, "desktop_app_path": "   "}, True),
        ]
        for index, (extra, expected) in enumerate(cases):
            with self.subTest(signal=extra):
                status = self.status(**extra)
                if "desktop_app_path" in extra and "desktop_app_installed" not in extra:
                    status.pop("desktop_app_installed")
                root = self.make_root(name="app-%d" % index)
                calls, fake = self.make_runner(status=status)
                with mock.patch.object(setup.subprocess, "run", side_effect=fake):
                    result = setup.configure(root=root, environ={"PATH": ""})
                self.assertIs(result["desktop_app_installed"], expected)

    def test_desktop_app_signal_missing_is_unknown(self):
        status = self.status()
        status.pop("desktop_app_installed")
        root = self.make_root()
        calls, fake = self.make_runner(status=status)
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, environ={"PATH": ""})
        self.assertIsNone(result["desktop_app_installed"])

    def test_desktop_doctor_failure_is_reported_without_raising(self):
        root = self.make_root()
        calls, fake = self.make_runner(doctor_stdout="", doctor_exit_code=3,
                                       doctor_stderr="desktop server unreachable")
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, environ={"PATH": ""})
        self.assertEqual(result["doctor_exit_code"], 3)
        self.assertEqual(result["doctor_stderr"], "desktop server unreachable")
        self.assertEqual(result["doctor"], "")
        self.assertIsNone(result["doctor_status"])
        self.assertEqual(result["steps"], ["install-codex"])
        self.assertTrue(any(name.endswith("install-codex") for name in self.names(calls)))

    def test_desktop_url_defaults_and_override(self):
        self.assertEqual(setup.DEFAULT_DESKTOP_URL, "http://127.0.0.1:3080")
        for index, (config, expected) in enumerate([
            ({"desktop_url": ""}, setup.DEFAULT_DESKTOP_URL),
            ({"desktop_url": 123}, setup.DEFAULT_DESKTOP_URL),
            ({"desktop_url": "http://127.0.0.1:4096"}, "http://127.0.0.1:4096"),
        ]):
            with self.subTest(config=config):
                root = self.make_root(name="url-%d" % index, config=config)
                calls, fake = self.make_runner()
                with mock.patch.object(setup.subprocess, "run", side_effect=fake):
                    result = setup.configure(root=root, environ={"PATH": ""})
                self.assertEqual(result["desktop_url"], expected)

    # ---- backwards compatibility and shared contract ----------------------

    def test_config_without_execution_backend_stays_headless(self):
        root, node, environ = self.make_legacy_root()
        calls, fake = self.make_runner(status={})
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            result = setup.configure(root=root, environ=environ)
        self.assertEqual(result["execution_backend"], "headless")
        self.assertIn("setup-runtime", result["steps"])
        local = json.loads((root / "config.local.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(local["node"]).resolve(), node.resolve())
        self.assertTrue(result["local_config_written"])
        self.assertEqual(result["node_version"], "v24.1.0")
        self.assertIsNone(result["desktop_url"])
        self.assertIsNone(result["connection"])

    def test_both_backends_expose_the_same_key_set(self):
        desktop_root = self.make_root(name="keys-desktop")
        calls, fake = self.make_runner()
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            desktop = setup.configure(root=desktop_root, environ={"PATH": ""})
        legacy_root, node, environ = self.make_legacy_root()
        calls, fake = self.make_runner(status={})
        with mock.patch.object(setup.subprocess, "run", side_effect=fake):
            headless = setup.configure(root=legacy_root, environ=environ)
        self.assertEqual(set(desktop), set(headless))
        for key in ("node", "node_version", "credentials_path",
                    "runtime_version", "runtime_recorded_matches"):
            self.assertIsNone(desktop[key])
            self.assertIn(key, headless)
        for key in ("desktop_url", "connection", "desktop_connected", "server_pid",
                    "harness_version", "model_selection", "plugin_policy", "subagents",
                    "api_tested_by_doctor", "desktop_app_installed"):
            self.assertIsNone(headless[key])
            self.assertIn(key, desktop)
        self.assertIsInstance(desktop["notes"], list)

    def test_resolve_backend_normalization(self):
        self.assertEqual(setup.resolve_backend({"execution_backend": " DESKTOP "}), "desktop")
        self.assertEqual(setup.resolve_backend({"execution_backend": "Desktop"}), "desktop")
        self.assertEqual(setup.resolve_backend({"execution_backend": "desktop"}), "desktop")
        for value in (None, "headless", " HEADLESS "):
            with self.subTest(value=value):
                self.assertEqual(setup.resolve_backend({"execution_backend": value}), "headless")
        for value in ("", "   ", "desktop-x", "unknown", 1, True, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                setup.resolve_backend({"execution_backend": value})
        self.assertEqual(setup.resolve_backend({}), "headless")
        self.assertEqual(setup.resolve_backend(None), "headless")
        self.assertEqual(setup.resolve_backend("desktop"), "headless")

    # ---- main: desktop reporting and exit codes ---------------------------

    def test_main_desktop_success_ignores_legacy_runtime_version(self):
        # runtime_version/recorded_matches carry stale headless values on purpose.
        code, out, err = self.run_main(self.base_result())
        self.assertEqual(code, 0)
        self.assertIn("desktop", out)
        self.assertIn("0.2.0", out)
        self.assertNotIn("0.1.7-alpha.2", out)
        self.assertNotIn("运行时实际版本", out)
        self.assertIn("未检查 Node/npm", out)
        self.assertIn("本机配置未改动", out)
        self.assertIn("保留键", out)
        self.assertIn("全局 CLI/Skill 链接", out)
        self.assertEqual(err, "")

    def test_main_desktop_pending_start_is_success(self):
        code, out, err = self.run_main(self.base_result(status=self.status(server_pid=None)))
        self.assertEqual(code, 0)
        self.assertIn("桌面连接：待启动", out)
        self.assertIn("无需手动打开", out)
        self.assertNotIn("已连接", out)
        self.assertEqual(err, "")

    def test_main_desktop_connected_prints_server_pid(self):
        code, out, err = self.run_main(self.base_result(status=self.status(server_pid=4242)))
        self.assertEqual(code, 0)
        self.assertIn("已连接", out)
        self.assertIn("4242", out)
        self.assertEqual(err, "")

    def test_main_desktop_rejects_doctor_failure(self):
        result = self.base_result(doctor_exit_code=1, doctor="", doctor_status=None,
                                  doctor_stderr="桌面服务未就绪")
        code, out, err = self.run_main(result)
        self.assertEqual(code, 1)
        self.assertIn("桌面服务未就绪", err)
        self.assertIn(setup.DESKTOP_APP_HINT, err)
        self.assertIn(setup.NO_HEADLESS_FALLBACK, err)
        self.assertIn("不会退回独立 headless", err)

    def test_main_desktop_rejects_unparsable_doctor(self):
        code, out, err = self.run_main(self.base_result(doctor="not JSON", doctor_status=None))
        self.assertEqual(code, 1)
        self.assertIn("doctor", err)

    def test_main_desktop_rejects_backend_mismatch(self):
        result = self.base_result(status=self.status(execution_backend="headless"))
        code, out, err = self.run_main(result)
        self.assertEqual(code, 1)
        self.assertIn("不一致", err)

    def test_main_desktop_rejects_failed_global_entry_checks(self):
        for field in ("skill_linked", "cli_linked"):
            code, out, err = self.run_main(self.base_result(status=self.status(**{field: False})))
            with self.subTest(field=field):
                self.assertEqual(code, 1)
                self.assertIn("全局入口检查未通过", err)
        missing = self.status()
        missing.pop("skill_linked")
        code, out, err = self.run_main(self.base_result(status=missing))
        self.assertEqual(code, 1)

    def test_main_desktop_rejects_missing_app_with_install_hint(self):
        result = self.base_result(status=self.status(desktop_app_installed=False))
        code, out, err = self.run_main(result)
        self.assertEqual(code, 1)
        self.assertIn(setup.DESKTOP_APP_HINT, err)
        self.assertIn("安装", setup.DESKTOP_APP_HINT)
        self.assertIn(setup.NO_HEADLESS_FALLBACK, err)
        self.assertIn("不会退回独立 headless", err)

    def test_main_desktop_unknown_app_state_succeeds(self):
        status = self.status()
        status.pop("desktop_app_installed")
        code, out, err = self.run_main(self.base_result(status=status))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_main_desktop_accepts_normalized_doctor_backend(self):
        # doctor's own field is compared with the same normalization as config.
        for value in (" DESKTOP ", "Desktop", "desktop"):
            code, out, err = self.run_main(
                self.base_result(status=self.status(execution_backend=value)))
            with self.subTest(value=value):
                self.assertEqual(code, 0)
                self.assertEqual(err, "")

    def test_desktop_app_flag_normalization_unit(self):
        cases = [
            (True, True), (False, False), (None, None),
            ("true", True), (" TRUE ", True), ("yes", True), ("installed", True), ("1", True),
            ("false", False), (" FALSE ", False), ("no", False), ("off", False),
            ("missing", False), ("not-installed", False), ("", False),
            (1, True), (0, False), (2, None),
            ("weird", None), ([], None), ({}, None),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertIs(setup.desktop_app_installed({"desktop_app_installed": value}), expected)
        self.assertIsNone(setup.desktop_app_installed(None))

    def test_main_desktop_stringified_app_flag_is_not_installed(self):
        # bool("false") is True, so a stringified flag must not pass as installed.
        for value in ("false", " no ", "0", "", "missing", 0):
            code, out, err = self.run_main(
                self.base_result(status=self.status(desktop_app_installed=value)))
            with self.subTest(value=value):
                self.assertEqual(code, 1)
                self.assertIn(setup.DESKTOP_APP_HINT, err)
                self.assertIn(setup.NO_HEADLESS_FALLBACK, err)

    def test_main_desktop_truthy_app_flag_words_are_installed(self):
        for value in ("true", "yes", "installed", 1, True):
            code, out, err = self.run_main(
                self.base_result(status=self.status(desktop_app_installed=value)))
            with self.subTest(value=value):
                self.assertEqual(code, 0)
                self.assertEqual(err, "")

    def test_main_desktop_unknown_app_flag_string_stays_unknown(self):
        code, out, err = self.run_main(
            self.base_result(status=self.status(desktop_app_installed="maybe")))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_main_without_backend_key_keeps_headless_reporting(self):
        result = {
            "root": "/tmp/root", "node": "/tmp/node", "node_version": "v24.1.0",
            "credentials_path": "/tmp/creds.yaml", "local_config": "/tmp/config.local.json",
            "local_keys": ["node"], "steps": ["setup-runtime", "install-codex"],
            "doctor": json.dumps({"version_matches": True, "skill_linked": True, "cli_linked": True}),
            "doctor_stderr": "", "doctor_exit_code": 0, "runtime_version": "0.1.7-alpha.2",
        }
        code, out, err = self.run_main(result)
        self.assertEqual(code, 0)
        self.assertIn("本机覆盖已写入", out)
        self.assertIn("运行时实际版本：0.1.7-alpha.2", out)
        self.assertEqual(err, "")


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
