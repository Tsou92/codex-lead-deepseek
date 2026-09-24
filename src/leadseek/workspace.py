"""Stage explicit inputs and apply reviewed outputs without overwriting new work."""

import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat


IGNORE_DIRS = {".git", ".state", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
MAX_FILES = 2000
MAX_BYTES = 50 * 1024 * 1024


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_path(value, allow_root=False):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("文件范围必须是非空、使用 / 的项目相对路径")
    parts = PurePosixPath(value).parts
    if PurePosixPath(value).is_absolute() or ".." in parts:
        raise ValueError("文件范围不能是绝对路径或包含 ..")
    path = str(PurePosixPath(value))
    if path == "." and not allow_root:
        raise ValueError("写入范围不能是整个项目，请指定文件或子目录")
    return path + ("/" if value.endswith("/") and path != "." else "")


def excluded(rel):
    p = PurePosixPath(rel)
    return (any(part in IGNORE_DIRS for part in p.parts)
            or p.name == ".env" or p.name.startswith(".env.")
            or p.name.startswith("config.local.json")
            or p.name in {".credentials.yaml", ".npmrc", ".DS_Store"}
            or p.suffix in {".pem", ".key"})


def safe_path(root, rel):
    """Reject symlinks in every path component, including missing-file parents."""
    root = Path(root).resolve(strict=True)
    rel = normalize_path(rel, allow_root=True).rstrip("/")
    current = root
    for part in PurePosixPath(rel).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("文件范围包含符号链接: " + rel)
    if not current.resolve().is_relative_to(root):
        raise ValueError("文件路径越出项目: " + rel)
    return current


def writable(rel, scopes):
    return any(rel == scope.rstrip("/") or (scope.endswith("/") and rel.startswith(scope))
               for scope in scopes)


def inventory(root):
    files = {}
    total_bytes = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        forbidden = set(dirs) & {".git", ".state", "node_modules", ".venv", "venv"}
        if forbidden:
            raise ValueError("暂存区出现未授权内部目录: " + ", ".join(sorted(forbidden)))
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS)
        for name in dirs + names:
            path = Path(base) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ValueError("暂存区不允许符号链接: " + rel)
        for name in sorted(names):
            path = Path(base) / name
            rel = path.relative_to(root).as_posix()
            if name == ".DS_Store":
                continue
            if not path.is_file():
                raise ValueError("暂存区只允许普通文件: " + rel)
            total_bytes += path.stat().st_size
            if len(files) >= MAX_FILES or total_bytes > MAX_BYTES:
                raise ValueError("暂存文件超过 2000 个或 50 MiB，请缩小任务范围")
            files[rel] = {"sha256": digest(path), "mode": stat.S_IMODE(path.stat().st_mode) & 0o777}
    return files


def prepare(run_dir, task):
    source = Path(task["workspace"]).resolve(strict=True)
    stage = run_dir / "workspace"
    baseline = run_dir / "baseline"
    stage.mkdir()
    baseline.mkdir()
    scopes = []
    for raw in task["write_paths"]:
        rel = normalize_path(raw)
        candidate = safe_path(source, rel)
        if excluded(rel):
            raise ValueError("不能委派凭据、环境配置或内部状态路径: " + rel)
        if candidate.is_dir() and not rel.endswith("/"):
            rel += "/"
        scopes.append(rel)
    task["write_paths"] = scopes
    selected = set()
    for raw in task["read_paths"] + scopes:
        rel = normalize_path(raw, allow_root=True).rstrip("/")
        candidate = safe_path(source, rel)
        if excluded(rel):
            raise ValueError("不会复制凭据或内部状态路径: " + rel)
        if candidate.is_file():
            selected.add(rel)
        elif candidate.is_dir():
            for base, dirs, names in os.walk(candidate, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not (Path(base) / d).is_symlink())
                for name in names:
                    file = Path(base) / name
                    child = file.relative_to(source).as_posix()
                    if not excluded(child) and not file.is_symlink() and file.is_file():
                        selected.add(child)
        elif rel not in [s.rstrip("/") for s in scopes]:
            raise ValueError("读取路径不存在: " + rel)
    total_bytes = 0
    for rel in sorted(selected):
        file = safe_path(source, rel)
        total_bytes += file.stat().st_size
        if len(selected) > MAX_FILES or total_bytes > MAX_BYTES:
            raise ValueError("输入超过 2000 个文件或 50 MiB，请缩小任务范围")
        for destination in (stage / rel, baseline / rel):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, destination)
            destination.chmod(stat.S_IMODE(file.stat().st_mode) & 0o777)
    for scope in scopes:
        destination = stage / scope.rstrip("/")
        (destination if scope.endswith("/") else destination.parent).mkdir(parents=True, exist_ok=True)
    before = inventory(baseline)
    (run_dir / "baseline.json").write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
    return before


def collect_changes(run_dir, task):
    before = json.loads((run_dir / "baseline.json").read_text(encoding="utf-8"))
    after = inventory(run_dir / "workspace")
    changes, violations = [], []
    with (run_dir / "changes.patch").open("w", encoding="utf-8") as patch:
        for rel in sorted(before.keys() | after.keys()):
            if before.get(rel) == after.get(rel):
                continue
            if not writable(rel, task["write_paths"]) or excluded(rel):
                violations.append(rel)
            changes.append({"path": rel, "before": before.get(rel), "after": after.get(rel)})
            old = (run_dir / "baseline" / rel).read_bytes() if rel in before else b""
            new = (run_dir / "workspace" / rel).read_bytes() if rel in after else b""
            try:
                if b"\x00" in old or b"\x00" in new:
                    raise UnicodeError()
                lines = difflib.unified_diff(old.decode("utf-8").splitlines(keepends=True),
                                             new.decode("utf-8").splitlines(keepends=True),
                                             fromfile="a/" + rel, tofile="b/" + rel)
                for line in lines:
                    patch.write(line)
                    if not line.endswith("\n"):
                        patch.write("\n\\ No newline at end of file\n")
            except UnicodeError:
                patch.write("Binary file changed: " + rel + "\n")
    (run_dir / "changes.json").write_text(json.dumps(changes, ensure_ascii=False, indent=2), encoding="utf-8")
    return changes, violations


def carry_revision(previous, destination, task):
    """Keep allowed prior work while retaining the original live-file baseline."""
    original = json.loads((previous / "baseline.json").read_text(encoding="utf-8"))
    fresh = json.loads((destination / "baseline.json").read_text(encoding="utf-8"))
    if original != fresh:
        raise ValueError("原项目已变化，不能沿用旧暂存结果；请重新委派并说明需要保留的改动")
    after = inventory(previous / "workspace")
    for rel in sorted(original.keys() | after.keys()):
        if not writable(rel, task["write_paths"]) or excluded(rel):
            continue
        target = safe_path(destination / "workspace", rel)
        if rel in after:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(safe_path(previous / "workspace", rel), target)
        elif target.exists():
            target.unlink()


def apply_changes(run_dir):
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    if result["status"] != "completed":
        raise ValueError("只有执行成功且范围检查通过的任务才能应用")
    if (run_dir / "applied.json").exists():
        raise ValueError("这个任务已经应用过")
    task = json.loads((run_dir / "task.json").read_text(encoding="utf-8"))
    changes = json.loads((run_dir / "changes.json").read_text(encoding="utf-8"))
    source = Path(task["workspace"]).resolve(strict=True)
    baseline = json.loads((run_dir / "baseline.json").read_text(encoding="utf-8"))
    # Check every supplied context file, not just the files the worker changed.
    for rel, original in baseline.items():
        live = safe_path(source, rel)
        if (not live.is_file() or digest(live) != original["sha256"]
                or stat.S_IMODE(live.stat().st_mode) & 0o777 != original["mode"]):
            raise ValueError("原项目已变化，拒绝覆盖；请重新委派: " + rel)
    for change in changes:
        rel = change["path"]
        if not writable(rel, task["write_paths"]) or excluded(rel):
            raise ValueError("改动越出授权范围: " + rel)
        live = safe_path(source, rel)
        if change["before"] is None and live.exists():
            raise ValueError("目标文件已被其他工作创建: " + rel)
        if change["after"]:
            staged = safe_path(run_dir / "workspace", rel)
            if not staged.is_file() or digest(staged) != change["after"]["sha256"]:
                raise ValueError("暂存结果已变化，请重新生成检查结果: " + rel)
    applied = []
    try:
        for change in changes:
            rel = change["path"]
            destination = safe_path(source, rel)
            applied.append(change)
            if change["after"] is None:
                destination.unlink()
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(run_dir / "workspace" / rel, destination)
                destination.chmod(change["after"]["mode"])
    except OSError:
        for change in reversed(applied):
            destination = safe_path(source, change["path"])
            if change["before"]:
                shutil.copyfile(run_dir / "baseline" / change["path"], destination)
                destination.chmod(change["before"]["mode"])
            elif destination.exists():
                destination.unlink()
        raise
    receipt = {"workspace": str(source), "applied_files": [c["path"] for c in changes]}
    (run_dir / "applied.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt
