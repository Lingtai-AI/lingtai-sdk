"""Tests for lingtai_kernel.i18n."""
from lingtai_kernel.i18n import t


class TestT:

    def test_simple_key(self):
        result = t("en", "system.current_time", time="2026-03-19T00:00:00Z", ctx="CTX")
        assert "[Current time: 2026-03-19T00:00:00Z | context: CTX]" in result

    def test_chinese_key(self):
        result = t("zh", "system.current_time", time="2026-03-19T00:00:00Z", ctx="CTX")
        assert "2026-03-19T00:00:00Z" in result

    def test_template_substitution(self):
        result = t("en", "system.current_time", time="2026-03-19T00:00:00Z", ctx="CTX")
        assert "[Current time: 2026-03-19T00:00:00Z | context: CTX]" in result

    def test_chinese_template(self):
        result = t("zh", "system.current_time", time="2026-03-19T00:00:00Z", ctx="CTX")
        assert "2026-03-19T00:00:00Z" in result

    def test_wen_key(self):
        result = t("wen", "system.current_time", time="2026-03-19T00:00:00Z", ctx="CTX")
        assert "2026-03-19T00:00:00Z" in result

    def test_unknown_lang_falls_back_to_en(self):
        result = t("xx", "system.current_time", time="now", ctx="CTX")
        assert "now" in result

    def test_unknown_key_returns_key(self):
        result = t("en", "nonexistent.key")
        assert result == "nonexistent.key"


class TestContextBreakdownKeys:
    def test_context_breakdown_en(self):
        result = t("en", "system.context_breakdown", pct="7.1%", sys=4720, ctx=9450)
        assert result == "7.1% (sys 4720 + ctx 9450)"

    def test_context_unknown_en(self):
        assert t("en", "system.context_unknown") == "unavailable"

    def test_current_time_en_extended(self):
        result = t("en", "system.current_time", time="T", ctx="CTX")
        assert result == "[Current time: T | context: CTX]"


class TestFallbackToEnglish:
    """Tool-schema / operating-instruction keys fall back to English."""

    def test_zh_falls_back_for_notification_tool(self):
        result = t("zh", "notification_tool.action_description")
        assert result == t("en", "notification_tool.action_description")

    def test_wen_falls_back_for_notification_tool(self):
        result = t("wen", "notification_tool.action_description")
        assert result == t("en", "notification_tool.action_description")

    def test_zh_falls_back_for_system_tool(self):
        result = t("zh", "system_tool.action_description")
        assert result == t("en", "system_tool.action_description")

    def test_wen_falls_back_for_system_tool(self):
        result = t("wen", "system_tool.action_description")
        assert result == t("en", "system_tool.action_description")

    def test_zh_falls_back_for_email_schema(self):
        result = t("zh", "email.description")
        assert result == t("en", "email.description")

    def test_wen_falls_back_for_email_schema(self):
        result = t("wen", "email.description")
        assert result == t("en", "email.description")

    def test_zh_falls_back_for_psyche_schema(self):
        result = t("zh", "psyche.object_description")
        assert result == t("en", "psyche.object_description")

    def test_wen_falls_back_for_psyche_schema(self):
        result = t("wen", "psyche.object_description")
        assert result == t("en", "psyche.object_description")

    def test_zh_falls_back_for_soul_schema(self):
        result = t("zh", "soul.action_description")
        assert result == t("en", "soul.action_description")

    def test_wen_falls_back_for_soul_schema(self):
        result = t("wen", "soul.action_description")
        assert result == t("en", "soul.action_description")

    def test_zh_falls_back_for_tool_reasoning_schema(self):
        result = t("zh", "tool.reasoning_description")
        assert result == t("en", "tool.reasoning_description")

    def test_wen_falls_back_for_tool_reasoning_schema(self):
        result = t("wen", "tool.reasoning_description")
        assert result == t("en", "tool.reasoning_description")
