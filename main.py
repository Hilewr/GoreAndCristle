import os
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.utils.payload import decode_payload, encode_payload
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    exit("Ошибка: Переменная окружения BOT_TOKEN не задана!")

bot = Bot(token=TOKEN)
dp = Dispatcher()


# 1. Принимаем видео и генерируем ссылку
@dp.message(F.video)
async def video_handler(message: types.Message):
    # Получаем file_id видео
    file_id = message.video.file_id
    
    # Кодируем file_id для ссылки (Telegram не любит сырые file_id в параметрах start)
    payload = encode_payload(file_id)
    
    # Получаем имя бота, чтобы собрать ссылку
    bot_info = await bot.get_me()
    link = f"https://t.me{bot_info.username}?start={payload}"
    
    await message.reply(
        f"🔗 **Ссылка на просмотр видео:**\n`{link}`\n\n"
        f"Поделитесь ей, и при переходе пользователь сразу получит этот ролик.",
        parse_mode="Markdown"
    )


# 2. Обрабатываем переход по ссылке (команда /start с параметром)
@dp.message(CommandStart())
async def start_command(message: types.Message):
    # Достаем то, что идет после /start
    args = message.text.split(maxsplit=1)
    
    if len(args) > 1:
        try:
            # Декодируем оригинальный file_id видео
            file_id = decode_payload(args[1])
            
            # Отправляем видео пользователю (мгновенно, без скачивания на сервер!)
            await message.answer_video(video=file_id, caption="Ваше видео 🍿")
        except Exception:
            await message.answer("❌ Неверная или устаревшая ссылка.")
    else:
        await message.answer("Привет! Отправь мне видео, а я сделаю на него ссылку для просмотра.")


async def main():
    print("Бот-переходник запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
