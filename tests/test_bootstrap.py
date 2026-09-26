"""Reproducible tests for the portable bootstrap and launcher.

No test downloads anything or writes to HOME: every archive is a tiny locally
built tarball and every project lives in a TemporaryDirectory.
"""

import io
import os
from hashlib import sha256
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


def host_arch():
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    raise unittest.SkipTest("unsupported host architecture: " + machine)


PYTHON_SCRIPT = "#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then echo 3.12.14; else echo 3.12.14; fi\n"
NODE_SCRIPT = "#!/bin/sh\necho v24.21.0\n"
NPM_SCRIPT = "#!/bin/sh\necho 11.0.0\n"


def build_archive(dest, root_name, members):
    with tarfile.open(str(dest), "w:gz") as archive:
        for relative, content, mode in members:
            data = content.encode("utf-8")
            info = tarfile.TarInfo(root_name + "/" + relative)
            info.size = len(data)
            info.mode = mode
            archive.addfile(info, io.BytesIO(data))


def sha256_file(path):
    digest = sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def python_basename(arch):
    tag = "aarch64" if arch == "arm64" else "x86_64"
    return "cpython-3.12.14%2B20260924-" + tag + "-apple-darwin-install_only_stripped.tar.gz"


def node_basename(arch):
    tag = "arm64" if arch == "arm64" else "x64"
    return "node-v24.21.0-darwin-" + tag + ".tar.gz"


class BootstrapTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.arch = host_arch()

    def make_project(self, name="project"):
        project = Path(self.temp.name) / name
        (project / "bin").mkdir(parents=True)
        (project / "install").mkdir()
        (project / "bundled").mkdir()
        for script in ("bootstrap", "python"):
            target = project / "bin" / script
            shutil.copy2(str(REPO_ROOT / "bin" / script), str(target))
            target.chmod(0o755)
        return project

    def make_python_archive(self, project, sha_override=None):
        name = python_basename(self.arch)
        path = project / "bundled" / name
        build_archive(path, "python", [("bin/python3", PYTHON_SCRIPT, 0o755)])
        return name, (sha_override or sha256_file(path))

    def make_node_archive(self, project, sha_override=None):
        name = node_basename(self.arch)
        root_name = "node-v24.21.0-darwin-" + ("arm64" if self.arch == "arm64" else "x64")
        path = project / "bundled" / name
        build_archive(path, root_name, [("bin/node", NODE_SCRIPT, 0o755),
                                        ("bin/npm", NPM_SCRIPT, 0o755)])
        return name, (sha_override or sha256_file(path))

    def write_manifest(self, project, rows):
        header = ["# component", "arch", "version", "basename", "url", "sha256"]
        lines = ["\t".join(header)]
        lines += ["\t".join(row) for row in rows]
        (project / "install" / "runtime-manifest.tsv").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    def entry(self, component, basename, digest):
        version = "3.12.14" if component == "python" else "24.21.0"
        return (component, self.arch, version, basename, "https://example.invalid/" + basename, digest)

    def run_bootstrap(self, project, args, path="/usr/bin:/bin:/usr/sbin:/sbin"):
        env = {"PATH": path, "HOME": str(Path(self.temp.name) / "home"), "LANG": "C"}
        return subprocess.run([str(project / "bin" / "bootstrap")] + list(args),
                              env=env, capture_output=True, text=True)

    def run_python_launcher(self, project, args, path):
        env = {"PATH": path, "HOME": str(Path(self.temp.name) / "home")}
        return subprocess.run([str(project / "bin" / "python")] + list(args),
                              env=env, capture_output=True, text=True)

    def run_bootstrap_env(self, project, args, path="/usr/bin:/bin:/usr/sbin:/sbin", extra=None):
        env = {"PATH": path, "HOME": str(Path(self.temp.name) / "home"), "LANG": "C"}
        if extra:
            env.update(extra)
        return subprocess.run([str(project / "bin" / "bootstrap")] + list(args),
                              env=env, capture_output=True, text=True)

    def make_mock_uname(self, os_name, arch=None):
        """Fake uname that answers per argument and records every call."""
        arch = arch or self.arch
        bindir = Path(self.temp.name) / ("unamebin-" + os_name + "-" + arch)
        bindir.mkdir(parents=True, exist_ok=True)
        log = Path(self.temp.name) / ("uname-" + os_name + ".log")
        script = ("#!/bin/sh\n"
                  'echo "$@" >> "' + str(log) + '"\n'
                  'case "$1" in\n'
                  '  -s) echo "' + os_name + '" ;;\n'
                  '  -m) echo "' + arch + '" ;;\n'
                  '  *) echo "' + os_name + '" ;;\n'
                  'esac\n')
        uname = bindir / "uname"
        uname.write_text(script, encoding="utf-8")
        uname.chmod(0o755)
        return bindir, log

    def make_mock_curl(self, source):
        """Fake curl that copies a prebuilt archive and prints to stdout."""
        bindir = Path(self.temp.name) / "curlbin"
        bindir.mkdir(parents=True, exist_ok=True)
        script = ("#!/bin/sh\n"
                  'dest=""\nprev=""\n'
                  'for arg in "$@"; do\n'
                  '  if [ "$prev" = "-o" ]; then dest="$arg"; fi\n'
                  '  prev="$arg"\n'
                  'done\n'
                  'cp "' + str(source) + '" "$dest" || exit 1\n'
                  'echo "fake-curl-progress"\n')
        curl = bindir / "curl"
        curl.write_text(script, encoding="utf-8")
        curl.chmod(0o755)
        return bindir


class BootstrapTests(BootstrapTestBase):
    def test_offline_missing_archive_fails_clearly(self):
        project = self.make_project()
        name, digest = self.make_python_archive(project)
        (project / "bundled" / name).unlink()
        self.write_manifest(project, [self.entry("python", name, digest)])
        result = self.run_bootstrap(project, ["--offline"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("离线", result.stderr)

    def test_checksum_mismatch_fails(self):
        project = self.make_project()
        name, _ = self.make_python_archive(project)
        self.write_manifest(project, [self.entry("python", name, "0" * 64)])
        result = self.run_bootstrap(project, ["--offline", "--only", "python"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA256", result.stderr)
        self.assertFalse((project / ".portable").exists())

    def test_installs_verifies_and_reuses(self):
        project = self.make_project()
        py_name, py_digest = self.make_python_archive(project)
        node_name, node_digest = self.make_node_archive(project)
        self.write_manifest(project, [self.entry("python", py_name, py_digest),
                                      self.entry("node", node_name, node_digest)])
        first = self.run_bootstrap(project, ["--offline"])
        self.assertEqual(first.returncode, 0, first.stderr)
        py = project / ".portable" / "python" / "bin" / "python3"
        node = project / ".portable" / "node" / "bin" / "node"
        npm = project / ".portable" / "node" / "bin" / "npm"
        self.assertTrue(py.is_file() and os.access(str(py), os.X_OK))
        self.assertTrue(node.is_file() and os.access(str(node), os.X_OK))
        self.assertTrue(npm.is_file() and os.access(str(npm), os.X_OK))
        shutil.rmtree(str(project / "bundled"))
        second = self.run_bootstrap(project, ["--offline"])
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("跳过", second.stdout)

    def test_project_path_with_spaces(self):
        project = self.make_project(name="project with spaces")
        name, digest = self.make_python_archive(project)
        self.write_manifest(project, [self.entry("python", name, digest)])
        result = self.run_bootstrap(project, ["--offline", "--only", "python"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((project / ".portable" / "python" / "bin" / "python3").is_file())

    def test_symlinked_portable_is_rejected(self):
        project = self.make_project()
        name, digest = self.make_python_archive(project)
        self.write_manifest(project, [self.entry("python", name, digest)])
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (project / ".portable").symlink_to(outside)
        result = self.run_bootstrap(project, ["--offline", "--only", "python"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("符号链接", result.stderr)

    def test_help_exits_zero(self):
        project = self.make_project()
        result = self.run_bootstrap(project, ["--help"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("--offline", result.stdout)


class LauncherTests(BootstrapTestBase):
    def install_fake_portable_python(self, project):
        target = project / ".portable" / "python" / "bin" / "python3"
        target.parent.mkdir(parents=True)
        target.write_text("#!/bin/sh\necho \"PORTABLE:$*\"\n", encoding="utf-8")
        target.chmod(0o755)
        return target

    def test_portable_python_wins_without_external_path(self):
        project = self.make_project()
        self.install_fake_portable_python(project)
        result = self.run_python_launcher(project, ["-c", "pass"], str(project / "empty-path"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PORTABLE:-c pass", result.stdout)

    def test_missing_interpreter_points_at_command(self):
        project = self.make_project()
        result = self.run_python_launcher(project, ["--version"], str(project / "empty-path"))
        self.assertEqual(result.returncode, 127)
        self.assertIn(".command", result.stderr)

    def test_symlinked_launcher_resolves_to_project(self):
        project = self.make_project()
        self.install_fake_portable_python(project)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = outside / "pylink"
        link.symlink_to(project / "bin" / "python")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(Path(self.temp.name) / "home")}
        result = subprocess.run([str(link), "hello"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PORTABLE:hello", result.stdout)

    def test_polyglot_launcher_runs_under_portable_python(self):
        project = self.make_project()
        target = project / ".portable" / "python" / "bin" / "python3"
        target.parent.mkdir(parents=True)
        target.write_text("#!/bin/sh\nexec /usr/bin/env python3 \"$@\"\n", encoding="utf-8")
        target.chmod(0o755)
        header = (REPO_ROOT / "bin" / "dsh").read_text(encoding="utf-8").splitlines()[:2]
        probe = project / "bin" / "probe"
        probe.write_text("\n".join(header) + "\nimport sys\nprint('ARGS:' + '|'.join(sys.argv[1:]))\n",
                         encoding="utf-8")
        probe.chmod(0o755)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = outside / "probe-link"
        link.symlink_to(probe)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(Path(self.temp.name) / "home")}
        result = subprocess.run([str(link), "one", "two"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ARGS:one|two", result.stdout)


class BootstrapSafetyTests(BootstrapTestBase):
    def prepare_python(self, project):
        name, digest = self.make_python_archive(project)
        self.write_manifest(project, [self.entry("python", name, digest)])
        return name, digest

    def test_download_branch_keeps_stdout_clean(self):
        project = self.make_project()
        name, digest = self.make_python_archive(project)
        staged = Path(self.temp.name) / "source-archive.tar.gz"
        shutil.move(str(project / "bundled" / name), str(staged))
        self.write_manifest(project, [self.entry("python", name, digest)])
        curlbin = self.make_mock_curl(staged)
        path = str(curlbin) + ":/usr/bin:/bin:/usr/sbin:/sbin"
        result = self.run_bootstrap_env(project, ["--only", "python"], path=path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("fake-curl-progress", result.stdout)
        self.assertTrue((project / ".portable" / "python" / "bin" / "python3").is_file())
        self.assertTrue((project / ".state" / "downloads" / name).is_file())

    def test_uname_queried_with_s_and_m(self):
        project = self.make_project()
        self.prepare_python(project)
        bindir, log = self.make_mock_uname("Darwin")
        path = str(bindir) + ":/usr/bin:/bin:/usr/sbin:/sbin"
        result = self.run_bootstrap_env(project, ["--offline", "--only", "python"], path=path)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = log.read_text(encoding="utf-8")
        self.assertIn("-s", calls)
        self.assertIn("-m", calls)

    def test_non_darwin_is_rejected(self):
        project = self.make_project()
        self.prepare_python(project)
        bindir, log = self.make_mock_uname("Linux")
        path = str(bindir) + ":/usr/bin:/bin:/usr/sbin:/sbin"
        result = self.run_bootstrap_env(project, ["--offline", "--only", "python"], path=path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Darwin", result.stderr)
        self.assertIn("-s", log.read_text(encoding="utf-8"))
        self.assertFalse((project / ".portable").exists())

    def test_symlinked_state_is_rejected(self):
        project = self.make_project()
        self.prepare_python(project)
        outside = Path(self.temp.name) / "outside-state"
        outside.mkdir()
        (project / ".state").symlink_to(outside)
        result = self.run_bootstrap(project, ["--offline", "--only", "python"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("符号链接", result.stderr)

    def test_symlinked_downloads_is_rejected(self):
        project = self.make_project()
        self.prepare_python(project)
        outside = Path(self.temp.name) / "outside-downloads"
        outside.mkdir()
        (project / ".state").mkdir()
        (project / ".state" / "downloads").symlink_to(outside)
        result = self.run_bootstrap(project, ["--offline", "--only", "python"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("符号链接", result.stderr)

    def test_symlinked_tmp_is_rejected(self):
        project = self.make_project()
        self.prepare_python(project)
        outside = Path(self.temp.name) / "outside-tmp"
        outside.mkdir()
        (project / ".state").mkdir()
        (project / ".state" / "tmp").symlink_to(outside)
        result = self.run_bootstrap(project, ["--offline", "--only", "python"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("符号链接", result.stderr)

    def test_symlinked_archive_file_is_rejected(self):
        project = self.make_project()
        name = python_basename(self.arch)
        real = Path(self.temp.name) / "real-archive.tar.gz"
        build_archive(real, "python", [("bin/python3", PYTHON_SCRIPT, 0o755)])
        (project / "bundled" / name).symlink_to(real)
        self.write_manifest(project, [self.entry("python", name, sha256_file(real))])
        result = self.run_bootstrap(project, ["--offline", "--only", "python"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("符号链接", result.stderr)

    def test_symlinked_cache_tmp_is_rejected(self):
        project = self.make_project()
        name, digest = self.make_python_archive(project)
        (project / "bundled" / name).unlink()
        self.write_manifest(project, [self.entry("python", name, digest)])
        downloads = project / ".state" / "downloads"
        downloads.mkdir(parents=True)
        (downloads / (name + ".tmp")).symlink_to(Path(self.temp.name) / "somewhere")
        result = self.run_bootstrap(project, ["--only", "python"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("符号链接", result.stderr)

    def test_symlinked_portable_with_valid_target_is_rejected(self):
        project = self.make_project()
        self.prepare_python(project)
        outside = Path(self.temp.name) / "outside-portable"
        target = outside / "python" / "bin" / "python3"
        target.parent.mkdir(parents=True)
        target.write_text(PYTHON_SCRIPT, encoding="utf-8")
        target.chmod(0o755)
        (project / ".portable").symlink_to(outside)
        result = self.run_bootstrap(project, ["--offline", "--only", "python"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("符号链接", result.stderr)

    def test_failed_upgrade_keeps_existing_runtime(self):
        project = self.make_project()
        name, digest = self.make_python_archive(project)
        self.write_manifest(project, [self.entry("python", name, digest)])
        first = self.run_bootstrap(project, ["--offline", "--only", "python"])
        self.assertEqual(first.returncode, 0, first.stderr)
        (project / "bundled" / name).unlink()
        build_archive(project / "bundled" / name, "python",
                      [("bin/python3", "#!/bin/sh\necho 3.11.0\n", 0o755)])
        bad = sha256_file(project / "bundled" / name)
        self.write_manifest(project, [self.entry("python", name, bad)])
        result = self.run_bootstrap(project, ["--offline", "--force", "--only", "python"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("校验失败", result.stderr)
        py = project / ".portable" / "python" / "bin" / "python3"
        probe = subprocess.run([str(py)], capture_output=True, text=True)
        self.assertEqual(probe.stdout.strip(), "3.12.14")

    def test_existing_non_tool_target_is_not_deleted(self):
        project = self.make_project()
        self.prepare_python(project)
        target = project / ".portable" / "python"
        target.mkdir(parents=True)
        keep = target / "keepme.txt"
        keep.write_text("user data", encoding="utf-8")
        result = self.run_bootstrap(project, ["--offline", "--only", "python"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("非本工具", result.stderr)
        self.assertTrue(keep.is_file())

    def test_install_lock_prevents_concurrent_runs(self):
        project = self.make_project()
        self.prepare_python(project)
        (project / ".state" / "tmp" / "portable-install.lock").mkdir(parents=True)
        result = self.run_bootstrap(project, ["--offline", "--only", "python"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("正在进行", result.stderr)
        self.assertFalse((project / ".portable" / "python").exists())

    def test_temp_directory_cleaned_after_install(self):
        project = self.make_project()
        self.prepare_python(project)
        result = self.run_bootstrap(project, ["--offline", "--only", "python"])
        self.assertEqual(result.returncode, 0, result.stderr)
        tmp = project / ".state" / "tmp"
        leftovers = [p.name for p in tmp.iterdir() if p.name.startswith("bootstrap.")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
