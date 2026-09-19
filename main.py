import os
import json
import time
import html
import asyncio
import random
import string
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
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

ADMIN_IDS = [8053962845, 8519289540, 6612202387]  # Админы бота
PRICE_STARS = 150        # Цена в Звездах (150 Stars)
PRICE_LINK = "https://t.me/hebesm"  # Твой Прайс/Директ

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATERMARK_PATH = os.path.join(BASE_DIR, "watermark.png")

PRIVATE_LIMIT = 2  # сколько приватов продаётся вместе
BOT_API_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # больше этого бот не может скачать через Bot API

# --- ХРАНИЛИЩЕ ---
# Каналы, приваты, ссылки на видео, пул автопостов и счётчик лежат в JSON-файле и переживают перезапуск.
# Файл создаётся рядом с main.py. Если хостинг стирает диск при перезапуске/деплое, подключи постоянный том
# и укажи путь к нему в переменной окружения DATA_DIR.
DATA_DIR = os.getenv("DATA_DIR", BASE_DIR)
DATA_FILE = os.path.join(DATA_DIR, "bot_data.json")


def parse_ids(raw):
    ids = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return ids


def load_state():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"⚠️ Не удалось прочитать {DATA_FILE}: {e}")
        return {}


_stored = load_state()
STATE_LOADED = bool(_stored)

# slug -> file_id видео для кнопок «Смотреть видео»
video_database = dict(_stored.get("videos", {}))

# Основной канал для постов. Можно задать заранее переменной CHANNEL_ID.
_env_channel = os.getenv("CHANNEL_ID", "")
CHANNEL_DATA = {"id": int(_env_channel) if _env_channel.lstrip("-").isdigit() else _stored.get("channel_id")}

# ID привязанных приватов. Заполняются командами /private_links и /private_here.
# Можно задать заранее переменной: PRIVATE_CHANNEL_IDS="-1001111111111,-1002222222222"
PRIVATE_CHANNELS = (parse_ids(os.getenv("PRIVATE_CHANNEL_IDS")) or list(_stored.get("private_channels", [])))[:PRIVATE_LIMIT]

# Автопосты: пул видео из привата, счётчик номера, дата последнего поста
AUTOPOST = {
    "enabled": True, "source_id": None, "counter": 0,
    "per_day": None,      # сколько постов в день; None = значение по умолчанию (переменная AUTOPOST_PER_DAY, иначе 1)
    "start_date": None,   # с какого дня работает расписание (при первом запуске это завтра)
    "last_date": None,    # день последнего поста
    "day_date": None, "day_count": 0,  # сколько постов уже вышло в день day_date
    "pool": [], "used": [],
}
AUTOPOST.update(_stored.get("autopost", {}))
if AUTOPOST["start_date"] is None and AUTOPOST["last_date"]:
    # данные из прошлой версии: расписание уже работало, пост в last_date уже вышел
    AUTOPOST["start_date"] = AUTOPOST["last_date"]
    AUTOPOST["day_date"], AUTOPOST["day_count"] = AUTOPOST["last_date"], 1
_env_source = os.getenv("AUTOPOST_SOURCE_ID", "")
if _env_source.lstrip("-").isdigit():
    AUTOPOST["source_id"] = int(_env_source)


def save_state():
    data = {
        "channel_id": CHANNEL_DATA["id"],
        "private_channels": PRIVATE_CHANNELS,
        "videos": video_database,
        "autopost": AUTOPOST,
    }
    tmp_path = DATA_FILE + ".tmp"
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, DATA_FILE)  # атомарно: файл не повредится, даже если бот упадёт посреди записи
    except Exception as e:
        print(f"⚠️ Не удалось сохранить {DATA_FILE}: {e}")


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


class AutopostStates(StatesGroup):
    importing = State()


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


_COVER_FILE_ID = None


async def get_cover_file_id(chat_id):
    """Картинка для постов без своей картинки: сама вотермарка.
    Загружаем один раз (через личку админа, сообщение сразу удаляем) и кэшируем file_id."""
    global _COVER_FILE_ID
    if _COVER_FILE_ID:
        return _COVER_FILE_ID
    if not os.path.exists(WATERMARK_PATH):
        return None
    try:
        temp = await bot.send_photo(chat_id=chat_id, photo=types.FSInputFile(WATERMARK_PATH))
        _COVER_FILE_ID = temp.photo[-1].file_id
        await bot.delete_message(chat_id=chat_id, message_id=temp.message_id)
    except Exception as e:
        print(f"Не удалось подготовить картинку-вотермарку: {e}")
        return None
    return _COVER_FILE_ID


async def prepare_video(file_id, upload_chat_id, tag, size=None):
    """Скачивает видео, накладывает вотермарку и загружает обратно в Telegram.
    Возвращает (file_id для выдачи, предупреждение или None).
    Файлы больше 20 МБ бот скачать не может, поэтому они уходят как есть, по оригинальному file_id."""
    input_path = f"input_{tag}.mp4"
    output_path = f"output_{tag}.mp4"
    too_big = "ℹ️ Видео больше 20 МБ: оно уйдёт без вотермарки."
    try:
        if size and size > BOT_API_DOWNLOAD_LIMIT:
            return file_id, too_big
        try:
            video_file = await bot.get_file(file_id)
        except TelegramBadRequest as e:
            if "too big" not in str(e).lower():
                raise
            return file_id, too_big
        await bot.download_file(video_file.file_path, input_path)

        # moviepy работает синхронно и долго, поэтому гоняем в отдельном потоке,
        # чтобы бот не «замерзал» для остальных пользователей
        success = await asyncio.to_thread(apply_watermark, input_path, output_path)
        final_path = output_path if success else input_path
        warning = None if success else "⚠️ Вотермарка на видео не наложилась (подробности в логах): оно уйдёт без неё."

        # Загружаем видео в Telegram и вытаскиваем нормальный file_id из облака
        temp_msg = await bot.send_video(chat_id=upload_chat_id, video=types.FSInputFile(final_path))
        new_file_id = temp_msg.video.file_id
        await bot.delete_message(chat_id=upload_chat_id, message_id=temp_msg.message_id)
        return new_file_id, warning
    finally:
        # Временные файлы чистим в любом случае, даже если что-то упало
        for path in (input_path, output_path):
            if os.path.exists(path):
                os.remove(path)


def build_caption(title, video_link, bot_username):
    # Текст идёт с parse_mode=HTML, поэтому экранируем спецсимволы (<, >, &)
    caption = f"┃ {html.escape(title)} ❞\n\n"
    if video_link and video_link.lower() != "нет":
        caption += f"ссылка на видео\n👇👇👇👇👇👇\n\n{html.escape(video_link)}\n\n"
    caption += f"<a href='{make_bot_link(bot_username, 'buy')}'>приват</a>"
    return caption


# --- ПРИВЯЗКА ПРИВАТОВ: /private_links ---
# Эти хендлеры должны стоять ВЫШЕ привязки основного канала: пока админ в режиме
# привязки приватов, пересланный пост должен попасть сюда, а не в основной канал.
async def private_binding_error(chat_id, title):
    """Текст ошибки, если бот не сможет создавать в этом чате одноразовые ссылки, иначе None.
    Проверяем сразу при привязке, а не после чьей-то оплаты."""
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
    except Exception as e:
        return f"❌ Не удалось проверить права бота в «{title}»: {e}\nДобавь бота админом и попробуй ещё раз."
    if member.status != "administrator" or not getattr(member, "can_invite_users", False):
        return f"❌ В «{title}» бот не админ или у него нет права «Приглашать пользователей». Выдай право и попробуй ещё раз."
    return None


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
        "Если приват — группа с темами (постов для пересылки там нет), закончи здесь через /done, "
        "а потом напиши /private_here прямо в этой группе: она добавится к уже привязанным.\n\n"
        "Закончить — /done, отменить — /cancel. "
        "Старая привязка заменится, только когда закончишь."
    )


async def commit_private_channels(message: types.Message, state: FSMContext, ids, note=""):
    PRIVATE_CHANNELS[:] = ids
    save_state()
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
    if chat.type not in ("channel", "supergroup"):
        await message.answer("Это не канал. Перешли пост из канала-привата:")
        return

    data = await state.get_data()
    ids = list(data.get("private_ids", []))
    if chat.id in ids:
        await message.answer("Этот приват уже добавлен. Перешли пост из другого или напиши /done.")
        return

    error = await private_binding_error(chat.id, chat.title)
    if error:
        await message.answer(error + "\nПотом перешли пост ещё раз.")
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
    await message.answer(
        "Перешли мне пост из канала-привата (именно пересылкой) или напиши /done, /cancel.\n"
        "Группа с темами: закончи через /done и напиши /private_here прямо в ней."
    )


@dp.message(Command("cancel"))
async def cancel_any(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.clear()
    await message.answer("❌ Отменено.")


# --- ПРИВЯЗКА ГРУППЫ-ПРИВАТА (в т.ч. с темами): /private_here ---
# У группы с темами нет «постов», которые можно переслать, поэтому админ пишет команду
# прямо в группе, а бот берёт ID из самого сообщения. Приват ДОБАВЛЯЕТСЯ к уже привязанным.
GROUP_ANONYMOUS_BOT_ID = 1087968824  # так Telegram подписывает анонимных админов


@dp.message(Command("private_here"))
async def bind_private_here(message: types.Message):
    user = message.from_user
    chat = message.chat

    if chat.type == "private":
        if user and user.id in ADMIN_IDS:
            await message.answer("Эту команду надо написать прямо в группе-привате, а не мне в личку.")
        return
    if user and user.id == GROUP_ANONYMOUS_BOT_ID:
        await message.answer("Ты пишешь анонимно (от имени группы), и я не вижу, кто ты. Отключи анонимность у админа и повтори.")
        return
    if not user or user.id not in ADMIN_IDS:
        return
    if chat.type not in ("group", "supergroup"):
        return

    if chat.id in PRIVATE_CHANNELS:
        await message.answer("Этот приват уже привязан.")
        return
    if len(PRIVATE_CHANNELS) >= PRIVATE_LIMIT:
        await message.answer(f"Уже привязано приватов: {PRIVATE_LIMIT}. Чтобы изменить, напиши мне в личку /private_links.")
        return

    error = await private_binding_error(chat.id, chat.title)
    if error:
        await message.answer(error)
        return

    PRIVATE_CHANNELS.append(chat.id)
    save_state()
    ids_str = ",".join(str(i) for i in PRIVATE_CHANNELS)
    await message.answer(
        f"✅ Приват «{chat.title}» привязан ({len(PRIVATE_CHANNELS)}/{PRIVATE_LIMIT}).\n\n"
        "Чтобы привязка переживала перезапуск бота, задай на хостинге переменную окружения:\n"
        f"PRIVATE_CHANNEL_IDS={ids_str}"
    )


# --- АВТОПОСТЫ: контент из привата (группы с темами) ---
# Бот копит видео из группы в «пул» (новые сообщения он видит сам, старые можно переслать ему командой
# /autopost import), а раз в день в случайное время выкладывает в канал одно случайное видео из пула
# с подписью «Приват контент #N». Картинка поста — вотермарка. Кнопка «Смотреть видео» работает как в обычных постах.
AUTOPOST_TZ_NAME = os.getenv("AUTOPOST_TZ", "Europe/Moscow")
try:
    AUTOPOST_TZ = ZoneInfo(AUTOPOST_TZ_NAME)
except Exception:
    print(f"⚠️ Часовой пояс «{AUTOPOST_TZ_NAME}» не найден, использую UTC")
    AUTOPOST_TZ, AUTOPOST_TZ_NAME = timezone.utc, "UTC"


def _parse_window(raw):
    try:
        start, end = (int(x) for x in (raw or "11-21").split("-"))
        if 0 <= start < end <= 24:
            return start, end
    except ValueError:
        pass
    return 11, 21


AUTOPOST_WINDOW = _parse_window(os.getenv("AUTOPOST_WINDOW"))  # часы, в которые может выйти пост, например "11-21"
MAX_PER_DAY = 20
try:
    DEFAULT_PER_DAY = min(MAX_PER_DAY, max(1, int(os.getenv("AUTOPOST_PER_DAY", "1"))))  # значение по умолчанию
except ValueError:
    DEFAULT_PER_DAY = 1
AUTOPOST_LOCK = asyncio.Lock()  # чтобы ручной запуск и расписание не пересеклись
IMPORT_STATS = {"added": 0, "dups": 0}
_SCHED = {"fail_date": None, "fails": 0, "next_retry": 0.0}


def autopost_now():
    return datetime.now(AUTOPOST_TZ)


def posts_per_day():
    return AUTOPOST.get("per_day") or DEFAULT_PER_DAY


def planned_times(day, n):
    """Время n автопостов на указанный день. Окно делится на n равных частей, в каждой берётся случайная минута,
    так что посты разнесены по дню. Времена одинаковы при перезапусках бота."""
    start_h, end_h = AUTOPOST_WINDOW
    seg = max(1, (end_h - start_h) * 60 // n)
    rng = random.Random(f"autopost-{day.isoformat()}-{n}")
    minutes = [start_h * 60 + i * seg + rng.randrange(seg) for i in range(n)]
    return [datetime(day.year, day.month, day.day, m // 60, m % 60, tzinfo=AUTOPOST_TZ) for m in minutes]


def done_today(today_iso):
    return AUTOPOST["day_count"] if AUTOPOST["day_date"] == today_iso else 0


def schedule_text(today):
    per_day = posts_per_day()
    start = AUTOPOST["start_date"]
    if start is None or today.isoformat() < start:
        first_day = datetime.fromisoformat(start).date() if start else today + timedelta(days=1)
        first = planned_times(first_day, per_day)[0]
        return f"расписание стартует {first:%d.%m в %H:%M}"
    done = done_today(today.isoformat())
    if done >= per_day:
        nxt = planned_times(today + timedelta(days=1), per_day)[0]
        return f"на сегодня всё (вышло {done} из {per_day}), следующий {nxt:%d.%m в %H:%M}"
    slots = planned_times(today, per_day)
    return "сегодня по расписанию " + ", ".join(f"{t:%H:%M}" for t in slots) + f" (уже вышло: {done})"


def add_to_pool(video):
    """Добавляет видео в пул. False, если оно там уже есть."""
    uid = video.file_unique_id
    if any(item["uid"] == uid for item in AUTOPOST["pool"]):
        return False
    AUTOPOST["pool"].append({"fid": video.file_id, "uid": uid, "size": video.file_size})
    save_state()
    return True


def pick_item(exclude):
    """Случайное видео, которое ещё не выходило в этом круге. Когда все вышли, начинается новый круг."""
    used = set(AUTOPOST["used"])
    candidates = [i for i in AUTOPOST["pool"] if i["uid"] not in exclude]
    fresh = [i for i in candidates if i["uid"] not in used]
    restarted = False
    if not fresh and candidates:
        AUTOPOST["used"] = []
        fresh = candidates
        restarted = True
    return (random.choice(fresh) if fresh else None), restarted


async def publish_autopost():
    """Публикует один автопост в основной канал. Возвращает (успех, текст для админа)."""
    async with AUTOPOST_LOCK:
        channel_id = get_channel_id()
        if not channel_id:
            return False, "Основной канал не привязан: перешли мне в личку любой пост из него."
        if not AUTOPOST["pool"]:
            return False, "В пуле нет видео. Напиши /autopost source в группе-привате и/или /autopost import мне в личку."

        bot_username = await get_bot_username()
        upload_chat = ADMIN_IDS[0]
        tried, last_error = set(), None

        for _ in range(5):
            item, restarted = pick_item(tried)
            if item is None:
                break
            tried.add(item["uid"])
            number = AUTOPOST["counter"] + 1

            # 1) готовим видео; если это видео не открылось (удалено и т.п.), пробуем другое
            try:
                new_file_id, warning = await prepare_video(item["fid"], upload_chat, "autopost", item.get("size"))
            except Exception as e:
                last_error = str(e)
                print(f"Автопост: не удалось подготовить видео {item['uid']}: {e}")
                continue

            # 2) публикуем; если не вышло здесь, дело не в видео, и другие пробовать бессмысленно
            slug = generate_random_slug()
            video_database[slug] = new_file_id  # запись должна быть до поста: кнопку могут нажать сразу
            caption = build_caption(f"Приват контент #{number}", None, bot_username)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎬 Смотреть видео", url=make_bot_link(bot_username, f"vid_{slug}"))]
            ])
            try:
                cover_id = await get_cover_file_id(upload_chat)
                if cover_id:
                    await bot.send_photo(chat_id=channel_id, photo=cover_id, caption=caption, reply_markup=kb, parse_mode="HTML")
                else:
                    await bot.send_message(chat_id=channel_id, text=caption, reply_markup=kb, parse_mode="HTML")
            except Exception as e:
                video_database.pop(slug, None)
                return False, f"Не удалось опубликовать пост в канал: {e}"

            AUTOPOST["counter"] = number
            AUTOPOST["used"].append(item["uid"])
            today = autopost_now().date().isoformat()
            if AUTOPOST["day_date"] != today:
                AUTOPOST["day_date"], AUTOPOST["day_count"] = today, 0
            AUTOPOST["day_count"] += 1
            AUTOPOST["last_date"] = today
            save_state()

            info = f"✅ Опубликовано: «Приват контент #{number}»."
            if warning:
                info += f"\n{warning}"
            if restarted:
                info += "\n🔁 Все видео из пула уже выходили, начался новый круг."
            return True, info

        return False, f"Не удалось подготовить видео: {last_error or 'в пуле нет подходящих'}"


async def autopost_tick():
    """Одна проверка расписания (вызывается раз в минуту)."""
    now = autopost_now()
    today = now.date().isoformat()

    if AUTOPOST["start_date"] is None:
        # Самый первый запуск: чтобы бот не выложил пост «с порога», расписание стартует с завтрашнего дня.
        AUTOPOST["start_date"] = (now.date() + timedelta(days=1)).isoformat()
        save_state()
        return
    if today < AUTOPOST["start_date"] or not AUTOPOST["enabled"]:
        return

    due = sum(1 for t in planned_times(now.date(), posts_per_day()) if t <= now)  # сколько слотов уже наступило
    if done_today(today) >= due:
        return
    if not AUTOPOST["pool"] or not get_channel_id():
        return  # ещё не настроено, ждём молча
    if _SCHED["fail_date"] == today and (_SCHED["fails"] >= 3 or time.time() < _SCHED["next_retry"]):
        return

    ok, info = await publish_autopost()
    if ok:
        _SCHED["fails"] = 0
        # слоты, пропущенные из-за простоя бота, не наверстываем пачкой постов
        AUTOPOST["day_count"] = max(AUTOPOST["day_count"], due)
        save_state()
        return
    if _SCHED["fail_date"] != today:
        _SCHED["fail_date"], _SCHED["fails"] = today, 0
    _SCHED["fails"] += 1
    _SCHED["next_retry"] = time.time() + 600  # повтор через 10 минут, максимум 3 попытки в день
    await notify_admins(f"⚠️ Автопост не удался (попытка {_SCHED['fails']}/3): {info}")


async def autopost_scheduler():
    while True:
        try:
            await autopost_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Ошибка планировщика автопостов: {e}")
        await asyncio.sleep(60)


def is_source_video(message: types.Message) -> bool:
    return bool(message.video) and AUTOPOST["source_id"] is not None and message.chat.id == AUTOPOST["source_id"]


@dp.message(is_source_video)
async def collect_source_video(message: types.Message):
    add_to_pool(message.video)


@dp.message(Command("autopost"))
async def autopost_cmd(message: types.Message, command: CommandObject, state: FSMContext):
    user, chat = message.from_user, message.chat
    if user and user.id == GROUP_ANONYMOUS_BOT_ID:
        await message.answer("Ты пишешь анонимно (от имени группы), и я не вижу, кто ты. Отключи анонимность у админа и повтори.")
        return
    if not user or user.id not in ADMIN_IDS:
        return

    parts = (command.args or "").split()
    action = parts[0].lower() if parts else "now"

    if action == "now":
        await message.answer("⏳ Готовлю автопост (вотермарка может занять время)...")
        ok, info = await publish_autopost()
        await message.answer(info)

    elif action == "status":
        pool = AUTOPOST["pool"]
        used = set(AUTOPOST["used"])
        fresh = sum(1 for i in pool if i["uid"] not in used)
        source = AUTOPOST["source_id"] or "не задан (напиши /autopost source в группе-привате)"
        await message.answer(
            f"Автопост: {'включён' if AUTOPOST['enabled'] else 'выключен'}\n"
            f"Источник контента: {source}\n"
            f"Видео в пуле: {len(pool)} (ещё не выходили в этом круге: {fresh})\n"
            f"Постов в день: {posts_per_day()} (изменить: /autopost perday N)\n"
            f"Расписание ({AUTOPOST_TZ_NAME}, окно {AUTOPOST_WINDOW[0]}:00–{AUTOPOST_WINDOW[1]}:00): "
            f"{schedule_text(autopost_now().date())}\n"
            f"Следующий номер: #{AUTOPOST['counter'] + 1}\n"
            f"Последний автопост: {AUTOPOST['last_date'] or 'ещё не было'}\n"
            f"Файл данных: {DATA_FILE}"
        )

    elif action in ("perday", "per_day"):
        if len(parts) < 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= MAX_PER_DAY:
            await message.answer(
                f"Сколько постов в день (от 1 до {MAX_PER_DAY})? Например: /autopost perday 3\n"
                f"Сейчас: {posts_per_day()}."
            )
            return
        AUTOPOST["per_day"] = int(parts[1])
        save_state()
        await message.answer(
            f"✅ Теперь автопостов в день: {AUTOPOST['per_day']}. Они выходят в случайное время "
            f"({AUTOPOST_TZ_NAME}, окно {AUTOPOST_WINDOW[0]}:00–{AUTOPOST_WINDOW[1]}:00), "
            f"{schedule_text(autopost_now().date())}."
        )

    elif action in ("on", "off"):
        AUTOPOST["enabled"] = action == "on"
        save_state()
        await message.answer("✅ Автопост включён." if AUTOPOST["enabled"] else "⏸ Автопост выключен (ручной /autopost продолжит работать).")

    elif action == "number":
        if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) < 1:
            await message.answer("Напиши номер, с которого продолжить, например: /autopost number 57")
            return
        AUTOPOST["counter"] = int(parts[1]) - 1
        save_state()
        await message.answer(f"✅ Следующий автопост будет «Приват контент #{parts[1]}».")

    elif action == "source":
        if chat.type not in ("group", "supergroup"):
            await message.answer("Эту команду надо писать прямо в группе-привате, откуда брать контент.")
            return
        try:
            member = await bot.get_chat_member(chat_id=chat.id, user_id=bot.id)
            is_admin = member.status == "administrator"
        except Exception:
            is_admin = False
        if not is_admin:
            await message.answer("Сделай бота админом этой группы, иначе он не видит сообщения, и повтори команду.")
            return
        AUTOPOST["source_id"] = chat.id
        save_state()
        await message.answer(
            "✅ Эта группа теперь источник контента для автопостов. Новые видео попадут в пул сами.\n"
            "Старые видео: напиши мне в личку /autopost import и перешли их."
        )

    elif action == "import":
        if chat.type != "private":
            await message.answer("Эту команду надо писать мне в личку.")
            return
        IMPORT_STATS["added"] = IMPORT_STATS["dups"] = 0
        await state.set_state(AutopostStates.importing)
        await message.answer(
            "Пересылай мне видео из группы-привата: можно выделить много сообщений и переслать пачкой. "
            "Я молчу, пока не напишешь /done (или /cancel, уже добавленное останется)."
        )

    else:
        await message.answer(
            "Команды автопоста:\n"
            "/autopost: выложить пост прямо сейчас (проверка или если долго не было постов)\n"
            "/autopost status: состояние и расписание\n"
            "/autopost perday N: сколько постов в день (от 1 до 20)\n"
            "/autopost on, /autopost off: включить или выключить расписание\n"
            "/autopost number N: следующий пост будет #N\n"
            "/autopost source: (в группе-привате) брать контент отсюда\n"
            "/autopost import: (в личке) добавить в пул старые видео пересылкой"
        )


@dp.message(AutopostStates.importing, F.video)
async def import_video(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    if add_to_pool(message.video):
        IMPORT_STATS["added"] += 1
    else:
        IMPORT_STATS["dups"] += 1


@dp.message(AutopostStates.importing, Command("done"))
async def import_done(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.clear()
    await message.answer(
        f"✅ Готово. Добавлено новых: {IMPORT_STATS['added']}, уже были: {IMPORT_STATS['dups']}. "
        f"Всего в пуле: {len(AUTOPOST['pool'])}."
    )


@dp.message(AutopostStates.importing, lambda m: not m.video and not (m.text or "").startswith("/"))
async def import_hint(message: types.Message):
    await message.answer("Пересылай мне видео или напиши /done, /cancel.")


# --- ПРИВЯЗКА КАНАЛА ---
# Работает только когда админ НЕ в процессе создания поста (StateFilter(None)):
# внутри /post пересланные видео и картинки из других каналов — это материал для поста,
# а не привязка. Плюс канал привязывается, только если бот там админ с правом публикации,
# поэтому случайная пересылка из чужого канала ничего не перепривяжет.
@dp.message(StateFilter(None), F.chat.type == "private", F.forward_from_chat)
async def handle_forwarded_channel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    chat = message.forward_from_chat
    if chat.type != "channel": return

    try:
        member = await bot.get_chat_member(chat_id=chat.id, user_id=bot.id)
        can_post = member.status == "administrator" and bool(getattr(member, "can_post_messages", False))
    except Exception:
        can_post = False
    if not can_post:
        await message.answer(
            f"⚠️ Канал «{chat.title}» не привязан: бота там нет или у него нет права публиковать сообщения.\n"
            "Если ты пересылаешь материал для поста, сначала напиши /post, а потом пересылай."
        )
        return

    CHANNEL_DATA["id"] = chat.id
    save_state()
    await message.answer(f"✅ Канал «{chat.title}» привязан для постов.\nID канала: {chat.id}\nТеперь можно создавать посты.")


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
    await state.update_data(file_id=message.video.file_id, video_size=message.video.file_size)
    text = "Видео получено."
    size = message.video.file_size
    if size and size > BOT_API_DOWNLOAD_LIMIT:
        text += f"\nℹ️ Оно весит {size / 1024 / 1024:.0f} МБ (больше 20 МБ), поэтому уйдёт без вотермарки."
    text += "\n\nТеперь отправь картинку для поста (или напиши «нет», тогда картинкой будет вотермарка):"
    await message.answer(text)
    await state.set_state(PostStates.waiting_for_photo)


@dp.message(PostStates.waiting_for_photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    await state.update_data(photo_file_id=message.photo[-1].file_id)
    await message.answer("Картинка получена. Напиши имя автора / описание (например: «влад сопляков»):")
    await state.set_state(PostStates.waiting_for_title)


@dp.message(PostStates.waiting_for_photo, F.text)
async def process_photo_skip(message: types.Message, state: FSMContext):
    if message.text.strip().lower() != "нет":
        await message.answer("Отправь картинку или напиши «нет»:")
        return
    await state.update_data(photo_file_id=None)
    await message.answer("Ок, картинкой будет вотермарка. Напиши имя автора / описание (например: «влад сопляков»):")
    await state.set_state(PostStates.waiting_for_title)


@dp.message(PostStates.waiting_for_title)
async def process_title(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Нужен текст. Напиши имя автора / описание:")
        return
    await state.update_data(title=message.text)
    await message.answer("Укажи ссылку на полное видео (если ссылок нет, напиши «нет»):")
    await state.set_state(PostStates.waiting_for_link)


@dp.message(PostStates.waiting_for_link)
async def process_link(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Нужен текст. Отправь ссылку или напиши «нет»:")
        return

    data = await state.get_data()
    video_link = message.text.strip()
    title = data['title']
    file_id = data['file_id']
    photo_file_id = data.get('photo_file_id')

    await message.answer("⏳ Обрабатываю видео и накладываю вотермарку...")

    user_id = message.from_user.id
    input_photo_path = f"input_{user_id}.jpg"
    output_photo_path = f"output_{user_id}.jpg"
    new_photo_id = None
    warnings = []

    try:
        new_file_id, warning = await prepare_video(file_id, user_id, str(user_id), data.get('video_size'))
        if warning:
            warnings.append(warning)

        if photo_file_id:
            # Своя картинка: та же вотермарка + тот же приём с file_id
            photo_file = await bot.get_file(photo_file_id)
            await bot.download_file(photo_file.file_path, input_photo_path)
            photo_ok = await asyncio.to_thread(apply_watermark_image, input_photo_path, output_photo_path)
            final_photo_path = output_photo_path if photo_ok else input_photo_path
            if not photo_ok:
                warnings.append("⚠️ Вотермарка на картинке не наложилась (подробности в логах): она уйдёт без неё.")

            temp_photo = await bot.send_photo(chat_id=user_id, photo=types.FSInputFile(final_photo_path))
            new_photo_id = temp_photo.photo[-1].file_id
            await bot.delete_message(chat_id=user_id, message_id=temp_photo.message_id)
        else:
            # Картинки нет («нет»): картинкой поста становится сама вотермарка
            new_photo_id = await get_cover_file_id(user_id)
    except Exception as e:
        await message.answer(f"❌ Не удалось обработать видео: {e}")
        await state.clear()
        return
    finally:
        for path in (input_photo_path, output_photo_path):
            if os.path.exists(path):
                os.remove(path)

    video_slug = generate_random_slug()
    video_database[video_slug] = new_file_id
    save_state()

    bot_username = await get_bot_username()
    caption = build_caption(title, video_link, bot_username)

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
    confirm_text = "👀 Выше предпросмотр поста. Публикуем в канал?"
    if warnings:
        confirm_text += "\n\n" + "\n".join(warnings)
    await message.answer(confirm_text, reply_markup=confirm_kb)
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
    scheduler = asyncio.create_task(autopost_scheduler())
    try:
        # Сообщение о запуске: сразу видно, сохранились ли данные после перезапуска
        note = "" if STATE_LOADED else (
            "\n\n⚠️ Сохранённых данных нет. Это нормально при первом запуске. Если запуск не первый, "
            "значит хостинг стирает диск: подключи постоянный том и укажи его в DATA_DIR."
        )
        await notify_admins(
            "🔄 Бот запущен.\n"
            f"Основной канал: {'привязан' if get_channel_id() else 'не привязан'}\n"
            f"Приваты: {len(PRIVATE_CHANNELS)} из {PRIVATE_LIMIT}\n"
            f"Видео в пуле автопостов: {len(AUTOPOST['pool'])}, автопост {'включён' if AUTOPOST['enabled'] else 'выключен'}"
            + note
        )
        await dp.start_polling(bot)
    finally:
        scheduler.cancel()


if __name__ == "__main__":
    asyncio.run(main())
