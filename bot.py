import os
import io
import re
import time
import sqlite3
import logging
import tempfile
import gzip
import json
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, InputFile
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

TOKEN = os.getenv("TELEGRAM_TOKEN") or "8710441127:AAGFenboPwsVFxyMd6KxkK68HHagwEVcH1I"

ADMIN_ID = 8572202921

BASE_DIR      = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
SHARED_DIR    = TEMPLATES_DIR / "shared"
SETS_DIR      = BASE_DIR / "sets"
DB_PATH       = BASE_DIR / "bot.db"
TEMPLATES_DIR.mkdir(exist_ok=True)
SHARED_DIR.mkdir(exist_ok=True)
SETS_DIR.mkdir(exist_ok=True)

MAX_PAGES   = 9
PER_PAGE    = 12
CHANNEL     = "@sexoez"
PRICE_STARS = 2
MAX_TOPUP   = 100000

RECOLOR_KEY = "recolor"

CATEGORIES = [
    ("main1",    "🎨", "Основной 1"),
    ("main2",    "🎨", "Основной 2"),
    ("main3",    "🎨", "Основной 3"),
    ("main4",    "🎨", "Основной 4"),
    ("recolor",  "🌈", "Перекраска"),
    ("passport", "🛂", "Паспорт"),
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
    return [(p.stem, p) for p in sorted(d.glob("*.tgs"))]


def _category_info(key: str):
    for k, emoji, name in CATEGORIES:
        if k == key:
            return emoji, name
    return "📁", key


# ─── БД ─────────────────────────────────────────────────────────────────────

def db_init():
    con = sqlite3.connect(DB_PATH)
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
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()}
    if "generated" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN generated INTEGER NOT NULL DEFAULT 0")

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
    con = sqlite3.connect(DB_PATH)
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


def _gallery_keyboard(cat: str, page: int, total_pages: int, count_on_page: int):
    """
    Сетка шаблонов + строка навигации: стрелки + номера страниц рядом.
    """
    rows = []

    # Сетка шаблонов (4 в ряд)
    row = []
    for i in range(count_on_page):
        row.append(InlineKeyboardButton(
            str(i + 1), callback_data=f"pick:{cat}:{page}:{i}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Навигация: [⬅️] [1] [2] [·3·] [4] [5] [➡️]
    nav = []

    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"cat:{cat}:{page-1}"))

    # Показываем до 5 номеров вокруг текущей
    start = max(0, page - 2)
    end = min(total_pages, start + 5)
    start = max(0, end - 5)

    for p in range(start, end):
        if p == page:
            nav.append(InlineKeyboardButton(
                f"· {p+1} ·", callback_data="noop"))
        else:
            nav.append(InlineKeyboardButton(
                str(p + 1), callback_data=f"cat:{cat}:{p}"))

    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"cat:{cat}:{page+1}"))

    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="main")])
    return InlineKeyboardMarkup(rows)
def _color_keyboard(target: str):
    rows = []
    row = []
    for i, (label, hex_color) in enumerate(COLORS):
        row.append(InlineKeyboardButton(
            label,
            callback_data=f"color:{target}:{hex_color.lstrip('#')}"
        ))
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
        if val == "не выбран":
            return f"{name}: не выбран"
        return f"{name}: {val}"

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
        "🎨 Выбери категорию и создай свой стикер!\n\n"
        f"💰 Стоимость: {PRICE_STARS} ⭐ за стикер\n"
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

    if data.startswith("pick_color:"):
        await q.answer()
        target = data.split(":", 1)[1]
        target_names = {"bg": "🖼️ Фон", "shape": "🧍 Персонаж",
                        "outline": "🖌 Контур", "text": "✏️ Текст"}
        await q.message.reply_text(
            f"🎨 Выбери цвет для <b>{target_names.get(target, target)}</b>:",
            parse_mode="HTML",
            reply_markup=_color_keyboard(target)
        )
        return

    if data.startswith("color:"):
        await q.answer()
        parts = data.split(":")
        if len(parts) != 3:
            return
        _, target, hex_color = parts
        hex_color = "#" + hex_color

        colors = ctx.user_data.setdefault("recolor_colors", {})
        colors[target] = hex_color
        ctx.user_data["recolor_colors"] = colors

        target_names = {"bg": "🖼️ Фон", "shape": "🧍 Персонаж",
                        "outline": "🖌 Контур", "text": "✏️ Текст"}

        await q.message.reply_text(
            f"✅ {target_names.get(target, target)} → <code>{hex_color}</code>\n\n"
            f"Что-то ещё или нажми «Готово»:",
            parse_mode="HTML",
            reply_markup=_recolor_panel_keyboard(colors)
        )
        return

    if data == "recolor_done":
        await q.answer()
        text = ctx.user_data.get("pending_text")
        template_path = ctx.user_data.get("selected_template")
        colors = ctx.user_data.get("recolor_colors", {})

        if not text or not template_path:
            await q.message.reply_text(
                "❌ Данные потерялись. Выбери шаблон заново.",
                reply_markup=_main_menu(user_id))
            return

        if not colors:
            await q.message.reply_text(
                "❌ Ты не выбрал ни одного цвета.",
                reply_markup=_recolor_panel_keyboard(colors))
            return

        await _generate_with_colors(
            update, ctx, q.message, text, template_path, colors=colors
        )
        return

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
            lines.append(f"💳 Платежей: <b>{stat_get('payments')}</b>")
            lines.append(f"⭐ Звёзд: <b>{stat_get('stars')}</b>\n")
            lines.append("📁 <b>Категории:</b>")
            for key, emoji, name in CATEGORIES:
                cnt = len(_category_templates(key))
                lines.append(f"  {emoji} {name}: {cnt}")
            await q.message.reply_text("\n".join(lines), parse_mode="HTML",
                                       reply_markup=_admin_keyboard())
            return

        if data == "adm_users":
            rows = list_users(50)
            if not rows:
                await q.message.reply_text("👥 Пользователей пока нет.",
                                           reply_markup=_admin_keyboard())
                return
            lines = ["👥 <b>Пользователи</b>\n"]
            for r in rows:
                uname = f"@{r['username']}" if r["username"] else "—"
                lines.append(f"• <code>{r['user_id']}</code> · {uname} · "
                             f"{r['first_name'] or ''} · ⭐ {r['balance']}")
            if count_users() > 50:
                lines.append(f"\n…и ещё {count_users() - 50}")
            await q.message.reply_text("\n".join(lines), parse_mode="HTML",
                                       reply_markup=_admin_keyboard())
            return

        if data == "adm_give_stars":
            ctx.user_data["awaiting_give_stars"] = True
            await q.message.reply_text(
                "⭐ <b>Выдать звёзды</b>\n\n"
                "Отправь: <code>@username 100</code> или <code>123456789 100</code>\n"
                "Отрицательное — списать.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🛠 Отмена", callback_data="admin")]]))
            return

        if data == "adm_broadcast":
            ctx.user_data["awaiting_broadcast"] = True
            await q.message.reply_text(
                "📢 Напиши текст рассылки.\n\nОтмена: /cancel",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🛠 Отмена", callback_data="admin")]]))
            return

        if data == "adm_files":
            lines = ["📁 <b>Файлы shared/</b>\n"]
            for key, emoji, name in CATEGORIES:
                d = _category_dir(key)
                cnt = len(list(d.glob("*.tgs"))) if d.exists() else 0
                lines.append(f"{emoji} <b>{name}</b> (<code>{key}</code>): {cnt}")
            await q.message.reply_text("\n".join(lines), parse_mode="HTML",
                                       reply_markup=_admin_keyboard())
            return

    if data.startswith("cat:"):
        try:
            _, cat, page_s = data.split(":")
            page = int(page_s)
        except (ValueError, IndexError):
            await q.answer("Ошибка категории", show_alert=True)
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
        caption = (f"{emoji} <b>{cat_name}</b> · стр. {page+1}/{total_pages}\n"
                   f"Всего: {len(templates)}")
        keyboard = _gallery_keyboard(cat, page, total_pages, len(page_items))

        try:
            with open(png_path, "rb") as f:
                await q.message.reply_photo(
                    photo=InputFile(f, filename="gallery.png"),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    write_timeout=60, read_timeout=60, connect_timeout=30)
        except Exception:
            logger.exception("Фото не ушло")
        return

    if data.startswith("pick:"):
        try:
            _, cat, page_s, idx_s = data.split(":")
            page = int(page_s)
            idx = int(idx_s)
        except (ValueError, IndexError):
            await q.answer("Ошибка", show_alert=True)
            return

        templates = _category_templates(cat)
        global_idx = page * PER_PAGE + idx
        if global_idx < 0 or global_idx >= len(templates):
            await q.answer("Шаблон не найден", show_alert=True)
            return

        name, path = templates[global_idx]
        ctx.user_data["selected_template"] = str(path)
        ctx.user_data["selected_name"]     = name
        ctx.user_data["selected_cat"]      = cat
        ctx.user_data.pop("personal_template", None)
        ctx.user_data.pop("pending_text", None)
        ctx.user_data.pop("recolor_colors", None)

        await q.answer()

        try:
            with open(path, "rb") as f:
                await q.message.reply_sticker(
                    sticker=f,
                    write_timeout=60, read_timeout=60, connect_timeout=30)
        except Exception:
            logger.exception("Превью не ушло")

        emoji, cat_name = _category_info(cat)

        if cat == RECOLOR_KEY:
            await q.message.reply_text(
                f"✅ Выбран <b>{emoji} {cat_name}</b> → <code>{name}</code>\n"
                f"Тип: {_describe_template(path)}\n\n"
                f"✏️ Напиши текст (до 12 символов).\n"
                f"🎨 После этого выберешь цвета.\n"
                f"💰 Стоимость: {PRICE_STARS} ⭐",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"⬅️ К {cat_name}", callback_data=f"cat:{cat}:{page}"),
                    InlineKeyboardButton("🏠 В меню",        callback_data="main")]]))
            return

        await q.message.reply_text(
            f"✅ Выбран <b>{emoji} {cat_name}</b> → <code>{name}</code>\n"
            f"Тип: {_describe_template(path)}\n\n"
            f"✏️ Напиши текст (до 12 символов).\n"
            f"💰 Стоимость: {PRICE_STARS} ⭐",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"⬅️ К {cat_name}", callback_data=f"cat:{cat}:{page}"),
                InlineKeyboardButton("🏠 В меню",        callback_data="main")]]))
        return

    if data == "topup":
        await q.answer()
        await q.message.reply_text(
            f"⭐ <b>Пополнение баланса</b>\n\n"
            f"1 стикер = {PRICE_STARS} ⭐\n\n"
            f"Выбери сумму или введи свою:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ 10",  callback_data="buy:10"),
                 InlineKeyboardButton("⭐ 50",  callback_data="buy:50")],
                [InlineKeyboardButton("⭐ 100", callback_data="buy:100"),
                 InlineKeyboardButton("⭐ 500", callback_data="buy:500")],
                [InlineKeyboardButton("✏️ Своя сумма", callback_data="buy_custom")],
                [InlineKeyboardButton("🏠 В меню", callback_data="main")],
            ]))
        return

    if data == "buy_custom":
        await q.answer()
        ctx.user_data["awaiting_topup"] = True
        await q.message.reply_text(
            "✏️ Введи сумму в звёздах (целое число, от 1):\n\n"
            "Например: <code>25</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Отмена", callback_data="main")]]))
        return

    if data.startswith("buy:"):
        await q.answer()
        try:
            amount = int(data.split(":", 1)[1])
        except (IndexError, ValueError):
            return
        if amount < 1 or amount > MAX_TOPUP:
            await q.message.reply_text(f"❌ Сумма от 1 до {MAX_TOPUP}.")
            return
        try:
            await send_topup_invoice(ctx.bot, user_id, amount)
        except Exception as e:
            logger.exception("send_invoice failed")
            await q.message.reply_text(f"❌ Ошибка: {e}")
        return

    if data == "main":
        await q.answer()
        await q.message.reply_text(
            f"✨ <b>Главное меню</b>\n\n⭐ <b>Баланс:</b> {get_balance(user_id)}",
            parse_mode="HTML", reply_markup=_main_menu(user_id))
        return

    if data == "help":
        await q.answer()
        await q.message.reply_text(
            "ℹ️ <b>Как пользоваться:</b>\n\n"
            "1. Выбери категорию\n"
            "2. Выбери шаблон\n"
            "3. Напиши текст (до 12 символов)\n"
            "4. В «Перекраске» выбери цвета для фона, персонажа, контура, текста\n"
            f"5. Получи стикер за {PRICE_STARS} ⭐",
            parse_mode="HTML", reply_markup=_back_menu())
        return

    if data == "my_sets":
        await q.answer()
        sets = list_sets(user_id)
        if not sets:
            await q.message.reply_text(
                "📦 <b>Ваши стикеры</b>\n\nПока пусто.",
                parse_mode="HTML", reply_markup=_back_menu())
            return
        await q.message.reply_text(
            f"📦 <b>Ваши стикеры</b>\n\nСохранено: <b>{len(sets)}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 Показать", callback_data="my_sets_show:0")],
                [InlineKeyboardButton("🗑 Очистить", callback_data="my_sets_clear")],
                [InlineKeyboardButton("🏠 В меню", callback_data="main")],
            ]))
        return

    if data.startswith("my_sets_show:"):
        await q.answer()
        try:
            idx = int(data.split(":", 1)[1])
        except (IndexError, ValueError):
            idx = 0

        sets = list_sets(user_id)
        if not sets:
            await q.message.reply_text("📭 Пусто.", reply_markup=_back_menu())
            return

        PER = 5
        chunk = sets[idx:idx + PER]
        for item in chunk:
            p = item["path"]
            if p and Path(p).exists():
                try:
                    with open(p, "rb") as f:
                        await q.message.reply_sticker(sticker=f)
                except Exception:
                    logger.exception("Не отправилось")
            else:
                await q.message.reply_text(f"• «{item['text'] or '?'}» — потерян")

        total = len(sets)
        nav = []
        if idx > 0:
            nav.append(InlineKeyboardButton(
                "⬅️", callback_data=f"my_sets_show:{max(0, idx - PER)}"))
        nav.append(InlineKeyboardButton(
            f"{idx // PER + 1}/{(total + PER - 1) // PER}", callback_data="noop"))
        if idx + PER < total:
            nav.append(InlineKeyboardButton(
                "➡️", callback_data=f"my_sets_show:{idx + PER}"))

        rows = []
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("🏠 В меню", callback_data="main")])
        await q.message.reply_text(
            f"📦 Стикеры {idx+1}–{min(idx+PER, total)} из {total}",
            reply_markup=InlineKeyboardMarkup(rows))
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
    await q.message.reply_text("🤔 Не понял.", reply_markup=_main_menu(user_id))


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
    if amount < 1 or amount > MAX_TOPUP:
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
            stat_inc("payments")
            stat_inc("stars", amount)
            await update.message.reply_text(
                f"✅ Баланс пополнен на {amount} ⭐!\n"
                f"Текущий баланс: {get_balance(user.id)}",
                reply_markup=_main_menu(user.id))
            if ADMIN_ID and user.id != ADMIN_ID:
                try:
                    await ctx.bot.send_message(
                        ADMIN_ID,
                        f"💰 Платёж: {amount} ⭐ от {user.first_name} "
                        f"(@{user.username or '—'}, id {user.id})")
                except Exception:
                    pass
            return
    await update.message.reply_text("✅ Оплата получена.")


# ─── Стикер-шаблон ──────────────────────────────────────────────────────────

async def on_sticker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    remember_user(user)
    logger.info("HANDLER: sticker from %s", user.id)

    if not await check_subscription(ctx.bot, user.id):
        await update.message.reply_text(f"📢 Сначала подпишитесь на {CHANNEL}.",
                                        reply_markup=_sub_keyboard())
        return

    sticker = update.message.sticker
    if not sticker.is_animated:
        await update.message.reply_text(
            "Это обычный стикер. Мне нужен анимированный (.tgs).",
            reply_markup=_main_menu(user.id))
        return

    msg = await update.message.reply_text("⬇️ Скачиваю шаблон...")
    template = _personal_path(user.id)

    try:
        file = await ctx.bot.get_file(sticker.file_id)
        await file.download_to_drive(str(template))

        with gzip.open(str(template), "rb") as f:
            data = json.load(f)

        _, ltype, extra = _find_text_layer(data)
        if ltype == LAYER_TYPE_PIXEL:
            kind = f"пиксельный ({extra['n_cols']}x{extra['n_rows']})"
        elif ltype == LAYER_TYPE_BLOB:
            kind = f"векторный ({extra['n_sh']} контуров)"
        else:
            kind = f"глифовый ({len(_collect_all_groups(extra['main_shape']))} букв)"

        ctx.user_data["personal_template"] = str(template)
        ctx.user_data.pop("selected_template", None)
        ctx.user_data.pop("selected_name", None)

        await msg.edit_text(
            f"✅ Личный шаблон сохранён! Тип: {kind}\n"
            f"Напиши текст (до 12 символов). Стоимость: {PRICE_STARS} ⭐",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 В меню", callback_data="main")]]))

    except ValueError:
        template.unlink(missing_ok=True)
        await msg.edit_text("❌ Не нашёл текстовый слой.",
                            reply_markup=_main_menu(user.id))
    except Exception as e:
        logger.exception("Ошибка шаблона")
        await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=_main_menu(user.id))


# ─── Генерация ──────────────────────────────────────────────────────────────

async def _generate_with_colors(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                 message, text: str, template_path: str,
                                 colors: dict):
    user_id = update.effective_user.id
    is_adm = is_admin(user_id)

    if not is_adm:
        if not spend_balance(user_id, PRICE_STARS):
            await message.reply_text(
                f"❌ Недостаточно звёзд.\n\n"
                f"Нужно: {PRICE_STARS} ⭐\n"
                f"У тебя: {get_balance(user_id)} ⭐",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⭐ Пополнить", callback_data="topup")],
                    [InlineKeyboardButton("🏠 В меню", callback_data="main")],
                ]))
            return

    msg = await message.reply_text(f"⚙️ Генерирую «{text}»...")
    out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tgs", delete=False) as tmp:
            out_path = tmp.name

        generate_sticker(
            text, template_path, out_path,
            bg_color=colors.get("bg"),
            shape_color=colors.get("shape"),
            outline_color=colors.get("outline"),
            text_color=colors.get("text"),
        )

        saved = SETS_DIR / f"{user_id}_{int(time.time())}.tgs"
        try:
            with open(out_path, "rb") as src, open(saved, "wb") as dst:
                dst.write(src.read())
            add_set(user_id, str(saved), text,
                    ctx.user_data.get("selected_name", "свой"))
        except Exception:
            logger.exception("Не сохранилось")

        with open(out_path, "rb") as f:
            await message.reply_sticker(
                sticker=f,
                write_timeout=60, read_timeout=60, connect_timeout=30)

        await msg.delete()
        inc_generated(user_id)
        stat_inc("generated")

        note = "бесплатно (админ)" if is_adm else f"списано {PRICE_STARS} ⭐"
        parts_note = []
        if colors.get("bg"):      parts_note.append(f"🖼️ {colors['bg']}")
        if colors.get("shape"):   parts_note.append(f"🧍 {colors['shape']}")
        if colors.get("outline"): parts_note.append(f"🖌 {colors['outline']}")
        if colors.get("text"):    parts_note.append(f"✏️ {colors['text']}")
        color_str = " | ".join(parts_note) if parts_note else "—"

        await message.reply_text(
            f"✅ Готово! {note}\n"
            f"🎨 Цвета: {color_str}\n"
            f"⭐ Баланс: {get_balance(user_id)}\n\n"
            f"📦 Стикер сохранён в «Ваши стикеры».",
            parse_mode="HTML",
            reply_markup=_main_menu(user_id))

        ctx.user_data.pop("recolor_colors", None)
        ctx.user_data.pop("pending_text", None)
    except ValueError as e:
        await msg.edit_text(f"❌ {e}")
    except Exception as e:
        logger.exception("Ошибка генерации")
        await msg.edit_text(f"❌ Ошибка генерации: {e}")
    finally:
        if out_path and os.path.exists(out_path):
            try:
                os.unlink(out_path)
            except Exception:
                pass


async def _generate_simple(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                            message, text: str, template_path: str):
    user_id = update.effective_user.id
    is_adm = is_admin(user_id)

    if not is_adm:
        if not spend_balance(user_id, PRICE_STARS):
            await message.reply_text(
                f"❌ Недостаточно звёзд.\n\n"
                f"Нужно: {PRICE_STARS} ⭐\n"
                f"У тебя: {get_balance(user_id)} ⭐",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⭐ Пополнить", callback_data="topup")],
                    [InlineKeyboardButton("🏠 В меню", callback_data="main")],
                ]))
            return

    msg = await message.reply_text(f"⚙️ Генерирую «{text}»...")
    out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tgs", delete=False) as tmp:
            out_path = tmp.name

        generate_sticker(text, template_path, out_path)

        saved = SETS_DIR / f"{user_id}_{int(time.time())}.tgs"
        try:
            with open(out_path, "rb") as src, open(saved, "wb") as dst:
                dst.write(src.read())
            add_set(user_id, str(saved), text,
                    ctx.user_data.get("selected_name", "свой"))
        except Exception:
            logger.exception("Не сохранилось")

        with open(out_path, "rb") as f:
            await message.reply_sticker(
                sticker=f,
                write_timeout=60, read_timeout=60, connect_timeout=30)

        await msg.delete()
        inc_generated(user_id)
        stat_inc("generated")

        note = "бесплатно (админ)" if is_adm else f"списано {PRICE_STARS} ⭐"
        await message.reply_text(
            f"✅ Готово! {note}\n⭐ Баланс: {get_balance(user_id)}\n\n"
            f"📦 Стикер сохранён в «Ваши стикеры».",
            parse_mode="HTML",
            reply_markup=_main_menu(user_id))
    except ValueError as e:
        await msg.edit_text(f"❌ {e}")
    except Exception as e:
        logger.exception("Ошибка генерации")
        await msg.edit_text(f"❌ Ошибка генерации: {e}")
    finally:
        if out_path and os.path.exists(out_path):
            try:
                os.unlink(out_path)
            except Exception:
                pass


# ─── /gen, /cancel, текст ───────────────────────────────────────────────────

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("awaiting_topup", None)
    ctx.user_data.pop("awaiting_broadcast", None)
    ctx.user_data.pop("awaiting_give_stars", None)
    ctx.user_data.pop("pending_text", None)
    ctx.user_data.pop("recolor_colors", None)
    await update.message.reply_text(
        "Отменено.", reply_markup=_main_menu(update.effective_user.id))


async def cmd_gen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    remember_user(update.effective_user)
    if not await check_subscription(ctx.bot, update.effective_user.id):
        await update.message.reply_text(f"📢 Подпишитесь на {CHANNEL}.",
                                        reply_markup=_sub_keyboard())
        return
    text = " ".join(ctx.args).strip()
    if not text:
        await update.message.reply_text("Укажи текст: /gen Привет")
        return
    await _do_generate(update, ctx, text)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    remember_user(user)
    user_id = user.id

    if ctx.user_data.get("awaiting_give_stars") and is_admin(user_id):
        ctx.user_data["awaiting_give_stars"] = False
        parts = update.message.text.strip().split()
        if len(parts) != 2:
            await update.message.reply_text(
                "❌ Формат: <code>@username 100</code> или <code>123456789 100</code>",
                parse_mode="HTML", reply_markup=_admin_keyboard())
            return

        target_raw, amount_raw = parts
        try:
            amount = int(amount_raw)
        except ValueError:
            await update.message.reply_text("❌ Сумма должна быть числом.",
                                            reply_markup=_admin_keyboard())
            return

        if amount == 0:
            await update.message.reply_text("❌ Сумма не может быть 0.",
                                            reply_markup=_admin_keyboard())
            return

        if target_raw.startswith("@") or not target_raw.isdigit():
            target_id = find_user_by_username(target_raw)
            if target_id is None:
                await update.message.reply_text(
                    f"❌ Пользователь <code>{target_raw}</code> не найден.\n"
                    f"<i>Он должен хоть раз написать боту /start.</i>",
                    parse_mode="HTML", reply_markup=_admin_keyboard())
                return
            display = target_raw
        else:
            target_id = int(target_raw)
            display = f"<code>{target_id}</code>"

        add_balance(target_id, amount)
        action = "Выдано" if amount > 0 else "Списано"
        new_bal = get_balance(target_id)
        await update.message.reply_text(
            f"✅ {action} {abs(amount)} ⭐ → {display}\n"
            f"Баланс: <b>{new_bal}</b> ⭐",
            parse_mode="HTML", reply_markup=_admin_keyboard())

        if target_id != user_id:
            try:
                if amount > 0:
                    await ctx.bot.send_message(
                        target_id,
                        f"🎁 Зачислено {amount} ⭐!\nБаланс: {new_bal} ⭐")
                else:
                    await ctx.bot.send_message(
                        target_id,
                        f"⚠️ Списано {-amount} ⭐.\nБаланс: {new_bal} ⭐")
            except Exception:
                pass
        return

    if ctx.user_data.get("awaiting_broadcast") and is_admin(user_id):
        ctx.user_data["awaiting_broadcast"] = False
        text = update.message.text.strip()
        sent, failed = 0, 0
        for uid in get_all_user_ids():
            try:
                await ctx.bot.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(
            f"📢 Отправлено: {sent}, ошибок: {failed}.",
            reply_markup=_admin_keyboard())
        return

    if ctx.user_data.get("awaiting_topup"):
        ctx.user_data["awaiting_topup"] = False
        raw = update.message.text.strip()
        if not raw.isdigit():
            await update.message.reply_text(
                "❌ Нужно целое число.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 В меню", callback_data="main")]]))
            return
        amount = int(raw)
        if amount < 1 or amount > MAX_TOPUP:
            await update.message.reply_text(f"❌ Сумма от 1 до {MAX_TOPUP}.")
            return
        try:
            await send_topup_invoice(ctx.bot, user_id, amount)
        except Exception as e:
            logger.exception("send_invoice failed")
            await update.message.reply_text(f"❌ Не удалось создать счёт: {e}")
        return

    if not await check_subscription(ctx.bot, user_id):
        await update.message.reply_text(f"📢 Подпишитесь на {CHANNEL}.",
                                        reply_markup=_sub_keyboard())
        return

    text = update.message.text.strip()
    logger.info("HANDLER: text=%r", text)
    if not text or text.startswith("/"):
        return

    if len(text) > 12:
        await update.message.reply_text(
            f"Слишком длинный текст ({len(text)}). Максимум — 12.")
        return

    template_path = _resolve_template(ctx)
    if template_path is None:
        await update.message.reply_text(
            "Сначала выбери стикер из галереи или пришли свой .tgs.",
            reply_markup=_main_menu(user_id))
        return

    cat = ctx.user_data.get("selected_cat")

    if cat == RECOLOR_KEY:
        ctx.user_data["pending_text"] = text
        ctx.user_data["recolor_colors"] = {}
        await update.message.reply_text(
            f"✏️ Текст: <b>{text}</b>\n\n"
            f"🎨 Выбери цвета для каждой части:",
            parse_mode="HTML",
            reply_markup=_recolor_panel_keyboard({})
        )
        return

    await _generate_simple(update, ctx, update.message, text, str(template_path))


def _resolve_template(ctx):
    p = ctx.user_data.get("personal_template")
    if p and Path(p).exists():
        return Path(p)
    p = ctx.user_data.get("selected_template")
    if p and Path(p).exists():
        return Path(p)
    return None


async def _do_generate(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    template = _resolve_template(ctx)
    if template is None:
        await update.message.reply_text(
            "Сначала выбери стикер из галереи или пришли свой .tgs.",
            reply_markup=_main_menu(user_id))
        return

    if len(text) > 12:
        await update.message.reply_text(
            f"Слишком длинный текст ({len(text)}). Максимум — 12.")
        return

    cat = ctx.user_data.get("selected_cat")
    if cat == RECOLOR_KEY:
        ctx.user_data["pending_text"] = text
        ctx.user_data["recolor_colors"] = {}
        await update.message.reply_text(
            f"✏️ Текст: <b>{text}</b>\n\n🎨 Выбери цвета:",
            parse_mode="HTML",
            reply_markup=_recolor_panel_keyboard({})
        )
        return

    await _generate_simple(update, ctx, update.message, text, str(template))


# ─── Запуск ─────────────────────────────────────────────────────────────────

async def on_error(update, ctx):
    logger.error("Handler error: %s", ctx.error, exc_info=ctx.error)


def main():
    if not TOKEN:
        raise ValueError("Укажи TELEGRAM_TOKEN в .env")

    db_init()

    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=30.0,
    )

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(request)
        .build()
    )

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("help",   start))
    app.add_handler(CommandHandler("gen",    cmd_gen))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Sticker.ANIMATED, on_sticker))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()