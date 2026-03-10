"""Rate limiting middleware for the Telegram bot."""

from collections import defaultdict
from datetime import datetime, timedelta

from aiogram import BaseMiddleware
from aiogram.types import Message

# (limit, window_seconds) — maximum limit requests per window seconds
LIMITS: dict[str, tuple[int, int]] = {
    "ask":     (5, 60),
    "report":  (3, 60),
    "scope":   (3, 60),
    "plateau": (3, 60),
    "lab":     (5, 60),
    "default": (20, 60),
}


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        # user_id → command → list of datetime
        self._history: dict[str, dict[str, list[datetime]]] = defaultdict(lambda: defaultdict(list))

    async def __call__(self, handler, event: Message, data):
        if not isinstance(event, Message):
            return await handler(event, data)

        user_id = str(event.from_user.id)
        text = event.text or ""
        cmd = text.lstrip("/").split()[0].lower() if text.startswith("/") else "default"

        limit, window = LIMITS.get(cmd, LIMITS["default"])
        now = datetime.now()
        cutoff = now - timedelta(seconds=window)

        history = self._history[user_id][cmd]
        self._history[user_id][cmd] = [t for t in history if t > cutoff]

        if len(self._history[user_id][cmd]) >= limit:
            await event.answer("Too many requests. Please wait.")
            return

        self._history[user_id][cmd].append(now)
        return await handler(event, data)
