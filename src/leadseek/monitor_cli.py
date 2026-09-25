"""Lifecycle CLI and background launcher for the local monitor.

``leadseek monitor <start|open|status|stop|link|serve>`` is implemented here.
The HTTP side lives in :mod:`leadseek.monitor_server`; this module only manages
the ``.state/monitor`` token/metadata files, the file lock that prevents a
double start, the detached ``serve`` child and system browser integration.

Only the standard library is used.  Nothing here opens a write API.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional, Tuple

from . import __version__
from . import monitor_server
from .monitor_data import MonitorStore

ROOT = Path(__file__).resolve().parents[2]
SRC = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8765
MONITOR_DIRNAME = "monitor"
START_TIMEOUT = 12.0
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | _NOFOLLOW
_LOG_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | _NOFOLLOW


# ------------------------------------------------------------------ paths
def _monitor_dir(root) -> Path:
    return Path(root) / ".state" / MONITOR_DIRNAME


def _token_path(root) -> Path:
    return _monitor_dir(root) / "token"


def _state_path(root) -> Path:
    return _monitor_dir(root) / "state.json"


def _lock_path(root) -> Path:
    return _monitor_dir(root) / "monitor.lock"


def _log_path(root) -> Path:
    return _monitor_dir(root) / "monitor.log"


def _ensure_dir(root) -> Path:
    """Create ``.state/monitor`` privately, refusing any symlinked component.

    The monitor writes tokens and instance metadata here, so neither ``.state``
    nor ``monitor`` may be a symlink: following one would place private files
    outside the workspace.
    """
    state_dir = Path(root) / ".state"
    directory = state_dir / MONITOR_DIRNAME
    for candidate in (state_dir, directory):
        if candidate.is_symlink():
            raise OSError("拒绝符号链接目录：%s" % candidate)
    directory.mkdir(parents=True, exist_ok=True)
    for candidate in (state_dir, directory):
        if candidate.is_symlink() or not candidate.is_dir():
            raise OSError("监控目录不安全：%s" % candidate)
    os.chmod(str(directory), 0o700)
    return directory


def _write_private(path: Path, data: str) -> None:
    # ``O_NOFOLLOW`` refuses to truncate a symlink target; the file is created
    # 0600 and forced to 0600 even if it already existed.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data.encode("utf-8"))
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def _read_text_nofollow(path: Path) -> str:
    fd = os.open(str(path), os.O_RDONLY | _NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        return handle.read()


def _read_token(root) -> Optional[str]:
    try:
        value = _read_text_nofollow(_token_path(root))
    except (OSError, UnicodeError):
        return None
    return value.strip() or None


def _read_json(path: Path) -> Optional[dict]:
    if path.is_symlink():
        return None
    try:
        payload = json.loads(_read_text_nofollow(path))
    except (OSError, ValueError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _auth_url(port: int, token: str) -> str:
    return "http://127.0.0.1:%d/auth?token=%s" % (port, token)


def _address(port: int) -> str:
    return "http://127.0.0.1:%d/" % port


# ------------------------------------------------------------------ probes
def _probe(port, token=None, timeout: float = 2.0) -> Optional[dict]:
    if not isinstance(port, int) or port < 1 or port > 65535:
        return None
    request = urllib.request.Request("http://127.0.0.1:%d/api/health" % port)
    if token:
        request.add_header("Cookie", "%s=%s" % (monitor_server.COOKIE_NAME, token))
    # Never let an environment/system proxy see the loopback request or its
    # cookie; an empty ProxyHandler disables proxy discovery entirely.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("service") != monitor_server.SERVICE_NAME:
        return None
    return payload


def _is_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    # Reap a zombie that is our own direct child: ``kill(pid, 0)`` would still
    # succeed for a zombie, so an exited detached serve child must be treated
    # as dead.  ``waitpid`` on a non-child raises ChildProcessError.
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _reap_async(process) -> None:
    """Wait on a detached child in the background.

    The ``Popen`` object would otherwise be garbage collected while the child
    is still running, which raises ``ResourceWarning`` and leaves the child
    unreaped.
    """

    def _wait():
        try:
            process.wait()
        except Exception:
            pass

    threading.Thread(target=_wait, name="monitor-reaper", daemon=True).start()


def _reuse(root) -> Optional[dict]:
    state = _read_json(_state_path(root))
    token = _read_token(root)
    if not state or not token:
        return None
    port = state.get("port")
    instance_id = state.get("instance_id")
    pid = state.get("pid")
    if not isinstance(port, int) or not _is_alive(pid):
        return None
    health = _probe(port, token)
    if (
        not health
        or health.get("instance_id") != instance_id
        or health.get("pid") != pid
    ):
        return None
    return {
        "status": "running",
        "reused": True,
        "port": port,
        "pid": pid,
        "instance_id": instance_id,
        "url": _auth_url(port, token),
        "address": _address(port),
    }


def _open_browser(url: str) -> None:
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass


def _link_current_thread(root) -> None:
    thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not thread_id:
        return
    try:
        MonitorStore(str(root)).link_codex(thread_id, str(root))
    except (ValueError, OSError):
        # Linking is best effort and never blocks startup.
        pass


def _cleanup_state(root, instance_id) -> None:
    state = _read_json(_state_path(root))
    if state and instance_id and state.get("instance_id") == instance_id:
        try:
            _state_path(root).unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ start
def start(root=ROOT, port: int = DEFAULT_PORT, no_open: bool = False, timeout: float = START_TIMEOUT):
    root = Path(root)
    try:
        _ensure_dir(root)
    except OSError as error:
        return {"status": "error", "message": "监控目录不可用（拒绝符号链接）：%s" % error}, 1
    try:
        lock_fd = os.open(str(_lock_path(root)), _LOCK_FLAGS, 0o600)
    except OSError:
        return {"status": "error", "message": "监控锁文件不可用（拒绝符号链接），未启动"}, 1
    try:
        os.fchmod(lock_fd, 0o600)
    except OSError:
        pass
    lock = os.fdopen(lock_fd, "a+")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            existing = _reuse(root)
            if existing:
                if not no_open:
                    _open_browser(existing["url"])
                return existing, 0
            return {"status": "error", "message": "监控正在启动或状态未知，未重复启动"}, 1

        existing = _reuse(root)
        if existing:
            if not no_open:
                _open_browser(existing["url"])
            return existing, 0

        if not isinstance(port, int) or port < 0 or port > 65535:
            return {"status": "error", "message": "端口无效"}, 1

        try:
            token = secrets.token_urlsafe(32)
            _write_private(_token_path(root), token)
        except OSError:
            return {"status": "error", "message": "令牌文件不可用（拒绝符号链接），未启动"}, 1
        instance_id = uuid.uuid4().hex
        state_path = _state_path(root)
        try:
            if state_path.is_symlink() or state_path.exists():
                state_path.unlink()
        except OSError:
            pass

        env = dict(os.environ)
        pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + pythonpath if pythonpath else "")
        env["LEADSEEK_MONITOR_INSTANCE_ID"] = instance_id
        command = [
            sys.executable,
            "-B",
            "-m",
            "leadseek.monitor_cli",
            "serve",
            "--port",
            str(port),
            "--root",
            str(root),
        ]
        try:
            log_fd = os.open(str(_log_path(root)), _LOG_FLAGS, 0o600)
            os.fchmod(log_fd, 0o600)
        except OSError:
            _cleanup_state(root, instance_id)
            return {"status": "error", "message": "日志文件不可用（拒绝符号链接），未启动"}, 1
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,
                env=env,
                cwd=str(root),
            )
        finally:
            os.close(log_fd)

        deadline = time.time() + max(1.0, float(timeout))
        health = None
        state = None
        while time.time() < deadline:
            if process.poll() is not None:
                break
            state = _read_json(state_path)
            if state and state.get("instance_id") == instance_id:
                candidate = _probe(state.get("port"), token)
                if candidate and candidate.get("instance_id") == instance_id:
                    health = candidate
                    break
            time.sleep(0.08)

        if health is None:
            if process.poll() is None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except OSError:
                    try:
                        process.terminate()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=3)
                except Exception:
                    try:
                        process.kill()
                    except OSError:
                        pass
            try:
                process.wait(timeout=0)
            except Exception:
                pass
            _cleanup_state(root, instance_id)
            return {"status": "error", "message": "启动失败：端口 %s 可能被占用或服务未就绪" % port}, 1

        # Detached child keeps running after this function returns; a
        # background reaper waits on it so the Popen is not GC'd live.
        _reap_async(process)
        if not state:
            state = _read_json(state_path) or {}
        actual_port = health.get("port") or state.get("port") or port
        result = {
            "status": "running",
            "reused": False,
            "port": actual_port,
            "pid": process.pid,
            "instance_id": instance_id,
            "url": _auth_url(actual_port, token),
            "address": _address(actual_port),
        }
        _link_current_thread(root)
        if not no_open:
            _open_browser(result["url"])
        return result, 0
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            pass
        lock.close()


# ------------------------------------------------------------------ stop
def _terminate(pid: int, timeout: float) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + max(0.5, float(timeout))
    while time.time() < deadline:
        if not _is_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop(root=ROOT, timeout: float = 5.0):
    root = Path(root)
    state = _read_json(_state_path(root))
    token = _read_token(root)
    if not state:
        return {"status": "stopped", "already": True}, 0
    pid = state.get("pid")
    port = state.get("port")
    instance_id = state.get("instance_id")
    if not _is_alive(pid):
        _cleanup_state(root, instance_id)
        return {"status": "stopped", "stale_pid": True, "pid": pid}, 0
    health = _probe(port, token)
    if (
        not health
        or health.get("instance_id") != instance_id
        or health.get("pid") != pid
    ):
        return {
            "status": "error",
            "message": "健康身份与记录不匹配，拒绝终止陌生进程",
            "pid": pid,
        }, 1
    _terminate(pid, timeout)
    if _is_alive(pid):
        return {"status": "error", "message": "进程未能终止", "pid": pid}, 1
    _cleanup_state(root, instance_id)
    return {"status": "stopped", "pid": pid, "instance_id": instance_id}, 0


# ------------------------------------------------------------------ open/status
def open_monitor(root=ROOT):
    root = Path(root)
    state = _read_json(_state_path(root))
    token = _read_token(root)
    if not state or not token:
        return {"status": "error", "message": "监控未运行，请先执行 leadseek monitor start"}, 1
    health = _probe(state.get("port"), token)
    if (
        not health
        or health.get("instance_id") != state.get("instance_id")
        or health.get("pid") != state.get("pid")
    ):
        return {"status": "error", "message": "监控未运行或身份不匹配"}, 1
    result = _reuse(root)
    if not result:
        return {"status": "error", "message": "监控未就绪"}, 1
    _open_browser(result["url"])
    return result, 0


def status(root=ROOT):
    root = Path(root)
    state = _read_json(_state_path(root))
    token = _read_token(root)
    if not state:
        return {"running": False}, 0
    health = _probe(state.get("port"), token) if token else None
    running = bool(
        health
        and health.get("instance_id") == state.get("instance_id")
        and health.get("pid") == state.get("pid")
    )
    port = state.get("port")
    payload = {
        "running": running,
        "port": port,
        "pid": state.get("pid"),
        "instance_id": state.get("instance_id"),
    }
    if running:
        payload["address"] = _address(port)
        payload["url"] = _auth_url(port, token)
    else:
        payload["stale"] = True
    return payload, 0


# ------------------------------------------------------------------ link
def link(root=ROOT, thread_id: str = "", workspace: str = ""):
    try:
        record = MonitorStore(str(root)).link_codex(thread_id, workspace)
    except ValueError as error:
        return {"status": "error", "message": str(error)}, 2
    return {"status": "linked", **record}, 0


# ------------------------------------------------------------------ serve
def serve(root=ROOT, port: int = DEFAULT_PORT):
    root = Path(root)
    token = _read_token(root)
    if not token:
        sys.stderr.write("缺少访问令牌\n")
        return 2
    instance_id = os.environ.get("LEADSEEK_MONITOR_INSTANCE_ID", "")
    store = MonitorStore(str(root))
    server = monitor_server.create_server(
        store, port, token, instance_id=instance_id, web_dir=root / "web"
    )
    actual_port = int(server.server_address[1])
    try:
        _ensure_dir(root)
        _write_private(
            _state_path(root),
            json.dumps(
                {
                    "instance_id": instance_id,
                    "pid": os.getpid(),
                    "port": actual_port,
                    "host": "127.0.0.1",
                    "started_at": _now_iso(),
                    "version": __version__,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except OSError as error:
        server.server_close()
        sys.stderr.write("监控目录或状态文件不可用：%s\n" % error)
        return 3
    stopping = threading.Event()

    def _handle_signal(signum, frame):  # noqa: ARG001 - signal signature
        stopping.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    server.timeout = 0.5
    try:
        while not stopping.is_set():
            server.handle_request()
    finally:
        server.server_close()
        _cleanup_state(root, instance_id)
    return 0


# ------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="leadseek monitor", description="本机只读任务监控")
    sub = parser.add_subparsers(dest="monitor_command", required=True)
    p_start = sub.add_parser("start", help="后台启动并返回认证 URL；已运行则复用")
    p_start.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_start.add_argument("--no-open", action="store_true", help="不打开系统浏览器")
    sub.add_parser("open", help="在系统浏览器中打开已运行的监控")
    sub.add_parser("status", help="查看运行状态")
    sub.add_parser("stop", help="验证身份后停止监控")
    p_link = sub.add_parser("link", help="仅本机关联 Codex 会话与工作区")
    p_link.add_argument("--thread-id", required=True)
    p_link.add_argument("--workspace", required=True)
    p_serve = sub.add_parser("serve", help="内部命令：前台运行服务")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.add_argument("--root", default=str(ROOT))
    return parser


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.monitor_command == "start":
        payload, code = start(port=args.port, no_open=args.no_open)
    elif args.monitor_command == "open":
        payload, code = open_monitor()
    elif args.monitor_command == "status":
        payload, code = status()
    elif args.monitor_command == "stop":
        payload, code = stop()
    elif args.monitor_command == "link":
        payload, code = link(thread_id=args.thread_id, workspace=args.workspace)
    else:  # serve
        return serve(root=Path(args.root), port=args.port)
    _print(payload)
    return code


if __name__ == "__main__":
    sys.exit(main())
