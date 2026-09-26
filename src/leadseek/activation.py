"""Enable the local read-only monitor for the first time in a Codex task.

``leadseek activate`` is the single entry point the Skill asks Codex to run
once per task before any delegated work starts.  It makes sure the monitor is
running and authenticated, opens it in the system browser and links the current
``CODEX_THREAD_ID`` (inherited through the environment).

Every normal call actually invokes ``打开监控.command`` and may therefore open
a page again; de-duplication is the Skill's responsibility (one ``activate`` per
task), not something this module enforces.  After a service interruption the
Skill may call it again.

The normal path deliberately delegates to the repository's ``打开监控.command``
as an argument array (``/bin/bash <ROOT>/打开监控.command``) so the existing,
user-visible startup flow stays the single source of truth.  ``stdin`` is
``DEVNULL`` so the script's interactive ``read`` on failure cannot block, and
its output is discarded: the helper may print an authenticated ``/auth?token=``
URL and that secret must never reach the model or the logs.

The only thing returned to Codex is a compact JSON receipt with clean fields
(``running``/``address``/``port``).  The access token and the authenticated URL
are never included.  ``--no-open`` is a verification switch that asks the
existing :mod:`leadseek.monitor_cli` to start the service without a browser.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
COMMAND_NAME = "打开监控.command"
ACTIVATE_TIMEOUT = 45.0

# Fixed, token-free error messages: a failing helper may have printed an
# authenticated URL on stdout/stderr, so raw output is never surfaced.
ERROR_COMMAND = "监控启动脚本未成功完成，请重试或手动运行 打开监控.command"
ERROR_NO_OPEN = "监控启动失败，未打开浏览器"
ERROR_STATUS = "无法确认监控状态，请手动运行 打开监控.command 后重试"


def _monitor():
    """Import :mod:`leadseek.monitor_cli` lazily.

    Keeping the import inside the function means activation itself stays
    importable even when the monitor helpers are unavailable, and the service
    module is only loaded right before it is actually used.
    """
    from . import monitor_cli

    return monitor_cli


def command_path(root=None) -> Path:
    base = Path(root) if root else ROOT
    return base / COMMAND_NAME


def _clean_status(root):
    """Return a token-free receipt derived from the monitor status.

    ``monitor_cli.status`` reports both an ``address`` and an authenticated
    ``url`` when running; only the former is copied so the token can never leak.
    Failures inside the helper become a fixed error instead of an exception, so
    a traceback (which could embed a nonce) can never escape.
    """
    try:
        payload, _code = _monitor().status(root=Path(root))
    except Exception:
        return {"status": "error", "running": False, "message": ERROR_STATUS}, 1
    running = bool(isinstance(payload, dict) and payload.get("running"))
    result = {"status": "running" if running else "error", "running": running}
    if running:
        address = payload.get("address")
        if isinstance(address, str):
            result["address"] = address
        port = payload.get("port")
        if isinstance(port, int):
            result["port"] = port
    else:
        result["message"] = "监控未运行：请确认可手动双击或运行 打开监控.command"
    return result, (0 if running else 1)


def activate(root=None, no_open=False, timeout=ACTIVATE_TIMEOUT):
    """Ensure the monitor is running; return ``(receipt, exit_code)``.

    A failing helper always yields a non-zero, token-free error, even when a
    previously running daemon still answers ``status``: an exit code of zero
    means the activation step itself succeeded.
    """
    base = Path(root) if root else ROOT

    if no_open:
        # Verification path: use the existing launcher without a browser.
        try:
            _payload, start_code = _monitor().start(root=base, no_open=True)
        except Exception:
            return {"status": "error", "running": False,
                    "message": ERROR_NO_OPEN}, 1
        if start_code != 0:
            return {"status": "error", "running": False,
                    "message": ERROR_NO_OPEN}, 1
    else:
        script = command_path(base)
        if not script.is_file():
            return {"status": "error", "running": False,
                    "message": "缺少监控启动脚本：%s" % script}, 1
        try:
            completed = subprocess.run(
                ["/bin/bash", str(script)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1.0, float(timeout)),
                check=False,
                # Inherit CODEX_THREAD_ID (and PATH) so the helper links the
                # current Codex task; never rewrite the environment.
                env=dict(os.environ),
                cwd=str(base),
            )
        except (OSError, subprocess.SubprocessError):
            # Never surface raw helper output: it may contain the auth URL.
            return {"status": "error", "running": False,
                    "message": ERROR_COMMAND}, 1
        if completed.returncode != 0:
            return {"status": "error", "running": False,
                    "message": ERROR_COMMAND}, 1

    return _clean_status(base)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leadseek activate",
        description="首次启用本机只读监控并打开认证页面；每次调用都会实际启动",
    )
    parser.add_argument("--no-open", action="store_true", help="只启动监控，不打开浏览器（用于验证）")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    payload, code = activate(no_open=args.no_open)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
