import os
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.utils.payload import decode_payload, encode_payload
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    exit("Ошибка: Переменная окружения BOT_TOKEN не задана!")

bot = Bot(token=TOKEN)
dp = Dispatcher()


# 1. Принимаем видео и выдаем ОДНУ железно рабочую ссылку
@dp.message(F.video)
async def video_handler(message: types.Message):
    file_id = message.video.file_id
    
    # Кодируем file_id, чтобы Telegram пропустил его через параметр start
    payload = encode_payload(file_id)
    
    # Получаем юзернейм бота
    bot_info = await bot.get_me()
    username = bot_info.username
    
    # Создаем прямую ссылку, которая открывается строго внутри приложения Telegram
    tg_link = f"tg://resolve?domain={username}&start={payload}"
    
    # Отрендерим красивый текст с кнопкой для удобства
    text = (
        f"🔗 **Ссылка на видео готова!**\n\n"
        f"Скопируйте её и отправьте кому угодно. При клике человек сразу откроет бота и получит это видео:\n\n"
        f"`{tg_link}`"
    )
    
    # Добавим инлайн-кнопку для еще более быстрого перехода
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="Смотреть видео 🍿", url=tg_link)]
        ]
    )
    
    await message.reply(text, reply_markup=keyboard, parse_mode="Markdown")


# 2. Обрабатываем клик по этой ссылке (команда /start с параметром)
@dp.message(CommandStart())
async def start_command(message: types.Message):
    # Пытаемся достать аргумент из команды /start (все, что идет после start=)
    args = message.text.split(maxsplit=1)
    
    if len(args) > 1:
        raw_payload = args[1]
        try:
            # Декодируем обратно оригинальный file_id видео
            file_id = decode_payload(raw_payload)
            
            # Отправляем видео из облака Telegram (0% нагрузки на твой сервер)
            await message.answer_video(video=file_id, caption="Ваше видео через переходник 🔄")
        except Exception:
            await message.answer("❌ Неверная, поврежденная или устаревшая ссылка.")
    else:
        # Обычный старт без параметров
        await message.answer(
            "Привет! Я бот-переходник.\n\n"
            "Просто отправь мне любое видео, а я сделаю на него прямую "
            "ссылку, по которой его сможет посмотреть любой пользователь."
        )


async def main():
    print("Бот-переходник запущен и готов к работе...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
