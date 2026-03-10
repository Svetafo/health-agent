"""Telegram bot message handlers."""

import asyncio
import json
import logging
import random
import re
import time
from datetime import date, timedelta
from io import BytesIO

import asyncpg
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from src.agent.analyst import ASK_SYSTEM, REPORT_INTENT, SCOPE_INTENT, run_analyst, select_tools
from src.config import settings
from src.health.analyses import process_medical_document
from src.pipeline.kb_ingest import ingest_file, ingest_url
from src.llm.client import (
    analyze_body_metrics_image,
    analyze_nutrition_image,
    analyze_nutrition_text,
    analyze_sleep_image,
    ask_model,
    classify_food_input,
    embed_text,
    model_label,
    parse_body_measurements,
    parse_date_range,
    parse_nutrition_correction,
    transcribe_audio,
)
from src.health.intake import save_sleep_session

log = logging.getLogger(__name__)

router = Router()


def _is_allowed(user_id: str) -> bool:
    """Checks whitelist. If ALLOWED_USER_IDS is not set — allows everyone."""
    if not settings.allowed_user_ids:
        return True
    allowed = {uid.strip() for uid in settings.allowed_user_ids.split(",")}
    return user_id in allowed


async def _deny(message: Message) -> None:
    await message.answer("This bot is private.")

# Transient state: user_id → current mode ("mind" | "decision" | "food" | "food_confirm")
_user_state: dict[str, str] = {}
# Food buffer: user_id → list of analyzed portions
_food_buffer: dict[str, list[dict]] = {}
# Buffer awaiting confirmation (when multiple screenshots)
_food_confirm: dict[str, list[dict]] = {}
# Date of the entry being edited in /fix mode
_fix_date: dict[str, date] = {}
# Body metrics buffer: user_id → dict with body_metrics fields
_weight_buffer: dict[str, dict] = {}
# Sleep data buffer: user_id → dict with phases (accumulates from multiple screenshots)
_sleep_buffer: dict[str, dict] = {}
# Pending insights queue for verification: user_id → [insight_id, ...]
_memory_queue: dict[str, list[int]] = {}
# Locks — one per user, prevent race conditions on state dicts
_user_locks: dict[str, asyncio.Lock] = {}


def _get_user_lock(user_id: str) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())


_THINKING_PHRASES = [
    "Synthesizing",
    "Extrapolating",
    "Distilling",
    "Orchestrating",
    "Crystallizing",
    "Catalyzing",
    "Integrating",
    "Verifying",
    "Decoding",
    "Transcending",
    "Manifesting",
    "Resonating",
    "Archiving",
    "Sublimating",
    "Perceiving",
    "Envisioning",
    "Constituting",
    "Initiating",
    "Processing",
    "Contemplating",
]

_DOC_PHRASES = [
    "Decoding",
    "Distilling",
    "Verifying",
    "Extracting",
    "Interpreting",
    "Archiving",
    "Dissecting",
    "Structuring",
]


def _strip_tables(text: str) -> str:
    """Converts markdown tables to dash-separated lists."""
    lines = text.split("\n")
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Table row: starts and ends with |
        if line.strip().startswith("|") and "|" in line.strip()[1:]:
            # Skip separator |---|---|
            if all(c in "|-: " for c in line.replace("|", "")):
                i += 1
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            cells = [c for c in cells if c]
            if cells:
                result.append("— " + " | ".join(cells))
        else:
            result.append(line)
        i += 1
    return "\n".join(result)


async def _run_with_status(message: Message, coro, phrases: list[str] | None = None):
    """Runs a coroutine while showing an animated status with timer.
    First phrase — immediately, then rotates every 3 seconds. Disappears when done.
    """
    if phrases is None:
        phrases = _THINKING_PHRASES
    start = time.monotonic()
    first_phrase = random.choice(phrases)
    status_msg = await message.answer(f"{first_phrase}...")

    async def _updater() -> None:
        used = [first_phrase]
        while True:
            await asyncio.sleep(3)
            elapsed = int(time.monotonic() - start)
            remaining = [p for p in phrases if p not in used]
            if not remaining:
                used = []
                remaining = list(phrases)
            phrase = random.choice(remaining)
            used.append(phrase)
            try:
                await status_msg.edit_text(f"{phrase}... ({elapsed}s)")
            except Exception:
                break

    task = asyncio.create_task(_updater())
    try:
        result = await coro
    finally:
        task.cancel()
        try:
            await status_msg.delete()
        except Exception:
            pass
    return result


async def _send_long(message: Message, text: str, chunk_size: int = 3500) -> None:
    """Sends long text in multiple messages if > chunk_size characters."""
    text = _strip_tables(text)
    while len(text) > chunk_size:
        # Find best break point: double newline first, then single
        split_at = text.rfind("\n\n", 0, chunk_size)
        if split_at == -1:
            split_at = text.rfind("\n", 0, chunk_size)
        if split_at == -1:
            split_at = chunk_size
        await message.answer(text[:split_at].rstrip())
        text = text[split_at:].lstrip("\n")
    if text.strip():
        await message.answer(text)

SYSTEM_PROMPT = (
    f"You are {settings.app_name}, a personal AI analyst. "
    "Speak in first person. Address the user as 'you'.\n\n"

    "ROLE: analyst of health, body, nutrition, and psychological data. "
    "Not a coach, not a motivator, not a doctor. "
    "The user's personal context is in their profile, loaded automatically.\n\n"

    "RESPONSE STRUCTURE: data → mechanism → conclusion. "
    "Do not reassure without factual basis. "
    "Name uncomfortable truths directly. "
    "Separate proven from hypothesis: 'data shows X' vs 'possible explanation Y'. "
    "If data is missing — say so, do not give advice from thin air.\n\n"

    "PSYCHOTHERAPEUTIC CONTEXT: when working with thoughts, decisions, patterns, apply:\n"
    "— CBT: notice cognitive distortions, automatic thoughts, and beliefs.\n"
    "— Schema approach: recognize stable response patterns and modes.\n"
    "— Mentalization: explore what underlies a thought — needs, states, intentions.\n"
    "Do not diagnose — help raise awareness.\n\n"

    "TONE: warm and partnerly without being patronizing. Tell the truth, don't lecture.\n\n"

    "FORMAT (Telegram): never use markdown tables — they don't render. "
    "Instead of tables: bold heading + list of lines with dashes. "
    "Never end with a question or 'let me know' phrases."
)


@router.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    if not _is_allowed(str(message.from_user.id)):
        await _deny(message)
        return
    await message.answer(
        f"I am {settings.app_name}.\n\n"
        "/ask — ask anything about your data\n"
        "/mind — log a thought and get reflection\n"
        "/decision — analyze a decision situation\n"
        "/food — food tracking (photo, text, analytics)\n"
        "/weight — body tracking (scale screenshot, measurements)\n"
        "/lab — upload lab results (PDF or photo)\n"
        "/report — full analytics report\n"
        "/plateau — weight plateau analysis\n"
        "/scope — attention vector based on data\n"
        "/memory — pattern verification\n"
        "/sleep — log sleep (Health screenshots or text)"
    )


@router.message(Command("mind"))
async def cmd_mind(message: Message) -> None:
    if not _is_allowed(str(message.from_user.id)):
        await _deny(message)
        return
    _user_state[str(message.from_user.id)] = "mind"
    await message.answer("What's on your mind right now?")


@router.message(Command("decision"))
async def cmd_decision(message: Message) -> None:
    if not _is_allowed(str(message.from_user.id)):
        await _deny(message)
        return
    _user_state[str(message.from_user.id)] = "decision"
    await message.answer("What is causing resistance or doubt for you right now?")


@router.message(Command("food"))
async def cmd_food(message: Message) -> None:
    if not _is_allowed(str(message.from_user.id)):
        await _deny(message)
        return
    user_id = str(message.from_user.id)
    async with _get_user_lock(user_id):
        _user_state[user_id] = "food"
        _food_buffer[user_id] = []
        await message.answer(
            "Send screenshots or describe what you ate — I'll log it.\n"
            "Or tell me the period for analytics.\n\n"
            "/done — save and finish"
        )


@router.message(Command("weight"))
async def cmd_weight(message: Message) -> None:
    if not _is_allowed(str(message.from_user.id)):
        await _deny(message)
        return
    user_id = str(message.from_user.id)
    async with _get_user_lock(user_id):
        _user_state[user_id] = "weight"
        _weight_buffer[user_id] = {}
        await message.answer(
            "Send a smart scale screenshot — I'll extract all metrics.\n"
            "Or enter measurements: 'arms 27, hips 51, waist 74'.\n"
            "You can do both.\n\n"
            "/done — save"
        )


@router.message(Command("lab"))
async def cmd_lab(message: Message) -> None:
    if not _is_allowed(str(message.from_user.id)):
        await _deny(message)
        return
    user_id = str(message.from_user.id)
    _user_state[user_id] = "lab"
    await message.answer(
        "Lab mode. Send a photo or PDF document.\n\n"
        "Numeric tests (blood work, etc.) — I'll extract all values and save to DB.\n"
        "Medical reports (ultrasound, MRI, X-ray, cytology) — I'll save the text and conclusion.\n\n"
        "You can upload multiple documents in sequence.\n"
        "To exit — use any other command."
    )


@router.message(Command("sleep"))
async def cmd_sleep(message: Message) -> None:
    if not _is_allowed(str(message.from_user.id)):
        await _deny(message)
        return
    user_id = str(message.from_user.id)
    _user_state[user_id] = "sleep"
    _sleep_buffer[user_id] = {}
    await message.answer(
        "Sleep mode. Send screenshot(s) from Apple Health:\n"
        "— summary screen (phases with duration) — date + phases\n"
        "— interval screen for any phase (Core/Deep/REM/Awake) — shows start/end times\n\n"
        "Or enter time manually: 'start 23:10 end 6:45'\n\n"
        "/done — save."
    )


@router.message(Command("done"))
async def cmd_done(message: Message, db: asyncpg.Pool) -> None:
    user_id = str(message.from_user.id)
    async with _get_user_lock(user_id):
        # --- Save sleep data ---
        if _user_state.get(user_id) == "sleep":
            buf = _sleep_buffer.pop(user_id, {})
            _user_state.pop(user_id, None)
            if not buf:
                await message.answer("No data. Send screenshots or enter sleep time.")
                return
            await _save_sleep(message, db, user_id, buf)
            return

        # --- Save body metrics ---
        if _user_state.get(user_id) == "weight":
            buf = _weight_buffer.pop(user_id, {})
            _user_state.pop(user_id, None)
            if not buf:
                await message.answer("No data. Send scale photo or enter measurements.")
                return
            await _save_body_metrics(message, db, user_id, buf)
            return

        if _user_state.get(user_id) != "food" or not _food_buffer.get(user_id):
            await message.answer("Nothing to save. Use /food to start.")
            return

        buf = _food_buffer.pop(user_id)
        _user_state.pop(user_id, None)

        # Multiple entries → ask how to interpret
        if len(buf) > 1:
            lines = []
            for i, entry in enumerate(buf, 1):
                meals_preview = ", ".join(m["name"] for m in (entry.get("meals") or [])[:2])
                kcal = entry.get("calories") or 0
                lines.append(f"• Screenshot {i}: {float(kcal):.0f} kcal" + (f" ({meals_preview})" if meals_preview else ""))
            summary = "\n".join(lines)
            _food_confirm[user_id] = buf
            _user_state[user_id] = "food_confirm"
            await message.answer(
                f"Detected multiple entries:\n{summary}\n\n"
                "How to save?\n"
                "— type **meals** — separate meals, sum them up\n"
                "— type **total** — these are screenshots of one day, take the maximum"
            )
            return

        await _save_nutrition(message, db, user_id, buf)


@router.message(Command("report"))
async def cmd_report(message: Message, db: asyncpg.Pool) -> None:
    user_id = str(message.from_user.id)
    try:
        reply, label = await _run_with_status(message, run_analyst(db, user_id, REPORT_INTENT, return_model=True))
    except Exception as e:
        if "rate_limit" in str(e).lower() or "RateLimitError" in type(e).__name__:
            await message.answer("Anthropic rate limit exceeded. Wait 1 minute and retry.")
        elif "timeout" in str(e).lower() or "Timeout" in type(e).__name__:
            await message.answer("Request timed out — API did not respond in 90 seconds. Try again.")
        else:
            log.exception("run_analyst (report) failed: %s", e)
            await message.answer(f"Agent error: {e}")
        return
    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        session_id = await _get_or_create_session(conn, user_id)
        await conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ($1, 'user', $2)",
            session_id, "[REPORT]",
        )
        await conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ($1, 'assistant', $2)",
            session_id, reply,
        )
    await _send_long(message, f"{label}:\n\n{reply}")


@router.message(Command("plateau"))
async def cmd_plateau(message: Message, db: asyncpg.Pool) -> None:
    user_id = str(message.from_user.id)
    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        weight_history = await _get_weight_history(conn, user_id, days=30)
        nutrition = await _get_nutrition_range(conn, user_id, date.today() - timedelta(days=14), date.today())
        health = await _get_last_health(conn, user_id)
        system = await _build_system(conn, user_id)

    if not weight_history:
        await message.answer("No weight data. Use /weight to start tracking.")
        return

    plateau = _detect_plateau(weight_history)
    if not plateau:
        weights = [r["weight"] for r in weight_history if r["weight"] is not None]
        if weights:
            await message.answer(
                f"No plateau detected — weight changes enough.\n"
                f"Last value: {float(weights[-1]):.1f} kg."
            )
        else:
            await message.answer("No weight data in the last 30 days.")
        return

    prompt = (
        f"The user has been in a weight plateau for {plateau['days']} days "
        f"({plateau['start_date'].strftime('%d.%m')} — {plateau['end_date'].strftime('%d.%m')}). "
        f"Weight stays in the range {plateau['min']:.1f}—{plateau['max']:.1f} kg "
        f"(average {plateau['avg']:.1f} kg).\n"
        f"Weight history for 30 days: {weight_history}\n"
        f"Nutrition for 2 weeks: {nutrition if nutrition else 'no data'}\n"
        f"Latest health metrics: {health if health else 'no data'}\n\n"
        "Analyze the plateau as a partner:\n"
        "1. Look at correlations — caloric intake, activity, HRV\n"
        "2. If body measurements available — show recomposition "
        "(weight holds, measurements drop — that's progress)\n"
        "3. Consider the user's health profile context — "
        "a 3–4 week plateau can be normal, weight may drop suddenly\n"
        "4. Do not amplify anxiety, explain the physiology\n"
        "5. Give one concrete next step if there is one"
    )
    try:
        reply, label = await ask_model(prompt, system=system, model=settings.agent_model, max_tokens=1500, return_model=True)
    except Exception as e:
        log.exception("ask_model failed: %s", e)
        await message.answer(f"LLM error: {e}")
        return
    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        session_id = await _get_or_create_session(conn, user_id)
        await conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ($1, 'user', $2)",
            session_id, "[PLATEAU]",
        )
        await conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ($1, 'assistant', $2)",
            session_id, reply,
        )
    await _send_long(message, f"{label}:\n\n{reply}")


@router.message(Command("fix"))
async def cmd_fix(message: Message, db: asyncpg.Pool) -> None:
    user_id = str(message.from_user.id)
    async with _get_user_lock(user_id):
        async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
            row = await conn.fetchrow(
                """
                SELECT logged_date, calories, protein, fat, carbs
                FROM nutrition_logs WHERE user_id = $1
                ORDER BY logged_date DESC LIMIT 1
                """,
                user_id,
            )
        if not row:
            await message.answer("No saved nutrition logs.")
            return
        _fix_date[user_id] = row["logged_date"]
        _user_state[user_id] = "fix"
        await message.answer(
            f"Last entry — {row['logged_date'].strftime('%d.%m')}:\n"
            f"Calories: {row['calories'] or 0:.0f} kcal\n"
            f"Protein: {row['protein'] or 0:.0f} g  "
            f"Fat: {row['fat'] or 0:.0f} g  "
            f"Carbs: {row['carbs'] or 0:.0f} g\n\n"
            "What to fix? Write, for example:\n"
            "calories 1200\n"
            "protein 50, carbs 180"
        )


@router.message(Command("ask"))
async def cmd_ask(message: Message, db: asyncpg.Pool) -> None:
    user_id = str(message.from_user.id)
    # Text after command: "/ask why ringing in my head" → "why ringing in my head"
    intent = (message.text or "").removeprefix("/ask").strip()
    if not intent:
        _user_state[user_id] = "ask"
        await message.answer("Ask away 👀")
        return
    agent_intent = (
        f"User question: {intent}\n\n"
        f"You must use tools to retrieve real data from the database "
        f"and answer based on actual numbers. Do not answer from general knowledge."
    )
    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        session_id = await _get_or_create_session(conn, user_id)
        history = await _get_agent_history(conn, session_id, query=intent)
    try:
        reply, label = await _run_with_status(
            message,
            run_analyst(db, user_id, agent_intent, force_tools=True, system=ASK_SYSTEM, history=history, return_model=True, tools=select_tools(intent)),
        )
    except Exception as e:
        if "rate_limit" in str(e).lower() or "RateLimitError" in type(e).__name__:
            await message.answer("Anthropic rate limit exceeded. Wait 1 minute and retry.")
        elif "timeout" in str(e).lower() or "Timeout" in type(e).__name__:
            await message.answer("Request timed out — API did not respond in 90 seconds. Try again.")
        else:
            log.exception("run_analyst (ask) failed: %s", e)
            await message.answer(f"Agent error: {e}")
        return
    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        await conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ($1, 'user', $2)",
            session_id, f"[ASK] {intent}",
        )
        await conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ($1, 'assistant', $2)",
            session_id, reply,
        )
    await _send_long(message, f"{label}:\n\n{reply}")


@router.message(Command("scope"))
async def cmd_scope(message: Message, db: asyncpg.Pool) -> None:
    user_id = str(message.from_user.id)
    try:
        reply, label = await _run_with_status(message, run_analyst(db, user_id, SCOPE_INTENT, return_model=True))
    except Exception as e:
        if "rate_limit" in str(e).lower() or "RateLimitError" in type(e).__name__:
            await message.answer("Anthropic rate limit exceeded. Wait 1 minute and retry.")
        elif "timeout" in str(e).lower() or "Timeout" in type(e).__name__:
            await message.answer("Request timed out — API did not respond in 90 seconds. Try again.")
        else:
            log.exception("run_analyst (scope) failed: %s", e)
            await message.answer(f"Agent error: {e}")
        return
    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        session_id = await _get_or_create_session(conn, user_id)
        await conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ($1, 'user', $2)",
            session_id, "[SCOPE]",
        )
        await conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ($1, 'assistant', $2)",
            session_id, reply,
        )
    await _send_long(message, f"{label}:\n\n{reply}")


@router.message(Command("memory"))
async def cmd_memory(message: Message, db: asyncpg.Pool) -> None:
    user_id = str(message.from_user.id)
    async with _get_user_lock(user_id):
        async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
            rows = await conn.fetch(
                """
                SELECT id, pattern_text, confirmations, first_seen
                FROM pending_insights
                WHERE user_id = $1 AND status = 'pending'
                ORDER BY confirmations DESC
                """,
                user_id,
            )
        if not rows:
            await message.answer("No patterns for verification.")
            return
        ids = [r["id"] for r in rows]
        _memory_queue[user_id] = ids
        _user_state[user_id] = "memory"
        await _show_memory_pattern(message, db, user_id, rows)


async def _show_memory_pattern(message: Message, db: asyncpg.Pool, user_id: str, rows) -> None:
    """Shows the first pattern from the queue."""
    queue = _memory_queue.get(user_id, [])
    if not queue:
        _user_state.pop(user_id, None)
        await message.answer("Done ✓ All patterns processed.")
        return
    current_id = queue[0]
    # Find record among rows or re-query
    row = next((r for r in rows if r["id"] == current_id), None)
    if row is None:
        async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
            row = await conn.fetchrow(
                "SELECT id, pattern_text, confirmations, first_seen FROM pending_insights WHERE id = $1",
                current_id,
            )
    if not row:
        _memory_queue[user_id].pop(0)
        await _show_memory_pattern(message, db, user_id, [])
        return
    remaining = len(_memory_queue.get(user_id, []))
    first_seen = row["first_seen"].strftime("%d.%m") if row["first_seen"] else "?"
    await message.answer(
        f"🧠 Pattern (remaining: {remaining}):\n"
        f"«{row['pattern_text']}»\n"
        f"Seen {row['confirmations']} times (first seen {first_seen})\n\n"
        "'yes' — accept, 'no' — reject"
    )


@router.message()
async def text_handler(message: Message, db: asyncpg.Pool) -> None:
    user_id = str(message.from_user.id)
    if not _is_allowed(user_id):
        await _deny(message)
        return
    async with _get_user_lock(user_id):
        try:
            await _handle_text(message, db, user_id)
        except asyncio.TimeoutError:
            await message.answer("Service temporarily unavailable. Try in a minute.")


async def _handle_text(message: Message, db: asyncpg.Pool, user_id: str) -> None:
    # --- Document in /lab mode ---
    if message.document and _user_state.get(user_id) == "lab":
        filename = message.document.file_name or "document.pdf"
        try:
            buf = BytesIO()
            await message.bot.download(message.document, destination=buf)
            summary = await _run_with_status(
                message,
                process_medical_document(db, user_id, buf.getvalue(), None, filename),
                phrases=_DOC_PHRASES,
            )
            await message.answer(f"Saved: {summary}")
        except Exception as e:
            log.exception("lab document processing failed for %s: %s", filename, e)
            await message.answer(f"Error processing {filename}: {e}")
        return

    # --- Document: auto-index into knowledge base ---
    if message.document:
        filename = message.document.file_name or ""
        if filename.lower().endswith((".pdf", ".txt", ".md")):
            tmp_path = f"/tmp/{filename}"
            try:
                await message.bot.download(message.document, destination=tmp_path)
                await ingest_file(db, tmp_path, title=filename)
                await message.answer(f"📚 {filename} — added to knowledge base")
            except Exception as e:
                log.exception("kb ingest failed for %s: %s", filename, e)
                await message.answer(f"Indexing error {filename}: {e}")
        else:
            await message.answer("Supported formats: PDF, TXT, MD.")
        return

    # --- Photo in /lab mode ---
    if message.photo and _user_state.get(user_id) == "lab":
        photo = message.photo[-1]
        try:
            buf = BytesIO()
            await message.bot.download(photo, destination=buf)
            summary = await _run_with_status(
                message,
                process_medical_document(db, user_id, buf.getvalue(), "image/jpeg", "photo.jpg"),
                phrases=_DOC_PHRASES,
            )
            await message.answer(f"Saved: {summary}")
        except Exception as e:
            log.exception("lab photo processing failed: %s", e)
            await message.answer(f"Error processing photo: {e}")
        return

    # --- Photo in /food mode ---
    if message.photo and _user_state.get(user_id) == "food":
        photo = message.photo[-1]  # highest resolution
        try:
            buf = BytesIO()
            await message.bot.download(photo, destination=buf)
            result = await analyze_nutrition_image(buf.getvalue(), "image/jpeg")
            _food_buffer.setdefault(user_id, []).append(result)
            meals_preview = ", ".join(m["name"] for m in (result.get("meals") or [])[:3])
            await message.answer(
                f"Logged: {result.get('calories', 0):.0f} kcal"
                + (f" ({meals_preview})" if meals_preview else "")
                + "\n\nSend more or /done to save."
            )
        except Exception as e:
            log.exception("analyze_nutrition_image failed: %s", e)
            await message.answer(f"Could not recognize photo: {e}")
        return

    # --- Photo in /weight mode ---
    if message.photo and _user_state.get(user_id) == "weight":
        photo = message.photo[-1]
        try:
            buf = BytesIO()
            await message.bot.download(photo, destination=buf)
            result = await analyze_body_metrics_image(buf.getvalue(), "image/jpeg")
            clean = {k: v for k, v in result.items() if v is not None}

            # Year validation: scales show date without year → vision LLM may pick wrong year
            if "date" in clean:
                try:
                    extracted = date.fromisoformat(clean["date"])
                    today = date.today()
                    if extracted.year < today.year:
                        fixed = extracted.replace(year=today.year)
                        log.warning("date year corrected: %s → %s", clean["date"], fixed)
                        clean["date"] = fixed.isoformat()
                except (ValueError, TypeError):
                    clean.pop("date", None)

            # If buffer already has data with different date — save previous
            buf_date = _weight_buffer.get(user_id, {}).get("date")
            new_date = clean.get("date")
            if buf_date and new_date and buf_date != new_date and _weight_buffer.get(user_id):
                await _save_body_metrics(message, db, user_id, dict(_weight_buffer[user_id]))
                _weight_buffer[user_id] = {}

            _weight_buffer.setdefault(user_id, {}).update(clean)
            _weight_buffer[user_id]["_source"] = "scale_photo"

            parts = []
            if result.get("date"):
                parts.append(f"Date: {result['date']}")
            if result.get("weight"):
                parts.append(f"Weight: {result['weight']} kg")
            if result.get("body_fat_pct"):
                parts.append(f"Body fat: {result['body_fat_pct']}%")
            if result.get("muscle_kg"):
                parts.append(f"Muscle: {result['muscle_kg']} kg")
            if result.get("water_pct"):
                parts.append(f"Water: {result['water_pct']}%")
            if result.get("visceral_fat"):
                parts.append(f"Visceral fat: {result['visceral_fat']}")
            if result.get("bmi"):
                parts.append(f"BMI: {result['bmi']}")
            summary = "\n".join(parts) if parts else "no data found"
            await message.answer(
                f"Recognized:\n{summary}\n\n"
                "Enter measurements (arms, hips, waist...) or /done to save."
            )
        except Exception as e:
            log.exception("analyze_body_metrics_image failed: %s", e)
            await message.answer(f"Could not recognize photo: {e}")
        return

    # --- Photo in /sleep mode ---
    if message.photo and _user_state.get(user_id) == "sleep":
        photo = message.photo[-1]
        try:
            img_buf = BytesIO()
            await message.bot.download(photo, destination=img_buf)
            result = await analyze_sleep_image(img_buf.getvalue(), "image/jpeg")
            clean = {k: v for k, v in result.items() if v is not None}
            buf = _sleep_buffer.get(user_id, {})
            # If buffer already has different date — auto-save and start new buffer
            if buf and clean.get("sleep_date") and buf.get("sleep_date") and buf["sleep_date"] != clean["sleep_date"]:
                await _save_sleep(message, db, user_id, buf)
                _sleep_buffer[user_id] = {}
            _sleep_buffer.setdefault(user_id, {}).update(clean)
            buf = _sleep_buffer[user_id]
            parts = []
            if buf.get("sleep_date"):
                parts.append(f"Date: {buf['sleep_date']}")
            if buf.get("bedtime_start"):
                parts.append(f"Start: {buf['bedtime_start']}")
            if buf.get("bedtime_end"):
                parts.append(f"End: {buf['bedtime_end']}")
            if buf.get("deep_min"):
                parts.append(f"Deep: {buf['deep_min']} min")
            if buf.get("rem_min"):
                parts.append(f"REM: {buf['rem_min']} min")
            if buf.get("core_min"):
                parts.append(f"Core: {buf['core_min']} min")
            if buf.get("awake_min"):
                parts.append(f"Awake: {buf['awake_min']} min")
            missing = []
            if not buf.get("bedtime_start"):
                missing.append("sleep start")
            if not buf.get("bedtime_end"):
                missing.append("sleep end")
            hint = (f"\n\nMissing: {', '.join(missing)}. Enter manually (e.g. 'start 23:10 end 6:45')." if missing else "")
            await message.answer(
                "Recognized:\n" + "\n".join(parts) + hint +
                "\n\nSend another screenshot or /done to save."
            )
        except Exception as e:
            log.exception("analyze_sleep_image failed: %s", e)
            await message.answer(f"Could not recognize screenshot: {e}")
        return

    if message.voice:
        try:
            buf = BytesIO()
            await message.bot.download(message.voice, destination=buf)
            text = await transcribe_audio(buf.getvalue())
            await message.answer(f"🎙 _{text}_", parse_mode="Markdown")
        except Exception as e:
            log.exception("transcribe failed: %s", e)
            await message.answer(f"Could not transcribe voice message: {e}")
            return
    elif message.text:
        text = message.text
    else:
        return

    mode = _user_state.get(user_id)

    # --- URL: auto-index into knowledge base (only without active mode) ---
    if not mode and text.strip().startswith(("http://", "https://")):
        url = text.strip().split()[0]  # take only URL, without trailing text
        await message.answer("⏳ Loading...")
        try:
            doc_title = await ingest_url(db, url)
            await message.answer(f"📚 {doc_title} — added to knowledge base")
        except Exception as e:
            log.exception("ingest_url failed for %s: %s", url, e)
            await message.answer(f"Could not load URL: {e}")
        return

    # --- Text input in /sleep mode ---
    if mode == "sleep":
        buf = _sleep_buffer.setdefault(user_id, {})
        t = text.lower()
        # Parse time: "start 23:10 end 6:45" or just numbers
        start_m = re.search(r"(?:начало|старт|лег(?:ла)?|лёг(?:ла)?)\s+(\d{1,2}:\d{2})", t)
        end_m = re.search(r"(?:конец|встал(?:а)?|проснул(?:ась)?|финал|конец|окончание)\s+(\d{1,2}:\d{2})", t)
        # Also look for two times separated by dash: "23:10 - 6:45"
        range_m = re.search(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})", text)
        if start_m:
            buf["bedtime_start"] = start_m.group(1)
        if end_m:
            buf["bedtime_end"] = end_m.group(1)
        if range_m and not start_m and not end_m:
            buf["bedtime_start"] = range_m.group(1)
            buf["bedtime_end"] = range_m.group(2)
        parts = []
        if buf.get("sleep_date"):
            parts.append(f"Date: {buf['sleep_date']}")
        if buf.get("bedtime_start"):
            parts.append(f"Start: {buf['bedtime_start']}")
        if buf.get("bedtime_end"):
            parts.append(f"End: {buf['bedtime_end']}")
        if buf.get("deep_min"):
            parts.append(f"Deep: {buf['deep_min']} min")
        if buf.get("rem_min"):
            parts.append(f"REM: {buf['rem_min']} min")
        if buf.get("core_min"):
            parts.append(f"Core: {buf['core_min']} min")
        if buf.get("awake_min"):
            parts.append(f"Awake: {buf['awake_min']} min")
        if parts:
            await message.answer("Current data:\n" + "\n".join(parts) + "\n\n/done — save.")
        else:
            await message.answer("Didn't understand. Write time: 'start 23:10 end 6:45' or '23:10 - 6:45'")
        return

    # --- Confirmation of multiple screenshots ---
    if mode == "food_confirm":
        buf = _food_confirm.pop(user_id, [])
        _user_state.pop(user_id, None)
        if not buf:
            await message.answer("No data to save.")
            return
        choice = text.lower()
        if any(w in choice for w in ("total", "max", "last", "one", "итог", "максим", "последн", "один")):
            # Take entry with maximum calories
            best = max(buf, key=lambda e: float(e.get("calories") or 0))
            await _save_nutrition(message, db, user_id, [best])
        else:
            # "meals" or any other response → sum up
            await _save_nutrition(message, db, user_id, buf)
        return

    # --- Pattern verification /memory ---
    if mode == "memory":
        queue = _memory_queue.get(user_id, [])
        if not queue:
            _user_state.pop(user_id, None)
            await message.answer("Done ✓ All patterns processed.")
            return
        current_id = queue.pop(0)
        choice = text.strip().lower()
        accepted = choice in ("yes", "да", "+", "д", "y")
        rejected = choice in ("no", "нет", "-", "н", "n")
        if accepted:
            async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT pattern_text FROM pending_insights WHERE id = $1 AND user_id = $2",
                        current_id, user_id,
                    )
                    await conn.execute(
                        "UPDATE pending_insights SET status = 'confirmed' WHERE id = $1 AND user_id = $2",
                        current_id, user_id,
                    )
                    if row:
                        await conn.execute(
                            """
                            INSERT INTO memory_insights (user_id, insight_text, confirmed_at)
                            VALUES ($1, $2, $3)
                            """,
                            user_id, row["pattern_text"], date.today(),
                        )
        elif rejected:
            async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
                await conn.execute(
                    "UPDATE pending_insights SET status = 'rejected' WHERE id = $1 AND user_id = $2",
                    current_id, user_id,
                )
        else:
            # Unclear answer — return to front of queue
            queue.insert(0, current_id)
            await message.answer("Reply with 'yes' or 'no'.")
            return
        # Show next pattern
        if queue:
            _memory_queue[user_id] = queue
            await _show_memory_pattern(message, db, user_id, [])
        else:
            _memory_queue.pop(user_id, None)
            _user_state.pop(user_id, None)
            await message.answer("Done ✓ All patterns processed.")
        return

    # --- Agent question (/ask mode without argument) ---
    if mode == "ask":
        _user_state.pop(user_id, None)
        agent_intent = (
            f"User question: {text}\n\n"
            f"You must use tools to retrieve real data from the database "
            f"and answer based on actual numbers. Do not answer from general knowledge."
        )
        async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
            session_id = await _get_or_create_session(conn, user_id)
            history = await _get_agent_history(conn, session_id, query=text)
        try:
            reply, label = await _run_with_status(
                message,
                run_analyst(db, user_id, agent_intent, force_tools=True, system=ASK_SYSTEM, history=history, return_model=True, tools=select_tools(text)),
            )
        except Exception as e:
            log.exception("run_analyst (ask-state) failed: %s", e)
            await message.answer(f"Agent error: {e}")
            return
        async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
            await conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES ($1, 'user', $2)",
                session_id, f"[ASK] {text}",
            )
            await conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES ($1, 'assistant', $2)",
                session_id, reply,
            )
        await _send_long(message, f"{label}:\n\n{reply}")
        return

    # --- Edit entry /fix ---
    if mode == "fix":
        fix_d = _fix_date.pop(user_id, None)
        _user_state.pop(user_id, None)
        if not fix_d:
            await message.answer("No active entry to edit.")
            return
        try:
            updates = await parse_nutrition_correction(text)
        except Exception as e:
            log.exception("parse_nutrition_correction failed: %s", e)
            await message.answer(f"Could not parse correction: {e}")
            return
        if not updates:
            await message.answer("Didn't understand what to fix. Try: 'calories 1200' or 'protein 50'.")
            return
        # Build SET dynamically only for provided fields
        field_map = {"calories": "calories", "protein": "protein", "fat": "fat", "carbs": "carbs"}
        set_parts = []
        values = []
        for key, col in field_map.items():
            if key in updates and updates[key] is not None:
                values.append(float(updates[key]))
                set_parts.append(f"{col} = ${len(values)}")
        if not set_parts:
            await message.answer("No fields found to update.")
            return
        values.extend([user_id, fix_d])
        async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
            await conn.execute(
                f"UPDATE nutrition_logs SET {', '.join(set_parts)} "
                f"WHERE user_id = ${len(values) - 1} AND logged_date = ${len(values)}",
                *values,
            )
        changed = ", ".join(
            f"{k}: {updates[k]:.0f}" for k in ("calories", "protein", "fat", "carbs") if k in updates and updates[k] is not None
        )
        await message.answer(f"Updated entry for {fix_d.strftime('%d.%m')}: {changed}")
        return

    # --- Text in /food mode ---
    if mode == "food":
        try:
            kind = await classify_food_input(text)
        except Exception as e:
            log.exception("classify_food_input failed: %s", e)
            await message.answer(f"Classification error: {e}")
            return

        if kind == "report":
            try:
                date_from, date_to = await parse_date_range(text)
            except Exception as e:
                log.exception("parse_date_range failed: %s", e)
                await message.answer(f"Could not parse period: {e}")
                return
            async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
                nutrition_rows = await _get_nutrition_range(conn, user_id, date_from, date_to)
            if not nutrition_rows:
                await message.answer(f"No nutrition data for {date_from} — {date_to}.")
                return
            prompt = (
                f"Analyze nutrition for the period {date_from} — {date_to}.\n"
                f"Data by day: {nutrition_rows}\n"
                "Evaluate average macros, trends, provide recommendations."
            )
            try:
                reply, label = await ask_model(prompt, system=SYSTEM_PROMPT, model=settings.agent_model, return_model=True)
            except Exception as e:
                log.exception("ask_model failed: %s", e)
                await message.answer(f"LLM error: {e}")
                return
            await _send_long(message, f"{label}:\n\n{reply}")
        else:
            # log — record food
            try:
                result = await analyze_nutrition_text(text)
                _food_buffer.setdefault(user_id, []).append(result)
                meals_preview = ", ".join(m["name"] for m in (result.get("meals") or [])[:3])
                await message.answer(
                    f"Logged: {result.get('calories', 0):.0f} kcal"
                    + (f" ({meals_preview})" if meals_preview else "")
                    + "\n\nSend more or /done to save."
                )
            except Exception as e:
                log.exception("analyze_nutrition_text failed: %s", e)
                await message.answer(f"Could not parse food: {e}")
        return

    # --- Text in /weight mode ---
    if mode == "weight":
        try:
            parsed = await parse_body_measurements(text)
        except Exception as e:
            log.exception("parse_body_measurements failed: %s", e)
            await message.answer(f"Could not parse measurements: {e}")
            return
        if not parsed:
            await message.answer("Didn't understand measurements. Try: 'arms 27, hips 51, waist 74'.")
            return
        _weight_buffer.setdefault(user_id, {}).update(parsed)
        _weight_buffer[user_id].setdefault("_source", "text")
        fields_summary = ", ".join(
            f"{k.replace('_cm', '')} {v}" for k, v in parsed.items()
        )
        await message.answer(
            f"Logged: {fields_summary}\n\n"
            "Send scale screenshot or /done to save."
        )
        return

    if mode == "mind":
        tagged = f"[MIND] {text}"
        prompt = (
            f"User's thought:\n{text}\n\n"
            "Analyze in layers:\n"
            "1. WHAT IS SAID EXPLICITLY — what this thought signals on the surface, what is central\n"
            "2. CONTRADICTIONS AND TENSIONS — where inside the thought there are inconsistencies, duality, incompleteness\n"
            "3. WHAT IS HIDDEN DEEPER — what needs, fears, schemas, or beliefs underlie this\n"
            "4. PATTERN — is there a connection to previous entries, a recurring theme\n"
            "5. WHAT THIS THOUGHT MIGHT MEAN — one clear conclusion or question worth sitting with\n"
            "Apply CBT, schema approach, mentalization — no jargon, natural language."
        )
    elif mode == "decision":
        tagged = f"[DECISION] {text}"
        prompt = f"Help analyze this as a decision situation and suggest a possible direction: {text}"
    else:
        tagged = text
        prompt = text

    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        session_id = await _get_or_create_session(conn, user_id)
        history = await _get_session_history(conn, session_id)
        system = await _build_system(conn, user_id)
        if mode == "mind":
            past = await _get_past_mind_thoughts(conn, user_id)
            if past:
                system += (
                    "\n\nUser's past thoughts (for context):\n"
                    + "\n".join(f"- {t}" for t in past)
                )

        msg_id = await conn.fetchval(
            "INSERT INTO messages (session_id, role, content) VALUES ($1, 'user', $2) RETURNING id",
            session_id,
            tagged,
        )
        asyncio.create_task(_embed_and_store(db, msg_id, user_id, tagged))

        # Plain text without mode — route through agent if it is a data query
        if not mode:
            agent_tools = select_tools(text)
            # If select_tools found anything beyond profile/memory — it is a data query
            data_tools = [t for t in agent_tools if t["function"]["name"] not in ("get_user_profile", "get_memory_insights")]
            if data_tools:
                agent_intent = (
                    f"User question: {text}\n\n"
                    "Use tools to retrieve real data and answer with facts."
                )
                agent_history = await _get_agent_history(conn, session_id, query=text)
                try:
                    reply, label = await _run_with_status(
                        message,
                        run_analyst(db, user_id, agent_intent, force_tools=True, system=ASK_SYSTEM, history=agent_history, return_model=True, tools=agent_tools),
                    )
                except Exception as e:
                    log.exception("run_analyst (text) failed: %s", e)
                    await message.answer(f"Agent error: {e}")
                    return
                await conn.execute(
                    "INSERT INTO messages (session_id, role, content) VALUES ($1, 'assistant', $2)",
                    session_id, reply,
                )
                await _send_long(message, f"{label}:\n\n{reply}")
                return

        try:
            reply, label = await _run_with_status(
                message,
                ask_model(prompt, system=system, history=history, model=settings.agent_model, max_tokens=4000, return_model=True),
            )
        except Exception as e:
            log.exception("ask_model failed: %s", e)
            _user_state.pop(user_id, None)
            await message.answer(f"LLM error: {e}")
            return
        await conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ($1, 'assistant', $2)",
            session_id,
            reply,
        )

    if mode in ("mind", "decision"):
        _user_state.pop(user_id, None)
        reply += "\n\n/mind  /decision  /food  /report  /scope"

    await _send_long(message, f"{label}:\n\n{reply}")


async def _save_nutrition(message: Message, db: asyncpg.Pool, user_id: str, buf: list[dict]) -> None:
    """Summarizes the buffer and saves to nutrition_logs."""
    total = {"calories": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0}
    all_meals: list[dict] = []
    log_date = date.today()
    for entry in buf:
        for key in total:
            total[key] += float(entry.get(key) or 0)
        all_meals.extend(entry.get("meals") or [])
        if entry.get("date"):
            try:
                log_date = date.fromisoformat(entry["date"])
            except ValueError:
                pass

    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        await conn.execute(
            """
            INSERT INTO nutrition_logs (user_id, logged_date, calories, protein, fat, carbs, meals_json, source)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'bot')
            ON CONFLICT (user_id, logged_date) DO UPDATE SET
                calories   = EXCLUDED.calories,
                protein    = EXCLUDED.protein,
                fat        = EXCLUDED.fat,
                carbs      = EXCLUDED.carbs,
                meals_json = EXCLUDED.meals_json
            """,
            user_id,
            log_date,
            total["calories"],
            total["protein"],
            total["fat"],
            total["carbs"],
            json.dumps(all_meals, ensure_ascii=False),
        )

    await message.answer(
        f"Saved for {log_date.strftime('%d.%m')}:\n"
        f"Calories: {total['calories']:.0f} kcal\n"
        f"Protein: {total['protein']:.0f} g  Fat: {total['fat']:.0f} g  Carbs: {total['carbs']:.0f} g"
    )


# --- helpers ---

async def _embed_and_store(db: asyncpg.Pool, msg_id: int, user_id: str, content: str) -> None:
    """Background task: creates message embedding and saves to message_embeddings."""
    if not content.strip():
        return
    try:
        vec = await embed_text(content)
        vec_str = "[" + ",".join(str(x) for x in vec) + "]"
        async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
            await conn.execute(
                """
                INSERT INTO message_embeddings (message_id, user_id, content, embedding)
                VALUES ($1, $2, $3, $4::vector)
                ON CONFLICT DO NOTHING
                """,
                msg_id, user_id, content, vec_str,
            )
    except Exception as e:
        log.warning("_embed_and_store failed for msg_id=%d: %s", msg_id, e)


async def _get_or_create_session(conn: asyncpg.Connection, user_id: str) -> int:
    row = await conn.fetchrow(
        """
        SELECT id FROM dialog_sessions
        WHERE user_id = $1 AND ended_at IS NULL
        ORDER BY started_at DESC LIMIT 1
        """,
        user_id,
    )
    if row:
        return row["id"]
    row = await conn.fetchrow(
        "INSERT INTO dialog_sessions (user_id) VALUES ($1) RETURNING id",
        user_id,
    )
    return row["id"]


async def _get_session_history(conn: asyncpg.Connection, session_id: int) -> list[dict]:
    """Last 6 messages of the current session (3 user/assistant pairs) for context."""
    rows = await conn.fetch(
        """
        SELECT role, content FROM messages
        WHERE session_id = $1
        ORDER BY created_at DESC LIMIT 6
        """,
        session_id,
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


_FOLLOWUP_SIGNALS = (
    "these", "this", "that", "then", "there",
    "above", "below", "and now", "continue", "more details",
    "tell me more", "more", "and what", "why then",
)


def _needs_history(query: str) -> bool:
    """True if the query looks like a follow-up and needs context of previous messages."""
    q = query.lower()
    return any(signal in q for signal in _FOLLOWUP_SIGNALS)


async def _get_agent_history(conn: asyncpg.Connection, session_id: int, query: str = "") -> list[dict]:
    """Session history for the agent.

    If the query is standalone (not follow-up), history is not needed → 0 tokens.
    If follow-up — last 4 messages (2 pairs).
    """
    if not _needs_history(query):
        return []
    rows = await conn.fetch(
        """
        SELECT role, content FROM messages
        WHERE session_id = $1
        ORDER BY created_at DESC LIMIT 4
        """,
        session_id,
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def _get_past_mind_thoughts(conn: asyncpg.Connection, user_id: str) -> list[str]:
    """Last 5 [MIND] thoughts from history for /mind context."""
    rows = await conn.fetch(
        """
        SELECT m.content FROM messages m
        JOIN dialog_sessions s ON s.id = m.session_id
        WHERE s.user_id = $1 AND m.role = 'user' AND m.content LIKE '[MIND]%'
        ORDER BY m.created_at DESC LIMIT 5
        """,
        user_id,
    )
    return [r["content"].removeprefix("[MIND] ") for r in reversed(rows)]


async def _get_recent_thoughts(conn: asyncpg.Connection, user_id: str) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT m.content FROM messages m
        JOIN dialog_sessions s ON s.id = m.session_id
        WHERE s.user_id = $1 AND m.role = 'user'
        ORDER BY m.created_at DESC LIMIT 20
        """,
        user_id,
    )
    return [r["content"] for r in reversed(rows)]


async def _get_user_profile(conn: asyncpg.Connection, user_id: str) -> str | None:
    row = await conn.fetchrow(
        "SELECT profile_text FROM user_profile WHERE user_id = $1",
        user_id,
    )
    return row["profile_text"] if row else None


async def _build_system(conn: asyncpg.Connection, user_id: str) -> str:
    """SYSTEM_PROMPT + user profile from DB."""
    profile = await _get_user_profile(conn, user_id)
    if not profile:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + "\n\n## User Profile\n\n" + profile


async def _get_nutrition_range(
    conn: asyncpg.Connection, user_id: str, date_from: date, date_to: date
) -> list[dict]:
    """nutrition_logs entries for the period [date_from, date_to]."""
    rows = await conn.fetch(
        """
        SELECT logged_date, calories, protein, fat, carbs
        FROM nutrition_logs
        WHERE user_id = $1 AND logged_date BETWEEN $2 AND $3
        ORDER BY logged_date
        """,
        user_id,
        date_from,
        date_to,
    )
    return [dict(r) for r in rows]


async def _get_last_health(conn: asyncpg.Connection, user_id: str) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT recorded_date, hrv_ms, heart_rate, resting_hr, steps,
               active_kcal, distance_km, vo2max
        FROM health_metrics
        WHERE user_id = $1
        ORDER BY recorded_date DESC LIMIT 1
        """,
        user_id,
    )
    return dict(row) if row else None


async def _get_weight_history(conn: asyncpg.Connection, user_id: str, days: int = 30) -> list[dict]:
    """Last N days of body_metrics data."""
    cutoff = date.today() - timedelta(days=days)
    rows = await conn.fetch(
        """
        SELECT recorded_date, weight, body_fat_pct, muscle_kg, water_pct,
               visceral_fat, bmi, arms_cm, thighs_cm, waist_cm, hips_cm
        FROM body_metrics
        WHERE user_id = $1 AND recorded_date >= $2
        ORDER BY recorded_date
        """,
        user_id,
        cutoff,
    )
    return [dict(r) for r in rows]


def _detect_plateau(weight_history: list[dict]) -> dict | None:
    """Returns plateau info if weight is stable for 7+ days (range ≤ 1 kg), otherwise None."""
    weights = [
        (r["recorded_date"], float(r["weight"]))
        for r in weight_history
        if r["weight"] is not None
    ]
    if len(weights) < 3:
        return None
    span = (weights[-1][0] - weights[0][0]).days
    if span < 7:
        return None
    values = [w for _, w in weights]
    w_min, w_max = min(values), max(values)
    if (w_max - w_min) <= 1.0:
        return {
            "days": span,
            "count": len(weights),
            "min": w_min,
            "max": w_max,
            "avg": sum(values) / len(values),
            "start_date": weights[0][0],
            "end_date": weights[-1][0],
        }
    return None


async def _save_sleep(message: Message, db: asyncpg.Pool, user_id: str, data: dict) -> None:
    """Saves sleep data from buffer to sleep_sessions."""
    from datetime import datetime as _dt

    sleep_date_raw = data.get("sleep_date")
    from src.health.intake import parse_date as _parse_date
    try:
        sleep_date = _parse_date(sleep_date_raw) if sleep_date_raw else date.today()
    except Exception:
        sleep_date = date.today()

    # bedtime_start/end: strings "HH:MM" → datetime for the target day
    def _to_dt(time_str: str, base_date: date, next_day: bool = False):
        if not time_str:
            return None
        try:
            t = _dt.strptime(time_str.strip(), "%H:%M").time()
            d = base_date + timedelta(days=1) if next_day else base_date
            return _dt.combine(d, t)
        except ValueError:
            return None

    start_str = data.get("bedtime_start")
    end_str = data.get("bedtime_end")
    bedtime_start = _to_dt(start_str, sleep_date) if start_str else None
    if bedtime_start and end_str:
        # If end < start — end is next day (normal case: went to bed at 23:00, woke at 7:00)
        end_dt = _to_dt(end_str, sleep_date)
        if end_dt and end_dt <= bedtime_start:
            end_dt = _to_dt(end_str, sleep_date, next_day=True)
        bedtime_end = end_dt
    else:
        bedtime_end = _to_dt(end_str, sleep_date) if end_str else None

    intake_data = {
        "sleep_date": sleep_date.isoformat(),
        "bedtime_start": bedtime_start.isoformat() if bedtime_start else None,
        "bedtime_end": bedtime_end.isoformat() if bedtime_end else None,
        "deep_min": data.get("deep_min"),
        "rem_min": data.get("rem_min"),
        "core_min": data.get("core_min"),
        "awake_min": data.get("awake_min"),
        "in_bed_min": data.get("in_bed_min"),
    }
    # remove None to avoid overwriting existing data
    intake_data = {k: v for k, v in intake_data.items() if v is not None}

    await save_sleep_session(db, intake_data, user_id)

    parts = [f"Saved — {sleep_date}"]
    if bedtime_start:
        parts.append(f"Start: {start_str}")
    if bedtime_end:
        parts.append(f"End: {end_str}")
    if data.get("deep_min"):
        parts.append(f"Deep: {data['deep_min']} min")
    if data.get("rem_min"):
        parts.append(f"REM: {data['rem_min']} min")
    if data.get("core_min"):
        parts.append(f"Core: {data['core_min']} min")
    if data.get("awake_min"):
        parts.append(f"Awake: {data['awake_min']} min")
    total = sum(v for k, v in data.items() if k in ("deep_min", "rem_min", "core_min") and v)
    if total:
        h, m = divmod(total, 60)
        parts.append(f"Total sleep: {h}h {m}min")
    await message.answer("\n".join(parts))


async def _save_body_metrics(message: Message, db: asyncpg.Pool, user_id: str, data: dict) -> None:
    """Saves body metrics to body_metrics (upsert by date), shows dynamics."""
    source = data.pop("_source", "manual")

    # Date: from screenshot/text if recognized, otherwise today
    raw_date = data.pop("date", None)
    if raw_date:
        try:
            recorded_date = date.fromisoformat(str(raw_date))
        except ValueError:
            recorded_date = date.today()
    else:
        recorded_date = date.today()

    # body_metrics table fields (order matters only for readability)
    _BODY_FIELDS = [
        "weight", "body_fat_pct", "muscle_kg", "water_pct", "visceral_fat",
        "bone_mass_kg", "bmr_kcal", "bmi",
        "arms_cm", "thighs_cm", "neck_cm", "shin_cm", "waist_cm", "chest_cm", "hips_cm",
    ]
    _LABELS: dict[str, tuple[str, str]] = {
        "weight":       ("Weight",      "kg"),
        "body_fat_pct": ("Body fat",   "%"),
        "muscle_kg":    ("Muscle",     "kg"),
        "water_pct":    ("Water",      "%"),
        "visceral_fat": ("Visc. fat",  ""),
        "bone_mass_kg": ("Bone",       "kg"),
        "bmr_kcal":     ("BMR",        "kcal"),
        "bmi":          ("BMI",        ""),
        "arms_cm":      ("Arms",       "cm"),
        "thighs_cm":    ("Thighs",     "cm"),
        "neck_cm":      ("Neck",       "cm"),
        "shin_cm":      ("Shin",       "cm"),
        "waist_cm":     ("Waist",      "cm"),
        "chest_cm":     ("Chest",      "cm"),
        "hips_cm":      ("Hips",       "cm"),
    }

    # Only known fields with non-None values
    clean = {f: float(data[f]) for f in _BODY_FIELDS if f in data and data[f] is not None}
    if not clean:
        await message.answer("No data to save.")
        return

    field_names = list(clean.keys())
    field_values = [clean[f] for f in field_names]
    cols = ", ".join(["user_id", "recorded_date", "source"] + field_names)
    placeholders = ", ".join(f"${i + 1}" for i in range(3 + len(field_names)))
    updates = ", ".join([f"{f} = EXCLUDED.{f}" for f in field_names] + ["source = EXCLUDED.source"])

    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        await conn.execute(
            f"""
            INSERT INTO body_metrics ({cols})
            VALUES ({placeholders})
            ON CONFLICT (user_id, recorded_date) DO UPDATE SET {updates}
            """,
            user_id, recorded_date, source, *field_values,
        )
        prev = await conn.fetchrow(
            """
            SELECT * FROM body_metrics
            WHERE user_id = $1 AND recorded_date < $2
            ORDER BY recorded_date DESC LIMIT 1
            """,
            user_id, recorded_date,
        )

    lines = [f"Saved for {recorded_date.strftime('%d.%m')}:"]
    for field in _BODY_FIELDS:
        if field not in clean:
            continue
        name, unit = _LABELS[field]
        val = clean[field]
        suffix = f" {unit}" if unit else ""
        line = f"{name}: {val:.1f}{suffix}"
        if prev and prev[field] is not None:
            diff = val - float(prev[field])
            sign = "+" if diff > 0 else ""
            line += f" ({sign}{diff:.1f})"
        lines.append(line)

    if prev:
        lines.append(f"\n(in brackets — change vs {prev['recorded_date'].strftime('%d.%m')})")
    await message.answer("\n".join(lines))
