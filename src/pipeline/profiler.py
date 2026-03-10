"""Profile Updater — updates factual data in user_profile once a week."""

import logging
from datetime import date, timedelta

import asyncpg

from src.config import settings
from src.llm.client import ask_model

log = logging.getLogger(__name__)


async def run_profiler(db: asyncpg.Pool, user_id: str) -> dict:
    """
    Reads data for 2 weeks, updates the factual sections of user_profile.

    Only updates objective facts that change over time:
    weight, calorie intake, training regimen, sleep patterns.
    Does not touch: medical block, personality, goals, analysis rules.

    Returns:
        {"updated": bool, "summary": str}
    """
    today = date.today()
    cutoff = today - timedelta(days=14)

    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        profile_row = await conn.fetchrow(
            "SELECT profile_text FROM user_profile WHERE user_id = $1",
            user_id,
        )
        if not profile_row:
            log.warning("profiler: no profile for user %s", user_id)
            return {"updated": False, "summary": "profile not found"}

        body_rows = await conn.fetch(
            """
            SELECT recorded_date, weight, body_fat_pct, muscle_kg,
                   waist_cm, hips_cm, arms_cm
            FROM body_metrics
            WHERE user_id = $1 AND recorded_date >= $2
            ORDER BY recorded_date DESC
            """,
            user_id, cutoff,
        )

        health_rows = await conn.fetch(
            """
            SELECT recorded_date, hrv_ms, steps, active_kcal, resting_hr
            FROM health_metrics
            WHERE user_id = $1 AND recorded_date >= $2
            ORDER BY recorded_date DESC
            """,
            user_id, cutoff,
        )

        nutrition_rows = await conn.fetch(
            """
            SELECT logged_date, calories, protein, fat, carbs
            FROM nutrition_logs
            WHERE user_id = $1 AND logged_date >= $2
            ORDER BY logged_date DESC
            """,
            user_id, cutoff,
        )

        workout_rows = await conn.fetch(
            """
            SELECT workout_date, workout_type, duration_min, avg_heart_rate
            FROM workout_sessions
            WHERE user_id = $1 AND workout_date >= $2
            ORDER BY workout_date DESC
            """,
            user_id, cutoff,
        )

        sleep_rows = await conn.fetch(
            """
            SELECT sleep_date, total_min, deep_min, rem_min, efficiency_pct
            FROM sleep_sessions
            WHERE user_id = $1 AND sleep_date >= $2
            ORDER BY sleep_date DESC
            """,
            user_id, cutoff,
        )

    current_profile = profile_row["profile_text"]

    # Build data summary
    body_summary = ""
    if body_rows:
        latest = dict(body_rows[0])
        body_summary = f"Latest measurement ({latest['recorded_date']}): weight {latest['weight']} kg"
        if latest.get('body_fat_pct'):
            body_summary += f", fat {latest['body_fat_pct']}%"
        if latest.get('muscle_kg'):
            body_summary += f", muscle {latest['muscle_kg']} kg"

    nutrition_summary = ""
    if nutrition_rows:
        avg_cal = sum(r['calories'] or 0 for r in nutrition_rows) / len(nutrition_rows)
        avg_protein = sum(r['protein'] or 0 for r in nutrition_rows) / len(nutrition_rows)
        nutrition_summary = f"Average calorie intake over 2 weeks: {avg_cal:.0f} kcal/day, protein {avg_protein:.0f} g/day"

    workout_summary = ""
    if workout_rows:
        from collections import Counter
        types = Counter(r['workout_type'] for r in workout_rows)
        workout_summary = f"Workouts over 2 weeks: {len(workout_rows)} sessions — " + ", ".join(
            f"{t} {n}x" for t, n in types.most_common()
        )

    sleep_summary = ""
    if sleep_rows:
        avg_total = sum(r['total_min'] or 0 for r in sleep_rows) / len(sleep_rows)
        avg_eff = sum(r['efficiency_pct'] or 0 for r in sleep_rows) / len(sleep_rows)
        sleep_summary = f"Average sleep over 2 weeks: {avg_total:.0f} min/night, efficiency {avg_eff:.0f}%"

    data_block = "\n".join(filter(None, [body_summary, nutrition_summary, workout_summary, sleep_summary]))

    if not data_block:
        log.info("profiler: no recent data for user %s, skipping", user_id)
        return {"updated": False, "summary": "no data for the last 2 weeks"}

    prompt = (
        f"Current user profile:\n{current_profile}\n\n"
        f"Factual data for the last 2 weeks:\n{data_block}\n\n"
        "Update the profile: replace outdated facts in the BODY & ACTIVITY section "
        "(weight, calories, training regimen, sleep) with current data from above.\n"
        "STRICTLY prohibited to modify:\n"
        "— medical block (━━━ BACKGROUND CONTEXT ━━━)\n"
        "— WHO SHE IS section\n"
        "— HOW TO ANALYZE section\n"
        "— HOW TO COMMUNICATE section\n"
        "— any goals and long-term plans\n"
        "Return only the full updated profile text without any comments."
    )

    try:
        updated_profile = await ask_model(
            prompt,
            model=settings.agent_model,
            max_tokens=2000,
        )
    except Exception as e:
        log.exception("profiler: LLM call failed: %s", e)
        return {"updated": False, "summary": f"LLM error: {e}"}

    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        await conn.execute(
            """
            UPDATE user_profile SET profile_text = $1, updated_at = now()
            WHERE user_id = $2
            """,
            updated_profile, user_id,
        )

    log.info("profiler: profile updated for user %s", user_id)
    return {"updated": True, "summary": data_block}
