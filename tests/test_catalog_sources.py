"""源适配器测试：fixture 驱动（JSON/HTML），不发真实请求。

真实网络探针结论已回填 docs/specs/0002（skills.sh API = OIDC-only；
LobeHub 端点自 market-sdk 反查），真实注册/下载归人工验收票。
"""

import base64
import hashlib
import hmac
import json
import unittest

from services.catalog.config import CatalogSettings
from services.catalog.sources import build_sources
from services.catalog.sources.lobehub import LobeHubSource, sign_client_assertion
from services.catalog.sources.skills_sh import SkillsShSource, extract_entry_objects
from services.catalog.schema import CatalogError

from tests.fakes import FakeHttpClient, FakeResponse, load_catalog_fixture

HOME_HTML = load_catalog_fixture("skills-sh-home.html")
TOKEN_JSON = load_catalog_fixture("lobehub-token.json")
SEARCH_JSON = load_catalog_fixture("lobehub-search.json")


class SkillsShTest(unittest.TestCase):
    def make_source(self, routes=None):
        client = FakeHttpClient(
            routes
            or {
                "https://skills.sh/": FakeResponse(HOME_HTML),
            }
        )
        return SkillsShSource(client=client), client

    def test_crawl_extracts_embedded_entries(self):
        source, client = self.make_source()
        entries = source.crawl()
        by_id = {entry.id: entry for entry in entries}
        # 同一 fixture 喂三个榜单视图 + 内含重复 alpha：按 id 去重后 3 个唯一条目
        self.assertEqual(len(entries), 3)
        alpha = by_id["owner/repo/alpha"]
        self.assertEqual(alpha.installs, 500)
        self.assertEqual(alpha.tags, ("official",))
        self.assertEqual(alpha.detail_url, "https://skills.sh/owner/repo/alpha")
        self.assertFalse(by_id["owner/repo/beta"].is_duplicate)
        self.assertEqual(by_id["agent.qq.com/mail"].origin, "agent.qq.com")

    def test_crawl_sends_browser_ua_and_follows_views(self):
        source, client = self.make_source()
        source.crawl()
        urls = [url for _method, url, _kwargs in client.calls]
        self.assertEqual(len(urls), 3)  # /、/trending、/hot
        headers = client.calls[0][2]["headers"]
        self.assertIn("Mozilla", headers["User-Agent"])

    def test_empty_parse_raises_page_changed_hint(self):
        source, _ = self.make_source({"https://skills.sh/": FakeResponse("<html>无载荷</html>")})
        with self.assertRaises(CatalogError) as ctx:
            source.crawl()
        self.assertIn("改版", str(ctx.exception))
        self.assertEqual(ctx.exception.status, 502)

    def test_network_failure_raises(self):
        source, _ = self.make_source({"https://skills.sh/": ConnectionError("断网")})
        with self.assertRaises(CatalogError) as ctx:
            source.crawl()
        self.assertIn("请求失败", str(ctx.exception))

    def test_extractor_skips_malformed_payloads(self):
        html = '<script>self.__next_f.push([1,"\\uZZ坏转义"])</script>'
        self.assertEqual(extract_entry_objects(html), [])


class LobeHubTest(unittest.TestCase):
    def make_source(self, routes=None, creds=None):
        client = FakeHttpClient(
            routes
            or {
                "/oauth/token": FakeResponse(TOKEN_JSON),
                "/api/v1/skills": FakeResponse(SEARCH_JSON),
            }
        )
        source = LobeHubSource(
            client=client,
            creds_store=dict(creds if creds is not None else {"client_id": "cid", "client_secret": "csec"}),
        )
        return source, client

    def test_crawl_authenticates_and_maps_items(self):
        source, client = self.make_source()
        entries = source.crawl()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.id, "owner-repo")
        self.assertEqual(entry.description, "编辑 PDF 文件")
        self.assertEqual(entry.origin, "https://github.com/owner/repo")
        self.assertEqual(entry.stars, 100)
        self.assertTrue(entry.validated)
        self.assertEqual(entry.tags, ("pdf", "tool"))
        # 搜索请求带 Bearer
        search_calls = [c for c in client.calls if "/api/v1/skills" in c[1]]
        self.assertTrue(search_calls[0][2]["headers"]["Authorization"].startswith("Bearer "))

    def test_token_cached_across_crawls(self):
        source, client = self.make_source()
        source.crawl()
        source.crawl()
        token_calls = [c for c in client.calls if "/oauth/token" in c[1]]
        self.assertEqual(len(token_calls), 1)

    def test_missing_creds_raises_registration_hint(self):
        source, _ = self.make_source(creds={})
        with self.assertRaises(CatalogError) as ctx:
            source.crawl()
        self.assertIn("注册", str(ctx.exception))
        self.assertEqual(ctx.exception.status, 401)

    def test_register_stores_creds(self):
        source, client = self.make_source(
            routes={"/api/v1/clients/register": FakeResponse('{"clientId":"new-id","client_secret":"new-sec"}')},
            creds={},
        )
        creds = source.register()
        self.assertEqual(creds, {"client_id": "new-id", "client_secret": "new-sec"})
        method, url, kwargs = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("/api/v1/clients/register", url)
        self.assertEqual(kwargs["json"]["source"], "multiagent")

    def test_register_rate_limit_surfaces_chinese_error(self):
        source, _ = self.make_source(
            routes={"/api/v1/clients/register": FakeResponse("rate limited", 429)},
            creds={},
        )
        with self.assertRaises(CatalogError) as ctx:
            source.register()
        self.assertIn("429", str(ctx.exception))

    def test_sign_client_assertion_is_hs256_jwt(self):
        assertion = sign_client_assertion("cid", "csec", "https://market.lobehub.com/oauth/token")
        header_b64, payload_b64, sig_b64 = assertion.split(".")
        header = json.loads(base64.urlsafe_b64decode(header_b64 + "=="))
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        self.assertEqual(header["alg"], "HS256")
        self.assertEqual(payload["iss"], "cid")
        self.assertEqual(payload["sub"], "cid")
        self.assertEqual(payload["aud"], "https://market.lobehub.com/oauth/token")
        expected = hmac.new(b"csec", f"{header_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
        self.assertEqual(sig_b64, base64.urlsafe_b64encode(expected).rstrip(b"=").decode())


class BuildSourcesTest(unittest.TestCase):
    def test_registry_builds_configured_sources(self):
        settings = CatalogSettings(sources=("skills-sh", "lobehub", "unknown"))
        sources = build_sources(settings)
        self.assertEqual(sorted(sources), ["lobehub", "skills-sh"])
        self.assertIsInstance(sources["skills-sh"], SkillsShSource)
        self.assertIsInstance(sources["lobehub"], LobeHubSource)


if __name__ == "__main__":
    unittest.main()
