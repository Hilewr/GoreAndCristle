import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]  # токен берётся из переменной окружения

DB_FILE = Path(__file__).parent / "links.json"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Простое хранилище "код -> file_id" (JSON-файл, без скачивания видео)
# ---------------------------------------------------------------------------

def _load() -> dict:
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text(encoding="utf-8"))
    return {}


def _save(data: dict) -> None:
    DB_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def save_link(code: str, file_id: str) -> None:
    async with _lock:
        data = _load()
        data[code] = file_id
        _save(data)


async def get_file_id(code: str) -> str | None:
    async with _lock:
        data = _load()
        return data.get(code)


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------

@dp.message(CommandStart(deep_link=True))
async def start_with_code(message: Message, command: CommandObject):
    code = command.args
    file_id = await get_file_id(code)
    if not file_id:
        await message.answer("Ссылка недействительна или устарела.")
        return
    await message.answer_video(file_id)


@dp.message(CommandStart())
async def start_plain(message: Message):
    await message.answer(
        "Привет! Пришли мне видео — я верну ссылку, по которой его можно получить снова."
    )


@dp.message(F.video)
async def handle_video(message: Message):
    file_id = message.video.file_id
    code = uuid.uuid4().hex[:10]
    await save_link(code, file_id)

    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={code}"
    await message.answer(f"Готово! Ссылка на видео:\n{link}")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
