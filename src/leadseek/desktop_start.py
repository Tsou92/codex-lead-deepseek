"""Start the normal desktop app only when its backend is absent."""

import subprocess
import time
from urllib.parse import urlsplit

from .desktop_client import DesktopClient, DesktopError


def listener_present(url):
    port = urlsplit(url).port or 80
    found = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-Fp"],
        capture_output=True, text=True, timeout=5,
    )
    return any(line.startswith("p") and line[1:].isdigit() for line in found.stdout.splitlines())


def ensure_running(url, log_path=None, auto_start=True, wait_seconds=30):
    from .desktop import server_identity

    # Validate the destination before inspecting or launching anything.
    with DesktopClient(url=url, log_path=log_path) as validation:
        url = validation.url
    try:
        return {**server_identity(url), "desktop_auto_started": False}
    except ValueError:
        # An occupied, unrecognised port must never receive the desktop token.
        if listener_present(url) or not auto_start:
            raise
    launched = subprocess.run(
        ["/usr/bin/open", "-g", "-a", "DSH"],
        capture_output=True, text=True, timeout=10,
    )
    if launched.returncode != 0:
        raise ValueError("无法自动打开 DSH.app；请先安装桌面客户端后重试")
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            identity = server_identity(url)
            with DesktopClient(url=url, log_path=log_path, timeout=2) as client:
                client.rpc("session/modelCatalog")
            return {**identity, "desktop_auto_started": True}
        except (OSError, ValueError, subprocess.SubprocessError):
            time.sleep(0.3)
    raise DesktopError("DSH.app 已请求启动，但桌面服务未在等待时间内就绪；未启动独立 Harness")
