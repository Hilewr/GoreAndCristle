import os
import asyncio
import random
import string
import requests
import shutil
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import LabeledPrice, PreCheckoutQuery, InlineKeyboardMarkup, InlineKeyboardButton
from moviepy.editor import VideoFileClip, ImageClip, CompositeVideoClip

# --- НАСТРОЙКИ (ВШИТЫ НАПРЯМУЮ) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [8053962845]  # Твой админский ID
PRICE_STARS = 150        # Цена в Звездах (150 Stars)
PRICE_LINK = "https://t.me/hebesm"  # Твой Прайс/Директ

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Базы данных в оперативной памяти бота
video_database = {}
CHANNEL_DATA = {"id": None}  # Хранение ID канала без создания файлов

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATERMARK_PATH = os.path.join(BASE_DIR, "watermark.png")

def get_channel_id():
    return CHANNEL_DATA["id"]

def generate_random_slug(length=8):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

class PostStates(StatesGroup):
    waiting_for_video = State()
    waiting_for_title = State()
    waiting_for_link = State()
    waiting_for_confirm = State()

# --- НАЛОЖЕНИЕ ВОТЕРМАРКИ ---
def apply_watermark(input_path, output_path):
    if not os.path.exists(WATERMARK_PATH):
        print(f"⚠️ Вотермарка не найдена по пути: {WATERMARK_PATH}")
        return False
    try:
        video = VideoFileClip(input_path)
        watermark = (ImageClip(WATERMARK_PATH)
                     .resize(width=video.w * 0.2)
                     .set_opacity(0.6)
                     .set_duration(video.duration)
                     .set_position(("right", "bottom")))
        final_video = CompositeVideoClip([video, watermark])
        final_video.write_videofile(output_path, codec="libx264", audio_codec="aac", logger=None)
        video.close()
        final_video.close()
        return True
    except Exception as e:
        print(f"Ошибка вотермарки: {e}")
        return False

# --- ПОДГОТОВКА ОБЛОЖКИ ПОСТА ---
def download_random_image(output_path="random_preview.jpg"):
    if os.path.exists(WATERMARK_PATH):
        try:
            shutil.copy(WATERMARK_PATH, output_path)
            return True
        except Exception as e:
            print(f"Ошибка копирования вотермарки: {e}")
            
    try:
        url = "https://picsum.photos"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            with open(output_path, "wb") as f: f.write(response.content)
            return True
    except Exception as e:
        print(f"Ошибка скачивания фото: {e}")
    return False

# --- ПРИВЯЗКА КАНАЛА ---
@dp.message(F.forward_from_chat)
async def handle_forwarded_channel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    if message.forward_from_chat.type == "channel":
        channel_id = message.forward_from_chat.id
        CHANNEL_DATA["id"] = channel_id
        await message.answer(f"✅ Канал успешно привязан в память бота!\nID канала: `{channel_id}`\nТеперь можно создавать посты.")
# --- СТАРТ И ВЫДАЧА ВИДЕО В ЛИЧКУ ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message, command: CommandObject):
    args = command.args
    if args and args.startswith("vid_"):
        video_slug = args.replace("vid_", "")
        if video_slug in video_database:
            saved_file_id = video_database[video_slug]
            await message.answer("🎬 Твое video готово к просмотру:")
            await bot.send_video(chat_id=message.from_user.id, video=saved_file_id)
            return
        else:
            await message.answer("⚠️ Ссылка устарела или видео больше недоступно.")
            return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить приват (навсегда)", callback_data="buy_private")],
        [InlineKeyboardButton(text="📬 Написать в DIRECT (Прайс)", url=PRICE_LINK)]
    ])
    await message.answer("Привет! Через этого бота можно получить доступ в приват или посмотреть видео по кнопкам из канала.", reply_markup=kb)

# --- ОПЛАТА СТАРСАМИ ---
@dp.callback_query(F.data == "buy_private")
async def send_invoice(callback: types.CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Доступ в Приватный Канал",
        description="Единоразовая оплата. Доступ навсегда.",
        payload="private_lifetime_access",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Приват навсегда", amount=PRICE_STARS)]
    )
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def success_payment(message: types.Message):
    channel_id = get_channel_id()
    if not channel_id:
        await message.answer("⚠️ Ошибка: Главный канал не привязан админом.")
        return
    try:
        invite_link = await bot.create_chat_invite_link(chat_id=channel_id, member_limit=1)
        await message.answer(f"🎉 Успешно! Твоя уникальная ссылка для входа в приват:\n\n{invite_link.invite_link}")
    except Exception as e:
        await message.answer(f"Ошибка создания ссылки: {e}")

# --- АДМИНКА: СОЗДАНИЕ ПОСТА-СКРЫТКИ ---
@dp.message(Command("post"))
async def start_post(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    if not get_channel_id():
        await message.answer("⚠️ Сначала перешли мне любой пост из канала для привязки!")
        return
    await message.answer("Отправь секретное видео:")
    await state.set_state(PostStates.waiting_for_video)

@dp.message(PostStates.waiting_for_video, F.video)
async def process_video(message: types.Message, state: FSMContext):
    await state.update_data(file_id=message.video.file_id)
    await message.answer("Видео получено. Напиши имя автора / описание (например: `влад сопляков`):")
    await state.set_state(PostStates.waiting_for_title)

@dp.message(PostStates.waiting_for_title)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    await message.answer("Укажи ссылку на полное видео (если ссылок нет, напиши `нет`):")
    await state.set_state(PostStates.waiting_for_link)

@dp.message(PostStates.waiting_for_link)
async def process_link(message: types.Message, state: FSMContext):
    data = await state.get_data()
    video_link = message.text
    title = data['title']
    file_id = data['file_id']
    
    await message.answer("⏳ Обрабатываю видео, накладываю вотермарку...")
    
    input_video_path = f"input_{message.from_user.id}.mp4"
    output_video_path = f"output_{message.from_user.id}.mp4"
    preview_img_path = f"preview_{message.from_user.id}.jpg"
    
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, input_video_path)
    
    success = apply_watermark(input_video_path, output_video_path)
    final_video_path = output_video_path if success else input_video_path
    
    temp_msg = await bot.send_video(chat_id=message.from_user.id, video=types.FSInputFile(final_video_path))
    new_file_id = temp_msg.video.file_id
    await bot.delete_message(chat_id=message.from_user.id, message_id=temp_msg.message_id)
    
    video_slug = generate_random_slug()
    video_database[video_slug] = new_file_id
    
    download_random_image(preview_img_path)
    
    bot_user = await bot.get_me()
    caption = f"┃ {title} ❞\n\n"
    if video_link.lower() != "нет":
        caption += f"ссылка на видео\n👇👇👇👇👇👇\n\n{video_link}\n\n"
    caption += f"<a href='https://t.me{bot_user.username}?start=buy'>приват</a>"
    
    channel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Смотреть видео", url=f"https://t.me{bot_user.username}?start=vid_{video_slug}")]
    ])
    
    await bot.send_photo(
        chat_id=message.from_user.id, photo=types.FSInputFile(preview_img_path),
        caption=f"👀 <b>ПРЕДПРОСМОТР ПОСТА:</b>\n\n{caption}", reply_markup=channel_kb, parse_mode="HTML"
    )
    
    await state.update_data(caption=caption, video_slug=video_slug, preview_path=preview_img_path)
    
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Опубликовать", callback_data="publish_post")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_post")]
    ])
    await message.answer("Публикуем эту заглушку в канал?", reply_markup=confirm_kb)
    await state.set_state(PostStates.waiting_for_confirm)
    
    if os.path.exists(input_video_path): os.remove(input_video_path)
    if os.path.exists(output_video_path): os.remove(output_video_path)

@dp.callback_query(PostStates.waiting_for_confirm, F.data == "publish_post")
async def publish_post(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    channel_id = get_channel_id()
    bot_user = await bot.get_me()
    
    channel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Смотреть видео", url=f"https://t.me{bot_user.username}?start=vid_{data['video_slug']}")]
    ])
    
    try:
        await bot.send_photo(
            chat_id=channel_id, photo=types.FSInputFile(data['preview_path']),
            caption=data['caption'], reply_markup=channel_kb, parse_mode="HTML"
        )
        await callback.message.answer("🚀 Пост-заглушка успешно отправлен в канал!")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка публикации: {e}")
        
    if os.path.exists(data['preview_path']): os.remove(data['preview_path'])
    await state.clear()
    await callback.answer()

@dp.callback_query(PostStates.waiting_for_confirm, F.data == "cancel_post")
async def cancel_post(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if os.path.exists(data['preview_path']): os.remove(data['preview_path'])
    await state.clear()
    await callback.message.answer("❌ Публикация отменена.")
    await callback.answer()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
