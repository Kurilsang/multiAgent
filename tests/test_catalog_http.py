"""标准库 HTTP 客户端测试：本地 http.server 实测，零外网。"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from services.catalog.stdlib_http import UrllibHttpClient


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音测试输出
        pass

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/echo"):
            self._send(200, {"path": self.path, "ua": self.headers.get("User-Agent", "")})
        else:
            self._send(404, {"detail": "没找到"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode()
        self._send(200, {"body": raw, "ctype": self.headers.get("Content-Type", "")})


class UrllibHttpClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_get_with_params_and_headers(self):
        response = UrllibHttpClient().get(
            self.url + "/echo", params={"q": "技能", "skip": ""}, headers={"User-Agent": "ua-x"}
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertIn("q=%E6%8A%80%E8%83%BD", payload["path"])
        self.assertNotIn("skip", payload["path"])
        self.assertEqual(payload["ua"], "ua-x")

    def test_post_json_and_form(self):
        client = UrllibHttpClient()
        payload = client.post(self.url + "/echo", json={"a": 1}).json()
        self.assertEqual(json.loads(payload["body"]), {"a": 1})
        self.assertIn("application/json", payload["ctype"])
        payload = client.post(self.url + "/echo", data={"grant_type": "client_credentials"}).json()
        self.assertIn("grant_type=client_credentials", payload["body"])
        self.assertIn("x-www-form-urlencoded", payload["ctype"])

    def test_http_error_returns_response_not_exception(self):
        response = UrllibHttpClient().get(self.url + "/missing")
        self.assertEqual(response.status_code, 404)
        self.assertIn("没找到", response.text)


if __name__ == "__main__":
    unittest.main()
