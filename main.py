
import os
import re
import sqlite3
import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    MessageHandler, ChatMemberHandler, filters,
)

ADMIN_ID = 8754266926
REWARD_PER_REFERRAL = 1
MIN_WITHDRAWAL = 20
DB_FILE = "bot.db"

def get_bot_token():
    for name in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN"):
        value = os.getenv(name)
        if value and value.strip():
            return value.strip().strip('"').strip("'")
    for key, value in os.environ.items():
        if key.strip().upper() in {"BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN"} and value:
            return value.strip().strip('"').strip("'")
    return None

BOT_TOKEN = get_bot_token()

REQUIRED_CHATS = [
    {"name": "Main Channel", "join_url": "https://t.me/+CvMzUOQFDiczNjZl", "chat_id": "-1003540916448"},
    {"name": "Main GC", "join_url": "https://t.me/+si63pZE9oqg3MTdl", "chat_id": "-1003709273961"},
    {"name": "Second Channel", "join_url": "https://t.me/+727mMnMYUdJhMDZl", "chat_id": "-1004318439063"},
    {"name": "Second GC", "join_url": "https://t.me/+GJDIGLvRE9g1ZDJl", "chat_id": "-1004405504432"},
    {"name": "Last Channel", "join_url": "https://t.me/lxmodemenu", "chat_id": "@lxmodemenu"},
    {"name": "Last GC", "join_url": "https://t.me/+oY2-2veF8zU5NzY9", "chat_id": "-1004437059817"},
]

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("GOKU_REFERRAL")

def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
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
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            points INTEGER NOT NULL DEFAULT 0,
            referral_code TEXT UNIQUE NOT NULL,
            referred_by INTEGER,
            referral_paid INTEGER NOT NULL DEFAULT 0,
            upi_id TEXT DEFAULT '',
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
    cols = {r["name"] for r in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "upi_id" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN upi_id TEXT DEFAULT ''")
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row

def create_user(tg_user, referred_by=None):
    existing = get_user(tg_user.id)
    if existing:
        conn = db()
        conn.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?",
                     (tg_user.username or "", tg_user.first_name or "", tg_user.id))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (tg_user.id,)).fetchone()
        conn.close()
        return row, False
    conn = db()
    conn.execute("""
        INSERT INTO users
        (user_id, username, first_name, points, referral_code, referred_by, referral_paid, upi_id, joined_at)
        VALUES (?, ?, ?, 0, ?, ?, 0, '', ?)
    """, (tg_user.id, tg_user.username or "", tg_user.first_name or "",
          f"REF{tg_user.id}", referred_by, now()))
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (tg_user.id,)).fetchone()
    conn.close()
    return row, True

def set_upi(user_id, upi):
    conn = db()
    conn.execute("UPDATE users SET upi_id=? WHERE user_id=?", (upi, user_id))
    conn.commit()
    conn.close()

def add_referral_reward(referrer_id, referred_id):
    if not referrer_id or referrer_id == referred_id:
        return False
    conn = db()
    cur = conn.cursor()
    row = cur.execute("SELECT referred_by, referral_paid FROM users WHERE user_id=?", (referred_id,)).fetchone()
    if not row or row["referred_by"] != referrer_id or row["referral_paid"] == 1:
        conn.close()
        return False
    cur.execute("UPDATE users SET points=points+? WHERE user_id=?", (REWARD_PER_REFERRAL, referrer_id))
    cur.execute("UPDATE users SET referral_paid=1 WHERE user_id=?", (referred_id,))
    conn.commit()
    conn.close()
    return True

def get_referral_stats(user_id):
    conn = db()
    row = conn.execute("""
        SELECT COUNT(*) AS total FROM users
        WHERE referred_by=? AND referral_paid=1
    """, (user_id,)).fetchone()
    conn.close()
    return row["total"]

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["👥 Refer Friend", "💰 Points"], ["💸 Withdrawal", "🆘 Help"]],
    resize_keyboard=True
)
BACK_KEYBOARD = ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True)

def join_keyboard():
    rows = [[InlineKeyboardButton(f"📢 Join {c['name']}", url=c["join_url"])] for c in REQUIRED_CHATS]
    rows.append([InlineKeyboardButton("✅ Verify Joined", callback_data="verify_join")])
    return InlineKeyboardMarkup(rows)

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Pending Requests", callback_data="admin_pending")],
        [InlineKeyboardButton("📋 All Requests", callback_data="admin_all")],
        [InlineKeyboardButton("✅ Completed", callback_data="admin_completed")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
    ])

RUNTIME_CHAT_IDS = {}

def member_is_joined(member):
    if member.status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    }:
        return True
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False

async def resolve_chat_id(bot, chat):
    key = chat["name"]
    if key in RUNTIME_CHAT_IDS:
        return RUNTIME_CHAT_IDS[key]

    raw = chat["chat_id"]

    try:
        info = await bot.get_chat(raw)
        RUNTIME_CHAT_IDS[key] = info.id
        return info.id
    except Exception as exc:
        # If Telegram says the old basic group was migrated,
        # use the new ID automatically for this running process.
        text = str(exc)
        match = re.search(r"migrat(?:ed|e).*?(-100\d{5,})", text, re.I)
        if match:
            new_id = int(match.group(1))
            RUNTIME_CHAT_IDS[key] = new_id
            logger.warning(
                "%s migrated. Using new runtime ID %s. Config value kept unchanged.",
                key, new_id
            )
            return new_id

        # For a public @username, keep the username as-is.
        RUNTIME_CHAT_IDS[key] = raw
        return raw

async def check_one_chat(bot, user_id, chat):
    try:
        actual_chat_id = await resolve_chat_id(bot, chat)
        member = await bot.get_chat_member(actual_chat_id, user_id)
        return member_is_joined(member), None
    except TelegramError as exc:
        logger.warning("Membership check failed | %s | %s", chat["name"], exc)
        return False, str(exc)
    except Exception as exc:
        logger.exception("Unexpected membership error | %s", chat["name"])
        return False, str(exc)

async def get_not_joined_chats(bot, user_id):
    missing = []
    errors = {}
    for chat in REQUIRED_CHATS:
        ok, error = await check_one_chat(bot, user_id, chat)
        if not ok:
            missing.append(chat["name"])
            if error:
                errors[chat["name"]] = error
    return missing, errors

async def send_join_screen(update, missing=None):
    if missing:
        missing_text = "\n".join(f"❌ {name}" for name in missing)
        text = (
            "🔒 <b>JOIN REQUIRED</b>\n\n"
            "You still need to join:\n\n"
            f"{missing_text}\n\n"
            "After joining all of them, tap <b>Verify Joined</b>."
        )
    else:
        text = (
            "🔒 <b>JOIN REQUIRED</b>\n\n"
            "Please join all required channels/groups.\n\n"
            "After joining all of them, tap <b>Verify Joined</b>."
        )
    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="HTML", reply_markup=join_keyboard())
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=join_keyboard())

async def gate_user(bot, user_id):
    missing, errors = await get_not_joined_chats(bot, user_id)
    return missing, errors

def extract_referrer(args):
    if not args:
        return None
    value = str(args[0]).strip().upper()
    if not value.startswith("REF"):
        return None
    try:
        return int(value[3:])
    except ValueError:
        return None

async def maybe_reward_and_open_menu(bot, user_id, notify=True):
    user = get_user(user_id)
    if not user:
        return False
    rewarded = False
    if user["referred_by"]:
        rewarded = add_referral_reward(user["referred_by"], user_id)
        if rewarded:
            try:
                await bot.send_message(
                    user["referred_by"],
                    f"🎉 <b>New Referral!</b>\n\nYou earned <b>₹{REWARD_PER_REFERRAL}</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
    if notify:
        try:
            await bot.send_message(
                user_id,
                "🎉 <b>All required joins are complete!</b>\n\n"
                "Your account is now active.\n"
                "You can use the menu below.",
                parse_mode="HTML",
                reply_markup=MAIN_KEYBOARD,
            )
        except Exception:
            pass
    return rewarded

async def start(update, context):
    user = update.effective_user
    if not user or not update.message:
        return
    referrer_id = extract_referrer(context.args)
    existing = get_user(user.id)
    if not existing:
        if referrer_id == user.id:
            referrer_id = None
        elif referrer_id and not get_user(referrer_id):
            referrer_id = None
        existing, _ = create_user(user, referrer_id)
    else:
        create_user(user)
    context.user_data.clear()
    missing, _ = await gate_user(context.bot, user.id)
    if missing:
        await send_join_screen(update, missing)
        return
    await maybe_reward_and_open_menu(context.bot, user.id, notify=False)

async def verify_join(update, context):
    query = update.callback_query
    await query.answer("Checking all 6...")
    missing, errors = await gate_user(context.bot, query.from_user.id)
    if missing:
        missing_text = "\n".join(f"❌ {name}" for name in missing)
        error_lines = []
        for name, err in errors.items():
            if "migrat" in err.lower():
                error_lines.append(f"⚠️ {name}: this group ID may have changed after migration.")
        extra = ("\n\n" + "\n".join(error_lines)) if error_lines else ""
        text = (
            "❌ <b>Verification Failed</b>\n\n"
            "You still need to join:\n\n"
            f"{missing_text}{extra}\n\n"
            "Join them and tap <b>Verify Joined</b> again."
        )
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=join_keyboard())
        return
    await query.message.edit_text(
        "🎉 <b>Verification Successful!</b>\n\n"
        "✅ Main Channel\n✅ Main GC\n✅ Second Channel\n"
        "✅ Second GC\n✅ Last Channel\n✅ Last GC",
        parse_mode="HTML",
    )
    await maybe_reward_and_open_menu(context.bot, query.from_user.id, notify=True)

async def verify_command(update, context):
    if not update.message:
        return
    missing, _ = await gate_user(context.bot, update.effective_user.id)
    if missing:
        await send_join_screen(update, missing)
        return
    await maybe_reward_and_open_menu(context.bot, update.effective_user.id, notify=True)

async def handle_chat_member(update, context):
    cm = update.chat_member
    if not cm:
        return
    user = cm.new_chat_member.user
    new_joined = member_is_joined(cm.new_chat_member)
    old_joined = member_is_joined(cm.old_chat_member)
    if not new_joined or old_joined:
        return
    # Match the actual Telegram chat ID. This also handles
    # @lxmodemenu and groups that have migrated to a new ID.
    matched = False
    for required in REQUIRED_CHATS:
        try:
            actual_id = await resolve_chat_id(context.bot, required)
            if int(actual_id) == int(cm.chat.id):
                matched = True
                break
        except Exception:
            if str(required["chat_id"]) == str(cm.chat.id):
                matched = True
                break

    if not matched:
        return

    if not get_user(user.id):
        return
    missing, _ = await gate_user(context.bot, user.id)
    if not missing:
        await maybe_reward_and_open_menu(context.bot, user.id, notify=True)

async def refer_friend(update, context):
    user = get_user(update.effective_user.id)
    if not user:
        return
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={user['referral_code']}"
    count = get_referral_stats(user["user_id"])
    await update.message.reply_text(
        "👥 <b>Refer Friend</b>\n\n"
        "Share your personal referral link:\n\n"
        f"<code>{link}</code>\n\n"
        f"💰 Reward per successful referral: <b>₹{REWARD_PER_REFERRAL}</b>\n"
        f"👥 Successful referrals: <b>{count}</b>\n\n"
        "A referral is counted only once after the referred user completes all required joins.",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )

async def points(update, context):
    user = get_user(update.effective_user.id)
    if not user:
        return
    count = get_referral_stats(user["user_id"])
    await update.message.reply_text(
        "💰 <b>Your Balance</b>\n\n"
        f"Balance: <b>₹{user['points']}</b>\n"
        f"Successful referrals: <b>{count}</b>\n"
        f"Reward per referral: <b>₹{REWARD_PER_REFERRAL}</b>",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )

def valid_upi(upi):
    upi = upi.strip()
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{2,}@[A-Za-z0-9._-]{2,}", upi))

async def withdrawal(update, context):
    user = get_user(update.effective_user.id)
    if not user:
        return
    if user["points"] < MIN_WITHDRAWAL:
        await update.message.reply_text(
            f"❌ Minimum withdrawal is <b>₹{MIN_WITHDRAWAL}</b>.\n"
            f"Your balance: <b>₹{user['points']}</b>",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if user["upi_id"]:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Use {user['upi_id']}", callback_data="wd_use_saved")],
            [InlineKeyboardButton("✏️ Change UPI", callback_data="wd_change_upi")],
            [InlineKeyboardButton("❌ Cancel", callback_data="wd_cancel")],
        ])
        await update.message.reply_text(
            f"💸 <b>Withdrawal</b>\n\n"
            f"Balance: <b>₹{user['points']}</b>\n"
            f"Saved UPI: <code>{user['upi_id']}</code>\n\n"
            "Choose an option:",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        context.user_data["withdrawal_step"] = "upi"
        await update.message.reply_text(
            "💳 <b>Bind UPI</b>\n\n"
            "Send your UPI ID.\n"
            "Example: <code>name@upi</code>",
            parse_mode="HTML",
            reply_markup=BACK_KEYBOARD,
        )

async def create_withdrawal(update, context, upi):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user or user["points"] < MIN_WITHDRAWAL:
        context.user_data.clear()
        await update.message.reply_text("❌ Insufficient balance.", reply_markup=MAIN_KEYBOARD)
        return
    amount = user["points"]
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET points=points-? WHERE user_id=? AND points>=?",
        (amount, user_id, amount),
    )
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text("❌ Withdrawal could not be created.", reply_markup=MAIN_KEYBOARD)
        return
    cur.execute(
        "INSERT INTO withdrawals (user_id, amount, upi_id, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        (user_id, amount, upi, now()),
    )
    wid = cur.lastrowid
    conn.commit()
    conn.close()
    context.user_data.clear()
    await update.message.reply_text(
        f"✅ <b>Withdrawal Request Submitted</b>\n\n"
        f"Request: <b>#{wid}</b>\nAmount: <b>₹{amount}</b>\nUPI: <code>{upi}</code>\n\n"
        "Your request is waiting for admin review.",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )
    user = get_user(user_id)
    username = f"@{user['username']}" if user["username"] else "No username"
    admin_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Complete", callback_data=f"wd_complete_{wid}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"wd_reject_{wid}")]
    ])
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"💸 <b>New Withdrawal Request</b>\n\n"
            f"Request: <b>#{wid}</b>\n"
            f"User ID: <code>{user_id}</code>\n"
            f"Username: {username}\n"
            f"Amount: <b>₹{amount}</b>\n"
            f"UPI: <code>{upi}</code>\n"
            f"Time: {now()}",
            parse_mode="HTML",
            reply_markup=admin_kb,
        )
    except Exception as exc:
        logger.warning("Admin notification failed: %s", exc)

async def withdrawal_callback(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "wd_cancel":
        await q.edit_message_text("❌ Withdrawal cancelled.")
        return
    if data == "wd_change_upi":
        context.user_data["withdrawal_step"] = "upi"
        await q.message.reply_text(
            "✏️ <b>Change UPI</b>\n\nSend your new UPI ID.",
            parse_mode="HTML",
            reply_markup=BACK_KEYBOARD,
        )
        return
    if data == "wd_use_saved":
        user = get_user(q.from_user.id)
        if not user or not user["upi_id"]:
            await q.message.reply_text("❌ No saved UPI found.")
            return
        await q.edit_message_text("⏳ Creating withdrawal request...")
        fake_update = None
        # Direct version of create_withdrawal for callback
        user_id = q.from_user.id
        user = get_user(user_id)
        if user["points"] < MIN_WITHDRAWAL:
            await q.message.reply_text("❌ Insufficient balance.", reply_markup=MAIN_KEYBOARD)
            return
        amount = user["points"]
        conn = db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET points=points-? WHERE user_id=? AND points>=?", (amount, user_id, amount))
        if cur.rowcount != 1:
            conn.rollback(); conn.close()
            await q.message.reply_text("❌ Withdrawal could not be created.", reply_markup=MAIN_KEYBOARD)
            return
        cur.execute("INSERT INTO withdrawals (user_id, amount, upi_id, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
                    (user_id, amount, user["upi_id"], now()))
        wid = cur.lastrowid
        conn.commit(); conn.close()
        await q.message.reply_text(
            f"✅ <b>Withdrawal Request Submitted</b>\n\nRequest: <b>#{wid}</b>\nAmount: <b>₹{amount}</b>\nUPI: <code>{user['upi_id']}</code>",
            parse_mode="HTML", reply_markup=MAIN_KEYBOARD
        )
        admin_kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Complete", callback_data=f"wd_complete_{wid}"),
                                          InlineKeyboardButton("❌ Reject", callback_data=f"wd_reject_{wid}")]])
        username = f"@{user['username']}" if user["username"] else "No username"
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"💸 <b>New Withdrawal Request</b>\n\nRequest: <b>#{wid}</b>\nUser ID: <code>{user_id}</code>\nUsername: {username}\nAmount: <b>₹{amount}</b>\nUPI: <code>{user['upi_id']}</code>",
                parse_mode="HTML", reply_markup=admin_kb
            )
        except Exception:
            pass

async def help_menu(update, context):
    await update.message.reply_text(
        "🆘 <b>Help</b>\n\n"
        "👥 Refer Friend — get your referral link.\n"
        "💰 Points — check your balance.\n"
        "💸 Withdrawal — withdraw your available balance.\n"
        "UPI is saved after you bind it and can be changed anytime before a new withdrawal.",
        parse_mode="HTML", reply_markup=MAIN_KEYBOARD
    )

async def handle_text(update, context):
    if not update.message:
        return
    user = update.effective_user
    if not user:
        return

    # Always re-check membership on any user message.
    # This fixes the "send random message first, then buttons" issue.
    missing, _ = await gate_user(context.bot, user.id)
    if missing:
        await send_join_screen(update, missing)
        return

    text = (update.message.text or "").strip()

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
        await update.message.reply_text("🏠 <b>Main Menu</b>", parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
        return

    if context.user_data.get("withdrawal_step") == "upi":
        upi = text
        if not valid_upi(upi):
            await update.message.reply_text(
                "❌ Invalid UPI ID.\n\nExample: <code>name@upi</code>",
                parse_mode="HTML", reply_markup=BACK_KEYBOARD
            )
            return
        set_upi(user.id, upi)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ UPI bound successfully.\n\nUPI: <code>{upi}</code>\n\n"
            "Now tap Withdrawal again to submit your request.",
            parse_mode="HTML", reply_markup=MAIN_KEYBOARD
        )
        return

    await update.message.reply_text(
        "🏠 <b>Main Menu</b>\n\nChoose an option below:",
        parse_mode="HTML", reply_markup=MAIN_KEYBOARD
    )

def withdrawal_rows(status=None, limit=20):
    conn = db()
    if status:
        rows = conn.execute(
            "SELECT * FROM withdrawals WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM withdrawals ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return rows

def format_withdrawal(row):
    conn = db()
    user = conn.execute("SELECT username, first_name FROM users WHERE user_id=?", (row["user_id"],)).fetchone()
    conn.close()
    username = f"@{user['username']}" if user and user["username"] else "No username"
    return (
        f"#{row['id']} | {row['status'].upper()}\n"
        f"User: {user['first_name'] if user else 'Unknown'}\n"
        f"Username: {username}\n"
        f"User ID: {row['user_id']}\n"
        f"Amount: ₹{row['amount']}\n"
        f"UPI: {row['upi_id']}\n"
        f"Created: {row['created_at']}\n"
        f"Processed: {row['processed_at'] or '-'}"
    )

async def admin_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Admin only.")
        return
    await update.message.reply_text(
        "🛠 <b>Admin Panel</b>\n\nChoose an option:",
        parse_mode="HTML", reply_markup=admin_keyboard()
    )

async def admin_callback(update, context):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("Admin only.", show_alert=True)
        return
    await q.answer()
    data = q.data
    if data == "admin_pending":
        rows = withdrawal_rows("pending")
        if not rows:
            text = "📥 <b>Pending Withdrawals</b>\n\nNo pending requests."
        else:
            text = "📥 <b>Pending Withdrawals</b>\n\n" + "\n\n".join(format_withdrawal(r) for r in rows)
    elif data == "admin_completed":
        rows = withdrawal_rows("completed")
        if not rows:
            text = "✅ <b>Completed Withdrawals</b>\n\nNo completed requests."
        else:
            text = "✅ <b>Completed Withdrawals</b>\n\n" + "\n\n".join(format_withdrawal(r) for r in rows)
    elif data == "admin_all":
        rows = withdrawal_rows()
        if not rows:
            text = "📋 <b>All Withdrawals</b>\n\nNo requests."
        else:
            text = "📋 <b>All Withdrawals</b>\n\n" + "\n\n".join(format_withdrawal(r) for r in rows)
    elif data == "admin_stats":
        conn = db()
        users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        pending = conn.execute("SELECT COUNT(*) AS n FROM withdrawals WHERE status='pending'").fetchone()["n"]
        completed = conn.execute("SELECT COUNT(*) AS n FROM withdrawals WHERE status='completed'").fetchone()["n"]
        total_completed = conn.execute("SELECT COALESCE(SUM(amount),0) AS n FROM withdrawals WHERE status='completed'").fetchone()["n"]
        conn.close()
        text = (
            "📊 <b>Bot Stats</b>\n\n"
            f"Users: <b>{users}</b>\n"
            f"Pending withdrawals: <b>{pending}</b>\n"
            f"Completed withdrawals: <b>{completed}</b>\n"
            f"Completed amount: <b>₹{total_completed}</b>"
        )
    else:
        return
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=admin_keyboard())

async def admin_withdrawal_action(update, context):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("Admin only.", show_alert=True)
        return
    await q.answer()
    parts = q.data.split("_")
    if len(parts) != 3:
        return
    action, wid_s = parts[1], parts[2]
    try:
        wid = int(wid_s)
    except ValueError:
        return

    conn = db()
    row = conn.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
    if not row:
        conn.close()
        await q.edit_message_text("❌ Withdrawal not found.")
        return
    if row["status"] != "pending":
        conn.close()
        await q.answer(f"Already {row['status']}.", show_alert=True)
        return

    if action == "complete":
        conn.execute(
            "UPDATE withdrawals SET status='completed', processed_at=? WHERE id=? AND status='pending'",
            (now(), wid)
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        conn.close()
        await q.edit_message_text(
            f"✅ <b>Withdrawal Completed</b>\n\n{format_withdrawal(updated)}",
            parse_mode="HTML"
        )
        try:
            await context.bot.send_message(
                row["user_id"],
                f"✅ <b>Withdrawal Completed</b>\n\nRequest: #{wid}\nAmount: ₹{row['amount']}\nUPI: <code>{row['upi_id']}</code>",
                parse_mode="HTML", reply_markup=MAIN_KEYBOARD
            )
        except Exception:
            pass

    elif action == "reject":
        conn.execute(
            "UPDATE withdrawals SET status='rejected', processed_at=? WHERE id=? AND status='pending'",
            (now(), wid)
        )
        conn.execute(
            "UPDATE users SET points=points+? WHERE user_id=?",
            (row["amount"], row["user_id"])
        )
        conn.commit()
        conn.close()
        await q.edit_message_text(
            f"❌ <b>Withdrawal Rejected</b>\n\n₹{row['amount']} returned to the user's balance.",
            parse_mode="HTML"
        )
        try:
            await context.bot.send_message(
                row["user_id"],
                f"❌ <b>Withdrawal Rejected</b>\n\nRequest: #{wid}\n₹{row['amount']} has been returned to your balance.",
                parse_mode="HTML", reply_markup=MAIN_KEYBOARD
            )
        except Exception:
            pass

async def error_handler(update, context):
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("verify", verify_command))
    app.add_handler(CommandHandler("admin", admin_command))

    app.add_handler(
        CallbackQueryHandler(
            verify_join,
            pattern=r"^verify_join$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            withdrawal_callback,
            pattern=r"^wd_(use_saved|change_upi|cancel)$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_(pending|all|completed|stats)$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            admin_withdrawal_action,
            pattern=r"^wd_(complete|reject)_\d+$"
        )
    )

    # Immediate referral verification when Telegram sends membership update.
    app.add_handler(
        ChatMemberHandler(
            handle_chat_member,
            ChatMemberHandler.CHAT_MEMBER
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text
        )
    )

    app.add_error_handler(error_handler)

    logger.info("GOKU REFERRAL BOT ONLINE")
    logger.info("Required chats: %s", len(REQUIRED_CHATS))

    # ALL_TYPES includes chat_member, required for instant referral credit.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
