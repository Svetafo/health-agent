"""FastAPI — application entry point."""

import asyncio
import asyncpg
import logging
from contextlib import asynccontextmanager

from src.logging_setup import setup_logging
setup_logging()

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, Request, HTTPException, Header
from pathlib import Path

from src.config import settings
from src.bot.handlers import router
from src.bot.throttle import ThrottleMiddleware
from src.health.intake import save_health_metrics, save_sleep_session, save_workout_session
from src.pipeline import run_synthesizer, run_profiler
from src.pipeline.kb_ingest import ingest_file

log = logging.getLogger(__name__)

bot: Bot | None = None
dp: Dispatcher | None = None
db_pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot, dp, db_pool

    log.info("Starting %s app", settings.app_name)
    db_pool = await asyncpg.create_pool(settings.database_url)

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.message.middleware(ThrottleMiddleware())
    dp.include_router(router)
    dp["db"] = db_pool  # injected into handlers as the db parameter

    # Polling — works without HTTPS, until n8n webhook is set up
    polling_task = asyncio.create_task(dp.start_polling(bot))

    yield

    polling_task.cancel()
    await bot.session.close()
    await db_pool.close()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Receives a Telegram Update from n8n (for future webhook mode)."""
    if dp is None or bot is None:
        raise HTTPException(status_code=503, detail="Bot not ready")
    update_data = await request.json()
    update = Update.model_validate(update_data)
    await dp.feed_update(bot, update)
    return {"ok": True}


@app.post("/webhook/health")
async def health_webhook(request: Request):
    """Receives Apple Health data from n8n, normalizes and saves to DB."""
    if db_pool is None:
        raise HTTPException(status_code=503, detail="DB not ready")
    data = await request.json()
    # If n8n sent a wrapper {"body": {"": {metrics}}, "headers": ...} — unwrap it
    if "body" in data and isinstance(data["body"], dict) and data["body"]:
        data = data["body"]
    # Shortcuts wraps metrics in {"": {metrics}}
    if "" in data and isinstance(data[""], dict):
        data = data[""]
    # If date is not provided — use today
    if not data.get("date"):
        from datetime import date as _date
        data["date"] = _date.today().isoformat()
    try:
        await save_health_metrics(db_pool, data, settings.health_user_id)
        log.info("Health metrics saved: date=%s", data.get("date"))
    except ValueError as e:
        log.warning("Health metrics rejected: %s", e)
        raise HTTPException(status_code=422, detail=str(e))
    return {"status": "ok", "saved": True}


@app.post("/webhook/sleep")
async def sleep_webhook(request: Request):
    """Receives sleep data from n8n (Shortcuts → n8n → here), upsert into sleep_sessions."""
    if db_pool is None:
        raise HTTPException(status_code=503, detail="DB not ready")
    data = await request.json()
    if "body" in data and isinstance(data["body"], dict) and data["body"]:
        data = data["body"]
    try:
        await save_sleep_session(db_pool, data, settings.health_user_id)
        log.info("Sleep session saved: date=%s", data.get("sleep_date"))
    except Exception as e:
        log.warning("Sleep session rejected: %s", e)
        raise HTTPException(status_code=422, detail=str(e))
    return {"status": "ok"}


@app.post("/webhook/workout")
async def workout_webhook(request: Request):
    """Receives a single workout from n8n (Shortcuts → n8n → here), upsert into workout_sessions."""
    if db_pool is None:
        raise HTTPException(status_code=503, detail="DB not ready")
    data = await request.json()
    if "body" in data and isinstance(data["body"], dict) and data["body"]:
        data = data["body"]
    try:
        await save_workout_session(db_pool, data, settings.health_user_id)
        log.info("Workout saved: date=%s type=%s", data.get("workout_date"), data.get("workout_type"))
    except Exception as e:
        log.warning("Workout rejected: %s", e)
        raise HTTPException(status_code=422, detail=str(e))
    return {"status": "ok"}


_KNOWLEDGE_ALLOWED_DIRS = ["/app/knowledge", "/app/data"]


def _check_internal_key(x_api_key: str | None) -> None:
    """Checks the internal_api_key if it is set in settings."""
    expected = settings.internal_api_key
    if expected and x_api_key != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


@app.post("/pipeline/ingest-knowledge")
async def pipeline_ingest_knowledge(
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    """Indexes a document into the knowledge base. path — absolute path on the server."""
    _check_internal_key(x_api_key)
    if db_pool is None:
        raise HTTPException(status_code=503, detail="DB not ready")
    data = await request.json()
    path = data.get("path")
    if not path:
        raise HTTPException(status_code=422, detail="path is required")
    # Path traversal protection: only paths under /app/knowledge and /app/data are allowed
    resolved = str(Path(path).resolve())
    if not any(resolved.startswith(d) for d in _KNOWLEDGE_ALLOWED_DIRS):
        log.warning("ingest-knowledge: path not allowed: %s", resolved)
        raise HTTPException(status_code=403, detail=f"Path not allowed: {path}")
    title = data.get("title")
    try:
        n = await ingest_file(db_pool, resolved, title)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    except Exception as e:
        log.exception("ingest_file failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "chunks": n}


@app.post("/pipeline/profile")
async def pipeline_profile(x_api_key: str | None = Header(default=None)):
    """Updates factual data in user_profile based on the last 2 weeks of data."""
    _check_internal_key(x_api_key)
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Not ready")
    result = await run_profiler(db_pool, settings.health_user_id)
    return {"ok": True, "updated": result["updated"], "summary": result["summary"]}


@app.post("/pipeline/synthesize")
async def pipeline_synthesize(x_api_key: str | None = Header(default=None)):
    """Runs the Memory Synthesizer: finds patterns over 4 weeks, pushes to Telegram."""
    _check_internal_key(x_api_key)
    if db_pool is None or bot is None:
        raise HTTPException(status_code=503, detail="Not ready")
    result = await run_synthesizer(db_pool, settings.health_user_id)
    for _id, text in result["push_patterns"]:
        await bot.send_message(
            settings.health_user_id,
            f"🧠 Noticed a pattern:\n{text}\n\nUse /memory to confirm or reject.",
        )
    return {"ok": True, "new": result["new"], "confirmed": result["confirmed"]}
