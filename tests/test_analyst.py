"""Tests for src/agent/analyst.py — select_tools(), no LLM or DB required."""

import pytest

from src.agent.analyst import select_tools, ANALYST_TOOLS

ALL_TOOL_NAMES = {t["function"]["name"] for t in ANALYST_TOOLS}
ALWAYS_NAMES = {"get_user_profile", "get_memory_insights"}


def tool_names(tools: list[dict]) -> set[str]:
    return {t["function"]["name"] for t in tools}


# ---------------------------------------------------------------------------
# Always included
# ---------------------------------------------------------------------------

class TestAlwaysIncluded:
    def test_always_tools_in_specific_query(self):
        names = tool_names(select_tools("show my sleep for the week"))
        assert ALWAYS_NAMES.issubset(names)

    def test_always_tools_in_fallback(self):
        names = tool_names(select_tools("what is happening with my health"))
        assert ALWAYS_NAMES.issubset(names)


# ---------------------------------------------------------------------------
# Specific queries → correct tools
# ---------------------------------------------------------------------------

class TestSpecificRouting:
    def test_sleep_query(self):
        names = tool_names(select_tools("show my sleep for 2 weeks"))
        assert "get_sleep" in names

    def test_sleep_query_russian(self):
        names = tool_names(select_tools("покажи мой сон за 2 недели"))
        assert "get_sleep" in names

    def test_workout_query(self):
        names = tool_names(select_tools("how many workouts in february"))
        assert "get_workouts" in names

    def test_workout_query_russian(self):
        names = tool_names(select_tools("сколько тренировок за февраль"))
        assert "get_workouts" in names

    def test_nutrition_query(self):
        names = tool_names(select_tools("calories for the last week"))
        assert "get_nutrition" in names

    def test_nutrition_query_russian(self):
        names = tool_names(select_tools("калории за последнюю неделю"))
        assert "get_nutrition" in names

    def test_body_query(self):
        names = tool_names(select_tools("weight trend for 6 months"))
        assert "get_body_metrics" in names

    def test_body_query_russian(self):
        names = tool_names(select_tools("динамика веса за полгода"))
        assert "get_body_metrics" in names

    def test_health_query(self):
        names = tool_names(select_tools("show HRV for the month"))
        assert "get_health_metrics" in names

    def test_health_query_russian(self):
        names = tool_names(select_tools("покажи HRV за месяц"))
        assert "get_health_metrics" in names

    def test_lab_query(self):
        names = tool_names(select_tools("hormone trends for the year"))
        assert "get_lab_results" in names

    def test_lab_query_russian(self):
        names = tool_names(select_tools("динамика гормонов за год"))
        assert "get_lab_results" in names

    def test_lab_query_progesterone(self):
        names = tool_names(select_tools("сравни прогестерон за все время"))
        assert "get_lab_results" in names

    def test_lab_query_estradiol(self):
        names = tool_names(select_tools("эстрадиол в 2010 году"))
        assert "get_lab_results" in names

    def test_doctor_report_query(self):
        names = tool_names(select_tools("what did the ultrasound show"))
        assert "get_doctor_reports" in names

    def test_doctor_report_query_russian(self):
        names = tool_names(select_tools("что показало УЗИ"))
        assert "get_doctor_reports" in names

    def test_mind_query(self):
        names = tool_names(select_tools("what did I write about stress"))
        assert "get_mind_entries" in names

    def test_mind_query_russian(self):
        names = tool_names(select_tools("что я писала про стресс"))
        assert "get_mind_entries" in names

    def test_correlation_query(self):
        names = tool_names(select_tools("is there a correlation between steps and HRV"))
        assert "compute_correlation" in names

    def test_correlation_query_russian(self):
        names = tool_names(select_tools("есть ли связь между шагами и HRV"))
        assert "compute_correlation" in names

    def test_trend_query(self):
        names = tool_names(select_tools("monthly trend for the year"))
        assert "get_trend" in names

    def test_trend_query_russian(self):
        names = tool_names(select_tools("тренд по месяцам за год"))
        assert "get_trend" in names

    def test_knowledge_query(self):
        names = tool_names(select_tools("what does the research say about metformin"))
        assert "search_knowledge_base" in names

    def test_knowledge_query_russian(self):
        names = tool_names(select_tools("что говорит исследование про метформин"))
        assert "search_knowledge_base" in names


# ---------------------------------------------------------------------------
# Fallback to all tools
# ---------------------------------------------------------------------------

class TestFallback:
    def test_empty_query_returns_all(self):
        result = select_tools("")
        assert set(tool_names(result)) == ALL_TOOL_NAMES

    def test_vague_query_returns_all(self):
        """Vague query with no keywords → fallback."""
        result = select_tools("tell me something interesting")
        assert set(tool_names(result)) == ALL_TOOL_NAMES

    def test_vague_query_russian_returns_all(self):
        """Vague Russian query without keywords → fallback."""
        result = select_tools("расскажи что-нибудь интересное")
        assert set(tool_names(result)) == ALL_TOOL_NAMES

    def test_too_many_categories_returns_all(self):
        """4+ categories → fallback to all tools."""
        query = "sleep workouts calories weight hormones"  # 5 categories
        result = select_tools(query)
        assert set(tool_names(result)) == ALL_TOOL_NAMES

    def test_too_many_categories_russian_returns_all(self):
        """4+ categories (Russian) → fallback to all tools."""
        query = "сон тренировки калории вес гормоны"  # 5 categories
        result = select_tools(query)
        assert set(tool_names(result)) == ALL_TOOL_NAMES


# ---------------------------------------------------------------------------
# Specific → no extra tools
# ---------------------------------------------------------------------------

class TestSelectiveRouting:
    def test_sleep_query_no_lab(self):
        names = tool_names(select_tools("show deep sleep for the week"))
        assert "get_lab_results" not in names
        assert "get_nutrition" not in names

    def test_sleep_query_russian_no_lab(self):
        names = tool_names(select_tools("покажи глубокий сон за неделю"))
        assert "get_lab_results" not in names
        assert "get_nutrition" not in names

    def test_lab_query_no_workout(self):
        # "hormones" → lab, should not include workouts
        names = tool_names(select_tools("hormone trends for the year"))
        assert "get_workouts" not in names
        assert "get_nutrition" not in names

    def test_lab_query_russian_no_workout(self):
        # "гормоны" → lab, should not include workouts
        names = tool_names(select_tools("динамика гормонов за год"))
        assert "get_workouts" not in names
        assert "get_nutrition" not in names

    def test_tsh_keyword_routes_to_lab(self):
        # "ттг" should route to lab
        names = tool_names(select_tools("динамика ТТГ за год"))
        assert "get_lab_results" in names

    def test_result_is_subset_of_all_tools(self):
        names = tool_names(select_tools("show weight for 6 months"))
        assert names.issubset(ALL_TOOL_NAMES)
