"""Owner guard: first /start claims the bot, everyone else is ignored."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message

from .config import settings

log = logging.getLogger(__name__)


class OwnerMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        if getattr(user, "is_bot", False):
            return None
        owner_id = await settings.owner_id()
        if owner_id is None:
            # Claim-on-first-start: the very first /start becomes the owner.
            if isinstance(event, Message):
                text = (event.text or "").strip()
                command = text.split()[0].split("@")[0].lower() if text else ""
                if command == "/start":
                    return await handler(event, data)
            return None
        if int(user.id) != int(owner_id):
            log.debug("Ignoring user %s (owner is %s)", user.id, owner_id)
            return None
        return await handler(event, data)
