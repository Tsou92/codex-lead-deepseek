"""Allowlist selection shared by the in-place copy and the ZIP builder.

Only explicitly listed public files travel with the distribution.  Globs are
one level deep and never recurse into unknown subdirectories, so a private file
that merely lives under a packaged folder is not swept in by accident.

Runtime directories are handled separately by :func:`copy_runtime_tree`.  That
is the only place that preserves symlinks, and only relative links that resolve
inside the runtime root (portable Python's ``python3`` shim, Node's ``npm`` /
``npx`` shims).  Absolute or escaping links are refused.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path, PurePosixPath

PACKAGE_DIR = "codex-lead-deepseek"
MARKER_NAME = ".codex-lead-deepseek-install.json"
MANIFEST_NAME = "SHA256SUMS"
RUNTIME_MANIFEST_REL = "install/runtime-manifest.tsv"

# Explicit, auditable allowlist of authored public files.  Nothing outside this
# list (and the one-level globs below) is ever packaged or copied.
EXPLICIT_FILES = (
    "bin/bootstrap",
    "bin/dsh",
    "bin/import-monitor-history",
    "bin/install-codex",
    "bin/leadseek",
    "bin/monitor",
    "bin/python",
    "bin/setup",
    "bin/setup-runtime",
    "bin/test",
    "一键安装.command",
    "打开监控.command",
    "README.md",
    "CHANGELOG.md",
    "config.json",
    "使用说明.md",
    "验证记录.md",
    "先看我.txt",
    "web/index.html",
    "web/app.js",
    "web/styles.css",
    "install/runtime-manifest.tsv",
    "scripts/build-installer.py",
    "scripts/import-monitor-history.mjs",
    "skill/deepseek-delegate/SKILL.md",
    "skill/deepseek-delegate/agents/openai.yaml",
    # Public example inputs shipped with the delegate docs.
    "examples/edit-task.json",
    "examples/inspect-task.json",
    "examples/process-task.json",
    "examples/research-task.json",
    "examples/demo-project/math_utils.py",
    "examples/demo-project/test_math_utils.py",
)

# One level deep only: (directory, glob).  No recursion into subdirectories.
GLOB_FILES = (
    ("src/leadseek", "*.py"),
    ("plugins", "*.mjs"),
    ("examples", "*.py"),
    ("docs", "*.md"),
    ("tests", "*.py"),
    ("tests", "*.mjs"),
)

EXCLUDED_DIRS = frozenset({
    ".git", ".state", ".monitor-context", "runtime", "node_modules",
    "__pycache__", ".venv", "venv", "dist", ".portable", "bundled",
})
EXCLUDED_NAMES = frozenset({
    "config.local.json", ".credentials", ".credentials.yaml", ".env",
    ".npmrc", ".DS_Store",
})
EXCLUDED_SUFFIXES = (".pyc", ".pem", ".key")


class UnsafePath(ValueError):
    """Raised when a symlink or an excluded path would be written."""


def _relative(root, path):
    return path.relative_to(root).as_posix()


def _excluded(rel):
    parts = PurePosixPath(rel).parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    name = parts[-1]
    if name in EXCLUDED_NAMES or name.startswith(".env"):
        return True
    return name.endswith(EXCLUDED_SUFFIXES)


def _safe(path, root):
    """False when the path or any component below ``root`` is a symlink."""
    current = Path(root)
    for part in Path(path).relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def source_files(root):
    """Return the sorted, symlink-free allowlist for one checkout."""
    root = Path(root)
    found = set()

    def add(rel):
        rel = PurePosixPath(rel).as_posix()
        path = root / rel
        if path.is_file() and _safe(path, root) and not _excluded(rel):
            found.add(rel)

    for rel in EXPLICIT_FILES:
        add(rel)
    for base_rel, pattern in GLOB_FILES:
        base = root / base_rel
        if base.is_dir() and _safe(base, root):
            for path in sorted(base.glob(pattern)):
                if path.is_file():
                    add(_relative(root, path))
    return sorted(found)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reject_symlink_ancestors(path):
    """Refuse a path whose user-level ancestor directory is a symlink.

    Depth-one system links such as macOS ``/var -> /private/var`` are expected,
    so they are allowed; anything deeper is treated as user-controlled.
    """
    path = Path(path)
    for parent in path.parents:
        if parent.is_symlink() and parent.parent != Path("/"):
            raise UnsafePath("拒绝经过符号链接目录: " + str(parent))


def _mkdir_plain(path, dest_root):
    dest_root = Path(dest_root)
    if dest_root.is_symlink():
        raise UnsafePath("拒绝写入符号链接: " + str(dest_root))
    dest_root.mkdir(parents=True, exist_ok=True)
    current = dest_root
    for part in Path(path).relative_to(dest_root).parts:
        current = current / part
        if current.is_symlink():
            raise UnsafePath("拒绝写入符号链接: " + str(current))
        current.mkdir(exist_ok=True)


def copy_public_tree(root, dest, files=None):
    """Copy only allowlisted files; existing non-allowlisted paths stay intact."""
    root = Path(root).resolve()
    dest = Path(dest)
    reject_symlink_ancestors(dest)
    if dest.is_symlink():
        raise UnsafePath("拒绝把安装目录指向符号链接: " + str(dest))
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for rel in (source_files(root) if files is None else sorted(files)):
        if _excluded(rel):
            raise UnsafePath("拒绝复制被排除的路径: " + rel)
        source = root / rel
        if source.is_symlink() or not source.is_file():
            continue
        target = dest / rel
        _mkdir_plain(target.parent, dest)
        if target.is_symlink():
            raise UnsafePath("拒绝覆盖符号链接: " + str(target))
        data = source.read_bytes()
        mode = stat.S_IMODE(source.stat().st_mode)
        target.write_bytes(data)
        os.chmod(target, mode)
        copied.append(rel)
    return copied


def _copy_symlink(item, target, boundary, dest_root):
    link = os.readlink(str(item))
    if os.path.isabs(link):
        raise UnsafePath("拒绝绝对符号链接: " + str(item))
    resolved = (item.parent / link).resolve()
    try:
        resolved.relative_to(boundary)
    except ValueError:
        raise UnsafePath("符号链接指向运行时之外: " + str(item))
    _mkdir_plain(target.parent, dest_root)
    if target.exists() or target.is_symlink():
        raise UnsafePath("拒绝覆盖: " + str(target))
    os.symlink(link, str(target))


def copy_runtime_tree(src, dst, boundary=None):
    """Copy a portable runtime, preserving only internal relative symlinks."""
    src = Path(src)
    if src.is_symlink() or not src.is_dir():
        raise UnsafePath("运行时目录不可用: " + str(src))
    boundary = Path(boundary).resolve() if boundary is not None else src.resolve()
    dst = Path(dst)
    reject_symlink_ancestors(dst)
    if dst.is_symlink():
        raise UnsafePath("拒绝写入符号链接: " + str(dst))
    dst.mkdir(parents=True, exist_ok=True)
    for current, dirs, files in os.walk(str(src), followlinks=False, topdown=True):
        current_path = Path(current)
        dirs.sort()
        files.sort()
        keep = []
        for name in dirs:
            item = current_path / name
            target = dst / item.relative_to(src)
            if item.is_symlink():
                _copy_symlink(item, target, boundary, dst)
            else:
                _mkdir_plain(target, dst)
                keep.append(name)
        dirs[:] = keep
        for name in files:
            item = current_path / name
            target = dst / item.relative_to(src)
            if item.is_symlink():
                _copy_symlink(item, target, boundary, dst)
            else:
                _mkdir_plain(target.parent, dst)
                if target.is_symlink():
                    raise UnsafePath("拒绝覆盖符号链接: " + str(target))
                shutil.copy2(str(item), str(target))
    return dst
