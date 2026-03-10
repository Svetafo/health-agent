"""Memory Synthesizer — analyzes data for 4 weeks, accumulates patterns."""

import json
import logging
from datetime import date, timedelta

import asyncpg

from src.config import settings
from src.llm.client import ask_model

log = logging.getLogger(__name__)

CONFIRMATIONS_THRESHOLD = 3
PATTERN_TTL_DAYS = 30
MAX_PATTERNS = 7


async def run_synthesizer(db: asyncpg.Pool, user_id: str) -> dict:
    """
    Analyzes data for 28 days, finds patterns, accumulates confirmations.

    Returns:
        {
            "new": int,
            "confirmed": int,
            "push_patterns": [(id, text), ...]  # those whose confirmations reached 3
        }
    """
    today = date.today()

    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        # 1. Clean up expired pending_insights
        deleted = await conn.execute(
            "DELETE FROM pending_insights WHERE expires_at < $1 AND status = 'pending'",
            today,
        )
        log.info("synthesizer: cleaned expired insights (%s)", deleted)

        # 2. Collect data for 28 days
        cutoff = today - timedelta(days=28)

        health_rows = await conn.fetch(
            """
            SELECT recorded_date, hrv_ms, heart_rate, resting_hr, steps,
                   active_kcal, distance_km, vo2max
            FROM health_metrics
            WHERE user_id = $1 AND recorded_date >= $2
            ORDER BY recorded_date
            """,
            user_id, cutoff,
        )

        mind_rows = await conn.fetch(
            """
            SELECT m.content, m.created_at
            FROM messages m
            JOIN dialog_sessions s ON s.id = m.session_id
            WHERE s.user_id = $1 AND m.role = 'user'
              AND (m.content LIKE '[MIND]%%' OR m.content LIKE '[DECISION]%%')
              AND m.created_at >= $2
            ORDER BY m.created_at
            """,
            user_id, cutoff,
        )

        nutrition_rows = await conn.fetch(
            """
            SELECT logged_date, calories, protein, fat, carbs
            FROM nutrition_logs
            WHERE user_id = $1 AND logged_date >= $2
            ORDER BY logged_date
            """,
            user_id, cutoff,
        )

        body_rows = await conn.fetch(
            """
            SELECT recorded_date, weight, body_fat_pct, muscle_kg, water_pct,
                   visceral_fat, bmi, arms_cm, thighs_cm, waist_cm, hips_cm
            FROM body_metrics
            WHERE user_id = $1 AND recorded_date >= $2
            ORDER BY recorded_date
            """,
            user_id, cutoff,
        )

        profile_row = await conn.fetchrow(
            "SELECT profile_text FROM user_profile WHERE user_id = $1",
            user_id,
        )

        # 3. Load existing pending_insights
        existing_rows = await conn.fetch(
            """
            SELECT id, pattern_text, confirmations, first_seen
            FROM pending_insights
            WHERE user_id = $1 AND status = 'pending'
            ORDER BY confirmations DESC
            """,
            user_id,
        )

    health_data = [dict(r) for r in health_rows]
    mind_data = [r["content"] for r in mind_rows]
    nutrition_data = [dict(r) for r in nutrition_rows]
    body_data = [dict(r) for r in body_rows]
    profile_text = profile_row["profile_text"] if profile_row else "no data"
    existing = [dict(r) for r in existing_rows]

    # If no data sources at all — exit
    if not health_data and not nutrition_data and not body_data and not mind_data:
        log.info("synthesizer: no data for user %s, skipping", user_id)
        return {"new": 0, "confirmed": 0, "push_patterns": []}

    # 4. LLM call
    existing_list = "\n".join(
        f"[id={r['id']}] {r['pattern_text']} (confirmed {r['confirmations']} times, first seen {r['first_seen']})"
        for r in existing
    ) or "none"

    prompt = (
        f"User profile:\n{profile_text}\n\n"
        f"Apple Health data for 28 days ({len(health_data)} records):\n{json.dumps(health_data, default=str, ensure_ascii=False)}\n\n"
        f"Thoughts and decisions [MIND]/[DECISION] ({len(mind_data)} records):\n{json.dumps(mind_data, ensure_ascii=False)}\n\n"
        f"Nutrition ({len(nutrition_data)} records):\n{json.dumps(nutrition_data, default=str, ensure_ascii=False)}\n\n"
        f"Body metrics ({len(body_data)} records):\n{json.dumps(body_data, default=str, ensure_ascii=False)}\n\n"
        f"Already known patterns (do not duplicate):\n{existing_list}\n\n"
        f"Analyze 4 weeks of data. Find {MAX_PATTERNS} significant patterns/correlations.\n"
        "Each pattern is a specific observation with numbers, 1-2 sentences.\n"
        "Return strict JSON (no markdown):\n"
        '{"new_patterns": ["text..."], "confirmed_ids": [1, 2]}\n\n'
        "confirmed_ids — ids of already known patterns confirmed by new data.\n"
        "new_patterns — new patterns not yet in the list."
    )

    try:
        raw = await ask_model(
            prompt,
            model=settings.agent_model,
            max_tokens=1000,
        )
        # Strip possible markdown block
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        llm_result = json.loads(raw)
    except Exception as e:
        log.exception("synthesizer: LLM call or parse failed: %s", e)
        return {"new": 0, "confirmed": 0, "push_patterns": []}

    new_patterns: list[str] = llm_result.get("new_patterns", [])[:MAX_PATTERNS]
    confirmed_ids: list[int] = llm_result.get("confirmed_ids", [])

    push_patterns: list[tuple[int, str]] = []

    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        async with conn.transaction():
            # 5a. Update confirmed patterns
            for pid in confirmed_ids:
                row = await conn.fetchrow(
                    """
                    UPDATE pending_insights
                    SET confirmations = confirmations + 1, last_seen = $1
                    WHERE id = $2 AND user_id = $3 AND status = 'pending'
                    RETURNING id, pattern_text, confirmations
                    """,
                    today, pid, user_id,
                )
                if row and row["confirmations"] == CONFIRMATIONS_THRESHOLD:
                    push_patterns.append((row["id"], row["pattern_text"]))

            # 5b. Insert new patterns
            for text in new_patterns:
                await conn.execute(
                    """
                    INSERT INTO pending_insights
                        (user_id, pattern_text, confirmations, first_seen, last_seen,
                         expires_at, status)
                    VALUES ($1, $2, 1, $3, $3, $4, 'pending')
                    """,
                    user_id, text, today, today + timedelta(days=PATTERN_TTL_DAYS),
                )

    log.info(
        "synthesizer: new=%d confirmed=%d push=%d",
        len(new_patterns), len(confirmed_ids), len(push_patterns),
    )
    return {
        "new": len(new_patterns),
        "confirmed": len(confirmed_ids),
        "push_patterns": push_patterns,
    }
