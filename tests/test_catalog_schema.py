"""单元测试：目录 schema 与不可信输入清洗。"""

import unittest

from services.catalog.schema import (
    AuditBadge,
    CatalogDetail,
    CatalogEntry,
    CatalogError,
    CatalogPack,
    clean_text,
)


class CleanTextTest(unittest.TestCase):
    def test_strips_control_chars_and_collapses_whitespace(self):
        self.assertEqual(clean_text("a\x00b\n c\t\td", 100), "a b c d")

    def test_truncates_to_limit(self):
        self.assertEqual(clean_text("x" * 50, 10), "x" * 10)

    def test_none_and_non_string_become_empty(self):
        self.assertEqual(clean_text(None, 10), "")
        self.assertEqual(clean_text(123, 10), "123")


class EntryFromRawTest(unittest.TestCase):
    def test_missing_id_or_name_rejected(self):
        with self.assertRaises(CatalogError):
            CatalogEntry.from_raw({"name": "x"}, source="fake")
        with self.assertRaises(CatalogError):
            CatalogEntry.from_raw({"id": "x"}, source="fake")

    def test_cleans_and_caps_fields(self):
        entry = CatalogEntry.from_raw(
            {
                "id": " fake/a ",
                "name": "n\x01ame",
                "description": "d" * 500,
                "origin": "owner/repo",
                "installs": "42",
                "stars": -3,
                "tags": ["t1", "x" * 40, "t3"],
                "detail_url": "https://example.invalid",
            },
            source="fake",
        )
        self.assertEqual(entry.id, "fake/a")
        self.assertEqual(entry.name, "n ame")
        self.assertEqual(len(entry.description), 400)
        self.assertEqual(entry.installs, 42)
        self.assertEqual(entry.stars, 0)
        self.assertEqual(len(entry.tags), 3)
        self.assertEqual(len(entry.tags[1]), 32)

    def test_to_dict_shape(self):
        entry = CatalogEntry.from_raw(
            {"id": "a/b", "name": "n", "audits": [{"provider": "Snyk", "status": "PASS"}]},
            source="fake",
        )
        data = entry.to_dict()
        self.assertEqual(data["id"], "a/b")
        self.assertEqual(data["audits"][0]["status"], "pass")
        self.assertFalse(data["validated"])


class EntryKindTest(unittest.TestCase):
    """资产类型 kind：技能 / MCP 条目在统一条目里的区分字段。"""

    def test_defaults_to_skill(self):
        entry = CatalogEntry.from_raw({"id": "a/b", "name": "n"}, source="fake")
        self.assertEqual(entry.kind, "skill")
        self.assertEqual(entry.to_dict()["kind"], "skill")

    def test_kind_passes_through_from_raw(self):
        entry = CatalogEntry.from_raw(
            {"id": "io.x/y", "name": "n", "kind": " mcp "}, source="fake"
        )
        self.assertEqual(entry.kind, "mcp")
        self.assertEqual(entry.to_dict()["kind"], "mcp")


class AuditBadgeTest(unittest.TestCase):
    def test_status_normalized_to_known_set(self):
        self.assertEqual(AuditBadge.from_raw({"status": "FAIL"}).status, "fail")
        self.assertEqual(AuditBadge.from_raw({"status": "weird"}).status, "unknown")


class CatalogPackTest(unittest.TestCase):
    def test_to_dict_shape(self):
        pack = CatalogPack(
            files=(("SKILL.md", "# 正文"),),
            extra_files=("scripts/run.js",),
            origin="owner/repo",
            source="fake",
            manifest_path="SKILL.md",
        )
        data = pack.to_dict()
        self.assertEqual(data["files"][0]["path"], "SKILL.md")
        self.assertEqual(data["extra_files"], ["scripts/run.js"])
        self.assertEqual(data["manifest_path"], "SKILL.md")
        self.assertEqual(data["kind"], "skill")


class CatalogDetailTest(unittest.TestCase):
    """确认卡预览载荷：条目 + manifest 全文（通用字段，取代技能专用 skill_md）。"""

    def test_to_dict_exposes_manifest_fields(self):
        entry = CatalogEntry.from_raw({"id": "a/b", "name": "n"}, source="fake")
        detail = CatalogDetail(
            entry=entry, manifest_path="SKILL.md", manifest_text="# 正文"
        )
        data = detail.to_dict()
        self.assertEqual(data["manifest_path"], "SKILL.md")
        self.assertEqual(data["manifest_text"], "# 正文")
        self.assertNotIn("skill_md", data)


if __name__ == "__main__":
    unittest.main()
