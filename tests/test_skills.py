"""单元测试：技能注册表（SKILL.md 解析/校验/自产）与安装器。

缝隙 = 工具注册表（假工具）与临时技能目录，不发真实网络请求；
git 适配器用本地仓库（离线克隆），仅在装有 git 时执行。
"""

import tempfile
import unittest
from pathlib import Path

from app.skills import (
    MAX_GUIDE_CHARS,
    Skill,
    SkillError,
    SkillRegistry,
    catalog_section,
    parse_skill_md,
    render_skill_md,
)
from app.tools import default_registry, skill_tools

VALID_PACK = """---
name: 时间报告
description: 日期推算
tools:
  - get_current_time
  - calculator
---

先取时间，再换算日期。
"""


def make_registry(root: Path) -> SkillRegistry:
    tools = default_registry()
    registry = SkillRegistry(root / "skills", tools=tools)
    for tool in skill_tools(registry):
        tools.register(tool)
    return registry


def write_pack(base: Path, dirname: str, text: str) -> None:
    pack_dir = base / dirname
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "SKILL.md").write_text(text, encoding="utf-8")


class ParseTest(unittest.TestCase):
    def test_parses_fields_and_body(self):
        fields, body = parse_skill_md(VALID_PACK)
        self.assertEqual(fields["name"], "时间报告")
        self.assertEqual(fields["description"], "日期推算")
        self.assertEqual(fields["tools"], ["get_current_time", "calculator"])
        self.assertEqual(body, "先取时间，再换算日期。")

    def test_render_roundtrip(self):
        skill = Skill(name="a-b", description="d", guide="正文", tools=("calculator",))
        fields, body = parse_skill_md(render_skill_md(skill))
        self.assertEqual(fields["name"], "a-b")
        self.assertEqual(fields["tools"], ["calculator"])
        self.assertEqual(body, "正文")

    def test_rejects_unknown_field(self):
        text = VALID_PACK.replace("description:", "evil: 1\ndescription:")
        with self.assertRaises(SkillError) as ctx:
            parse_skill_md(text)
        self.assertIn("未知字段", str(ctx.exception))

    def test_rejects_missing_frontmatter(self):
        with self.assertRaises(SkillError):
            parse_skill_md("正文没有 frontmatter")


class LoadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = make_registry(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_pack_loaded(self):
        write_pack(self.root / "skills", "时间报告", VALID_PACK)
        errors = self.registry.reload()
        self.assertEqual(errors, [])
        entry = self.registry.get("时间报告")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.skill.guide, "先取时间，再换算日期。")
        self.assertEqual(entry.skill.tools, ("get_current_time", "calculator"))
        self.assertTrue(entry.enabled)
        self.assertEqual(entry.source, "local")

    def test_enabled_state_persisted(self):
        write_pack(self.root / "skills", "时间报告", VALID_PACK)
        self.registry.reload()
        self.registry.disable("时间报告")
        fresh = make_registry(self.root)
        fresh.reload()
        self.assertFalse(fresh.get("时间报告").enabled)
        self.assertEqual(fresh.enabled_skills(), ())

    def test_dir_name_mismatch_rejected(self):
        write_pack(self.root / "skills", "别的目录", VALID_PACK)
        errors = self.registry.reload()
        self.assertEqual(len(errors), 1)
        self.assertIn("不一致", errors[0])
        self.assertIsNone(self.registry.get("时间报告"))

    def test_unregistered_tool_rejected(self):
        text = VALID_PACK.replace("calculator", "missing_tool")
        write_pack(self.root / "skills", "时间报告", text)
        errors = self.registry.reload()
        self.assertEqual(len(errors), 1)
        self.assertIn("未注册的工具", errors[0])

    def test_hot_reload_picks_up_new_pack(self):
        self.registry.reload()
        self.assertEqual(self.registry.entries(), ())
        write_pack(self.root / "skills", "时间报告", VALID_PACK)
        self.registry.reload()
        self.assertIsNotNone(self.registry.get("时间报告"))


class CreateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = make_registry(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_persists_pack_immediately(self):
        skill = self.registry.create("新技能", "描述", "指引正文", ("calculator",), source="agent")
        self.assertEqual(skill.name, "新技能")
        content = self.registry.pack_file("新技能").read_text(encoding="utf-8")
        self.assertIn("name: 新技能", content)
        self.assertIn("指引正文", content)
        entry = self.registry.get("新技能")
        self.assertEqual(entry.source, "agent")
        self.assertTrue(entry.enabled)

    def test_duplicate_rejected(self):
        self.registry.create("新技能", "描述", "指引")
        with self.assertRaises(SkillError) as ctx:
            self.registry.create("新技能", "描述2", "指引2")
        self.assertIn("已存在", str(ctx.exception))

    def test_unregistered_tool_rejected(self):
        with self.assertRaises(SkillError) as ctx:
            self.registry.create("新技能", "描述", "指引", ("missing",))
        self.assertIn("未注册的工具", str(ctx.exception))

    def test_bad_name_rejected(self):
        with self.assertRaises(SkillError):
            self.registry.create("bad name", "描述", "指引")
        with self.assertRaises(SkillError):
            self.registry.create("", "描述", "指引")

    def test_oversize_rejected(self):
        with self.assertRaises(SkillError) as ctx:
            self.registry.create("新技能", "x" * 101, "指引")
        self.assertIn("description 超长", str(ctx.exception))
        with self.assertRaises(SkillError) as ctx:
            self.registry.create("新技能", "描述", "x" * (MAX_GUIDE_CHARS + 1))
        self.assertIn("正文超长", str(ctx.exception))

    def test_remove_deletes_pack(self):
        self.registry.create("新技能", "描述", "指引")
        self.registry.remove("新技能")
        self.assertIsNone(self.registry.get("新技能"))
        self.assertFalse((self.root / "skills" / "新技能").exists())


class MetaToolTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = make_registry(self.root)
        self.tools = self.registry._tools  # 与注册表同源的工具注册表

    def tearDown(self):
        self._tmp.cleanup()

    def test_use_skill_returns_guide_with_hints(self):
        self.registry.create("时间报告", "日期", "先取时间", ("calculator",))
        result = self.tools.get("use_skill").run({"name": "时间报告"})
        self.assertIn("先取时间", result)
        self.assertIn("calculator", result)

    def test_use_skill_unknown_and_disabled(self):
        with self.assertRaises(ValueError) as ctx:
            self.tools.get("use_skill").run({"name": "nope"})
        self.assertIn("search_skills", str(ctx.exception))
        self.registry.create("时间报告", "日期", "先取时间")
        self.registry.disable("时间报告")
        with self.assertRaises(ValueError) as ctx:
            self.tools.get("use_skill").run({"name": "时间报告"})
        self.assertIn("禁用", str(ctx.exception))

    def test_search_skills_hit_and_miss(self):
        self.registry.create("时间报告", "日期推算", "指引")
        self.assertIn("时间报告", self.tools.get("search_skills").run({"query": "日期"}))
        self.assertIn("没有匹配", self.tools.get("search_skills").run({"query": "zzz"}))

    def test_create_skill_end_to_end(self):
        result = self.tools.get("create_skill").run(
            {"name": "新技能", "description": "d", "guide": "g", "tools": ["calculator"]}
        )
        self.assertIn("已创建并生效", result)
        self.assertIsNotNone(self.registry.get("新技能"))


class CatalogSectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = make_registry(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_registry_has_no_section(self):
        self.registry.reload()
        self.assertEqual(catalog_section(self.registry, 30), "")

    def test_catalog_lists_name_and_description_only(self):
        self.registry.create("时间报告", "日期推算", "很长的正文不应出现在清单里")
        section = catalog_section(self.registry, 30)
        self.assertIn("时间报告", section)
        self.assertIn("日期推算", section)
        self.assertNotIn("很长的正文", section)

    def test_over_limit_truncated_with_search_hint(self):
        # 名称用 ASCII：排序确定（中文按码点排序不直观）
        self.registry.create("skill-a", "d1", "g1")
        self.registry.create("skill-b", "d2", "g2")
        section = catalog_section(self.registry, 1)
        self.assertIn("skill-a", section)
        self.assertNotIn("skill-b", section)
        self.assertIn("search_skills", section)


if __name__ == "__main__":
    unittest.main()
