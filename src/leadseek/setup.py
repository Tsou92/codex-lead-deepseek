"""One-shot, machine-local setup for a fresh Git clone.

Setup only writes under the checkout (``config.local.json`` and ``runtime/``)
or, when the user runs it, the already-reviewed global entry links.  It never
downloads Homebrew, uses sudo, starts a service, or calls the model API.
Tests call :func:`configure` with a temporary root and a mocked
``subprocess.run``, so no real install or HOME write happens.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import configuration
from . import runtime_install


ROOT = Path(__file__).resolve().parents[2]
MIN_PYTHON = (3, 9)
MIN_NODE_MAJOR = 24


def python_ok(version_info=None):
    version = tuple(sys.version_info[:2]) if version_info is None else tuple(version_info)
    if version < MIN_PYTHON:
        raise ValueError("需要 Python 3.9+，当前是 %d.%d" % (version[0], version[1]))
    return version


def node_major(version_text):
    text = (version_text or "").strip().lstrip("v")
    head = text.split(".")[0]
    if not head.isdigit():
        raise ValueError("无法识别 Node 版本: " + repr(version_text))
    return int(head)


def _detail(completed):
    return (completed.stderr or completed.stdout or "").strip()


def check_node(node):
    completed = subprocess.run([node, "--version"], capture_output=True, text=True)
    if completed.returncode != 0:
        raise ValueError("无法运行 Node（" + node + "）：" + (_detail(completed) or ("退出码 %d" % completed.returncode)))
    major = node_major(completed.stdout)
    if major < MIN_NODE_MAJOR:
        raise ValueError("需要 Node 24+，当前是 " + completed.stdout.strip())
    return completed.stdout.strip()


def find_npm(node, environ=None):
    environ = os.environ if environ is None else environ
    sibling = Path(node).parent / "npm"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    found = shutil.which("npm", path=environ.get("PATH", ""))
    if not found:
        raise ValueError("找不到 npm；请安装 Node 24（含 npm）后重试")
    return found


def write_local_config(root, local, values):
    """Merge machine overrides into config.local.json, keeping other keys, mode 0600.

    Delegates the actual write to :func:`runtime_install.atomic_write_json`, so
    both setup paths share the same symlink check, same-directory temp file and
    0600 handling.  Other keys, including an existing credentials path, stay.
    """
    merged = dict(local)
    merged.update(values)
    path = Path(root) / configuration.LOCAL_CONFIG_NAME
    runtime_install.atomic_write_json(path, merged)
    return merged


def _environment(environ):
    environment = dict(os.environ if environ is None else environ)
    environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return environment


def run_step(command, root, environ):
    """Run a project script with an argument vector (no shell), paths may contain spaces."""
    completed = subprocess.run(command, cwd=str(root), env=_environment(environ),
                               capture_output=True, text=True)
    if completed.returncode != 0:
        label = Path(str(command[-1])).name if command else "步骤"
        raise ValueError("步骤失败（" + label + "，退出码 %d）：%s"
                         % (completed.returncode, _detail(completed) or "无错误输出"))


def configure(root=None, node=None, credentials=None, skip_runtime=False, environ=None, version_info=None):
    """Check the machine, record local overrides and (re)install the runtime and links."""
    root = Path(root).resolve() if root is not None else ROOT
    python_ok(version_info)
    common = configuration.read_config_file(root / configuration.CONFIG_NAME)
    local_path = root / configuration.LOCAL_CONFIG_NAME
    runtime_install.reject_unsafe_config_path(local_path)
    local = configuration.read_config_file(local_path) if local_path.is_file() else {}
    effective = configuration.merge_config(common, local)
    if node:
        effective["node"] = node
    if credentials:
        effective["credentials_path"] = credentials

    resolved_node = configuration.resolve_node(effective.get("node"), environ)
    resolved_credentials = configuration.resolve_credentials_path(effective.get("credentials_path"), root)
    node_version = check_node(resolved_node)
    find_npm(resolved_node, environ)

    runtime_cli = root / configuration.RUNTIME_CLI
    if skip_runtime and not runtime_cli.is_file():
        raise ValueError("未找到固定 Harness 运行时: " + str(runtime_cli)
                         + "；请去掉 --skip-runtime 重新安装，不要只重接入全局命令")

    merged_local = write_local_config(root, local, {
        "node": resolved_node,
        "credentials_path": resolved_credentials,
    })

    steps = []
    if not skip_runtime:
        run_step([sys.executable, str(root / "bin/setup-runtime")], root, environ)
        steps.append("setup-runtime")
        # setup-runtime records the actual version/source it reused or installed.
        if local_path.is_file():
            merged_local = configuration.read_config_file(local_path)
    missing = runtime_install.missing_required_packages(root)
    if missing:
        raise ValueError("Harness 运行时缺少关键包: " + "、".join(missing)
                         + "；请检查安装输出，不会自动覆盖或降级")
    run_step([sys.executable, str(root / "bin/install-codex")], root, environ)
    steps.append("install-codex")

    doctor = subprocess.run([sys.executable, str(root / "bin/leadseek"), "doctor"],
                            cwd=str(root), env=_environment(environ),
                            capture_output=True, text=True)
    return {
        "root": str(root),
        "node": resolved_node,
        "node_version": node_version,
        "credentials_path": resolved_credentials,
        "local_config": str(local_path),
        "local_keys": sorted(merged_local),
        "steps": steps,
        "doctor": doctor.stdout.strip(),
        "doctor_stderr": doctor.stderr.strip(),
        "doctor_exit_code": doctor.returncode,
        "runtime_version": merged_local.get("runtime_version"),
        "runtime_recorded_matches": _recorded_matches(merged_local, doctor.stdout),
    }


def _recorded_matches(local, doctor_output):
    """Compare doctor's actual harness version with the recorded local version."""
    recorded = local.get("runtime_version")
    if not recorded or str(recorded) == "latest":
        return True
    try:
        health = json.loads(doctor_output)
    except (TypeError, ValueError):
        return False
    actual = health.get("actual") or health.get("harness_version") if isinstance(health, dict) else None
    if not actual:
        return False
    return str(actual).strip().lstrip("v") == str(recorded).strip().lstrip("v")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="setup",
        description="为当前 Git clone 配置本机运行环境（不下载 Homebrew、不使用 sudo、不启动服务）。")
    parser.add_argument("--node", help="本机 Node 可执行文件路径或命令名；缺省沿用 config.json 的 auto")
    parser.add_argument("--credentials", help="本机 DeepSeek 凭据文件路径；只记录路径，不读取内容")
    parser.add_argument("--skip-runtime", action="store_true", help="Harness 已装好时只重接入，不重装运行时")
    args = parser.parse_args(argv)
    try:
        result = configure(node=args.node, credentials=args.credentials, skip_runtime=args.skip_runtime)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("安装未完成：" + str(error), file=sys.stderr)
        return 1
    print("环境检查通过：Python 与 " + result["node_version"] + " 可用")
    print("本机覆盖已写入 " + result["local_config"] + "（权限 0600），保留键：" + ", ".join(result["local_keys"]))
    print("已完成步骤：" + "、".join(result["steps"]) if result["steps"] else "未执行安装步骤")
    print("凭据路径：" + result["credentials_path"])
    if not Path(result["credentials_path"]).is_file():
        print("提示：凭据文件尚不存在，请在本机单独配置 DeepSeek 认证；安装成功不代表 API 已验证。")
    if result["doctor"]:
        print("doctor：\n" + result["doctor"])
    else:
        print("doctor 未返回内容（退出码 " + str(result["doctor_exit_code"]) + "）")
    if result["doctor_exit_code"] != 0:
        detail = result.get("doctor_stderr") or result["doctor"] or ""
        print("doctor 自检失败（退出码 %d）：%s"
              % (result["doctor_exit_code"], detail.strip() or "请检查运行时与 Node 环境"),
              file=sys.stderr)
        print("安装未完成：环境自检未通过，请按上面提示修复后重新运行 bin/setup。", file=sys.stderr)
        return 1
    try:
        health = json.loads(result["doctor"])
    except (TypeError, ValueError):
        health = None
    required = ("version_matches", "skill_linked", "cli_linked")
    if not isinstance(health, dict) or any(health.get(key) is not True for key in required):
        print("安装未完成：doctor 的版本或全局入口检查未通过，请按输出修复后重新运行 bin/setup。",
              file=sys.stderr)
        return 1
    if result.get("runtime_recorded_matches") is False:
        print("安装未完成：doctor 返回的实际 Harness 版本与 config.local.json 记录不一致，"
              "请按 doctor 的 expected/actual/version_policy 字段核对。", file=sys.stderr)
        return 1
    if result.get("runtime_version"):
        print("运行时实际版本：" + str(result["runtime_version"]) + "（来源见 config.local.json runtime_source）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
