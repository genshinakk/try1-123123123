# =====================================================================
# NotFunPay — гарант-бот (aiogram 3.x)
# =====================================================================
# Запуск:
#   BOT_TOKEN="ваш_токен" ADMIN_ID="ваш_telegram_id" python notfunpay_bot_fixed.py
#
# ВНИМАНИЕ (безопасность): токен ниже оставлен как значение по умолчанию,
# но он был опубликован в переписке и считается скомпрометированным!
# Перевыпустите токен у @BotFather и задавайте его через переменную окружения
# BOT_TOKEN, иначе любой, кто видел код, сможет управлять ботом.
# =====================================================================

import asyncio
import os
import sqlite3
from contextlib import closing, suppress

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
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

# Автоочистка: по умолчанию закрытые сделки хранятся 30 дней,
# проверка выполняется при запуске и далее каждые 6 часов.
# DEAL_RETENTION_DAYS=0 полностью отключает автоматическое удаление.
try:
    DEAL_RETENTION_DAYS = int(os.getenv("DEAL_RETENTION_DAYS", "30"))
    CLEANUP_INTERVAL_HOURS = int(os.getenv("CLEANUP_INTERVAL_HOURS", "6"))
except ValueError as exc:
    raise RuntimeError("DEAL_RETENTION_DAYS и CLEANUP_INTERVAL_HOURS должны быть целыми числами.") from exc
if DEAL_RETENTION_DAYS < 0:
    raise RuntimeError("DEAL_RETENTION_DAYS не может быть отрицательным.")
if CLEANUP_INTERVAL_HOURS <= 0:
    raise RuntimeError("CLEANUP_INTERVAL_HOURS должен быть больше нуля.")
CLEANUP_BATCH_SIZE = 200

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

        # Миграция для баз, созданных предыдущими версиями бота.
        existing = {row["name"] for row in con.execute("PRAGMA table_info(deals)")}
        migrations = {
            "resolution": "TEXT",
            "resolution_reason": "TEXT",
            "refund_amount": "REAL NOT NULL DEFAULT 0",
            "resolved_by": "INTEGER",
            "resolved_at": "TEXT",
            "closed_at": "TEXT",
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
        if new_status in FINAL_STATUSES:
            cur = con.execute(
                "UPDATE deals SET status=?, closed_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status=?",
                (new_status, deal_id, expected)
            )
        else:
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


def cleanup_old_deals():
    """Удаляет старые закрытые сделки и связанный мусор одной транзакцией.

    Балансы и balance_transactions намеренно сохраняются: это финансовая история.
    Возвращает количество удалённых сделок.
    """
    if DEAL_RETENTION_DAYS == 0:
        return 0

    status_placeholders = ",".join("?" for _ in FINAL_STATUSES)
    cutoff = f"-{DEAL_RETENTION_DAYS} days"
    with closing(connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute(
            f"SELECT id FROM deals WHERE status IN ({status_placeholders}) "
            "AND datetime(COALESCE(closed_at, resolved_at, created_at)) <= datetime('now', ?) "
            "ORDER BY id LIMIT ?",
            (*FINAL_STATUSES, cutoff, CLEANUP_BATCH_SIZE)
        ).fetchall()
        deal_ids = [row["id"] for row in rows]
        if not deal_ids:
            con.rollback()
            return 0

        id_placeholders = ",".join("?" for _ in deal_ids)
        con.execute(
            f"DELETE FROM reviews WHERE deal_id IN ({id_placeholders})",
            deal_ids
        )
        con.execute(
            f"DELETE FROM audit WHERE deal_id IN ({id_placeholders})",
            deal_ids
        )
        cur = con.execute(
            f"DELETE FROM deals WHERE id IN ({id_placeholders})",
            deal_ids
        )
        con.commit()
        return cur.rowcount


async def cleanup_worker():
    """Периодически очищает базу, не блокируя обработку сообщений."""
    while True:
        try:
            deleted = await asyncio.to_thread(cleanup_old_deals)
            if deleted:
                print(f"Автоочистка: удалено закрытых сделок: {deleted}")
        except Exception as exc:
            print(f"Ошибка автоочистки: {type(exc).__name__}: {exc}")
        await asyncio.sleep(CLEANUP_INTERVAL_HOURS * 3600)


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


async def delete_msg(message):
    """Удаляет сообщение (например, команду пользователя), не падая на ошибках."""
    try:
        if message is not None:
            await message.delete()
    except Exception:
        pass


def _not_modified(exc) -> bool:
    return "message is not modified" in str(exc).lower()


async def safe_edit(message, text, reply_markup=None):
    """Редактирует сообщение на месте. 'message is not modified' ошибкой не считается."""
    try:
        await message.edit_text(text[:MAX_TEXT_LEN], reply_markup=reply_markup)
        return True
    except Exception as exc:
        if _not_modified(exc):
            return True
        print(f"safe_edit: {type(exc).__name__}: {exc}")
        return False


# Последние карточки сделок в чатах: {(deal_id, user_id): (chat_id, message_id)}.
# Позволяет обновлять карточку сделки на месте, а не плодить новые сообщения.
deal_notifications = {}


def track_card(deal_id, uid, message):
    """Запоминает карточку сделки, чтобы потом обновлять её на месте."""
    if message is not None:
        deal_notifications[(deal_id, uid)] = (message.chat.id, message.message_id)


async def notify_deal(d, text=None):
    """АНТИ-МУСОР: обновляет карточку сделки у участников НА МЕСТЕ (edit_text).
    Если карточку найти/обновить нельзя — старая удаляется и шлётся новая.
    В итоге на сделку в чате одна карточка, а не десятки сообщений."""
    content = text or deal_text(d)
    kb = user_deal_keyboard(d)
    for uid in {d["seller"], d["buyer"]} - {None}:
        key = (d["id"], uid)
        old = deal_notifications.get(key)
        if old is not None:
            chat_id, message_id = old
            try:
                await bot.edit_message_text(
                    content[:MAX_TEXT_LEN], chat_id=chat_id,
                    message_id=message_id, reply_markup=kb
                )
                if d["status"] in FINAL_STATUSES:
                    deal_notifications.pop(key, None)
                continue
            except Exception as exc:
                if _not_modified(exc):
                    continue
            # Старая карточка недоступна — удаляем её и шлём новую.
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:
                pass
            deal_notifications.pop(key, None)
        try:
            msg = await bot.send_message(uid, content[:MAX_TEXT_LEN], reply_markup=kb)
            deal_notifications[key] = (msg.chat.id, msg.message_id)
            if d["status"] in FINAL_STATUSES:
                deal_notifications.pop(key, None)
        except Exception:
            pass


USER_COMMANDS = [
    BotCommand(command="start", description="Запустить бота"),
    BotCommand(command="new", description="Создать сделку"),
    BotCommand(command="mydeals", description="Мои сделки"),
    BotCommand(command="deal", description="Показать сделку по номеру"),
    BotCommand(command="balance", description="Мой баланс"),
    BotCommand(command="reviews", description="Смотреть отзывы"),
    BotCommand(command="addreview", description="Оставить отзыв"),
    BotCommand(command="help", description="Помощь"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="admin", description="Панель гаранта"),
    BotCommand(command="active", description="Активные сделки"),
    BotCommand(command="disputes", description="Открытые споры"),
    BotCommand(command="paid", description="Подтвердить оплату: /paid ID"),
    BotCommand(command="dispute", description="Открыть спор: /dispute ID"),
    BotCommand(command="refund", description="Отметить возврат: /refund ID"),
    BotCommand(command="close", description="Закрыть спор: /close ID"),
    BotCommand(command="history", description="История сделки: /history ID"),
    BotCommand(command="cleanup", description="Запустить очистку вручную"),
    BotCommand(command="delreview", description="Удалить отзыв: /delreview ID"),
    BotCommand(command="addfake", description="Добавить отзыв вручную: /addfake 5 Имя|Текст"),
]


async def setup_quick_commands():
    """Публикует меню команд у пользователей и расширенное меню у гаранта."""
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    await bot.set_my_commands(
        ADMIN_COMMANDS,
        scope=BotCommandScopeChat(chat_id=ADMIN_ID)
    )


# ================= USER COMMANDS =================
@dp.message(CommandStart())
async def start(message: Message):
    # FIX: message.text/from_user могут отсутствовать (редкие типы апдейтов).
    if not message.from_user or message.text is None:
        return
    # АНТИ-МУСОР: команду пользователя удаляем, ответ бота остаётся.
    await delete_msg(message)
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
        msg = await message.answer(
            "Вы присоединились к сделке. Проверьте условия и подтвердите их кнопкой.\n\n"
            + deal_text(d), reply_markup=user_deal_keyboard(d)
        )
        track_card(deal_id, uid, msg)
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
    "🔎 Сделка по номеру: /deal ID\n"
    "💰 Мой баланс: /balance\n"
    "⭐ Отзывы: /reviews, оставить отзыв: /addreview\n"
    "ℹ️ Помощь: /help\n\n"
    "👨‍💼 Для гаранта: /admin",
    parse_mode="HTML"
)


@dp.message(Command("help"))
async def help_command(message: Message):
    await delete_msg(message)
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
    "🔎 Сделка по номеру: /deal ID\n"
        "💰 Мой баланс: /balance\n"
        "⭐ Отзывы: /reviews, оставить отзыв: /addreview\n"
        "ℹ️ Помощь: /help\n\n"
        "👨‍💼 Для гаранта: /admin"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("new"))
async def new_deal(message: Message):
    if not message.from_user:
        return
    await delete_msg(message)
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
    msg = await message.answer(
        deal_text(d) + "\n\nОтправь покупателю ссылку:\n" + link +
        "\n\nВажно: оплата ещё не проверена. Не передавайте товар до подтверждения гаранта."
    )
    track_card(deal_id, message.from_user.id, msg)


@dp.message(Command("balance"))
async def balance_command(message: Message):
    if not message.from_user:
        return
    await delete_msg(message)
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
    await delete_msg(message)
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
        msg = await message.answer(deal_text(d), reply_markup=user_deal_keyboard(d))
        # АНТИ-МУСОР: запоминаем карточку, чтобы потом обновлять её на месте.
        if d["status"] not in FINAL_STATUSES:
            track_card(d["id"], message.from_user.id, msg)


@dp.message(Command("deal"))
async def deal_command(message: Message):
    """Быстрый просмотр сделки: участникам — своя, гаранту — любая."""
    if not message.from_user or not message.text:
        return
    await delete_msg(message)
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /deal ID")
        return
    d = get_deal(int(parts[1]))
    if not d:
        await message.answer("Сделка не найдена.")
        return
    uid = message.from_user.id
    if uid != ADMIN_ID and uid not in (d["seller"], d["buyer"]):
        await message.answer("Нет доступа к этой сделке.")
        return
    if uid == ADMIN_ID:
        await message.answer(deal_text(d), reply_markup=admin_deal_keyboard(d))
        return
    msg = await message.answer(deal_text(d), reply_markup=user_deal_keyboard(d))
    if d["status"] not in FINAL_STATUSES:
        track_card(d["id"], uid, msg)


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
    await delete_msg(message)
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
    prompt = f"⭐ Отзыв по сделке №{deal_id}\nВыберите оценку:"
    if (callback.message.text or "").startswith("⭐ Выберите сделку"):
        # АНТИ-МУСОР: нажали из списка /addreview — редактируем список на месте.
        await safe_edit(callback.message, prompt, kb)
    else:
        # Нажали кнопку на карточке сделки — карточку не трогаем, шлём отдельное сообщение.
        try:
            await callback.message.answer(prompt, reply_markup=kb)
        except Exception:
            pass
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
    if callback.message is None:
        pending_review[callback.from_user.id] = {"deal_id": deal_id, "rating": rating}
        await callback.answer()
        return
    # АНТИ-МУСОР: запоминаем сообщение с оценкой, чтобы после отправки отзыва
    # отредактировать его в итоговый ответ, а не слать новое.
    src = None
    try:
        src = (callback.message.chat.id, callback.message.message_id)
    except AttributeError:
        src = None
    pending_review[callback.from_user.id] = {
        "deal_id": deal_id, "rating": rating, "src": src,
    }
    await safe_edit(
        callback.message,
        f"Оценка: {review_stars(rating)} ({rating}/5)\n\n"
        "Напишите текст отзыва одним сообщением (до 1000 символов).\n"
        "/skip — отправить только оценку\n"
        "/cancelreview — отмена",
        None
    )
    await callback.answer()


async def finish_review(message: Message, text):
    pending = pending_review.pop(message.from_user.id, None)
    if not pending:
        return
    ok, result = save_review(message.from_user, pending["deal_id"], pending["rating"], text)
    # АНТИ-МУСОР: сообщение пользователя с текстом отзыва удаляем.
    await delete_msg(message)
    thanks = "✅ Спасибо! Отзыв опубликован. Все отзывы: /reviews" if ok else f"⛔ {result}"
    # АНТИ-МУСОР: сообщение с выбором оценки редактируем в итоговый ответ.
    edited = False
    src = pending.get("src")
    if src:
        try:
            await bot.edit_message_text(thanks, chat_id=src[0], message_id=src[1])
            edited = True
        except Exception:
            pass
    if not edited:
        await message.answer(thanks)
    if not ok:
        return
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
    cancelled = bool(message.from_user and pending_review.pop(message.from_user.id, None))
    await delete_msg(message)
    if cancelled:
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
    await delete_msg(message)
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
    await delete_msg(message)
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /delreview ID (ID виден в /reviews)")
        return
    with closing(connect()) as con:
        cur = con.execute("DELETE FROM reviews WHERE id=?", (int(parts[1]),))
    await message.answer("🗑 Отзыв удалён." if cur.rowcount else "Отзыв не найден.")


@dp.message(Command("addfake"))
async def add_fake_review(message: Message):
    """Гарант добавляет отзыв вручную: /addfake 5 Имя автора|Текст отзыва"""
    if not await admin_only(message):
        return
    await delete_msg(message)
    parts = message.text.split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer(
            "Формат: /addfake 5 Имя автора|Текст отзыва\n"
            "Пример: /addfake 5 Алексей|Всё прошло идеально, аккаунт получил!"
        )
        return
    rating = int(parts[1])
    if not 1 <= rating <= 5:
        await message.answer("Оценка должна быть от 1 до 5.")
        return
    if "|" not in parts[2]:
        await message.answer("Раздели имя и текст символом |")
        return
    author_name, text = parts[2].split("|", 1)
    author_name = author_name.strip()[:100] or "Покупатель"
    text = text.strip()[:1000]
    if not text:
        await message.answer("Текст отзыва пустой.")
        return
    with closing(connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO reviews(author_id, author_name, rating, text, deal_id, target_id) "
            "VALUES(0, ?, ?, ?, NULL, NULL)",
            (author_name, rating, text)
        )
        con.commit()
    await message.answer(
        f"✅ Отзыв добавлен: {review_stars(rating)} — {author_name}\n\n{text}"
    )



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
    # АНТИ-МУСОР: текущее сообщение превращаем в карточку сделки со спором.
    if callback.message is not None:
        await safe_edit(callback.message, deal_text(d), admin_deal_keyboard(d))
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
    return d, None


# ================= ADMIN COMMANDS / PANEL =================
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if await admin_only(message):
        await delete_msg(message)
        await message.answer("🛡 NotFunPay — ПАНЕЛЬ ГАРАНТА\nКомиссия: 0%", reply_markup=admin_menu())


@dp.message(Command("paid"))
async def paid_command(message: Message):
    if not await admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /paid ID")
        return
    await delete_msg(message)
    await perform_admin_action(message, "paid", int(parts[1]))


@dp.message(Command("close"))
async def close_command(message: Message):
    if not await admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /close ID")
        return
    await delete_msg(message)
    await perform_admin_action(message, "close", int(parts[1]))


@dp.message(Command("refund"))
async def refund_command(message: Message):
    if not await admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /refund ID")
        return
    await delete_msg(message)
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
    await delete_msg(message)
    d, error = await open_dispute_as_admin(int(parts[1]))
    if error:
        await message.answer(error)
        return
    await message.answer("⚠️ Спор открыт.\n\n" + deal_text(d), reply_markup=admin_deal_keyboard(d))


async def perform_admin_action(message, action, deal_id):
    config = ADMIN_ACTIONS[action]
    await delete_msg(message)
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


@dp.message(Command("active"))
async def active_command(message: Message):
    if not await admin_only(message):
        return
    await delete_msg(message)
    text, kb = admin_deals_list_kb(ADMIN_ACTIVE, "📋 АКТИВНЫЕ СДЕЛКИ")
    await message.answer(text[:MAX_TEXT_LEN], reply_markup=kb)


@dp.message(Command("cleanup"))
async def cleanup_command(message: Message):
    """Ручной запуск той же безопасной очистки, что работает по расписанию."""
    if not await admin_only(message):
        return
    await delete_msg(message)
    if DEAL_RETENTION_DAYS == 0:
        await message.answer("Автоочистка отключена: DEAL_RETENTION_DAYS=0.")
        return
    try:
        deleted = await asyncio.to_thread(cleanup_old_deals)
    except Exception as exc:
        print(f"Ошибка ручной очистки: {type(exc).__name__}: {exc}")
        await message.answer("Не удалось выполнить очистку. Подробности в консоли.")
        return
    await message.answer(
        f"🧹 Очистка выполнена. Удалено закрытых сделок: {deleted}.\n"
        f"Срок хранения: {DEAL_RETENTION_DAYS} дн. Балансы и начисления сохранены."
    )


@dp.message(Command("disputes"))
async def disputes_command(message: Message):
    if not await admin_only(message):
        return
    await delete_msg(message)
    text, kb = admin_deals_list_kb(("disputed",), "⚠️ ОТКРЫТЫЕ СПОРЫ")
    await message.answer(text[:MAX_TEXT_LEN], reply_markup=kb)


@dp.message(Command("history"))
async def history_command(message: Message):
    if not await admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /history ID")
        return
    await delete_msg(message)
    result = build_history(int(parts[1])) or "Сделка не найдена."
    await message.answer(result)


def admin_deals_list_kb(statuses, title):
    """АНТИ-МУСОР: список сделок ОДНИМ сообщением: текст + кнопки перехода к карточке.
    Вместо кучи сообщений — одно, которое потом ещё и редактируется на месте."""
    placeholders = ",".join("?" for _ in statuses)
    with closing(connect()) as con:
        rows = con.execute(
            f"SELECT * FROM deals WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT 15",
            statuses
        ).fetchall()
    if not rows:
        return title + "\n\nСделок в этом разделе нет.", None
    text = title + "\n"
    kb_rows = []
    for d in rows:
        desc_full = (d["description"] or "").replace(chr(10), " ")
        text += (
            f"\n№{d['id']} · {d['amount']:.2f} · {STATUSES.get(d['status'], d['status'])}\n"
            f"{desc_full[:40]}\n"
        )
        kb_rows.append([InlineKeyboardButton(
            text=f"№{d['id']} · {d['amount']:.2f} · {desc_full[:20]}",
            callback_data=f"panel:deal:{d['id']}"
        )])
    kb_rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="panel:home")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


def build_history(deal_id):
    d = get_deal(deal_id)
    if not d:
        return None
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT actor, action, created_at FROM audit WHERE deal_id=? ORDER BY id DESC LIMIT 30",
            (deal_id,)
        ).fetchall()
    text = "\n".join(
        f"{r['created_at']} | ID {r['actor']} | {r['action']}" for r in rows
    ) or "История пока пуста."
    # FIX: обрезаем итог до безопасной длины (deal_text + 30 строк могли превысить 4096).
    return ("🕓 ИСТОРИЯ СДЕЛКИ\n\n" + deal_text(d) + "\n\n" + text)[:MAX_TEXT_LEN]


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
    # АНТИ-МУСОР: вся навигация по панели редактирует ОДНО сообщение,
    # а не плодит новые на каждый клик.
    if section == "home":
        await safe_edit(callback.message, "🛡 NotFunPay — ПАНЕЛЬ ГАРАНТА\nКомиссия: 0%", admin_menu())
    elif section == "active":
        text, kb = admin_deals_list_kb(ADMIN_ACTIVE, "📋 АКТИВНЫЕ СДЕЛКИ")
        await safe_edit(callback.message, text, kb)
    elif section == "disputes":
        text, kb = admin_deals_list_kb(("disputed",), "⚠️ ОТКРЫТЫЕ СПОРЫ")
        await safe_edit(callback.message, text, kb)
    elif section == "logs":
        with closing(connect()) as con:
            rows = con.execute(
                "SELECT deal_id, actor, action, created_at FROM audit ORDER BY id DESC LIMIT 15"
            ).fetchall()
        if not rows:
            await safe_edit(callback.message, "История пока пуста.", admin_menu())
        else:
            text = "\n".join(
                f"#{r['deal_id']} | {r['created_at']} | ID {r['actor']} | {r['action']}"
                for r in rows
            )
            await safe_edit(
                callback.message, ("🕓 ПОСЛЕДНИЕ ДЕЙСТВИЯ\n\n" + text)[:MAX_TEXT_LEN],
                admin_menu()
            )
    elif section == "history" and len(parts) == 3 and parts[2].isdigit():
        result = build_history(int(parts[2])) or "Сделка не найдена."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К сделке", callback_data=f"panel:deal:{parts[2]}")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="panel:home")],
        ])
        await safe_edit(callback.message, result, kb)
    elif section == "deal" and len(parts) == 3 and parts[2].isdigit():
        d = get_deal(int(parts[2]))
        if not d:
            await safe_edit(callback.message, "Сделка не найдена.", admin_menu())
        else:
            await safe_edit(callback.message, deal_text(d), admin_deal_keyboard(d))
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
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"panel:deal:{deal_id}")],
    ])
    # АНТИ-МУСОР: подтверждение показываем на месте карточки, а не новым сообщением.
    await safe_edit(
        callback.message,
        f"Подтверди: {config['label']}\n\n{deal_text(d)}\n\n"
        "Напоминание: статус не означает, что перевод денег выполнен.",
        confirm
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
        # АНТИ-МУСОР: сообщение-подтверждение превращается в обновлённую карточку сделки.
        await safe_edit(
            callback.message,
            config["notice"] + "\n\n" + deal_text(updated),
            admin_deal_keyboard(updated)
        )
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
    pending_resolution[callback.from_user.id] = {
        "deal_id": deal_id, "outcome": outcome, "src": None,
    }
    try:
        pending_resolution[callback.from_user.id]["src"] = (
            callback.message.chat.id, callback.message.message_id
        )
    except AttributeError:
        pass
    if outcome == "partial":
        prompt = "Отправь сумму возврата и причину одним сообщением. Пример: 500 Частичное выполнение заказа."
    else:
        prompt = "Напиши причину решения одним сообщением."
    # АНТИ-МУСОР: карточка превращается в приглашение ввода, после ввода станет итогом.
    await safe_edit(
        callback.message,
        f"Решение: {RESOLUTION_LABELS[outcome]}\n"
        f"Сделка №{deal_id}, сумма {d['amount']:.2f}\n\n"
        f"{prompt}\nДля отмены: /canceldecision",
        None
    )
    await callback.answer()


@dp.message(Command("canceldecision"))
async def cancel_resolution(message: Message):
    if not await admin_only(message):
        return
    pending_resolution.pop(message.from_user.id, None)
    await delete_msg(message)
    await message.answer("Ввод решения отменён.")


@dp.message(F.text)
async def receive_resolution_reason(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    pending = pending_resolution.get(message.from_user.id)
    if not pending:
        return
    src = pending.get("src")
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
            "refund_amount=?, resolved_by=?, resolved_at=CURRENT_TIMESTAMP, "
            "closed_at=CURRENT_TIMESTAMP WHERE id=? AND status='disputed'",
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
    # АНТИ-МУСОР: команду гаранта удаляем, а сообщение-приглашение редактируем в итог.
    await delete_msg(message)
    edited = False
    if src:
        try:
            await bot.edit_message_text(
                result[:MAX_TEXT_LEN], chat_id=src[0], message_id=src[1],
                reply_markup=admin_deal_keyboard(updated)
            )
            edited = True
        except Exception:
            pass
    if not edited:
        await message.answer(result[:MAX_TEXT_LEN], reply_markup=admin_deal_keyboard(updated))
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
    await setup_quick_commands()
    me = await bot.get_me()
    print(f"Бот запущен: @{me.username or 'unknown'}. База данных: {DB_PATH}")
    if DEAL_RETENTION_DAYS:
        print(
            f"Автоочистка: закрытые сделки старше {DEAL_RETENTION_DAYS} дн., "
            f"проверка каждые {CLEANUP_INTERVAL_HOURS} ч."
        )
    else:
        print("Автоочистка отключена (DEAL_RETENTION_DAYS=0).")
    cleanup_task = asyncio.create_task(cleanup_worker())
    try:
        await dp.start_polling(bot)
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        # FIX: aiogram сам закрывает сессию после polling — закрываем только если ещё открыта.
        if not bot.session.closed:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
