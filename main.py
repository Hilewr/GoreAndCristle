import os
import html
import asyncio
import random
import string
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import LabeledPrice, PreCheckoutQuery, InlineKeyboardMarkup, InlineKeyboardButton
from moviepy.editor import VideoFileClip, ImageClip, CompositeVideoClip
import PIL.Image
from PIL import Image, ImageOps

# ФИКС: moviepy 1.0.3 использует PIL.Image.ANTIALIAS, который удалён в Pillow 10+.
# Без этой заплатки вотермарка молча не накладывалась (видео уходило без неё).
if not hasattr(PIL.Image, "ANTIALIAS"):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

ADMIN_IDS = [8053962845]  # Твой админский ID
PRICE_STARS = 150        # Цена в Звездах (150 Stars)
PRICE_LINK = "https://t.me/hebesm"  # Твой Прайс/Директ

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Базы данных в оперативной памяти бота
video_database = {}
CHANNEL_DATA = {"id": None}  # Хранение ID канала без создания файлов

PRIVATE_LIMIT = 2  # сколько приватов продаётся вместе


def parse_ids(raw):
    ids = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return ids


# ID привязанных приватов. Заполняются командой /private_links.
# Чтобы привязка переживала перезапуск бота, можно задать их на хостинге переменной:
# PRIVATE_CHANNEL_IDS="-1001111111111,-1002222222222"
PRIVATE_CHANNELS = parse_ids(os.getenv("PRIVATE_CHANNEL_IDS"))[:PRIVATE_LIMIT]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATERMARK_PATH = os.path.join(BASE_DIR, "watermark.png")

_BOT_USERNAME = None


def get_channel_id():
    return CHANNEL_DATA["id"]


async def get_bot_username():
    """Username бота запрашиваем у Telegram один раз и кэшируем."""
    global _BOT_USERNAME
    if _BOT_USERNAME is None:
        _BOT_USERNAME = (await bot.get_me()).username
    return _BOT_USERNAME


def make_bot_link(username, start_param):
    # ФИКС: после t.me обязательно нужен слеш, иначе Telegram отдаёт BUTTON_URL_INVALID
    return f"https://t.me/{username}?start={start_param}"


async def notify_admins(text):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=text)
        except Exception as e:
            print(f"Не удалось уведомить админа {admin_id}: {e}")


# Только маленькие латинские буквы и цифры (заглавные запрещены в параметре ?start=)
def generate_random_slug(length=12):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


class PrivateStates(StatesGroup):
    waiting_for_private_forward = State()


class PostStates(StatesGroup):
    waiting_for_video = State()
    waiting_for_photo = State()
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

        final_video.write_videofile(
            output_path,
            codec="libx264",
            audio_codec="aac",
            logger=None,
            threads=4,
            preset="ultrafast"
        )

        video.close()
        final_video.close()
        return True
    except Exception as e:
        print(f"Ошибка вотермарки: {e}")
        return False


def apply_watermark_image(input_path, output_path):
    """Та же вотермарка, что и на видео: 20% ширины, прозрачность 60%, правый нижний угол."""
    if not os.path.exists(WATERMARK_PATH):
        print(f"⚠️ Вотермарка не найдена по пути: {WATERMARK_PATH}")
        return False
    try:
        with Image.open(input_path) as src:
            base = ImageOps.exif_transpose(src).convert("RGB")
        with Image.open(WATERMARK_PATH) as wm_src:
            wm = wm_src.convert("RGB")

        new_w = max(1, int(base.width * 0.2))
        new_h = max(1, int(wm.height * new_w / wm.width))
        if new_h > base.height:  # на случай очень «широкой» картинки
            new_h = base.height
            new_w = max(1, int(wm.width * new_h / wm.height))
        wm = wm.resize((new_w, new_h), Image.LANCZOS)

        opacity_mask = Image.new("L", wm.size, int(255 * 0.6))
        base.paste(wm, (base.width - new_w, base.height - new_h), opacity_mask)
        base.save(output_path, "JPEG", quality=95)
        return True
    except Exception as e:
        print(f"Ошибка вотермарки на картинке: {e}")
        return False


# --- ПРИВЯЗКА ПРИВАТОВ: /private_links ---
# Эти хендлеры должны стоять ВЫШЕ привязки основного канала: пока админ в режиме
# привязки приватов, пересланный пост должен попасть сюда, а не в основной канал.
@dp.message(Command("private_links"))
async def start_private_binding(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    if PRIVATE_CHANNELS:
        current = "Сейчас привязано: " + ", ".join(str(c) for c in PRIVATE_CHANNELS)
    else:
        current = "Сейчас ни один приват не привязан."
    await state.set_state(PrivateStates.waiting_for_private_forward)
    await state.set_data({"private_ids": []})
    await message.answer(
        f"{current}\n\n"
        f"Привязываем заново (максимум {PRIVATE_LIMIT}). Сначала добавь бота админом в каждый приват "
        "с правом «Приглашать пользователей», потом пересылай мне по одному посту из каждого привата.\n\n"
        "Закончить раньше — /done, отменить — /cancel. "
        "Старая привязка заменится, только когда закончишь."
    )


async def commit_private_channels(message: types.Message, state: FSMContext, ids, note=""):
    PRIVATE_CHANNELS[:] = ids
    await state.clear()
    ids_str = ",".join(str(i) for i in ids)
    await message.answer(
        f"✅ Готово, привязано приватов: {len(ids)}.{note}\n"
        "После оплаты покупатель получит по одноразовой ссылке на каждый.\n\n"
        "Чтобы привязка переживала перезапуск бота, добавь на хостинге переменную окружения:\n"
        f"PRIVATE_CHANNEL_IDS={ids_str}"
    )


@dp.message(PrivateStates.waiting_for_private_forward, F.forward_from_chat)
async def bind_private_channel(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    chat = message.forward_from_chat
    if chat.type != "channel":
        await message.answer("Это не канал. Перешли пост из канала-привата:")
        return

    data = await state.get_data()
    ids = list(data.get("private_ids", []))
    if chat.id in ids:
        await message.answer("Этот приват уже добавлен. Перешли пост из другого или напиши /done.")
        return

    # Проверяем права бота сразу, а не после чьей-то оплаты
    try:
        member = await bot.get_chat_member(chat_id=chat.id, user_id=bot.id)
    except Exception as e:
        await message.answer(f"❌ Не удалось проверить права бота в «{chat.title}»: {e}\nДобавь бота в канал админом и перешли пост ещё раз.")
        return
    if member.status != "administrator" or not getattr(member, "can_invite_users", False):
        await message.answer(f"❌ В «{chat.title}» бот не админ или у него нет права «Приглашать пользователей». Выдай право и перешли пост ещё раз.")
        return

    ids.append(chat.id)
    await state.update_data(private_ids=ids)
    note = "\n⚠️ Внимание: это тот же канал, что основной для постов." if chat.id == get_channel_id() else ""

    if len(ids) >= PRIVATE_LIMIT:
        await commit_private_channels(message, state, ids, note)
    else:
        await message.answer(
            f"✅ Приват {len(ids)}/{PRIVATE_LIMIT} добавлен: «{chat.title}».{note}\n"
            "Перешли пост из следующего привата или напиши /done, чтобы закончить."
        )


@dp.message(PrivateStates.waiting_for_private_forward, Command("done"))
async def finish_private_binding(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    ids = (await state.get_data()).get("private_ids", [])
    if not ids:
        await state.clear()
        await message.answer("Ничего не добавлено, старая привязка осталась как была.")
        return
    await commit_private_channels(message, state, ids)


@dp.message(PrivateStates.waiting_for_private_forward, lambda m: not (m.text or "").startswith("/"))
async def private_binding_hint(message: types.Message):
    await message.answer("Перешли мне пост из канала-привата (именно пересылкой) или напиши /done, /cancel.")


@dp.message(Command("cancel"))
async def cancel_any(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.clear()
    await message.answer("❌ Отменено.")


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
        video_slug = args[len("vid_"):]
        if video_slug in video_database:
            saved_file_id = video_database[video_slug]
            await message.answer("🎬 Твое видео готово к просмотру:")
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
    if not PRIVATE_CHANNELS:
        await callback.answer("Приват пока не настроен, загляни позже.", show_alert=True)
        return
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Доступ в приваты",
        description="Единоразовая оплата. Доступ ко всем приватам навсегда.",
        payload="private_lifetime_access",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Приваты навсегда", amount=PRICE_STARS)]
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def success_payment(message: types.Message):
    user = message.from_user
    charge_id = message.successful_payment.telegram_payment_charge_id
    who = f"{user.id} (@{user.username})" if user.username else str(user.id)
    channels = list(PRIVATE_CHANNELS)

    if not channels:
        await message.answer(f"✅ Оплата прошла, но выдать ссылки сразу не вышло. Напиши сюда, всё выдадим вручную: {PRICE_LINK}")
        await notify_admins(
            "⚠️ Оплата прошла, но приваты не привязаны (возможно, бот перезапускался). "
            f"Привяжи их через /private_links и выдай ссылки вручную.\nПокупатель: {who}\nID платежа: {charge_id}"
        )
        return

    links, failed = [], []
    for number, chat_id in enumerate(channels, start=1):
        try:
            # member_limit=1 → ссылка одноразовая; name помогает найти, кому она выдана
            invite = await bot.create_chat_invite_link(chat_id=chat_id, member_limit=1, name=f"buy_{user.id}"[:32])
            links.append(f"Приват {number}: {invite.invite_link}")
        except Exception as e:
            failed.append((number, chat_id, e))

    text = ""
    if links:
        text += "🎉 Успешно! Твои одноразовые ссылки для входа в приваты:\n\n" + "\n".join(links)
        text += "\n\nКаждая ссылка сработает только один раз, так что не пересылай их никому."
    if failed:
        numbers = ", ".join(f"приват {n}" for n, _, _ in failed)
        text += f"\n\n⚠️ Не удалось выдать: {numbers}. Админ уже в курсе и пришлёт ссылку вручную (или напиши сюда: {PRICE_LINK})."
        details = "\n".join(f"Приват {n} ({cid}): {e}" for n, cid, e in failed)
        await notify_admins(f"⚠️ Не все ссылки выданы после оплаты.\nПокупатель: {who}\nID платежа: {charge_id}\n{details}")
    await message.answer(text.strip())


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
    await message.answer("Видео получено. Теперь отправь картинку для поста (или напиши `нет`, если пост без картинки):")
    await state.set_state(PostStates.waiting_for_photo)


@dp.message(PostStates.waiting_for_photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    await state.update_data(photo_file_id=message.photo[-1].file_id)
    await message.answer("Картинка получена. Напиши имя автора / описание (например: `влад сопляков`):")
    await state.set_state(PostStates.waiting_for_title)


@dp.message(PostStates.waiting_for_photo, F.text)
async def process_photo_skip(message: types.Message, state: FSMContext):
    if message.text.strip().lower() != "нет":
        await message.answer("Отправь картинку или напиши `нет`:")
        return
    await state.update_data(photo_file_id=None)
    await message.answer("Ок, без картинки. Напиши имя автора / описание (например: `влад сопляков`):")
    await state.set_state(PostStates.waiting_for_title)


@dp.message(PostStates.waiting_for_title)
async def process_title(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Нужен текст. Напиши имя автора / описание:")
        return
    await state.update_data(title=message.text)
    await message.answer("Укажи ссылку на полное видео (если ссылок нет, напиши `нет`):")
    await state.set_state(PostStates.waiting_for_link)


@dp.message(PostStates.waiting_for_link)
async def process_link(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Нужен текст. Отправь ссылку или напиши `нет`:")
        return

    data = await state.get_data()
    video_link = message.text.strip()
    title = data['title']
    file_id = data['file_id']
    photo_file_id = data.get('photo_file_id')

    await message.answer("⏳ Обрабатываю видео и накладываю вотермарку...")

    user_id = message.from_user.id
    input_video_path = f"input_{user_id}.mp4"
    output_video_path = f"output_{user_id}.mp4"
    input_photo_path = f"input_{user_id}.jpg"
    output_photo_path = f"output_{user_id}.jpg"
    new_photo_id = None

    try:
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, input_video_path)

        # moviepy работает синхронно и долго, поэтому гоняем в отдельном потоке,
        # чтобы бот не «замерзал» для остальных пользователей
        success = await asyncio.to_thread(apply_watermark, input_video_path, output_video_path)
        final_video_path = output_video_path if success else input_video_path

        # Загружаем видео в Telegram и вытаскиваем нормальный file_id из облака
        temp_msg = await bot.send_video(chat_id=user_id, video=types.FSInputFile(final_video_path))
        new_file_id = temp_msg.video.file_id
        await bot.delete_message(chat_id=user_id, message_id=temp_msg.message_id)

        # Картинка для поста: та же вотермарка + тот же приём с file_id
        if photo_file_id:
            photo_file = await bot.get_file(photo_file_id)
            await bot.download_file(photo_file.file_path, input_photo_path)
            photo_ok = await asyncio.to_thread(apply_watermark_image, input_photo_path, output_photo_path)
            final_photo_path = output_photo_path if photo_ok else input_photo_path

            temp_photo = await bot.send_photo(chat_id=user_id, photo=types.FSInputFile(final_photo_path))
            new_photo_id = temp_photo.photo[-1].file_id
            await bot.delete_message(chat_id=user_id, message_id=temp_photo.message_id)
    except Exception as e:
        await message.answer(f"❌ Не удалось обработать видео: {e}")
        await state.clear()
        return
    finally:
        # Временные файлы чистим в любом случае, даже если что-то упало
        for path in (input_video_path, output_video_path, input_photo_path, output_photo_path):
            if os.path.exists(path):
                os.remove(path)

    video_slug = generate_random_slug()
    video_database[video_slug] = new_file_id

    bot_username = await get_bot_username()

    # Текст идёт с parse_mode=HTML, поэтому экранируем спецсимволы (<, >, &)
    caption = f"┃ {html.escape(title)} ❞\n\n"
    if video_link.lower() != "нет":
        caption += f"ссылка на видео\n👇👇👇👇👇👇\n\n{html.escape(video_link)}\n\n"
    caption += f"<a href='{make_bot_link(bot_username, 'buy')}'>приват</a>"

    channel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Смотреть видео", url=make_bot_link(bot_username, f"vid_{video_slug}"))]
    ])

    # Предпросмотр: админ видит пост ровно в том виде, в каком он уйдёт в канал
    try:
        if new_photo_id:
            await message.answer_photo(photo=new_photo_id, caption=caption, reply_markup=channel_kb, parse_mode="HTML")
        else:
            await message.answer(text=caption, reply_markup=channel_kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        await message.answer(
            f"❌ Не получилось собрать пост: {e}\n\n"
            "Если пост с картинкой, возможно, подпись длиннее 1024 символов. Начни заново: /post"
        )
        await state.clear()
        return

    await state.update_data(caption=caption, video_slug=video_slug, post_photo_id=new_photo_id)

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Опубликовать", callback_data="publish_post")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_post")]
    ])
    await message.answer("👀 Выше предпросмотр поста. Публикуем в канал?", reply_markup=confirm_kb)
    await state.set_state(PostStates.waiting_for_confirm)


@dp.callback_query(PostStates.waiting_for_confirm, F.data == "publish_post")
async def publish_post(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    channel_id = get_channel_id()
    bot_username = await get_bot_username()

    channel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Смотреть видео", url=make_bot_link(bot_username, f"vid_{data['video_slug']}"))]
    ])

    try:
        photo_id = data.get('post_photo_id')
        if photo_id:
            # Пост с картинкой (с вотермаркой), подписью и кнопкой
            await bot.send_photo(
                chat_id=channel_id,
                photo=photo_id,
                caption=data['caption'],
                reply_markup=channel_kb,
                parse_mode="HTML"
            )
        else:
            # Пост без картинки: форматированный текст со ссылками и кнопкой
            await bot.send_message(
                chat_id=channel_id,
                text=data['caption'],
                reply_markup=channel_kb,
                parse_mode="HTML"
            )
        await callback.message.answer("🚀 Пост успешно опубликован в канал!")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка публикации: {e}")

    await state.clear()
    await callback.answer()


@dp.callback_query(PostStates.waiting_for_confirm, F.data == "cancel_post")
async def cancel_post(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("❌ Публикация отменена.")
    await callback.answer()


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
