import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]  # токен берётся из переменной окружения

# Реклама RichAds (richads.com/ru/publishers) — необязательные переменные.
# Если не заданы, реклама просто не показывается и бот работает как раньше.
RICHADS_PUBLISHER_ID = os.environ.get("408222")
RICHADS_WIDGET_ID = os.environ.get("RICHADS_WIDGET_ID")  # опционально

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
# Реклама RichAds
# ---------------------------------------------------------------------------

RICHADS_URL = "http://15068.xml.adx1.com/telegram-mb"


async def fetch_ad(user_id: int, lang: str = "ru") -> dict | None:
    """Запрашивает рекламный креатив у RichAds. Возвращает None, если
    реклама не настроена или запрос не удался — тогда бот просто отдаёт видео."""
    if not RICHADS_PUBLISHER_ID:
        return None

    payload = {
        "language_code": lang,
        "publisher_id": RICHADS_PUBLISHER_ID,
        "telegram_id": str(user_id),
        "production": True,
    }
    if RICHADS_WIDGET_ID:
        payload["widget_id"] = RICHADS_WIDGET_ID

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(RICHADS_URL, json=payload, timeout=5) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if isinstance(data, list) and data:
                    return data[0]
                return None
    except Exception:
        logger.exception("Не удалось получить рекламу RichAds")
        return None


async def notify_impression(url: str) -> None:
    """Сообщает RichAds, что объявление реально показано пользователю."""
    try:
        async with aiohttp.ClientSession() as session:
            await session.get(url, timeout=5)
    except Exception:
        logger.exception("Не удалось отправить notification_url в RichAds")


async def show_ad(message: Message) -> None:
    lang = message.from_user.language_code or "ru"
    ad = await fetch_ad(message.from_user.id, lang)
    if not ad:
        return

    caption = ad.get("message") or ad.get("title") or "Спонсор"
    button_text = ad.get("button") or "Открыть"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=button_text, url=ad["link"])]]
    )
    try:
        if ad.get("image"):
            await message.answer_photo(
                ad["image"],
                caption=caption,
                reply_markup=keyboard,
                protect_content=True,
            )
        else:
            await message.answer(caption, reply_markup=keyboard, protect_content=True)

        if ad.get("notification_url"):
            asyncio.create_task(notify_impression(ad["notification_url"]))
    except Exception:
        logger.exception("Не удалось показать рекламу RichAds")


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

    await show_ad(message)
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
