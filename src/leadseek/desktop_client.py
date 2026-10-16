"""DSH 桌面服务的同进程 HTTP RPC 客户端。

协议（与 Codex 核验过的真实桌面服务一致）：

* ``GET /?token=<token>`` 返回 302/303 与 ``Set-Cookie``；客户端不跟随
  ``Location``，只把会话 Cookie 留在内存里。
* token 从 ``~/Library/Logs/DSH/server.log`` 末尾 8 MiB 里按
  ``token=([A-Za-z0-9_-]+)`` 匹配，取最后一个。
* :meth:`DesktopClient.rpc` 向 ``POST /api/<endpoint>`` 发送
  ``{"type": "client-request", "rpcId": ..., "method": endpoint,
  "payload": {"args": ...}}``，并校验
  ``{"type": "server-response", "rpcId": ..., "result": {"ok": ...}}``。

安全约束：

* 只允许 ``http://`` 的 ``127.0.0.1`` / ``localhost`` / ``[::1]``；``localhost``
  会先校验解析结果仍为 loopback，再直接映射到 ``127.0.0.1``，避免 DNS 把连接
  带离本机。
* 使用 :class:`http.client.HTTPConnection` 直连 loopback，不使用代理，不跟随
  重定向。
* token 与 Cookie 只驻留内存：不写文件、不打日志、不进入异常信息，也不转述
  服务端返回的原始响应、堆栈或 message；业务 ``code`` 仅在安全字符集内保留。
* 不读取 DeepSeek API 凭据；本模块唯一打开的文件是本地 DSH 桌面日志的尾部。
"""

import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import threading
import uuid
from urllib.parse import urlsplit

__all__ = ["DesktopClient", "DesktopError", "DEFAULT_LOG_PATH", "DEFAULT_URL"]

DEFAULT_URL = "http://127.0.0.1:3080"
DEFAULT_LOG_PATH = "~/Library/Logs/DSH/server.log"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
LOG_TAIL_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

_TOKEN_RE = re.compile(r"token=([A-Za-z0-9_-]{1,4096})")
_ENDPOINT_RE = re.compile(r"[A-Za-z0-9_-]{1,128}/[A-Za-z0-9_-]{1,128}")
_COOKIE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_COOKIE_VALUE_RE = re.compile(r"[\x21-\x7e]{1,4096}")
_CODE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_AUTH_STATUSES = (302, 303)
_READ_CHUNK = 64 * 1024
_UNSAFE_COOKIE_CHARS = ',;"\\'
_MAX_TIMEOUT_SECONDS = 86400


class DesktopError(ValueError):
    """DSH 桌面客户端的所有失败都以本异常抛出（不泄漏敏感内容）。"""


class DesktopClient:
    """连接本机 DSH 桌面服务的 HTTP RPC 客户端。

    参数：

    * ``url``：桌面服务基础地址，默认 ``http://127.0.0.1:3080``，只接受
      loopback、无 userinfo/query/fragment、路径为空或 ``/``。
    * ``log_path``：桌面日志路径，默认 ``~/Library/Logs/DSH/server.log``。
    * ``timeout``：连接与读取超时秒数，默认 15。
    """

    def __init__(self, url=DEFAULT_URL, log_path=None, timeout=15):
        normalized, host, port = _parse_url(url)
        self.url = normalized
        self._host = host
        self._port = port
        self._log_path = _parse_log_path(log_path)
        self._timeout = _parse_timeout(timeout)
        self._cookie = None
        self._authenticated = False
        self._closed = False
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 生命周期

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def __repr__(self):
        # 刻意不含 cookie / token / 日志内容。
        return "DesktopClient(url=%r)" % (self.url,)

    def close(self):
        """关闭客户端：丢弃内存中的 Cookie，之后任何调用都会报错。"""
        with self._lock:
            self._closed = True
            self._cookie = None
            self._authenticated = False

    # ------------------------------------------------------------------ 公共接口

    def authenticate(self):
        """读取本地日志中的 token，完成 ``GET /?token=...`` 换取内存 Cookie。"""
        with self._lock:
            self._ensure_open()
            self._authenticate_once()
            return True

    def rpc(self, endpoint, args=None):
        """调用 ``POST /api/<endpoint>`` 并返回 ``result.value``。

        首次使用会自动认证。服务返回 401 时只重新认证并重发一次；其他故障
        （尤其是超时）一律不重发，避免任务被重复执行。
        """
        if not isinstance(endpoint, str) or _ENDPOINT_RE.fullmatch(endpoint) is None:
            raise DesktopError("endpoint 必须是 '分组/动作' 形式的两个安全路径片段")
        body, request_id = _encode_request(endpoint, args)
        with self._lock:
            self._ensure_open()
            if not self._authenticated:
                self._authenticate_once()
            status, payload = self._post(endpoint, body)
            if status == 401:
                self._cookie = None
                self._authenticated = False
                self._authenticate_once()
                status, payload = self._post(endpoint, body)
                if status == 401:
                    raise DesktopError("DSH 桌面服务拒绝认证：重新认证一次后仍返回 401")
            return _unwrap(status, payload, request_id)

    # -------------------------------------------------------------------- 内部实现

    def _ensure_open(self):
        if self._closed:
            raise DesktopError("客户端已关闭，无法发起请求")

    def _authenticate_once(self):
        token = _read_token(self._log_path)
        status, headers, _body = self._request("GET", "/?token=" + token, None, {})
        if status not in _AUTH_STATUSES:
            raise DesktopError("DSH 桌面服务认证失败：意外的 HTTP 状态码 %d" % status)
        self._cookie = _extract_cookie(headers)
        self._authenticated = True

    def _post(self, endpoint, body):
        headers = {
            "Content-Type": "application/json",
            "Cookie": self._cookie or "",
        }
        status, _headers, payload = self._request("POST", "/api/" + endpoint, body, headers)
        return status, payload

    def _connect_host(self):
        if self._host == "localhost":
            _verify_localhost_is_loopback(self._port)
            return "127.0.0.1"
        return self._host

    def _request(self, method, path, body, headers):
        connection = None
        # 兜底钳制：socket 无法表示的超时既不安全，也会在 create_connection
        # 内部留下未关闭的套接字。
        timeout = min(self._timeout, _MAX_TIMEOUT_SECONDS)
        try:
            connection = http.client.HTTPConnection(
                self._connect_host(), self._port, timeout=timeout
            )
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            payload = _read_limited(response)
            return status, response.headers, payload
        except DesktopError:
            raise
        except socket.timeout as error:
            raise DesktopError(
                "连接或读取 DSH 桌面服务超时；未重发请求以避免重复执行"
            ) from error
        except (http.client.HTTPException, OSError, OverflowError) as error:
            raise DesktopError("无法连接或读取 DSH 桌面服务（连接已关闭）") from error
        finally:
            if connection is not None:
                connection.close()


# ------------------------------------------------------------------------ 解析工具


def normalize_url(url):
    """Validate a loopback destination without authenticating or doing I/O."""
    return _parse_url(url)[0]


def _parse_url(url):
    if not isinstance(url, str) or not url.strip():
        raise DesktopError("url 必须是非空字符串")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as error:
        raise DesktopError("url 无法解析") from error
    if parts.scheme != "http":
        raise DesktopError("仅支持 http:// 的 loopback 地址")
    if parts.username is not None or parts.password is not None:
        raise DesktopError("url 不能包含用户名或密码")
    if parts.query or parts.fragment:
        raise DesktopError("url 不能包含查询串或片段")
    if parts.path not in ("", "/"):
        raise DesktopError("url 的路径必须为空或 /")
    host = parts.hostname
    if host is None or host.lower() not in LOOPBACK_HOSTS:
        raise DesktopError("仅允许 127.0.0.1、localhost 或 [::1] 主机")
    host = host.lower()
    if port is None:
        port = 80
    if isinstance(port, bool) or not 1 <= port <= 65535:
        raise DesktopError("url 端口无效")
    literal = "[%s]" % host if ":" in host else host
    return "http://%s:%d" % (literal, port), host, port


def _parse_log_path(log_path):
    if log_path is None:
        return str(Path(DEFAULT_LOG_PATH).expanduser())
    if not isinstance(log_path, str) or not log_path.strip():
        raise DesktopError("log_path 必须是非空路径字符串")
    if "\x00" in log_path:
        raise DesktopError("log_path 不能包含空字符")
    return str(Path(log_path.strip()).expanduser())


def _parse_timeout(timeout):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise DesktopError("timeout 必须是正数")
    if not math.isfinite(timeout) or not timeout > 0:
        raise DesktopError("timeout 必须是有限正数")
    if timeout > _MAX_TIMEOUT_SECONDS:
        raise DesktopError("timeout 不能超过 %d 秒" % _MAX_TIMEOUT_SECONDS)
    return float(timeout)


def _verify_localhost_is_loopback(port):
    """确认 localhost 仍解析为 loopback；解析不可用时直接映射 127.0.0.1。"""
    try:
        infos = socket.getaddrinfo("localhost", port, type=socket.SOCK_STREAM)
    except OSError:
        return
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as error:
            raise DesktopError("localhost 解析结果不是 IP 地址，已拒绝连接") from error
        if not parsed.is_loopback:
            raise DesktopError("localhost 未解析为 loopback 地址，已拒绝连接")


def _encode_request(endpoint, args):
    request_id = str(uuid.uuid4())
    document = {
        "type": "client-request",
        "rpcId": request_id,
        "method": endpoint,
        "payload": {"args": {} if args is None else args},
    }
    try:
        body = json.dumps(
            document, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise DesktopError("args 无法序列化为 JSON") from error
    return body, request_id


def _read_token(log_path):
    path = Path(log_path)
    try:
        if path.is_symlink():
            raise DesktopError("DSH 桌面日志是符号链接，已拒绝读取")
        if not path.exists():
            raise DesktopError("找不到 DSH 桌面日志文件: " + str(path))
        if not path.is_file():
            raise DesktopError("DSH 桌面日志路径不是普通文件: " + str(path))
    except DesktopError:
        raise
    except (OSError, ValueError) as error:
        raise DesktopError("无法访问 DSH 桌面日志文件: " + str(path)) from error
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DesktopError("无法打开 DSH 桌面日志文件: " + str(path)) from error
    try:
        with os.fdopen(descriptor, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            data = handle.read(LOG_TAIL_BYTES)
    except OSError as error:
        raise DesktopError("无法读取 DSH 桌面日志文件: " + str(path)) from error
    matches = _TOKEN_RE.findall(data.decode("utf-8", "replace"))
    if not matches:
        raise DesktopError("DSH 桌面日志中未找到认证 token")
    return matches[-1]


def _extract_cookie(headers):
    values = headers.get_all("Set-Cookie") or []
    for value in values:
        name, separator, cookie_value = value.split(";", 1)[0].strip().partition("=")
        name = name.strip()
        cookie_value = cookie_value.strip()
        if not separator:
            continue
        if _COOKIE_NAME_RE.fullmatch(name) is None:
            continue
        if _COOKIE_VALUE_RE.fullmatch(cookie_value) is None:
            continue
        if any(char in cookie_value for char in _UNSAFE_COOKIE_CHARS):
            continue
        return name + "=" + cookie_value
    raise DesktopError("DSH 桌面服务认证失败：未返回有效的会话 Cookie")


def _read_limited(response, limit=None):
    limit = MAX_RESPONSE_BYTES if limit is None else limit
    declared = response.getheader("Content-Length")
    if declared is not None:
        try:
            declared_length = int(declared.strip())
        except (TypeError, ValueError):
            declared_length = None
        if declared_length is not None and declared_length > limit:
            raise DesktopError("DSH 桌面服务响应超过上限（%d 字节）" % limit)
    chunks = []
    total = 0
    while True:
        chunk = response.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise DesktopError("DSH 桌面服务响应超过上限（%d 字节）" % limit)
        chunks.append(chunk)
    return b"".join(chunks)


def _unwrap(status, payload, request_id):
    if status != 200:
        raise DesktopError("DSH 桌面服务返回 HTTP 状态码 %d" % status)
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DesktopError("DSH 桌面服务响应不是有效 JSON") from error
    if not isinstance(document, dict):
        raise DesktopError("协议异常：响应不是 JSON 对象")
    if document.get("type") != "server-response":
        raise DesktopError("协议异常：响应信封类型不是 server-response")
    if document.get("rpcId") != request_id:
        raise DesktopError("协议异常：响应 rpcId 与请求不匹配")
    result = document.get("result")
    if not isinstance(result, dict):
        raise DesktopError("协议异常：响应缺少 result 对象")
    ok = result.get("ok")
    if not isinstance(ok, bool):
        raise DesktopError("协议异常：result.ok 不是布尔值")
    if ok:
        return result.get("value")
    error = result.get("error")
    if not isinstance(error, dict):
        raise DesktopError("协议异常：失败响应缺少 error 对象")
    raise DesktopError("DSH 桌面服务返回业务错误：code=%s" % _safe_code(error.get("code")))


def _safe_code(code):
    """只保留安全字符集内的业务 code；不携带服务端 message。"""
    if isinstance(code, bool) or code is None:
        return "unknown"
    if isinstance(code, int):
        return str(code)
    if isinstance(code, str) and _CODE_RE.fullmatch(code):
        return code
    return "unknown"
