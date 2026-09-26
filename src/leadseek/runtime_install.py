"""Choose how to obtain the DeepSeek Harness runtime.

Strategy (minimal, no global installs):

1. If this project already has a working ``runtime/node_modules/@deepseek-ai/dsh``
   (package.json plus ``lib/bin.js`` that runs), reuse it unchanged.  Existing
   installs are never upgraded or downgraded automatically.
2. Otherwise, if ``dsh`` on ``PATH`` resolves to a real official
   ``@deepseek-ai/dsh`` package, install that *same* version into the project's
   isolated ``runtime/`` directory.  This is same-version local access, not an
   upgrade.
3. Otherwise install ``@deepseek-ai/dsh@latest`` from the public npm registry
   explicitly, so a lagging mirror's dist-tag cannot pin an old build.

Only case 3 asks for the newest version, and even then the actual installed
version is whatever the registry reports at that moment: no concrete version is
pinned in this code.  Every step runs without sudo, without a global install,
and without touching other tools.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


PACKAGE_NAME = "@deepseek-ai/dsh"
REGISTRY = "https://registry.npmjs.org"
RUNTIME_DIR = Path("runtime")
NPM_CACHE = Path(".state/npm-cache")
LOCAL_CONFIG_NAME = "config.local.json"

CLI_TIMEOUT_SECONDS = 15
NPM_TIMEOUT_SECONDS = 600

SOURCE_EXISTING = "local-existing"
SOURCE_GLOBAL = "global-version"
SOURCE_LATEST = "latest"

# Verified against the published @deepseek-ai/dsh dependency tree.
REQUIRED_PACKAGES = (
    "@deepseek-ai/cordis",
    "@deepseek-ai/schemastery",
    "@deepseek-ai/dsh-headless",
    "@deepseek-ai/dsh-session",
)

# macOS exposes these as system-level symlinks (e.g. /var -> /private/var);
# they are not user-controlled redirects and must stay writable.
_ALLOWED_SYSTEM_SYMLINK_NAMES = ("var", "tmp", "etc")


def _package_path(root):
    return Path(root) / RUNTIME_DIR / "node_modules" / PACKAGE_NAME / "package.json"


def _cli_path(root):
    return Path(root) / RUNTIME_DIR / "node_modules" / PACKAGE_NAME / "lib" / "bin.js"


def read_package_version(root):
    """Return the installed version, rejecting a missing or foreign package."""
    path = _package_path(root)
    if not path.is_file():
        raise ValueError("找不到 Harness 包描述: " + str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("Harness 包描述不是有效 JSON: " + str(path)) from error
    if not isinstance(data, dict) or data.get("name") != PACKAGE_NAME or not data.get("version"):
        raise ValueError("runtime 内的包不是官方 " + PACKAGE_NAME + ": " + str(path))
    return str(data["version"])


def cli_version(node, root, run=subprocess.run):
    """Run ``node lib/bin.js --version`` and return the reported version."""
    cli = _cli_path(root)
    if not cli.is_file():
        raise ValueError("Harness 运行时 CLI 缺失: " + str(cli))
    try:
        completed = run([node, str(cli), "--version"], capture_output=True, text=True,
                        timeout=CLI_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise ValueError("Harness CLI 在 %d 秒内没有响应；请检查 Node 与运行时"
                         % CLI_TIMEOUT_SECONDS) from error
    if completed.returncode != 0:
        raise ValueError("Harness CLI 无法运行（退出码 %d）；跳过安装或修复方式请见文档"
                         % completed.returncode)
    return (completed.stdout or "").strip()


def _same_version(left, right):
    return str(left).strip().lstrip("v") == str(right).strip().lstrip("v")


def probe_existing(root, node, run=subprocess.run):
    """Return reuse info when the project runtime exists, else ``None``.

    A half-present or mismatched runtime is broken: raise instead of silently
    reinstalling over it, so a local error is never masked.
    """
    package = _package_path(root)
    cli = _cli_path(root)
    if not package.exists() and not cli.exists():
        return None
    if not package.is_file() or not cli.is_file():
        raise ValueError("已有 Harness 运行时损坏（缺少 package.json 或 lib/bin.js）；"
                         "请先手工确认，不会自动覆盖: " + str(Path(root) / RUNTIME_DIR))
    version = read_package_version(root)
    actual = cli_version(node, root, run)
    if not _same_version(actual, version):
        raise ValueError("已有 Harness 运行时版本不一致（包 %s，CLI 报告 %s）；"
                         "不会自动升级或降级" % (version, actual or "空"))
    return {"source": SOURCE_EXISTING, "version": version, "actual": actual,
            "same_version_as_global": False, "upgraded": False}


def find_global_dsh(environ=None, which=shutil.which):
    """Resolve ``dsh`` on PATH to a real official package, else ``None``."""
    environment = os.environ if environ is None else environ
    found = which("dsh", path=environment.get("PATH", ""))
    if not found:
        return None
    target = Path(found)
    try:
        resolved = target.resolve()
    except OSError:
        return None
    for directory in [resolved.parent, *resolved.parents]:
        package = directory / "package.json"
        if not package.is_file():
            continue
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("name") == PACKAGE_NAME and data.get("version"):
            return {"version": str(data["version"]), "path": str(directory),
                    "command": str(found)}
    return None


def find_npm(node, environ=None):
    """Prefer the npm next to the chosen node, then PATH."""
    environment = os.environ if environ is None else environ
    sibling = Path(node).parent / "npm"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    found = shutil.which("npm", path=environment.get("PATH", ""))
    if not found:
        raise ValueError("找不到 npm；请安装 Node 24（含 npm）后重试")
    return found


def _npm_environment(node, environ):
    environment = dict(os.environ if environ is None else environ)
    environment["PATH"] = str(Path(node).parent) + os.pathsep + environment.get("PATH", "")
    environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return environment


def install_version(root, node, specifier, environ=None, run=subprocess.run):
    """Install one npm specifier into the project runtime and verify it.

    Errors stay short on purpose: npm output is never echoed back, so a failed
    install cannot leak environment variables or credentials into the report.
    """
    npm = find_npm(node, environ)
    command = [npm, "install", "--prefix", str(Path(root) / RUNTIME_DIR),
               "--cache", str(Path(root) / NPM_CACHE),
               "--no-audit", "--no-fund", "--save-exact",
               "--registry", REGISTRY, PACKAGE_NAME + "@" + str(specifier)]
    try:
        completed = run(command, capture_output=True, text=True,
                        timeout=NPM_TIMEOUT_SECONDS, env=_npm_environment(node, environ))
    except subprocess.TimeoutExpired as error:
        raise ValueError("npm 安装 %s@%s 超时（%d 秒）；未记录成功，也不会自动改用其他版本"
                         % (PACKAGE_NAME, specifier, NPM_TIMEOUT_SECONDS)) from error
    if completed.returncode != 0:
        raise ValueError("npm 安装 " + PACKAGE_NAME + "@" + str(specifier)
                         + " 失败（退出码 %d）；未记录成功，也不会自动改用其他版本"
                         % completed.returncode)
    version = read_package_version(root)
    actual = cli_version(node, root, run)
    if not _same_version(actual, version):
        raise ValueError("安装后的 Harness 版本不一致（包 %s，CLI 报告 %s）"
                         % (version, actual or "空"))
    if str(specifier) != "latest" and not _same_version(version, specifier):
        raise ValueError("安装结果与请求版本不一致（请求 %s，实际 %s）；"
                         "不会自动改用其他版本" % (specifier, version))
    return version


def ensure_runtime(root, node, environ=None, run=subprocess.run, which=shutil.which):
    """Apply the reuse/global/latest strategy and return the chosen runtime.

    Whatever path is taken, the required package set must be structurally
    present before success is reported, so nothing half-installed is recorded.
    """
    root = Path(root)
    existing = probe_existing(root, node, run)
    if existing:
        result = existing
    else:
        global_package = find_global_dsh(environ, which)
        if global_package:
            version = install_version(root, node, global_package["version"], environ, run)
            result = {"source": SOURCE_GLOBAL, "version": version, "actual": version,
                      "same_version_as_global": True, "upgraded": False,
                      "global_path": global_package["path"]}
        else:
            version = install_version(root, node, "latest", environ, run)
            result = {"source": SOURCE_LATEST, "version": version, "actual": version,
                      "same_version_as_global": False, "upgraded": False}
    missing = missing_required_packages(root)
    if missing:
        raise ValueError("Harness 运行时缺少关键包: " + "、".join(missing)
                         + "；安装不完整，未记录成功，也不会自动降级")
    return result


def reject_unsafe_config_path(path):
    """Reject symlinked config files and user-level symlinked ancestors.

    A symlinked target could redirect the 0600 write somewhere unexpected, so
    it is refused outright.  macOS system symlinks such as ``/var`` and ``/tmp``
    are not user-controlled and stay allowed.
    """
    path = Path(path)
    if path.is_symlink():
        raise ValueError("拒绝写入符号链接配置文件: " + str(path))
    for parent in path.parents:
        system_link = parent.parent == Path("/") and parent.name in _ALLOWED_SYSTEM_SYMLINK_NAMES
        if parent.is_symlink() and not system_link:
            raise ValueError("拒绝写入符号链接目录下的配置: " + str(parent))
    return path


def atomic_write_json(path, data):
    """Write JSON atomically with mode 0600 using a same-directory temp file."""
    path = reject_unsafe_config_path(path)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".leadseek-", suffix=".json", dir=str(path.parent))
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(temporary, path)
        temporary = None
    finally:
        if handle is not None:
            handle.close()
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    os.chmod(path, 0o600)
    return data


def _read_local(path):
    if path.is_symlink():
        raise ValueError("拒绝读取符号链接配置文件: " + str(path))
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("config.local.json 不是有效 JSON: " + str(path)) from error
    if not isinstance(data, dict):
        raise ValueError("config.local.json 必须是 JSON 对象: " + str(path))
    return data


def merge_local_config(root, values):
    """Merge values into ``config.local.json`` atomically, mode 0600."""
    path = Path(root) / LOCAL_CONFIG_NAME
    merged = _read_local(path)
    merged.update(values)
    atomic_write_json(path, merged)
    return merged


def record_runtime(root, result):
    """Persist the actual version and how it was obtained; API stays untested."""
    return merge_local_config(root, {
        "runtime_version": result["version"],
        "runtime_source": result["source"],
        "api_tested": False,
    })


def doctor_version_fields(policy, actual, recorded=None):
    """Describe expected/actual under the latest or pinned policy.

    ``latest`` is a request, not a remote query: a valid local install makes the
    structure ready, and ``latest_checked_remotely`` stays False.  ``version_matches``
    also folds in the recorded version so an incompatible runtime is not
    reported as healthy.
    """
    policy = str(policy) if policy else "latest"
    actual = (actual or "").strip()
    if policy == "latest":
        policy_matches = bool(actual)
    else:
        policy_matches = _same_version(actual, policy)
    if recorded in (None, "", "latest"):
        recorded_matches = True
    else:
        recorded_matches = _same_version(actual, recorded)
    return {"expected": policy, "actual": actual, "version_policy": policy,
            "version_matches": bool(policy_matches and recorded_matches),
            "policy_matches": policy_matches,
            "recorded": recorded, "recorded_matches": recorded_matches,
            "latest_checked_remotely": False}


def missing_required_packages(root):
    """Names of required packages that are structurally absent from runtime/."""
    base = Path(root) / RUNTIME_DIR / "node_modules"
    return [name for name in REQUIRED_PACKAGES
            if not (base / name / "package.json").is_file()]


def runtime_dependency_status(root):
    """Structural presence check only; it never claims full API compatibility."""
    base = Path(root) / RUNTIME_DIR / "node_modules"
    packages = {name: (base / name / "package.json").is_file() for name in REQUIRED_PACKAGES}
    missing = [name for name, present in packages.items() if not present]
    return {"packages": packages, "required": list(REQUIRED_PACKAGES),
            "all_present": not missing, "missing": missing,
            "structural_only": True, "api_compatible": None,
            "headless_profile_package": packages.get("@deepseek-ai/dsh-headless", False),
            "note": "仅检查必需包的 package.json 是否存在，未验证全部接口，也不代表支持未来所有版本"}
