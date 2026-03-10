"""Analyst agent: tool-calling agent for /scope and /report."""

import json
import logging
from datetime import date, timedelta
from typing import Optional

import asyncpg

from src.config import settings
from src.llm.client import ask_model_with_tools

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intent prompts
# ---------------------------------------------------------------------------

SCOPE_INTENT = (
    "Analyze recent journal entries and body data (health, activity, sleep, weight). "
    "Load data for the last 7-14 days. Suggest a focus vector — what matters now, "
    "what requires action, what can be let go. Be specific."
)

REPORT_INTENT = (
    "Generate a full analytical report across all data: "
    "journal entries for the last 3 weeks, health metrics for 14 days, "
    "sleep for 14 days, nutrition for 7 days, body metrics for 30 days, "
    "profile and memory insights. "
    "If a weight plateau is detected (3+ measurements over 7+ days, range ≤ 1 kg) — "
    "explain the physiology, check for recomposition via body measurements. "
    "Structure: body & weight → sleep → nutrition → mental state → one clear conclusion. "
    "No recommendation lists, no generic advice."
)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

ANALYST_SYSTEM = (
    f"You are {settings.app_name}, a personal AI analyst.\n\n"
    "ROLE: analyst of health, body, nutrition and mental state data. "
    "Not a coach, not a motivator, not a doctor. "
    "User's personal context is in the profile — load via get_user_profile() when needed.\n\n"
    "RESPONSE STRUCTURE: data → mechanism → conclusion. "
    "Do not reassure without factual basis. Name uncomfortable things directly. "
    "Separate proven from hypothesis: 'data says X' vs 'possible explanation Y'.\n\n"
    "FORMAT (Telegram): never use markdown tables — they don't render. "
    "Instead of tables: bold heading + list of lines with dashes or numbers. "
    "Never end with a question, 'let me know', or an invitation to continue.\n\n"
    "PSYCHOLOGICAL CONTEXT: when analyzing thoughts, decisions, patterns apply:\n"
    "— CBT: notice cognitive distortions, automatic thoughts and beliefs.\n"
    "— Schema approach: recognize stable response patterns and modes.\n"
    "— Mentalization: explore what is behind a thought — needs, states, intentions.\n"
    "Do not diagnose — help to become aware.\n\n"
    "USER PROFILE: the profile may contain medical context. "
    "Use it only for data interpretation and calculations — "
    "NEVER mention medical facts explicitly in conversation: diagnoses, medications, "
    "hormonal status, age-related changes and similar. "
    "This is background knowledge, not arguments in dialogue. "
    "Exception — only if the user asked directly.\n\n"
    "TONE: warm and direct without condescension. Tell the truth, do not lecture.\n\n"
    "Use tools to load the needed data from the DB, then provide analysis.\n\n"
    "DATE PARAMETERS: all tools support days (last N days) "
    "or from_date + to_date (format YYYY-MM-DD)."
)

ASK_SYSTEM = (
    f"You are {settings.app_name}, a personal analyst.\n\n"
    "First load the needed data via tools, then answer.\n\n"
    "TONE: facts + one conclusion, brief and direct. "
    "Never end with a question or 'let me know'. "
    "No praise, exclamations, or bureaucratic language. "
    "Round numbers, add context when it helps.\n\n"
    "FORMAT (Telegram): never use markdown tables — they don't render. "
    "Instead of tables: bold heading + list of lines with dashes. "
    "Do not list raw data day by day — compute yourself: average, total, trend. "
    "Show the final number + brief explanation. "
    "If the question is about a single number — 1-2 sentences. No headings.\n\n"
    "TOOLS — load only what is needed:\n"
    "— HRV/steps/activity/VO2max → get_health_metrics\n"
    "— Weight/body composition → get_body_metrics\n"
    "— Sleep → get_sleep\n"
    "— Lab results → get_lab_results (with parameter_key or category)\n"
    "— Doctor reports → get_doctor_reports\n"
    "— Nutrition → get_nutrition\n"
    "— Workouts → get_workouts (can filter by type: strength/cardio/low_intensity)\n"
    "— Personal context → get_user_profile\n\n"
    "DATES:\n"
    "— Specific year → from_date='YYYY-01-01', to_date='YYYY-12-31'\n"
    "— Relative period → ONLY days (180, 90, 365), do not compute from_date manually\n"
    "— Lab results and doctor reports: always from_date='2010-01-01' unless user specifies a period\n"
    "— Lab results → always specify parameter_key or category, do not load everything at once"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _date_range(
    days: int,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> tuple[date, date]:
    """Returns (date_from, date_to) from tool parameters."""
    if from_date:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date) if to_date else date.today()
    else:
        d_to = date.today()
        d_from = d_to - timedelta(days=days)
    return d_from, d_to


# ---------------------------------------------------------------------------
# METRIC_MAP — mapping of metrics to tables and columns
# ---------------------------------------------------------------------------

# (table, date_column, value_column, parameter_key_filter_or_None)
METRIC_MAP: dict[str, tuple] = {
    # health_metrics
    "hrv_ms":        ("health_metrics",  "recorded_date", "hrv_ms",        None),
    "steps":         ("health_metrics",  "recorded_date", "steps",         None),
    "heart_rate":    ("health_metrics",  "recorded_date", "heart_rate",    None),
    "resting_hr":    ("health_metrics",  "recorded_date", "resting_hr",    None),
    "active_kcal":   ("health_metrics",  "recorded_date", "active_kcal",   None),
    "distance_km":   ("health_metrics",  "recorded_date", "distance_km",   None),
    "vo2max":        ("health_metrics",  "recorded_date", "vo2max",        None),
    # sleep_sessions
    "total_min":       ("sleep_sessions", "sleep_date", "total_min",       None),
    "deep_min":        ("sleep_sessions", "sleep_date", "deep_min",        None),
    "rem_min":         ("sleep_sessions", "sleep_date", "rem_min",         None),
    "efficiency_pct":  ("sleep_sessions", "sleep_date", "efficiency_pct",  None),
    # nutrition_logs
    "calories": ("nutrition_logs", "logged_date", "calories", None),
    "protein":  ("nutrition_logs", "logged_date", "protein",  None),
    "fat":      ("nutrition_logs", "logged_date", "fat",      None),
    "carbs":    ("nutrition_logs", "logged_date", "carbs",    None),
    # body_metrics
    "weight":        ("body_metrics", "recorded_date", "weight",        None),
    "body_fat_pct":  ("body_metrics", "recorded_date", "body_fat_pct",  None),
    "muscle_kg":     ("body_metrics", "recorded_date", "muscle_kg",     None),
    # lab_results (EAV: filter by parameter_key)
    "insulin":        ("lab_results", "test_date", "value_numeric", "insulin"),
    "glucose":        ("lab_results", "test_date", "value_numeric", "glucose"),
    "homa_ir":        ("lab_results", "test_date", "value_numeric", "homa_ir"),
    "hba1c":          ("lab_results", "test_date", "value_numeric", "hba1c"),
    "cholesterol":    ("lab_results", "test_date", "value_numeric", "cholesterol"),
    "ldl":            ("lab_results", "test_date", "value_numeric", "ldl"),
    "hdl":            ("lab_results", "test_date", "value_numeric", "hdl"),
    "triglycerides":  ("lab_results", "test_date", "value_numeric", "triglycerides"),
    "tsh":            ("lab_results", "test_date", "value_numeric", "tsh"),
    "vitamin_d":      ("lab_results", "test_date", "value_numeric", "vitamin_d"),
    "ferritin":       ("lab_results", "test_date", "value_numeric", "ferritin"),
    "cortisol":       ("lab_results", "test_date", "value_numeric", "cortisol"),
    "testosterone":   ("lab_results", "test_date", "value_numeric", "testosterone"),
    "estradiol":      ("lab_results", "test_date", "value_numeric", "estradiol"),
    "prolactin":      ("lab_results", "test_date", "value_numeric", "prolactin"),
    "hemoglobin":     ("lab_results", "test_date", "value_numeric", "hemoglobin"),
}

_METRIC_NAMES = ", ".join(sorted(METRIC_MAP))


async def _fetch_metric_series(
    conn, user_id: str, metric: str, d_from: date, d_to: date
) -> list[tuple[date, float]]:
    """Returns [(date, value), ...] for a metric. Skips NULLs."""
    if metric not in METRIC_MAP:
        raise ValueError(
            f"Unknown metric: {metric!r}. Available: {_METRIC_NAMES}"
        )
    table, date_col, val_col, param_key = METRIC_MAP[metric]

    if table == "lab_results":
        rows = await conn.fetch(
            f"SELECT {date_col}, {val_col} FROM {table} "
            f"WHERE user_id=$1 AND parameter_key=$2 "
            f"AND {date_col} BETWEEN $3 AND $4 AND {val_col} IS NOT NULL "
            f"ORDER BY {date_col}",
            user_id, param_key, d_from, d_to,
        )
    else:
        rows = await conn.fetch(
            f"SELECT {date_col}, {val_col} FROM {table} "
            f"WHERE user_id=$1 AND {date_col} BETWEEN $2 AND $3 "
            f"AND {val_col} IS NOT NULL ORDER BY {date_col}",
            user_id, d_from, d_to,
        )
    return [(r[date_col], float(r[val_col])) for r in rows]

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

# Common date parameter descriptions (used in every tool)
_DATE_PARAMS = {
    "days": {
        "type": "integer",
        "description": "Number of days back from today. Used if from_date is not set.",
    },
    "from_date": {
        "type": "string",
        "description": "Start of period in YYYY-MM-DD format. If set — days is ignored.",
    },
    "to_date": {
        "type": "string",
        "description": "End of period in YYYY-MM-DD format. Default — today.",
    },
}

ANALYST_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_health_metrics",
            "description": (
                "Loads Apple Health metrics (HRV, steps, HR, calories, VO2max, etc.). "
                "Supports arbitrary period via from_date/to_date or last N days."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_DATE_PARAMS,
                    "days": {**_DATE_PARAMS["days"], "default": 14},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sleep",
            "description": (
                "Loads sleep data: phases (deep, REM, core), total duration, efficiency. "
                "Supports arbitrary period via from_date/to_date or last N days."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_DATE_PARAMS,
                    "days": {**_DATE_PARAMS["days"], "default": 14},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_mind_entries",
            "description": (
                "Loads journal entries ([MIND]) and decisions ([DECISION]) from message history. "
                "Supports arbitrary period via from_date/to_date or last N days."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_DATE_PARAMS,
                    "days": {**_DATE_PARAMS["days"], "default": 21},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_nutrition",
            "description": (
                "Loads nutrition data (macros per day). "
                "Supports arbitrary period via from_date/to_date or last N days."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_DATE_PARAMS,
                    "days": {**_DATE_PARAMS["days"], "default": 7},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_body_metrics",
            "description": (
                "Loads body metrics (weight, body composition, measurements in cm). "
                "Supports arbitrary period via from_date/to_date or last N days."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_DATE_PARAMS,
                    "days": {**_DATE_PARAMS["days"], "default": 30},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_profile",
            "description": "Loads the user profile (physiological parameters, goals, context).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_memory_insights",
            "description": "Loads confirmed patterns and insights from long-term memory.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_personal_data",
            "description": "Semantic search over the user's message history via pgvector. Returns the most relevant entries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results. Default 5.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lab_results",
            "description": (
                "Loads lab results from the DB. "
                "Can be filtered by specific parameter (parameter_key) or category. "
                "Use for trends in insulin, hormones, cholesterol, and any other markers. "
                "Example parameter_key values: glucose, insulin, homa_ir, alt, ast, cholesterol, hdl, ldl, "
                "fsh, lh, estradiol, progesterone, testosterone, shbg, free_testosterone_index, prolactin, "
                "dhea_s, cortisol, 17oh_progesterone, tsh, vitamin_d, ferritin, iron, "
                "calcium, magnesium, ca125, he4, hba1c, hemoglobin, wbc, esr. "
                "Categories: biochemistry, hormones, cbc, lipids, thyroid, vitamins, tumor_markers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_DATE_PARAMS,
                    "days": {**_DATE_PARAMS["days"], "default": 5000},
                    "parameter_key": {
                        "type": "string",
                        "description": "Standard parameter key. If not set — returns all markers for the period.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Parameter category. Applied if parameter_key is not set.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_doctor_reports",
            "description": (
                "Loads doctor reports: ultrasound, MRI, X-ray, mammography, cytology. "
                "Use when the user asks about ultrasound, MRI, or other diagnostic results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_DATE_PARAMS,
                    "days": {**_DATE_PARAMS["days"], "default": 3650},
                    "study_type": {
                        "type": "string",
                        "description": "uzi, mrt, rentgen, mammografia, cytology, other. If not set — all reports.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "Search the knowledge base: uploaded articles, studies, medical research on conditions, medications, nutrition, training. Use when medical context or reference information is needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results. Default 5.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_correlation",
            "description": (
                "Computes the Spearman correlation coefficient between two metrics. "
                "Returns r (from -1 to 1), p-value, and n matching data points. "
                "Use to answer questions like 'is there a relationship between X and Y'. "
                "Supports lag_days — time shift (e.g., does today's nutrition affect weight 3 days later). "
                "Available metrics: hrv_ms, steps, heart_rate, resting_hr, active_kcal, distance_km, vo2max, "
                "total_min, deep_min, rem_min, efficiency_pct, calories, protein, fat, carbs, "
                "weight, body_fat_pct, muscle_kg, "
                "insulin, glucose, homa_ir, hba1c, cholesterol, ldl, hdl, triglycerides, "
                "tsh, vitamin_d, ferritin, cortisol, testosterone, estradiol, prolactin, hemoglobin."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric_a": {"type": "string", "description": "First metric."},
                    "metric_b": {"type": "string", "description": "Second metric."},
                    **_DATE_PARAMS,
                    "days": {**_DATE_PARAMS["days"], "default": 365},
                    "lag_days": {
                        "type": "integer",
                        "description": "Shift metric_b forward by N days. Default 0 (same day).",
                        "default": 0,
                    },
                },
                "required": ["metric_a", "metric_b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workouts",
            "description": (
                "Loads workouts from workout_sessions. "
                "Types: strength, cardio, low_intensity (yoga, walking), other. "
                "Returns date, type, duration, kcal, HR. "
                "For periods >90 days aggregates by month."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_DATE_PARAMS,
                    "days": {**_DATE_PARAMS["days"], "default": 30},
                    "workout_type": {
                        "type": "string",
                        "description": "Filter by type: strength, cardio, low_intensity, other. If not set — all types.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trend",
            "description": (
                "Returns monthly averages for one metric over an arbitrary period. "
                "Use for questions like 'how did X change month by month / year by year'. "
                "Much more compact than raw data: one year → 12 rows instead of 365. "
                "Available metrics: same as in compute_correlation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "Metric name."},
                    **_DATE_PARAMS,
                    "days": {**_DATE_PARAMS["days"], "default": 365},
                },
                "required": ["metric"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Dynamic tool selection
# ---------------------------------------------------------------------------

_ALWAYS_TOOL_NAMES = {"get_user_profile", "get_memory_insights"}

_TOOL_CATEGORIES: dict[str, dict] = {
    "workout": {
        "keywords": ["workout", "strength", "yoga", "cardio", "exercise", "sport",
                     "тренировк", "силов", "йог", "кардио", "нагрузк", "упражнен", "спорт"],
        "tools":    ["get_workouts", "get_health_metrics"],
    },
    "sleep": {
        "keywords": ["sleep", "rem", "deep", "wake", "bedtime", "insomnia",
                     "сон", "сплю", "просыпа", "ночь", "засып", "фаз сна", "будил"],
        "tools":    ["get_sleep", "get_health_metrics"],
    },
    "nutrition": {
        "keywords": ["food", "nutrition", "calories", "protein", "fat", "carbs", "diet", "kcal",
                     "еда", "питани", "калори", "белок", "жир", "углевод", "рацион", "ккал"],
        "tools":    ["get_nutrition"],
    },
    "body": {
        "keywords": ["weight", "body", "composition", "fat", "muscle", "waist", "hips", "bmi",
                     "вес", "тело", "состав", "жировая", "мышц", "замер", "объём", "талия", "бёдр", "похуд"],
        "tools":    ["get_body_metrics"],
    },
    "health": {
        "keywords": ["hrv", "steps", "heart rate", "vo2", "activity", "distance",
                     "шаги", "пульс", "сердц", "активност", "дистанц"],
        "tools":    ["get_health_metrics"],
    },
    "lab": {
        "keywords": ["lab", "blood", "hormone", "insulin", "testosterone", "vitamin", "ultrasound",
                     "mri", "cholesterol", "glucose", "cortisol", "ferritin", "hemoglobin",
                     "tsh", "t3", "t4", "b12", "dhea", "triglycerid",
                     "анализ", "гормон", "кровь", "инсулин", "тестостерон", "витамин", "лаборатор",
                     "узи", "мрт", "заключени", "прогестерон", "эстрадиол", "эстроген", "лг", "фсг",
                     "пролактин", "кортизол", "ферритин", "глюкоз", "холестерин", "гемоглобин",
                     "ттг", "витамин д", "онкомаркер", "биохими", "липид", "триглицерид"],
        "tools":    ["get_lab_results", "get_doctor_reports"],
    },
    "mind": {
        "keywords": ["thought", "mood", "stress", "emotion", "decision", "journal", "wellbeing",
                     "мысл", "настроени", "стресс", "эмоц", "реши", "дневник", "самочувстви"],
        "tools":    ["get_mind_entries", "search_personal_data"],
    },
    "knowledge": {
        "keywords": ["supplement", "research", "study", "paper", "medication",
                     "исследовани", "статья", "научн", "препарат", "лекарств"],
        "tools":    ["search_knowledge_base"],
    },
    "stats": {
        "keywords": ["correlation", "trend", "pattern", "over years", "over months",
                     "корреляц", "связь между", "влияет", "зависит", "тренд", "по годам", "по месяц"],
        "tools":    ["compute_correlation", "get_trend"],
    },
}

_FALLBACK_THRESHOLD = 4  # 4+ categories → query too broad → all tools

_TOOL_BY_NAME = {t["function"]["name"]: t for t in ANALYST_TOOLS}


def select_tools(query: str) -> list[dict]:
    """Returns the minimal set of tools for the given query.

    If the query is vague (0 or 4+ categories matched) — fallback to all tools.
    Always includes get_user_profile and get_memory_insights.
    """
    q = query.lower()
    selected_names = set(_ALWAYS_TOOL_NAMES)
    matched = 0

    for config in _TOOL_CATEGORIES.values():
        if any(kw in q for kw in config["keywords"]):
            selected_names.update(config["tools"])
            matched += 1

    if matched == 0 or matched >= _FALLBACK_THRESHOLD:
        return ANALYST_TOOLS  # fallback: too vague or too broad a query

    return [_TOOL_BY_NAME[name] for name in selected_names if name in _TOOL_BY_NAME]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _tool_get_health_metrics(
    conn: asyncpg.Connection,
    user_id: str,
    days: int = 14,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    d_from, d_to = _date_range(days, from_date, to_date)
    span_days = (d_to - d_from).days
    if span_days > 90:
        rows = await conn.fetch(
            """
            SELECT to_char(recorded_date, 'YYYY-MM') AS month,
                   ROUND(AVG(hrv_ms)::numeric, 1)         AS hrv,
                   ROUND(AVG(heart_rate)::numeric, 1)     AS hr,
                   ROUND(AVG(resting_hr)::numeric, 1)     AS rhr,
                   ROUND(AVG(steps)::numeric, 0)          AS steps,
                   ROUND(AVG(active_kcal)::numeric, 0)    AS kcal,
                   ROUND(AVG(distance_km)::numeric, 1)    AS km,
                   COUNT(*) AS n
            FROM health_metrics
            WHERE user_id = $1 AND recorded_date BETWEEN $2 AND $3
            GROUP BY 1 ORDER BY 1
            """,
            user_id, d_from, d_to,
        )
        if not rows:
            return f"No health data for period {d_from} — {d_to}."
        lines = [f"Health metrics {d_from} — {d_to} by month:"]
        for r in rows:
            lines.append(
                f"  {r['month']}: HRV {r['hrv']}ms, HR {r['hr']}, RestHR {r['rhr']}, "
                f"steps {r['steps']}/d, {r['kcal']}kcal, {r['km']}km ({r['n']}d)"
            )
        return "\n".join(lines)

    rows = await conn.fetch(
        """
        SELECT recorded_date, hrv_ms, heart_rate, resting_hr, steps,
               active_kcal, (active_kcal + resting_kcal) AS total_kcal,
               distance_km, vo2max
        FROM health_metrics
        WHERE user_id = $1 AND recorded_date BETWEEN $2 AND $3
        ORDER BY recorded_date DESC LIMIT 60
        """,
        user_id, d_from, d_to,
    )
    if not rows:
        return f"No health data for period {d_from} — {d_to}."

    # Averages for the period
    hrv_vals  = [r["hrv_ms"] for r in rows if r["hrv_ms"]]
    hr_vals   = [r["heart_rate"] for r in rows if r["heart_rate"]]
    rhr_vals  = [r["resting_hr"] for r in rows if r["resting_hr"]]
    step_vals = [r["steps"] for r in rows if r["steps"]]
    kcal_vals = [r["active_kcal"] for r in rows if r["active_kcal"]]
    km_vals   = [r["distance_km"] for r in rows if r["distance_km"]]

    def avg(lst): return round(sum(lst) / len(lst), 1) if lst else None

    lines = [f"Health metrics {d_from} — {d_to} ({len(rows)} days):"]
    lines.append(
        f"Averages: HRV {avg(hrv_vals)}ms, HR {avg(hr_vals)}, RestHR {avg(rhr_vals)}, "
        f"steps {round(avg(step_vals)) if avg(step_vals) else '—'}/d, "
        f"activity {round(avg(kcal_vals)) if avg(kcal_vals) else '—'}kcal, "
        f"distance {avg(km_vals)}km"
    )
    lines.append("Details:")
    for r in rows:
        parts = []
        if r["hrv_ms"]:    parts.append(f"HRV {r['hrv_ms']}")
        if r["heart_rate"]: parts.append(f"HR {r['heart_rate']}")
        if r["resting_hr"]: parts.append(f"RestHR {r['resting_hr']}")
        if r["steps"]:     parts.append(f"steps {r['steps']}")
        if r["active_kcal"]: parts.append(f"{r['active_kcal']}kcal")
        if r["distance_km"]: parts.append(f"{r['distance_km']}km")
        if r["vo2max"]:    parts.append(f"VO2 {r['vo2max']}")
        lines.append(f"  {r['recorded_date']}: {', '.join(parts)}")
    return "\n".join(lines)


async def _tool_get_sleep(
    conn: asyncpg.Connection,
    user_id: str,
    days: int = 14,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    d_from, d_to = _date_range(days, from_date, to_date)
    span_days = (d_to - d_from).days
    if span_days > 90:
        rows = await conn.fetch(
            """
            SELECT to_char(sleep_date, 'YYYY-MM') AS month,
                   ROUND(AVG(total_min)::numeric, 0)  AS avg_total_min,
                   ROUND(AVG(deep_min)::numeric, 0)   AS avg_deep_min,
                   ROUND(AVG(rem_min)::numeric, 0)    AS avg_rem_min,
                   ROUND(AVG(efficiency_pct)::numeric, 1) AS avg_efficiency_pct,
                   COUNT(*)                            AS nights_count
            FROM sleep_sessions
            WHERE user_id = $1 AND sleep_date BETWEEN $2 AND $3
            GROUP BY 1
            ORDER BY 1
            """,
            user_id, d_from, d_to,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT sleep_date, bedtime_start, bedtime_end,
                   total_min, in_bed_min, deep_min, rem_min, core_min, awake_min,
                   efficiency_pct
            FROM sleep_sessions
            WHERE user_id = $1 AND sleep_date BETWEEN $2 AND $3
            ORDER BY sleep_date DESC
            LIMIT 60
            """,
            user_id, d_from, d_to,
        )
    if not rows:
        return f"No sleep data for period {d_from} — {d_to}."

    if span_days > 90:
        lines = [f"Sleep {d_from} — {d_to} by month:"]
        for r in rows:
            lines.append(
                f"  {r['month']}: {r['avg_total_min']}min, deep {r['avg_deep_min']}, "
                f"REM {r['avg_rem_min']}, eff {r['avg_efficiency_pct']}% ({r['nights_count']}n)"
            )
        return "\n".join(lines)

    total_vals = [r["total_min"] for r in rows if r["total_min"]]
    deep_vals  = [r["deep_min"] for r in rows if r["deep_min"]]
    rem_vals   = [r["rem_min"] for r in rows if r["rem_min"]]
    eff_vals   = [r["efficiency_pct"] for r in rows if r["efficiency_pct"]]
    def avg(lst): return round(sum(lst) / len(lst), 1) if lst else None

    lines = [f"Sleep {d_from} — {d_to} ({len(rows)} nights):"]
    tot_h = f"{int(avg(total_vals)//60)}h {int(avg(total_vals)%60)}min" if avg(total_vals) else "—"
    lines.append(f"Averages: {tot_h}, deep {avg(deep_vals)}min, REM {avg(rem_vals)}min, eff {avg(eff_vals)}%")
    lines.append("Details:")
    for r in rows:
        tot = r["total_min"]
        h = f"{tot//60}h {tot%60}min" if tot else "?"
        parts = [h]
        if r["deep_min"]:  parts.append(f"deep {r['deep_min']}")
        if r["rem_min"]:   parts.append(f"REM {r['rem_min']}")
        if r["efficiency_pct"]: parts.append(f"eff {r['efficiency_pct']}%")
        lines.append(f"  {r['sleep_date']}: {', '.join(parts)}")
    return "\n".join(lines)


async def _tool_get_mind_entries(
    conn: asyncpg.Connection,
    user_id: str,
    days: int = 21,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    d_from, d_to = _date_range(days, from_date, to_date)
    rows = await conn.fetch(
        """
        SELECT m.content, m.created_at
        FROM messages m
        JOIN dialog_sessions s ON s.id = m.session_id
        WHERE s.user_id = $1
          AND m.role = 'user'
          AND (m.content LIKE '[MIND]%' OR m.content LIKE '[DECISION]%')
          AND m.created_at::date BETWEEN $2 AND $3
        ORDER BY m.created_at DESC
        """,
        user_id, d_from, d_to,
    )
    if not rows:
        return f"No journal/decision entries for period {d_from} — {d_to}."
    lines = [f"{r['created_at'].strftime('%d %b %H:%M')} {r['content']}" for r in rows]
    return f"Journal/decisions {d_from} — {d_to} ({len(rows)} entries):\n" + "\n".join(lines)


async def _tool_get_nutrition(
    conn: asyncpg.Connection,
    user_id: str,
    days: int = 7,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    d_from, d_to = _date_range(days, from_date, to_date)
    span_days = (d_to - d_from).days
    if span_days > 90:
        rows = await conn.fetch(
            """
            SELECT to_char(logged_date, 'YYYY-MM') AS month,
                   ROUND(AVG(calories)::numeric, 0) AS avg_calories,
                   ROUND(AVG(protein)::numeric, 1)  AS avg_protein,
                   ROUND(AVG(fat)::numeric, 1)      AS avg_fat,
                   ROUND(AVG(carbs)::numeric, 1)    AS avg_carbs,
                   COUNT(*) AS days_count
            FROM nutrition_logs
            WHERE user_id=$1 AND logged_date BETWEEN $2 AND $3
            GROUP BY 1 ORDER BY 1
            """,
            user_id, d_from, d_to,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT logged_date, calories, protein, fat, carbs
            FROM nutrition_logs
            WHERE user_id = $1 AND logged_date BETWEEN $2 AND $3
            ORDER BY logged_date DESC
            LIMIT 60
            """,
            user_id, d_from, d_to,
        )
    if not rows:
        return f"No nutrition data for period {d_from} — {d_to}."

    if span_days > 90:
        lines = [f"Nutrition {d_from} — {d_to} by month:"]
        for r in rows:
            lines.append(
                f"  {r['month']}: {r['avg_calories']}kcal, P {r['avg_protein']}g, "
                f"F {r['avg_fat']}g, C {r['avg_carbs']}g ({r['days_count']} d)"
            )
        return "\n".join(lines)

    kcal_vals = [float(r["calories"]) for r in rows if r["calories"]]
    prot_vals = [float(r["protein"]) for r in rows if r["protein"]]
    fat_vals  = [float(r["fat"]) for r in rows if r["fat"]]
    carb_vals = [float(r["carbs"]) for r in rows if r["carbs"]]
    def avg(lst): return round(sum(lst) / len(lst), 1) if lst else None

    lines = [f"Nutrition {d_from} — {d_to} ({len(rows)} days):"]
    lines.append(f"Averages: {avg(kcal_vals)}kcal, P {avg(prot_vals)}g, F {avg(fat_vals)}g, C {avg(carb_vals)}g")
    lines.append("Details:")
    for r in rows:
        lines.append(
            f"  {r['logged_date']}: {r['calories']}kcal P{r['protein']} F{r['fat']} C{r['carbs']}"
        )
    return "\n".join(lines)


async def _tool_get_body_metrics(
    conn: asyncpg.Connection,
    user_id: str,
    days: int = 30,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    d_from, d_to = _date_range(days, from_date, to_date)
    span_days = (d_to - d_from).days
    if span_days > 90:
        rows = await conn.fetch(
            """
            SELECT to_char(recorded_date, 'YYYY-MM') AS month,
                   ROUND(AVG(weight)::numeric, 2)       AS avg_weight,
                   ROUND(AVG(body_fat_pct)::numeric, 1) AS avg_body_fat_pct,
                   ROUND(AVG(muscle_kg)::numeric, 1)    AS avg_muscle_kg,
                   COUNT(*) AS measurements_count
            FROM body_metrics
            WHERE user_id=$1 AND recorded_date BETWEEN $2 AND $3
            GROUP BY 1 ORDER BY 1
            """,
            user_id, d_from, d_to,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT recorded_date, weight, body_fat_pct, muscle_kg, water_pct,
                   visceral_fat, bmi, arms_cm, thighs_cm, waist_cm, hips_cm
            FROM body_metrics
            WHERE user_id = $1 AND recorded_date BETWEEN $2 AND $3
            ORDER BY recorded_date DESC
            LIMIT 60
            """,
            user_id, d_from, d_to,
        )
    if not rows:
        return f"No body data for period {d_from} — {d_to}."

    if span_days > 90:
        lines = [f"Body metrics {d_from} — {d_to} by month:"]
        for r in rows:
            lines.append(
                f"  {r['month']}: weight {r['avg_weight']}kg, fat {r['avg_body_fat_pct']}%, "
                f"muscle {r['avg_muscle_kg']}kg ({r['measurements_count']} meas)"
            )
        return "\n".join(lines)

    lines = [f"Body metrics {d_from} — {d_to} ({len(rows)} measurements):"]
    for r in rows:
        parts = []
        if r["weight"]:       parts.append(f"weight {r['weight']}kg")
        if r["body_fat_pct"]: parts.append(f"fat {r['body_fat_pct']}%")
        if r["muscle_kg"]:    parts.append(f"muscle {r['muscle_kg']}kg")
        if r["water_pct"]:    parts.append(f"water {r['water_pct']}%")
        if r["visceral_fat"]: parts.append(f"visc {r['visceral_fat']}")
        if r["waist_cm"]:     parts.append(f"waist {r['waist_cm']}cm")
        if r["hips_cm"]:      parts.append(f"hips {r['hips_cm']}cm")
        if r["arms_cm"]:      parts.append(f"arms {r['arms_cm']}cm")
        if r["thighs_cm"]:    parts.append(f"thighs {r['thighs_cm']}cm")
        lines.append(f"  {r['recorded_date']}: {', '.join(parts)}")
    return "\n".join(lines)


async def _tool_compute_correlation(
    conn,
    user_id: str,
    metric_a: str,
    metric_b: str,
    days: int = 365,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    lag_days: int = 0,
) -> str:
    from scipy.stats import spearmanr

    d_from, d_to = _date_range(days, from_date, to_date)

    try:
        series_a = dict(await _fetch_metric_series(conn, user_id, metric_a, d_from, d_to))
        series_b = dict(await _fetch_metric_series(conn, user_id, metric_b, d_from, d_to))
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    # Shift: metric_b is taken lag_days days after metric_a
    if lag_days:
        series_b = {k - timedelta(days=lag_days): v for k, v in series_b.items()}

    common_dates = sorted(set(series_a) & set(series_b))
    n = len(common_dates)

    if n < 3:
        return json.dumps({
            "error": (
                f"Not enough matching data points (n={n}). "
                f"{metric_a} — {len(series_a)} points, {metric_b} — {len(series_b)}."
            ),
        }, ensure_ascii=False)

    a_vals = [series_a[d] for d in common_dates]
    b_vals = [series_b[d] for d in common_dates]

    r, p = spearmanr(a_vals, b_vals)

    abs_r = abs(r)
    if abs_r >= 0.7:   strength = "strong"
    elif abs_r >= 0.4: strength = "moderate"
    elif abs_r >= 0.2: strength = "weak"
    else:              strength = "negligible"
    direction = "positive" if r > 0 else "negative"

    return json.dumps({
        "metric_a": metric_a,
        "metric_b": metric_b,
        "r": round(float(r), 3),
        "p_value": round(float(p), 4),
        "n": n,
        "lag_days": lag_days,
        "period": f"{d_from} — {d_to}",
        "interpretation": f"{strength} {direction} correlation",
        "significant": bool(p < 0.05),
        "warning": "n < 20, interpret with caution" if n < 20 else None,
    }, ensure_ascii=False)


async def _tool_get_trend(
    conn,
    user_id: str,
    metric: str,
    period: str = "month",
    days: int = 365,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    d_from, d_to = _date_range(days, from_date, to_date)

    if metric not in METRIC_MAP:
        return json.dumps(
            {"error": f"Unknown metric: {metric!r}. Available: {_METRIC_NAMES}"},
            ensure_ascii=False,
        )

    table, date_col, val_col, param_key = METRIC_MAP[metric]

    if table == "lab_results":
        rows = await conn.fetch(
            "SELECT to_char(test_date, 'YYYY-MM') AS month, "
            "ROUND(AVG(value_numeric)::numeric, 3) AS avg_val, "
            "MIN(value_numeric) AS min_val, MAX(value_numeric) AS max_val, "
            "COUNT(*) AS n "
            "FROM lab_results "
            "WHERE user_id=$1 AND parameter_key=$2 AND test_date BETWEEN $3 AND $4 "
            "AND value_numeric IS NOT NULL "
            "GROUP BY 1 ORDER BY 1",
            user_id, param_key, d_from, d_to,
        )
    else:
        rows = await conn.fetch(
            f"SELECT to_char({date_col}, 'YYYY-MM') AS month, "
            f"ROUND(AVG({val_col})::numeric, 2) AS avg_val, "
            f"MIN({val_col}) AS min_val, MAX({val_col}) AS max_val, "
            f"COUNT(*) AS n "
            f"FROM {table} "
            f"WHERE user_id=$1 AND {date_col} BETWEEN $2 AND $3 "
            f"AND {val_col} IS NOT NULL "
            f"GROUP BY 1 ORDER BY 1",
            user_id, d_from, d_to,
        )

    result = [dict(r) for r in rows]
    total_n = sum(int(r["n"]) for r in result)
    lines = [
        f"{r['month']}: avg={r['avg_val']} min={r['min_val']} max={r['max_val']} (n={r['n']})"
        for r in result
    ]
    return f"{metric} {d_from} — {d_to} (total {total_n} points):\n" + "\n".join(lines)


async def _tool_get_user_profile(conn: asyncpg.Connection, user_id: str) -> str:
    row = await conn.fetchrow(
        "SELECT profile_text FROM user_profile WHERE user_id = $1",
        user_id,
    )
    if not row:
        return "User profile not found."
    return row["profile_text"]


async def _tool_get_memory_insights(conn: asyncpg.Connection, user_id: str) -> str:
    rows = await conn.fetch(
        """
        SELECT insight_text, confirmed_at
        FROM memory_insights
        WHERE user_id = $1
        ORDER BY confirmed_at DESC
        """,
        user_id,
    )
    if not rows:
        return "No confirmed insights yet."
    insights = [{"insight": r["insight_text"], "confirmed": r["confirmed_at"].isoformat()} for r in rows]
    return json.dumps(insights, ensure_ascii=False)


async def _tool_search_knowledge_base(
    conn: asyncpg.Connection, query: str, top_k: int = 5
) -> str:
    from src.llm.client import embed_text

    try:
        vec = await embed_text(query)
    except Exception as e:
        return f"Failed to create query embedding: {e}"

    vec_str = "[" + ",".join(str(x) for x in vec) + "]"
    rows = await conn.fetch(
        """
        SELECT title, content, source,
               1 - (embedding <=> $1::vector) AS similarity
        FROM knowledge_chunks
        ORDER BY embedding <=> $1::vector
        LIMIT $2
        """,
        vec_str, top_k,
    )
    if not rows:
        return "Knowledge base is empty or contains no relevant materials."
    results = [
        {
            "title": r["title"],
            "source": r["source"],
            "similarity": float(r["similarity"]),
            "content": r["content"],
        }
        for r in rows
    ]
    return json.dumps(results, ensure_ascii=False)


async def _tool_search_personal_data(
    conn: asyncpg.Connection, user_id: str, query: str, top_k: int = 5
) -> str:
    from src.llm.client import embed_text

    try:
        vec = await embed_text(query)
    except Exception as e:
        return f"Failed to create query embedding: {e}"

    vec_str = "[" + ",".join(str(x) for x in vec) + "]"
    rows = await conn.fetch(
        """
        SELECT content, embedded_at,
               1 - (embedding <=> $1::vector) AS similarity
        FROM message_embeddings
        WHERE user_id = $2
        ORDER BY embedding <=> $1::vector
        LIMIT $3
        """,
        vec_str, user_id, top_k,
    )
    if not rows:
        return "No indexed data available for semantic search."
    results = [
        {"content": r["content"], "similarity": float(r["similarity"]), "at": r["embedded_at"].isoformat()}
        for r in rows
    ]
    return json.dumps(results, ensure_ascii=False)


async def _tool_get_workouts(
    conn: asyncpg.Connection,
    user_id: str,
    days: int = 30,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    workout_type: Optional[str] = None,
) -> str:
    """Returns a compact text digest of workouts — aggregated, not raw rows."""
    d_from, d_to = _date_range(days, from_date, to_date)
    span_days = (d_to - d_from).days
    type_filter = "AND workout_type = $4" if workout_type else ""
    params = [user_id, d_from, d_to] + ([workout_type] if workout_type else [])

    if span_days > 90:
        # Monthly aggregation — SQL computes, Python formats
        rows = await conn.fetch(
            f"""
            SELECT to_char(workout_date, 'YYYY-MM') AS month,
                   workout_type,
                   COUNT(*) AS cnt,
                   ROUND(AVG(duration_min)::numeric, 0) AS avg_min,
                   ROUND(AVG(avg_heart_rate)::numeric, 0) AS avg_hr
            FROM workout_sessions
            WHERE user_id = $1 AND workout_date BETWEEN $2 AND $3 {type_filter}
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
            *params,
        )
        if not rows:
            return f"No workouts for period {d_from} — {d_to}."
        lines = [f"Workouts {d_from} — {d_to} by month:"]
        for r in rows:
            hr = f", avg HR {r['avg_hr']}" if r['avg_hr'] else ""
            lines.append(f"  {r['month']} {r['workout_type']}: {r['cnt']}×, avg {r['avg_min']} min{hr}")
        return "\n".join(lines)

    # Short period — aggregate in Python, don't push raw JSON to LLM
    rows = await conn.fetch(
        f"""
        SELECT workout_date, workout_type,
               duration_min, avg_heart_rate, max_heart_rate
        FROM workout_sessions
        WHERE user_id = $1 AND workout_date BETWEEN $2 AND $3 {type_filter}
        ORDER BY workout_date DESC
        LIMIT 100
        """,
        *params,
    )
    if not rows:
        return f"No workouts for period {d_from} — {d_to}."

    # Aggregate by type
    by_type: dict[str, list] = {}
    for r in rows:
        wt = r["workout_type"] or "Other"
        by_type.setdefault(wt, []).append(r)

    lines = [f"Workouts {d_from} — {d_to} ({len(rows)} sessions):"]
    lines.append("\nBy type:")
    for wt, sessions in sorted(by_type.items()):
        durs = [s["duration_min"] for s in sessions if s["duration_min"]]
        hrs  = [s["avg_heart_rate"] for s in sessions if s["avg_heart_rate"]]
        max_hrs = [s["max_heart_rate"] for s in sessions if s["max_heart_rate"]]
        avg_dur = round(sum(durs) / len(durs)) if durs else None
        avg_hr  = round(sum(hrs) / len(hrs)) if hrs else None
        max_hr  = max(max_hrs) if max_hrs else None
        hr_str = f", avg HR {avg_hr}, max HR {max_hr}" if avg_hr else ""
        lines.append(f"  {wt}: {len(sessions)}×, avg {avg_dur} min{hr_str}")

    # Weekly breakdown — pass start and end explicitly so the model doesn't compute them
    from collections import defaultdict
    weeks: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        d = r["workout_date"]
        week_start = d - timedelta(days=d.weekday())  # Monday
        weeks[week_start][r["workout_type"] or "Other"] += 1

    if len(weeks) > 1:
        lines.append("\nBy week:")
        for ws in sorted(weeks.keys()):
            we = ws + timedelta(days=6)  # Sunday
            parts = ", ".join(f"{wt} {cnt}×" for wt, cnt in sorted(weeks[ws].items()))
            lines.append(f"  {ws.strftime('%d %b')} – {we.strftime('%d %b')}: {parts}")

    # Detailed list (compact, one line per session)
    lines.append("\nDetails:")
    for r in rows:
        dur = f"{r['duration_min']} min" if r["duration_min"] else "?"
        hr  = f" HR {r['avg_heart_rate']}/{r['max_heart_rate']}" if r["avg_heart_rate"] else ""
        lines.append(f"  {r['workout_date']} {r['workout_type']} {dur}{hr}")

    return "\n".join(lines)


async def _tool_get_lab_results(
    conn: asyncpg.Connection,
    user_id: str,
    days: int = 365,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    parameter_key: Optional[str] = None,
    category: Optional[str] = None,
) -> str:
    d_from, d_to = _date_range(days, from_date, to_date)
    if parameter_key:
        rows = await conn.fetch(
            """
            SELECT r.test_date, r.parameter_name, r.parameter_key, r.category,
                   r.value_numeric, r.value_text, r.unit,
                   r.ref_min, r.ref_max, r.is_abnormal,
                   s.lab_name, s.notes
            FROM lab_results r
            JOIN lab_sessions s ON s.id = r.session_id
            WHERE r.user_id = $1 AND r.parameter_key = $2
              AND r.test_date BETWEEN $3 AND $4
            ORDER BY r.test_date
            """,
            user_id, parameter_key, d_from, d_to,
        )
    elif category:
        rows = await conn.fetch(
            """
            SELECT r.test_date, r.parameter_name, r.parameter_key, r.category,
                   r.value_numeric, r.value_text, r.unit,
                   r.ref_min, r.ref_max, r.is_abnormal,
                   s.lab_name, s.notes
            FROM lab_results r
            JOIN lab_sessions s ON s.id = r.session_id
            WHERE r.user_id = $1 AND r.category = $2
              AND r.test_date BETWEEN $3 AND $4
            ORDER BY r.test_date, r.parameter_key
            """,
            user_id, category, d_from, d_to,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT r.test_date, r.parameter_name, r.parameter_key, r.category,
                   r.value_numeric, r.value_text, r.unit,
                   r.ref_min, r.ref_max, r.is_abnormal,
                   s.lab_name, s.notes
            FROM lab_results r
            JOIN lab_sessions s ON s.id = r.session_id
            WHERE r.user_id = $1
              AND r.test_date BETWEEN $2 AND $3
            ORDER BY r.test_date DESC, r.parameter_key
            LIMIT 100
            """,
            user_id, d_from, d_to,
        )
    if not rows:
        return f"No lab results for period {d_from} — {d_to}."

    lines = [f"Lab results {d_from} — {d_to}:"]
    for r in rows:
        flag = "↑" if r["is_abnormal"] else "✓"
        val = r["value_numeric"] if r["value_numeric"] is not None else r["value_text"] or "?"
        ref = ""
        if r["ref_min"] is not None and r["ref_max"] is not None:
            ref = f" [N: {r['ref_min']}–{r['ref_max']}]"
        lines.append(
            f"  {r['test_date']} [{r['category']}] {r['parameter_name']}: "
            f"{val} {r['unit'] or ''}{ref} {flag}"
        )
    return "\n".join(lines)


async def _tool_get_doctor_reports(
    conn: asyncpg.Connection,
    user_id: str,
    days: int = 730,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    study_type: Optional[str] = None,
) -> str:
    d_from, d_to = _date_range(days, from_date, to_date)
    if study_type:
        rows = await conn.fetch(
            """
            SELECT study_date, study_type, body_area,
                   LEFT(conclusion, 1000) AS conclusion,
                   doctor, lab_name
            FROM doctor_reports
            WHERE user_id = $1 AND study_type = $2
              AND study_date BETWEEN $3 AND $4
            ORDER BY study_date DESC
            LIMIT 10
            """,
            user_id, study_type, d_from, d_to,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT study_date, study_type, body_area,
                   LEFT(conclusion, 1000) AS conclusion,
                   doctor, lab_name
            FROM doctor_reports
            WHERE user_id = $1
              AND study_date BETWEEN $2 AND $3
            ORDER BY study_date DESC
            LIMIT 10
            """,
            user_id, d_from, d_to,
        )
    if not rows:
        return f"No doctor reports for period {d_from} — {d_to}."

    lines = [f"Doctor reports {d_from} — {d_to}:"]
    for r in rows:
        who = f" — {r['doctor']}" if r["doctor"] else ""
        lines.append(f"\n{r['study_date']} {r['study_type'].upper()} ({r['body_area'] or ''}){who}:")
        lines.append(r["conclusion"] or "")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_analyst(db: asyncpg.Pool, user_id: str, intent: str, force_tools: bool = False, system: str | None = None, history: list[dict] | None = None, return_model: bool = False, tools: list[dict] | None = None) -> str | tuple[str, str]:
    """Runs the analyst agent with tool-calling for the given intent.

    The agent autonomously decides which data to load via tools,
    then returns an analytical response.
    """
    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        tool_callables = {
            "get_health_metrics":  lambda days=14, from_date=None, to_date=None: _tool_get_health_metrics(conn, user_id, days, from_date, to_date),
            "get_sleep":           lambda days=14, from_date=None, to_date=None: _tool_get_sleep(conn, user_id, days, from_date, to_date),
            "get_mind_entries":    lambda days=21, from_date=None, to_date=None: _tool_get_mind_entries(conn, user_id, days, from_date, to_date),
            "get_nutrition":       lambda days=7,  from_date=None, to_date=None: _tool_get_nutrition(conn, user_id, days, from_date, to_date),
            "get_body_metrics":    lambda days=30, from_date=None, to_date=None: _tool_get_body_metrics(conn, user_id, days, from_date, to_date),
            "get_user_profile":    lambda: _tool_get_user_profile(conn, user_id),
            "get_memory_insights": lambda: _tool_get_memory_insights(conn, user_id),
            "search_personal_data":  lambda query, top_k=5: _tool_search_personal_data(conn, user_id, query, top_k),
            "search_knowledge_base": lambda query, top_k=5: _tool_search_knowledge_base(conn, query, top_k),
            "get_lab_results":       lambda days=5000, from_date="2010-01-01", to_date=None, parameter_key=None, category=None: _tool_get_lab_results(conn, user_id, days, from_date, to_date, parameter_key, category),
            "get_doctor_reports":    lambda days=3650, from_date=None, to_date=None, study_type=None: _tool_get_doctor_reports(conn, user_id, days, from_date, to_date, study_type),
            "compute_correlation":   lambda metric_a, metric_b, days=365, from_date=None, to_date=None, lag_days=0: _tool_compute_correlation(conn, user_id, metric_a, metric_b, days, from_date, to_date, lag_days),
            "get_trend":             lambda metric, period="month", days=365, from_date=None, to_date=None: _tool_get_trend(conn, user_id, metric, period, days, from_date, to_date),
            "get_workouts":          lambda days=30, from_date=None, to_date=None, workout_type=None: _tool_get_workouts(conn, user_id, days, from_date, to_date, workout_type),
        }

        return await ask_model_with_tools(
            prompt=intent,
            tools=tools if tools is not None else ANALYST_TOOLS,
            tool_callables=tool_callables,
            system=system or ANALYST_SYSTEM,
            model=settings.agent_model,
            max_tokens=6000,
            max_iterations=10,
            force_tools=force_tools,
            history=history,
            return_model=return_model,
        )
