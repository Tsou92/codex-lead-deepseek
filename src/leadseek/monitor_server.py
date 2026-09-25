"""Local read-only HTTP monitor for the task data store.

Only binds ``127.0.0.1`` and exposes a small, fixed API that maps directly to
:class:`leadseek.monitor_data.MonitorStore`.  There is no write path: non-GET
requests are rejected, static files are a hard whitelist and every API call
except ``/api/health`` requires the random access token delivered through the
``/auth`` cookie exchange.

Safety properties:

* ``Host`` must be exactly ``127.0.0.1:<bound port>`` and, when present,
  ``Origin`` must be the matching same-origin URL (no permissive localhost or
  external origins);
* the auth token is never echoed into logs or the public health payload;
* pagination arguments are validated rather than clamped, unknown routes are
  404 and errors never leak a traceback or local file content.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional

from urllib.parse import parse_qs, urlsplit

from . import __version__
from . import monitor_data

COOKIE_NAME = "leadseek_monitor_token"
SERVICE_NAME = "leadseek-monitor"
DEFAULT_WEB_DIR = Path(__file__).resolve().parent / "web"

STATIC_FILES: Dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
}

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)

MAX_QUERY_LEN = 500
_PAGINATION_RE = re.compile(r"^[0-9]+$")


class Forbidden(Exception):
    """The request failed Host/Origin validation."""


class Unauthorized(Exception):
    """The request did not present a valid access token."""


class BadRequest(Exception):
    """A query parameter was malformed or out of range."""


class NotFound(Exception):
    """No route or resource matched the request."""


class MonitorRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "leadseek-monitor"
    sys_version = ""

    # ------------------------------------------------------------ logging
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        # Silence the stock logger; log_request does the sanitised version.
        pass

    def log_error(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def log_request(self, code="-", size="-"):
        # Never log the raw request target: /auth carries the token in the
        # query string.  Only the path is recorded.
        try:
            path = urlsplit(self.path).path
        except Exception:
            path = "/"
        try:
            client = self.client_address[0]
        except Exception:
            client = "-"
        sys.stderr.write('%s - - "%s %s" %s %s\n' % (client, self.command, path, code, size))

    # ------------------------------------------------------------ methods
    def do_GET(self):  # noqa: N802 - stdlib naming
        self._handle()

    def _reject_method(self):
        self._respond(
            HTTPStatus.METHOD_NOT_ALLOWED,
            json.dumps({"error": "仅支持 GET"}, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            {"Allow": "GET"},
        )

    do_HEAD = _reject_method
    do_POST = _reject_method
    do_PUT = _reject_method
    do_PATCH = _reject_method
    do_DELETE = _reject_method
    do_OPTIONS = _reject_method

    # ------------------------------------------------------------ plumbing
    def _respond(self, status, body: bytes, content_type: str, extra: Optional[dict] = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status, payload, extra: Optional[dict] = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._respond(status, body, "application/json; charset=utf-8", extra)

    def _bound_port(self) -> int:
        return int(self.server.server_address[1])

    def _check_request(self):
        expected_host = "127.0.0.1:%d" % self._bound_port()
        if self.headers.get("Host", "") != expected_host:
            raise Forbidden()
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + expected_host:
            raise Forbidden()

    def _cookie_token(self) -> Optional[str]:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel is not None else None

    def _token_ok(self, supplied) -> bool:
        # ``hmac.compare_digest`` raises ``TypeError`` for non-ASCII ``str``
        # input; compare bytes so a malformed cookie/token is a clean 401
        # instead of a 500.
        if not isinstance(supplied, str) or not supplied:
            return False
        try:
            return hmac.compare_digest(supplied.encode("utf-8"), self.server.token.encode("utf-8"))
        except (TypeError, UnicodeError):
            return False

    def _authenticated(self):
        if not self._token_ok(self._cookie_token()):
            raise Unauthorized()

    # ------------------------------------------------------------ dispatch
    def _handle(self):
        try:
            self._check_request()
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query, keep_blank_values=True)
            if path == "/auth":
                self._auth(query)
            elif path == "/api/health":
                self._health()
            elif path == "/api/overview":
                self._authenticated()
                self._overview(query)
            elif path.startswith("/api/runs/"):
                self._authenticated()
                self._run_route(path, query)
            elif path.startswith("/api/codex/"):
                self._authenticated()
                self._codex_route(path, query)
            elif path in ("/", "/index.html"):
                self._static("index.html")
            elif path in ("/app.js", "/styles.css"):
                self._static(path[1:])
            else:
                raise NotFound()
        except Forbidden:
            self._json(HTTPStatus.FORBIDDEN, {"error": "请求来源不被允许"})
        except Unauthorized:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "未认证或令牌无效"})
        except NotFound:
            self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})
        except BadRequest as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except ValueError as error:
            message = str(error)
            status = HTTPStatus.NOT_FOUND if message.startswith(("找不到", "未关联")) else HTTPStatus.BAD_REQUEST
            self._json(status, {"error": message})
        except Exception:
            # No traceback and no local file content to the client.
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "服务器内部错误"})

    # ------------------------------------------------------------ routes
    def _auth(self, query):
        supplied = (query.get("token") or [""])[0]
        if not self._token_ok(supplied):
            raise Unauthorized()
        cookie = "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (COOKIE_NAME, self.server.token)
        self._respond(
            HTTPStatus.SEE_OTHER,
            b"",
            "text/plain; charset=utf-8",
            {"Location": "/", "Set-Cookie": cookie},
        )

    def _health(self):
        payload = {"service": SERVICE_NAME, "status": "ok", "version": __version__}
        if self._token_ok(self._cookie_token()):
            payload["instance_id"] = self.server.instance_id
            payload["pid"] = os.getpid()
            payload["port"] = self._bound_port()
        self._json(HTTPStatus.OK, payload)

    def _overview(self, query):
        offset = self._int_param(query, "offset", 0, 0, 10 ** 9)
        limit = self._int_param(query, "limit", monitor_data.EVENT_LIMIT_DEFAULT, 1, monitor_data.EVENT_LIMIT_MAX)
        workspace = self._str_param(query, "workspace")
        search = self._str_param(query, "q")
        status = self._str_param(query, "status")
        result = self.server.store.overview(
            workspace=workspace or None,
            query=search,
            status=status,
            offset=offset,
            limit=limit,
        )
        self._json(HTTPStatus.OK, result)

    def _run_route(self, path, query):
        rest = path[len("/api/runs/"):]
        parts = rest.split("/")
        store = self.server.store
        if len(parts) == 1 and parts[0]:
            result = store.run_detail(parts[0])
        elif len(parts) == 2 and parts[0] and parts[1] == "events":
            after = self._int_param(query, "after", 0, 0, 10 ** 9)
            limit = self._int_param(query, "limit", monitor_data.EVENT_LIMIT_DEFAULT, 1, monitor_data.EVENT_LIMIT_MAX)
            agent_id = self._str_param(query, "agent_id")
            result = store.run_events(parts[0], after=after, limit=limit, agent_id=agent_id or None)
        elif len(parts) == 3 and parts[0] and parts[1] == "artifacts" and parts[2]:
            result = store.artifact(parts[0], parts[2])
        else:
            raise NotFound()
        self._json(HTTPStatus.OK, result)

    def _codex_route(self, path, query):
        rest = path[len("/api/codex/"):]
        parts = rest.split("/")
        if len(parts) != 1 or not parts[0]:
            raise NotFound()
        after = self._int_param(query, "after", 0, 0, 10 ** 9)
        limit = self._int_param(query, "limit", monitor_data.EVENT_LIMIT_DEFAULT, 1, monitor_data.EVENT_LIMIT_MAX)
        result = self.server.store.codex_detail(parts[0], after=after, limit=limit)
        self._json(HTTPStatus.OK, result)

    def _static(self, name: str):
        if name not in STATIC_FILES:
            raise NotFound()
        web_dir = self.server.web_dir
        if web_dir is None:
            raise NotFound()
        web_dir = Path(web_dir)
        # The web root itself must not be a symlink; a symlinked directory
        # would otherwise let the whitelist resolve outside the intended tree.
        if web_dir.is_symlink() or not web_dir.is_dir():
            raise NotFound()
        path = web_dir / name
        if path.is_symlink() or not path.is_file():
            raise NotFound()
        try:
            path.resolve().relative_to(web_dir.resolve())
        except (ValueError, OSError):
            raise NotFound()
        body = path.read_bytes()
        self._respond(HTTPStatus.OK, body, STATIC_FILES[name])

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _int_param(query, name, default, minimum, maximum):
        values = query.get(name)
        if not values:
            return default
        value = values[0]
        if not _PAGINATION_RE.match(value):
            raise BadRequest("参数 %s 必须是非负整数" % name)
        number = int(value)
        if number < minimum or number > maximum:
            raise BadRequest("参数 %s 超出允许范围" % name)
        return number

    @staticmethod
    def _str_param(query, name):
        values = query.get(name)
        if not values:
            return ""
        value = values[0]
        if len(value) > MAX_QUERY_LEN:
            raise BadRequest("参数 %s 过长" % name)
        return value


class MonitorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # ``HTTPServer.server_bind`` performs ``socket.getfqdn`` which can hang
        # in sandboxed/offline environments.  Binding to loopback needs no
        # reverse DNS, so skip it and use the literal address.
        import socketserver

        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = int(self.server_address[1])

    def __init__(self, address, handler, store, token, instance_id, web_dir):
        super().__init__(address, handler)
        self.store = store
        self.token = token
        self.instance_id = instance_id
        self.web_dir = Path(web_dir) if web_dir is not None else None


def create_server(store, port, token, instance_id="", host="127.0.0.1", web_dir=None):
    """Create the monitor HTTP server bound only to loopback."""
    if host != "127.0.0.1":
        raise ValueError("监控服务只能绑定 127.0.0.1")
    if not isinstance(token, str) or not token:
        raise ValueError("访问令牌不能为空")
    return MonitorHTTPServer((host, int(port)), MonitorRequestHandler, store, token, instance_id, web_dir)
