"""单元测试:工具注册表与占位工具(纯函数,不依赖网络)。"""

import unittest

from app.tools import Tool, ToolRegistry, calculator, default_registry


class CalculatorTest(unittest.TestCase):
    def test_evaluates_four_arithmetic(self):
        self.assertEqual(calculator("(3+7)*2"), "20")
        self.assertEqual(calculator("10/4"), "2.5")

    def test_rejects_non_arithmetic_characters(self):
        with self.assertRaises(ValueError):
            calculator("__import__('os').system('dir')")
        with self.assertRaises(ValueError):
            calculator("1 + a")

    def test_rejects_empty_expression(self):
        with self.assertRaises(ValueError):
            calculator("   ")

    def test_division_by_zero_is_value_error(self):
        with self.assertRaises(ValueError):
            calculator("1/0")


class ToolRegistryTest(unittest.TestCase):
    def make_tool(self, name="echo", result="ok"):
        return Tool(
            name=name,
            description="测试工具",
            parameters={"type": "object", "properties": {}, "required": []},
            func=lambda: result,
        )

    def test_register_get_run(self):
        registry = ToolRegistry()
        registry.register(self.make_tool(result="ok"))
        tool = registry.get("echo")
        self.assertEqual(tool.run({}), "ok")

    def test_missing_tool_raises_key_error(self):
        registry = ToolRegistry()
        with self.assertRaises(KeyError):
            registry.get("nope")

    def test_duplicate_registration_rejected(self):
        registry = ToolRegistry()
        registry.register(self.make_tool())
        with self.assertRaises(ValueError):
            registry.register(self.make_tool())

    def test_openai_schemas_shape(self):
        registry = ToolRegistry()
        registry.register(self.make_tool(name="t1"))
        schemas = registry.openai_schemas()
        self.assertEqual(len(schemas), 1)
        fn = schemas[0]["function"]
        self.assertEqual(schemas[0]["type"], "function")
        self.assertEqual(fn["name"], "t1")
        self.assertIn("parameters", fn)


class DefaultRegistryTest(unittest.TestCase):
    def test_contains_placeholder_tools_only(self):
        registry = default_registry()
        self.assertEqual(registry.names(), ["calculator", "get_current_time"])

    def test_get_current_time_returns_parseable_text(self):
        text = default_registry().get("get_current_time").run({})
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2}")


if __name__ == "__main__":
    unittest.main()
