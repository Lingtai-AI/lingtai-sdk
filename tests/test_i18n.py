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

    def test_wen_template(self):
        result = t("wen", "system.current_time", time="2026-03-19T00:00:00Z", ctx="CTX")
        assert "此时" in result

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

    def test_context_breakdown_zh(self):
        result = t("zh", "system.context_breakdown", pct="7.1%", sys=4720, ctx=9450)
        assert result == "7.1% (系统 4720 + 对话 9450)"

    def test_context_breakdown_wen(self):
        result = t("wen", "system.context_breakdown", pct="7.1%", sys=4720, ctx=9450)
        assert result == "7.1% (系统 4720 + 对话 9450)"

    def test_context_unknown_en(self):
        assert t("en", "system.context_unknown") == "unavailable"

    def test_context_unknown_zh(self):
        assert t("zh", "system.context_unknown") == "未知"

    def test_context_unknown_wen(self):
        assert t("wen", "system.context_unknown") == "未知"

    def test_current_time_en_extended(self):
        result = t("en", "system.current_time", time="T", ctx="CTX")
        assert result == "[Current time: T | context: CTX]"

    def test_current_time_zh_extended(self):
        result = t("zh", "system.current_time", time="T", ctx="CTX")
        assert result == "[此时：T | 上下文：CTX]"

    def test_current_time_wen_extended(self):
        result = t("wen", "system.current_time", time="T", ctx="CTX")
        assert result == "[此时：T | 上下文：CTX]"
