"""Load the portable config and resolve machine-local values.

This module never imports :mod:`leadseek.runner`, so the runner can depend on
it without a cycle.  The helpers take explicit paths and environments so tests
can run in temporary directories without touching the real HOME or install.
"""

import json
import os
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[2]
CONFIG_NAME = "config.json"
LOCAL_CONFIG_NAME = "config.local.json"
RUNTIME_CLI = Path("runtime/node_modules/@deepseek-ai/dsh/lib/bin.js")
PORTABLE_NODE = Path(".portable") / "node" / "bin" / "node"
NODE24_CANDIDATES = (
    "/opt/homebrew/opt/node@24/bin/node",
    "/usr/local/opt/node@24/bin/node",
)
NODE_HINT = "请安装 Node 24，或用 bin/setup --node /path/to/node 指定可执行文件"


def default_root():
    return ROOT


def _is_executable(path):
    path = Path(path)
    return path.is_file() and os.access(path, os.X_OK)


def portable_node(root=None):
    """Return the project-local portable Node binary when it is installed."""
    base = ROOT if root is None else Path(root)
    candidate = Path(base) / PORTABLE_NODE
    if _is_executable(candidate):
        return str(candidate.absolute())
    return None


def read_config_file(path):
    """Read one JSON object; reject missing, invalid or non-object content."""
    path = Path(path)
    if not path.is_file():
        raise ValueError("找不到配置文件: " + str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("配置文件不是有效 JSON: " + str(path) + "（" + error.msg + "）") from error
    if not isinstance(data, dict):
        raise ValueError("配置文件必须是 JSON 对象: " + str(path))
    return data


def merge_config(base, override):
    """Shallow merge; later values override earlier ones and other keys stay."""
    if not isinstance(base, dict) or not isinstance(override, dict):
        raise ValueError("配置必须是 JSON 对象")
    merged = dict(base)
    merged.update(override)
    return merged


def find_node(environ=None, candidates=NODE24_CANDIDATES, root=None):
    """Prefer the project portable Node, then local Node 24 paths, then PATH."""
    environ = os.environ if environ is None else environ
    local = portable_node(root)
    if local:
        return local
    for candidate in candidates:
        path = Path(candidate)
        if _is_executable(path):
            return str(path.absolute())
    found = shutil.which("node", path=environ.get("PATH", ""))
    if found:
        return str(Path(found).absolute())
    raise ValueError("找不到 Node 24；" + NODE_HINT)


def resolve_node(value, environ=None, candidates=NODE24_CANDIDATES, root=None):
    """Resolve ``auto`` or an explicit absolute path/command name to a node binary.

    ``auto`` prefers the project's ``.portable/node`` when ``root`` is known (or
    the checkout root by default).  An explicit value from ``config.local.json``
    is always honoured, so an old local ``node`` keeps working.
    """
    environ = os.environ if environ is None else environ
    if value is None or value == "auto":
        return find_node(environ, candidates, root)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("node 必须是 'auto'、绝对路径或可执行命令名；" + NODE_HINT)
    lookup = os.path.expanduser(value.strip())
    found = shutil.which(lookup, path=environ.get("PATH", ""))
    if not found:
        raise ValueError("找不到可执行的 node: " + value + "；" + NODE_HINT)
    return str(Path(found).absolute())


def resolve_credentials_path(value, root=None):
    """Expand ``~`` and return an absolute path; the file itself is not read.

    A relative value is resolved against the checkout ``root`` (not the current
    working directory) so the recorded path keeps working from any cwd.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("credentials_path 必须是非空路径")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        base = Path(root).resolve() if root is not None else Path.cwd()
        path = (base / path).resolve()
    return str(path)


def load_config(root=None):
    """Load ``config.json`` then the optional local override, resolving machine values."""
    base = Path(root).resolve() if root is not None else default_root()
    common = read_config_file(base / CONFIG_NAME)
    local_path = base / LOCAL_CONFIG_NAME
    local = read_config_file(local_path) if local_path.is_file() else {}
    merged = merge_config(common, local)
    merged["node"] = resolve_node(merged.get("node"), root=base)
    merged["credentials_path"] = resolve_credentials_path(merged.get("credentials_path"), base)
    return merged
