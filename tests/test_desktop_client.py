""":mod:`leadseek.desktop_client` 的单元测试。

所有流量都留在 127.0.0.1 上由脚本化的 ThreadingHTTPServer 处理：不访问真实 DSH
桌面服务、真实日志、真实凭据，也不发任何模型请求。日志一律使用临时目录里的
脱敏假日志。
"""

import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
import uuid

from leadseek.desktop_client import (
    DEFAULT_LOG_PATH,
    DEFAULT_URL,
    LOG_TAIL_BYTES,
    MAX_RESPONSE_BYTES,
    DesktopClient,
    DesktopError,
)

TOKEN = "fakeTokenValue123"
COOKIE = "dsh_session=fakeCookieValue456"
COOKIE_REFRESHED = "dsh_session=fakeCookieValue789"
RAW_SENTINEL = "RAW-SERVER-BODY-SENTINEL"


# --------------------------------------------------------------------- 假服务端


class ScriptedResponse:
    """一次脚本化的 HTTP 响应。"""

    def __init__(
        self,
        status=200,
        body=b"",
        headers=(),
        delay=0.0,
        declared_length=None,
        with_length=True,
        close=False,
    ):
        self.status = status
        self.body = body
        self.headers = list(headers)
        self.delay = delay
        self.declared_length = declared_length
        self.with_length = with_length
        self.close = close


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeDSH"
    sys_version = ""

    def log_message(self, format, *args):  # noqa: A002 - stdlib 签名
        pass

    def do_GET(self):
        try:
            server = self.server
            server.record("GET", self.path, dict(self.headers.items()))
            delay = server.next_auth_delay()
            if delay:
                time.sleep(delay)
            headers = [
                ("Set-Cookie", value) for value in server.next_auth_cookies()
            ]
            if server.auth_location is not None:
                headers.append(("Location", server.auth_location))
            self._respond(server.auth_status, b"", headers)
        except OSError:
            self.close_connection = True

    def do_POST(self):
        try:
            server = self.server
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            server.record("POST", self.path, dict(self.headers.items()), body)
            response = server.next_post_response(body)
            if response.delay:
                time.sleep(response.delay)
            self._respond(
                response.status,
                response.body,
                response.headers,
                declared_length=response.declared_length,
                with_length=response.with_length,
                close=response.close,
            )
        except OSError:
            self.close_connection = True

    def _respond(self, status, body=b"", headers=(), declared_length=None,
                 with_length=True, close=False):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if with_length:
            length = len(body) if declared_length is None else declared_length
            self.send_header("Content-Length", str(length))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        if close:
            self.close_connection = True


class FakeDSHServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.requests = []
        self.auth_status = 303
        self.auth_cookie = COOKIE
        self.auth_cookie_script = []
        self.auth_delays = []
        self.auth_location = "/"
        self.post_script = []
        self.post_default = None

    @property
    def port(self):
        return self.server_address[1]

    def record(self, method, path, headers, body=b""):
        with self.lock:
            self.requests.append(
                {"method": method, "path": path, "headers": headers, "body": body}
            )

    def clear_calls(self):
        with self.lock:
            self.requests.clear()

    def next_auth_cookies(self):
        with self.lock:
            if self.auth_cookie_script:
                value = self.auth_cookie_script.pop(0)
            else:
                value = self.auth_cookie
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    def next_auth_delay(self):
        with self.lock:
            if self.auth_delays:
                return self.auth_delays.pop(0)
        return 0.0

    def next_post_response(self, raw_body):
        with self.lock:
            if self.post_script:
                return self.post_script.pop(0)
        try:
            request = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ScriptedResponse(status=400, body=b"{}")
        if self.post_default is not None:
            return self.post_default(request)
        return ScriptedResponse(
            body=envelope(
                ok_result({"echo": request.get("method")}), request.get("rpcId")
            )
        )

    # -- 断言辅助 ---------------------------------------------------------

    def calls(self, method=None):
        with self.lock:
            items = list(self.requests)
        if method is None:
            return items
        return [item for item in items if item["method"] == method]

    def get_calls(self):
        return self.calls("GET")

    def post_calls(self):
        return self.calls("POST")


def envelope(result, rpc_id, envelope_type="server-response"):
    document = {"type": envelope_type, "rpcId": rpc_id, "result": result}
    return json.dumps(document).encode("utf-8")


def ok_result(value=None):
    return {"ok": True, "value": value}


def error_result(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


def respond(result, envelope_type="server-response"):
    """构造按请求 rpcId 回 ok/error 信封的 post_default 处理函数。"""

    def handler(request):
        return ScriptedResponse(
            body=envelope(result, request.get("rpcId"), envelope_type)
        )

    return handler


def respond_raw(body, status=200):
    return ScriptedResponse(status=status, body=body)


# ------------------------------------------------------------------- 测试基类


class DesktopClientTestCase(unittest.TestCase):
    def setUp(self):
        self.server = FakeDSHServer()
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base_url = "http://127.0.0.1:%d" % self.server.port

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # -- 辅助 -------------------------------------------------------------

    def write_log(self, text, name="server.log"):
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def default_log(self):
        return self.write_log(
            "2026-10-16 11:00:00 INFO desktop listening\n"
            "2026-10-16 11:00:01 INFO open this url: "
            "http://127.0.0.1:%d/?token=%s\n" % (self.server.port, TOKEN)
        )

    def make_client(self, url=None, log_path=None, timeout=5):
        return DesktopClient(
            url=self.base_url if url is None else url,
            log_path=self.default_log() if log_path is None else log_path,
            timeout=timeout,
        )


# ------------------------------------------------------------------- URL 校验


class UrlValidationTest(unittest.TestCase):
    def test_default_url_is_normalized_loopback_base(self):
        self.assertEqual(DEFAULT_URL, "http://127.0.0.1:3080")
        self.assertEqual(DesktopClient().url, "http://127.0.0.1:3080")

    def test_url_normalization(self):
        cases = {
            "http://127.0.0.1:3080/": "http://127.0.0.1:3080",
            "HTTP://LOCALHOST:3080/": "http://localhost:3080",
            "http://[::1]:3080": "http://[::1]:3080",
            "http://127.0.0.1": "http://127.0.0.1:80",
            "  http://127.0.0.1:9999  ": "http://127.0.0.1:9999",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(DesktopClient(url).url, expected)

    def test_unsafe_urls_are_rejected(self):
        urls = [
            "https://127.0.0.1:3080",
            "http://example.com:3080",
            "http://169.254.169.254:3080",
            "http://10.0.0.5:3080",
            "http://localhost.example.com:3080",
            "http://127.0.0.1.evil.test:3080",
            "http://user:pass@127.0.0.1:3080",
            "http://user@127.0.0.1:3080",
            "http://127.0.0.1:3080/?token=leakedTokenValue",
            "https://127.0.0.1:3080/?token=leakedTokenValue",
            "http://127.0.0.1:3080/api",
            "http://127.0.0.1:3080//",
            "http://127.0.0.1:3080/#frag",
            "http://127.0.0.1:notaport",
            "http://127.0.0.1:0",
            "http://127.0.0.1:70000",
            "",
            "   ",
            None,
            123,
        ]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(DesktopError) as ctx:
                    DesktopClient(url)
                self.assertNotIn("leakedTokenValue", str(ctx.exception))

    def test_invalid_timeout_and_log_path_are_rejected(self):
        for timeout in (0, -1, "15", None, True, float("nan"), float("inf"), 10 ** 20):
            with self.subTest(timeout=timeout):
                with self.assertRaises(DesktopError):
                    DesktopClient(timeout=timeout)
        for log_path in (123, "", "   ", b"/tmp/x", "\x00"):
            with self.subTest(log_path=log_path):
                with self.assertRaises(DesktopError):
                    DesktopClient(log_path=log_path)

    def test_default_log_path_is_the_desktop_log(self):
        client = DesktopClient()
        self.assertEqual(client._log_path, str(Path(DEFAULT_LOG_PATH).expanduser()))
        self.assertEqual(
            client._log_path, str(Path("~/Library/Logs/DSH/server.log").expanduser())
        )


# ------------------------------------------------------------------ token 读取


class TokenReadingTest(DesktopClientTestCase):
    def test_token_is_taken_from_the_last_match(self):
        log = self.write_log("banner token=oldToken\ntrailer token=newToken\nend\n")
        client = self.make_client(log_path=log)
        client.authenticate()
        self.assertEqual(self.server.get_calls()[0]["path"], "/?token=newToken")

    def test_only_the_last_8_mib_of_the_log_is_scanned(self):
        padding = "x" * 4096 + "\n"
        repeats = LOG_TAIL_BYTES // len(padding) + 2
        path = Path(self.tmp.name) / "big.log"
        with path.open("w", encoding="utf-8") as handle:
            handle.write("banner token=tooOldToken\n")
            for _ in range(repeats):
                handle.write(padding)
            handle.write("trailer token=freshToken\n")
        client = self.make_client(log_path=str(path))
        client.authenticate()
        self.assertEqual(self.server.get_calls()[0]["path"], "/?token=freshToken")

    def test_missing_log_is_rejected(self):
        client = self.make_client(log_path=str(Path(self.tmp.name) / "absent.log"))
        with self.assertRaises(DesktopError):
            client.authenticate()
        self.assertEqual(self.server.get_calls(), [])

    def test_log_without_token_is_rejected(self):
        log = self.write_log("nothing to see here\n")
        client = self.make_client(log_path=log)
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        self.assertNotIn(RAW_SENTINEL, str(ctx.exception))
        self.assertEqual(self.server.get_calls(), [])

    def test_symlink_log_is_rejected(self):
        real = self.write_log("token=%s\n" % TOKEN, name="real.log")
        link = Path(self.tmp.name) / "link.log"
        link.symlink_to(real)
        client = self.make_client(log_path=str(link))
        with self.assertRaises(DesktopError) as ctx:
            client.authenticate()
        self.assertIn("符号链接", str(ctx.exception))
        self.assertEqual(self.server.get_calls(), [])

    def test_directory_log_is_rejected(self):
        client = self.make_client(log_path=self.tmp.name)
        with self.assertRaises(DesktopError):
            client.authenticate()
        self.assertEqual(self.server.get_calls(), [])


# ------------------------------------------------------------------- 认证流程


class AuthenticationTest(DesktopClientTestCase):
    def test_authenticate_exchanges_token_for_in_memory_cookie(self):
        client = self.make_client()
        self.assertTrue(client.authenticate())
        gets = self.server.get_calls()
        self.assertEqual(len(gets), 1)
        self.assertEqual(gets[0]["path"], "/?token=" + TOKEN)
        self.assertNotIn("Cookie", gets[0]["headers"])
        self.assertEqual(client._cookie, COOKIE)
        # 只读日志：目录里没有新增文件，日志内容也没变。
        files = sorted(path.name for path in Path(self.tmp.name).iterdir())
        self.assertEqual(files, ["server.log"])

    def test_authenticate_accepts_302_and_303_without_following(self):
        for status in (302, 303):
            with self.subTest(status=status):
                self.server.clear_calls()
                self.server.auth_status = status
                self.server.auth_location = "/should-not-be-followed"
                client = self.make_client()
                self.assertTrue(client.authenticate())
                paths = [call["path"] for call in self.server.get_calls()]
                self.assertEqual(paths, ["/?token=" + TOKEN])

    def test_authenticate_rejects_unexpected_status(self):
        for status in (200, 301, 307, 400, 401, 500):
            with self.subTest(status=status):
                self.server.clear_calls()
                self.server.auth_status = status
                self.server.auth_location = "/should-not-be-followed"
                client = self.make_client()
                with self.assertRaises(DesktopError) as ctx:
                    client.authenticate()
                self.assertIn(str(status), str(ctx.exception))
                paths = [call["path"] for call in self.server.get_calls()]
                self.assertEqual(paths, ["/?token=" + TOKEN])

    def test_authenticate_rejects_bad_or_missing_cookie(self):
        for cookie in (None, "", "novalue", "bad name=value", "name=", "n@me=value"):
            with self.subTest(cookie=cookie):
                self.server.clear_calls()
                self.server.auth_cookie = cookie
                client = self.make_client()
                with self.assertRaises(DesktopError) as ctx:
                    client.authenticate()
                self.assertNotIn(TOKEN, str(ctx.exception))
                self.assertNotIn("fakeCookieValue", str(ctx.exception))

    def test_first_valid_set_cookie_entry_wins(self):
        self.server.auth_cookie = ["garbage", "first=cookie-one", "second=cookie-two"]
        client = self.make_client()
        self.assertTrue(client.authenticate())
        self.assertEqual(client._cookie, "first=cookie-one")


# --------------------------------------------------------------------- RPC


class RpcTest(DesktopClientTestCase):
    def test_rpc_sends_expected_request_and_returns_value(self):
        self.server.post_default = respond(ok_result({"sessionId": "s-1"}))
        client = self.make_client()
        value = client.rpc("session/create", {"title": "hello"})
        self.assertEqual(value, {"sessionId": "s-1"})

        posts = self.server.post_calls()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["path"], "/api/session/create")
        self.assertEqual(posts[0]["headers"].get("Content-Type"), "application/json")
        self.assertEqual(posts[0]["headers"].get("Cookie"), COOKIE)
        payload = json.loads(posts[0]["body"].decode("utf-8"))
        self.assertEqual(payload["type"], "client-request")
        self.assertEqual(payload["method"], "session/create")
        self.assertEqual(payload["payload"], {"args": {"title": "hello"}})
        self.assertEqual(str(uuid.UUID(payload["rpcId"])), payload["rpcId"])

    def test_rpc_defaults_args_to_empty_object(self):
        client = self.make_client()
        client.rpc("session/list")
        payload = json.loads(self.server.post_calls()[0]["body"].decode("utf-8"))
        self.assertEqual(payload["payload"], {"args": {}})
        self.assertEqual(payload["method"], "session/list")

    def test_rpc_ids_are_unique_per_call(self):
        client = self.make_client()
        client.rpc("session/list")
        client.rpc("session/list")
        ids = [
            json.loads(call["body"].decode("utf-8"))["rpcId"]
            for call in self.server.post_calls()
        ]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])

    def test_rpc_returns_arbitrary_json_values(self):
        values = [None, True, 0, "text", [], [1, 2, 3], {"nested": {"a": [1]}}]
        client = self.make_client()
        for value in values:
            with self.subTest(value=value):
                self.server.post_default = respond(ok_result(value))
                self.assertEqual(client.rpc("session/create"), value)

    def test_rpc_authenticates_only_once_for_many_calls(self):
        client = self.make_client()
        client.rpc("session/list")
        client.rpc("session/list")
        client.rpc("session/list")
        self.assertEqual(len(self.server.get_calls()), 1)
        self.assertEqual(len(self.server.post_calls()), 3)

    def test_rpc_validates_endpoint_before_any_traffic(self):
        endpoints = [
            "",
            "session",
            "a/b/c",
            "session/create/",
            "/session/create",
            "session//create",
            "session/create?x=1",
            "session/../secret",
            "../session",
            "session/create\nX",
            "session/crea te",
            "session/create#frag",
            None,
            123,
        ]
        client = self.make_client()
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(DesktopError):
                    client.rpc(endpoint)
        self.assertEqual(self.server.get_calls(), [])
        self.assertEqual(self.server.post_calls(), [])

    def test_rpc_rejects_unserializable_args(self):
        client = self.make_client()
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create", {"bad": object()})
        self.assertNotIn("object", str(ctx.exception))
        deeply_nested = {}
        for _ in range(20000):
            deeply_nested = {"next": deeply_nested}
        with self.assertRaises(DesktopError):
            client.rpc("session/create", deeply_nested)
        self.assertEqual(self.server.get_calls(), [])
        self.assertEqual(self.server.post_calls(), [])


# ----------------------------------------------------------------- 协议异常


class ProtocolErrorTest(DesktopClientTestCase):
    def test_malformed_json_is_rejected(self):
        self.server.post_script.append(respond_raw(b"{" + RAW_SENTINEL.encode()))
        client = self.make_client()
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        self.assertNotIn(RAW_SENTINEL, str(ctx.exception))
        self.assertEqual(len(self.server.post_calls()), 1)

    def test_non_object_response_is_rejected(self):
        self.server.post_script.append(respond_raw(json.dumps([1, 2, 3]).encode()))
        client = self.make_client()
        with self.assertRaises(DesktopError):
            client.rpc("session/create")

    def test_wrong_envelope_type_is_rejected(self):
        self.server.post_default = respond(ok_result("x"), envelope_type="client-request")
        client = self.make_client()
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        self.assertIn("协议异常", str(ctx.exception))
        self.assertEqual(len(self.server.post_calls()), 1)

    def test_rpc_id_mismatch_is_rejected(self):
        self.server.post_script.append(
            respond_raw(
                envelope(ok_result("x"), "00000000-0000-4000-8000-000000000000")
            )
        )
        client = self.make_client()
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        self.assertIn("协议异常", str(ctx.exception))

    def test_missing_result_or_bad_ok_is_rejected(self):
        templates = [
            {"type": "server-response"},
            {"type": "server-response", "result": []},
            {"type": "server-response", "result": {}},
            {"type": "server-response", "result": {"ok": "true"}},
            {"type": "server-response", "result": {"ok": 1}},
        ]
        client = self.make_client()
        for template in templates:
            with self.subTest(template=template):

                def handler(request, template=template):
                    document = dict(template)
                    document["rpcId"] = request.get("rpcId")
                    return ScriptedResponse(body=json.dumps(document).encode("utf-8"))

                self.server.post_default = handler
                with self.assertRaises(DesktopError) as ctx:
                    client.rpc("session/create")
                self.assertIn("协议异常", str(ctx.exception))

    def test_http_error_status_is_rejected_without_body_leak(self):
        self.server.post_script.append(
            respond_raw(("{" + RAW_SENTINEL + "}").encode(), status=500)
        )
        client = self.make_client()
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        self.assertIn("500", str(ctx.exception))
        self.assertNotIn(RAW_SENTINEL, str(ctx.exception))
        self.assertEqual(len(self.server.post_calls()), 1)

    def test_declared_oversize_response_is_rejected(self):
        self.server.post_script.append(
            ScriptedResponse(
                status=200, body=b"", declared_length=MAX_RESPONSE_BYTES + 1, close=True
            )
        )
        client = self.make_client()
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        self.assertNotIn(RAW_SENTINEL, str(ctx.exception))
        self.assertEqual(len(self.server.post_calls()), 1)

    def test_streamed_oversize_response_is_rejected(self):
        self.server.post_script.append(
            ScriptedResponse(body=b"x" * 4096, with_length=False, close=True)
        )
        client = self.make_client()
        with mock.patch("leadseek.desktop_client.MAX_RESPONSE_BYTES", 1024):
            with self.assertRaises(DesktopError) as ctx:
                client.rpc("session/create")
        self.assertNotIn("xxxx", str(ctx.exception))
        self.assertEqual(len(self.server.post_calls()), 1)


# ----------------------------------------------------------------- 业务错误


class BusinessErrorTest(DesktopClientTestCase):
    def test_code_is_preserved_and_message_is_hidden(self):
        secret = "internal detail token=%s cookie=%s" % (TOKEN, COOKIE)
        self.server.post_default = respond(error_result("E_SESSION_LIMIT", secret))
        client = self.make_client()
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        text = str(ctx.exception)
        self.assertIn("E_SESSION_LIMIT", text)
        self.assertNotIn("internal detail", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(COOKIE, text)

    def test_unsafe_code_is_sanitized(self):
        unsafe_codes = [
            "bad code with spaces",
            "code\ninjected",
            "token=%s" % TOKEN,
            {"nested": "object"},
            ["list"],
        ]
        client = self.make_client()
        for code in unsafe_codes:
            with self.subTest(code=code):
                self.server.post_default = respond(error_result(code, "hidden"))
                with self.assertRaises(DesktopError) as ctx:
                    client.rpc("session/create")
                self.assertIn("code=unknown", str(ctx.exception))
                self.assertNotIn(TOKEN, str(ctx.exception))

    def test_numeric_code_and_missing_error_object(self):
        self.server.post_default = respond(
            {"ok": False, "error": {"code": 42, "message": "hidden"}}
        )
        client = self.make_client()
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        self.assertIn("code=42", str(ctx.exception))

        for result in ({"ok": False}, {"ok": False, "error": "text"}):
            with self.subTest(result=result):
                self.server.post_default = respond(result)
                with self.assertRaises(DesktopError) as ctx:
                    client.rpc("session/create")
                self.assertIn("协议异常", str(ctx.exception))


# ------------------------------------------------------------------- 重试策略


class RetryPolicyTest(DesktopClientTestCase):
    def test_401_triggers_one_reauth_and_replay(self):
        self.server.auth_cookie_script = [[COOKIE], [COOKIE_REFRESHED]]
        self.server.post_script.append(ScriptedResponse(status=401, body=b"{}"))
        client = self.make_client()
        value = client.rpc("session/create", {"title": "hello"})
        self.assertEqual(value, {"echo": "session/create"})

        self.assertEqual(len(self.server.get_calls()), 2)
        posts = self.server.post_calls()
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["headers"].get("Cookie"), COOKIE)
        self.assertEqual(posts[1]["headers"].get("Cookie"), COOKIE_REFRESHED)
        first = json.loads(posts[0]["body"].decode("utf-8"))
        second = json.loads(posts[1]["body"].decode("utf-8"))
        self.assertEqual(first["rpcId"], second["rpcId"])
        self.assertEqual(first["payload"], second["payload"])

    def test_second_401_is_not_retried_again(self):
        self.server.post_script.extend(
            [
                ScriptedResponse(status=401, body=b"{}"),
                ScriptedResponse(status=401, body=b"{}"),
            ]
        )
        client = self.make_client()
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        self.assertIn("401", str(ctx.exception))
        self.assertEqual(len(self.server.post_calls()), 2)
        self.assertEqual(len(self.server.get_calls()), 2)

    def test_401_without_refresh_cookie_stops_without_replay(self):
        self.server.post_script.append(ScriptedResponse(status=401, body=b"{}"))
        self.server.auth_cookie_script = [[COOKIE], None]
        client = self.make_client()
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertEqual(len(self.server.post_calls()), 1)
        self.assertEqual(len(self.server.get_calls()), 2)

    def test_server_error_is_not_replayed(self):
        self.server.post_script.append(
            ScriptedResponse(status=500, body=b'{"error": "boom"}')
        )
        client = self.make_client()
        with self.assertRaises(DesktopError):
            client.rpc("session/create")
        self.assertEqual(len(self.server.post_calls()), 1)
        self.assertEqual(len(self.server.get_calls()), 1)

    def test_protocol_error_is_not_replayed(self):
        self.server.post_script.append(respond_raw(b"not json"))
        client = self.make_client()
        with self.assertRaises(DesktopError):
            client.rpc("session/create")
        self.assertEqual(len(self.server.post_calls()), 1)

    def test_timeout_is_not_replayed(self):
        self.server.post_script.append(ScriptedResponse(delay=1.5, body=b"{}"))
        client = self.make_client(timeout=0.3)
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        self.assertIn("超时", str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertEqual(len(self.server.post_calls()), 1)
        self.assertEqual(len(self.server.get_calls()), 1)

    def test_initial_auth_timeout_sends_no_request(self):
        self.server.auth_delays = [1.5]
        client = self.make_client(timeout=0.3)
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        self.assertIn("超时", str(ctx.exception))
        self.assertEqual(len(self.server.post_calls()), 0)

    def test_refresh_auth_timeout_stops_without_replay(self):
        self.server.auth_delays = [0.0, 1.5]
        self.server.post_script.append(ScriptedResponse(status=401, body=b"{}"))
        client = self.make_client(timeout=0.3)
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertEqual(len(self.server.post_calls()), 1)
        self.assertEqual(len(self.server.get_calls()), 2)


class ConnectionFailureTest(unittest.TestCase):
    def test_connection_refused_is_a_desktop_error(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "server.log"
            log.write_text("token=%s\n" % TOKEN, encoding="utf-8")
            client = DesktopClient(
                url="http://127.0.0.1:%d" % port, log_path=str(log), timeout=2
            )
            with self.assertRaises(DesktopError) as ctx:
                client.rpc("session/create")
            # 兜底：即使内部超时被改成 socket 无法表示的值，也只抛 DesktopError。
            client._timeout = 10 ** 20
            with self.assertRaises(DesktopError):
                client.rpc("session/create")
        text = str(ctx.exception)
        self.assertIn("无法连接", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("Traceback", text)


# ------------------------------------------------------------------- 生命周期


class LifecycleTest(DesktopClientTestCase):
    def test_context_manager_closes_the_client(self):
        with self.make_client() as client:
            self.assertEqual(client.rpc("session/create"), {"echo": "session/create"})
            self.assertFalse(client._closed)
        self.assertTrue(client._closed)
        self.assertIsNone(client._cookie)
        with self.assertRaises(DesktopError) as ctx:
            client.rpc("session/create")
        self.assertIn("已关闭", str(ctx.exception))
        with self.assertRaises(DesktopError):
            client.authenticate()

    def test_close_is_idempotent_and_repr_has_no_secrets(self):
        client = self.make_client()
        client.authenticate()
        self.assertEqual(client._cookie, COOKIE)
        self.assertNotIn(COOKIE, repr(client))
        self.assertNotIn(TOKEN, repr(client))
        client.close()
        client.close()
        self.assertTrue(client._closed)
        self.assertIsNone(client._cookie)

    def test_client_is_reusable_after_a_protocol_error(self):
        self.server.post_script.append(respond_raw(b"not json"))
        client = self.make_client()
        with self.assertRaises(DesktopError):
            client.rpc("session/create")
        self.assertEqual(client.rpc("session/create"), {"echo": "session/create"})
        self.assertEqual(len(self.server.post_calls()), 2)

    def test_cookie_is_never_written_to_disk(self):
        client = self.make_client()
        client.rpc("session/create")
        files = sorted(path.name for path in Path(self.tmp.name).iterdir())
        self.assertEqual(files, ["server.log"])
        for path in Path(self.tmp.name).iterdir():
            self.assertNotIn("fakeCookieValue", path.read_text(encoding="utf-8"))

    def test_localhost_url_connects_to_loopback_without_dns(self):
        client = self.make_client(url="http://localhost:%d" % self.server.port)
        self.assertEqual(client.url, "http://localhost:%d" % self.server.port)
        self.assertEqual(client.rpc("session/create"), {"echo": "session/create"})

    def test_secrets_never_appear_in_error_messages(self):
        def business_error():
            self.server.post_default = respond(
                error_result("E_X", "token=%s cookie=%s" % (TOKEN, COOKIE))
            )

        def mismatched_envelope():
            self.server.post_default = None
            self.server.post_script.append(
                respond_raw(
                    json.dumps(
                        {
                            "type": "server-response",
                            "rpcId": "mismatch",
                            "token": TOKEN,
                            "cookie": COOKIE,
                        }
                    ).encode()
                )
            )

        def error_status():
            self.server.post_default = None
            self.server.post_script.append(respond_raw(b"token=" + TOKEN.encode(), 500))

        def broken_body():
            self.server.post_default = None
            self.server.post_script.append(
                respond_raw(b"token=" + TOKEN.encode() + b" not-json")
            )

        for configure in (
            business_error,
            mismatched_envelope,
            error_status,
            broken_body,
        ):
            with self.subTest(configure=configure.__name__):
                self.server.post_script.clear()
                configure()
                client = self.make_client()
                with self.assertRaises(DesktopError) as ctx:
                    client.rpc("session/create")
                text = str(ctx.exception)
                self.assertNotIn(TOKEN, text)
                self.assertNotIn(COOKIE, text)
                self.assertNotIn("fakeCookieValue", text)


if __name__ == "__main__":
    unittest.main()
