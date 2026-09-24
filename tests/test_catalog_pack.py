"""目录包获取测试：skills.sh git 坐标拉取 + LobeHub ZIP 解包（零真实网络/克隆）。"""

import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from services.catalog.schema import CatalogError
from services.catalog.sources.lobehub import LobeHubSource, _zip_to_pack
from services.catalog.sources.skills_sh import SkillsShSource, parse_audits

from tests.fakes import FakeHttpClient, FakeResponse

SKILL_MD = "---\nname: alpha\ndescription: 测试技能\ntools: []\n---\n\n正文内容。\n"
AUDIT_HTML = (
    '<a href="/owner/repo/alpha/security/socket"><span>Socket</span>'
    '<span class="green">Pass</span></a>'
    '<a href="/owner/repo/alpha/security/snyk"><span>Snyk</span>'
    '<span class="amber">Warn</span></a>'
)


def make_zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class SkillsShPackTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "fixture-repo"
        (self.repo / "alpha" / "scripts").mkdir(parents=True)
        (self.repo / "alpha" / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
        (self.repo / "alpha" / "scripts" / "run.js").write_text("x", encoding="utf-8")
        (self.repo / "skills" / "beta").mkdir(parents=True)
        (self.repo / "skills" / "beta" / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
        self.clones: list[tuple[str, str]] = []

    def tearDown(self):
        self._tmp.cleanup()

    def cloner(self, url: str, dest: str) -> None:
        self.clones.append((url, dest))
        shutil.copytree(self.repo, dest)

    def make_source(self):
        client = FakeHttpClient({"skills.sh/owner/repo/alpha": FakeResponse(AUDIT_HTML)})
        return SkillsShSource(client=client, cloner=self.cloner)

    def test_fetch_pack_collects_skill_files(self):
        pack = self.make_source().fetch_pack("owner/repo/alpha")
        self.assertEqual([path for path, _ in pack.files], ["SKILL.md"])
        self.assertEqual(pack.extra_files, ("scripts/run.js",))
        self.assertEqual(pack.origin, "https://github.com/owner/repo")
        self.assertEqual(self.clones[0][0], "https://github.com/owner/repo")

    def test_fetch_pack_supports_skills_subdir_layout(self):
        pack = self.make_source().fetch_pack("owner/repo/beta")
        self.assertEqual(pack.files[0][0], "SKILL.md")

    def test_wellknown_ref_rejected(self):
        with self.assertRaises(CatalogError) as ctx:
            self.make_source().fetch_pack("agent.qq.com/mail")
        self.assertIn("well-known", str(ctx.exception))

    def test_missing_slug_reports_not_found(self):
        with self.assertRaises(CatalogError) as ctx:
            self.make_source().fetch_pack("owner/repo/gone")
        self.assertEqual(ctx.exception.status, 404)

    def test_clone_failure_wrapped(self):
        def broken(url, dest):
            raise RuntimeError("git 不存在")

        source = SkillsShSource(client=FakeHttpClient(), cloner=broken)
        with self.assertRaises(CatalogError) as ctx:
            source.fetch_pack("owner/repo/alpha")
        self.assertIn("克隆失败", str(ctx.exception))

    def test_detail_returns_manifest_text_and_audits(self):
        detail = self.make_source().detail("owner/repo/alpha")
        self.assertIn("正文内容", detail.manifest_text)
        self.assertEqual(detail.manifest_path, "SKILL.md")
        self.assertEqual(detail.entry.name, "alpha")
        self.assertEqual([b.provider for b in detail.audits], ["Socket", "Snyk"])
        self.assertEqual(detail.audits[1].status, "warn")


class ParseAuditsTest(unittest.TestCase):
    def test_parses_provider_status_rows(self):
        badges = parse_audits(AUDIT_HTML)
        self.assertEqual([(b.provider, b.status) for b in badges], [("Socket", "pass"), ("Snyk", "warn")])

    def test_empty_page_gives_no_badges(self):
        self.assertEqual(parse_audits("<html></html>"), ())


class LobeHubPackTest(unittest.TestCase):
    TOKEN = '{"access_token":"tok","token_type":"Bearer","expires_in":3600}'

    def make_source(self, blob: bytes):
        client = FakeHttpClient(
            {
                "/oauth/token": FakeResponse(self.TOKEN),
                "/download": FakeResponse(content=blob),
            }
        )
        return LobeHubSource(client=client, creds_store={"client_id": "c", "client_secret": "s"})

    def test_zip_unpacked_with_root_stripped(self):
        blob = make_zip({"owner-repo/SKILL.md": SKILL_MD, "owner-repo/assets/run.js": "x"})
        pack = self.make_source(blob).fetch_pack("owner-repo")
        self.assertEqual([path for path, _ in pack.files], ["SKILL.md"])
        self.assertEqual(pack.extra_files, ("assets/run.js",))

    def test_zip_slip_rejected(self):
        blob = make_zip({"../evil.md": "x", "SKILL.md": SKILL_MD})
        with self.assertRaises(CatalogError) as ctx:
            _zip_to_pack(blob, origin="x")
        self.assertIn("zip-slip", str(ctx.exception))

    def test_missing_skill_md_rejected(self):
        with self.assertRaises(CatalogError) as ctx:
            _zip_to_pack(make_zip({"readme.md": "x"}), origin="x")
        self.assertIn("SKILL.md", str(ctx.exception))

    def test_bad_zip_rejected(self):
        with self.assertRaises(CatalogError) as ctx:
            _zip_to_pack(b"not a zip", origin="x")
        self.assertIn("ZIP", str(ctx.exception))

    def test_detail_parses_frontmatter(self):
        blob = make_zip({"SKILL.md": SKILL_MD})
        detail = self.make_source(blob).detail("owner-repo")
        self.assertEqual(detail.entry.name, "alpha")
        self.assertEqual(detail.entry.description, "测试技能")
        self.assertIn("正文内容", detail.manifest_text)


if __name__ == "__main__":
    unittest.main()
