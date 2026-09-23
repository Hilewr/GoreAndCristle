import os
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.utils.payload import decode_payload, encode_payload
from dotenv import load_dotenv

# Инициализируем переменные окружения
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    exit("Ошибка: Переменная окружения BOT_TOKEN не задана!")

bot = Bot(token=TOKEN)
dp = Dispatcher()


# 1. Принимаем видео и генерируем одну железную tg:// ссылку
@dp.message(F.video)
async def video_handler(message: types.Message):
    file_id = message.video.file_id
    
    # Безопасно кодируем file_id для передачи внутри ссылки
    payload = encode_payload(file_id)
    
    # Получаем актуальный юзернейм вашего бота
    bot_info = await bot.get_me()
    username = bot_info.username
    
    # Создаем прямую внутреннюю ссылку
    tg_link = f"tg://resolve?domain={username}&start={payload}"
    
    text = (
        f"🔗 **Ссылка на видео готова!**\n\n"
        f"Скопируйте и перешлите её в любой чат:\n\n"
        f"`{tg_link}`"
    )
    
    # Инлайн-кнопка для моментального просмотра прямо из бота для тестов
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="Смотреть видео 🍿", url=tg_link)]
        ]
    )
    
    await message.reply(text, reply_markup=keyboard, parse_mode="Markdown")


# 2. ИСПРАВЛЕНО: Правильный перехват аргумента по клику на ссылку
@dp.message(CommandStart())
async def start_command(message: types.Message, command: CommandObject):
    # Достаем аргумент (payload) напрямую через инструмент aiogram
    args = command.args
    
    if args:
        try:
            # Декодируем оригинальный file_id видео
            file_id = decode_payload(args)
            
            # Telegram мгновенно отправляет его из своего облака пользователю
            await message.answer_video(video=file_id, caption="Ваше видео 🍿")
        except Exception as e:
            # Если токен ссылки битый или старый
            await message.answer("❌ Ссылка повреждена или устарела.")
            print(f"Ошибка декодирования: {e}")
    else:
        # Если пользователь просто зашел в бота и нажал Старт без ссылки
        await message.answer(
            "Привет! Я бот-переходник.\n\n"
            "Отправь мне любое видео, а я сделаю на него прямую "
            "ссылку, по которой его сможет посмотреть любой пользователь."
        )


async def main():
    print("Бот-переходник успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
