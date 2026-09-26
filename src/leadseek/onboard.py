"""Cross-Mac onboarding wizard for a fresh checkout.

It copies the public tree to a user-writable location, brings along portable
Python/Node and bundled runtime archives when present, runs ``bin/setup`` and
finally ``bin/leadseek activate``.  The DeepSeek key is only ever read from the
user's own TTY with :func:`getpass` and written straight to a new 0600 file; it
is never passed on a command line, through the environment, to stdout or into
a log.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid
import warnings

from . import configuration
from . import distribution

DEFAULT_RELATIVE_DESTINATION = Path("Applications") / "Codex-Lead-DeepSeek"
DEFAULT_LOCAL_BIN = Path(".local") / "bin" / "leadseek"
PORTABLE_DIRS = (".portable/python", ".portable/node")
BUNDLED_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".zip")
# Official entry point shown when the Codex app is missing; this is a hint, not
# an installation performed by this tool.
CODEX_APP_URL = "https://openai.com/codex/"
CODEX_APP_PATHS = (
    "/Applications/Codex.app",
    "/Applications/Codex Beta.app",
)


def build_credentials_document(key):
    """JSON document that is also valid YAML, shaped for the Harness."""
    return {"version": 1, "refs": {"DEEPSEEK_API_KEY": key}}


def _marker_document():
    return {"product": distribution.PACKAGE_DIR, "version": 1}


def _valid_marker(dest):
    marker = Path(dest) / distribution.MARKER_NAME
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return data.get("product") == distribution.PACKAGE_DIR and data.get("version") == 1


def validate_destination(dest):
    """Return ``create``/``update`` or refuse an unrelated, non-empty directory."""
    dest = Path(dest)
    distribution.reject_symlink_ancestors(dest)
    if dest.is_symlink():
        raise distribution.UnsafePath("拒绝把安装目录指向符号链接: " + str(dest))
    if dest.exists():
        if not dest.is_dir():
            raise ValueError("安装位置已被同名文件占用: " + str(dest))
        if _valid_marker(dest):
            return "update"
        if not any(dest.iterdir()):
            return "create"
        raise ValueError("目标目录非空且不是本工具的安装标记: " + str(dest)
                         + "；请换用空目录或 --destination")
    return "create"


def ensure_destination(dest, mode):
    dest = Path(dest)
    created = not dest.exists()
    dest.mkdir(parents=True, exist_ok=True)
    if created:
        os.chmod(dest, 0o700)
        mode = "create"
    marker = dest / distribution.MARKER_NAME
    if marker.is_symlink():
        raise distribution.UnsafePath("拒绝覆盖符号链接: " + str(marker))
    if not _valid_marker(dest):
        marker.write_text(json.dumps(_marker_document(), indent=2) + "\n", encoding="utf-8")
        os.chmod(marker, 0o600)
    return mode


def _points_into(link, source):
    link = Path(link)
    if not link.is_symlink():
        return False
    try:
        link.resolve().relative_to(Path(source).resolve())
    except ValueError:
        return False
    return True


def resolve_install_root(source, destination=None, in_place=False, home=None, local_bin=None):
    """Pick the install root, reusing the current checkout when already linked."""
    source = Path(source).resolve()
    if in_place:
        return source
    if destination:
        return Path(destination).expanduser()
    home = Path(home).expanduser() if home else Path("~").expanduser()
    link = Path(local_bin) if local_bin else (home / DEFAULT_LOCAL_BIN)
    if _points_into(link, source):
        return source
    return home / DEFAULT_RELATIVE_DESTINATION


def _swap_runtime(src, target):
    """Copy a runtime to a unique temp sibling, then swap it into place.

    Both the staging and the backup directory carry a unique suffix, so a
    pre-existing ``*.new-*``/``*.old*`` entry is never reused or removed.  If
    the final switch fails after the previous runtime was moved aside, that
    runtime is renamed back before the error is re-raised, and only the
    temporary directories created by this call are cleaned up.
    """
    target = Path(target)
    parent = target.parent
    distribution.reject_symlink_ancestors(parent)
    parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise distribution.UnsafePath("拒绝覆盖符号链接: " + str(target))
    if target.exists() and not target.is_dir():
        raise ValueError("运行时目标已被同名文件占用: " + str(target))
    temp = Path(tempfile.mkdtemp(prefix=target.name + ".new-", dir=str(parent)))
    try:
        distribution.copy_runtime_tree(src, temp, boundary=src)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    backup = parent / (target.name + ".old-" + uuid.uuid4().hex)
    moved = False
    try:
        if target.exists():
            os.rename(str(target), str(backup))
            moved = True
        os.rename(str(temp), str(target))
    except BaseException:
        if moved and not target.exists():
            # Roll the previous runtime back into place; if this also fails we
            # deliberately keep the uniquely named backup instead of deleting it.
            os.rename(str(backup), str(target))
        shutil.rmtree(temp, ignore_errors=True)
        raise
    if moved:
        # This backup was created by this call, so it is safe to clean up.
        shutil.rmtree(backup, ignore_errors=True)


def copy_portable_runtime(source, dest):
    """Bring portable Python/Node and bundled archives so a moved package runs."""
    source = Path(source)
    dest = Path(dest)
    copied = {}
    for rel in PORTABLE_DIRS:
        src = source / rel
        if src.is_dir() and not src.is_symlink():
            _swap_runtime(src, dest / rel)
            copied[rel] = True
    bundled = source / "bundled"
    if bundled.is_dir() and not bundled.is_symlink():
        target_dir = dest / "bundled"
        distribution.reject_symlink_ancestors(target_dir)
        if target_dir.is_symlink():
            raise distribution.UnsafePath("拒绝把 bundled 指向符号链接: " + str(target_dir))
        target_dir.mkdir(parents=True, exist_ok=True)
        for item in sorted(bundled.iterdir()):
            if not item.is_file() or item.is_symlink() or not item.name.endswith(BUNDLED_SUFFIXES):
                continue
            target = target_dir / item.name
            if target.is_symlink():
                raise distribution.UnsafePath("拒绝覆盖符号链接: " + str(target))
            target.write_bytes(item.read_bytes())
            os.chmod(target, 0o600)
            copied["bundled/" + item.name] = True
    return copied


def credentials_path_for(root):
    root = Path(root)
    common = configuration.read_config_file(root / configuration.CONFIG_NAME)
    local_path = root / configuration.LOCAL_CONFIG_NAME
    local = configuration.read_config_file(local_path) if local_path.is_file() else {}
    merged = configuration.merge_config(common, local)
    return configuration.resolve_credentials_path(merged.get("credentials_path"), root)


def write_credentials(path, key):
    """Create the credential file once, 0600, without following symlinks."""
    path = Path(path)
    if not isinstance(key, str) or not key or "\n" in key or "\r" in key:
        raise ValueError("API key 不能为空或包含换行")
    parent = path.parent
    distribution.reject_symlink_ancestors(parent)
    if parent.is_symlink():
        raise distribution.UnsafePath("拒绝写入符号链接目录: " + str(parent))
    if path.is_symlink():
        raise distribution.UnsafePath("拒绝写入符号链接: " + str(path))
    created_parent = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if created_parent:
        os.chmod(parent, 0o700)
    payload = (json.dumps(build_credentials_document(key), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags, 0o600)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("写入凭据文件失败")
            remaining = remaining[written:]
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _environment(environ):
    environment = dict(os.environ if environ is None else environ)
    environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return environment


def default_prompter():
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        value = getpass.getpass("请粘贴 DeepSeek API key（回车跳过，输入不显示）：")
    if "\n" in value or "\r" in value:
        raise ValueError("API key 不能包含换行")
    return value.strip()


def _isatty(stdin):
    stdin = sys.stdin if stdin is None else stdin
    try:
        return bool(stdin.isatty())
    except (AttributeError, ValueError):
        return False


def run_process(command, root, environ):
    return subprocess.run(command, cwd=str(root), env=environ,
                          capture_output=True, text=True)


def resolve_python(install_root, python_executable=None):
    """Prefer the copied portable interpreter so the download dir can vanish."""
    if python_executable:
        return str(python_executable)
    candidate = Path(install_root) / ".portable" / "python" / "bin" / "python3"
    if candidate.is_file() and os.access(str(candidate), os.X_OK):
        return str(candidate)
    return sys.executable


def _portable_node(install_root):
    candidate = Path(install_root) / ".portable" / "node" / "bin" / "node"
    if candidate.is_file() and os.access(str(candidate), os.X_OK):
        return str(candidate)
    return None


def _absolute_destination(destination):
    if destination is None:
        return None
    # ``abspath`` normalizes the path lexically without resolving symlinks, so a
    # symlinked destination is still visible to the symlink audit in
    # :func:`validate_destination`/``copy_public_tree``.
    return Path(os.path.abspath(os.path.expanduser(str(destination))))


def onboard(source=None, destination=None, in_place=False, node=None,
            skip_runtime=False, no_open=False, non_interactive=False,
            environ=None, home=None, local_bin=None, stdin=None,
            runner=None, prompter=None, python_executable=None):
    """Run the full wizard and return a structured, secret-free result."""
    source = Path(source).resolve() if source else Path(__file__).resolve().parents[2]
    runner = run_process if runner is None else runner
    destination = _absolute_destination(destination)
    install_root = resolve_install_root(source, destination, in_place, home, local_bin)
    result = {"source": str(source), "install_root": str(install_root),
              "steps": [], "warnings": [], "ok": True, "portable": {}}

    if install_root == source:
        result["mode"] = "in-place"
    else:
        mode = validate_destination(install_root)
        ensure_destination(install_root, mode)
        distribution.copy_public_tree(source, install_root)
        result["portable"] = copy_portable_runtime(source, install_root)
        result["mode"] = mode

    python = resolve_python(install_root, python_executable)
    # Point setup at the copied portable Node only when the install root is a
    # real copy; when it is the source checkout itself we must not hand setup a
    # runtime directory it may move or delete, so an explicit --node wins.
    if Path(install_root).resolve() == source:
        chosen_node = node
    else:
        chosen_node = node or _portable_node(install_root)

    command = [python, str(install_root / "bin" / "setup")]
    if chosen_node:
        command += ["--node", chosen_node]
    if skip_runtime:
        command += ["--skip-runtime"]
    completed = runner(command, install_root, _environment(environ))
    result["steps"].append("setup")
    result["setup_exit_code"] = completed.returncode
    if completed.returncode != 0:
        result["ok"] = False
        result["setup_error"] = ("setup 未通过（退出码 %d），已停止后续凭据与 activate 步骤；"
                                 "输出已省略以免泄露本机信息。" % completed.returncode)
        return result

    try:
        credentials_path = credentials_path_for(install_root)
    except (OSError, ValueError) as error:
        credentials_path = None
        result["warnings"].append("无法确定凭据路径: " + str(error))
    if credentials_path:
        result["credentials_path"] = credentials_path
        if Path(credentials_path).is_file():
            result["credentials"] = "exists"
        elif non_interactive or not _isatty(stdin):
            result["credentials"] = "pending"
            result["warnings"].append("非交互终端未读取 API key，安装后可重新运行本向导配置。")
        else:
            key = (prompter or default_prompter)()
            if not key:
                result["credentials"] = "pending-skipped"
            else:
                write_credentials(credentials_path, key)
                result["credentials"] = "created"

    activate_command = [python, str(install_root / "bin" / "leadseek"), "activate"]
    if no_open:
        activate_command += ["--no-open"]
    activated = runner(activate_command, install_root, _environment(environ))
    result["steps"].append("activate")
    result["activate_exit_code"] = activated.returncode
    if activated.returncode != 0:
        result["ok"] = False
        result["activate_error"] = "activate 未成功（退出码 %d）。" % activated.returncode
    return result


def codex_app_installed(home=None):
    home = Path(home).expanduser() if home else Path("~").expanduser()
    candidates = [Path(path) for path in CODEX_APP_PATHS] + [home / "Applications" / "Codex.app"]
    return any(path.exists() for path in candidates)


def summary_lines(result, home=None):
    status = result.get("credentials")
    labels = {
        "exists": "凭据文件已存在（未读取、未改动，不代表 API 已验证）",
        "created": "已新建 0600 凭据文件（不代表 API 已验证）",
        "pending": "待配置（非交互，未读取密钥）",
        "pending-skipped": "待配置（已跳过输入）",
    }
    setup_ok = result.get("ok") and "setup" in result.get("steps", [])
    activate_ok = result.get("activate_exit_code") == 0
    lines = [
        "[1/3] 安装位置：" + result["install_root"] + "（%s）" % result.get("mode", "未知"),
        "[2/3] 运行时与接入：" + ("setup 完成（含 doctor 自检）" if setup_ok else "setup 未完成"),
        "[3/3] 凭据与激活：" + labels.get(status, "待配置")
        + "；activate " + ("完成" if activate_ok else "未成功"),
    ]
    if result.get("setup_error"):
        lines.append("      " + result["setup_error"])
    if result.get("activate_error"):
        lines.append("      " + result["activate_error"])
    for warning in result.get("warnings", []):
        lines.append("      提示：" + warning)
    portable = result.get("portable") or {}
    if portable:
        lines.append("      已随包携带：" + "、".join(sorted(portable)))
    lines.append("")
    lines.append("最终清单：")
    lines.append("- doctor 只确认安装结构，未验证 DeepSeek API。")
    if status in ("exists", "created"):
        lines.append("- 凭据文件已就位（仅表示文件存在，未验证密钥有效）。")
    else:
        lines.append("- 尚未配置 DeepSeek API key，委派功能未就绪；可重新运行本安装器配置。")
    if not codex_app_installed(home):
        lines.append("- 默认位置未检测到 Codex；若尚未安装，请从官方入口安装并登录：" + CODEX_APP_URL)
        lines.append("  （本工具未安装 Codex，也未验证你的账号或 API。）")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m leadseek.onboard",
        description="跨 Mac 一站式安装向导（默认复制到 ~/Applications/Codex-Lead-DeepSeek）。")
    parser.add_argument("--source", help="待复制的项目根目录；缺省用本模块所在安装")
    parser.add_argument("--destination", help="安装目录；缺省 ~/Applications/Codex-Lead-DeepSeek")
    parser.add_argument("--in-place", action="store_true", help="直接在当前源码目录就地安装")
    parser.add_argument("--node", help="传给 bin/setup 的 Node 可执行文件")
    parser.add_argument("--skip-runtime", action="store_true", help="已有正确运行时，只重接入")
    parser.add_argument("--no-open", action="store_true", help="activate 时不打开浏览器")
    parser.add_argument("--non-interactive", action="store_true", help="禁止任何读取密钥的交互")
    args = parser.parse_args(argv)
    try:
        result = onboard(source=args.source, destination=args.destination, in_place=args.in_place,
                         node=args.node, skip_runtime=args.skip_runtime, no_open=args.no_open,
                         non_interactive=args.non_interactive)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("安装未完成：" + str(error), file=sys.stderr)
        return 1
    for line in summary_lines(result):
        print(line)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
