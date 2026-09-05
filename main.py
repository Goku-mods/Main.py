import os
import sqlite3
import logging
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("8849206316:AAEAl8qRiQkzmOSkVPwMBfzBxrNl9jliBXQ")
ADMIN_ID = 8754266926

REWARD_PER_REFERRAL = 1
MIN_WITHDRAWAL = 20

# IMPORTANT:
# For membership verification Telegram needs the actual chat ID
# (or @publicusername), NOT a private invite link.
#
# Put the real chat IDs/usernames here after adding the bot to
# every required channel/group.
REQUIRED_CHATS = [
    {
        "name": "Main Channel",
        "join_url": "https://t.me/+CvMzUOQFDiczNjZl",
        "chat_id": "PASTE_MAIN_CHANNEL_CHAT_ID",
    },
    {
        "name": "Main GC",
        "join_url": "https://t.me/GOKUPROFITZONE",
        "chat_id": "@GOKUPROFITZONE",
    },
    {
        "name": "Second Channel",
        "join_url": "https://t.me/+727mMnMYUdJhMDZl",
        "chat_id": "PASTE_SECOND_CHANNEL_CHAT_ID",
    },
    {
        "name": "Second GC",
        "join_url": "https://t.me/+Ld0qauMMtjowNzg1",
        "chat_id": "PASTE_SECOND_GC_CHAT_ID",
    },
    {
        "name": "Last Channel",
        "join_url": "https://t.me/lxmodemenu",
        "chat_id": "@lxmodemenu",
    },
    {
        "name": "Last GC",
        "join_url": "https://t.me/+oY2-2veF8zU5NzY9",
        "chat_id": "PASTE_LAST_GC_CHAT_ID",
    },
]

DB_FILE = "bot.db"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            points INTEGER NOT NULL DEFAULT 0,
            referral_code TEXT UNIQUE NOT NULL,
            referred_by INTEGER,
            referral_paid INTEGER NOT NULL DEFAULT 0,
            joined_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            upi_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            processed_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def make_referral_code(user_id: int) -> str:
    return f"REF{user_id}"


def get_user(user_id: int):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row


def create_user(tg_user, referred_by=None):
    existing = get_user(tg_user.id)
    if existing:
        return existing, False

    code = make_referral_code(tg_user.id)
    conn = db()
    conn.execute("""
        INSERT INTO users
        (user_id, username, first_name, points, referral_code,
         referred_by, referral_paid, joined_at)
        VALUES (?, ?, ?, 0, ?, ?, 0, ?)
    """, (
        tg_user.id,
        tg_user.username or "",
        tg_user.first_name or "",
        code,
        referred_by,
        datetime.utcnow().isoformat(),
    ))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?", (tg_user.id,)
    ).fetchone()
    conn.close()
    return row, True


def add_referral_reward(referrer_id: int, referred_id: int) -> bool:
    if referrer_id == referred_id:
        return False

    conn = db()
    cur = conn.cursor()

    ref = cur.execute(
        "SELECT referred_by FROM users WHERE user_id = ?",
        (referred_id,),
    ).fetchone()

    if not ref or ref["referred_by"] != referrer_id:
        conn.close()
        return False

    already = cur.execute(
        "SELECT referral_paid FROM users WHERE user_id = ?",
        (referred_id,),
    ).fetchone()

    if already and already["referral_paid"]:
        conn.close()
        return False

    cur.execute(
        "UPDATE users SET points = points + ? WHERE user_id = ?",
        (REWARD_PER_REFERRAL, referrer_id),
    )
    cur.execute(
        "UPDATE users SET referral_paid = 1 WHERE user_id = ?",
        (referred_id,),
    )
    conn.commit()
    conn.close()
    return True


# ============================================================
# KEYBOARDS
# ============================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["👥 Refer Friend", "💰 Points"],
        ["💸 Withdrawal", "🆘 Help"],
    ],
    resize_keyboard=True,
)

BACK_KEYBOARD = ReplyKeyboardMarkup(
    [["⬅️ Back"]],
    resize_keyboard=True,
)


def join_keyboard():
    rows = []
    for chat in REQUIRED_CHATS:
        rows.append([
            InlineKeyboardButton(
                f"📢 Join {chat['name']}",
                url=chat["join_url"],
            )
        ])

    rows.append([
        InlineKeyboardButton("✅ Verify Joined", callback_data="verify_join")
    ])
    return InlineKeyboardMarkup(rows)


# ============================================================
# MEMBERSHIP CHECK
# ============================================================

async def is_member(bot, user_id: int, chat_id) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        }
    except Exception as exc:
        logger.warning("Membership check failed for %s: %s", chat_id, exc)
        return False


async def all_required_joined(bot, user_id: int) -> bool:
    for chat in REQUIRED_CHATS:
        chat_id = chat["chat_id"]

        if str(chat_id).startswith("PASTE_"):
            return False

        if not await is_member(bot, user_id, chat_id):
            return False

    return True


# ============================================================
# START / JOIN FLOW
# ============================================================

def extract_referrer(args):
    if not args:
        return None

    value = args[0].strip().upper()
    if not value.startswith("REF"):
        return None

    try:
        return int(value[3:])
    except ValueError:
        return None


async def send_join_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🔒 *MUST JOIN ALL*\n\n"
        "Bot use karva pela badha required channels/groups join karo.\n\n"
        "Badha join karya pachhi niche *Verify Joined* dabavo."
    )

    if update.callback_query:
        await update.callback_query.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=join_keyboard(),
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=join_keyboard(),
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referrer_id = extract_referrer(context.args)

    existing = get_user(user.id)

    if not existing:
        # Prevent self referral and only store a referrer that already exists.
        if referrer_id == user.id:
            referrer_id = None
        elif referrer_id and not get_user(referrer_id):
            referrer_id = None

        existing, _ = create_user(user, referrer_id)

    context.user_data.clear()

    if not await all_required_joined(context.bot, user.id):
        await send_join_screen(update, context)
        return

    # Pay the referral only after the referred user passes the join check.
    if existing["referred_by"]:
        if add_referral_reward(existing["referred_by"], user.id):
            try:
                await context.bot.send_message(
                    existing["referred_by"],
                    "🎉 *New Referral!*\n\n"
                    f"💰 You earned +{REWARD_PER_REFERRAL} point (₹{REWARD_PER_REFERRAL}).",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    await update.message.reply_text(
        "🎉 *Welcome!*\n\n"
        "Tamaro account active che.\n"
        "Niche thi option select karo:",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def verify_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await all_required_joined(context.bot, query.from_user.id):
        await query.message.reply_text(
            "❌ Badha required channels/groups join nathi karya.\n\n"
            "Please badha 6 join kari ne fari Verify Joined dabavo.",
            reply_markup=join_keyboard(),
        )
        return

    user = get_user(query.from_user.id)
    if user and user["referred_by"]:
        if add_referral_reward(user["referred_by"], query.from_user.id):
            try:
                await context.bot.send_message(
                    user["referred_by"],
                    "🎉 *New Referral!*\n\n"
                    f"💰 You earned +{REWARD_PER_REFERRAL} point (₹{REWARD_PER_REFERRAL}).",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    await query.message.reply_text(
        "✅ Verification successful!\n\n🏠 Main menu open thai gayu.",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# USER MENU
# ============================================================

async def refer_friend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user:
        return

    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={user['referral_code']}"

    text = (
        "👥 *Refer Friend*\n\n"
        "Aa tamaro personal referral link che:\n\n"
        f"`{link}`\n\n"
        f"👤 Successful referral = +{REWARD_PER_REFERRAL} point (₹{REWARD_PER_REFERRAL})\n\n"
        "⚠️ Reward tyare j credit thashe jyare referred user badha required joins complete kare."
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user:
        return

    conn = db()
    referral_count = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE referred_by = ? AND referral_paid = 1",
        (user["user_id"],),
    ).fetchone()["c"]
    conn.close()

    await update.message.reply_text(
        "💰 *Your Points*\n\n"
        f"💵 Balance: *₹{user['points']}*\n"
        f"⭐ Points: *{user['points']}*\n"
        f"👥 Successful Referrals: *{referral_count}*\n\n"
        f"1 point = ₹{REWARD_PER_REFERRAL}",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user:
        return

    if user["points"] < MIN_WITHDRAWAL:
        await update.message.reply_text(
            f"❌ Withdrawal mate minimum *₹{MIN_WITHDRAWAL}* joiye.\n\n"
            f"Tamaro current balance: *₹{user['points']}*",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    context.user_data["withdrawal_step"] = "upi"

    await update.message.reply_text(
        f"💸 *Withdrawal*\n\n"
        f"Minimum withdrawal: ₹{MIN_WITHDRAWAL}\n"
        f"Your balance: ₹{user['points']}\n\n"
        "Tamari UPI ID moklo.\n"
        "Example: `name@upi`",
        parse_mode="Markdown",
        reply_markup=BACK_KEYBOARD,
    )


async def help_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆘 *Help*\n\n"
        "👥 Refer Friend → Personal referral link male.\n"
        "💰 Points → Balance ane referrals check karo.\n"
        f"💸 Withdrawal → Minimum ₹{MIN_WITHDRAWAL}.\n\n"
        "Referral reward successful join verification pachhi credit thay che.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# WITHDRAWAL HANDLER
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    if text == "👥 Refer Friend":
        await refer_friend(update, context)
        return

    if text == "💰 Points":
        await points(update, context)
        return

    if text == "💸 Withdrawal":
        await withdrawal(update, context)
        return

    if text == "🆘 Help":
        await help_menu(update, context)
        return

    if text == "⬅️ Back":
        context.user_data.clear()
        await update.message.reply_text(
            "🏠 Main menu",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if context.user_data.get("withdrawal_step") == "upi":
        upi = text

        if len(upi) < 3 or "@" not in upi or " " in upi:
            await update.message.reply_text(
                "❌ Invalid UPI ID.\n\nExample: `name@upi`",
                parse_mode="Markdown",
                reply_markup=BACK_KEYBOARD,
            )
            return

        user = get_user(user_id)
        if not user or user["points"] < MIN_WITHDRAWAL:
            context.user_data.clear()
            await update.message.reply_text(
                "❌ Insufficient balance.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        amount = user["points"]

        # Reserve/deduct the balance atomically when request is created.
        conn = db()
        cur = conn.cursor()

        cur.execute(
            "UPDATE users SET points = points - ? WHERE user_id = ? AND points >= ?",
            (amount, user_id, amount),
        )

        if cur.rowcount != 1:
            conn.rollback()
            conn.close()
            await update.message.reply_text(
                "❌ Withdrawal create thai shakyo nahi. Please try again.",
                reply_markup=MAIN_KEYBOARD,
            )
            context.user_data.clear()
            return

        cur.execute("""
            INSERT INTO withdrawals
            (user_id, amount, upi_id, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
        """, (
            user_id,
            amount,
            upi,
            datetime.utcnow().isoformat(),
        ))

        withdrawal_id = cur.lastrowid
        conn.commit()
        conn.close()

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ *Withdrawal Request Submitted*\n\n"
            f"🆔 Request: #{withdrawal_id}\n"
            f"💰 Amount: ₹{amount}\n"
            f"💳 UPI: `{upi}`\n\n"
            "Admin approval pachhi payment process thashe.",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )

        admin_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=f"wd_approve_{withdrawal_id}",
                ),
                InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=f"wd_reject_{withdrawal_id}",
                ),
            ]
        ])

        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"💸 *New Withdrawal*\n\n"
                f"🆔 Request: #{withdrawal_id}\n"
                f"👤 User ID: `{user_id}`\n"
                f"👤 Username: @{user.username if user.username else 'N/A'}\n"
                f"💰 Amount: ₹{amount}\n"
                f"💳 UPI: `{upi}`",
                parse_mode="Markdown",
                reply_markup=admin_keyboard,
            )
        except Exception as exc:
            logger.error("Could not notify admin: %s", exc)

        return

    await update.message.reply_text(
        "Please menu mathi option select karo.",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# ADMIN WITHDRAWAL ACTIONS
# ============================================================

async def withdrawal_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorized.", show_alert=True)
        return

    parts = query.data.split("_")
    action = parts[1]
    withdrawal_id = int(parts[2])

    conn = db()
    row = conn.execute(
        "SELECT * FROM withdrawals WHERE id = ?",
        (withdrawal_id,),
    ).fetchone()

    if not row:
        conn.close()
        await query.edit_message_text("❌ Withdrawal not found.")
        return

    if row["status"] != "pending":
        conn.close()
        await query.answer(
            f"Already {row['status']}.",
            show_alert=True,
        )
        return

    if action == "approve":
        conn.execute("""
            UPDATE withdrawals
            SET status = 'approved', processed_at = ?
            WHERE id = ? AND status = 'pending'
        """, (datetime.utcnow().isoformat(), withdrawal_id))
        conn.commit()
        conn.close()

        await query.edit_message_text(
            f"✅ Withdrawal #{withdrawal_id} approved.\n"
            f"Amount: ₹{row['amount']}\n"
            f"UPI: {row['upi_id']}"
        )

        try:
            await context.bot.send_message(
                row["user_id"],
                f"✅ *Withdrawal Approved*\n\n"
                f"Request: #{withdrawal_id}\n"
                f"Amount: ₹{row['amount']}\n"
                f"UPI: `{row['upi_id']}`",
                parse_mode="Markdown",
                reply_markup=MAIN_KEYBOARD,
            )
        except Exception:
            pass

    elif action == "reject":
        conn.execute("""
            UPDATE withdrawals
            SET status = 'rejected', processed_at = ?
            WHERE id = ? AND status = 'pending'
        """, (datetime.utcnow().isoformat(), withdrawal_id))

        # Return the reserved balance to the user.
        conn.execute(
            "UPDATE users SET points = points + ? WHERE user_id = ?",
            (row["amount"], row["user_id"]),
        )

        conn.commit()
        conn.close()

        await query.edit_message_text(
            f"❌ Withdrawal #{withdrawal_id} rejected.\n"
            f"₹{row['amount']} returned to user balance."
        )

        try:
            await context.bot.send_message(
                row["user_id"],
                f"❌ *Withdrawal Rejected*\n\n"
                f"Request: #{withdrawal_id}\n"
                f"₹{row['amount']} points balance ma return kari didha che.",
                parse_mode="Markdown",
                reply_markup=MAIN_KEYBOARD,
            )
        except Exception:
            pass


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled exception:", exc_info=context.error)


# ============================================================
# MAIN
# ============================================================

def main():
    if BOT_TOKEN == "PASTE_BOT_TOKEN_HERE":
        raise RuntimeError(
            "BOT_TOKEN set karo. Environment variable BOT_TOKEN use karo "
            "athva code ma PASTE_BOT_TOKEN_HERE replace karo."
        )

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        CallbackQueryHandler(verify_join, pattern=r"^verify_join$")
    )
    app.add_handler(
        CallbackQueryHandler(
            withdrawal_action,
            pattern=r"^wd_(approve|reject)_\d+$",
        )
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    app.add_error_handler(error_handler)

    logger.info("Referral earning bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
