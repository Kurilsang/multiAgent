"""单元测试：目录 schema 与不可信输入清洗。"""

import unittest

from services.catalog.schema import (
    AuditBadge,
    CatalogEntry,
    CatalogError,
    SkillPack,
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


class AuditBadgeTest(unittest.TestCase):
    def test_status_normalized_to_known_set(self):
        self.assertEqual(AuditBadge.from_raw({"status": "FAIL"}).status, "fail")
        self.assertEqual(AuditBadge.from_raw({"status": "weird"}).status, "unknown")


class SkillPackTest(unittest.TestCase):
    def test_to_dict_shape(self):
        pack = SkillPack(
            files=(("SKILL.md", "# 正文"),),
            extra_files=("scripts/run.js",),
            origin="owner/repo",
            source="fake",
        )
        data = pack.to_dict()
        self.assertEqual(data["files"][0]["path"], "SKILL.md")
        self.assertEqual(data["extra_files"], ["scripts/run.js"])


if __name__ == "__main__":
    unittest.main()
