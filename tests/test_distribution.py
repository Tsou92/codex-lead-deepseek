import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from leadseek import distribution  # noqa: E402


def load_builder():
    path = REPO / "scripts" / "build-installer.py"
    spec = importlib.util.spec_from_file_location("build_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ARCHIVES = (
    ("node", "arm64", "24.21.0", "node-v24.21.0-darwin-arm64.tar.gz",
     "node-v24.21.0-darwin-arm64.tar.gz"),
    ("node", "x86_64", "24.21.0", "node-v24.21.0-darwin-x64.tar.gz",
     "node-v24.21.0-darwin-x64.tar.gz"),
    ("python", "arm64", "3.12.14",
     "cpython-3.12.14%2B20260924-aarch64-apple-darwin-install_only_stripped.tar.gz",
     "cpython-3.12.14+20260924-aarch64-apple-darwin-install_only_stripped.tar.gz"),
    ("python", "x86_64", "3.12.14",
     "cpython-3.12.14%2B20260924-x86_64-apple-darwin-install_only_stripped.tar.gz",
     "cpython-3.12.14+20260924-x86_64-apple-darwin-install_only_stripped.tar.gz"),
)


def archive_bytes(name):
    return b"archive::" + name.encode("utf-8")


def make_repo(root, bundled=True, sha_ok=True):
    root = Path(root)
    (root / "src" / "leadseek").mkdir(parents=True)
    (root / "src" / "leadseek" / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    (root / "src" / "leadseek" / "onboard.py").write_text("x = 1\n", encoding="utf-8")
    (root / "bin").mkdir()
    for name in ("leadseek", "setup", "dsh", "import-monitor-history"):
        (root / "bin" / name).write_text("#!/bin/sh\n", encoding="utf-8")
    test_entry = root / "bin" / "test"
    test_entry.write_text("#!/bin/sh\n", encoding="utf-8")
    test_entry.chmod(0o755)
    (root / "bin" / "unknown-tool").write_text("x\n", encoding="utf-8")
    (root / "plugins").mkdir()
    (root / "plugins" / "demo.mjs").write_text("export {};\n", encoding="utf-8")
    (root / "plugins" / "private.js").write_text("export {};\n", encoding="utf-8")
    (root / "web").mkdir()
    for name in ("index.html", "app.js", "styles.css"):
        (root / "web" / name).write_text("x\n", encoding="utf-8")
    (root / "install").mkdir()
    (root / "scripts").mkdir()
    (root / "scripts" / "build-installer.py").write_text("#\n", encoding="utf-8")
    (root / "scripts" / "import-monitor-history.mjs").write_text("export {};\n", encoding="utf-8")
    (root / "skill" / "deepseek-delegate" / "agents").mkdir(parents=True)
    (root / "skill" / "deepseek-delegate" / "SKILL.md").write_text("s\n", encoding="utf-8")
    (root / "skill" / "deepseek-delegate" / "agents" / "openai.yaml").write_text("y\n", encoding="utf-8")
    (root / "examples").mkdir()
    (root / "examples" / "demo.py").write_text("x = 1\n", encoding="utf-8")
    (root / "examples" / "other.txt").write_text("x\n", encoding="utf-8")
    for name in ("edit-task.json", "inspect-task.json", "process-task.json", "research-task.json"):
        (root / "examples" / name).write_text("{}\n", encoding="utf-8")
    (root / "examples" / "demo-project").mkdir()
    (root / "examples" / "demo-project" / "math_utils.py").write_text("x = 1\n", encoding="utf-8")
    (root / "examples" / "demo-project" / "test_math_utils.py").write_text("x = 1\n", encoding="utf-8")
    (root / "examples" / "demo-project" / "secret.txt").write_text("x\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_sample.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests" / "sample.mjs").write_text("export {};\n", encoding="utf-8")
    (root / "tests" / "secret.txt").write_text("x\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "monitor-history.md").write_text("d\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("changes\n", encoding="utf-8")
    (root / "config.json").write_text("{}\n", encoding="utf-8")
    (root / "先看我.txt").write_text("先看我\n", encoding="utf-8")
    (root / "一键安装.command").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / "打开监控.command").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / "config.local.json").write_text('{"node":"secret"}\n', encoding="utf-8")
    (root / ".credentials").write_text("token\n", encoding="utf-8")
    (root / ".env").write_text("KEY=1\n", encoding="utf-8")
    (root / ".state").mkdir()
    (root / ".state" / "log").write_text("x\n", encoding="utf-8")
    (root / "runtime" / "node_modules").mkdir(parents=True)
    (root / "runtime" / "node_modules" / "x.js").write_text("x\n", encoding="utf-8")

    lines = ["# component\tarch\tversion\tbasename\turl\tsha256"]
    for component, arch, version, basename, disk in ARCHIVES:
        data = archive_bytes(disk)
        digest = hashlib.sha256(data).hexdigest() if sha_ok else "0" * 64
        lines.append("\t".join([component, arch, version, basename,
                                "https://example.invalid/" + disk, digest]))
    (root / "install" / "runtime-manifest.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if bundled:
        (root / "bundled").mkdir()
        for _, _, _, _, disk in ARCHIVES:
            (root / "bundled" / disk).write_bytes(archive_bytes(disk))
    return root


class AllowlistTests(unittest.TestCase):
    def test_allowlist_includes_real_structure_and_excludes_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo with space")
            (root / "bin" / "setup").unlink()
            (root / "bin" / "setup").symlink_to(root / "bin" / "leadseek")
            (root / "src" / "leadseek" / "link.py").symlink_to(root / "config.json")
            files = distribution.source_files(root)
            for expected in ("src/leadseek/onboard.py", "bin/leadseek", "bin/dsh",
                             "bin/import-monitor-history", "bin/test",
                             "plugins/demo.mjs",
                             "web/index.html", "web/app.js", "web/styles.css",
                             "install/runtime-manifest.tsv",
                             "scripts/build-installer.py",
                             "scripts/import-monitor-history.mjs",
                             "skill/deepseek-delegate/SKILL.md",
                             "skill/deepseek-delegate/agents/openai.yaml",
                             "examples/demo.py", "config.json", "先看我.txt",
                             "CHANGELOG.md",
                             "examples/edit-task.json", "examples/inspect-task.json",
                             "examples/process-task.json", "examples/research-task.json",
                             "examples/demo-project/math_utils.py",
                             "examples/demo-project/test_math_utils.py",
                             "tests/test_sample.py", "tests/sample.mjs",
                             "一键安装.command", "打开监控.command"):
                self.assertIn(expected, files)
            for forbidden in ("bin/unknown-tool", "bin/run", "plugins/private.js",
                              "examples/other.txt", "examples/demo-project/secret.txt",
                              "tests/secret.txt",
                              "config.local.json",
                              ".credentials", ".env", "bin/setup",
                              "src/leadseek/link.py"):
                self.assertNotIn(forbidden, files)
            self.assertFalse([n for n in files if n.startswith(".state/") or "node_modules" in n])

    def test_allowlist_contract_names(self):
        for name in ("web/index.html", "web/app.js", "web/styles.css",
                     "install/runtime-manifest.tsv", "bin/dsh",
                     "bin/import-monitor-history", "bin/test",
                     "CHANGELOG.md",
                     "examples/edit-task.json", "examples/inspect-task.json",
                     "examples/process-task.json", "examples/research-task.json",
                     "examples/demo-project/math_utils.py",
                     "examples/demo-project/test_math_utils.py",
                     "scripts/import-monitor-history.mjs",
                     "skill/deepseek-delegate/agents/openai.yaml"):
            self.assertIn(name, distribution.EXPLICIT_FILES)
        self.assertNotIn("bin/run", distribution.EXPLICIT_FILES)
        self.assertIn(("examples", "*.py"), distribution.GLOB_FILES)
        self.assertIn(("tests", "*.py"), distribution.GLOB_FILES)
        self.assertIn(("tests", "*.mjs"), distribution.GLOB_FILES)

    def test_real_repo_entrypoints_are_listed(self):
        files = distribution.source_files(REPO)
        candidates = ("src/leadseek/onboard.py", "src/leadseek/distribution.py",
                      "bin/leadseek", "bin/setup", "README.md", "先看我.txt",
                      "一键安装.command", "config.json",
                      "scripts/build-installer.py",
                      "tests/test_onboard.py", "tests/test_distribution.py",
                      "examples/edit-task.json",
                      "examples/demo-project/math_utils.py")
        for expected in (rel for rel in candidates if (REPO / rel).is_file()):
            self.assertIn(expected, files)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.builder = load_builder()

    def _build(self, bundled=True, sha_ok=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        root = make_repo(base / "repo", bundled=bundled, sha_ok=sha_ok)
        out = base / "dist out"
        return self.builder.build(root=root, output_dir=out, with_runtimes=True)

    def test_zip_is_clean_reproducible_and_keeps_modes(self):
        first = self._build()
        second = self._build()
        self.assertEqual(first.name, "codex-lead-deepseek-9.9.9-macos.zip")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            names = archive.namelist()
            for expected in ("codex-lead-deepseek/README.md",
                             "codex-lead-deepseek/src/leadseek/onboard.py",
                             "codex-lead-deepseek/web/index.html",
                             "codex-lead-deepseek/install/runtime-manifest.tsv",
                             "codex-lead-deepseek/一键安装.command",
                             "codex-lead-deepseek/SHA256SUMS"):
                self.assertIn(expected, names)
            joined = "\n".join(names)
            for forbidden in ("config.local.json", ".credentials", ".env", "/.state/", "node_modules"):
                self.assertNotIn(forbidden, joined)
            manifest = archive.read("codex-lead-deepseek/SHA256SUMS").decode("utf-8")
            self.assertIn("codex-lead-deepseek/web/index.html", manifest)
            info = archive.getinfo("codex-lead-deepseek/bin/leadseek")
            self.assertNotEqual((info.external_attr >> 16) & 0o777, 0)
        sha_file = Path(str(first) + ".sha256")
        self.assertTrue(sha_file.is_file())
        digest = hashlib.sha256(first.read_bytes()).hexdigest()
        self.assertIn(digest, sha_file.read_text(encoding="utf-8"))

    def test_zip_carries_official_archives_with_plus_decoding(self):
        path = self._build()
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for _, _, _, _, disk in ARCHIVES:
                self.assertIn("codex-lead-deepseek/bundled/" + disk, names)
            self.assertIn("codex-lead-deepseek/bundled/"
                          "cpython-3.12.14+20260924-aarch64-apple-darwin-install_only_stripped.tar.gz",
                          names)

    def test_bundled_percent_encoded_names_are_normalized(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        root = make_repo(base / "repo")
        for _, _, _, _, disk in ARCHIVES:
            if "%2B" in disk:
                continue
            literal = root / "bundled" / disk
            literal.rename(root / "bundled" / disk.replace("+", "%2B"))
        path = self.builder.build(root=root, output_dir=base / "dist", with_runtimes=True)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for _, _, _, _, disk in ARCHIVES:
                self.assertIn("codex-lead-deepseek/bundled/" + disk, names)
            self.assertFalse([name for name in names if "%2B" in name])

    def test_zip_ships_public_tests_examples_and_runnable_bin_test(self):
        path = self._build()
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for expected in ("codex-lead-deepseek/CHANGELOG.md",
                             "codex-lead-deepseek/tests/test_sample.py",
                             "codex-lead-deepseek/tests/sample.mjs",
                             "codex-lead-deepseek/examples/edit-task.json",
                             "codex-lead-deepseek/examples/demo-project/math_utils.py"):
                self.assertIn(expected, names)
            self.assertNotIn("codex-lead-deepseek/tests/secret.txt", names)
            info = archive.getinfo("codex-lead-deepseek/bin/test")
            self.assertNotEqual((info.external_attr >> 16) & 0o111, 0)

    def test_wrong_manifest_sha_is_refused(self):
        with self.assertRaises(SystemExit):
            self._build(sha_ok=False)

    def test_missing_archives_fail_loudly(self):
        with self.assertRaises(SystemExit):
            self._build(bundled=False)

    def test_override_name_with_path_separator_is_refused(self):
        with self.assertRaises(SystemExit):
            self.builder.main(["--runtime-archive", "../evil=foo"])
        with self.assertRaises(SystemExit):
            self.builder.main(["--runtime-archive", "a/b=foo"])


if __name__ == "__main__":
    unittest.main()
