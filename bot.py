import os
import io
import re
import time
import string
import random
import logging
import tempfile
import gzip
import json
import sqlite3
from pathlib import Path

import libsql_experimental as libsql

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, InputFile, InputSticker
)
from telegram.request import HTTPXRequest
from telegram.error import RetryAfter, BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

from sticker_gen import (
    generate_sticker,
    _find_text_layer,
    _collect_all_groups,
    LAYER_TYPE_PIXEL,
    LAYER_TYPE_GLYPH,
    LAYER_TYPE_BLOB,
)
from gallery_render import make_gallery_image

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN         = os.getenv("TELEGRAM_TOKEN")
TURSO_URL     = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN   = os.getenv("TURSO_AUTH_TOKEN")
BOT_USERNAME  = os.getenv("BOT_USERNAME", "emojieditoryahimkibot")

ADMIN_ID = 8572202921

BASE_DIR      = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
SHARED_DIR    = TEMPLATES_DIR / "shared"
SETS_DIR      = BASE_DIR / "sets"
FONTS_DIR     = BASE_DIR / "fonts"
TEMPLATES_DIR.mkdir(exist_ok=True)
SHARED_DIR.mkdir(exist_ok=True)
SETS_DIR.mkdir(exist_ok=True)
FONTS_DIR.mkdir(exist_ok=True)

MAX_PAGES   = 9
PER_PAGE    = 12
CHANNEL     = "@sexoez"
PRICE_STARS = 2
MAX_TOPUP   = 100000
MAX_SELECT  = 12

RECOLOR_KEY = "recolor"

CATEGORIES = [
    ("main1",    "🎨", "Основной 1"),
    ("main2",    "🎨", "Основной 2"),
    ("main3",    "🎨", "Основной 3"),
    ("main4",    "🎨", "Основной 4"),
    ("recolor",  "🌈", "Перекраска"),
    ("passport", "🛂", "Паспорт"),
]

FONTS = [
    ("Caveat-Bold",        "1. Caveat"),
    ("EBGaramond-Bold",    "2. EBGaramond"),
    ("JetBrainsMono-Bold", "3. JetBrainsMono"),
    ("Montserrat-Bold",    "4. Montserrat"),
    ("NotoSans-Bold",      "5. NotoSans"),
    ("Oswald-Bold",        "6. Oswald"),
    ("Pacifico-Regular",   "7. Pacifico"),
    ("PressStart2P-Regular","8. PressStart2P"),
]

COLORS = [
    ("⚪ Белый",     "#ffffff"),
    ("⚫ Чёрный",    "#000000"),
    ("🔴 Красный",   "#ff3b30"),
    ("🟠 Оранжевый", "#ff9500"),
    ("🟡 Жёлтый",    "#ffd60a"),
    ("🟢 Зелёный",   "#34c759"),
    ("🔵 Синий",     "#007aff"),
    ("🟣 Фиолетовый","#af52de"),
    ("🩷 Розовый",   "#ff2d92"),
    ("🩵 Голубой",   "#5ac8fa"),
]


def _category_dir(key: str) -> Path:
    return SHARED_DIR / key


def _category_templates(key: str):
    d = _category_dir(key)
    if not d.exists():
        return []
    files = list(d.glob("*.tgs"))

    def sort_key(p):
        m = re.search(r"(\d+)", p.stem)
        if m:
            return (0, int(m.group(1)), p.stem)
        return (1, 0, p.stem)

    files.sort(key=sort_key)
    return [(p.stem, p) for p in files]


def _category_info(key: str):
    for k, emoji, name in CATEGORIES:
        if k == key:
            return emoji, name
    return "📁", key


def _font_path(key: str) -> Path | None:
    for fname, _label in FONTS:
        if fname == key:
            p = FONTS_DIR / f"{fname}.ttf"
            if p.exists():
                return p
            p2 = FONTS_DIR / f"{fname}.otf"
            if p2.exists():
                return p2
    return None


# ─── БД (Turso) ─────────────────────────────────────────────────────────────

def db_init():
    con = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            balance     INTEGER NOT NULL DEFAULT 0,
            generated   INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            path        TEXT NOT NULL,
            text        TEXT,
            template    TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            key   TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.commit()
    con.close()


def db():
    con = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    con.row_factory = sqlite3.Row
    return con


def remember_user(user):
    if user is None:
        return
    con = db()
    try:
        con.execute("""
            INSERT INTO users (user_id, username, first_name, balance)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
        """, (user.id, user.username or "", user.first_name or ""))
        con.commit()
    finally:
        con.close()


def find_user_by_username(username: str):
    if not username:
        return None
    u = username.lstrip("@").lower()
    con = db()
    try:
        row = con.execute(
            "SELECT user_id FROM users WHERE LOWER(username) = ? LIMIT 1", (u,)
        ).fetchone()
        return row["user_id"] if row else None
    finally:
        con.close()


def get_balance(user_id: int) -> int:
    con = db()
    try:
        row = con.execute("SELECT balance FROM users WHERE user_id = ?",
                          (user_id,)).fetchone()
        return row["balance"] if row else 0
    finally:
        con.close()


def add_balance(user_id: int, amount: int):
    con = db()
    try:
        con.execute("""
            INSERT INTO users (user_id, balance) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?
        """, (user_id, amount, amount))
        con.commit()
    finally:
        con.close()


def spend_balance(user_id: int, amount: int) -> bool:
    con = db()
    try:
        row = con.execute("SELECT balance FROM users WHERE user_id = ?",
                          (user_id,)).fetchone()
        bal = row["balance"] if row else 0
        if bal < amount:
            return False
        con.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?",
                    (amount, user_id))
        con.commit()
        return True
    finally:
        con.close()


def inc_generated(user_id: int):
    con = db()
    try:
        con.execute("UPDATE users SET generated = generated + 1 WHERE user_id = ?",
                    (user_id,))
        con.commit()
    finally:
        con.close()


def stat_inc(key: str, amount: int = 1):
    con = db()
    try:
        con.execute("""
            INSERT INTO stats (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = value + ?
        """, (key, amount, amount))
        con.commit()
    finally:
        con.close()


def stat_get(key: str) -> int:
    con = db()
    try:
        row = con.execute("SELECT value FROM stats WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else 0
    finally:
        con.close()


def count_users() -> int:
    con = db()
    try:
        return con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    finally:
        con.close()


def list_users(limit: int = 50):
    con = db()
    try:
        return con.execute(
            "SELECT * FROM users ORDER BY user_id LIMIT ?", (limit,)
        ).fetchall()
    finally:
        con.close()


def get_all_user_ids():
    con = db()
    try:
        return [r["user_id"] for r in con.execute("SELECT user_id FROM users").fetchall()]
    finally:
        con.close()


def add_set(user_id: int, path: str, text: str, template_name: str):
    con = db()
    try:
        con.execute(
            "INSERT INTO sets (user_id, path, text, template) VALUES (?, ?, ?, ?)",
            (user_id, path, text, template_name))
        con.commit()
    finally:
        con.close()


def list_sets(user_id: int):
    con = db()
    try:
        return con.execute(
            "SELECT * FROM sets WHERE user_id = ? ORDER BY id DESC",
            (user_id,)
        ).fetchall()
    finally:
        con.close()


def clear_sets(user_id: int):
    con = db()
    try:
        rows = con.execute("SELECT path FROM sets WHERE user_id = ?",
                           (user_id,)).fetchall()
        for r in rows:
            try:
                Path(r["path"]).unlink(missing_ok=True)
            except Exception:
                pass
        con.execute("DELETE FROM sets WHERE user_id = ?", (user_id,))
        con.commit()
    finally:
        con.close()


# ─── Утилиты ────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID) and user_id == ADMIN_ID


def _personal_path(user_id: int) -> Path:
    return TEMPLATES_DIR / f"{user_id}.tgs"


def _describe_template(path: Path) -> str:
    try:
        with gzip.open(str(path), "rb") as f:
            data = json.load(f)
        _, ltype, extra = _find_text_layer(data)
        if ltype == LAYER_TYPE_PIXEL:
            return f"пиксельный ({extra['n_cols']}x{extra['n_rows']})"
        if ltype == LAYER_TYPE_BLOB:
            return f"векторный ({extra['n_sh']} контуров)"
        groups = _collect_all_groups(extra["main_shape"])
        return f"глифовый ({len(groups)} букв)"
    except Exception:
        return "изображение"


async def check_subscription(bot, user_id: int) -> bool:
    if is_admin(user_id):
        return True
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


async def send_topup_invoice(bot, chat_id: int, amount: int):
    await bot.send_invoice(
        chat_id=chat_id,
        title="Пополнение баланса",
        description=f"{amount} ⭐ на баланс",
        payload=f"topup_{amount}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice("Пополнение", amount)],
    )


def _sanitize_pack_name(raw: str) -> str:
    """Приводит ввод к допустимому имени пака (латиница, цифры, _)."""
    s = raw.strip().lower()
    s = re.sub(r"[^a-z0-9_]", "_", s)
    s = re.sub(r"__+", "_", s).strip("_")
    if not s or not s[0].isalpha():
        s = "pack_" + s
    suffix = f"_by_{BOT_USERNAME}"
    max_len = 64 - len(suffix)
    if len(s) > max_len:
        s = s[:max_len].rstrip("_")
    return s + suffix


# ─── Клавиатуры ─────────────────────────────────────────────────────────────

def _sub_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал",
                              url=f"https://t.me/{CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub")],
    ])


def _main_menu(user_id: int):
    rows = []
    for key, emoji, name in CATEGORIES:
        if _category_templates(key):
            rows.append([InlineKeyboardButton(
                f"{emoji} {name}",
                callback_data=f"cat:{key}:0"
            )])

    rows.append([InlineKeyboardButton("📦 Ваши стикеры", callback_data="my_sets")])
    rows.append([InlineKeyboardButton("⭐ Пополнить",    callback_data="topup")])
    rows.append([InlineKeyboardButton("ℹ️ Помощь",       callback_data="help")])

    if is_admin(user_id):
        rows.append([InlineKeyboardButton("🛠 Админ-панель", callback_data="admin")])

    return InlineKeyboardMarkup(rows)


def _admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton("⭐ Выдать звёзды", callback_data="adm_give_stars")],
        [InlineKeyboardButton("👥 Пользователи", callback_data="adm_users")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton("📁 Файлы shared", callback_data="adm_files")],
        [InlineKeyboardButton("🏠 В меню", callback_data="main")],
    ])


def _gallery_keyboard(cat: str, page: int, total_pages: int,
                      selected: set, count_on_page: int):
    rows = []
    row = []
    for i in range(count_on_page):
        idx_global = page * PER_PAGE + i
        mark = "✅" if idx_global in selected else "⬜"
        row.append(InlineKeyboardButton(
            f"{mark} {i+1}", callback_data=f"toggle:{cat}:{page}:{i}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Навигация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"cat:{cat}:{page-1}"))

    start = max(0, page - 2)
    end = min(total_pages, start + 5)
    start = max(0, end - 5)
    for p in range(start, end):
        if p == page:
            nav.append(InlineKeyboardButton(f"· {p+1} ·", callback_data="noop"))
        else:
            nav.append(InlineKeyboardButton(str(p + 1),
                                            callback_data=f"cat:{cat}:{p}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"cat:{cat}:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton("✅ Выбрать всё", callback_data=f"sel_all:{cat}:{page}"),
        InlineKeyboardButton("❌ Снять всё",   callback_data=f"desel:{cat}:{page}"),
    ])
    rows.append([
        InlineKeyboardButton("💳 Оплатить", callback_data="pay_start"),
        InlineKeyboardButton("🏠 В меню",   callback_data="main"),
    ])
    return InlineKeyboardMarkup(rows)


def _font_keyboard():
    rows = []
    row = []
    for i, (fkey, label) in enumerate(FONTS):
        p = _font_path(fkey)
        mark = "" if p else "❓"
        row.append(InlineKeyboardButton(f"{mark} {label}",
                                        callback_data=f"font:{fkey}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬆️ Загрузить .ttf/.otf",
                                      callback_data="font_upload")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="main")])
    return InlineKeyboardMarkup(rows)


def _color_keyboard(target: str):
    rows = []
    row = []
    for i, (label, hex_color) in enumerate(COLORS):
        row.append(InlineKeyboardButton(
            label,
            callback_data=f"color:{target}:{hex_color.lstrip('#')}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="main")])
    return InlineKeyboardMarkup(rows)


def _recolor_panel_keyboard(colors: dict):
    bg = colors.get("bg", "не выбран")
    shape = colors.get("shape", "не выбран")
    outline = colors.get("outline", "не выбран")
    text = colors.get("text", "не выбран")

    def label(name, val):
        return f"{name}: {val if val != 'не выбран' else 'не выбран'}"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label("🖼️ Фон", bg),       callback_data="pick_color:bg"),
         InlineKeyboardButton(label("🧍 Персонаж", shape), callback_data="pick_color:shape")],
        [InlineKeyboardButton(label("🖌 Контур", outline), callback_data="pick_color:outline"),
         InlineKeyboardButton(label("✏️ Текст", text),     callback_data="pick_color:text")],
        [InlineKeyboardButton("✅ Готово",               callback_data="recolor_done")],
        [InlineKeyboardButton("❌ Отмена",                callback_data="main")],
    ])


def _back_menu():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠 В меню", callback_data="main")]])


# ─── /start ─────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logger.info("HANDLER: /start")
    user = update.effective_user
    remember_user(user)
    user_id = user.id

    if not await check_subscription(ctx.bot, user_id):
        await update.message.reply_text(
            f"📢 Чтобы пользоваться ботом, подпишитесь на канал {CHANNEL}.",
            reply_markup=_sub_keyboard())
        return

    bal = get_balance(user_id)
    text = (
        "✨ <b>Edit Emoji Bot</b> ✨\n\n"
        f"Привет, {user.first_name}!\n\n"
        "🎨 Выбери категорию, шаблоны (галочками) и текст.\n"
        f"📦 Максимум за раз: {MAX_SELECT} стикеров\n"
        f"💰 Цена: {PRICE_STARS} ⭐ за стикер\n"
        f"⭐ <b>Баланс:</b> {bal}"
    )
    await update.message.reply_text(text, reply_markup=_main_menu(user_id),
                                    parse_mode="HTML")


# ─── Callback ───────────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    logger.info("CALLBACK: %r", q.data)

    data = q.data or ""
    user = q.from_user
    remember_user(user)
    user_id = user.id

    if data == "check_sub":
        if await check_subscription(ctx.bot, user_id):
            await q.answer("✅ Подписка подтверждена!")
            await q.message.reply_text(
                f"✅ Спасибо!\n⭐ Баланс: {get_balance(user_id)}",
                reply_markup=_main_menu(user_id))
        else:
            await q.answer("❌ Вы ещё не подписаны", show_alert=True)
        return

    if not await check_subscription(ctx.bot, user_id):
        await q.answer("❌ Сначала подпишитесь на канал", show_alert=True)
        return

    # Выбор шрифта
    if data.startswith("font:"):
        await q.answer()
        fkey = data.split(":", 1)[1]
        p = _font_path(fkey)
        if not p:
            await q.message.reply_text(
                f"❌ Шрифт <code>{fkey}</code> не найден.\n"
                f"Скачай его в папку <code>fonts/</code>.",
                parse_mode="HTML")
            return
        ctx.user_data["font_path"] = str(p)
        ctx.user_data["font_label"] = fkey
        await q.message.reply_text(
            f"✅ Шрифт <b>{fkey}</b> выбран.\n\n"
            f"Теперь напиши текст (до 12 символов) — сгенерирую все выбранные.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 В меню", callback_data="main")]]))
        return

    if data == "font_upload":
        await q.answer()
        ctx.user_data["awaiting_font"] = True
        await q.message.reply_text(
            "⬆️ Отправь .ttf или .otf файл — сохраню как шрифт.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Отмена", callback_data="main")]]))
        return

    if data == "pay_start":
        await q.answer()
        selected = ctx.user_data.get("selected_templates", [])
        if not selected:
            await q.message.reply_text("❌ Сначала выбери хотя бы один шаблон.")
            return
        price = PRICE_STARS * len(selected)
        # Списываем сразу (можно перенести после оплаты)
        await q.message.reply_text(
            f"💳 Оплата: <b>{price} ⭐</b> за {len(selected)} стикер(ов).\n\n"
            f"Напиши текст (до 12 символов) — сгенерирую.",
            parse_mode="HTML",
            reply_markup=_font_keyboard())
        return

    # Админка — упрощённая
    if data.startswith("adm") or data == "admin":
        if not is_admin(user_id):
            await q.answer("❌ Нет доступа", show_alert=True)
            return
        await q.answer()

        if data == "admin":
            await q.message.reply_text("🛠 <b>Админ-панель</b>",
                                       parse_mode="HTML",
                                       reply_markup=_admin_keyboard())
            return

        if data == "adm_stats":
            lines = ["📊 <b>Статистика</b>\n"]
            lines.append(f"👥 Пользователей: <b>{count_users()}</b>")
            lines.append(f"🎨 Сгенерировано: <b>{stat_get('generated')}</b>")
            lines.append(f"⭐ Звёзд: <b>{stat_get('stars')}</b>\n")
            for key, emoji, name in CATEGORIES:
                cnt = len(_category_templates(key))
                lines.append(f"  {emoji} {name}: {cnt}")
            await q.message.reply_text("\n".join(lines), parse_mode="HTML",
                                       reply_markup=_admin_keyboard())
            return

        if data == "adm_files":
            lines = ["📁 <b>Файлы shared/</b>\n"]
            for key, emoji, name in CATEGORIES:
                d = _category_dir(key)
                cnt = len(list(d.glob("*.tgs"))) if d.exists() else 0
                lines.append(f"{emoji} <b>{name}</b>: {cnt}")
            await q.message.reply_text("\n".join(lines), parse_mode="HTML",
                                       reply_markup=_admin_keyboard())
            return

        if data in ("adm_give_stars", "adm_users", "adm_broadcast"):
            await q.message.reply_text("🚧 Функция в разработке.",
                                       reply_markup=_admin_keyboard())
            return

    # Мультивыбор шаблонов
    if data.startswith("toggle:"):
        await q.answer()
        try:
            _, cat, page_s, idx_s = data.split(":")
            page = int(page_s); idx = int(idx_s)
        except (ValueError, IndexError):
            return
        idx_global = page * PER_PAGE + idx
        selected = set(ctx.user_data.get("selected_templates", []))
        if idx_global in selected:
            selected.discard(idx_global)
        else:
            if len(selected) >= MAX_SELECT:
                await q.answer(f"Максимум {MAX_SELECT} шаблонов", show_alert=True)
                return
            selected.add(idx_global)
        ctx.user_data["selected_templates"] = list(selected)
        # Перерисовываем клавиатуру
        templates = _category_templates(cat)
        total_pages = min(MAX_PAGES, (len(templates) + PER_PAGE - 1) // PER_PAGE)
        kb = _gallery_keyboard(cat, page, total_pages, selected, PER_PAGE)
        try:
            await q.edit_message_reply_markup(reply_markup=kb)
        except Exception:
            pass
        return

    if data.startswith("sel_all:"):
        await q.answer("Выбрано всё на странице")
        _, cat, page_s = data.split(":")
        page = int(page_s)
        templates = _category_templates(cat)
        selected = set(ctx.user_data.get("selected_templates", []))
        start = page * PER_PAGE
        end = min(start + PER_PAGE, len(templates))
        for i in range(start, end):
            if len(selected) >= MAX_SELECT:
                break
            selected.add(i)
        ctx.user_data["selected_templates"] = list(selected)
        total_pages = min(MAX_PAGES, (len(templates) + PER_PAGE - 1) // PER_PAGE)
        kb = _gallery_keyboard(cat, page, total_pages, selected, PER_PAGE)
        try:
            await q.edit_message_reply_markup(reply_markup=kb)
        except Exception:
            pass
        return

    if data.startswith("desel:"):
        await q.answer("Снято")
        _, cat, page_s = data.split(":")
        page = int(page_s)
        templates = _category_templates(cat)
        selected = set(ctx.user_data.get("selected_templates", []))
        start = page * PER_PAGE
        end = min(start + PER_PAGE, len(templates))
        for i in range(start, end):
            selected.discard(i)
        ctx.user_data["selected_templates"] = list(selected)
        total_pages = min(MAX_PAGES, (len(templates) + PER_PAGE - 1) // PER_PAGE)
        kb = _gallery_keyboard(cat, page, total_pages, selected, PER_PAGE)
        try:
            await q.edit_message_reply_markup(reply_markup=kb)
        except Exception:
            pass
        return

    # Показ галереи
    if data.startswith("cat:"):
        try:
            _, cat, page_s = data.split(":")
            page = int(page_s)
        except (ValueError, IndexError):
            await q.answer("Ошибка", show_alert=True)
            return

        templates = _category_templates(cat)
        if not templates:
            await q.answer()
            await q.message.reply_text("📭 Нет шаблонов.",
                                       reply_markup=_back_menu())
            return

        total_pages = min(MAX_PAGES, (len(templates) + PER_PAGE - 1) // PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        start_idx = page * PER_PAGE
        page_items = templates[start_idx:start_idx + PER_PAGE]
        paths = [p for _, p in page_items]

        try:
            png_path = make_gallery_image(
                paths, page_number=page + 1, total_pages=total_pages)
        except Exception:
            logger.exception("Ошибка рендера")
            await q.answer("Ошибка галереи", show_alert=True)
            return

        try:
            await q.answer()
        except Exception:
            pass

        emoji, cat_name = _category_info(cat)
        selected = set(ctx.user_data.get("selected_templates", []))
        caption = (f"{emoji} <b>{cat_name}</b> · стр. {page+1}/{total_pages}\n"
                   f"Всего: {len(templates)} · Выбрано: {len(selected)}")
        kb = _gallery_keyboard(cat, page, total_pages, selected, len(page_items))

        try:
            with open(png_path, "rb") as f:
                await q.message.reply_photo(
                    photo=InputFile(f, filename="gallery.png"),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=kb,
                    write_timeout=60, read_timeout=60, connect_timeout=30)
        except Exception:
            logger.exception("Фото не ушло")
        return

    if data == "topup":
        await q.answer()
        await q.message.reply_text(
            "⭐ <b>Пополнение баланса</b>\n\nВыбери сумму:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ 10",  callback_data="buy:10"),
                 InlineKeyboardButton("⭐ 50",  callback_data="buy:50")],
                [InlineKeyboardButton("⭐ 100", callback_data="buy:100"),
                 InlineKeyboardButton("⭐ 500", callback_data="buy:500")],
                [InlineKeyboardButton("🏠 В меню", callback_data="main")],
            ]))
        return

    if data.startswith("buy:"):
        await q.answer()
        try:
            amount = int(data.split(":", 1)[1])
        except (IndexError, ValueError):
            return
        if amount < 1 or amount > MAX_TOPUP:
            return
        try:
            await send_topup_invoice(ctx.bot, user_id, amount)
        except Exception as e:
            await q.message.reply_text(f"❌ Ошибка: {e}")
        return

    if data == "main":
        await q.answer()
        await q.message.reply_text(
            f"✨ <b>Меню</b>\n⭐ Баланс: {get_balance(user_id)}",
            parse_mode="HTML", reply_markup=_main_menu(user_id))
        return

    if data == "help":
        await q.answer()
        await q.message.reply_text(
            "ℹ️ Выбери категорию, отметь шаблоны (✅), затем напиши текст.\n\n"
            "После текста выбери шрифт — бот сгенерирует все выбранные стикеры.",
            reply_markup=_back_menu())
        return

    if data == "my_sets":
        await q.answer()
        sets = list_sets(user_id)
        if not sets:
            await q.message.reply_text("📦 Пусто.", reply_markup=_back_menu())
            return
        await q.message.reply_text(
            f"📦 Сохранено: <b>{len(sets)}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Очистить", callback_data="my_sets_clear")],
                [InlineKeyboardButton("🏠 В меню", callback_data="main")],
            ]))
        return

    if data == "my_sets_clear":
        await q.answer("Очищено")
        clear_sets(user_id)
        await q.message.reply_text("🗑 Удалено.", reply_markup=_back_menu())
        return

    if data == "noop":
        await q.answer()
        return

    await q.answer()


# ─── Payment ────────────────────────────────────────────────────────────────

async def precheckout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    payload = q.invoice_payload or ""
    if not payload.startswith("topup_"):
        await q.answer(ok=False, error_message="Неизвестный платёж")
        return
    try:
        amount = int(payload.split("_", 1)[1])
    except (IndexError, ValueError):
        await q.answer(ok=False, error_message="Некорректная сумма")
        return
    await q.answer(ok=True)


async def successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    remember_user(user)
    payload = update.message.successful_payment.invoice_payload or ""
    if payload.startswith("topup_"):
        try:
            amount = int(payload.split("_", 1)[1])
        except (IndexError, ValueError):
            amount = 0
        if amount > 0:
            add_balance(user.id, amount)
            stat_inc("stars", amount)
            await update.message.reply_text(
                f"✅ Баланс пополнен на {amount} ⭐!\n"
                f"⭐ Баланс: {get_balance(user.id)}",
                reply_markup=_main_menu(user.id))
            return
    await update.message.reply_text("✅ Оплата получена.")


# ─── Стикер-шаблон ──────────────────────────────────────────────────────────

async def on_sticker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    remember_user(user)

    if not await check_subscription(ctx.bot, user.id):
        await update.message.reply_text(f"📢 Сначала подпишитесь на {CHANNEL}.",
                                        reply_markup=_sub_keyboard())
        return

    sticker = update.message.sticker
    if not sticker.is_animated:
        await update.message.reply_text("Нужен анимированный (.tgs).",
                                        reply_markup=_main_menu(user.id))
        return

    msg = await update.message.reply_text("⬇️ Скачиваю...")
    template = _personal_path(user.id)

    try:
        file = await ctx.bot.get_file(sticker.file_id)
        await file.download_to_drive(str(template))
        with gzip.open(str(template), "rb") as f:
            data = json.load(f)
        _, ltype, extra = _find_text_layer(data)
        kind = "изображение"
        if ltype == LAYER_TYPE_PIXEL:
            kind = f"пиксельный ({extra['n_cols']}x{extra['n_rows']})"
        elif ltype == LAYER_TYPE_BLOB:
            kind = f"векторный ({extra['n_sh']} контуров)"
        else:
            kind = f"глифовый ({len(_collect_all_groups(extra['main_shape']))} букв)"

        ctx.user_data["personal_template"] = str(template)
        await msg.edit_text(
            f"✅ Личный шаблон сохранён! Тип: {kind}\nНапиши текст.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 В меню", callback_data="main")]]))
    except ValueError:
        template.unlink(missing_ok=True)
        await msg.edit_text("❌ Не нашёл текстовый слой.",
                            reply_markup=_main_menu(user.id))
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=_main_menu(user.id))


# ─── Обработка текста / генерация ───────────────────────────────────────────

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    remember_user(user)
    user_id = user.id
    text = update.message.text.strip()

    # Загрузка шрифта
    if ctx.user_data.get("awaiting_font"):
        ctx.user_data["awaiting_font"] = False
        await update.message.reply_text(
            "❌ Нужно отправить .ttf или .otf файл, а не текст.",
            reply_markup=_main_menu(user_id))
        return

    if not await check_subscription(ctx.bot, user_id):
        await update.message.reply_text(f"📢 Подпишитесь на {CHANNEL}.",
                                        reply_markup=_sub_keyboard())
        return

    if not text or text.startswith("/"):
        return

    if len(text) > 12:
        await update.message.reply_text(f"Слишком длинный текст ({len(text)}). Макс — 12.")
        return

    # Проверяем — есть ли выбранные шаблоны
    selected = ctx.user_data.get("selected_templates", [])
    cat = None
    for k, _, _ in CATEGORIES:
        if ctx.user_data.get("selected_cat") == k:
            cat = k
    if not selected or not cat:
        await update.message.reply_text(
            "Сначала выбери категорию и отметь шаблоны (✅).",
            reply_markup=_main_menu(user_id))
        return

    # Проверяем шрифт
    font_path = ctx.user_data.get("font_path")
    if not font_path:
        await update.message.reply_text(
            "🎨 Выбери шрифт:",
            reply_markup=_font_keyboard())
        return

    # Списываем баланс
    is_adm = is_admin(user_id)
    total_price = PRICE_STARS * len(selected)
    if not is_adm:
        if not spend_balance(user_id, total_price):
            await update.message.reply_text(
                f"❌ Не хватает звёзд. Нужно: {total_price} ⭐",
                reply_markup=_main_menu(user_id))
            return

    templates = _category_templates(cat)
    # Генерируем стикеры
    msg = await update.message.reply_text(
        f"⚙️ Генерирую {len(selected)} стикер(ов)...")
    out_files = []
    try:
        for idx_global in selected:
            if idx_global >= len(templates):
                continue
            name, path = templates[idx_global]
            with tempfile.NamedTemporaryFile(suffix=".tgs", delete=False) as tmp:
                out_path = tmp.name
            generate_sticker(text, str(path), out_path, font_path=font_path)
            out_files.append(out_path)

        # Отправляем по одному
        for f_path in out_files:
            with open(f_path, "rb") as f:
                await update.message.reply_sticker(sticker=f)
            add_set(user_id, f_path, text, "multi")

        await msg.delete()
        inc_generated(user_id)
        stat_inc("generated")

        await update.message.reply_text(
            f"✅ Готово! Списано {total_price} ⭐\n"
            f"⭐ Баланс: {get_balance(user_id)}\n\n"
            f"📦 Сохранено в «Ваши стикеры».",
            parse_mode="HTML",
            reply_markup=_main_menu(user_id))

        ctx.user_data["selected_templates"] = []
        ctx.user_data.pop("pending_text", None)
    except Exception as e:
        logger.exception("Ошибка генерации")
        await msg.edit_text(f"❌ Ошибка: {e}")
    finally:
        # Чистим временные (но сохранённые — оставляем)
        pass


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    for k in ("awaiting_font", "pending_text"):
        ctx.user_data.pop(k, None)
    ctx.user_data["selected_templates"] = []
    await update.message.reply_text("Отменено.",
                                    reply_markup=_main_menu(update.effective_user.id))


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Приём шрифта от пользователя."""
    user = update.effective_user
    doc = update.message.document
    if not doc:
        return
    if not doc.file_name.lower().endswith((".ttf", ".otf")):
        return
    msg = await update.message.reply_text("⬆️ Скачиваю шрифт...")
    try:
        file = await ctx.bot.get_file(doc.file_id)
        target = FONTS_DIR / doc.file_name
        await file.download_to_drive(str(target))
        ctx.user_data["font_path"] = str(target)
        ctx.user_data["font_label"] = doc.file_name
        await msg.edit_text(
            f"✅ Шрифт <code>{doc.file_name}</code> сохранён и выбран.",
            parse_mode="HTML",
            reply_markup=_main_menu(user.id))
    except Exception as e:
        logger.exception("Ошибка загрузки шрифта")
        await msg.edit_text(f"❌ Ошибка: {e}")


# ─── Запуск ─────────────────────────────────────────────────────────────────

async def on_error(update, ctx):
    logger.error("Handler error: %s", ctx.error, exc_info=ctx.error)


def main():
    if not TOKEN:
        raise ValueError("Укажи TELEGRAM_TOKEN в .env")
    if not TURSO_URL or not TURSO_TOKEN:
        raise ValueError("Укажи TURSO_DATABASE_URL и TURSO_AUTH_TOKEN")

    db_init()

    request = HTTPXRequest(
        connect_timeout=30.0, read_timeout=60.0,
        write_timeout=60.0, pool_timeout=30.0,
    )

    app = (ApplicationBuilder()
           .token(TOKEN).request(request).build())

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("help",   start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Sticker.ANIMATED, on_sticker))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()