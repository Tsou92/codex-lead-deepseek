import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import json
import subprocess
import tempfile
import unittest

from leadseek import runtime_install


A = "@deepseek-ai/dsh"


class RuntimeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.bindir = self.root / "fake-node"
        self.bindir.mkdir()
        self.node = self.bindir / "node"
        self.node.write_text("#!/bin/sh\n")
        self.node.chmod(0o755)
        self.npm = self.bindir / "npm"
        self.npm.write_text("#!/bin/sh\n")
        self.npm.chmod(0o755)
        self.environ = {"PATH": str(self.bindir)}

    def make_runtime(self, version, root=None):
        base = (root or self.root) / "runtime/node_modules" / A
        (base / "lib").mkdir(parents=True, exist_ok=True)
        (base / "package.json").write_text(
            json.dumps({"name": A, "version": version}), encoding="utf-8")
        (base / "lib/bin.js").write_text("// fake\n", encoding="utf-8")
        for name in runtime_install.REQUIRED_PACKAGES:
            package = (root or self.root) / "runtime/node_modules" / name
            package.mkdir(parents=True, exist_ok=True)
            (package / "package.json").write_text(
                json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8")
        return base

    def make_global(self, version):
        base = Path(self.temp.name) / "global/node_modules" / A
        (base / "lib").mkdir(parents=True, exist_ok=True)
        (base / "package.json").write_text(
            json.dumps({"name": A, "version": version}), encoding="utf-8")
        (base / "lib/bin.js").write_text("// fake\n", encoding="utf-8")
        command = self.bindir / "dsh"
        command.symlink_to(base / "lib/bin.js")
        return base

    def fake_run(self, calls, npm_version=None, cli_version=None, npm_fail=False):
        def run(command, **kwargs):
            calls.append(command)
            if Path(str(command[0])).name == "npm":
                if npm_fail:
                    return subprocess.CompletedProcess(command, 1, "", "npm boom")
                if npm_version:
                    self.make_runtime(npm_version)
                return subprocess.CompletedProcess(command, 0, "", "")
            if "--version" in command:
                reported = cli_version if cli_version is not None else npm_version
                return subprocess.CompletedProcess(command, 0, (reported or "") + "\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")
        return run

    def test_fresh_install_requests_latest_with_explicit_registry(self):
        calls = []
        result = runtime_install.ensure_runtime(
            self.root, str(self.node), self.environ,
            run=self.fake_run(calls, npm_version="3.0.0"), which=lambda *a, **k: None)
        self.assertEqual(result["source"], runtime_install.SOURCE_LATEST)
        self.assertEqual(result["version"], "3.0.0")
        self.assertTrue(result["same_version_as_global"] is False)
        command = " ".join(calls[0])
        self.assertIn(A + "@latest", command)
        self.assertIn("--registry https://registry.npmjs.org", command)
        self.assertIn("--save-exact", command)
        self.assertNotIn("--global", command)
        self.assertNotIn(" -g", command)

    def test_existing_runtime_is_reused_without_npm(self):
        self.make_runtime("4.1.0")
        calls = []
        result = runtime_install.ensure_runtime(
            self.root, str(self.node), self.environ,
            run=self.fake_run(calls, cli_version="4.1.0"), which=lambda *a, **k: None)
        self.assertEqual(result["source"], runtime_install.SOURCE_EXISTING)
        self.assertEqual(result["version"], "4.1.0")
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(Path(str(c[0])).name != "npm" for c in calls))

    def test_broken_existing_runtime_fails_and_does_not_reinstall(self):
        package = self.root / "runtime/node_modules" / A
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"name": A, "version": "1.0.0"}))
        calls = []
        with self.assertRaises(ValueError) as raised:
            runtime_install.ensure_runtime(
                self.root, str(self.node), self.environ,
                run=self.fake_run(calls, npm_version="9.9.9"), which=lambda *a, **k: None)
        self.assertIn("损坏", str(raised.exception))
        self.assertEqual(calls, [])

    def test_global_package_reinstalls_same_version_not_latest(self):
        self.make_global("2.5.0")
        calls = []
        result = runtime_install.ensure_runtime(
            self.root, str(self.node), self.environ,
            run=self.fake_run(calls, npm_version="2.5.0"),
            which=lambda name, path=None: str(self.bindir / "dsh") if name == "dsh" else None)
        self.assertEqual(result["source"], runtime_install.SOURCE_GLOBAL)
        self.assertEqual(result["version"], "2.5.0")
        self.assertTrue(result["same_version_as_global"])
        command = " ".join(calls[0])
        self.assertIn(A + "@2.5.0", command)
        self.assertNotIn("@latest", command)

    def test_npm_failure_raises_and_writes_no_success(self):
        calls = []
        with self.assertRaises(ValueError) as raised:
            runtime_install.ensure_runtime(
                self.root, str(self.node), self.environ,
                run=self.fake_run(calls, npm_fail=True), which=lambda *a, **k: None)
        self.assertIn("失败", str(raised.exception))
        self.assertNotIn("npm boom", str(raised.exception))
        self.assertFalse((self.root / "config.local.json").exists())

    def test_record_runtime_preserves_other_keys_and_uses_0600(self):
        path = self.root / "config.local.json"
        path.write_text(json.dumps({
            "node": "/usr/bin/node", "credentials_path": "/tmp/c.yaml",
            "runtime_version": "0.0.1", "custom_key": 7,
        }), encoding="utf-8")
        runtime_install.record_runtime(self.root, {
            "source": runtime_install.SOURCE_LATEST, "version": "5.0.0"})
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["node"], "/usr/bin/node")
        self.assertEqual(data["credentials_path"], "/tmp/c.yaml")
        self.assertEqual(data["custom_key"], 7)
        self.assertEqual(data["runtime_version"], "5.0.0")
        self.assertEqual(data["runtime_source"], runtime_install.SOURCE_LATEST)
        self.assertFalse(data["api_tested"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_doctor_latest_policy_is_structural_not_remote_claim(self):
        fields = runtime_install.doctor_version_fields("latest", "6.0.0", "6.0.0")
        self.assertEqual(fields["expected"], "latest")
        self.assertEqual(fields["actual"], "6.0.0")
        self.assertEqual(fields["version_policy"], "latest")
        self.assertTrue(fields["version_matches"])
        self.assertTrue(fields["recorded_matches"])
        self.assertFalse(fields["latest_checked_remotely"])

    def test_doctor_pinned_policy_compares_exact_version(self):
        self.assertTrue(runtime_install.doctor_version_fields("1.0.0", "v1.0.0")["version_matches"])
        self.assertFalse(runtime_install.doctor_version_fields("1.0.0", "2.0.0")["version_matches"])
        self.assertFalse(runtime_install.doctor_version_fields("latest", "2.0.0", "1.0.0")["recorded_matches"])

    def test_runtime_dependency_check_is_structural_only(self):
        status = runtime_install.runtime_dependency_status(self.root)
        self.assertTrue(status["structural_only"])
        self.assertIsNone(status["api_compatible"])
        self.assertFalse(status["all_present"])
        self.assertIn("@deepseek-ai/dsh-headless", status["missing"])
        self.assertIn("@deepseek-ai/dsh-session", status["missing"])
        self.assertNotIn("@deepseek-ai/dsh-session-local", status["packages"])
        self.assertEqual(set(status["required"]),
                         {"@deepseek-ai/cordis", "@deepseek-ai/schemastery",
                          "@deepseek-ai/dsh-headless", "@deepseek-ai/dsh-session"})

    def test_required_packages_all_present_when_directories_exist(self):
        base = self.root / "runtime/node_modules"
        for name in runtime_install.REQUIRED_PACKAGES:
            package = base / name
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"name": name}))
        status = runtime_install.runtime_dependency_status(self.root)
        self.assertTrue(status["all_present"])
        self.assertEqual(status["missing"], [])
        self.assertEqual(runtime_install.missing_required_packages(self.root), [])

    def test_cli_version_uses_15_second_timeout(self):
        self.make_runtime("1.0.0")
        seen = {}

        def run(command, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(command, 0, "1.0.0\n", "")

        runtime_install.cli_version(str(self.node), self.root, run=run)
        self.assertEqual(seen.get("timeout"), runtime_install.CLI_TIMEOUT_SECONDS)
        self.assertEqual(runtime_install.CLI_TIMEOUT_SECONDS, 15)

    def test_cli_version_timeout_raises_concise_error(self):
        self.make_runtime("1.0.0")

        def run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

        with self.assertRaises(ValueError) as raised:
            runtime_install.cli_version(str(self.node), self.root, run=run)
        self.assertIn("秒内没有响应", str(raised.exception))

    def test_npm_install_uses_600_second_timeout_and_hides_output(self):
        timeouts = []

        def run(command, **kwargs):
            if Path(str(command[0])).name == "npm":
                timeouts.append(kwargs.get("timeout"))
                return subprocess.CompletedProcess(command, 1, "", "SECRET_TOKEN=abc")
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaises(ValueError) as raised:
            runtime_install.ensure_runtime(
                self.root, str(self.node), self.environ,
                run=run, which=lambda *a, **k: None)
        self.assertEqual(timeouts, [runtime_install.NPM_TIMEOUT_SECONDS])
        self.assertEqual(runtime_install.NPM_TIMEOUT_SECONDS, 600)
        self.assertNotIn("SECRET_TOKEN", str(raised.exception))

    def test_npm_install_timeout_raises_without_leaking_environment(self):
        def run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

        with self.assertRaises(ValueError) as raised:
            runtime_install.ensure_runtime(
                self.root, str(self.node), self.environ,
                run=run, which=lambda *a, **k: None)
        message = str(raised.exception)
        self.assertIn("超时", message)
        self.assertNotIn("PATH", message)

    def test_global_version_mismatch_is_rejected(self):
        self.make_global("2.5.0")
        calls = []
        with self.assertRaises(ValueError) as raised:
            runtime_install.ensure_runtime(
                self.root, str(self.node), self.environ,
                run=self.fake_run(calls, npm_version="2.4.0"),
                which=lambda name, path=None: str(self.bindir / "dsh") if name == "dsh" else None)
        self.assertIn("与请求版本不一致", str(raised.exception))

    def test_missing_required_packages_fail_instead_of_reporting_success(self):
        def run(command, **kwargs):
            if Path(str(command[0])).name == "npm":
                base = self.root / "runtime/node_modules" / A
                (base / "lib").mkdir(parents=True, exist_ok=True)
                (base / "package.json").write_text(
                    json.dumps({"name": A, "version": "1.0.0"}), encoding="utf-8")
                (base / "lib/bin.js").write_text("// fake\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 0, "1.0.0\n", "")

        with self.assertRaises(ValueError) as raised:
            runtime_install.ensure_runtime(
                self.root, str(self.node), self.environ,
                run=run, which=lambda *a, **k: None)
        self.assertIn("关键包", str(raised.exception))
        self.assertFalse((self.root / "config.local.json").exists())

    def test_atomic_write_rejects_symlinked_config(self):
        target = self.root / "outside.json"
        target.write_text("{}", encoding="utf-8")
        link = self.root / "config.local.json"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            runtime_install.merge_local_config(self.root, {"runtime_version": "1.2.3"})
        self.assertEqual(target.read_text(encoding="utf-8"), "{}")
        self.assertTrue(link.is_symlink())

    def test_atomic_write_is_0600_and_leaves_no_temp_files(self):
        path = self.root / "config.local.json"
        runtime_install.atomic_write_json(path, {"a": 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1})
        leftovers = [p.name for p in self.root.iterdir()
                     if p.name != "config.local.json" and p.name.endswith(".json")]
        self.assertEqual(leftovers, [])

    def test_doctor_latest_policy_fails_when_recorded_version_mismatches(self):
        fields = runtime_install.doctor_version_fields("latest", "2.0.0", "1.0.0")
        self.assertTrue(fields["policy_matches"])
        self.assertFalse(fields["recorded_matches"])
        self.assertFalse(fields["version_matches"])
        self.assertFalse(fields["latest_checked_remotely"])

    def test_npm_prefers_node_sibling(self):
        self.assertEqual(runtime_install.find_npm(str(self.node), self.environ), str(self.npm))


if __name__ == "__main__":
    unittest.main()
