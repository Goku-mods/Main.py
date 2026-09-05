import os
import sqlite3
import logging
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
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

# Hosting/Environment Variables ma BOT_TOKEN set karo
BOT_TOKEN = os.getenv("8849206316:AAEAl8qRiQkzmOSkVPwMBfzBxrNl9jliBXQ")

ADMIN_ID = 8754266926

REWARD_PER_REFERRAL = 1
MIN_WITHDRAWAL = 20

# ============================================================
# REQUIRED CHANNELS / GROUPS
# ============================================================

REQUIRED_CHATS = [
    {
        "name": "Main Channel",
        "join_url": "https://t.me/+CvMzUOQFDiczNjZl",
        "chat_id": "-1003540916448",
    },
    {
        "name": "Main GC",
        "join_url": "https://t.me/GOKUPROFITZONE",
        "chat_id": "-1003709273961",
    },
    {
        "name": "Second Channel",
        "join_url": "https://t.me/+727mMnMYUdJhMDZl",
        "chat_id": "-1004318439063",
    },
    {
        "name": "Second GC",
        "join_url": "https://t.me/+Ld0qauMMtjowNzg1",
        "chat_id": "-1003720001493",
    },
    {
        "name": "Last Channel",
        "join_url": "https://t.me/lxmodemenu",
        "chat_id": "@lxmodemenu",
    },
    {
        "name": "Last GC",
        "join_url": "https://t.me/+oY2-2veF8zU5NzY9",
        "chat_id": "-1004437059817",
    },
]

DB_FILE = "bot.db"

# ============================================================
# LOGGING
# ============================================================

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


def now():
    return datetime.now(timezone.utc).isoformat()


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

    conn.commit()
    conn.close()


def get_user(user_id):
    conn = db()

    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    conn.close()
    return row


def make_referral_code(user_id):
    return f"REF{user_id}"


def create_user(tg_user, referred_by=None):
    existing = get_user(tg_user.id)

    if existing:
        return existing, False

    referral_code = make_referral_code(tg_user.id)

    conn = db()

    conn.execute("""
        INSERT INTO users (
            user_id,
            username,
            first_name,
            points,
            referral_code,
            referred_by,
            referral_paid,
            joined_at
        )
        VALUES (?, ?, ?, 0, ?, ?, 0, ?)
    """, (
        tg_user.id,
        tg_user.username or "",
        tg_user.first_name or "",
        referral_code,
        referred_by,
        now(),
    ))

    conn.commit()

    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (tg_user.id,),
    ).fetchone()

    conn.close()

    return row, True


def add_referral_reward(referrer_id, referred_id):
    if referrer_id == referred_id:
        return False

    conn = db()
    cur = conn.cursor()

    referred = cur.execute(
        """
        SELECT referred_by, referral_paid
        FROM users
        WHERE user_id = ?
        """,
        (referred_id,),
    ).fetchone()

    if not referred:
        conn.close()
        return False

    if referred["referred_by"] != referrer_id:
        conn.close()
        return False

    if referred["referral_paid"]:
        conn.close()
        return False

    cur.execute(
        """
        UPDATE users
        SET points = points + ?
        WHERE user_id = ?
        """,
        (REWARD_PER_REFERRAL, referrer_id),
    )

    cur.execute(
        """
        UPDATE users
        SET referral_paid = 1
        WHERE user_id = ?
        """,
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
    [
        ["⬅️ Back"]
    ],
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
        InlineKeyboardButton(
            "✅ Verify Joined",
            callback_data="verify_join",
        )
    ])

    return InlineKeyboardMarkup(rows)


# ============================================================
# MEMBERSHIP CHECK
# ============================================================

async def is_member(bot, user_id, chat_id):
    try:
        member = await bot.get_chat_member(
            chat_id=chat_id,
            user_id=user_id,
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception as exc:
        logger.warning(
            "Membership check failed | chat=%s | user=%s | error=%s",
            chat_id,
            user_id,
            exc,
        )

        return False


async def all_required_joined(bot, user_id):
    for chat in REQUIRED_CHATS:
        if not await is_member(
            bot,
            user_id,
            chat["chat_id"],
        ):
            return False

    return True


# ============================================================
# REFERRAL
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


# ============================================================
# JOIN SCREEN
# ============================================================

async def send_join_screen(update, context):
    text = (
        "🔒 *MUST JOIN ALL*\n\n"
        "Bot use karva pela badha required "
        "channels/groups join karo.\n\n"
        "Badha join karya pachhi "
        "*Verify Joined* dabavo."
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


# ============================================================
# START
# ============================================================

async def start(update, context):
    user = update.effective_user

    if not user:
        return

    referrer_id = extract_referrer(
        context.args
    )

    existing = get_user(user.id)

    if not existing:

        if referrer_id == user.id:
            referrer_id = None

        elif referrer_id:
            if not get_user(referrer_id):
                referrer_id = None

        existing, _ = create_user(
            user,
            referrer_id,
        )

    context.user_data.clear()

    joined = await all_required_joined(
        context.bot,
        user.id,
    )

    if not joined:
        await send_join_screen(
            update,
            context,
        )
        return

    # Referral reward
    if existing["referred_by"]:

        rewarded = add_referral_reward(
            existing["referred_by"],
            user.id,
        )

        if rewarded:
            try:
                await context.bot.send_message(
                    existing["referred_by"],
                    "🎉 *New Referral!*\n\n"
                    f"💰 You earned +{REWARD_PER_REFERRAL} "
                    f"point (₹{REWARD_PER_REFERRAL}).",
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


# ============================================================
# VERIFY JOIN
# ============================================================

async def verify_join(update, context):
    query = update.callback_query

    await query.answer()

    joined = await all_required_joined(
        context.bot,
        query.from_user.id,
    )

    if not joined:

        await query.message.reply_text(
            "❌ Badha required "
            "channels/groups join nathi karya.\n\n"
            "Please badha 6 join kari ne "
            "fari Verify Joined dabavo.",
            reply_markup=join_keyboard(),
        )

        return

    user = get_user(
        query.from_user.id
    )

    if user and user["referred_by"]:

        rewarded = add_referral_reward(
            user["referred_by"],
            query.from_user.id,
        )

        if rewarded:

            try:
                await context.bot.send_message(
                    user["referred_by"],
                    "🎉 *New Referral!*\n\n"
                    f"💰 You earned +{REWARD_PER_REFERRAL} "
                    f"point (₹{REWARD_PER_REFERRAL}).",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    await query.message.reply_text(
        "✅ *Verification successful!*\n\n"
        "🏠 Main menu open thai gayu.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# REFER FRIEND
# ============================================================

async def refer_friend(update, context):
    user = get_user(
        update.effective_user.id
    )

    if not user:
        return

    me = await context.bot.get_me()

    link = (
        f"https://t.me/{me.username}"
        f"?start={user['referral_code']}"
    )

    text = (
        "👥 *Refer Friend*\n\n"
        "Aa tamaro personal referral link che:\n\n"
        f"`{link}`\n\n"
        f"👤 Successful referral = "
        f"+{REWARD_PER_REFERRAL} point "
        f"(₹{REWARD_PER_REFERRAL})\n\n"
        "⚠️ Reward tyare j credit thashe "
        "jyare referred user badha required "
        "joins complete kare."
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# POINTS
# ============================================================

async def points(update, context):
    user = get_user(
        update.effective_user.id
    )

    if not user:
        return

    conn = db()

    referral_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM users
        WHERE referred_by = ?
        AND referral_paid = 1
        """,
        (user["user_id"],),
    ).fetchone()["c"]

    conn.close()

    await update.message.reply_text(
        "💰 *Your Points*\n\n"
        f"💵 Balance: *₹{user['points']}*\n"
        f"⭐ Points: *{user['points']}*\n"
        f"👥 Successful Referrals: "
        f"*{referral_count}*\n\n"
        f"1 point = ₹{REWARD_PER_REFERRAL}",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# WITHDRAWAL START
# ============================================================

async def withdrawal(update, context):
    user = get_user(
        update.effective_user.id
    )

    if not user:
        return

    if user["points"] < MIN_WITHDRAWAL:

        await update.message.reply_text(
            f"❌ Withdrawal mate minimum "
            f"*₹{MIN_WITHDRAWAL}* joiye.\n\n"
            f"Tamaro current balance: "
            f"*₹{user['points']}*",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )

        return

    context.user_data[
        "withdrawal_step"
    ] = "upi"

    await update.message.reply_text(
        "💸 *Withdrawal*\n\n"
        f"Minimum withdrawal: "
        f"₹{MIN_WITHDRAWAL}\n"
        f"Your balance: "
        f"₹{user['points']}\n\n"
        "Tamari UPI ID moklo.\n\n"
        "Example: `name@upi`",
        parse_mode="Markdown",
        reply_markup=BACK_KEYBOARD,
    )


# ============================================================
# HELP
# ============================================================

async def help_menu(update, context):
    await update.message.reply_text(
        "🆘 *Help*\n\n"
        "👥 Refer Friend → Personal referral link.\n"
        "💰 Points → Balance ane referrals.\n"
        f"💸 Withdrawal → Minimum ₹{MIN_WITHDRAWAL}.\n\n"
        "Referral reward successful "
        "join verification pachhi credit thay che.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def handle_text(update, context):
    if not update.message:
        return

    text = (
        update.message.text or ""
    ).strip()

    user_id = update.effective_user.id

    if text == "👥 Refer Friend":
        await refer_friend(
            update,
            context,
        )
        return

    if text == "💰 Points":
        await points(
            update,
            context,
        )
        return

    if text == "💸 Withdrawal":
        await withdrawal(
            update,
            context,
        )
        return

    if text == "🆘 Help":
        await help_menu(
            update,
            context,
        )
        return

    if text == "⬅️ Back":

        context.user_data.clear()

        await update.message.reply_text(
            "🏠 Main menu",
            reply_markup=MAIN_KEYBOARD,
        )

        return

    # ========================================================
    # UPI
    # ========================================================

    if context.user_data.get(
        "withdrawal_step"
    ) == "upi":

        upi = text

        if (
            len(upi) < 3
            or "@" not in upi
            or " " in upi
        ):

            await update.message.reply_text(
                "❌ Invalid UPI ID.\n\n"
                "Example: `name@upi`",
                parse_mode="Markdown",
                reply_markup=BACK_KEYBOARD,
            )

            return

        user = get_user(user_id)

        if (
            not user
            or user["points"] < MIN_WITHDRAWAL
        ):

            context.user_data.clear()

            await update.message.reply_text(
                "❌ Insufficient balance.",
                reply_markup=MAIN_KEYBOARD,
            )

            return

        amount = user["points"]

        conn = db()
        cur = conn.cursor()

        cur.execute(
            """
            UPDATE users
            SET points = points - ?
            WHERE user_id = ?
            AND points >= ?
            """,
            (
                amount,
                user_id,
                amount,
            ),
        )

        if cur.rowcount != 1:

            conn.rollback()
            conn.close()

            await update.message.reply_text(
                "❌ Withdrawal create "
                "thai shakyo nahi.",
                reply_markup=MAIN_KEYBOARD,
            )

            context.user_data.clear()

            return

        cur.execute(
            """
            INSERT INTO withdrawals (
                user_id,
                amount,
                upi_id,
                status,
                created_at
            )
            VALUES (
                ?, ?, ?, 'pending', ?
            )
            """,
            (
                user_id,
                amount,
                upi,
                now(),
            ),
        )

        withdrawal_id = cur.lastrowid

        conn.commit()
        conn.close()

        context.user_data.clear()

        await update.message.reply_text(
            "✅ *Withdrawal Request Submitted*\n\n"
            f"🆔 Request: #{withdrawal_id}\n"
            f"💰 Amount: ₹{amount}\n"
            f"💳 UPI: `{upi}`\n\n"
            "Admin approval pachhi "
            "payment process thashe.",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )

        # ====================================================
        # ADMIN BUTTONS
        # ====================================================

        admin_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=(
                        f"wd_approve_{withdrawal_id}"
                    ),
                ),
                InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=(
                        f"wd_reject_{withdrawal_id}"
                    ),
                ),
            ]
        ])

        username = (
            f"@{user['username']}"
            if user["username"]
            else "N/A"
        )

        try:

            await context.bot.send_message(
                ADMIN_ID,
                "💸 *New Withdrawal*\n\n"
                f"🆔 Request: #{withdrawal_id}\n"
                f"👤 User ID: `{user_id}`\n"
                f"👤 Username: {username}\n"
                f"💰 Amount: ₹{amount}\n"
                f"💳 UPI: `{upi}`",
                parse_mode="Markdown",
                reply_markup=admin_keyboard,
            )

        except Exception as exc:

            logger.error(
                "Could not notify admin: %s",
                exc,
            )

        return

    await update.message.reply_text(
        "Please menu mathi option select karo.",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# ADMIN WITHDRAWAL ACTION
# ============================================================

async def withdrawal_action(update, context):
    query = update.callback_query

    if query.from_user.id != ADMIN_ID:

        await query.answer(
            "Not authorized.",
            show_alert=True,
        )

        return

    await query.answer()

    parts = query.data.split("_")

    if len(parts) != 3:
        return

    action = parts[1]

    try:
        withdrawal_id = int(parts[2])
    except ValueError:
        return

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM withdrawals
        WHERE id = ?
        """,
        (withdrawal_id,),
    ).fetchone()

    if not row:

        conn.close()

        await query.edit_message_text(
            "❌ Withdrawal not found."
        )

        return

    if row["status"] != "pending":

        conn.close()

        await query.answer(
            f"Already {row['status']}.",
            show_alert=True,
        )

        return

    # ========================================================
    # APPROVE
    # ========================================================

    if action == "approve":

        conn.execute(
            """
            UPDATE withdrawals
            SET
                status = 'approved',
                processed_at = ?
            WHERE id = ?
            AND status = 'pending'
            """,
            (
                now(),
                withdrawal_id,
            ),
        )

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
                "✅ *Withdrawal Approved*\n\n"
                f"Request: #{withdrawal_id}\n"
                f"Amount: ₹{row['amount']}\n"
                f"UPI: `{row['upi_id']}`",
                parse_mode="Markdown",
                reply_markup=MAIN_KEYBOARD,
            )

        except Exception:
            pass

        return

    # ========================================================
    # REJECT
    # ========================================================

    if action == "reject":

        conn.execute(
            """
            UPDATE withdrawals
            SET
                status = 'rejected',
                processed_at = ?
            WHERE id = ?
            AND status = 'pending'
            """,
            (
                now(),
                withdrawal_id,
            ),
        )

        conn.execute(
            """
            UPDATE users
            SET points = points + ?
            WHERE user_id = ?
            """,
            (
                row["amount"],
                row["user_id"],
            ),
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
                "❌ *Withdrawal Rejected*\n\n"
                f"Request: #{withdrawal_id}\n"
                f"₹{row['amount']} points "
                "balance ma return kari didha che.",
                parse_mode="Markdown",
                reply_markup=MAIN_KEYBOARD,
            )

        except Exception:
            pass


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):
    logger.error(
        "Unhandled exception",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable missing. "
            "Hosting ma BOT_TOKEN set karo."
        )

    init_db()

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            verify_join,
            pattern=r"^verify_join$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            withdrawal_action,
            pattern=r"^wd_(approve|reject)_\d+$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    app.add_error_handler(
        error_handler
    )

    logger.info(
        "Referral earning bot started."
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
