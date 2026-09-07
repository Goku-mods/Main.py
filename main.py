import os
import re
import json
import sqlite3
import logging
from datetime import datetime, timezone

try:
    import firebase_admin
    from firebase_admin import credentials, db as firebase_db
except ImportError:
    firebase_admin = None
    credentials = None
    firebase_db = None

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    ChatMemberHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

ADMIN_ID = 8754266926

REWARD_PER_REFERRAL = 2
MIN_WITHDRAWAL = 10

DB_FILE = "bot.db"

# Bot-wide maintenance mode.
# 0 = live, 1 = maintenance.
MAINTENANCE_KEY = "maintenance"



# ============================================================
# BOT TOKEN
# ============================================================

def get_bot_token():
    names = ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN")

    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip().strip('"').strip("'")

    for key, value in os.environ.items():
        if key.strip().upper() in set(names) and value:
            value = value.strip().strip('"').strip("'")
            if value:
                return value

    return None


BOT_TOKEN = get_bot_token()


# ============================================================
# FIREBASE BACKUP / SYNC
# ============================================================

FIREBASE_DATABASE_URL = "https://saiyan-v2-838a5-default-rtdb.firebaseio.com"
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
FIREBASE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
FIREBASE_ENABLED = False


def init_firebase():
    """Initialize Firebase Admin SDK using a service-account JSON/file.

    The Firebase Web API key is intentionally NOT used for server writes.
    Keep the service-account credentials in your hosting environment.
    """
    global FIREBASE_ENABLED

    if firebase_admin is None:
        logger.warning("firebase-admin is not installed; Firebase backup disabled.")
        return False

    if firebase_admin._apps:
        FIREBASE_ENABLED = True
        return True

    try:
        if FIREBASE_SERVICE_ACCOUNT_JSON:
            info = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
            cred = credentials.Certificate(info)
        elif FIREBASE_SERVICE_ACCOUNT_FILE:
            cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_FILE)
        else:
            logger.error(
                "Firebase credentials missing. Set FIREBASE_SERVICE_ACCOUNT_JSON "
                "or GOOGLE_APPLICATION_CREDENTIALS. Firebase backup is disabled."
            )
            return False

        firebase_admin.initialize_app(
            cred,
            {"databaseURL": FIREBASE_DATABASE_URL},
        )
        FIREBASE_ENABLED = True
        logger.info("Firebase backup connected: %s", FIREBASE_DATABASE_URL)
        return True
    except Exception as exc:
        FIREBASE_ENABLED = False
        logger.exception("Firebase initialization failed: %s", exc)
        return False


def firebase_ref(path):
    if not FIREBASE_ENABLED:
        return None
    return firebase_db.reference(path)


def firebase_safe_set(path, data):
    """Write to Firebase without ever crashing the Telegram bot."""
    if not FIREBASE_ENABLED:
        return False
    try:
        firebase_ref(path).set(data)
        return True
    except Exception as exc:
        logger.error("Firebase write failed | %s | %s", path, exc)
        return False


def firebase_get(path, default=None):
    if not FIREBASE_ENABLED:
        return default
    try:
        value = firebase_ref(path).get()
        return default if value is None else value
    except Exception as exc:
        logger.error("Firebase read failed | %s | %s", path, exc)
        return default


def firebase_user_dict(row):
    return {
        "user_id": int(row["user_id"]),
        "username": row["username"] or "",
        "first_name": row["first_name"] or "",
        "points": int(row["points"] or 0),
        "referral_code": row["referral_code"],
        "referred_by": int(row["referred_by"]) if row["referred_by"] is not None else None,
        "referral_paid": int(row["referral_paid"] or 0),
        "upi_id": row["upi_id"] or "",
        "joined_at": row["joined_at"],
        "updated_at": row["updated_at"],
    }


def firebase_withdrawal_dict(row):
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "amount": int(row["amount"]),
        "upi_id": row["upi_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "processed_at": row["processed_at"],
        "updated_at": row["updated_at"],
    }


def firebase_save_user_by_id(user_id):
    row = get_user(user_id)
    if not row:
        return False
    return firebase_safe_set(
        f"users/{int(user_id)}",
        firebase_user_dict(row),
    )


def firebase_save_withdrawal_by_id(withdrawal_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM withdrawals WHERE id=?",
        (withdrawal_id,),
    ).fetchone()
    conn.close()
    if not row:
        return False
    return firebase_safe_set(
        f"withdrawals/{int(withdrawal_id)}",
        firebase_withdrawal_dict(row),
    )


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def sync_sqlite_and_firebase():
    """Two-way startup sync. Newer records win; Firebase-only users are restored.

    This is the important recovery layer: if the hosting machine loses bot.db,
    the next start can rebuild users/referrals/UPI/withdrawals from Firebase.
    """
    if not FIREBASE_ENABLED:
        return

    try:
        remote_users = firebase_get("users", {}) or {}
        remote_withdrawals = firebase_get("withdrawals", {}) or {}

        conn = db()
        local_users = conn.execute("SELECT * FROM users").fetchall()
        local_withdrawals = conn.execute("SELECT * FROM withdrawals").fetchall()

        local_user_ids = set()
        for row in local_users:
            uid = str(row["user_id"])
            local_user_ids.add(uid)
            remote = remote_users.get(uid) if isinstance(remote_users, dict) else None

            if not remote:
                firebase_safe_set(f"users/{uid}", firebase_user_dict(row))
                continue

            local_time = _parse_time(row["updated_at"])
            remote_time = _parse_time(remote.get("updated_at", ""))

            if remote_time > local_time:
                conn.execute(
                    """
                    UPDATE users SET
                        username=?, first_name=?, points=?, referral_code=?,
                        referred_by=?, referral_paid=?, upi_id=?, joined_at=?, updated_at=?
                    WHERE user_id=?
                    """,
                    (
                        remote.get("username", ""),
                        remote.get("first_name", ""),
                        int(remote.get("points", 0)),
                        remote.get("referral_code", f"REF{uid}"),
                        remote.get("referred_by"),
                        int(remote.get("referral_paid", 0)),
                        remote.get("upi_id", ""),
                        remote.get("joined_at", now()),
                        remote.get("updated_at", now()),
                        int(uid),
                    ),
                )
            else:
                firebase_safe_set(f"users/{uid}", firebase_user_dict(row))

        # Restore users that exist in Firebase but not on the hosting disk.
        for uid, remote in (remote_users.items() if isinstance(remote_users, dict) else []):
            if str(uid) in local_user_ids:
                continue
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO users
                    (user_id, username, first_name, points, referral_code, referred_by,
                     referral_paid, upi_id, joined_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(uid),
                        remote.get("username", ""),
                        remote.get("first_name", ""),
                        int(remote.get("points", 0)),
                        remote.get("referral_code", f"REF{uid}"),
                        remote.get("referred_by"),
                        int(remote.get("referral_paid", 0)),
                        remote.get("upi_id", ""),
                        remote.get("joined_at", now()),
                        remote.get("updated_at", now()),
                    ),
                )
            except Exception as exc:
                logger.error("Could not restore Firebase user %s: %s", uid, exc)

        # Withdrawals are backed up/restored too, so balances can be reconciled.
        local_wd_ids = set()
        for row in local_withdrawals:
            wid = str(row["id"])
            local_wd_ids.add(wid)
            remote = remote_withdrawals.get(wid) if isinstance(remote_withdrawals, dict) else None
            if not remote:
                firebase_safe_set(f"withdrawals/{wid}", firebase_withdrawal_dict(row))
                continue

            local_time = _parse_time(row["updated_at"])
            remote_time = _parse_time(remote.get("updated_at", ""))
            if remote_time > local_time:
                conn.execute(
                    """
                    UPDATE withdrawals SET user_id=?, amount=?, upi_id=?, status=?,
                    created_at=?, processed_at=?, updated_at=? WHERE id=?
                    """,
                    (
                        int(remote.get("user_id", 0)),
                        int(remote.get("amount", 0)),
                        remote.get("upi_id", ""),
                        remote.get("status", "pending"),
                        remote.get("created_at", now()),
                        remote.get("processed_at"),
                        remote.get("updated_at", now()),
                        int(wid),
                    ),
                )
            else:
                firebase_safe_set(f"withdrawals/{wid}", firebase_withdrawal_dict(row))

        for wid, remote in (remote_withdrawals.items() if isinstance(remote_withdrawals, dict) else []):
            if str(wid) in local_wd_ids:
                continue
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO withdrawals
                    (id, user_id, amount, upi_id, status, created_at, processed_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(wid),
                        int(remote.get("user_id", 0)),
                        int(remote.get("amount", 0)),
                        remote.get("upi_id", ""),
                        remote.get("status", "pending"),
                        remote.get("created_at", now()),
                        remote.get("processed_at"),
                        remote.get("updated_at", now()),
                    ),
                )
            except Exception as exc:
                logger.error("Could not restore Firebase withdrawal %s: %s", wid, exc)

        conn.commit()
        conn.close()
        logger.info(
            "Firebase sync complete | remote users=%s | remote withdrawals=%s",
            len(remote_users) if isinstance(remote_users, dict) else 0,
            len(remote_withdrawals) if isinstance(remote_withdrawals, dict) else 0,
        )
    except Exception as exc:
        logger.exception("Firebase startup sync failed: %s", exc)


# ============================================================
# REQUIRED CHANNELS / GROUPS
# CHAT IDS ARE EXACTLY THE ONES YOU PROVIDED
# ============================================================

REQUIRED_CHATS = [
    {
        "name": "Main Channel",
        "join_url": "https://t.me/+CvMzUOQFDiczNjZl",
        "chat_id": "-1003540916448",
    },
    {
        "name": "Main GC",
        "join_url": "https://t.me/+si63pZE9oqg3MTdl",
        "chat_id": "-1003709273961",
    },
    {
        "name": "Second Channel",
        "join_url": "https://t.me/+727mMnMYUdJhMDZl",
        "chat_id": "-1004318439063",
    },
    {
        "name": "Last Channel",
        "join_url": "https://t.me/lxmodemenu",
        "chat_id": "@lxmodemenu",
    },
    {
        "name": "New Channel",
        "join_url": "https://t.me/+98SVstjmdf45OGQ1",
        "chat_id": "-1002818481856",
    },
]


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("GOKU_REFERRAL")


# ============================================================
# DATABASE
# ============================================================

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
            joined_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
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
            processed_at TEXT,
            updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    """)

    cur.execute(
        "INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)",
        (MAINTENANCE_KEY, "0"),
    )

    cols = {
        row["name"]
        for row in cur.execute(
            "PRAGMA table_info(users)"
        ).fetchall()
    }

    if "upi_id" not in cols:
        cur.execute(
            "ALTER TABLE users ADD COLUMN upi_id TEXT DEFAULT ''"
        )

    if "updated_at" not in cols:
        cur.execute(
            "ALTER TABLE users ADD COLUMN updated_at TEXT DEFAULT ''"
        )
        cur.execute(
            "UPDATE users SET updated_at=joined_at WHERE updated_at IS NULL OR updated_at=''"
        )

    wd_cols = {
        row["name"]
        for row in cur.execute("PRAGMA table_info(withdrawals)").fetchall()
    }
    if "updated_at" not in wd_cols:
        cur.execute(
            "ALTER TABLE withdrawals ADD COLUMN updated_at TEXT DEFAULT ''"
        )
        cur.execute(
            "UPDATE withdrawals SET updated_at=COALESCE(processed_at, created_at) "
            "WHERE updated_at IS NULL OR updated_at=''"
        )

    conn.commit()
    conn.close()


# ============================================================
# BOT SETTINGS / BROADCAST HELPERS
# ============================================================

def get_setting(key, default=""):
    conn = db()
    row = conn.execute(
        "SELECT value FROM bot_settings WHERE key=?",
        (key,),
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = db()
    conn.execute(
        """
        INSERT INTO bot_settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, str(value)),
    )
    conn.commit()
    conn.close()

    if key == MAINTENANCE_KEY and FIREBASE_ENABLED:
        firebase_safe_set(
            f"settings/{key}",
            {"value": str(value), "updated_at": now()},
        )


def maintenance_enabled():
    return get_setting(MAINTENANCE_KEY, "0") == "1"


def all_user_ids():
    conn = db()
    rows = conn.execute(
        "SELECT user_id FROM users ORDER BY user_id"
    ).fetchall()
    conn.close()
    return [int(row["user_id"]) for row in rows]


async def broadcast_text(bot, text):
    """
    Legacy text broadcaster. Sends plain text to every registered user.
    """
    sent = 0
    failed = 0

    for user_id in all_user_ids():
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
            )
            sent += 1
        except Exception as exc:
            failed += 1
            logger.warning(
                "Broadcast failed | user=%s | %s",
                user_id,
                exc,
            )

    return sent, failed


async def broadcast_message(bot, source_chat_id, message_id):
    """
    Copy the admin's message to every registered user.
    This supports text, photo, video, document, audio, sticker, etc.
    """
    sent = 0
    failed = 0

    for user_id in all_user_ids():
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=message_id,
            )
            sent += 1
        except Exception as exc:
            failed += 1
            logger.warning(
                "Broadcast copy failed | user=%s | %s",
                user_id,
                exc,
            )

    return sent, failed


# ============================================================
# USER FUNCTIONS
# ============================================================

def get_user(user_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row


def create_user(tg_user, referred_by=None):
    existing = get_user(tg_user.id)

    if existing:
        conn = db()
        conn.execute(
            """
            UPDATE users
            SET username=?, first_name=?, updated_at=?
            WHERE user_id=?
            """,
            (
                tg_user.username or "",
                tg_user.first_name or "",
                now(),
                tg_user.id,
            ),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (tg_user.id,),
        ).fetchone()

        conn.close()
        firebase_save_user_by_id(tg_user.id)
        return row, False

    conn = db()

    conn.execute(
        """
        INSERT INTO users
        (
            user_id,
            username,
            first_name,
            points,
            referral_code,
            referred_by,
            referral_paid,
            upi_id,
            joined_at,
            updated_at
        )
        VALUES (?, ?, ?, 0, ?, ?, 0, '', ?, ?)
        """,
        (
            tg_user.id,
            tg_user.username or "",
            tg_user.first_name or "",
            f"REF{tg_user.id}",
            referred_by,
            now(),
            now(),
        ),
    )

    conn.commit()

    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (tg_user.id,),
    ).fetchone()

    conn.close()

    firebase_save_user_by_id(tg_user.id)
    return row, True


def set_upi(user_id, upi):
    conn = db()
    conn.execute(
        "UPDATE users SET upi_id=?, updated_at=? WHERE user_id=?",
        (upi, now(), user_id),
    )
    conn.commit()
    conn.close()
    firebase_save_user_by_id(user_id)


# ============================================================
# REFERRAL
# ============================================================

def add_referral_reward(referrer_id, referred_id):
    if not referrer_id or referrer_id == referred_id:
        return False

    conn = db()
    cur = conn.cursor()

    row = cur.execute(
        """
        SELECT referred_by, referral_paid
        FROM users
        WHERE user_id=?
        """,
        (referred_id,),
    ).fetchone()

    if not row:
        conn.close()
        return False

    if row["referred_by"] != referrer_id:
        conn.close()
        return False

    if row["referral_paid"] == 1:
        conn.close()
        return False

    cur.execute(
        """
        UPDATE users
        SET points=points+?, updated_at=?
        WHERE user_id=?
        """,
        (REWARD_PER_REFERRAL, now(), referrer_id),
    )

    cur.execute(
        """
        UPDATE users
        SET referral_paid=1, updated_at=?
        WHERE user_id=?
        """,
        (now(), referred_id),
    )

    conn.commit()
    conn.close()

    # Backup BOTH sides of the referral transaction immediately.
    firebase_save_user_by_id(referrer_id)
    firebase_save_user_by_id(referred_id)

    return True


def get_referral_stats(user_id):
    conn = db()

    row = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM users
        WHERE referred_by=?
        AND referral_paid=1
        """,
        (user_id,),
    ).fetchone()

    conn.close()

    return row["total"]


# ============================================================
# KEYBOARDS
# ============================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["👥 Refer Friend", "💰 Points"],
        ["💳 Bind UPI", "💸 Withdrawal"],
        ["🆘 Help"],
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
        rows.append(
            [
                InlineKeyboardButton(
                    "JOIN",
                    url=chat["join_url"],
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "✅ Verify Joined",
                callback_data="verify_join",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


def admin_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 Broadcast",
                    callback_data="admin_broadcast",
                ),
                InlineKeyboardButton(
                    "🔧 Maintenance",
                    callback_data="admin_maintenance",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🟢 Live Now",
                    callback_data="admin_live",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📥 Pending Requests",
                    callback_data="admin_pending",
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 All Requests",
                    callback_data="admin_all",
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ Completed",
                    callback_data="admin_completed",
                )
            ],
            [
                InlineKeyboardButton(
                    "👥 Users",
                    callback_data="admin_users",
                ),
                InlineKeyboardButton(
                    "🔗 Referrals",
                    callback_data="admin_referrals",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📊 Stats",
                    callback_data="admin_stats",
                )
            ],
        ]
    )


# ============================================================
# MEMBERSHIP
# ============================================================

def member_is_joined(member):
    if member.status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    }:
        return True

    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(
            getattr(member, "is_member", False)
        )

    return False


RUNTIME_CHAT_IDS = {}


async def resolve_chat_id(bot, chat):
    name = chat["name"]

    if name in RUNTIME_CHAT_IDS:
        return RUNTIME_CHAT_IDS[name]

    raw = chat["chat_id"]

    try:
        info = await bot.get_chat(raw)
        RUNTIME_CHAT_IDS[name] = info.id
        return info.id

    except TelegramError as exc:
        logger.warning(
            "Could not resolve %s (%s): %s",
            name,
            raw,
            exc,
        )

        # Keep original value. This is important for
        # @lxmodemenu and already-correct numeric IDs.
        RUNTIME_CHAT_IDS[name] = raw
        return raw


async def check_one_chat(bot, user_id, chat):
    try:
        actual_chat_id = await resolve_chat_id(
            bot,
            chat,
        )

        member = await bot.get_chat_member(
            chat_id=actual_chat_id,
            user_id=user_id,
        )

        joined = member_is_joined(member)

        logger.info(
            "MEMBERSHIP | %s | user=%s | status=%s | joined=%s",
            chat["name"],
            user_id,
            member.status,
            joined,
        )

        return joined, None

    except TelegramError as exc:
        logger.warning(
            "MEMBERSHIP ERROR | %s | user=%s | %s",
            chat["name"],
            user_id,
            exc,
        )

        return False, str(exc)

    except Exception as exc:
        logger.exception(
            "Unexpected membership error | %s",
            chat["name"],
        )

        return False, str(exc)


async def get_not_joined_chats(bot, user_id):
    missing = []
    errors = {}

    for chat in REQUIRED_CHATS:
        joined, error = await check_one_chat(
            bot,
            user_id,
            chat,
        )

        if not joined:
            missing.append(chat["name"])

            if error:
                errors[chat["name"]] = error

    return missing, errors


async def gate_user(bot, user_id):
    return await get_not_joined_chats(
        bot,
        user_id,
    )


# ============================================================
# JOIN SCREEN
# ============================================================

async def send_join_screen(
    update,
    missing=None,
):
    if missing:
        missing_text = "\n".join(
            f"❌ {name}"
            for name in missing
        )

        text = (
            "🔒 <b>JOIN REQUIRED</b>\n\n"
            "You still need to join:\n\n"
            f"{missing_text}\n\n"
            "After joining all of them, tap "
            "<b>Verify Joined</b>."
        )

    else:
        text = (
            "🔒 <b>JOIN REQUIRED</b>\n\n"
            "Please join all required "
            "channels/groups.\n\n"
            "After joining all of them, tap "
            "<b>Verify Joined</b>."
        )

    if update.message:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=join_keyboard(),
        )

    elif update.callback_query:
        await update.callback_query.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=join_keyboard(),
        )


# ============================================================
# REFERRAL
# ============================================================

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


async def maybe_reward_user(
    bot,
    user_id,
):
    user = get_user(user_id)

    if not user:
        return False

    if not user["referred_by"]:
        return False

    rewarded = add_referral_reward(
        user["referred_by"],
        user_id,
    )

    if not rewarded:
        return False

    try:
        await bot.send_message(
            user["referred_by"],
            "🎉 <b>New Referral!</b>\n\n"
            f"You earned <b>₹{REWARD_PER_REFERRAL}</b>.",
            parse_mode="HTML",
        )
    except Exception:
        pass

    return True


# ============================================================
# MAINTENANCE GATE
# ============================================================

async def maintenance_gate(update, context):
    """
    Returns True when the current non-admin user is blocked by
    maintenance mode. Admin is always allowed through.
    """
    user = update.effective_user

    if not user:
        return False

    if user.id == ADMIN_ID:
        return False

    if not maintenance_enabled():
        return False

    # Never send maintenance replies into groups.
    chat = update.effective_chat
    if chat and chat.type != "private":
        return True

    message = getattr(update, "message", None)
    if message:
        await message.reply_text(
            "🔧 <b>Bot Maintenance</b>\n\n"
            "The bot is currently under maintenance.\n"
            "Please try again later.",
            parse_mode="HTML",
        )

    return True


# ============================================================
# START
# ============================================================

async def start(update, context):
    user = update.effective_user

    if (
        not user
        or not update.message
        or update.effective_chat.type != "private"
    ):
        return

    if await maintenance_gate(update, context):
        return

    referrer_id = extract_referrer(
        context.args
    )

    existing = get_user(user.id)

    if not existing:
        if referrer_id == user.id:
            referrer_id = None

        elif referrer_id and not get_user(referrer_id):
            referrer_id = None

        existing, _ = create_user(
            user,
            referrer_id,
        )

    else:
        create_user(user)

    context.user_data.clear()

    missing, _ = await gate_user(
        context.bot,
        user.id,
    )

    if missing:
        await send_join_screen(
            update,
            missing,
        )
        return

    await maybe_reward_user(
        context.bot,
        user.id,
    )

    await update.message.reply_text(
        "🎉 <b>Welcome!</b>\n\n"
        "Your account is active.\n"
        "Choose an option below:",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# VERIFY BUTTON
# ============================================================

async def verify_join(update, context):
    query = update.callback_query

    if (
        not query
        or not query.message
        or query.message.chat.type != "private"
    ):
        if query:
            await query.answer()
        return

    await query.answer(
        f"Checking all {len(REQUIRED_CHATS)}..."
    )

    user_id = query.from_user.id

    missing, errors = await gate_user(
        context.bot,
        user_id,
    )

    if missing:
        missing_text = "\n".join(
            f"❌ {name}"
            for name in missing
        )

        text = (
            "❌ <b>Verification Failed</b>\n\n"
            "You still need to join:\n\n"
            f"{missing_text}\n\n"
            "Join them and tap "
            "<b>Verify Joined</b> again."
        )

        # Show useful Telegram errors only in logs,
        # not as ugly API text to users.
        for name, error in errors.items():
            logger.warning(
                "Verification detail | %s | %s",
                name,
                error,
            )

        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=join_keyboard(),
        )

        return

    await maybe_reward_user(
        context.bot,
        user_id,
    )

    await query.message.edit_text(
        "🎉 <b>Verification Successful!</b>",
        parse_mode="HTML",
    )

    await query.message.reply_text(
        "🏠 <b>Main Menu</b>\n\n"
        "Choose an option below:",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# /VERIFY
# ============================================================

async def cancel_command(update, context):
    if (
        not update.message
        or update.effective_chat.type != "private"
    ):
        return

    if update.effective_user.id != ADMIN_ID:
        return

    context.user_data.pop("admin_broadcast", None)
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=MAIN_KEYBOARD,
    )


async def verify_command(update, context):
    if (
        not update.message
        or update.effective_chat.type != "private"
    ):
        return

    if await maintenance_gate(update, context):
        return

    missing, _ = await gate_user(
        context.bot,
        update.effective_user.id,
    )

    if missing:
        await send_join_screen(
            update,
            missing,
        )
        return

    await maybe_reward_user(
        context.bot,
        update.effective_user.id,
    )

    await update.message.reply_text(
        "✅ <b>All required joins are verified.</b>",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# INSTANT CHAT MEMBER UPDATE
# ============================================================

async def handle_chat_member(update, context):
    cm = update.chat_member

    if not cm:
        return

    user = cm.new_chat_member.user

    if not user or user.is_bot:
        return

    old_joined = member_is_joined(
        cm.old_chat_member
    )

    new_joined = member_is_joined(
        cm.new_chat_member
    )

    # Only react to an actual join/change-to-member.
    if not new_joined or old_joined:
        return

    matched = False

    for required in REQUIRED_CHATS:
        try:
            actual_id = await resolve_chat_id(
                context.bot,
                required,
            )

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

    missing, _ = await gate_user(
        context.bot,
        user.id,
    )

    # As soon as the last required chat is joined,
    # referral is credited and menu is sent.
    if not missing:
        await maybe_reward_user(
            context.bot,
            user.id,
        )

        try:
            await context.bot.send_message(
                user.id,
                "🎉 <b>All required joins are complete!</b>\n\n"
                "Your account is now active.",
                parse_mode="HTML",
                reply_markup=MAIN_KEYBOARD,
            )
        except Exception as exc:
            logger.warning(
                "Could not send instant menu: %s",
                exc,
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

    if not me.username:
        await update.message.reply_text(
            "❌ Bot username is unavailable.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    link = (
        f"https://t.me/{me.username}"
        f"?start={user['referral_code']}"
    )

    count = get_referral_stats(
        user["user_id"]
    )

    await update.message.reply_text(
        "👥 <b>Refer Friend</b>\n\n"
        "Share your personal referral link:\n\n"
        f"<code>{link}</code>\n\n"
        f"💰 Reward per successful referral: "
        f"<b>₹{REWARD_PER_REFERRAL}</b>\n"
        f"👥 Successful referrals: <b>{count}</b>\n\n"
        "A referral is counted once after the "
        "referred user completes all required joins.",
        parse_mode="HTML",
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

    count = get_referral_stats(
        user["user_id"]
    )

    await update.message.reply_text(
        "💰 <b>Your Balance</b>\n\n"
        f"Balance: <b>₹{user['points']}</b>\n"
        f"Successful referrals: <b>{count}</b>\n"
        f"Reward per referral: "
        f"<b>₹{REWARD_PER_REFERRAL}</b>",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# UPI BINDING
# ============================================================

async def bind_upi(update, context):
    user = get_user(update.effective_user.id)

    if not user:
        return

    if user["upi_id"]:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✏️ Change UPI",
                        callback_data="upi_change",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="upi_cancel",
                    )
                ],
            ]
        )

        await update.message.reply_text(
            "💳 <b>UPI Binding</b>\n\n"
            f"Saved UPI: <code>{user['upi_id']}</code>\n\n"
            "You can keep this UPI or change it.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    context.user_data["upi_step"] = "bind"

    await update.message.reply_text(
        "💳 <b>Bind UPI ID</b>\n\n"
        "Send your UPI ID.\n\n"
        "Example: <code>name@upi</code>",
        parse_mode="HTML",
        reply_markup=BACK_KEYBOARD,
    )


# ============================================================
# WITHDRAWAL
# ============================================================

def valid_upi(upi):
    return bool(
        re.fullmatch(
            r"[A-Za-z0-9._-]{2,}@[A-Za-z0-9._-]{2,}",
            upi.strip(),
        )
    )


async def withdrawal(update, context):
    user = get_user(
        update.effective_user.id
    )

    if not user:
        return

    if user["points"] < MIN_WITHDRAWAL:
        await update.message.reply_text(
            f"❌ Minimum withdrawal is "
            f"<b>₹{MIN_WITHDRAWAL}</b>.\n\n"
            f"Your balance: "
            f"<b>₹{user['points']}</b>",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if user["upi_id"]:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"✅ Use {user['upi_id']}",
                        callback_data="wd_use_saved",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✏️ Change UPI",
                        callback_data="wd_change_upi",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="wd_cancel",
                    )
                ],
            ]
        )

        await update.message.reply_text(
            "💸 <b>Withdrawal</b>\n\n"
            f"Balance: <b>₹{user['points']}</b>\n"
            f"Saved UPI: <code>{user['upi_id']}</code>\n\n"
            "Choose an option:",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    else:
        context.user_data[
            "withdrawal_step"
        ] = "upi"

        await update.message.reply_text(
            "💳 <b>Bind UPI</b>\n\n"
            "No UPI is saved yet. Send your UPI ID here.\n\n"
            "Example: <code>name@upi</code>",
            parse_mode="HTML",
            reply_markup=BACK_KEYBOARD,
        )


async def create_withdrawal(
    bot,
    user_id,
    upi,
):
    user = get_user(user_id)

    if not user:
        return None

    if user["points"] < MIN_WITHDRAWAL:
        return None

    amount = user["points"]

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE users
        SET points=points-?, updated_at=?
        WHERE user_id=?
        AND points>=?
        """,
        (
            amount,
            now(),
            user_id,
            amount,
        ),
    )

    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return None

    cur.execute(
        """
        INSERT INTO withdrawals
        (
            user_id,
            amount,
            upi_id,
            status,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, 'pending', ?, ?)
        """,
        (
            user_id,
            amount,
            upi,
            now(),
            now(),
        ),
    )

    withdrawal_id = cur.lastrowid

    conn.commit()
    conn.close()

    firebase_save_user_by_id(user_id)
    firebase_save_withdrawal_by_id(withdrawal_id)

    try:
        admin_keyboard_local = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Complete",
                        callback_data=(
                            f"wd_complete_{withdrawal_id}"
                        ),
                    ),
                    InlineKeyboardButton(
                        "❌ Reject",
                        callback_data=(
                            f"wd_reject_{withdrawal_id}"
                        ),
                    ),
                ]
            ]
        )

        username = (
            f"@{user['username']}"
            if user["username"]
            else "No username"
        )

        await bot.send_message(
            ADMIN_ID,
            "💸 <b>New Withdrawal Request</b>\n\n"
            f"Request: <b>#{withdrawal_id}</b>\n"
            f"User: <code>{user_id}</code>\n"
            f"Username: {username}\n"
            f"Amount: <b>₹{amount}</b>\n"
            f"UPI: <code>{upi}</code>\n"
            f"Time: {now()}",
            parse_mode="HTML",
            reply_markup=admin_keyboard_local,
        )

    except Exception as exc:
        logger.warning(
            "Admin notification failed: %s",
            exc,
        )

    return withdrawal_id, amount


# ============================================================
# WITHDRAWAL CALLBACKS
# ============================================================

async def withdrawal_callback(
    update,
    context,
):
    query = update.callback_query

    if (
        not query
        or not query.message
        or query.message.chat.type != "private"
    ):
        if query:
            await query.answer()
        return

    await query.answer()

    if query.data == "wd_cancel":
        await query.edit_message_text(
            "❌ Withdrawal cancelled."
        )
        return

    if query.data == "wd_change_upi":
        context.user_data[
            "withdrawal_step"
        ] = "upi"

        await query.message.reply_text(
            "✏️ <b>Change UPI</b>\n\n"
            "Send your new UPI ID.",
            parse_mode="HTML",
            reply_markup=BACK_KEYBOARD,
        )
        return

    if query.data == "upi_change":
        context.user_data.clear()
        context.user_data["upi_step"] = "bind"

        await query.message.reply_text(
            "✏️ <b>Change UPI ID</b>\n\n"
            "Send your new UPI ID.\n\n"
            "Example: <code>name@upi</code>",
            parse_mode="HTML",
            reply_markup=BACK_KEYBOARD,
        )
        return

    if query.data == "upi_cancel":
        await query.edit_message_text(
            "❌ UPI change cancelled."
        )
        return

    if query.data == "wd_use_saved":

        user = get_user(
            query.from_user.id
        )

        if not user or not user["upi_id"]:
            await query.message.reply_text(
                "❌ No saved UPI found.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        result = await create_withdrawal(
            context.bot,
            query.from_user.id,
            user["upi_id"],
        )

        if not result:
            await query.message.reply_text(
                "❌ Withdrawal could not be created.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        withdrawal_id, amount = result

        await query.edit_message_text(
            "✅ <b>Withdrawal Request Submitted</b>\n\n"
            f"Request: <b>#{withdrawal_id}</b>\n"
            f"Amount: <b>₹{amount}</b>\n"
            f"UPI: <code>{user['upi_id']}</code>\n\n"
            "Your request is waiting for admin review.",
            parse_mode="HTML",
        )

        await query.message.reply_text(
            "🏠 <b>Main Menu</b>",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )


# ============================================================
# HELP
# ============================================================

async def help_menu(update, context):
    await update.message.reply_text(
        "🆘 <b>Help</b>\n\n"
        "👥 Refer Friend — get your referral link.\n"
        "💰 Points — check your balance.\n"
        "💳 Bind UPI — save or change your UPI ID.\n"
        "💸 Withdrawal — withdraw your balance.\n\n"
        "For withdrawal issues, contact the admin.",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# TEXT HANDLER
# IMPORTANT FIX:
# MENU BUTTONS ARE HANDLED BEFORE JOIN GATE
# ============================================================

async def handle_any_message(update, context):
    """
    Routes every non-command private message.
    If admin is in broadcast mode, copy the exact message to all
    registered users. Otherwise keep the existing text flow.
    """
    if not update.message:
        return

    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type != "private":
        return

    if (
        user
        and user.id == ADMIN_ID
        and context.user_data.get("admin_broadcast") is True
    ):
        context.user_data.pop("admin_broadcast", None)

        sent, failed = await broadcast_message(
            context.bot,
            chat.id,
            update.message.message_id,
        )

        await update.message.reply_text(
            "📢 Broadcast Finished\n\n"
            f"✅ Sent: {sent}\n"
            f"❌ Failed: {failed}",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # Preserve the existing bot behaviour for normal text messages.
    if update.message.text:
        await handle_text(update, context)


async def handle_text(update, context):
    if (
        not update.message
        or update.effective_chat.type != "private"
    ):
        return

    if await maintenance_gate(update, context):
        return

    user = update.effective_user

    if not user:
        return

    text = (
        update.message.text or ""
    ).strip()

    # --------------------------------------------------------
    # ADMIN BROADCAST INPUT
    # --------------------------------------------------------
    if (
        user.id == ADMIN_ID
        and context.user_data.get("admin_broadcast") is True
    ):
        if not text:
            await update.message.reply_text(
                "❌ Broadcast message cannot be empty."
            )
            return

        context.user_data.pop("admin_broadcast", None)

        sent, failed = await broadcast_text(
            context.bot,
            text,
        )

        await update.message.reply_text(
            "📢 Broadcast Finished\n\n"
            f"✅ Sent: {sent}\n"
            f"❌ Failed: {failed}",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --------------------------------------------------------
    # BACK ALWAYS WORKS
    # --------------------------------------------------------

    if text == "⬅️ Back":
        context.user_data.clear()

        await update.message.reply_text(
            "🏠 <b>Main Menu</b>",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --------------------------------------------------------
    # HELP ALWAYS WORKS
    # --------------------------------------------------------

    if text == "🆘 Help":
        await help_menu(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # UPI BIND INPUT MUST BE CHECKED BEFORE GATE
    # --------------------------------------------------------

    if context.user_data.get("upi_step") == "bind":
        upi = text

        if not valid_upi(upi):
            await update.message.reply_text(
                "❌ Invalid UPI ID.\n\n"
                "Example: <code>name@upi</code>",
                parse_mode="HTML",
                reply_markup=BACK_KEYBOARD,
            )
            return

        set_upi(user.id, upi)
        context.user_data.clear()

        await update.message.reply_text(
            "✅ <b>UPI Bound Successfully</b>\n\n"
            f"UPI: <code>{upi}</code>\n\n"
            "Your UPI is now saved for future withdrawals.",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --------------------------------------------------------
    # WITHDRAWAL INPUT MUST BE CHECKED BEFORE GATE
    # --------------------------------------------------------

    if context.user_data.get(
        "withdrawal_step"
    ) == "upi":

        upi = text

        if not valid_upi(upi):
            await update.message.reply_text(
                "❌ Invalid UPI ID.\n\n"
                "Example: <code>name@upi</code>",
                parse_mode="HTML",
                reply_markup=BACK_KEYBOARD,
            )
            return

        set_upi(
            user.id,
            upi,
        )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ <b>UPI Bound Successfully</b>\n\n"
            f"UPI: <code>{upi}</code>\n\n"
            "Now tap <b>Withdrawal</b> to submit your request.",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --------------------------------------------------------
    # MAIN MENU BUTTONS
    # --------------------------------------------------------

    if text in {
        "👥 Refer Friend",
        "💰 Points",
        "💳 Bind UPI",
        "💸 Withdrawal",
    }:

        missing, _ = await gate_user(
            context.bot,
            user.id,
        )

        if missing:
            await send_join_screen(
                update,
                missing,
            )
            return

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

        if text == "💳 Bind UPI":
            await bind_upi(
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

    # --------------------------------------------------------
    # RANDOM MESSAGE
    # --------------------------------------------------------
    #
    # If user sends any random message:
    # 1. Check joins
    # 2. If missing -> show missing chats + buttons
    # 3. If all joined -> show main menu
    # --------------------------------------------------------

    missing, _ = await gate_user(
        context.bot,
        user.id,
    )

    if missing:
        await send_join_screen(
            update,
            missing,
        )
        return

    await update.message.reply_text(
        "🏠 <b>Main Menu</b>\n\n"
        "Choose an option below:",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


# ============================================================
# ADMIN
# ============================================================

def withdrawal_rows(
    status=None,
    limit=30,
):
    conn = db()

    if status:
        rows = conn.execute(
            """
            SELECT *
            FROM withdrawals
            WHERE status=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()

    else:
        rows = conn.execute(
            """
            SELECT *
            FROM withdrawals
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    conn.close()

    return rows


def format_withdrawal(row):
    conn = db()

    user = conn.execute(
        """
        SELECT username, first_name
        FROM users
        WHERE user_id=?
        """,
        (row["user_id"],),
    ).fetchone()

    conn.close()

    if user and user["username"]:
        username = f"@{user['username']}"
    else:
        username = "No username"

    return (
        f"#{row['id']} | "
        f"{row['status'].upper()}\n"
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
        await update.message.reply_text(
            "❌ Admin only."
        )
        return

    context.user_data.pop("admin_broadcast", None)

    status = (
        "🔧 Maintenance: <b>ON</b>"
        if maintenance_enabled()
        else "🟢 Status: <b>LIVE</b>"
    )

    await update.message.reply_text(
        "🛠 <b>Admin Panel</b>\n\n"
        f"{status}\n\n"
        "Choose an option:",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


def admin_users_text(limit=25):
    conn = db()
    rows = conn.execute(
        """
        SELECT user_id, username, first_name, points,
               referred_by, referral_paid, upi_id, joined_at
        FROM users
        ORDER BY joined_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()

    if not rows:
        return "👥 <b>Users</b>\\n\\nNo users found."

    parts = ["👥 <b>Users</b>", ""]
    for row in rows:
        username = f"@{row['username']}" if row["username"] else "No username"
        parts.append(
            f"👤 {row['first_name'] or 'Unknown'} ({username})\\n"
            f"ID: <code>{row['user_id']}</code>\\n"
            f"Balance: ₹{row['points']}\\n"
            f"UPI: <code>{row['upi_id'] or '-'}</code>\\n"
            f"Referred by: <code>{row['referred_by'] or '-'}</code>\\n"
            f"Referral paid: {'Yes' if row['referral_paid'] else 'No'}"
        )
        parts.append("")

    return "\\n".join(parts)


def admin_referrals_text(limit=30):
    conn = db()
    rows = conn.execute(
        """
        SELECT r.user_id AS referred_id,
               r.referred_by AS referrer_id,
               r.referral_paid,
               r.username,
               r.first_name
        FROM users r
        WHERE r.referred_by IS NOT NULL
        ORDER BY r.joined_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()

    if not rows:
        return "🔗 <b>Referrals</b>\\n\\nNo referrals found."

    parts = ["🔗 <b>Referral Records</b>", ""]
    for row in rows:
        status = "Paid" if row["referral_paid"] else "Pending"
        parts.append(
            f"Referred user: <code>{row['referred_id']}</code>\\n"
            f"Referrer: <code>{row['referrer_id']}</code>\\n"
            f"Reward: {status}"
        )
        parts.append("")

    return "\\n".join(parts)


async def admin_callback(update, context):
    query = update.callback_query

    if (
        not query
        or not query.message
        or query.message.chat.type != "private"
    ):
        if query:
            await query.answer()
        return

    if query.from_user.id != ADMIN_ID:
        await query.answer(
            "Admin only.",
            show_alert=True,
        )
        return

    await query.answer()

    if query.data == "admin_broadcast":
        context.user_data["admin_broadcast"] = True
        await query.message.reply_text(
            "📢 <b>Broadcast Mode</b>\n\n"
            "Send the message you want to deliver to "
            "<b>all registered members</b> by DM.\n\n"
            "Send <code>/cancel</code> to cancel.",
            parse_mode="HTML",
            reply_markup=BACK_KEYBOARD,
        )
        return

    if query.data == "admin_maintenance":
        enabled = maintenance_enabled()

        if enabled:
            set_setting(MAINTENANCE_KEY, "0")
            await query.message.reply_text(
                "🟢 <b>Maintenance OFF</b>\n\n"
                "Bot is live again.",
                parse_mode="HTML",
                reply_markup=admin_keyboard(),
            )
        else:
            set_setting(MAINTENANCE_KEY, "1")
            await query.message.reply_text(
                "🔧 <b>Maintenance ON</b>\n\n"
                "Members will see the maintenance message "
                "and normal bot functions are blocked.",
                parse_mode="HTML",
                reply_markup=admin_keyboard(),
            )
        return

    if query.data == "admin_live":
        sent, failed = await broadcast_text(
            context.bot,
            "🟢 <b>GOKU BOT IS LIVE NOW!</b>\n\n"
            "The bot is online and ready to use. 🔥",
        )

        await query.message.reply_text(
            "🟢 <b>Live Now Broadcast Finished</b>\n\n"
            f"✅ Sent: <b>{sent}</b>\n"
            f"❌ Failed: <b>{failed}</b>",
            parse_mode="HTML",
            reply_markup=admin_keyboard(),
        )
        return

    if query.data == "admin_pending":

        rows = withdrawal_rows(
            "pending"
        )

        if not rows:
            text = (
                "📥 <b>Pending Withdrawals</b>\n\n"
                "No pending requests."
            )
        else:
            text = (
                "📥 <b>Pending Withdrawals</b>\n\n"
                + "\n\n".join(
                    format_withdrawal(row)
                    for row in rows
                )
            )

    elif query.data == "admin_all":

        rows = withdrawal_rows()

        if not rows:
            text = (
                "📋 <b>All Withdrawals</b>\n\n"
                "No requests."
            )
        else:
            text = (
                "📋 <b>All Withdrawals</b>\n\n"
                + "\n\n".join(
                    format_withdrawal(row)
                    for row in rows
                )
            )

    elif query.data == "admin_completed":

        rows = withdrawal_rows(
            "completed"
        )

        if not rows:
            text = (
                "✅ <b>Completed Withdrawals</b>\n\n"
                "No completed requests."
            )
        else:
            text = (
                "✅ <b>Completed Withdrawals</b>\n\n"
                + "\n\n".join(
                    format_withdrawal(row)
                    for row in rows
                )
            )

    elif query.data == "admin_users":
        text = admin_users_text()

    elif query.data == "admin_referrals":
        text = admin_referrals_text()

    elif query.data == "admin_stats":

        conn = db()

        users = conn.execute(
            "SELECT COUNT(*) AS n FROM users"
        ).fetchone()["n"]

        pending = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM withdrawals
            WHERE status='pending'
            """
        ).fetchone()["n"]

        completed = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM withdrawals
            WHERE status='completed'
            """
        ).fetchone()["n"]

        total_completed = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS n
            FROM withdrawals
            WHERE status='completed'
            """
        ).fetchone()["n"]

        conn.close()

        text = (
            "📊 <b>Bot Stats</b>\n\n"
            f"Users: <b>{users}</b>\n"
            f"Pending withdrawals: <b>{pending}</b>\n"
            f"Completed withdrawals: <b>{completed}</b>\n"
            f"Completed amount: "
            f"<b>₹{total_completed}</b>"
        )

    else:
        return

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN WITHDRAWAL ACTION
# ============================================================

async def admin_withdrawal_action(
    update,
    context,
):
    query = update.callback_query

    if (
        not query
        or not query.message
        or query.message.chat.type != "private"
    ):
        if query:
            await query.answer()
        return

    if query.from_user.id != ADMIN_ID:
        await query.answer(
            "Admin only.",
            show_alert=True,
        )
        return

    await query.answer()

    parts = query.data.split("_")

    if len(parts) != 3:
        return

    action = parts[1]

    try:
        withdrawal_id = int(
            parts[2]
        )
    except ValueError:
        return

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM withdrawals
        WHERE id=?
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

    # --------------------------------------------------------
    # COMPLETE
    # --------------------------------------------------------

    if action == "complete":

        conn.execute(
            """
            UPDATE withdrawals
            SET
                status='completed',
                processed_at=?,
                updated_at=?
            WHERE id=?
            AND status='pending'
            """,
            (
                now(),
                now(),
                withdrawal_id,
            ),
        )

        conn.commit()

        updated = conn.execute(
            """
            SELECT *
            FROM withdrawals
            WHERE id=?
            """,
            (withdrawal_id,),
        ).fetchone()

        conn.close()
        firebase_save_withdrawal_by_id(withdrawal_id)

        await query.edit_message_text(
            "✅ <b>Withdrawal Completed</b>\n\n"
            + format_withdrawal(updated),
            parse_mode="HTML",
        )

        try:
            await context.bot.send_message(
                row["user_id"],
                "✅ <b>Withdrawal Completed</b>\n\n"
                f"Request: #{withdrawal_id}\n"
                f"Amount: ₹{row['amount']}\n"
                f"UPI: <code>{row['upi_id']}</code>",
                parse_mode="HTML",
                reply_markup=MAIN_KEYBOARD,
            )
        except Exception:
            pass

        return

    # --------------------------------------------------------
    # REJECT
    # --------------------------------------------------------

    if action == "reject":

        conn.execute(
            """
            UPDATE withdrawals
            SET
                status='rejected',
                processed_at=?,
                updated_at=?
            WHERE id=?
            AND status='pending'
            """,
            (
                now(),
                now(),
                withdrawal_id,
            ),
        )

        # Return amount to user balance.
        conn.execute(
            """
            UPDATE users
            SET points=points+?, updated_at=?
            WHERE user_id=?
            """,
            (
                row["amount"],
                now(),
                row["user_id"],
            ),
        )

        conn.commit()
        conn.close()
        firebase_save_withdrawal_by_id(withdrawal_id)
        firebase_save_user_by_id(row["user_id"])

        await query.edit_message_text(
            "❌ <b>Withdrawal Rejected</b>\n\n"
            f"Request: #{withdrawal_id}\n"
            f"₹{row['amount']} has been returned "
            "to the user's balance.",
            parse_mode="HTML",
        )

        try:
            await context.bot.send_message(
                row["user_id"],
                "❌ <b>Withdrawal Rejected</b>\n\n"
                f"Request: #{withdrawal_id}\n"
                f"₹{row['amount']} has been returned "
                "to your balance.",
                parse_mode="HTML",
                reply_markup=MAIN_KEYBOARD,
            )
        except Exception:
            pass


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):
    logger.error(
        "Unhandled exception: %s",
        context.error,
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable missing."
        )

    init_db()
    init_firebase()
    sync_sqlite_and_firebase()

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "verify",
            verify_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "cancel",
            cancel_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "admin",
            admin_command,
        )
    )

    # Verify join button
    app.add_handler(
        CallbackQueryHandler(
            verify_join,
            pattern=r"^verify_join$",
        )
    )

    # Withdrawal callbacks
    app.add_handler(
        CallbackQueryHandler(
            withdrawal_callback,
            pattern=r"^(wd_(use_saved|change_upi|cancel)|upi_(change|cancel))$",
        )
    )

    # Admin panel
    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_(broadcast|maintenance|live|pending|all|completed|stats|users|referrals)$",
        )
    )

    # Admin complete/reject
    app.add_handler(
        CallbackQueryHandler(
            admin_withdrawal_action,
            pattern=r"^wd_(complete|reject)_\d+$",
        )
    )

    # Instant membership updates
    app.add_handler(
        ChatMemberHandler(
            handle_chat_member,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # All normal messages / broadcast messages.
    # Commands are handled by CommandHandler above.
    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            handle_any_message,
        )
    )

    app.add_error_handler(
        error_handler
    )

    logger.info(
        "========================================"
    )
    logger.info(
        "GOKU REFERRAL BOT ONLINE"
    )
    logger.info(
        "Required chats: %d",
        len(REQUIRED_CHATS),
    )
    logger.info(
        "Reward: ₹%s",
        REWARD_PER_REFERRAL,
    )
    logger.info(
        "Minimum withdrawal: ₹%s",
        MIN_WITHDRAWAL,
    )
    logger.info(
        "Maintenance: %s",
        "ON" if maintenance_enabled() else "OFF",
    )
    logger.info(
        "========================================"
    )

    # ALL_TYPES is required for ChatMemberHandler.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
