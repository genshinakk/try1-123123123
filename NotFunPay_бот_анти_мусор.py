# =====================================================================
# NotFunPay — гарант-бот (aiogram 3.x)
# =====================================================================
# Запуск:
#   BOT_TOKEN="ваш_токен" ADMIN_ID="7212994322" python notfunpay_bot_fixed.py
#
# ВНИМАНИЕ (безопасность): токен ниже оставлен как значение по умолчанию,
# но он был опубликован в переписке и считается скомпрометированным!
# Перевыпустите токен у @BotFather и задавайте его через переменную окружения
# BOT_TOKEN, иначе любой, кто видел код, сможет управлять ботом.
# =====================================================================

import asyncio
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

# ================= CONFIG =================
TOKEN = os.getenv("BOT_TOKEN", "8986120714:AAEEWJjUGOP1G-P0bDYstSriKI6y05GxQCU").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "7212994322").strip()
if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. См. инструкцию по запуску.")
if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("Не задан корректный ADMIN_ID. См. инструкцию по запуску.")
ADMIN_ID = int(ADMIN_ID_RAW)
DB_PATH = os.getenv("DB_PATH", "guarantor.db")

bot = Bot(TOKEN)
dp = Dispatcher()

MAX_TEXT_LEN = 4000  # безопасный лимит длины сообщения (лимит Telegram — 4096)

STATUSES = {
    "created": "Ожидает покупателя",
    "negotiating": "Согласование условий",
    "awaiting_payment": "Ожидает оплату",
    "paid": "Оплата проверена гарантом",
    "delivered": "Ожидает подтверждение покупателя",
    "disputed": "Открыт спор",
    "completed": "Завершена",
    "refunded": "Возврат отмечен гарантом",
    "partially_refunded": "Частичный возврат отмечен",
}

ADMIN_ACTIVE = (
    "created", "negotiating", "awaiting_payment",
    "paid", "delivered", "disputed"
)

RESOLUTION_LABELS = {
    "buyer": "В пользу покупателя",
    "seller": "В пользу продавца",
    "partial": "Частичный возврат",
}

ADMIN_ACTIONS = {
    "paid": {
        "label": "Подтвердить оплату",
        "from_status": ("awaiting_payment",),
        "to_status": "paid",
        "audit": "payment_manually_verified",
        "notice": "Оплата прошла успешно."
    },
    "close": {
        "label": "Закрыть спор в пользу завершения",
        "from_status": ("disputed",),
        "to_status": "completed",
        "audit": "admin_closed_dispute",
        "notice": "Гарант закрыл спор и отметил сделку завершённой."
    },
    "refund": {
        "label": "Отметить возврат",
        "from_status": ("disputed", "paid"),
        "to_status": "refunded",
        "audit": "admin_marked_refund",
        "notice": "Гарант отметил возврат. Перевод денег выполняется отдельно."
    }
}

# Ввод причины/частичного возврата в текущем процессе бота.
# Уже сохранённые сделки и решения находятся в SQLite.
pending_resolution = {}

# Пользователи, которые сейчас пишут текст отзыва: {user_id: {deal_id, rating}}
pending_review = {}
# Анти-спам: (deal_id, uid) -> message_id последней карточки сделки в чате.
deal_msgs = {}
FINAL_STATUSES = ("completed", "refunded", "partially_refunded")
NO_REVIEW_TEXT = "Без комментария"


# ================= DATABASE =================
def connect():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    # FIX: isolation_level=None — полностью ручное управление транзакциями.
    # Без этого на Python 3.12 явный "BEGIN IMMEDIATE" конфликтует с неявными
    # транзакциями pysqlite (риск "cannot start a transaction within a
    # transaction" и неатомарных коммитов).
    con.isolation_level = None
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with closing(connect()) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller INTEGER NOT NULL,
                buyer INTEGER,
                description TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount > 0),
                status TEXT NOT NULL DEFAULT 'created',
                seller_ok INTEGER NOT NULL DEFAULT 0,
                buyer_ok INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id INTEGER NOT NULL,
                actor INTEGER NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS balances (
                user_id INTEGER PRIMARY KEY,
                amount REAL NOT NULL DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS balance_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id INTEGER NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id INTEGER NOT NULL,
                author_name TEXT NOT NULL,
                rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS faq (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Миграция для баз, созданных предыдущими версиями бота.
        existing = {row["name"] for row in con.execute("PRAGMA table_info(deals)")}
        migrations = {
            "resolution": "TEXT",
            "resolution_reason": "TEXT",
            "refund_amount": "REAL NOT NULL DEFAULT 0",
            "resolved_by": "INTEGER",
            "resolved_at": "TEXT",
        }
        for name, definition in migrations.items():
            if name not in existing:
                con.execute(f"ALTER TABLE deals ADD COLUMN {name} {definition}")
        review_cols = {row["name"] for row in con.execute("PRAGMA table_info(reviews)")}
        for name, definition in {"deal_id": "INTEGER", "target_id": "INTEGER"}.items():
            if name not in review_cols:
                con.execute(f"ALTER TABLE reviews ADD COLUMN {name} {definition}")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_reviews_deal_author "
            "ON reviews(deal_id, author_id)"
        )


def get_deal(deal_id):
    with closing(connect()) as con:
        return con.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()


def log_action(con, deal_id, actor, action):
    con.execute(
        "INSERT INTO audit(deal_id, actor, action) VALUES(?,?,?)",
        (deal_id, actor, action)
    )


def credit_seller_balance(con, deal_id, actor, description="Зачисление после завершения сделки"):
    """Начисляет сумму сделки на внутренний баланс продавца ровно один раз."""
    deal = con.execute("SELECT seller, amount FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not deal:
        return False
    cur = con.execute(
        "INSERT OR IGNORE INTO balance_transactions(deal_id, user_id, amount, description) VALUES(?,?,?,?)",
        (deal_id, deal["seller"], float(deal["amount"]), description)
    )
    if cur.rowcount == 1:
        con.execute(
            "INSERT INTO balances(user_id, amount) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET amount=balances.amount+excluded.amount",
            (deal["seller"], float(deal["amount"]))
        )
        log_action(con, deal_id, actor, f"seller_balance_credited:{float(deal['amount']):.2f}")
        return True
    return False


def change_status(deal_id, expected, new_status, actor, action):
    """Атомарно меняет статус; при завершении зачисляет продавцу баланс один раз."""
    with closing(connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            "UPDATE deals SET status=? WHERE id=? AND status=?",
            (new_status, deal_id, expected)
        )
        if cur.rowcount != 1:
            con.rollback()
            return False
        log_action(con, deal_id, actor, action)
        if new_status == "completed":
            credit_seller_balance(con, deal_id, actor)
        con.commit()
        return True


# ================= UI HELPERS =================
def safe_int(value):
    """FIX: безопасное преобразование данных callback'ов в int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def deal_text(d):
    result = (
        f"🛡 NotFunPay | Сделка №{d['id']}\n"
        f"Описание: {d['description']}\n"
        f"Сумма: {d['amount']:.2f}\n"
        f"Комиссия бота: 0%\n"
        f"Статус: {STATUSES.get(d['status'], d['status'])}\n"
        f"Продавец Telegram ID: {d['seller']}\n"
        f"Покупатель Telegram ID: {d['buyer'] or 'ещё не присоединился'}"
    )
    if d["resolution"]:
        result += (
            f"\nРешение: {RESOLUTION_LABELS.get(d['resolution'], d['resolution'])}"
            f"\nПричина: {d['resolution_reason'] or 'не указана'}"
            f"\nОтмеченный возврат: {float(d['refund_amount'] or 0):.2f}"
            f"\nРешение принял: {d['resolved_by']}"
            f"\nВремя решения: {d['resolved_at']}"
        )
    return result


def user_deal_keyboard(d):
    deal_id = d["id"]
    status = d["status"]
    rows = []
    if status == "negotiating":
        if d["seller"] and not d["seller_ok"]:
            rows.append([InlineKeyboardButton(
                text="✅ Согласен с условиями (продавец)",
                callback_data=f"agree:{deal_id}"
            )])
        if d["buyer"] and not d["buyer_ok"]:
            rows.append([InlineKeyboardButton(
                text="✅ Согласен с условиями (покупатель)",
                callback_data=f"agree:{deal_id}"
            )])
    if status == "paid" and d["seller"]:
        rows.append([InlineKeyboardButton(
            text="📦 Подтвердить передачу товара/услуги",
            callback_data=f"deliver:{deal_id}"
        )])
    if status == "delivered" and d["buyer"]:
        rows.append([InlineKeyboardButton(
            text="✅ Подтвердить получение",
            callback_data=f"complete:{deal_id}"
        )])
    # FIX: кнопки "Открыть спор" у пользователей больше нет —
    # спор открывает только гарант (см. admin_deal_keyboard и /dispute).
    if status in FINAL_STATUSES:
        rows.append([InlineKeyboardButton(
            text="⭐ Оставить отзыв",
            callback_data=f"rev:{deal_id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Все активные сделки", callback_data="panel:active")],
        [InlineKeyboardButton(text="⚠️ Открытые споры", callback_data="panel:disputes")],
        [InlineKeyboardButton(text="🕓 Последние действия", callback_data="panel:logs")],
    ])


def admin_deal_keyboard(d):
    deal_id = d["id"]
    status = d["status"]
    rows = []
    if status == "awaiting_payment":
        rows.append([InlineKeyboardButton(
            text="💳 Подтвердить оплату", callback_data=f"ask:paid:{deal_id}"
        )])
    if status == "disputed":
        rows.append([InlineKeyboardButton(
            text="👤 Решение в пользу покупателя", callback_data=f"resolve:buyer:{deal_id}"
        )])
        rows.append([InlineKeyboardButton(
            text="👤 Решение в пользу продавца", callback_data=f"resolve:seller:{deal_id}"
        )])
        rows.append([InlineKeyboardButton(
            text="⚖️ Частичный возврат", callback_data=f"resolve:partial:{deal_id}"
        )])
        rows.append([InlineKeyboardButton(
            text="🔒 Закрыть спор без отдельного решения", callback_data=f"ask:close:{deal_id}"
        )])
        rows.append([InlineKeyboardButton(
            text="↩️ Отметить полный возврат", callback_data=f"ask:refund:{deal_id}"
        )])
    if status == "paid":
        rows.append([InlineKeyboardButton(
            text="↩️ Отметить возврат", callback_data=f"ask:refund:{deal_id}"
        )])
    # FIX: спор открывает только гарант — кнопка перенесена в админ-клавиатуру.
    if status not in ("completed", "refunded", "partially_refunded", "disputed"):
        rows.append([InlineKeyboardButton(
            text="⚠️ Открыть спор", callback_data=f"dispute:{deal_id}"
        )])
    rows.append([InlineKeyboardButton(
        text="🕓 История сделки", callback_data=f"panel:history:{deal_id}"
    )])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="panel:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_only(message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("⛔ NotFunPay: доступ только для гаранта.")
        return False
    return True


async def notify_deal(d, text=None):
    """Анти-спам: редактируем существующую карточку сделки, а не шлём новую."""
    content = text or deal_text(d)
    for uid in {d["seller"], d["buyer"]} - {None}:
        key = (d["id"], uid)
        msg_id = deal_msgs.get(key)
        edited = False
        if msg_id is not None:
            try:
                await bot.edit_message_text(
                    chat_id=uid, message_id=msg_id, text=content,
                    reply_markup=user_deal_keyboard(d),
                )
                edited = True
            except TelegramBadRequest as e:
                # Текст не изменился (повторный клик) — это не ошибка, молча пропускаем.
                if "message is not modified" in str(e).lower():
                    edited = True
            except Exception:
                edited = False
        if not edited:
            # Карточки ещё нет / её удалили / она слишком старая — шлём новую и запоминаем.
            try:
                msg = await bot.send_message(uid, content, reply_markup=user_deal_keyboard(d))
                deal_msgs[key] = msg.message_id
            except Exception:
                pass


# ================= USER COMMANDS =================
@dp.message(CommandStart())
async def start(message: Message):
    # FIX: message.text/from_user могут отсутствовать (редкие типы апдейтов).
    if not message.from_user or message.text is None:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("deal_"):
        deal_id = safe_int(parts[1][5:])
        if deal_id is None:
            await message.answer("Некорректная ссылка на сделку.")
            return
        uid = message.from_user.id

        with closing(connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            d = con.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
            if not d:
                con.rollback()
                await message.answer("Сделка не найдена.")
                return
            if uid == d["seller"]:
                con.rollback()
                await message.answer("Вы продавец этой сделки.")
                return
            if d["buyer"] is not None:
                con.rollback()
                await message.answer("У сделки уже есть покупатель.")
                return
            if d["status"] != "created":
                con.rollback()
                await message.answer("Эта сделка больше недоступна.")
                return
            # FIX: проверка rowcount — защита от гонки между проверкой и апдейтом.
            cur = con.execute(
                "UPDATE deals SET buyer=?, status='negotiating', buyer_ok=0 "
                "WHERE id=? AND buyer IS NULL AND status='created'",
                (uid, deal_id)
            )
            if cur.rowcount != 1:
                con.rollback()
                await message.answer("Эта сделка больше недоступна.")
                return
            log_action(con, deal_id, uid, "buyer_joined")
            con.commit()

        d = get_deal(deal_id)
        await message.answer(
            "Вы присоединились к сделке. Проверьте условия и подтвердите их кнопкой.\n\n"
            + deal_text(d), reply_markup=user_deal_keyboard(d)
        )
        await notify_deal(d)
        return

    await message.answer(
    "🛡️ <b>NotFunPay — гарант-сервис для продажи аккаунтов</b>\n\n"

    "🤝 Покупай и продавай аккаунты с дополнительной защитой сделки!\n\n"

    "🔹 <b>Как проходит сделка?</b>\n"
    "📝 Продавец создаёт сделку и указывает сумму и условия.\n"
    "🤝 Покупатель присоединяется и подтверждает условия.\n"
    "🛡️ Гарант проверяет оплату.\n"
    "🔑 Продавец передаёт аккаунт покупателю.\n"
    "✅ Покупатель подтверждает получение, после чего сделка завершается.\n\n"

    "💎 <b>Преимущества NotFunPay</b>\n"
    "💸 Комиссия сервиса — <b>0%</b>\n"
    "🛡️ Участие гаранта в сделке\n"
    "📋 Фиксация условий и статусов\n"
    "⚖️ Возможность разрешения споров\n"
    "💰 Внутренний баланс для учёта начислений\n\n"
    "🚀 <b>NotFunPay — твои сделки, под контролем!</b>\n"

    "🛡️ Гарант: @notfunpay_garant\n\n"

    "📜 <b>Команды</b>\n"
    "🛒 Создать сделку: /new 1500 Продажа игрового аккаунта\n"
    "📂 Мои сделки: /mydeals\n"
    "💰 Мой баланс: /balance\n"
    "⭐ Отзывы: /reviews, оставить отзыв: /addreview\n"
    "ℹ️ Помощь: /help\n\n"
    "👨‍💼 Для гаранта: /admin",
    parse_mode="HTML"
)


@dp.message(Command("help"))
async def help_command(message: Message):
    text = (
        "🛡️ <b>NotFunPay — гарант-сервис для продажи аккаунтов</b>\n\n"

        "<b>📋 Как работает сделка?</b>\n"
        "1. 📝 Продавец создаёт сделку командой /new.\n"
        "2. 🤝 Покупатель открывает ссылку из бота.\n"
        "3. ✅ Обе стороны подтверждают условия.\n"
        "4. 🛡️ Гарант вручную проверяет оплату.\n"
        "5. 🔑 Продавец передаёт аккаунт покупателю.\n"
        "6. ✅ Покупатель подтверждает получение.\n"
        "7. ⚖️ При конфликте спор открывает гарант и разрешает его.\n"
        "8. 💰 После завершения сделки сумма начисляется "
        "на внутренний баланс продавца.\n\n"

        "💎 <b>Комиссия сервиса: 0%</b>\n\n"
        "<b>💸 Вывод баланса</b>\n"
        "Баланс можно запросить к выводу через гаранта:\n"
        "👉 @notfunpay_garant\n"
        "Вывод осуществляется вручную после проверки "
        "баланса и согласования способа выплаты.\n\n"

        "<b>📌 Команды</b>\n"
        "🛒 Создать сделку: /new 1500 Продажа аккаунта\n"
        "📂 Мои сделки: /mydeals\n"
        "💰 Мой баланс: /balance\n"
        "⭐ Отзывы: /reviews, оставить отзыв: /addreview\n"
        "❓ Частые вопросы: /faq\n"
        "ℹ️ Помощь: /help\n\n"
        "👨‍💼 Для гаранта: /admin"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("new"))
async def new_deal(message: Message):
    if not message.from_user:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("Формат: /new 1500 Покупка игрового предмета")
        return
    try:
        amount = float(parts[1].replace(",", "."))
        if not 0 < amount <= 1_000_000_000:
            raise ValueError
    except ValueError:
        await message.answer("Укажи корректную положительную сумму.")
        return
    description = parts[2].strip()
    if not 3 <= len(description) <= 1000:
        await message.answer("Описание должно быть длиной от 3 до 1000 символов.")
        return

    # FIX: создание сделки + запись аудита — одна атомарная транзакция.
    with closing(connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            "INSERT INTO deals(seller, description, amount) VALUES(?,?,?)",
            (message.from_user.id, description, amount)
        )
        deal_id = cur.lastrowid
        log_action(con, deal_id, message.from_user.id, "created")
        con.commit()

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=deal_{deal_id}"
    d = get_deal(deal_id)
    await message.answer(
        deal_text(d) + "\n\nОтправь покупателю ссылку:\n" + link +
        "\n\nВажно: оплата ещё не проверена. Не передавайте товар до подтверждения гаранта."
    )


@dp.message(Command("balance"))
async def balance_command(message: Message):
    if not message.from_user:
        return
    with closing(connect()) as con:
        row = con.execute("SELECT amount FROM balances WHERE user_id=?", (message.from_user.id,)).fetchone()
        amount = float(row["amount"]) if row else 0.0
        rows = con.execute(
            "SELECT deal_id, amount, created_at FROM balance_transactions WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (message.from_user.id,)
        ).fetchall()
    text = f"💰 NotFunPay — ваш баланс: {amount:.2f}\n\n Для вывода средств с баланса бота вам необходима связаться с админом бота."
    if rows:
        text += "\n\nПоследние зачисления:\n" + "\n".join(
            f"Сделка №{r['deal_id']}: +{float(r['amount']):.2f} ({r['created_at']})" for r in rows
        )
    await message.answer(text)


@dp.message(Command("mydeals"))
async def my_deals(message: Message):
    if not message.from_user:
        return
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT * FROM deals WHERE seller=? OR buyer=? ORDER BY id DESC LIMIT 30",
            (message.from_user.id, message.from_user.id)
        ).fetchall()
    if not rows:
        await message.answer("Сделок пока нет.")
        return
    first = True
    for d in rows:
        # FIX: пауза между сообщениями, чтобы не поймать flood limit Telegram.
        if not first:
            await asyncio.sleep(0.05)
        first = False
        await message.answer(deal_text(d), reply_markup=user_deal_keyboard(d))


# ================= REVIEWS =================
def review_stars(rating):
    return "⭐" * int(rating) + "☆" * (5 - int(rating))


def can_review(con, deal_id, uid):
    """Проверка: сделка есть, пользователь её участник, сделка завершена, отзыва ещё нет.
    Возвращает (сделка, текст_ошибки)."""
    d = con.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not d or uid not in (d["seller"], d["buyer"]):
        return None, "Нет доступа к этой сделке."
    if d["status"] not in FINAL_STATUSES:
        return None, "Отзыв можно оставить только после завершения сделки."
    exists = con.execute(
        "SELECT 1 FROM reviews WHERE deal_id=? AND author_id=?", (deal_id, uid)
    ).fetchone()
    if exists:
        return None, "Вы уже оставили отзыв по этой сделке."
    return d, None


def save_review(user, deal_id, rating, text):
    """Сохраняет отзыв. Возвращает (ok, ошибка_или_id_получателя)."""
    author_name = (user.full_name or f"ID {user.id}").strip()[:100]
    with closing(connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        d, error = can_review(con, deal_id, user.id)
        if error:
            con.rollback()
            return False, error
        target = d["buyer"] if user.id == d["seller"] else d["seller"]
        try:
            con.execute(
                "INSERT INTO reviews(author_id, author_name, rating, text, deal_id, target_id) "
                "VALUES(?,?,?,?,?,?)",
                (user.id, author_name, rating, text, deal_id, target)
            )
        except sqlite3.IntegrityError:
            con.rollback()
            return False, "Вы уже оставили отзыв по этой сделке."
        log_action(con, deal_id, user.id, f"review_added:{rating}")
        # FIX: при isolation_level=None и явном BEGIN коммит обязателен,
        # иначе транзакция откатывается при закрытии соединения.
        con.commit()
    return True, target


async def ask_for_reviews(d):
    """Сразу после завершения сделки отдельным сообщением предлагает покупателю оставить отзыв."""
    # FIX: проверка покупателя до построения клавиатуры.
    if not d or d["status"] not in FINAL_STATUSES or not d["buyer"]:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{n}⭐", callback_data=f"rate:{d['id']}:{n}")
        for n in range(1, 6)
    ]])
    try:
        await bot.send_message(
            d["buyer"],
            f"🎉 Сделка №{d['id']} закрыта!\n"
            "Оцените, как всё прошло — выберите оценку, "
            "затем можно написать пару слов:",
            reply_markup=kb
        )
    except Exception:
        pass


@dp.message(Command("addreview"))
async def add_review(message: Message):
    """Показывает завершённые сделки, по которым можно оставить отзыв."""
    if not message.from_user:
        return
    uid = message.from_user.id
    placeholders = ",".join("?" for _ in FINAL_STATUSES)
    with closing(connect()) as con:
        rows = con.execute(
            f"SELECT d.id, d.amount, d.description FROM deals d "
            f"WHERE (d.seller=? OR d.buyer=?) AND d.status IN ({placeholders}) "
            f"AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.deal_id=d.id AND r.author_id=?) "
            f"ORDER BY d.id DESC LIMIT 10",
            (uid, uid, *FINAL_STATUSES, uid)
        ).fetchall()
    if not rows:
        await message.answer(
            "Оставить отзыв можно после завершения сделки, "
            "по одному отзыву на сделку.\nВаши сделки: /mydeals"
        )
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"№{r['id']} · {r['amount']:.2f} · {r['description'][:25]}",
            callback_data=f"rev:{r['id']}"
        )] for r in rows
    ])
    await message.answer("⭐ Выберите сделку, по которой хотите оставить отзыв:", reply_markup=kb)


@dp.callback_query(F.data.startswith("rev:"))
async def review_choose_deal(callback: CallbackQuery):
    parts = callback.data.split(":")
    deal_id = safe_int(parts[1]) if len(parts) == 2 else None
    if deal_id is None:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    with closing(connect()) as con:
        d, error = can_review(con, deal_id, callback.from_user.id)
    if error:
        await callback.answer(error, show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{n}⭐", callback_data=f"rate:{deal_id}:{n}")
        for n in range(1, 6)
    ]])
    try:
        await callback.message.edit_text(
            f"⭐ Отзыв по сделке №{deal_id}\nВыберите оценку:", reply_markup=kb
        )
    except TelegramBadRequest:
        await callback.message.answer(
            f"⭐ Отзыв по сделке №{deal_id}\nВыберите оценку:", reply_markup=kb
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("rate:"))
async def review_choose_rating(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    deal_id, rating = int(parts[1]), int(parts[2])
    if not 1 <= rating <= 5:
        await callback.answer("Оценка должна быть от 1 до 5.", show_alert=True)
        return
    with closing(connect()) as con:
        d, error = can_review(con, deal_id, callback.from_user.id)
    if error:
        await callback.answer(error, show_alert=True)
        return
    pending_review[callback.from_user.id] = {"deal_id": deal_id, "rating": rating}
    if callback.message is None:
        await callback.answer()
        return
    try:
        await callback.message.edit_text(
            f"Оценка: {review_stars(rating)} ({rating}/5)\n\n"
            "Напишите текст отзыва одним сообщением (до 1000 символов).\n"
            "/skip — отправить только оценку\n"
            "/cancelreview — отмена"
        )
    except TelegramBadRequest:
        await callback.message.answer(
            f"Оценка: {review_stars(rating)} ({rating}/5)\n\n"
            "Напишите текст отзыва одним сообщением (до 1000 символов).\n"
            "/skip — отправить только оценку\n"
            "/cancelreview — отмена"
        )
    await callback.answer()


async def finish_review(message: Message, text):
    pending = pending_review.pop(message.from_user.id, None)
    if not pending:
        return
    ok, result = save_review(message.from_user, pending["deal_id"], pending["rating"], text)
    if not ok:
        await message.answer(f"⛔ {result}")
        return
    await message.answer("✅ Спасибо! Отзыв опубликован. Все отзывы: /reviews")
    # Уведомление о новом отзыве получает только гарант
    user = message.from_user
    review_body = "" if text == NO_REVIEW_TEXT else f"\n{text}"
    try:
        await bot.send_message(
            ADMIN_ID,
            f"⭐ Новый отзыв по сделке №{pending['deal_id']}\n"
            f"От: {user.full_name} (ID {user.id})\n"
            f"Оценка: {review_stars(pending['rating'])} ({pending['rating']}/5)"
            f"{review_body}"
        )
    except Exception:
        pass


@dp.message(Command("skip"))
async def review_skip(message: Message):
    if message.from_user and message.from_user.id in pending_review:
        await finish_review(message, NO_REVIEW_TEXT)


@dp.message(Command("cancelreview"))
async def review_cancel(message: Message):
    if message.from_user and pending_review.pop(message.from_user.id, None):
        await message.answer("Отзыв отменён.")


@dp.message(
    F.text, ~F.text.startswith("/"),
    lambda m: m.from_user is not None and m.from_user.id in pending_review
)
async def review_text(message: Message):
    text = message.text.strip()
    if not 1 <= len(text) <= 1000:
        await message.answer("Текст отзыва должен содержать от 1 до 1000 символов.")
        return
    await finish_review(message, text)


@dp.message(Command("reviews"))
async def reviews_command(message: Message):
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT id, author_name, rating, text, created_at FROM reviews "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
        stats = con.execute(
            "SELECT COUNT(*) AS total, AVG(rating) AS average FROM reviews"
        ).fetchone()
    if not rows:
        await message.answer("Отзывов пока нет. Оставить отзыв можно после сделки: /addreview")
        return
    average = float(stats["average"] or 0)
    uid = message.from_user.id if message.from_user else 0
    text = f"⭐ ОТЗЫВЫ NOTFUNPAY\nВсего: {stats['total']} | Средняя оценка: {average:.1f}/5\n"
    for row in rows:
        text += (
            f"\n{review_stars(row['rating'])} ({row['rating']}/5) — {row['author_name']}\n"
            f"{row['text']}\n"
            f"🕒 {row['created_at']}"
            + (f" | #{row['id']}" if uid == ADMIN_ID else "") + "\n"
        )
    await message.answer(text[:MAX_TEXT_LEN])


@dp.message(Command("delreview"))
async def delete_review(message: Message):
    """Модерация для гаранта: /delreview ID"""
    if not await admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /delreview ID (ID виден в /reviews)")
        return
    with closing(connect()) as con:
        cur = con.execute("DELETE FROM reviews WHERE id=?", (int(parts[1]),))
    await message.answer("🗑 Отзыв удалён." if cur.rowcount else "Отзыв не найден.")


# ================= FAKE REVIEWS + FAQ =================
def fake_time(raw):
    """
    Приводит время из /addfake к формату 'YYYY-MM-DD HH:MM:SS'.
    Понимает:
      2025-01-15 14:30  /  2025-01-15   — конкретная дата (если не указать время — 00:00)
      30м / 5ч / 2д / 1н                — 'N минут/часов/дней/недель назад'
      пусто                             — None (используем текущее время)
    Возвращает None, если строку не удалось распознать.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.fullmatch(r"(\d+)\s*([мmчhдdнw])", raw.lower())
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        kwargs = {"м": {"minutes": n}, "m": {"minutes": n},
                  "ч": {"hours": n}, "h": {"hours": n},
                  "д": {"days": n}, "d": {"days": n},
                  "н": {"weeks": n}, "w": {"weeks": n}}[unit]
        return (datetime.now() - timedelta(**kwargs)).strftime("%Y-%m-%d %H:%M:%S")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None


@dp.message(Command("addfake"))
async def add_fake(message: Message):
    """Гарант добавляет фейк-отзыв: /addfake 5 Автор|Текст|время
    Время необязательно: без него ставится текущее."""
    if not await admin_only(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or parts[1] not in {"1", "2", "3", "4", "5"}:
        await message.answer(
            "Формат: /addfake 5 Автор|Текст|время\n"
            "Время (можно опустить): 2025-01-15 14:30 или 30м / 5ч / 2д / 1н"
        )
        return
    head, sep, rest = parts[2].partition("|")
    text_part, tsep, time_raw = rest.rpartition("|")
    if not sep:
        await message.answer("Нужен формат: /addfake 5 Автор|Текст|время (текст не должен содержать |)")
        return
    if tsep:
        author_name, text, time_str = head, text_part, time_raw
    else:
        author_name, text, time_str = head, rest, None
    author_name = author_name.strip()[:100]
    text = text.strip()[:1000]
    if not author_name or not text:
        await message.answer("Автор и текст не могут быть пустыми.")
        return
    created_at = fake_time(time_str)
    if time_str and created_at is None:
        await message.answer(
            "Не понял время. Примеры: 2025-01-15 14:30, 2025-01-15, 30м, 5ч, 2д, 1н"
        )
        return
    if created_at is None:
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(connect()) as con:
        cur = con.execute(
            "INSERT INTO reviews(author_id, author_name, rating, text, created_at, deal_id, target_id) "
            "VALUES(0,?,?,?,?,NULL,NULL)",
            (author_name, int(parts[1]), text, created_at),
        )
    await message.answer(
        f"✅ Фейк-отзыв добавлен: ID {cur.lastrowid}, время {created_at}. Проверка: /reviews"
    )


@dp.message(Command("faq"))
async def faq_command(message: Message):
    if not message.from_user:
        return
    await show_faq(message, message.from_user.id, edit=False)


def faq_menu_keyboard(uid):
    """Список вопросов с кнопками. Гарант видит ID для /delfaq."""
    with closing(connect()) as con:
        rows = con.execute("SELECT id, question FROM faq ORDER BY id").fetchall()
    if not rows:
        return None, []
    buttons = [[InlineKeyboardButton(
        text=("❓ " + r["question"][:60]) + (f" | #{r['id']}" if uid == ADMIN_ID else ""),
        callback_data=f"faq:{r['id']}",
    )] for r in rows]
    return InlineKeyboardMarkup(inline_keyboard=buttons), rows


async def show_faq(target, uid, edit=False):
    """Отправляет (или редактирует на месте) меню FAQ — анти-спам."""
    kb, rows = faq_menu_keyboard(uid)
    if rows:
        text = "❓ ЧАСТЫЕ ВОПРОСЫ\n\nВыберите вопрос:"
    else:
        text = "Вопросов пока нет.\nГарант может добавить: /addfaq Вопрос|Ответ"
    if edit:
        try:
            await target.edit_text(text[:MAX_TEXT_LEN], reply_markup=kb)
            return
        except TelegramBadRequest:
            pass
    await target.answer(text[:MAX_TEXT_LEN], reply_markup=kb)


@dp.callback_query(F.data.startswith("faq:"))
async def faq_callback(callback: CallbackQuery):
    if callback.message is None:
        await callback.answer()
        return
    payload = callback.data.split(":", 1)[1] if callback.data else ""
    uid = callback.from_user.id if callback.from_user else 0
    if payload == "back":
        await show_faq(callback.message, uid, edit=True)
        await callback.answer()
        return
    qid = safe_int(payload)
    with closing(connect()) as con:
        row = con.execute("SELECT question, answer FROM faq WHERE id=?", (qid,)).fetchone()
    if not row:
        await callback.answer("Вопрос не найден.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад", callback_data="faq:back")
    ]])
    body = f"❓ {row['question']}\n\n✅ {row['answer']}"[:MAX_TEXT_LEN]
    try:
        await callback.message.edit_text(body, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(body, reply_markup=kb)
    await callback.answer()


@dp.message(Command("addfaq"))
async def add_faq(message: Message):
    """Гарант: /addfaq Вопрос|Ответ"""
    if not await admin_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or "|" not in parts[1]:
        await message.answer("Формат: /addfaq Вопрос|Ответ")
        return
    question, _, answer = parts[1].partition("|")
    question, answer = question.strip(), answer.strip()
    if not question or not answer:
        await message.answer("Вопрос и ответ не могут быть пустыми.")
        return
    if len(question) > 100 or len(answer) > 1000:
        await message.answer("Вопрос до 100 символов, ответ до 1000.")
        return
    with closing(connect()) as con:
        cur = con.execute("INSERT INTO faq(question, answer) VALUES(?,?)", (question, answer))
    await message.answer(f"✅ Вопрос добавлен (ID {cur.lastrowid}). Посмотреть: /faq")


@dp.message(Command("delfaq"))
async def delete_faq(message: Message):
    """Гарант: /delfaq ID (ID виден в /faq)"""
    if not await admin_only(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /delfaq ID (ID виден в /faq)")
        return
    with closing(connect()) as con:
        cur = con.execute("DELETE FROM faq WHERE id=?", (int(parts[1]),))
    await message.answer("🗑 Вопрос удалён." if cur.rowcount else "Вопрос не найден.")


# ================= USER CALLBACKS =================
@dp.callback_query(F.data.startswith("agree:"))
async def agree(callback: CallbackQuery):
    deal_id = safe_int(callback.data.split(":")[-1])
    if deal_id is None:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    uid = callback.from_user.id
    with closing(connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        d = con.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if not d or uid not in (d["seller"], d["buyer"]):
            con.rollback()
            await callback.answer("Нет доступа.", show_alert=True)
            return
        if d["status"] != "negotiating":
            con.rollback()
            await callback.answer("Согласование уже закрыто.")
            return
        field = "seller_ok" if uid == d["seller"] else "buyer_ok"
        if d[field]:
            con.rollback()
            await callback.answer("Вы уже подтвердили условия.")
            return
        con.execute(f"UPDATE deals SET {field}=1 WHERE id=?", (deal_id,))
        updated = con.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if updated["seller_ok"] and updated["buyer_ok"]:
            con.execute("UPDATE deals SET status='awaiting_payment' WHERE id=?", (deal_id,))
            log_action(con, deal_id, uid, "terms_accepted_by_both")
        else:
            log_action(con, deal_id, uid, "terms_accepted")
        con.commit()
    d = get_deal(deal_id)
    await callback.answer("Условия подтверждены.")
    await notify_deal(d)


@dp.callback_query(F.data.startswith("deliver:"))
async def deliver(callback: CallbackQuery):
    deal_id = safe_int(callback.data.split(":")[-1])
    if deal_id is None:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    d = get_deal(deal_id)
    if not d or callback.from_user.id != d["seller"]:
        await callback.answer("Только продавец может это сделать.", show_alert=True)
        return
    if change_status(deal_id, "paid", "delivered", callback.from_user.id, "seller_delivered"):
        await callback.answer("Передача отмечена.")
        await notify_deal(get_deal(deal_id))
    else:
        await callback.answer("Статус сделки изменился.", show_alert=True)


@dp.callback_query(F.data.startswith("complete:"))
async def complete(callback: CallbackQuery):
    deal_id = safe_int(callback.data.split(":")[-1])
    if deal_id is None:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    d = get_deal(deal_id)
    if not d or callback.from_user.id != d["buyer"]:
        await callback.answer("Только покупатель может подтвердить.", show_alert=True)
        return
    if change_status(deal_id, "delivered", "completed", callback.from_user.id, "buyer_confirmed_completion"):
        await callback.answer("Сделка завершена.")
        await notify_deal(get_deal(deal_id))
        await ask_for_reviews(get_deal(deal_id))
    else:
        await callback.answer("Статус сделки изменился.", show_alert=True)


@dp.callback_query(F.data.startswith("dispute:"))
async def dispute(callback: CallbackQuery):
    deal_id = safe_int(callback.data.split(":")[-1])
    if deal_id is None:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    # FIX: открывать спор может только гарант, а не участник сделки.
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Открыть спор может только гарант.", show_alert=True)
        return
    d, error = await open_dispute_as_admin(deal_id)
    if error:
        await callback.answer(error, show_alert=True)
        return
    await callback.answer("Спор открыт.")


# ================= DISPUTE (только гарант) =================
async def open_dispute_as_admin(deal_id):
    """Гарант открывает спор по сделке. Возвращает (сделка, текст_ошибки)."""
    d = get_deal(deal_id)
    if not d:
        return None, "Сделка не найдена."
    if d["status"] in ("completed", "refunded", "partially_refunded", "disputed"):
        return None, "Спор нельзя открыть в этом статусе."
    if not change_status(deal_id, d["status"], "disputed", ADMIN_ID, "dispute_opened_by_guarant"):
        return None, "Статус изменился; обнови список."
    d = get_deal(deal_id)
    await notify_deal(d, "⚠️ Гарант открыл спор по вашей сделке.\n\n" + deal_text(d))
    try:
        await bot.send_message(
            ADMIN_ID, "⚠️ Спор открыт (гарант).\n\n" + deal_text(d),
            reply_markup=admin_deal_keyboard(d)
        )
    except Exception:
        pass
    return d, None


# ================= ADMIN COMMANDS / PANEL =================
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if await admin_only(message):
        await message.answer("🛡 NotFunPay — ПАНЕЛЬ ГАРАНТА\nКомиссия: 0%", reply_markup=admin_menu())


@dp.message(Command("paid"))
async def paid_command(message: Message):
    if not await admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /paid ID")
        return
    await perform_admin_action(message, "paid", int(parts[1]))


@dp.message(Command("close"))
async def close_command(message: Message):
    if not await admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /close ID")
        return
    await perform_admin_action(message, "close", int(parts[1]))


@dp.message(Command("refund"))
async def refund_command(message: Message):
    if not await admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /refund ID")
        return
    await perform_admin_action(message, "refund", int(parts[1]))


@dp.message(Command("dispute"))
async def dispute_command(message: Message):
    """FIX: гарант открывает спор командой /dispute ID."""
    if not await admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /dispute ID")
        return
    d, error = await open_dispute_as_admin(int(parts[1]))
    if error:
        await message.answer(error)
        return
    await message.answer("⚠️ Спор открыт.\n\n" + deal_text(d), reply_markup=admin_deal_keyboard(d))


async def perform_admin_action(message, action, deal_id):
    config = ADMIN_ACTIONS[action]
    d = get_deal(deal_id)
    if not d:
        await message.answer("Сделка не найдена.")
        return
    if d["status"] not in config["from_status"]:
        # FIX: .get() вместо прямого обращения — KeyError при неизвестном статусе.
        await message.answer(
            f"Действие недоступно при статусе: {STATUSES.get(d['status'], d['status'])}"
        )
        return
    if change_status(deal_id, d["status"], config["to_status"], message.from_user.id, config["audit"]):
        updated = get_deal(deal_id)
        await message.answer(config["notice"] + "\n\n" + deal_text(updated))
        await notify_deal(updated, config["notice"] + "\n\n" + deal_text(updated))
        await ask_for_reviews(updated)
    else:
        await message.answer("Статус изменился. Проверь сделку ещё раз.")


@dp.message(Command("disputes"))
async def disputes_command(message: Message):
    if not await admin_only(message):
        return
    await show_admin_deals(message, ("disputed",), "⚠️ ОТКРЫТЫЕ СПОРЫ")


@dp.message(Command("history"))
async def history_command(message: Message):
    if not await admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /history ID")
        return
    await send_history(message, int(parts[1]))


async def show_admin_deals(message, statuses, title):
    placeholders = ",".join("?" for _ in statuses)
    with closing(connect()) as con:
        rows = con.execute(
            f"SELECT * FROM deals WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT 30",
            statuses
        ).fetchall()
    await message.answer(title)
    if not rows:
        await message.answer("Сделок в этом разделе нет.")
        return
    first = True
    for d in rows:
        # FIX: пауза, чтобы не поймать flood limit Telegram.
        if not first:
            await asyncio.sleep(0.05)
        first = False
        await message.answer(deal_text(d), reply_markup=admin_deal_keyboard(d))


async def send_history(message, deal_id):
    d = get_deal(deal_id)
    if not d:
        await message.answer("Сделка не найдена.")
        return
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT actor, action, created_at FROM audit WHERE deal_id=? ORDER BY id DESC LIMIT 30",
            (deal_id,)
        ).fetchall()
    text = "\n".join(
        f"{r['created_at']} | ID {r['actor']} | {r['action']}" for r in rows
    ) or "История пока пуста."
    # FIX: обрезаем итог до безопасной длины (deal_text + 30 строк могли превысить 4096).
    result = ("🕓 ИСТОРИЯ СДЕЛКИ\n\n" + deal_text(d) + "\n\n" + text)[:MAX_TEXT_LEN]
    await message.answer(result)


@dp.callback_query(F.data.startswith("panel:"))
async def admin_panel_navigation(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    section = parts[1]
    if section == "home":
        await callback.message.answer("🛡 NotFunPay — ПАНЕЛЬ ГАРАНТА", reply_markup=admin_menu())
    elif section == "active":
        await show_admin_deals(callback.message, ADMIN_ACTIVE, "📋 АКТИВНЫЕ СДЕЛКИ")
    elif section == "disputes":
        await show_admin_deals(callback.message, ("disputed",), "⚠️ ОТКРЫТЫЕ СПОРЫ")
    elif section == "logs":
        with closing(connect()) as con:
            rows = con.execute(
                "SELECT deal_id, actor, action, created_at FROM audit ORDER BY id DESC LIMIT 15"
            ).fetchall()
        if not rows:
            await callback.message.answer("История пока пуста.")
        else:
            text = "\n".join(
                f"#{r['deal_id']} | {r['created_at']} | ID {r['actor']} | {r['action']}"
                for r in rows
            )
            await callback.message.answer(("🕓 ПОСЛЕДНИЕ ДЕЙСТВИЯ\n\n" + text)[:MAX_TEXT_LEN])
    elif section == "history" and len(parts) == 3 and parts[2].isdigit():
        await send_history(callback.message, int(parts[2]))
    else:
        await callback.answer("Неизвестный раздел.")
        return
    await callback.answer()


@dp.callback_query(F.data.startswith("ask:"))
async def ask_admin_confirmation(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    action, deal_id = parts[1], int(parts[2])
    config = ADMIN_ACTIONS.get(action)
    d = get_deal(deal_id)
    if not config or not d:
        await callback.answer("Действие или сделка не найдены.", show_alert=True)
        return
    if d["status"] not in config["from_status"]:
        await callback.answer("Действие недоступно при текущем статусе.", show_alert=True)
        return
    confirm = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"do:{action}:{deal_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="panel:home")],
    ])
    await callback.message.answer(
        f"Подтверди: {config['label']}\n\n{deal_text(d)}\n\n"
        "Напоминание: статус не означает, что перевод денег выполнен.",
        reply_markup=confirm
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("do:"))
async def execute_admin_action(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    action, deal_id = parts[1], int(parts[2])
    config = ADMIN_ACTIONS.get(action)
    d = get_deal(deal_id)
    if not config or not d:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return
    if d["status"] not in config["from_status"]:
        await callback.answer("Статус изменился; обнови список.", show_alert=True)
        return
    if change_status(deal_id, d["status"], config["to_status"], callback.from_user.id, config["audit"]):
        updated = get_deal(deal_id)
        await callback.message.answer(config["notice"] + "\n\n" + deal_text(updated))
        await notify_deal(updated, config["notice"] + "\n\n" + deal_text(updated))
        await ask_for_reviews(updated)
        await callback.answer("Статус обновлён.")
    else:
        await callback.answer("Статус изменился; обнови список.", show_alert=True)


# ================= DISPUTE RESOLUTION =================
@dp.callback_query(F.data.startswith("resolve:"))
async def choose_resolution(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    outcome, deal_id = parts[1], int(parts[2])
    d = get_deal(deal_id)
    if outcome not in RESOLUTION_LABELS or not d or d["status"] != "disputed":
        await callback.answer("Сделка не найдена или спор уже закрыт.", show_alert=True)
        return
    pending_resolution[callback.from_user.id] = {"deal_id": deal_id, "outcome": outcome}
    if outcome == "partial":
        prompt = "Отправь сумму возврата и причину одним сообщением. Пример: 500 Частичное выполнение заказа."
    else:
        prompt = "Напиши причину решения одним сообщением."
    await callback.message.answer(
        f"Решение: {RESOLUTION_LABELS[outcome]}\n"
        f"Сделка №{deal_id}, сумма {d['amount']:.2f}\n\n"
        f"{prompt}\nДля отмены: /canceldecision"
    )
    await callback.answer()


@dp.message(Command("canceldecision"))
async def cancel_resolution(message: Message):
    if not await admin_only(message):
        return
    pending_resolution.pop(message.from_user.id, None)
    await message.answer("Ввод решения отменён.")


@dp.message(F.text)
async def receive_resolution_reason(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    pending = pending_resolution.get(message.from_user.id)
    if not pending:
        return
    if message.text.startswith("/"):
        await message.answer("Заверши ввод решения или отправь /canceldecision.")
        return

    deal_id = pending["deal_id"]
    outcome = pending["outcome"]
    d = get_deal(deal_id)
    if not d or d["status"] != "disputed":
        pending_resolution.pop(message.from_user.id, None)
        await message.answer("Спор уже обработан или сделка не найдена.")
        return

    refund_amount = 0.0
    if outcome == "partial":
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Формат: 500 Причина частичного возврата")
            return
        try:
            refund_amount = float(parts[0].replace(",", "."))
        except ValueError:
            await message.answer("Первым укажи сумму возврата, например: 500 Причина")
            return
        reason = parts[1].strip()
        if not 0 < refund_amount < d["amount"]:
            await message.answer(f"Возврат должен быть больше 0 и меньше {d['amount']:.2f}.")
            return
        new_status = "partially_refunded"
    else:
        reason = message.text.strip()
        if outcome == "buyer":
            refund_amount = float(d["amount"])
            new_status = "refunded"
        else:
            refund_amount = 0.0
            new_status = "completed"

    if not reason or len(reason) > 1000:
        await message.answer("Причина должна содержать от 1 до 1000 символов.")
        return

    with closing(connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if not current or current["status"] != "disputed":
            con.rollback()
            pending_resolution.pop(message.from_user.id, None)
            await message.answer("Спор уже обработан другим действием.")
            return
        con.execute(
            "UPDATE deals SET status=?, resolution=?, resolution_reason=?, "
            "refund_amount=?, resolved_by=?, resolved_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='disputed'",
            (new_status, outcome, reason, refund_amount, message.from_user.id, deal_id)
        )
        log_action(
            con, deal_id, message.from_user.id,
            f"dispute_resolution:{outcome};refund={refund_amount:.2f};reason={reason}"
        )
        if new_status == "completed":
            credit_seller_balance(con, deal_id, message.from_user.id)
        con.commit()

    pending_resolution.pop(message.from_user.id, None)
    updated = get_deal(deal_id)
    result = (
        "⚖️ Решение по спору сохранено\n\n" + deal_text(updated) +
        f"\n\nРешение: {RESOLUTION_LABELS[outcome]}" +
        f"\nОтмеченный возврат: {refund_amount:.2f}" +
        f"\nПричина: {reason}" +
        "\n\nЭто запись решения, а не подтверждение банковского перевода."
    )
    await message.answer(result)
    await notify_deal(updated, result)
    await ask_for_reviews(updated)


# ================= START =================
async def main():
    init_db()
    # FIX: сбрасываем непосланные апдейты, чтобы после рестарта бот не заваливал
    # пользователей старыми сообщениями.
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    me = await bot.get_me()
    print(f"Бот запущен: @{me.username or 'unknown'}. База данных: {DB_PATH}")
    try:
        await dp.start_polling(bot)
    finally:
        # FIX: aiogram сам закрывает сессию после polling — закрываем только если ещё открыта.
        if not bot.session.closed:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
