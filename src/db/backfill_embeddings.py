"""Backfill embeddings for existing user messages.

Usage:
    docker exec -it bmindset-app-1 python -m src.db.backfill_embeddings

Idempotent: ON CONFLICT DO NOTHING — safe to run repeatedly.
Rate limit: 0.1s delay between requests to the OpenAI Embeddings API.
"""

import asyncio
import logging
import time

import asyncpg

from src.config import settings
from src.llm.client import embed_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


async def backfill() -> None:
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.id, s.user_id, m.content
            FROM messages m
            JOIN dialog_sessions s ON s.id = m.session_id
            WHERE m.role = 'user'
              AND NOT EXISTS (
                  SELECT 1 FROM message_embeddings e WHERE e.message_id = m.id
              )
            ORDER BY m.id
            """
        )

    log.info("Found %d messages without embeddings", len(rows))
    if not rows:
        log.info("Nothing to backfill.")
        await pool.close()
        return

    ok = skip = err = 0
    for i, row in enumerate(rows, 1):
        msg_id = row["id"]
        user_id = row["user_id"]
        content = row["content"]

        if not content.strip():
            skip += 1
            continue

        try:
            vec = await embed_text(content)
            vec_str = "[" + ",".join(str(x) for x in vec) + "]"

            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO message_embeddings (message_id, user_id, content, embedding)
                    VALUES ($1, $2, $3, $4::vector)
                    ON CONFLICT DO NOTHING
                    """,
                    msg_id, user_id, content, vec_str,
                )
            ok += 1

            if i % 50 == 0:
                log.info("Progress: %d/%d (ok=%d skip=%d err=%d)", i, len(rows), ok, skip, err)

            await asyncio.sleep(0.1)

        except Exception as e:
            log.warning("Failed message_id=%d: %s", msg_id, e)
            err += 1
            await asyncio.sleep(1.0)  # Back off on error

    log.info("Done: ok=%d skip=%d err=%d / total=%d", ok, skip, err, len(rows))
    await pool.close()


if __name__ == "__main__":
    asyncio.run(backfill())
