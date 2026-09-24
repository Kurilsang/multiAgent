"""标准库 HTTP 客户端（隔离区取数专用，零第三方 HTTP 依赖）。

为什么不用第三方 HTTP 库：本仓踩过「依赖随传递依赖漂移」的坑（openai 3.x
改携 httpx2，直接 import httpx 运行时炸）；隔离区的请求形态有限
（GET/POST + 表单/JSON + 二进制回包 + UA 头），标准库足够且零供应链面。
主服务侧同款决策见 app/server.py 的 _UrllibCatalogClient。
"""

from __future__ import annotations

import json as _json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class HttpResponse:
    """最小响应合同（与测试替身同形：status_code / text / content）。"""

    status_code: int
    text: str
    content: bytes

    def json(self) -> dict:
        return _json.loads(self.text) if self.text else {}


class UrllibHttpClient:
    """GET/POST 客户端：params/headers/timeout/json/form data，4xx/5xx 不抛。"""

    def get(self, url: str, **kwargs) -> HttpResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> HttpResponse:
        return self.request("POST", url, **kwargs)

    def request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: float = 20.0,
        json=None,
        data=None,
        **_,
    ) -> HttpResponse:
        if params:
            kept = {k: v for k, v in params.items() if v not in (None, "")}
            if kept:
                url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(kept)
        body = None
        sent = dict(headers or {})
        if json is not None:
            body = _json.dumps(json).encode()
            sent.setdefault("Content-Type", "application/json")
        elif isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode()
            sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, (bytes, str)):
            body = data if isinstance(data, bytes) else data.encode()
        request = urllib.request.Request(url, data=body, headers=sent, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content = response.read()
                return HttpResponse(response.status, content.decode("utf-8", "replace"), content)
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也是响应，交给调用方判
            content = exc.read()
            return HttpResponse(exc.code, content.decode("utf-8", "replace"), content)
