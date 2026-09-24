from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from leadseek import install


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "delivery"
        self.root.mkdir()
        (self.root / "skill/deepseek-delegate").mkdir(parents=True)
        (self.root / "bin").mkdir()
        (self.root / "bin/leadseek").write_text("placeholder executable")
        self.home = Path(self.temp.name) / "home"
        (self.home / ".codex").mkdir(parents=True)
        self.agents = self.home / ".codex/AGENTS.md"
        self.agents.write_text("用户原有规则\n")
        self.mock = patch.object(install, "ROOT", self.root)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def test_install_is_idempotent_and_uninstall_keeps_user_rules(self):
        install.integrate(self.home)
        first = self.agents.read_text()
        install.integrate(self.home)
        self.assertEqual(self.agents.read_text(), first)
        self.assertTrue((self.home / ".local/bin/leadseek").is_symlink())
        self.agents.write_text(first + "\n用户后来添加的规则\n")
        install.integrate(self.home, uninstall=True)
        self.assertIn("用户原有规则", self.agents.read_text())
        self.assertIn("用户后来添加的规则", self.agents.read_text())
        self.assertNotIn(install.BEGIN, self.agents.read_text())
        self.assertFalse((self.home / ".local/bin/leadseek").is_symlink())

    def test_existing_unrelated_entry_is_not_overwritten(self):
        target = self.home / ".local/bin/leadseek"
        target.parent.mkdir(parents=True)
        target.write_text("existing tool")
        with self.assertRaises(ValueError):
            install.integrate(self.home)
        self.assertEqual(target.read_text(), "existing tool")
        self.assertEqual(self.agents.read_text(), "用户原有规则\n")

    def test_malformed_rule_markers_preserved(self):
        content = "user rules\n" + install.BEGIN
        self.agents.write_text(content)
        with self.assertRaises(ValueError):
            install.integrate(self.home)
        self.assertEqual(self.agents.read_text(), content)
