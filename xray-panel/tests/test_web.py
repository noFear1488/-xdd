import base64
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

from xcpanel.control import Controller
from xcpanel.web import App

from .helpers import FakeRunner, make_store


class WebTest(unittest.TestCase):
    def setUp(self):
        self.store = make_store(password="correct horse")
        self.addCleanup(self.store.close)
        self.runner = FakeRunner()
        self.app = App(self.store, Controller(self.store, self.runner), self.runner.key.parent)
        self.httpd = self.app.server("127.0.0.1", 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.panel = self.base + "/panelpath123/"
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method, path, body=None, headers=None, base=None):
        req = urllib.request.Request(
            (base or self.panel) + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"X-Requested-With": "xcpanel", "Content-Type": "application/json", **(headers or {})},
        )
        try:
            with self.opener.open(req) as res:
                return res.status, res.read(), res.headers
        except urllib.error.HTTPError as err:
            return err.code, err.read(), err.headers

    def login(self):
        status, _, _ = self.call("POST", "api/login", {"username": "admin", "password": "correct horse"})
        self.assertEqual(status, 200)

    def test_unknown_paths_are_404(self):
        self.assertEqual(self.call("GET", "", base=self.base + "/")[0], 404)
        self.assertEqual(self.call("GET", "api/state", base=self.base + "/wrong/")[0], 404)

    def test_index_served_with_csp(self):
        status, body, headers = self.call("GET", "")
        self.assertEqual(status, 200)
        self.assertIn(b"xcpanel", body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])

    def test_api_needs_login_and_header(self):
        self.assertEqual(self.call("GET", "api/state")[0], 401)
        self.login()
        self.assertEqual(self.call("GET", "api/state")[0], 200)
        self.assertEqual(self.call("GET", "api/state", headers={"X-Requested-With": ""})[0], 403)

    def test_login_throttled(self):
        for _ in range(5):
            self.assertEqual(self.call("POST", "api/login", {"username": "admin", "password": "nope"})[0], 401)
        self.assertEqual(self.call("POST", "api/login", {"username": "admin", "password": "correct horse"})[0], 429)

    def test_client_lifecycle_and_subscription(self):
        self.login()
        status, body, _ = self.call("POST", "api/clients", {"name": "Alice", "limit_gb": 10, "expire_at": 0})
        self.assertEqual(status, 201)
        created = json.loads(body)
        self.assertEqual(created["limit_bytes"], 10 * 1024**3)
        self.assertEqual(self.call("POST", "api/clients", {"name": "Alice"})[0], 400)

        token = created["sub_url"].rsplit("/", 1)[1]
        status, body, headers = self.call("GET", token, base=self.base + "/subpath45678/")
        self.assertEqual(status, 200)
        links = base64.b64decode(body).decode().splitlines()
        self.assertEqual(len(links), 2)
        self.assertTrue(all(link.startswith("vless://" + created["uuid"]) for link in links))
        self.assertIn("total=10737418240", headers["Subscription-Userinfo"])
        self.assertEqual(base64.b64decode(headers["Profile-Title"][7:]).decode(), "nordpine")

        status, page, _ = self.call("GET", token, base=self.base + "/subpath45678/", headers={"Accept": "text/html"})
        self.assertIn(b"Subscription for <strong>Alice</strong>", page)

        self.call("PUT", f"api/clients/{created['id']}", {"name": "Alice", "enabled": False})
        _, body, _ = self.call("GET", token, base=self.base + "/subpath45678/")
        self.assertEqual(base64.b64decode(body), b"")

        self.assertEqual(self.call("GET", "nonexistent", base=self.base + "/subpath45678/")[0], 404)

    def test_state_hides_private_key(self):
        self.login()
        _, body, _ = self.call("GET", "api/state")
        state = json.loads(body)
        reality = state["nodes"][0]["transports"]["reality"]
        self.assertNotIn("private_key", reality)
        self.assertIn("public_key", reality)
        self.assertNotIn("admin_hash", state["settings"])

    def test_node_validation_errors_are_400(self):
        self.login()
        status, body, _ = self.call(
            "POST",
            "api/nodes",
            {"name": "DE", "address": "203.0.113.7", "ssh_host": "root@203.0.113.7", "caddy": True,
             "transports": {"xhttp": {"enabled": True, "path": "/x", "port": 10001}}},
        )  # fmt: skip
        self.assertEqual(status, 400)
        self.assertIn("domain", json.loads(body)["error"])

    def test_ssh_option_injection_rejected(self):
        self.login()
        status, _, _ = self.call(
            "POST", "api/nodes", {"name": "x", "address": "de.nordpine.site", "ssh_host": "-oProxyCommand=sh", "transports": {}}
        )
        self.assertEqual(status, 400)

    def test_password_change_signs_out_others(self):
        self.login()
        other = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        req = urllib.request.Request(
            self.panel + "api/login",
            data=json.dumps({"username": "admin", "password": "correct horse"}).encode(),
            headers={"X-Requested-With": "xcpanel"},
            method="POST",
        )
        other.open(req).read()
        status, _, _ = self.call("POST", "api/password", {"current": "correct horse", "new": "battery staple"})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "api/state")[0], 200)
        stale = urllib.request.Request(self.panel + "api/state", headers={"X-Requested-With": "xcpanel"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            other.open(stale)
        self.assertEqual(ctx.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
