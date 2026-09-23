import os
import asyncio
from io import BytesIO
from aiogram import Bot, Dispatcher, types, F
from dotenv import load_dotenv

# Загружаем переменные из .env (если файл существует рядом с кодом)
load_dotenv()

# Получаем токен из переменной окружения
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    exit("Ошибка: Переменная окружения BOT_TOKEN не задана!")

bot = Bot(token=TOKEN)
dp = Dispatcher()


@dp.message(F.video)
async def video_handler(message: types.Message):
    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_video")

    video_buffer = BytesIO()

    # Скачиваем в оперативную память
    video_file = await message.bot.get_file(message.video.file_id)
    await message.bot.download_file(video_file.file_path, video_buffer)
    video_buffer.seek(0)

    # Отправляем обратно из памяти
    await message.answer_video(
        video=types.BufferedInputFile(video_buffer.read(), filename="video.mp4"),
        caption="Ваше видео через переходник 🔄"
    )


async def main():
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
