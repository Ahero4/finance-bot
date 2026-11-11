from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
import asyncio
import os
from router import router
from db.models import async_main


async def main():
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Set BOT_TOKEN before starting the bot")
    bot = Bot(token=token)
    try:
        await async_main()
        dp = Dispatcher(events_isolation=SimpleEventIsolation())
        dp.include_router(router)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Ручная остановка бота")
