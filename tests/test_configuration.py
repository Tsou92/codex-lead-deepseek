import json
import os
from pathlib import Path
import tempfile
import unittest

from leadseek import configuration


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def executable(self, name, directory=None):
        directory = directory or self.root
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def write(self, name, data):
        (self.root / name).write_text(json.dumps(data), encoding="utf-8")

    def test_auto_prefers_local_node24_path(self):
        node = self.executable("node")
        self.assertEqual(configuration.resolve_node("auto", {"PATH": ""}, candidates=(str(node),)), str(node))

    def test_auto_falls_back_to_path_node(self):
        bindir = self.root / "bin"
        node = self.executable("node", bindir)
        self.assertEqual(configuration.resolve_node("auto", {"PATH": str(bindir)}, candidates=()), str(node))

    def test_node_keeps_stable_symlink_when_target_version_changes(self):
        current = self.executable("node-v24-old")
        stable = self.root / "node"
        stable.symlink_to(current)
        resolved = configuration.resolve_node(str(stable), {"PATH": ""})
        self.assertEqual(resolved, str(stable))
        stable.unlink()
        stable.symlink_to(self.executable("node-v24-new"))
        current.unlink()
        self.assertTrue(Path(resolved).is_file())

    def test_explicit_command_name_is_resolved(self):
        bindir = self.root / "bin"
        node = self.executable("node", bindir)
        self.assertEqual(configuration.resolve_node("node", {"PATH": str(bindir)}, candidates=()), str(node))

    def test_explicit_absolute_path_is_resolved(self):
        node = self.executable("my-node")
        self.assertEqual(configuration.resolve_node(str(node), {"PATH": ""}), str(node))

    def test_missing_node_raises_actionable_error(self):
        with self.assertRaises(ValueError) as raised:
            configuration.resolve_node("node", {"PATH": ""}, candidates=())
        self.assertIn("Node", str(raised.exception))
        with self.assertRaises(ValueError):
            configuration.resolve_node(str(self.root / "absent"), {"PATH": ""})

    def test_credentials_expanduser_is_absolute(self):
        result = configuration.resolve_credentials_path("~/.dsh/.credentials.yaml")
        self.assertTrue(Path(result).is_absolute())
        self.assertEqual(result, str(Path.home() / ".dsh/.credentials.yaml"))

    def test_relative_credentials_resolve_against_root_not_cwd(self):
        result = configuration.resolve_credentials_path("state/creds.yaml", self.root)
        self.assertEqual(result, str((self.root / "state/creds.yaml").resolve()))
        node = self.executable("node")
        self.write("config.json", {"node": str(node), "credentials_path": "state/creds.yaml"})
        other = self.root / "elsewhere"
        other.mkdir()
        cwd = os.getcwd()
        os.chdir(other)
        try:
            config = configuration.load_config(self.root)
        finally:
            os.chdir(cwd)
        self.assertEqual(config["credentials_path"], str((self.root / "state/creds.yaml").resolve()))

    def test_explicit_node_is_absolute_from_other_cwd(self):
        bindir = self.root / "bin"
        node = self.executable("node", bindir)
        other = self.root / "elsewhere"
        other.mkdir()
        cwd = os.getcwd()
        os.chdir(other)
        try:
            result = configuration.resolve_node("node", {"PATH": str(bindir)}, candidates=())
        finally:
            os.chdir(cwd)
        self.assertTrue(Path(result).is_absolute())
        self.assertEqual(result, str(node.resolve()))

    def test_empty_credentials_rejected(self):
        with self.assertRaises(ValueError):
            configuration.resolve_credentials_path("   ")

    def test_load_config_local_override_keeps_other_keys(self):
        node = self.executable("node")
        self.write("config.json", {"node": str(node), "model": "base-model", "timeout_seconds": 600,
                                   "credentials_path": "~/.dsh/base.yaml"})
        self.write("config.local.json", {"model": "local-model", "custom_key": 7})
        config = configuration.load_config(self.root)
        self.assertEqual(config["model"], "local-model")
        self.assertEqual(config["custom_key"], 7)
        self.assertEqual(config["timeout_seconds"], 600)
        self.assertEqual(config["node"], str(node))
        self.assertTrue(Path(config["credentials_path"]).is_absolute())

    def test_load_config_without_local_file(self):
        node = self.executable("node")
        self.write("config.json", {"node": str(node), "credentials_path": "~/.dsh/x.yaml"})
        config = configuration.load_config(self.root)
        self.assertEqual(config["node"], str(node))

    def test_non_object_config_is_rejected(self):
        (self.root / "config.json").write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(ValueError):
            configuration.load_config(self.root)
        self.write("config.json", {})
        (self.root / "config.local.json").write_text('"not-an-object"', encoding="utf-8")
        with self.assertRaises(ValueError):
            configuration.load_config(self.root)

    def test_invalid_json_is_rejected(self):
        (self.root / "config.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            configuration.load_config(self.root)

    def test_missing_common_config_is_rejected(self):
        with self.assertRaises(ValueError):
            configuration.load_config(self.root)


if __name__ == "__main__":
    unittest.main()
