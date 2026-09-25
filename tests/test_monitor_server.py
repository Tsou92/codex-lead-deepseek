"""Tests for the local monitor HTTP server and lifecycle CLI.

The HTTP tests use an ephemeral in-process ``ThreadingHTTPServer`` over a
temporary data root; the lifecycle tests spawn a real detached child process
with ``--port 0`` and always clean it up.  Nothing touches ``$HOME`` or a
persistent service.
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from leadseek import monitor_cli as mc  # noqa: E402
from leadseek import monitor_server as ms  # noqa: E402
from leadseek.monitor_data import MonitorStore  # noqa: E402

TOKEN = "test-token-0123456789"
INSTANCE = "instance-under-test"


def write_web(root):
    web = Path(root) / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "index.html").write_text("<!doctype html><script src=/app.js></script>", encoding="utf-8")
    (web / "app.js").write_text("console.log('monitor');", encoding="utf-8")
    (web / "styles.css").write_text("body{margin:0}", encoding="utf-8")
    (web / "secret.txt").write_text("do not serve", encoding="utf-8")
    return web


def fetch(port, path, cookie=None):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
    if cookie:
        request.add_header("Cookie", "%s=%s" % (ms.COOKIE_NAME, cookie))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def raw_request(port, path, method="GET", headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        if host is None:
            conn.request(method, path, headers=headers or {})
        else:
            conn.putrequest(method, path, skip_host=True)
            conn.putheader("Host", host)
            for key, value in (headers or {}).items():
                conn.putheader(key, value)
            conn.endheaders()
        response = conn.getresponse()
        body = response.read()
        return response, body
    finally:
        conn.close()


class HTTPServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".state" / "runs").mkdir(parents=True, exist_ok=True)
        self.web = write_web(self.root)
        self.server = ms.create_server(
            MonitorStore(self.root), 0, TOKEN, instance_id=INSTANCE, web_dir=self.web
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = self._auth_cookie()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def _auth_cookie(self):
        response, _ = raw_request(self.port, "/auth?token=" + TOKEN)
        self.assertEqual(response.status, 303)
        set_cookie = response.getheader("Set-Cookie") or ""
        return set_cookie.split(";")[0]

    def _authed_headers(self, extra=None):
        headers = {"Cookie": self.cookie}
        if extra:
            headers.update(extra)
        return headers

    # --------------------------------------------------------- health/auth
    def test_health_is_public_and_non_sensitive(self):
        response, body = raw_request(self.port, "/api/health")
        self.assertEqual(response.status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["service"], ms.SERVICE_NAME)
        self.assertEqual(payload["status"], "ok")
        self.assertNotIn("instance_id", payload)
        self.assertNotIn("pid", payload)
        self.assertNotIn(TOKEN, body.decode("utf-8"))
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
        self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))

    def test_auth_cookie_flow_and_authenticated_health(self):
        response, _ = raw_request(self.port, "/auth?token=" + TOKEN)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.getheader("Location"), "/")
        cookie = response.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        response, body = raw_request(self.port, "/api/health", headers=self._authed_headers())
        payload = json.loads(body)
        self.assertEqual(payload["instance_id"], INSTANCE)
        self.assertEqual(payload["port"], self.port)
        self.assertIsInstance(payload["pid"], int)

    def test_bad_token_rejected(self):
        response, _ = raw_request(self.port, "/auth?token=wrong")
        self.assertEqual(response.status, 401)
        response, _ = raw_request(self.port, "/api/overview")
        self.assertEqual(response.status, 401)
        response, _ = raw_request(self.port, "/api/overview", headers={"Cookie": ms.COOKIE_NAME + "=wrong"})
        self.assertEqual(response.status, 401)

    def test_non_ascii_auth_input_is_401_not_500(self):
        response, _ = raw_request(self.port, "/auth?token=%C3%A9")
        self.assertEqual(response.status, 401)
        response, _ = raw_request(
            self.port,
            "/api/overview",
            headers={"Cookie": ms.COOKIE_NAME + "=\u00e9"},
        )
        self.assertEqual(response.status, 401)
        response, body = raw_request(self.port, "/api/health", headers={"Cookie": ms.COOKIE_NAME + "=\u00e9"})
        self.assertEqual(response.status, 200)
        self.assertNotIn("instance_id", json.loads(body))

    def test_probe_ignores_environment_proxy(self):
        original_getproxies = urllib.request.getproxies
        original_bypass = urllib.request.proxy_bypass
        urllib.request.getproxies = lambda: {"http": "http://127.0.0.1:9"}
        urllib.request.proxy_bypass = lambda host: False
        try:
            health = mc._probe(self.port, TOKEN)
            self.assertIsInstance(health, dict)
            self.assertEqual(health.get("instance_id"), INSTANCE)
        finally:
            urllib.request.getproxies = original_getproxies
            urllib.request.proxy_bypass = original_bypass

    def test_static_rejects_symlinked_web_dir(self):
        link = Path(self.tmp.name) / "weblink"
        os.symlink(str(self.web), str(link))
        server = ms.create_server(MonitorStore(self.root), 0, TOKEN, instance_id=INSTANCE, web_dir=link)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ("/", "/app.js", "/styles.css"):
                response, _ = raw_request(port, path)
                self.assertEqual(response.status, 404, path)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_authenticated_overview(self):
        response, body = raw_request(self.port, "/api/overview", headers=self._authed_headers())
        self.assertEqual(response.status, 200)
        payload = json.loads(body)
        self.assertIn("runs", payload)
        self.assertIn("counts", payload)

    # --------------------------------------------------------- host/origin
    def test_host_header_must_be_loopback_and_bound_port(self):
        response, _ = raw_request(self.port, "/api/health", host="evil.example")
        self.assertEqual(response.status, 403)
        response, _ = raw_request(self.port, "/api/health", host="localhost:%d" % self.port)
        self.assertEqual(response.status, 403)
        response, _ = raw_request(self.port, "/api/health", host="127.0.0.1:9999")
        self.assertEqual(response.status, 403)
        response, _ = raw_request(self.port, "/api/health", host="127.0.0.1:%d" % self.port)
        self.assertEqual(response.status, 200)

    def test_origin_if_present_must_be_same_origin(self):
        same = "http://127.0.0.1:%d" % self.port
        response, _ = raw_request(self.port, "/api/health", headers={"Origin": same})
        self.assertEqual(response.status, 200)
        response, _ = raw_request(self.port, "/api/health", headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status, 403)
        response, _ = raw_request(self.port, "/api/health", headers={"Origin": "http://localhost:%d" % self.port})
        self.assertEqual(response.status, 403)
        response, _ = raw_request(self.port, "/auth?token=" + TOKEN, headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status, 403)

    # --------------------------------------------------------- methods/static
    def test_non_get_methods_rejected(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            response, _ = raw_request(self.port, "/api/overview", method=method, headers=self._authed_headers())
            self.assertEqual(response.status, 405, method)
            self.assertEqual(response.getheader("Allow"), "GET")

    def test_static_whitelist(self):
        response, body = raw_request(self.port, "/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.getheader("Content-Type"))
        self.assertIn(b"<script", body)
        response, body = raw_request(self.port, "/app.js")
        self.assertEqual(response.status, 200)
        self.assertIn("javascript", response.getheader("Content-Type"))
        response, _ = raw_request(self.port, "/styles.css")
        self.assertEqual(response.status, 200)
        self.assertIn("text/css", response.getheader("Content-Type"))

    def test_static_traversal_and_unknown_paths_rejected(self):
        for path in (
            "/../etc/passwd",
            "/%2e%2e/%2e%2e/etc/passwd",
            "/secret.txt",
            "/web/index.html",
            "/api/nope",
            "/api/runs/",
            "/api/codex/",
        ):
            response, _ = raw_request(self.port, path, headers=self._authed_headers())
            self.assertEqual(response.status, 404, path)

    # --------------------------------------------------------- validation
    def test_pagination_arguments_rejected(self):
        for query in ("offset=-1", "offset=abc", "limit=0", "limit=99999", "limit=x"):
            response, body = raw_request(self.port, "/api/overview?" + query, headers=self._authed_headers())
            self.assertEqual(response.status, 400, query)
            self.assertNotIn(b"Traceback", body)

    def test_unknown_run_and_artifact(self):
        response, _ = raw_request(self.port, "/api/runs/not-a-run", headers=self._authed_headers())
        self.assertEqual(response.status, 400)
        response, _ = raw_request(self.port, "/api/runs/20260101-000000-abcdef01", headers=self._authed_headers())
        self.assertEqual(response.status, 404)
        response, _ = raw_request(
            self.port,
            "/api/runs/20260101-000000-abcdef01/artifacts/secret",
            headers=self._authed_headers(),
        )
        self.assertEqual(response.status, 400)
        response, _ = raw_request(self.port, "/api/codex/not-a-uuid", headers=self._authed_headers())
        self.assertEqual(response.status, 400)

    def test_unknown_thread_not_linked(self):
        thread = "11111111-2222-3333-4444-555555555555"
        response, _ = raw_request(self.port, "/api/codex/" + thread, headers=self._authed_headers())
        self.assertEqual(response.status, 404)

    def test_bind_only_loopback(self):
        with self.assertRaises(ValueError):
            ms.create_server(MonitorStore(self.root), 0, TOKEN, host="0.0.0.0")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_web(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stale_pid_state_is_cleaned(self):
        monitor = self.root / ".state" / "monitor"
        monitor.mkdir(parents=True)
        mc._write_private(mc._token_path(self.root), "tok")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        mc._write_private(
            mc._state_path(self.root),
            json.dumps({"instance_id": "gone", "pid": dead.pid, "port": 1}),
        )
        payload, code = mc.stop(root=self.root)
        self.assertEqual(code, 0)
        self.assertTrue(payload.get("stale_pid"))
        self.assertFalse(mc._state_path(self.root).exists())

    def test_stop_refuses_foreign_instance(self):
        server = ms.create_server(MonitorStore(self.root), 0, "state-token", instance_id="real-instance")
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        sleep = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            monitor = self.root / ".state" / "monitor"
            monitor.mkdir(parents=True, exist_ok=True)
            mc._write_private(mc._token_path(self.root), "state-token")
            mc._write_private(
                mc._state_path(self.root),
                json.dumps({"instance_id": "other-instance", "pid": sleep.pid, "port": port}),
            )
            payload, code = mc.stop(root=self.root)
            self.assertNotEqual(code, 0)
            self.assertIn("拒绝", payload.get("message", ""))
            self.assertIsNone(sleep.poll())
        finally:
            sleep.terminate()
            sleep.wait()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_start_port_conflict_returns_nonzero(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            payload, code = mc.start(root=self.root, port=port, no_open=True, timeout=8)
            self.assertNotEqual(code, 0)
            self.assertEqual(payload.get("status"), "error")
            self.assertIn("启动失败", payload.get("message", ""))
        finally:
            holder.close()

    def test_start_reuse_and_stop(self):
        started, code = None, None
        try:
            payload, code = mc.start(root=self.root, port=0, no_open=True, timeout=15)
            self.assertEqual(code, 0, payload)
            self.assertTrue(payload["port"] > 0)
            self.assertTrue(payload["url"].startswith("http://127.0.0.1:%d/auth?token=" % payload["port"]))
            started = payload
            status_payload, status_code = mc.status(root=self.root)
            self.assertEqual(status_code, 0)
            self.assertTrue(status_payload["running"])
            reused, reuse_code = mc.start(root=self.root, port=0, no_open=True, timeout=15)
            self.assertEqual(reuse_code, 0)
            self.assertTrue(reused.get("reused"))
            self.assertEqual(reused["instance_id"], started["instance_id"])
            self.assertTrue(mc._is_alive(started["pid"]))
            token = started["url"].split("token=", 1)[1]
            for path, needle in (
                ("/", b"app.js"),
                ("/app.js", b"monitor"),
                ("/styles.css", b"body"),
            ):
                status, body = fetch(started["port"], path, cookie=token)
                self.assertEqual(status, 200, (path, body))
                self.assertIn(needle, body)
        finally:
            if started:
                stop_payload, stop_code = mc.stop(root=self.root)
                self.assertEqual(stop_code, 0, stop_payload)
                self.assertFalse(mc._is_alive(started["pid"]))

    def test_serve_creates_private_state_files(self):
        monitor = self.root / ".state" / "monitor"
        monitor.mkdir(parents=True)
        mc._write_private(mc._token_path(self.root), "tok-123")
        os.environ["LEADSEEK_MONITOR_INSTANCE_ID"] = "inst-x"
        try:
            server = ms.create_server(MonitorStore(self.root), 0, "tok-123", instance_id="inst-x")
            self.assertEqual(server.server_address[0], "127.0.0.1")
            server.server_close()
        finally:
            os.environ.pop("LEADSEEK_MONITOR_INSTANCE_ID", None)


class SymlinkAndPermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_symlinked_state_dir_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(str(outside), str(self.root / ".state"))
        with self.assertRaises(OSError):
            mc._ensure_dir(self.root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_monitor_dir_refused(self):
        state = self.root / ".state"
        state.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(str(outside), str(state / "monitor"))
        with self.assertRaises(OSError):
            mc._ensure_dir(self.root)

    def test_token_and_state_symlinks_not_followed(self):
        monitor = self.root / ".state" / "monitor"
        monitor.mkdir(parents=True)
        target = self.root / "target.txt"
        target.write_text("original", encoding="utf-8")
        os.symlink(str(target), str(monitor / "token"))
        with self.assertRaises(OSError):
            mc._write_private(mc._token_path(self.root), "attacker")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        os.symlink(str(target), str(monitor / "state.json"))
        self.assertIsNone(mc._read_json(mc._state_path(self.root)))
        self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_symlinked_lock_refused(self):
        monitor = self.root / ".state" / "monitor"
        monitor.mkdir(parents=True)
        target = self.root / "lock-target.txt"
        target.write_text("original", encoding="utf-8")
        os.symlink(str(target), str(monitor / "monitor.lock"))
        payload, code = mc.start(root=self.root, port=0, no_open=True, timeout=5)
        self.assertNotEqual(code, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_start_files_are_private(self):
        payload, code = mc.start(root=self.root, port=0, no_open=True, timeout=15)
        self.assertEqual(code, 0, payload)
        try:
            monitor = self.root / ".state" / "monitor"
            self.assertEqual(oct(monitor.stat().st_mode & 0o777), "0o700")
            for name in ("token", "monitor.lock", "monitor.log", "state.json"):
                path = monitor / name
                self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600", name)
                self.assertFalse(path.is_symlink(), name)
        finally:
            mc.stop(root=self.root)


if __name__ == "__main__":
    unittest.main()
