"""Entry point: DB, owner guard, LLM healthcheck, scheduler and long polling."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from . import runtime
from .config import env, settings
from .database import Database
from .handlers import inbox, onboarding, views
from .llm_client import close_llm, wait_for_llm
from .middlewares import OwnerMiddleware
from .scheduler_jobs import create_scheduler
from .stt_whisper import whisper

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="menu", description="Главное меню и помощь"),
    BotCommand(command="today", description="Карточка дня"),
    BotCommand(command="week", description="Сводка на 7 дней"),
    BotCommand(command="events", description="События и регистрации"),
    BotCommand(command="tasks", description="Задачи и авто-раскладка"),
    BotCommand(command="closed", description="Просмотр закрытых задач"),
    BotCommand(command="now", description="Одна задача на сейчас"),
    BotCommand(command="schedule_edit", description="Сетка занятий и импорт"),
    BotCommand(command="inbox", description="История входящих"),
    BotCommand(command="status", description="Статус сервера и моделей"),
    BotCommand(command="notes", description="Заметки"),
    BotCommand(command="find", description="Поиск по заметкам и задачам"),
    BotCommand(command="reminders", description="Напоминания"),
    BotCommand(command="watch", description="Мониторинг страниц"),
    BotCommand(command="archive", description="Архив прошлого"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="task", description="Быстро добавить задачу"),
    BotCommand(command="note", description="Быстро добавить заметку"),
    BotCommand(command="remind", description="Быстро добавить напоминание"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="cancel", description="Отменить ввод"),
]


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, env.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _set_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(COMMANDS)
    except Exception as exc:  # noqa: BLE001 - Telegram may be temporarily unavailable
        log.warning("Could not set bot commands: %s", exc)


async def main() -> None:
    _configure_logging()
    if not env.bot_token:
        raise SystemExit(
            "BOT_TOKEN is not set. Copy .env.example to .env and fill the token."
        )

    Path(env.db_path).parent.mkdir(parents=True, exist_ok=True)
    database = Database(env.db_path)
    await database.connect()
    await database.init_schema()
    settings.bind(database)
    runtime.database = database

    recovered = await inbox.recover_unfinished()
    if recovered:
        log.info("Recovered %s unfinished inbox message(s) for retry", recovered)

    owner = await settings.owner_id()
    if owner is None and env.admin_id is not None:
        await settings.set_owner(env.admin_id)
        owner = env.admin_id
    if owner is None:
        log.info("Owner is not set yet: the first /start will claim the bot")

    bot = Bot(
        token=env.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    runtime.bot = bot

    scheduler = await create_scheduler(bot, database)
    runtime.scheduler = scheduler
    scheduler.start()

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.message.middleware(OwnerMiddleware())
    dispatcher.callback_query.middleware(OwnerMiddleware())

    # Order matters: wizard/state handlers and commands first, catch-all inbox last.
    dispatcher.include_router(onboarding.router)
    dispatcher.include_router(views.router)
    dispatcher.include_router(inbox.router)

    log.info(
        "Starting tg-secretary: db=%s, llm=%s/%s",
        env.db_path, env.llm_base_url, env.llm_model_name,
    )
    try:
        await asyncio.gather(
            wait_for_llm(120),
            _set_commands(bot),
        )
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            drop_pending_updates=False,
        )
    finally:
        log.info("Shutting down…")
        await inbox.shutdown_pending()
        try:
            if scheduler.running:
                scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            log.debug("Scheduler shutdown failed", exc_info=True)
        await whisper.shutdown()
        await close_llm()
        try:
            await bot.session.close()
        except Exception:  # noqa: BLE001
            pass
        await database.close()
        log.info("Stopped")


if __name__ == "__main__":
    asyncio.run(main())
