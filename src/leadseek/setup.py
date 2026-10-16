"""One-shot, machine-local setup for a fresh Git clone.

Setup only writes under the checkout (``config.local.json`` and ``runtime/``)
or, when the user runs it, the already-reviewed global entry links.  It never
downloads Homebrew, uses sudo, starts a service, or calls the model API.
Tests call :func:`configure` with a temporary root and a mocked
``subprocess.run``, so no real install or HOME write happens.

Two execution backends exist.  ``headless`` (the legacy default) needs Node,
``runtime/`` and a credentials file.  ``desktop`` only checks Python, installs
the same global entry links and then reads the desktop state from ``bin/leadseek
doctor``: it never touches Node/npm, ``runtime/``, ``config.local.json``,
credentials, models or plugins, so a desktop that is not running yet still
installs successfully and is reported as ``pending-start``.
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

HEADLESS_BACKEND = "headless"
DESKTOP_BACKEND = "desktop"
DEFAULT_DESKTOP_URL = "http://127.0.0.1:3080"
# Hints only: this tool never downloads or installs the desktop app itself.
DESKTOP_APP_HINT = ("未检测到 DSH 桌面版（DSH.app）；请先从官方入口安装并登录 DSH 桌面版，"
                    "再重新运行 bin/setup。")
NO_HEADLESS_FALLBACK = ("不会退回独立 headless 运行时：desktop 后端只连接本机 DSH 桌面版，"
                        "不安装或比对 Node/runtime/，也不读取 DeepSeek API key。")
DESKTOP_NOTES = ("桌面后端只检查 Python：不解析 Node/npm，不安装或比对本地 Harness 运行时，"
                 "也不写入或校验凭据路径与模型配置。")


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


def resolve_backend(config):
    return configuration.resolve_backend(config)


def _python_label(version):
    return "%d.%d" % (version[0], version[1])


def _run_doctor(root, environ):
    """Run ``bin/leadseek doctor``; a non-zero exit code never raises here."""
    command = [sys.executable, str(Path(root) / "bin/leadseek"), "doctor"]
    try:
        return subprocess.run(command, cwd=str(root), env=_environment(environ),
                              capture_output=True, text=True)
    except OSError as error:
        return subprocess.CompletedProcess(command, 127, stdout="",
                                           stderr="无法执行 doctor：" + str(error))


def doctor_status(doctor_stdout):
    """Parse doctor output; only a JSON object counts, anything else is ``None``."""
    try:
        parsed = json.loads(doctor_stdout)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


# A flag may arrive as a JSON boolean or as an enumerated/stringified value.
# ``bool("false")`` is ``True``, so a stringified "false" must never be read as
# "the desktop app is installed".
_TRUE_WORDS = frozenset({"true", "1", "yes", "on", "installed", "present"})
_FALSE_WORDS = frozenset({"", "false", "0", "no", "off", "missing", "absent",
                          "not-installed", "none", "null"})


def _tri_state(value):
    """Normalize a reported flag to ``True``/``False``, or ``None`` when unclear."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value) if value in (0, 1) else None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    return None


def desktop_app_installed(status):
    """Tri-state DSH.app signal: ``True``/``False`` when reported, else ``None``.

    ``None`` means "unknown" and is never treated as a failure, because an older
    doctor may not report the desktop app at all.  Only real booleans, ``0``/
    ``1`` and the recognized words above count as definitive, so an unexpected
    value can never silently claim that the desktop app is installed.
    """
    if not isinstance(status, dict):
        return None
    if "desktop_app_installed" in status:
        return _tri_state(status["desktop_app_installed"])
    if "desktop_app_path" in status:
        value = status["desktop_app_path"]
        if isinstance(value, str):
            return bool(value.strip())
        return bool(value)
    return None


def _status_field(status, key):
    return status.get(key) if isinstance(status, dict) else None


def _desktop_url(effective):
    value = effective.get("desktop_url")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_DESKTOP_URL


# Keys only one backend can fill; the other backend keeps them None so both
# results expose the same key set for :func:`main` and downstream consumers.
_HEADLESS_KEYS = ("node", "node_version", "credentials_path",
                  "runtime_version", "runtime_recorded_matches")
_DESKTOP_KEYS = ("desktop_url", "connection", "desktop_connected", "server_pid",
                 "harness_version", "model_selection", "plugin_policy", "subagents",
                 "api_tested_by_doctor", "desktop_app_installed")


def _result_frame(*, backend, root, python_version, local_path, local_keys, steps,
                  doctor, notes, local_config_written):
    frame = {
        "root": str(root),
        "execution_backend": backend,
        "python_version": python_version,
        "local_config": str(local_path),
        "local_keys": sorted(local_keys),
        "local_config_written": bool(local_config_written),
        "steps": list(steps),
        "doctor": (doctor.stdout or "").strip(),
        "doctor_stderr": (doctor.stderr or "").strip(),
        "doctor_exit_code": doctor.returncode,
        "doctor_status": doctor_status(doctor.stdout or ""),
        "notes": list(notes),
    }
    for key in _HEADLESS_KEYS + _DESKTOP_KEYS:
        frame[key] = None
    return frame


def _configure_headless(root, effective, local, local_path, node, credentials,
                        skip_runtime, environ, version):
    """Legacy path: Node/npm, local ``runtime/``, ``config.local.json``, doctor."""
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
    result = _result_frame(backend=HEADLESS_BACKEND, root=root,
                           python_version=_python_label(version), local_path=local_path,
                           local_keys=merged_local, steps=steps, doctor=doctor,
                           notes=[], local_config_written=True)
    result["node"] = resolved_node
    result["node_version"] = node_version
    result["credentials_path"] = resolved_credentials
    result["runtime_version"] = merged_local.get("runtime_version")
    result["runtime_recorded_matches"] = _recorded_matches(merged_local, doctor.stdout)
    return result


def _configure_desktop(root, effective, local, local_path, node, credentials,
                       skip_runtime, environ, version):
    """Desktop path: Python + global links + ``bin/leadseek doctor``, nothing else."""
    notes = [DESKTOP_NOTES]
    ignored = [name for name, value in (("node", node), ("credentials", credentials)) if value]
    if ignored:
        notes.append("已忽略显式入参 " + "、".join(ignored)
                     + "：desktop 后端不解析 Node，也不写入或校验凭据路径；这些参数只在 headless 后端生效。")
    if skip_runtime:
        notes.append("desktop 后端不安装或比对本地 Harness 运行时，--skip-runtime 为 no-op。")

    # Global CLI/Skill links are still installed and reported by doctor.
    run_step([sys.executable, str(root / "bin/install-codex")], root, environ)
    steps = ["install-codex"]

    doctor = _run_doctor(root, environ)
    status = doctor_status(doctor.stdout or "")
    server_pid = _status_field(status, "server_pid")

    result = _result_frame(backend=DESKTOP_BACKEND, root=root,
                           python_version=_python_label(version), local_path=local_path,
                           local_keys=local, steps=steps, doctor=doctor, notes=notes,
                           local_config_written=False)
    result["desktop_url"] = _desktop_url(effective)
    result["desktop_connected"] = bool(server_pid)
    result["connection"] = "connected" if server_pid else "pending-start"
    result["server_pid"] = server_pid
    for key in ("harness_version", "model_selection", "plugin_policy",
                "subagents", "api_tested_by_doctor"):
        result[key] = _status_field(status, key)
    result["desktop_app_installed"] = desktop_app_installed(status)
    return result


def configure(root=None, node=None, credentials=None, skip_runtime=False, environ=None, version_info=None):
    """Check the machine for the configured backend and install the global links.

    ``headless`` keeps the legacy path (Node/npm, local ``runtime/``,
    ``config.local.json``, doctor).  ``desktop`` only checks Python, installs the
    global entry links and reads the desktop state from doctor; it never touches
    Node/npm, ``runtime/``, ``config.local.json``, credentials, models or
    plugins, so an already-configured machine stays byte-identical.  Both
    backends return the same key set, with ``None`` on the inapplicable side.
    """
    root = Path(root).resolve() if root is not None else ROOT
    version = python_ok(version_info)
    common = configuration.read_config_file(root / configuration.CONFIG_NAME)
    local_path = root / configuration.LOCAL_CONFIG_NAME
    runtime_install.reject_unsafe_config_path(local_path)
    local = configuration.read_config_file(local_path) if local_path.is_file() else {}
    effective = configuration.merge_config(common, local)
    if resolve_backend(effective) == "desktop":
        return _configure_desktop(root, effective, local, local_path, node,
                                  credentials, skip_runtime, environ, version)
    return _configure_headless(root, effective, local, local_path, node,
                               credentials, skip_runtime, environ, version)


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


def _show(value):
    """Render a status value for humans; booleans stay JSON-style lower case."""
    if value is None:
        return "未知"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _report_headless(result):
    """Legacy reporting: runtime version and global entry checks decide success."""
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


def _report_desktop(result):
    """Desktop reporting: the doctor state decides success, no runtime comparison."""
    print("执行后端：desktop（本机 DSH 桌面版：" + str(result.get("desktop_url") or DEFAULT_DESKTOP_URL) + "）")
    print("环境检查通过：Python " + str(result.get("python_version") or "未知") + " 可用（未检查 Node/npm）")
    steps = result.get("steps") or []
    if steps:
        print("已完成步骤：" + "、".join(steps) + "（全局 CLI/Skill 链接）")
    else:
        print("未执行安装步骤")
    if not result.get("local_config_written"):
        keys = "、".join(result.get("local_keys") or []) or "无"
        print("本机配置未改动（保留键：" + keys + "）：" + str(result.get("local_config")))

    exit_code = result.get("doctor_exit_code")
    if exit_code != 0:
        detail = (result.get("doctor_stderr") or result.get("doctor") or "").strip()
        print("doctor 自检失败（退出码 %s）：%s"
              % (exit_code, detail or "请检查 DSH 桌面版与全局入口"), file=sys.stderr)
        print(DESKTOP_APP_HINT, file=sys.stderr)
        print(NO_HEADLESS_FALLBACK, file=sys.stderr)
        return 1

    status = result.get("doctor_status")
    if not isinstance(status, dict):
        print("安装未完成：doctor 未返回可解析的 JSON 状态，无法确认桌面执行后端；"
              "请检查 bin/leadseek doctor 的输出。", file=sys.stderr)
        return 1
    try:
        reported_backend = resolve_backend(status)
    except ValueError:
        reported_backend = None
    if reported_backend != DESKTOP_BACKEND:
        print("安装未完成：config.json 要求 desktop，但 doctor 报告的 execution_backend 是 "
              + repr(status.get("execution_backend")) + "，配置与 doctor 不一致"
              "（该字段按去空白、忽略大小写比较）。", file=sys.stderr)
        return 1
    if status.get("skill_linked") is not True or status.get("cli_linked") is not True:
        print("安装未完成：doctor 的全局入口检查未通过（skill_linked=%s、cli_linked=%s）；"
              "请按 doctor 输出修复后重新运行 bin/setup。"
              % (_show(status.get("skill_linked")), _show(status.get("cli_linked"))), file=sys.stderr)
        return 1
    # Normalize again at the decision point, so a hand-built result or a
    # stringified flag can never turn "desktop app missing" into a success.
    if _tri_state(result.get("desktop_app_installed")) is False:
        print(DESKTOP_APP_HINT, file=sys.stderr)
        print(NO_HEADLESS_FALLBACK, file=sys.stderr)
        return 1

    print("Harness 版本：" + _show(result.get("harness_version")))
    print("模型选择：" + _show(result.get("model_selection")))
    print("插件策略：" + _show(result.get("plugin_policy")))
    print("子代理：" + _show(result.get("subagents")))
    print("API 校验：api_tested_by_doctor=" + _show(result.get("api_tested_by_doctor"))
          + "（doctor 只确认安装结构，不验证 DeepSeek API）")
    if result.get("desktop_connected") and result.get("server_pid"):
        print("桌面连接：已连接（server_pid=" + _show(result.get("server_pid")) + "）")
    else:
        print("桌面连接：待启动（首次运行任务时由执行器自动启动 DSH.app，无需手动打开）")
    print("未检查 Node/npm，未安装或比对本地 Harness 运行时版本。")
    return 0


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
    if result.get("execution_backend", "headless") == "desktop":
        return _report_desktop(result)
    return _report_headless(result)


if __name__ == "__main__":
    sys.exit(main())
