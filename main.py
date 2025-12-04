import os
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

# --- env ---
try:
    from dotenv import load_dotenv
    BASE_DIR = Path(__file__).resolve().parent
    load_dotenv(BASE_DIR / ".env")
    load_dotenv()  # также читаем переменные из системного окружения, если есть
except Exception:
    BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = os.getenv("ADMIN_ID", "")
ADMIN_ID = int(ADMIN_ID) if ADMIN_ID and ADMIN_ID.isdigit() else None
ADMIN_PHONE = os.getenv("ADMIN_PHONE", "+79780214881")
DIRECTOR_USERNAME = os.getenv("DIRECTOR_USERNAME")  # без @

# Пути и константы
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "enrollments.db")))
AGE_GROUPS = ("9–11 лет", "12–14 лет")
SCHEDULE = {
    "9–11 лет": "воскресенье 09:30–12:30 или 15:30–18:30",
    "12–14 лет": "воскресенье 12:30–15:30",
}
DEFAULT_GROUP_LIMIT = int(os.getenv("DEFAULT_GROUP_LIMIT", "10"))

if not BOT_TOKEN:
    raise RuntimeError("Please set BOT_TOKEN in environment (.env)")

# --- DB helpers ---

def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    # Настройки для стабильности и производительности SQLite
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS enrollments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                child_full TEXT NOT NULL,
                age_group TEXT NOT NULL,
                phone TEXT,
                tg_user_id INTEGER,
                tg_username TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS group_limits (
                age_group TEXT PRIMARY KEY,
                limit_value INTEGER NOT NULL
            );
            """
        )
        # Индекс для быстрых COUNT по группе
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_enrollments_age ON enrollments(age_group);"
        )
        for ag in AGE_GROUPS:
            cur = conn.execute("SELECT 1 FROM group_limits WHERE age_group=?", (ag,))
            if not cur.fetchone():
                conn.execute(
                    "INSERT INTO group_limits(age_group, limit_value) VALUES(?, ?)",
                    (ag, DEFAULT_GROUP_LIMIT),
                )


init_db()

# --- Keyboards ---

def main_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Записаться на пробное занятие")
    kb.button(text="Задать вопрос")
    kb.button(text="Контакты")
    kb.adjust(1, 2)  # первая строка: 1 кнопка, вторая: 2
    return kb.as_markup(resize_keyboard=True)


def phone_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="📱 Поделиться контактом", request_contact=True)
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def confirm_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="✅ Подтвердить")
    kb.button(text="✏️ Изменить")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

# --- Logic helpers ---

def build_chat_url() -> str | None:
    if DIRECTOR_USERNAME:
        return f"https://t.me/{DIRECTOR_USERNAME.lstrip('@')}"
    if ADMIN_ID:
        return f"tg://user?id={ADMIN_ID}"
    return None


def count_in_group(age_group: str) -> int:
    with connect_db() as c:
        return c.execute(
            "SELECT COUNT(*) FROM enrollments WHERE age_group=?",
            (age_group,),
        ).fetchone()[0]


def get_group_limit(age_group: str) -> int:
    with connect_db() as c:
        row = c.execute(
            "SELECT limit_value FROM group_limits WHERE age_group=?",
            (age_group,),
        ).fetchone()
    return int(row[0]) if row else DEFAULT_GROUP_LIMIT


def get_remaining(age_group: str) -> int:
    return max(get_group_limit(age_group) - count_in_group(age_group), 0)


def try_enroll(child_full: str, age_group: str, phone: str | None, user_id: int, username: str | None) -> bool:
    """Атомарная запись: блокируем транзакцию, проверяем лимит, вставляем."""
    with connect_db() as c:
        c.execute("BEGIN IMMEDIATE;")  # блокировка на запись, защищает от гонок
        current = c.execute(
            "SELECT COUNT(*) FROM enrollments WHERE age_group=?",
            (age_group,),
        ).fetchone()[0]
        limit_v = get_group_limit(age_group)
        if current >= limit_v:
            return False
        c.execute(
            "INSERT INTO enrollments (child_full, age_group, phone, tg_user_id, tg_username, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                child_full,
                age_group,
                phone,
                user_id,
                username,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        return True


def age_kb_dynamic() -> ReplyKeyboardMarkup:
    options = [ag for ag in AGE_GROUPS if get_remaining(ag) > 0]
    kb = ReplyKeyboardBuilder()
    for ag in options:
        kb.button(text=ag)
    if not options:
        kb.button(text="⬅️ В меню")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

# --- FSM ---
class Enroll(StatesGroup):
    name_full = State()
    age_group = State()
    phone = State()
    confirm = State()


# --- Router ---
router = Router()


# --- Start ---
@router.message(F.text.in_(["/start", "start", "Меню"]))
async def cmd_start(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("Привет! Я бот Академии Mr.Code.\nВыберите действие:", reply_markup=main_menu_kb())


# --- Запись на пробное ---
@router.message(F.text == "Записаться на пробное занятие")
async def start_enroll(m: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Enroll.phone)
    await m.answer("Поделитесь контактом телефона нажав на кнопку ниже:", reply_markup=phone_kb())


@router.message(Enroll.phone, F.contact)
async def set_phone_from_contact(m: Message, state: FSMContext) -> None:
    phone = m.contact.phone_number
    if m.contact.user_id is None or m.contact.user_id == m.from_user.id:
        await state.update_data(phone=phone)
        await state.set_state(Enroll.name_full)
        await m.answer("Введите имя и фамилию ребёнка в одной строке Например: Иван Петров", reply_markup=ReplyKeyboardRemove())
    else:
        await m.answer("Пожалуйста, поделитесь своим контактом.")


@router.message(Enroll.phone, F.text.regexp(r"^[\d\s\+\-\(\)]+$"))
async def set_phone_manual(m: Message, state: FSMContext) -> None:
    phone = m.text.strip()
    digits = ''.join(filter(str.isdigit, phone))
    if len(digits) < 10:
        await m.answer("Номер телефона слишком короткий. Введите корректный номер.")
        return
    await state.update_data(phone=phone)
    await state.set_state(Enroll.name_full)
    await m.answer("Введите имя и фамилию ребёнка в одной строке Например: Иван Петров", reply_markup=ReplyKeyboardRemove())


@router.message(Enroll.phone)
async def invalid_phone(m: Message, state: FSMContext) -> None:
    await m.answer("Пожалуйста, поделитесь контактом или введите номер телефона.\n\nФормат: +79001234567 или 89001234567")


@router.message(Enroll.name_full)
async def set_name_full(m: Message, state: FSMContext) -> None:
    text = " ".join(m.text.split())
    parts = text.split(" ")
    if len(parts) < 2:
        await m.answer("Пожалуйста, укажите имя и фамилию. Например: Иван Петров")
        return
    first, last = parts[0], " ".join(parts[1:])
    await state.update_data(child_full=f"{first} {last}")
    await state.set_state(Enroll.age_group)

    left = {ag: get_remaining(ag) for ag in AGE_GROUPS}
    if all(v == 0 for v in left.values()):
        await m.answer("К сожалению, сейчас во всех группах мест нет. Напишите нам или попробуйте позже.", reply_markup=main_menu_kb())
        return

    lines = ["Выберите возрастную группу"]
    for ag in AGE_GROUPS:
        if left[ag] > 0:
            lines.append(f"{ag} — {SCHEDULE[ag]}")
    await m.answer("\n".join(lines), reply_markup=age_kb_dynamic())


@router.message(Enroll.age_group, F.text.in_(AGE_GROUPS))
async def set_age(m: Message, state: FSMContext) -> None:
    await state.update_data(age_group=m.text)
    await state.set_state(Enroll.confirm)
    d = await state.get_data()
    text = (
        "Проверьте данные:\n"
        f"👦 Имя и фамилия: {d['child_full']}\n"
        f"🎯 Возраст: {d['age_group']}\n"
        f"📱 Телефон: {d.get('phone', 'не указан')}"
    )
    await m.answer(text, reply_markup=confirm_kb())


@router.message(Enroll.confirm, F.text == "✅ Подтвердить")
async def confirm(m: Message, state: FSMContext) -> None:
    d = await state.get_data()

    ok = try_enroll(
        child_full=d["child_full"],
        age_group=d["age_group"],
        phone=d.get("phone"),
        user_id=m.from_user.id,
        username=m.from_user.username,
    )
    if not ok:
        # Мест нет — показываем актуальные остатки
        left = {ag: get_remaining(ag) for ag in AGE_GROUPS}
        if all(v == 0 for v in left.values()):
            await m.answer("Сейчас во всех группах мест нет. Попробуйте позже или свяжитесь с нами.", reply_markup=main_menu_kb())
            return
        lines = ["Выберите возрастную группу"]
        for ag in AGE_GROUPS:
            if left[ag] > 0:
                lines.append(f"{ag} — осталось мест: {left[ag]}")
        await m.answer("\n".join(lines), reply_markup=age_kb_dynamic())
        await state.set_state(Enroll.age_group)
        return

    # Уведомление администратору (если указан)
    if ADMIN_ID:
        try:
            phone_info = d.get("phone", "не указан")
            created_at = datetime.utcnow().isoformat(timespec="seconds")
            await m.bot.send_message(
                ADMIN_ID,
                (
                    "🆕 Новая заявка\n"
                    f"👦 {d['child_full']}\n"
                    f"🎯 {d['age_group']}\n"
                    f"📱 Телефон: {phone_info}\n"
                    f"👤 @{'-' if not m.from_user.username else m.from_user.username} (id {m.from_user.id})\n"
                    f"🕒 {created_at} UTC"
                ),
            )
        except Exception as e:
            logging.warning(f"Failed to notify admin: {e}")

    await m.answer("✅ Заявка отправлена! Мы с вами свяжемся.", reply_markup=main_menu_kb())
    await state.clear()


@router.message(Enroll.confirm, F.text == "✏️ Изменить")
async def edit(m: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Enroll.phone)
    await m.answer("Ок, начнём заново. Поделитесь контактом телефона или введите номер вручную:", reply_markup=phone_kb())


# --- Задать вопрос ---
@router.message(F.text == "Задать вопрос")
async def ask_question(m: Message, state: FSMContext) -> None:
    await state.clear()
    chat_url = build_chat_url()
    if chat_url:
        kb = InlineKeyboardBuilder()
        kb.button(text="Написать в Telegram", url=chat_url)
        kb.adjust(1)
        await m.answer("Нажмите, чтобы написать в Telegram:", reply_markup=kb.as_markup())
    else:
        await m.answer("Не удалось сформировать ссылку. Укажите DIRECTOR_USERNAME в .env или ADMIN_ID.")


# --- Контакты ---
@router.message(F.text == "Контакты")
async def contacts(m: Message, state: FSMContext) -> None:
    await state.clear()
    chat_url = build_chat_url()

    kb = InlineKeyboardBuilder()
    if chat_url:
        kb.button(text="Написать в Telegram", url=chat_url)
        kb.adjust(1)

    # Карточка контакта — из неё можно позвонить нативно
    phone_clean = ADMIN_PHONE.replace("+", "").replace(" ", "").replace("-", "")
    try:
        await m.answer_contact(phone_number=phone_clean, first_name="Директор")
    except Exception as e:
        logging.warning(f"Failed to send contact: {e}")

    await m.answer("Контакты директора:", reply_markup=kb.as_markup() if chat_url else None)


# --- Fallback ---
@router.message()
async def fallback(m: Message) -> None:
    await m.answer("Выберите действие в меню:", reply_markup=main_menu_kb())


# --- App bootstrap ---
async def main() -> None:
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
