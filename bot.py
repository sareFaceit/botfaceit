import os
import re
import telebot
from telebot import types
from telebot.handler_backends import CancelUpdate
import psycopg2
import psycopg2.extras
from flask import Flask
import threading
import random
import time
import datetime
import json

# Московское время UTC+3
MSK = datetime.timezone(datetime.timedelta(hours=3))

def fmt_dt(ts: int) -> str:
    """Форматирует unix-timestamp в московское время."""
    return datetime.datetime.fromtimestamp(ts, tz=MSK).strftime("%H:%M %d.%m")

# Очередь сообщений для авто-удаления: список (chat_id, msg_id, delete_at)
_delete_queue: list = []
_delete_lock = threading.Lock()

def AChedule_delete(chat_id, msg_id, min_minutes=5, max_minutes=20):
    """Запланировать удаление сообщения через случайный интервал 5–20 минут."""
    if not msg_id:
        return
    delay = random.randint(min_minutes * 60, max_minutes * 60)
    delete_at = int(time.time()) + delay
    with _delete_lock:
        _delete_queue.append((chat_id, msg_id, delete_at))

def _auto_delete_loop():
    """Фоновый поток: удаляет устаревшие сообщения бота."""
    while True:
        try:
            now = int(time.time())
            to_delete = []
            with _delete_lock:
                remaining = []
                for entry in _delete_queue:
                    if entry[2] <= now:
                        to_delete.append(entry)
                    else:
                        remaining.append(entry)
                _delete_queue[:] = remaining
            for chat_id, msg_id, _ in to_delete:
                try:
                    bot.delete_message(chat_id, msg_id)
                except Exception:
                    pass
        except Exception as e:
            print(f"[auto_delete] Ошибка: {e}")
        time.sleep(30)

from card_client import (
    generate_profile_card,
    generate_leaderboard_card,
    generate_match_result_card,
    generate_duo_leaderboard_card,
    CARDS_ENABLED,
    cache_avatar,
    get_cached_avatar,
)
print(f"✅ card_client загружен (CARDS_ENABLED={CARDS_ENABLED})")

def format_league(league) -> str:
    league = (league or "default").lower().strip()
    return {"quals": "Quals", "default": "Default", "2v2": "2v2"}.get(league, league.capitalize())

# ==================== FLASK ====================
app = Flask(__name__)

@app.route("/")
def health():
    return "Bot is running"

def run_flask():
    port = int(os.environ.get("PORT", 8099))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# ==================== КОНФИГ ====================
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID_RAW = os.environ.get("ADMIN_CHAT_ID", "0")
try:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)
except Exception:
    ADMIN_CHAT_ID = 0

# Канал/группа для логов наказаний и результатов матчей
LOG_CHAT_ID_RAW = os.environ.get("LOG_CHAT_ID", "0")
try:
    LOG_CHAT_ID = int(LOG_CHAT_ID_RAW)
except Exception:
    LOG_CHAT_ID = 0

# Начальные значения из env — могут быть переопределены через /setlogtopic и /setresulttopic
LOG_THREAD_ID_RAW = os.environ.get("LOG_THREAD_ID", "0")
try:
    LOG_THREAD_ID = int(LOG_THREAD_ID_RAW) if LOG_THREAD_ID_RAW and LOG_THREAD_ID_RAW != "0" else None
except Exception:
    LOG_THREAD_ID = None

RESULTS_THREAD_ID_RAW = os.environ.get("RESULTS_THREAD_ID", "0")
try:
    RESULTS_THREAD_ID = int(RESULTS_THREAD_ID_RAW) if RESULTS_THREAD_ID_RAW and RESULTS_THREAD_ID_RAW != "0" else None
except Exception:
    RESULTS_THREAD_ID = None

# Динамические значения (перезаписываются из БД при старте и через команды)
_dynamic_log_thread_id     = LOG_THREAD_ID
_dynamic_results_thread_id = RESULTS_THREAD_ID

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_URL", "")

ACCEPT_TIMEOUT = 60
MAPS = ["Zone 9", "Rust", "Province", "Sandstone", "Sakura"]

def _get_lobby_maps(lobby: dict) -> list:
    """Возвращает пул карт для лобби."""
    return list(MAPS)

_raw_ids = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS_LIST: list = [int(x.strip()) for x in _raw_ids.split(",") if x.strip().isdigit()]
ADMIN_ID = ADMIN_IDS_LIST[0] if ADMIN_IDS_LIST else 0

CREATOR_ID_RAW = os.environ.get("CREATOR_ID", "0")
try:
    CREATOR_ID = int(CREATOR_ID_RAW)
except Exception:
    CREATOR_ID = 0

telebot.apihelper.ENABLE_MIDDLEWARE = True
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# ==================== ОБЯЗАТЕЛЬНЫЕ КАНАЛЫ ====================
REQUIRED_CHANNELS = [
    {
        "id": "@sarefaceit",
        "url": "https://t.me/sarefaceit",
        "name": "Официальный канал",
    },
    {
        "id": os.environ.get("REQUIRED_CHANNEL_2_ID", ""),
        "url": "https://t.me/+CVI-8ZnLk0ZkMDcy",
        "name": "Паблик StandDarling",
    },
]

def check_subACriptions(user_id: int) -> list:
    """Возвращает список каналов, на которые пользователь не подписан."""
    not_subACribed = []
    for ch in REQUIRED_CHANNELS:
        ch_id = ch["id"]
        if not ch_id:
            continue
        try:
            member = bot.get_chat_member(ch_id, user_id)
            if member.status in ("left", "kicked", "banned"):
                not_subACribed.append(ch)
        except Exception as e:
            print(f"[check_sub] Не удалось проверить {ch_id}: {e} — пропускаем")
    return not_subACribed

def send_subACribe_message(chat_id: int, message_to_delete_id: int = None):
    """Отправляет сообщение с требованием подписаться на каналы."""
    if message_to_delete_id:
        try:
            bot.delete_message(chat_id, message_to_delete_id)
        except Exception:
            pass
    kb = types.InlineKeyboardMarkup(row_width=1)
    for ch in REQUIRED_CHANNELS:
        kb.add(types.InlineKeyboardButton(f"📢 {ch['name']}", url=ch["url"]))
    kb.add(types.InlineKeyboardButton("✅ Я подписался — проверить", callback_data="check_sub"))
    bot.send_message(
        chat_id,
        "⚠️ <b>Для использования бота необходимо подписаться на наши каналы:</b>\n\n"
        "1. 📢 <b>Официальный канал</b> — @actualfaceito\n"
        "2. 📢 <b>Паблик StandDarling</b>\n\n"
        "Подпишитесь на оба канала, затем нажмите кнопку ниже.",
        reply_markup=kb,
        parse_mode="HTML",
    )

# ==================== ГЛОБАЛЬНЫЕ СОСТОЯНИЯ ====================

active_lobbies        = {}
running_matches       = {}
user_lobby            = {}
lobby_player_messages = {}
ban_status_messages   = {}
ban_turn_messages     = {}
ban_notify_messages   = {}   # "Фаза бана началась!" — трекинг для удаления
draft_notify_messages = {}   # "Фаза выбора игроков!" — трекинг для удаления
draft_final_messages  = {}   # "Команды выбраны!" — трекинг для удаления
accept_status_messages= {}
match_found_messages  = {}
user_flow             = {}
awaiting_ACreenshot   = {}
rename_flow           = {}
parties               = {}
user_party            = {}
admin_action          = {}
match_registration    = {}
awaiting_party_invite = {}
change_flow           = {}
editstat_flow         = {}
promo_flow            = {}
promo_admin_flow      = {}
ban_flow              = {}   # uid -> {step, target_id, duration_days}
mute_flow             = {}   # uid -> {step, target_id, target_name, hours}
warn_flow             = {}   # uid -> {step, target_id, target_name}
give_item_flow        = {}   # uid -> {target_id, target_name}

def _build_shop_items_kb(target_id: int):
    """Inline keyboard with all shop items grouped by type for admin give-item."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    conn2 = _db(); cur2 = conn2.cursor()
    cur2.execute(
        "SELECT id, name, item_type, price FROM shop_items ORDER BY item_type, price",
    )
    rows = cur2.fetchall()
    conn2.close()

    TYPE_LABELS = {
        "frame":      "🖼 Рамки",
        "banner":     "🎨 Баннеры",
        "background": "🌄 Фоны",
        "sticker":    "🎭 Стикеры",
        "animation":  "✨ Анимации",
        "premium":    "👑 Premium",
        "x2coins":    "💰 x2 монеты",
        "unwarn":     "🛡 Снятие варна",
        "rename":     "✏️ Смена ника",
        "quals":      "⭐ Quals доступ",
    }

    from collections import OrderedDict
    by_type: dict = OrderedDict()
    for row_id, name, item_type, price in rows:
        by_type.setdefault(item_type, []).append((row_id, name, price))

    for item_type, items in by_type.items():
        label = TYPE_LABELS.get(item_type, item_type.title())
        kb.add(types.InlineKeyboardButton(f"── {label} ──", callback_data="admin_noop"))
        for row_id, name, price in items:
            kb.add(types.InlineKeyboardButton(
                f"{name}  ({price} AC)",
                callback_data=f"adm_gi_{target_id}_{row_id}",
            ))

    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="admin_noop"))
    return kb

cancel_flow           = {}   # uid -> {match_key, chat_id, thread_id, msg_id}
ticket_flow           = {}   # uid -> {step, match_code, reason, evidence_file_id, accused_id}
creator_flow          = {}   # uid -> {step, ...}

# ==================== КОНФИГ ПРИВАТОК ====================
PRIVATE_CONFIG = {
    "darling": {"table": "players", "display": "StandDarling", "emoji": "⚡", "matches_table": "darling_matches"},
}

# ==================== ЛОББИ: размеры по режиму ====================
def _lobby_max_size(league: str) -> int:
    """Максимальное количество игроков в лобби для данной лиги."""
    return 4 if league == "2v2" else 10

def _lobby_team_size(league: str) -> int:
    """Размер одной команды."""
    return 2 if league == "2v2" else 5

user_private = {}  # uid -> "darling"

# ==================== ТОВАРЫ МАГАЗИНА ====================
SHOP_ITEMS_DEFAULT = [
    ("Рамка Gold",             "Золотая рамка профиля",      "decor", 300,  "frame"),
    ("Рамка Diamond",          "Алмазная рамка профиля",     "decor", 600,  "frame"),
    ("Рамка Elite",            "Элитная рамка профиля",      "decor", 150,  "frame"),
    ("Рамка Blue Lock",        "Квадратная рамка в стиле Blue Lock с аниме-глазами", "decor", 800, "frame"),
    ("Стикер 🔥",              "Огненный стикер",            "decor", 50,   "sticker"),
    ("Стикер 💀",              "Стикер черепа",              "decor", 50,   "sticker"),
    ("Стикер ⚡",              "Стикер молнии",              "decor", 50,   "sticker"),
    ("Анимация Победа",        "Анимация при победе",        "decor", 400,  "animation"),
    ("Анимация Убийство",      "Анимация при убийстве",      "decor", 400,  "animation"),
    ("Баннер Gold",            "Золотой баннер профиля",     "decor", 400,  "banner"),
    ("Баннер Diamond",         "Алмазный баннер профиля",    "decor", 700,  "banner"),
    ("Баннер Elite",           "Элитный баннер профиля",     "decor", 250,  "banner"),
    ("Баннер Blue Lock",       "Баннер Blue Lock: белый хедер с аниме-глазами и слэшем", "decor", 900, "banner"),
    ("Фон Blue Lock",          "Геометрический фон карточки в стиле Blue Lock", "decor", 600, "background"),
    ("Premium статус",         "30 дней Premium: x1.5 монет, значок 👑", "goods", 1000, "premium"),
    ("x2 монеты",              "Удвоение монет за 7 дней",   "goods", 300,  "x2coins"),
    ("Снятие варна",           "Снять 1 предупреждение",     "goods", 150,  "unwarn"),
    ("Смена ника",             "Изменить ник в боте",        "goods", 10,   "rename"),
    ("Quals доступ",           "Постоянный доступ к QUALS",  "goods", 1500, "quals"),
]

CATEGORY_NAMES = {"decor": "🖼 Декор", "goods": "📦 Товары"}
CATEGORY_ICONS = {"decor": "🖼", "goods": "📦"}

COIN_PACKAGES = [
    ("Стартовый",   200,   40,   "40 ⭐"),
    ("Оптимальный", 600,   100,  "100 ⭐"),
    ("Выгодный",    2000,  300,  "300 ⭐"),
    ("Мега",        5000,  750,  "750 ⭐"),
    ("Элита",       70000, 1200, "1200 ⭐"),
]

NUMBER_EMOJI = ["①","②","③","④","⑤","⑥","⑦","⑧","⑨","⑩"]

def get_faceit_level(elo: int) -> int:
    if   elo < 801:  return 1
    elif elo < 951:  return 2
    elif elo < 1101: return 3
    elif elo < 1251: return 4
    elif elo < 1401: return 5
    elif elo < 1551: return 6
    elif elo < 1701: return 7
    elif elo < 1851: return 8
    elif elo < 2001: return 9
    else:            return 10

def elo_bar(elo: int, lvl: int) -> str:
    thresholds = [0, 801, 951, 1101, 1251, 1401, 1551, 1701, 1851, 2001, 3000]
    lo = thresholds[max(0, lvl - 1)]
    hi = thresholds[min(lvl, len(thresholds) - 1)]
    pct = (elo - lo) / (hi - lo) if hi > lo else 1.0
    pct = max(0.0, min(1.0, pct))
    filled = round(pct * 12)
    bar = "█" * filled + "░" * (12 - filled)
    return f"[{bar}] {round(pct * 100)}%"


# ==================== ПОДКЛЮЧЕНИЕ К БД ====================
def _db():
    url = DATABASE_URL
    if url and url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    last_err = None
    for _attempt in range(3):
        try:
            return psycopg2.connect(url, connect_timeout=15)
        except psycopg2.OperationalError as e:
            last_err = e
            if "SSL" in str(e) or "connection" in str(e).lower():
                time.sleep(1)
                continue
            raise
    raise last_err


def _add_column_if_missing(table, col, definition):
    conn = _db()
    cur = conn.cursor()
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
        conn.commit()
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()
    except Exception as e:
        conn.rollback()
        print(f"[_add_column_if_missing] {table}.{col}: {e}")
    finally:
        conn.close()


def init_db():
    conn = _db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            game_id TEXT,
            device TEXT,
            elo INTEGER DEFAULT 1000,
            coins INTEGER DEFAULT 100,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            kills INTEGER DEFAULT 0,
            deaths INTEGER DEFAULT 0,
            assists INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            registered INTEGER DEFAULT 0,
            is_bot INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            warns INTEGER DEFAULT 0,
            quals_access INTEGER DEFAULT 0,
            is_game_reg INTEGER DEFAULT 0,
            is_muted INTEGER DEFAULT 0,
            mute_until BIGINT DEFAULT 0,
            is_on_check INTEGER DEFAULT 0,
            check_admin_id BIGINT DEFAULT 0,
            tg_username TEXT DEFAULT ''
        )
    """)
    conn.commit()

    for col, definition in [
        ("is_banned",      "INTEGER DEFAULT 0"),
        ("warns",          "INTEGER DEFAULT 0"),
        ("quals_access",   "INTEGER DEFAULT 0"),
        ("is_game_reg",    "INTEGER DEFAULT 0"),
        ("is_muted",       "INTEGER DEFAULT 0"),
        ("mute_until",     "BIGINT DEFAULT 0"),
        ("is_on_check",    "INTEGER DEFAULT 0"),
        ("check_admin_id", "BIGINT DEFAULT 0"),
        ("tg_username",    "TEXT DEFAULT ''"),
        ("ban_reason",     "TEXT DEFAULT ''"),
        ("ban_until",      "BIGINT DEFAULT 0"),
        ("quals_wins",     "INTEGER DEFAULT 0"),
        ("quals_losses",   "INTEGER DEFAULT 0"),
        ("quals_kills",    "INTEGER DEFAULT 0"),
        ("quals_deaths",   "INTEGER DEFAULT 0"),
        ("quals_assists",  "INTEGER DEFAULT 0"),
        ("quals_elo",      "INTEGER DEFAULT 1000"),
        ("mvp_count",      "INTEGER DEFAULT 0"),
        ("premium_until",  "BIGINT DEFAULT 0"),
        ("quals_until",    "BIGINT DEFAULT 0"),
        ("duo_elo",        "INTEGER DEFAULT 1000"),
        ("duo_wins",       "INTEGER DEFAULT 0"),
        ("duo_losses",     "INTEGER DEFAULT 0"),
        ("duo_kills",      "INTEGER DEFAULT 0"),
        ("duo_deaths",     "INTEGER DEFAULT 0"),
        ("duo_assists",    "INTEGER DEFAULT 0"),
    ]:
        _add_column_if_missing("players", col, definition)

    # Таблица для хранения выбранной приватки пользователя между перезапусками
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_private_settings (
            user_id BIGINT PRIMARY KEY,
            private_key TEXT DEFAULT 'darling'
        )
    """)
    conn.commit()

    for admin_uid in ADMIN_IDS_LIST:
        cur.execute(
            "INSERT INTO players (user_id, username, registered, is_admin) VALUES (%s, 'Admin', 1, 1) ON CONFLICT (user_id) DO NOTHING",
            (admin_uid,),
        )
        cur.execute("UPDATE players SET is_admin=1 WHERE user_id=%s", (admin_uid,))
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM players WHERE is_bot=1")
    if cur.fetchone()[0] == 0:
        for i in range(1, 21):
            bot_id = 1000000000 + i
            cur.execute(
                "INSERT INTO players (user_id, username, game_id, device, registered, is_bot, elo) VALUES (%s, %s, %s, %s, 1, 1, 1000) ON CONFLICT (user_id) DO NOTHING",
                (bot_id, f"Bot_{i}", str(500000000 + i), "PC" if i % 2 == 0 else "MOBILE"),
            )
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS shop_items (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            deACription TEXT,
            category TEXT NOT NULL,
            price INTEGER NOT NULL,
            item_type TEXT NOT NULL,
            is_active INTEGER DEFAULT 1
        )
    """)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM shop_items")
    if cur.fetchone()[0] == 0:
        for row in SHOP_ITEMS_DEFAULT:
            cur.execute(
                "INSERT INTO shop_items (name, deACription, category, price, item_type) VALUES (%s, %s, %s, %s, %s)",
                row,
            )
    else:
        price_updates = [
            (1000, "premium"),
            (300,  "x2coins"),
            (150,  "unwarn"),
            (10,   "rename"),
            (1500, "quals"),
        ]
        for price, item_type in price_updates:
            cur.execute("UPDATE shop_items SET price=%s WHERE item_type=%s", (price, item_type))
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            item_id INTEGER NOT NULL,
            purchased_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
            is_activated INTEGER DEFAULT 0,
            activated_at BIGINT DEFAULT NULL,
            FOREIGN KEY (user_id) REFERENCES players(user_id),
            FOREIGN KEY (item_id) REFERENCES shop_items(id)
        )
    """)
    conn.commit()

    for col, definition in [
        ("is_activated", "INTEGER DEFAULT 0"),
        ("activated_at", "BIGINT DEFAULT NULL"),
    ]:
        _add_column_if_missing("inventory", col, definition)

    _add_column_if_missing("players", "active_frame",      "TEXT DEFAULT NULL")
    _add_column_if_missing("players", "active_banner",     "TEXT DEFAULT NULL")
    _add_column_if_missing("players", "active_background", "TEXT DEFAULT NULL")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS match_counter (
            id INTEGER PRIMARY KEY,
            value INTEGER DEFAULT 0
        )
    """)
    cur.execute("INSERT INTO match_counter (id, value) VALUES (1, 0) ON CONFLICT (id) DO NOTHING")
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id SERIAL PRIMARY KEY,
            match_id INTEGER NOT NULL,
            league TEXT,
            device TEXT,
            map_name TEXT,
            winner TEXT,
            ACore_w INTEGER,
            ACore_l INTEGER,
            finished_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
            players_json TEXT
        )
    """)
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            id SERIAL PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            reward_type TEXT NOT NULL,
            reward_value INTEGER DEFAULT 0,
            max_uses INTEGER DEFAULT 1,
            uses INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
            reward_days INTEGER DEFAULT 30,
            rewards_json TEXT DEFAULT ''
        )
    """)
    _add_column_if_missing("promo_codes", "reward_days",   "INTEGER DEFAULT 30")
    _add_column_if_missing("promo_codes", "rewards_json",  "TEXT DEFAULT ''")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS promo_uses (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            code TEXT NOT NULL
        )
    """)
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_logs (
            id SERIAL PRIMARY KEY,
            admin_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            target_id BIGINT DEFAULT NULL,
            details TEXT DEFAULT '',
            created_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
        )
    """)
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_restrictions (
            id SERIAL PRIMARY KEY,
            admin_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            UNIQUE(admin_id, action)
        )
    """)
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY,
            ticket_code TEXT UNIQUE NOT NULL,
            user_id BIGINT NOT NULL,
            match_code TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            evidence_file_id TEXT DEFAULT '',
            accused_id BIGINT DEFAULT NULL,
            accused_name TEXT DEFAULT '',
            status TEXT DEFAULT 'open',
            created_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
            closed_by BIGINT DEFAULT NULL,
            close_reason TEXT DEFAULT ''
        )
    """)
    conn.commit()

    _add_column_if_missing("matches", "match_code",      "TEXT DEFAULT ''")
    _add_column_if_missing("matches", "status",          "TEXT DEFAULT 'registered'")
    _add_column_if_missing("matches", "cancel_reason",   "TEXT DEFAULT ''")
    _add_column_if_missing("matches", "started_at",      "BIGINT DEFAULT 0")
    _add_column_if_missing("matches", "private_key",     "TEXT DEFAULT 'darling'")
    _add_column_if_missing("matches", "admin_thread_id", "BIGINT DEFAULT NULL")
    _add_column_if_missing("matches", "admin_msg_id",    "BIGINT DEFAULT NULL")

    # unregistered_matches — колонки которых может не быть в старой таблице
    _add_column_if_missing("unregistered_matches", "team_ct_json",      "TEXT DEFAULT '[]'")
    _add_column_if_missing("unregistered_matches", "team_t_json",       "TEXT DEFAULT '[]'")
    _add_column_if_missing("unregistered_matches", "host_game_id",      "TEXT DEFAULT ''")
    _add_column_if_missing("unregistered_matches", "ACreenshots_count", "INTEGER DEFAULT 0")
    _add_column_if_missing("unregistered_matches", "started_at",        "BIGINT DEFAULT 0")

    # Синяя галочка — верификация игрока
    _add_column_if_missing("players", "is_verified", "INTEGER DEFAULT 0")
    # UNIQUE-индекс на match_id для ON CONFLICT
    try:
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS matches_match_id_uq ON matches (match_id)")
        conn.commit()
    except Exception:
        conn.rollback()

    # Per-private match tables: darling_matches, fade_matches, lite_matches
    for _priv_key, _priv_cfg in PRIVATE_CONFIG.items():
        _mt = _priv_cfg["matches_table"]
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_mt} (
                id SERIAL PRIMARY KEY,
                match_id INTEGER NOT NULL,
                match_code TEXT DEFAULT '',
                league TEXT DEFAULT '',
                device TEXT DEFAULT '',
                map_name TEXT DEFAULT '',
                winner TEXT DEFAULT '',
                ACore_w INTEGER DEFAULT 0,
                ACore_l INTEGER DEFAULT 0,
                status TEXT DEFAULT 'registered',
                cancel_reason TEXT DEFAULT '',
                started_at BIGINT DEFAULT 0,
                players_json TEXT DEFAULT '[]',
                finished_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
            )
        """)
        conn.commit()
        try:
            cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {_mt}_match_id_uq ON {_mt} (match_id)")
            conn.commit()
        except Exception:
            conn.rollback()

    # ===== МАТЧИ С ТРЕМЯ СТАТУСАМИ =====
    # active = матч идёт, registered = зарегистрирован, cancelled = отменён
    cur.execute("""
        DO $$ BEGIN
            CREATE TYPE match_status_v2 AS ENUM ('active', 'registered', 'cancelled');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS matches_tracked (
            id SERIAL PRIMARY KEY,
            match_code TEXT NOT NULL UNIQUE,
            league TEXT,
            device TEXT,
            map_name TEXT,
            status match_status_v2 NOT NULL DEFAULT 'active',
            team1_json TEXT,
            team2_json TEXT,
            winner_id BIGINT DEFAULT NULL,
            ACore1 INTEGER DEFAULT NULL,
            ACore2 INTEGER DEFAULT NULL,
            cancel_reason TEXT DEFAULT NULL,
            players_json TEXT,
            created_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
            registered_at BIGINT DEFAULT NULL,
            cancelled_at BIGINT DEFAULT NULL,
            finished_at BIGINT DEFAULT NULL
        )
    """)
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS unregistered_matches (
            id SERIAL PRIMARY KEY,
            match_id INTEGER NOT NULL UNIQUE,
            match_code TEXT NOT NULL,
            league TEXT DEFAULT '',
            device TEXT DEFAULT '',
            map_name TEXT DEFAULT '',
            players_json TEXT DEFAULT '[]',
            team_ct_json TEXT DEFAULT '[]',
            team_t_json TEXT DEFAULT '[]',
            host_game_id TEXT DEFAULT '',
            ACreenshots_count INTEGER DEFAULT 0,
            started_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
        )
    """)
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS seasons (
            id SERIAL PRIMARY KEY,
            season_number INTEGER NOT NULL,
            name TEXT DEFAULT '',
            started_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
            ended_at BIGINT DEFAULT NULL,
            reset_by BIGINT DEFAULT NULL,
            is_active INTEGER DEFAULT 1
        )
    """)
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS season_player_history (
            id SERIAL PRIMARY KEY,
            season_id INTEGER NOT NULL,
            season_number INTEGER NOT NULL,
            user_id BIGINT NOT NULL,
            username TEXT DEFAULT '',
            elo INTEGER DEFAULT 1000,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            kills INTEGER DEFAULT 0,
            deaths INTEGER DEFAULT 0,
            assists INTEGER DEFAULT 0,
            quals_wins INTEGER DEFAULT 0,
            quals_losses INTEGER DEFAULT 0,
            quals_kills INTEGER DEFAULT 0,
            quals_deaths INTEGER DEFAULT 0,
            quals_assists INTEGER DEFAULT 0,
            quals_elo INTEGER DEFAULT 1000,
            mvp_count INTEGER DEFAULT 0,
            saved_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
        )
    """)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM seasons WHERE is_active=1")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO seasons (season_number, name, is_active) VALUES (1, 'Сезон 1', 1)"
        )
        conn.commit()

    conn.close()
    print("✅ БД инициализирована (PostgreSQL / Supabase).")


# ==================== БД ХЕЛПЕРЫ ====================
def get_setting(key):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM bot_settings WHERE key=%s", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bot_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        (key, str(value))
    )
    conn.commit()
    conn.close()

def load_dynamic_settings():
    """Загружает сохранённые thread ID из БД и обновляет глобальные переменные."""
    global _dynamic_log_thread_id, _dynamic_results_thread_id
    try:
        val = get_setting("log_thread_id")
        if val:
            _dynamic_log_thread_id = int(val)
        val2 = get_setting("results_thread_id")
        if val2:
            _dynamic_results_thread_id = int(val2)
        print(f"✅ Настройки веток загружены: logs={_dynamic_log_thread_id}, results={_dynamic_results_thread_id}")
    except Exception as e:
        print(f"load_dynamic_settings error: {e}")


def restore_active_matches():
    """Восстанавливает активные матчи из БД в running_matches после перезапуска бота."""
    global running_matches, awaiting_ACreenshot
    conn = _db()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT match_id, match_code, league, device, map_name, players_json, started_at, "
            "COALESCE(private_key, 'darling'), admin_thread_id, admin_msg_id, status "
            "FROM matches WHERE status IN ('active', 'registered')"
        )
        rows = cur.fetchall()
        restored = 0
        for row in rows:
            match_id, match_code, league, device, map_name, players_json, started_at, private_key, admin_thread_id, admin_msg_id, db_status = row
            match_key = f"match_{match_id}"

            team_ct, team_t, players = [], [], []
            try:
                players_info = json.loads(players_json or "[]")
                for p in players_info:
                    uid = p.get("user_id")
                    if uid:
                        players.append(uid)
                        if p.get("team") == "ct":
                            team_ct.append(uid)
                        else:
                            team_t.append(uid)
            except Exception:
                pass

            lobby = {
                "match_id":        match_id,
                "match_code":      match_code or "",
                "league":          league or "",
                "device":          device or "",
                "map_name":        map_name or "",
                "status":          db_status or "active",
                "players":         players,
                "team_ct":         team_ct,
                "team_t":          team_t,
                "ACreenshots":     {},
                "ACreenshots_count": 0,
                "reg_taken_by":    None,
                "match_key":       match_key,
                "started_at":      started_at or 0,
                "private":         private_key or "darling",
                "admin_thread_id": admin_thread_id,
                "admin_msg_id":    admin_msg_id,
            }
            running_matches[match_key] = lobby

            if db_status == "active":
                for uid in players:
                    awaiting_ACreenshot[uid] = match_key

            restored += 1

        print(f"♻️ Восстановлено матчей из БД: {restored}")
    except Exception as e:
        print(f"restore_active_matches error: {e}")
    finally:
        conn.close()

# ==================== ХЕЛПЕРЫ ПРИВАТОК ====================

def save_user_private(uid, key):
    """Сохраняет выбранную приватку пользователя в БД."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO user_private_settings (user_id, private_key) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET private_key = EXCLUDED.private_key",
            (uid, key),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[save_user_private] error: {e}")

def load_user_privates():
    """Загружает выбор приватки всех пользователей из БД в оперативную память."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT user_id, private_key FROM user_private_settings")
        rows = cur.fetchall()
        conn.close()
        for uid, key in rows:
            if key in PRIVATE_CONFIG:
                user_private[uid] = key
        print(f"✅ Загружены приватки пользователей: {len(rows)} записей")
    except Exception as e:
        print(f"[load_user_privates] error: {e}")

def get_user_private(uid):
    """Возвращает ключ текущей приватки пользователя."""
    return user_private.get(uid, "darling")

def get_user_table(uid):
    """Возвращает имя таблицы текущей приватки пользователя."""
    return PRIVATE_CONFIG[get_user_private(uid)]["table"]

def get_user_private_display(uid):
    """Возвращает отображаемое название текущей приватки пользователя."""
    cfg = PRIVATE_CONFIG[get_user_private(uid)]
    return f"{cfg['emoji']} {cfg['display']}"


def get_player(user_id):
    """Получает игрока из таблицы players (StandDarling) — используется в admin и общих проверках."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM players WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        conn.close()
        return row
    except Exception as e:
        print(f"[get_player] Ошибка: {e}")
        return None

def get_player_from_table(user_id, table):
    """Получает игрока из указанной таблицы приватки."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {table} WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        conn.close()
        return row
    except Exception:
        return None

def get_current_player(uid):
    """Получает игрока из таблицы текущей приватки пользователя."""
    try:
        table = get_user_table(uid)
        conn = _db()
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {table} WHERE user_id=%s", (uid,))
        row = cur.fetchone()
        conn.close()
        return row
    except Exception as e:
        print(f"[get_current_player] Ошибка: {e}")
        return None

def get_player_in_lobby(uid, lobby):
    """Получает игрока из таблицы приватки лобби/матча (не из user_private)."""
    private_key = lobby.get("private", "darling") if lobby else "darling"
    priv_table = PRIVATE_CONFIG.get(private_key, PRIVATE_CONFIG["darling"])["table"]
    p = get_player_from_table(uid, priv_table)
    return p if p else get_player(uid)

def is_registered(uid):
    p = get_current_player(uid)
    return p is not None and p[12] == 1

def is_admin(uid):
    p = get_player(uid)
    return p is not None and p[11] == 1

def is_creator(uid):
    """Super-admin (creator) — полный доступ поверх админов."""
    return CREATOR_ID != 0 and uid == CREATOR_ID

def is_admin_restricted(uid, action):
    """True если креатор запретил данному админу это действие."""
    if is_creator(uid):
        return False
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM admin_restrictions WHERE admin_id=%s AND action=%s",
            (uid, action),
        )
        row = cur.fetchone()
        conn.close()
        return bool(row)
    except Exception:
        return False

def log_admin_action(admin_id, action, target_id=None, details=""):
    """Записать действие админа в admin_logs."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO admin_logs (admin_id, action, target_id, details) VALUES (%s, %s, %s, %s)",
            (admin_id, action, target_id, str(details)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[log_admin_action] {e}")

def get_user_avatar(uid):
    """Скачивает аватарку пользователя из Telegram. Возвращает bytes или None."""
    try:
        photos = bot.get_user_profile_photos(uid, limit=1)
        if photos and photos.photos and photos.photos[0]:
            file_id   = photos.photos[0][-1].file_id
            file_info = bot.get_file(file_id)
            return bot.download_file(file_info.file_path)
    except Exception as e:
        print(f"[get_user_avatar] uid={uid}: {e}")
    return None

def is_game_reg_check(uid):
    p = get_player(uid)
    return p is not None and (p[11] == 1 or (len(p) > 17 and p[17] == 1))

def is_bot_player(uid):
    p = get_player(uid)
    return p is not None and p[13] == 1

def is_banned_check(uid):
    p = get_current_player(uid)
    return p is not None and len(p) > 14 and p[14] == 1

def is_muted_check(uid):
    p = get_current_player(uid)
    if p is None:
        return False
    if len(p) > 19 and p[18] == 1:
        mute_until = p[19] or 0
        if mute_until > time.time():
            return True
        table = get_user_table(uid)
        conn = _db()
        cur = conn.cursor()
        cur.execute(f"UPDATE {table} SET is_muted=0, mute_until=0 WHERE user_id=%s", (uid,))
        conn.commit()
        conn.close()
    return False

def get_mute_remaining(uid):
    p = get_current_player(uid)
    if p is None or len(p) <= 19:
        return 0
    return max(0, int((p[19] or 0) - time.time()))

def is_on_check_db(uid):
    p = get_current_player(uid)
    return p is not None and len(p) > 20 and p[20] == 1

def is_verified_check(uid):
    """Возвращает True если у игрока есть синяя галочка."""
    p = get_player(uid)
    if p is None:
        return False
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT is_verified FROM players WHERE user_id=%s", (uid,))
        row = cur.fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception:
        return False

def get_check_admin(uid):
    p = get_current_player(uid)
    if p is None or len(p) <= 21:
        return None
    return p[21]

def has_quals_access(uid):
    """True если admin, или quals_access=1 (постоянный), или quals_until > now (временный)."""
    if is_admin(uid):
        return True
    p = get_current_player(uid)
    if p is None:
        return False
    # Постоянный доступ
    if len(p) > 16 and p[16] == 1:
        return True
    # Временный доступ через quals_until
    conn = _db()
    cur = conn.cursor()
    table = get_user_table(uid)
    cur.execute(f"SELECT quals_until FROM {table} WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0] and row[0] > int(time.time()))

def has_active_premium(uid):
    """Возвращает True, если у игрока действует Premium (premium_until > now)."""
    conn = _db()
    cur = conn.cursor()
    table = get_user_table(uid)
    cur.execute(f"SELECT premium_until FROM {table} WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0] and row[0] > int(time.time()))

def register_user(uid, username, game_id, device, tg_username=""):
    table = get_user_table(uid)
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        f"""INSERT INTO {table} (user_id, username, game_id, device, registered, coins, elo, tg_username)
           VALUES (%s, %s, %s, %s, 1, 100, 1000, %s)
           ON CONFLICT (user_id) DO UPDATE SET
               username=EXCLUDED.username,
               game_id=EXCLUDED.game_id,
               device=EXCLUDED.device,
               registered=1,
               tg_username=EXCLUDED.tg_username""",
        (uid, username, game_id, device, tg_username),
    )
    conn.commit()
    conn.close()

def get_user_matches_table(uid):
    """Returns the matches table for the user's current private."""
    priv_key = get_user_private(uid)
    return PRIVATE_CONFIG.get(priv_key, PRIVATE_CONFIG["darling"])["matches_table"]

def update_tg_username(uid, tg_username):
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute("UPDATE players SET tg_username=%s WHERE user_id=%s", (tg_username or "", uid))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[update_tg_username] Ошибка: {e}")

def nick_taken(nick, uid=None, exclude_uid=None):
    table = get_user_table(uid) if uid else "players"
    conn = _db()
    cur = conn.cursor()
    if exclude_uid:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE username=%s AND user_id!=%s AND is_bot=0", (nick, exclude_uid))
    else:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE username=%s AND is_bot=0", (nick,))
    count = cur.fetchone()[0]
    conn.close()
    return count > 0

def game_id_taken(game_id, uid=None, exclude_uid=None):
    table = get_user_table(uid) if uid else "players"
    conn = _db()
    cur = conn.cursor()
    if exclude_uid:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE game_id=%s AND user_id!=%s AND is_bot=0", (game_id, exclude_uid))
    else:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE game_id=%s AND is_bot=0", (game_id,))
    count = cur.fetchone()[0]
    conn.close()
    return count > 0

def get_bots():
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username FROM players WHERE is_bot=1")
    bots = cur.fetchall()
    conn.close()
    return bots

def get_all_players(table="players"):
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT user_id, username, elo, wins, losses, kills, deaths, coins, is_banned, warns
            FROM {table} WHERE is_bot=0 AND registered=1 ORDER BY elo DESC
        """)
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"[get_all_players] Ошибка: {e}")
        return []

def get_quals_players(table="players"):
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT user_id, username, quals_elo, quals_wins, quals_losses, quals_kills, quals_deaths, quals_assists
            FROM {table} WHERE is_bot=0 AND registered=1 AND quals_access=1
            ORDER BY quals_elo DESC
        """)
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"[get_quals_players] Ошибка: {e}")
        return []

def get_duo_players(table="players"):
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT user_id, username, duo_elo, duo_wins, duo_losses, duo_kills, duo_deaths, duo_assists
            FROM {table} WHERE is_bot=0 AND registered=1
            ORDER BY duo_elo DESC
        """)
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"[get_duo_players] Ошибка: {e}")
        return []


def get_player_duo_stats(uid, table="players"):
    conn = _db()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT duo_elo, duo_wins, duo_losses, duo_kills, duo_deaths, duo_assists
            FROM {table} WHERE user_id=%s
        """, (uid,))
        row = cur.fetchone()
    except Exception:
        row = None
    conn.close()
    if not row:
        return None
    deo, dw, dl, dk, dd, da = row
    has_games = (dw or 0) + (dl or 0) > 0
    if not has_games:
        return None
    return {"elo": deo or 1000, "wins": dw or 0, "losses": dl or 0,
            "kills": dk or 0, "deaths": dd or 0, "assists": da or 0}

def get_player_quals_stats(uid, table="players"):
    conn = _db()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT quals_elo, quals_wins, quals_losses, quals_kills, quals_deaths, quals_assists, quals_access
            FROM {table} WHERE user_id=%s
        """, (uid,))
        row = cur.fetchone()
    except Exception:
        row = None
    conn.close()
    if not row:
        return None
    qelo, qw, ql, qk, qd, qa, q_access = row
    has_games  = (qw or 0) + (ql or 0) > 0
    has_access = bool(q_access)
    # Показываем секцию если есть доступ к quals ИЛИ уже есть сыгранные матчи
    if not has_games and not has_access:
        return None
    return {"elo": qelo or 1000, "wins": qw or 0, "losses": ql or 0,
            "kills": qk or 0, "deaths": qd or 0, "assists": qa or 0}

def get_player_by_game_id(game_id):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE game_id=%s AND is_bot=0", (game_id,))
    row = cur.fetchone()
    conn.close()
    return row

def add_coins_to_player(uid, amount, table=None):
    conn = _db()
    cur = conn.cursor()
    target = table or get_user_table(uid)
    cur.execute(f"UPDATE {target} SET coins=coins+%s WHERE user_id=%s", (amount, uid))
    conn.commit()
    conn.close()

def apply_mute(uid, hours=2):
    until = int(time.time()) + hours * 3600
    conn = _db()
    cur = conn.cursor()
    cur.execute("UPDATE players SET is_muted=1, mute_until=%s WHERE user_id=%s", (until, uid))
    conn.commit()
    conn.close()
    return until

def add_warn_to_player(uid):
    conn = _db()
    cur = conn.cursor()
    cur.execute("UPDATE players SET warns=warns+1 WHERE user_id=%s", (uid,))
    cur.execute("SELECT warns FROM players WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else 1

def get_next_match_id():
    conn = _db()
    cur = conn.cursor()
    # Ensure the counter row always exists before incrementing
    cur.execute("INSERT INTO match_counter (id, value) VALUES (1, 0) ON CONFLICT (id) DO NOTHING")
    cur.execute("UPDATE match_counter SET value=value+1 WHERE id=1 RETURNING value")
    row = cur.fetchone()
    if row is None:
        # Failsafe: counter row still missing — seed it now
        cur.execute("INSERT INTO match_counter (id, value) VALUES (1, 1) RETURNING value")
        row = cur.fetchone()
    val = row[0]
    conn.commit()
    conn.close()
    return val

def generate_match_code():
    """Generates a random 7-character alphanumeric code like H71BSY1"""
    chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    return ''.join(random.choices(chars, k=7))

def generate_ticket_code():
    """Generates a random ticket code like TKT-AB3X9"""
    chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    return 'TKT-' + ''.join(random.choices(chars, k=5))


# ==================== СЕЗОНЫ — ХЕЛПЕРЫ ====================

def get_current_season():
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, season_number, name, started_at FROM seasons WHERE is_active=1 ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    conn.close()
    return row  # (id, season_number, name, started_at) or None


def get_all_seasons():
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, season_number, name, started_at, ended_at, is_active FROM seasons ORDER BY season_number DESC"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_season_top(season_id, limit=10):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """SELECT username, elo, wins, losses, kills, deaths, mvp_count
           FROM season_player_history
           WHERE season_id=%s AND username NOT LIKE 'Bot_%%'
           ORDER BY elo DESC LIMIT %s""",
        (season_id, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def reset_season(admin_uid):
    """
    Архивирует статистику всех игроков в season_player_history,
    сбрасывает их ELO/статистику и создаёт новый сезон.
    Возвращает (new_season_number, players_archived).
    """
    conn = _db()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, season_number, name FROM seasons WHERE is_active=1 ORDER BY id DESC LIMIT 1"
        )
        cur_season = cur.fetchone()
        if not cur_season:
            season_id = 1
            season_number = 1
        else:
            season_id, season_number, _ = cur_season

        cur.execute(
            """SELECT user_id, username, elo, wins, losses, kills, deaths, assists,
                      COALESCE(quals_wins,0), COALESCE(quals_losses,0),
                      COALESCE(quals_kills,0), COALESCE(quals_deaths,0),
                      COALESCE(quals_assists,0), COALESCE(quals_elo,1000),
                      COALESCE(mvp_count,0)
               FROM players WHERE is_bot=0"""
        )
        players = cur.fetchall()

        for p in players:
            (uid, uname, elo, wins, losses, kills, deaths, assists,
             qw, ql, qk, qd, qa, qelo, mvp) = p
            cur.execute(
                """INSERT INTO season_player_history
                   (season_id, season_number, user_id, username, elo, wins, losses,
                    kills, deaths, assists, quals_wins, quals_losses, quals_kills,
                    quals_deaths, quals_assists, quals_elo, mvp_count)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (season_id, season_number, uid, uname or '', elo, wins, losses,
                 kills, deaths, assists, qw, ql, qk, qd, qa, qelo, mvp)
            )

        now_ts = int(time.time())
        cur.execute(
            "UPDATE seasons SET is_active=0, ended_at=%s, reset_by=%s WHERE id=%s",
            (now_ts, admin_uid, season_id)
        )

        new_season_number = season_number + 1
        new_name = f"Сезон {new_season_number}"
        cur.execute(
            "INSERT INTO seasons (season_number, name, is_active, started_at) VALUES (%s, %s, 1, %s)",
            (new_season_number, new_name, now_ts)
        )

        cur.execute(
            """UPDATE players SET
               elo=1000, wins=0, losses=0, kills=0, deaths=0, assists=0,
               quals_wins=0, quals_losses=0, quals_kills=0, quals_deaths=0,
               quals_assists=0, quals_elo=1000, mvp_count=0
               WHERE is_bot=0"""
        )
        conn.commit()
        conn.close()
        return new_season_number, len(players)
    except Exception as e:
        conn.rollback()
        conn.close()
        raise e

def save_match_start(lobby):
    """Сохраняет матч в БД при старте со статусом 'active'."""
    players_info = []
    for uid in list(lobby.get("team_ct", [])) + list(lobby.get("team_t", [])):
        if is_bot_player(uid):
            continue
        p = get_player_in_lobby(uid, lobby)
        players_info.append({
            "user_id": uid,
            "name": p[1] if p else str(uid),
            "team": "ct" if uid in lobby.get("team_ct", []) else "t",
        })
    players_json_str = json.dumps(players_info, ensure_ascii=False)
    team_ct_json_str = json.dumps(lobby.get("team_ct", []), ensure_ascii=False)
    team_t_json_str  = json.dumps(lobby.get("team_t",  []), ensure_ascii=False)
    match_id    = lobby.get("match_id", 0)
    match_code  = lobby.get("match_code", "")
    league      = lobby.get("league", "")
    device      = lobby.get("device", "")
    map_name    = lobby.get("map_name", "")
    private_key = lobby.get("private", "darling")
    admin_thread_id = lobby.get("admin_thread_id")
    admin_msg_id    = lobby.get("admin_msg_id")
    host_game_id    = lobby.get("host_game_id", "")
    now = int(time.time())

    conn = _db()
    cur = conn.cursor()
    try:
        # ── matches ──────────────────────────────────────────────────────────
        cur.execute("SELECT id FROM matches WHERE match_id=%s", (match_id,))
        if cur.fetchone():
            cur.execute(
                """UPDATE matches SET
                       match_code=%s, league=%s, device=%s, map_name=%s,
                       status='active', players_json=%s, started_at=%s,
                       winner='', ACore_w=0, ACore_l=0,
                       private_key=%s, admin_thread_id=%s, admin_msg_id=%s
                   WHERE match_id=%s""",
                (match_code, league, device, map_name,
                 players_json_str, now,
                 private_key, admin_thread_id, admin_msg_id,
                 match_id),
            )
        else:
            cur.execute(
                """INSERT INTO matches
                   (match_id, match_code, league, device, map_name, status, players_json,
                    started_at, winner, ACore_w, ACore_l, private_key, admin_thread_id, admin_msg_id)
                   VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, '', 0, 0, %s, %s, %s)""",
                (match_id, match_code, league, device, map_name,
                 players_json_str, now,
                 private_key, admin_thread_id, admin_msg_id),
            )
        conn.commit()

        # ── unregistered_matches ─────────────────────────────────────────────
        try:
            cur.execute("SELECT id FROM unregistered_matches WHERE match_id=%s", (match_id,))
            if not cur.fetchone():
                cur.execute(
                    """INSERT INTO unregistered_matches
                       (match_id, match_code, league, device, map_name, players_json,
                        team_ct_json, team_t_json, host_game_id, ACreenshots_count, started_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s)""",
                    (match_id, match_code, league, device, map_name,
                     players_json_str, team_ct_json_str, team_t_json_str,
                     host_game_id, now),
                )
            conn.commit()
        except Exception as e2:
            conn.rollback()
            print(f"[save_match_start] unregistered_matches error: {e2}")

        # ── приватка-специфичная таблица ─────────────────────────────────────
        try:
            _pmt = PRIVATE_CONFIG.get(private_key, PRIVATE_CONFIG["darling"])["matches_table"]
            cur.execute(f"SELECT id FROM {_pmt} WHERE match_id=%s", (match_id,))
            if not cur.fetchone():
                cur.execute(
                    f"""INSERT INTO {_pmt}
                       (match_id, match_code, league, device, map_name, status,
                        players_json, started_at, winner, ACore_w, ACore_l)
                       VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, '', 0, 0)""",
                    (match_id, match_code, league, device, map_name,
                     players_json_str, now),
                )
            conn.commit()
        except Exception as e3:
            conn.rollback()
            print(f"[save_match_start] {_pmt} error: {e3}")

    except Exception as e:
        print(f"[save_match_start] ГЛАВНАЯ ОШИБКА: {e}")
        import traceback; traceback.print_exc()
        conn.rollback()
    finally:
        conn.close()


def rollback_match_stats(lobby):
    """Откатывает статистику ранее зарегистрированного матча (для перерегистрации)."""
    all_stats = lobby.get("all_stats")
    if not all_stats:
        return
    priv_table = lobby.get("priv_table", "players")
    league     = lobby.get("league", "default")
    is_2v2     = (league == "2v2")
    is_quals   = (league == "quals")
    mvp_uid    = lobby.get("mvp_uid")
    conn = _db()
    cur  = conn.cursor()
    try:
        for uid, s in all_stats.items():
            won          = s["won"]
            elo_change   = s["elo_change"]
            kills        = s["kills"]
            deaths       = s["deaths"]
            assists      = s["assists"]
            coins_reward = s["coins_reward"]
            if is_2v2:
                if won:
                    cur.execute(
                        f"UPDATE {priv_table} SET duo_wins=GREATEST(0,duo_wins-1),"
                        "duo_elo=GREATEST(0,duo_elo-%s),"
                        "duo_kills=GREATEST(0,duo_kills-%s),duo_deaths=GREATEST(0,duo_deaths-%s),"
                        "duo_assists=GREATEST(0,duo_assists-%s),coins=GREATEST(0,coins-%s) WHERE user_id=%s",
                        (elo_change, kills, deaths, assists, coins_reward, uid),
                    )
                else:
                    cur.execute(
                        f"UPDATE {priv_table} SET duo_losses=GREATEST(0,duo_losses-1),"
                        "duo_elo=GREATEST(0,duo_elo-%s),"
                        "duo_kills=GREATEST(0,duo_kills-%s),duo_deaths=GREATEST(0,duo_deaths-%s),"
                        "duo_assists=GREATEST(0,duo_assists-%s),coins=GREATEST(0,coins-%s) WHERE user_id=%s",
                        (elo_change, kills, deaths, assists, coins_reward, uid),
                    )
            elif is_quals:
                if won:
                    cur.execute(
                        f"UPDATE {priv_table} SET quals_wins=GREATEST(0,quals_wins-1),"
                        "quals_elo=GREATEST(0,quals_elo-%s),"
                        "quals_kills=GREATEST(0,quals_kills-%s),quals_deaths=GREATEST(0,quals_deaths-%s),"
                        "quals_assists=GREATEST(0,quals_assists-%s),coins=GREATEST(0,coins-%s) WHERE user_id=%s",
                        (elo_change, kills, deaths, assists, coins_reward, uid),
                    )
                else:
                    cur.execute(
                        f"UPDATE {priv_table} SET quals_losses=GREATEST(0,quals_losses-1),"
                        "quals_elo=GREATEST(0,quals_elo-%s),"
                        "quals_kills=GREATEST(0,quals_kills-%s),quals_deaths=GREATEST(0,quals_deaths-%s),"
                        "quals_assists=GREATEST(0,quals_assists-%s),coins=GREATEST(0,coins-%s) WHERE user_id=%s",
                        (elo_change, kills, deaths, assists, coins_reward, uid),
                    )
            else:
                if won:
                    cur.execute(
                        f"UPDATE {priv_table} SET wins=GREATEST(0,wins-1),"
                        "elo=GREATEST(0,elo-%s),"
                        "kills=GREATEST(0,kills-%s),deaths=GREATEST(0,deaths-%s),"
                        "assists=GREATEST(0,assists-%s),coins=GREATEST(0,coins-%s) WHERE user_id=%s",
                        (elo_change, kills, deaths, assists, coins_reward, uid),
                    )
                else:
                    cur.execute(
                        f"UPDATE {priv_table} SET losses=GREATEST(0,losses-1),"
                        "elo=GREATEST(0,elo-%s),"
                        "kills=GREATEST(0,kills-%s),deaths=GREATEST(0,deaths-%s),"
                        "assists=GREATEST(0,assists-%s),coins=GREATEST(0,coins-%s) WHERE user_id=%s",
                        (elo_change, kills, deaths, assists, coins_reward, uid),
                    )
        if mvp_uid:
            cur.execute(
                f"UPDATE {priv_table} SET mvp_count=GREATEST(0,mvp_count-1) WHERE user_id=%s",
                (mvp_uid,),
            )
        conn.commit()
        lobby.pop("all_stats", None)
        lobby.pop("mvp_uid",   None)
        lobby.pop("priv_table_saved", None)
    except Exception as e:
        conn.rollback()
        print(f"[rollback_match_stats] Ошибка: {e}")
    finally:
        conn.close()


def save_match_to_history(lobby, data, all_stats):
    players_info = []
    for uid, s in all_stats.items():
        p = get_player_in_lobby(uid, lobby)
        players_info.append({
            "user_id": uid,
            "name": p[1] if p else str(uid),
            "kills": s["kills"],
            "deaths": s["deaths"],
            "assists": s["assists"],
            "won": s["won"],
        })
    players_json_str = json.dumps(players_info, ensure_ascii=False)
    match_id   = lobby.get("match_id", 0)
    match_code = lobby.get("match_code", "")
    league     = lobby.get("league", "")
    device     = lobby.get("device", "")
    map_name   = lobby.get("map_name", "")
    winner     = data.get("winner", "")
    score_w    = data.get("ACore_w", 0)
    score_l    = data.get("ACore_l", 0)
    now        = int(time.time())

    # ── 1. Обновляем главную таблицу matches (отдельная транзакция) ──────────
    # Важно: коммитим ДО работы с приваточной таблицей, иначе ошибка в
    # darling_matches откатит и этот UPDATE, оставив статус 'active' в БД.
    conn = None
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO matches
               (match_id, match_code, league, device, map_name, winner, ACore_w, ACore_l,
                players_json, status, started_at, finished_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'registered', %s, %s)
               ON CONFLICT (match_id) DO UPDATE SET
                   winner       = EXCLUDED.winner,
                   ACore_w      = EXCLUDED.ACore_w,
                   ACore_l      = EXCLUDED.ACore_l,
                   players_json = EXCLUDED.players_json,
                   status       = 'registered',
                   finished_at  = EXCLUDED.finished_at""",
            (match_id, match_code, league, device, map_name, winner,
             score_w, score_l, players_json_str, now, now),
        )
        try:
            cur.execute("DELETE FROM unregistered_matches WHERE match_id=%s", (match_id,))
        except Exception:
            pass
        conn.commit()
    except Exception as e:
        print(f"[save_match_to_history] matches UPDATE ERROR: {e}")
        import traceback; traceback.print_exc()
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    # ── 2. Обновляем таблицу конкретной приватки (отдельная транзакция) ──────
    # Если эта часть падает — главная таблица matches уже закоммичена выше,
    # матч не потеряется после рестарта.
    conn2 = None
    try:
        conn2 = _db()
        cur2  = conn2.cursor()
        _pmt  = PRIVATE_CONFIG.get(lobby.get("private", "darling"), PRIVATE_CONFIG["darling"])["matches_table"]
        cur2.execute(
            f"""INSERT INTO {_pmt}
               (match_id, match_code, league, device, map_name, winner, ACore_w, ACore_l,
                players_json, status, started_at, finished_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'registered', %s, %s)
               ON CONFLICT (match_id) DO UPDATE SET
                   winner       = EXCLUDED.winner,
                   ACore_w      = EXCLUDED.ACore_w,
                   ACore_l      = EXCLUDED.ACore_l,
                   players_json = EXCLUDED.players_json,
                   status       = 'registered',
                   finished_at  = EXCLUDED.finished_at""",
            (match_id, match_code, league, device, map_name, winner,
             score_w, score_l, players_json_str, now, now),
        )
        conn2.commit()
    except Exception as e2:
        print(f"[save_match_to_history] {_pmt} UPDATE ERROR: {e2}")
        try:
            if conn2:
                conn2.rollback()
        except Exception:
            pass
    finally:
        try:
            if conn2:
                conn2.close()
        except Exception:
            pass


def save_match_cancelled(lobby, reason=""):
    """Обновляет/сохраняет матч в БД со статусом 'cancelled'."""
    players_info = []
    for uid in list(lobby.get("team_ct", [])) + list(lobby.get("team_t", [])):
        if is_bot_player(uid):
            continue
        p = get_player_in_lobby(uid, lobby)
        players_info.append({
            "user_id": uid,
            "name": p[1] if p else str(uid),
            "team": "ct" if uid in lobby.get("team_ct", []) else "t",
        })
    players_json_str = json.dumps(players_info, ensure_ascii=False)
    match_id   = lobby.get("match_id", 0)
    match_code = lobby.get("match_code", "")
    league     = lobby.get("league", "")
    device     = lobby.get("device", "")
    map_name   = lobby.get("map_name", "")
    now        = int(time.time())

    # ── 1. Главная таблица matches (отдельная транзакция) ────────────────────
    conn = None
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO matches
               (match_id, match_code, league, device, map_name, status, cancel_reason,
                players_json, winner, ACore_w, ACore_l, started_at)
               VALUES (%s, %s, %s, %s, %s, 'cancelled', %s, %s, '', 0, 0, %s)
               ON CONFLICT (match_id) DO UPDATE SET
                   status        = 'cancelled',
                   cancel_reason = EXCLUDED.cancel_reason""",
            (match_id, match_code, league, device, map_name, reason, players_json_str, now),
        )
        try:
            cur.execute("DELETE FROM unregistered_matches WHERE match_id=%s", (match_id,))
        except Exception:
            pass
        conn.commit()
    except Exception as e:
        print(f"[save_match_cancelled] matches UPDATE ERROR: {e}")
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    # ── 2. Таблица приватки (отдельная транзакция) ───────────────────────────
    conn2 = None
    try:
        conn2 = _db()
        cur2  = conn2.cursor()
        _pmt  = PRIVATE_CONFIG.get(lobby.get("private", "darling"), PRIVATE_CONFIG["darling"])["matches_table"]
        cur2.execute(
            f"""INSERT INTO {_pmt}
               (match_id, match_code, league, device, map_name, status, cancel_reason,
                players_json, winner, ACore_w, ACore_l, started_at)
               VALUES (%s, %s, %s, %s, %s, 'cancelled', %s, %s, '', 0, 0, %s)
               ON CONFLICT (match_id) DO UPDATE SET
                   status = 'cancelled', cancel_reason = EXCLUDED.cancel_reason""",
            (match_id, match_code, league, device, map_name, reason, players_json_str, now),
        )
        conn2.commit()
    except Exception as e2:
        print(f"[save_match_cancelled] {_pmt} UPDATE ERROR: {e2}")
        try:
            if conn2:
                conn2.rollback()
        except Exception:
            pass
    finally:
        try:
            if conn2:
                conn2.close()
        except Exception:
            pass


# ==================== ТИКЕТЫ (БД хелперы) ====================
def create_ticket(user_id, match_code, reason, evidence_file_id, accused_id, accused_name):
    code = generate_ticket_code()
    conn = _db()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO tickets (ticket_code, user_id, match_code, reason, evidence_file_id, accused_id, accused_name)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (code, user_id, match_code, reason, evidence_file_id or '', accused_id, accused_name or ''),
        )
        conn.commit()
        conn.close()
        return code
    except Exception as e:
        print(f"create_ticket error: {e}")
        conn.rollback()
        conn.close()
        return None

def get_open_tickets():
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, ticket_code, user_id, match_code, reason, accused_name, status, created_at FROM tickets WHERE status='open' ORDER BY created_at DESC"
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def close_ticket(ticket_code, admin_id, close_reason, new_status):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE tickets SET status=%s, closed_by=%s, close_reason=%s WHERE ticket_code=%s",
        (new_status, admin_id, close_reason, ticket_code),
    )
    conn.commit()
    t = None
    cur.execute("SELECT user_id, match_code, reason FROM tickets WHERE ticket_code=%s", (ticket_code,))
    t = cur.fetchone()
    conn.close()
    return t

def get_match_history(limit=10):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT match_id, league, device, map_name, winner, ACore_w, ACore_l, finished_at FROM matches ORDER BY finished_at DESC LIMIT %s",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_player_map_stats(user_id, matches_table="matches"):
    """Returns list of {"map": str, "wr": float, "kd": float} for each map the player played in current season."""
    season_start = _get_season_start_ts()
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        f"SELECT map_name, players_json FROM {matches_table} WHERE status='registered' AND players_json IS NOT NULL AND finished_at >= %s",
        (season_start,)
    )
    rows = cur.fetchall()
    conn.close()

    from collections import defaultdict
    stats = defaultdict(lambda: {"wins": 0, "total": 0, "kills": 0, "deaths": 0})
    for map_name, pj in rows:
        if not map_name:
            continue
        try:
            players = json.loads(pj or "[]")
        except Exception:
            continue
        for p in players:
            if p.get("user_id") == user_id:
                k = stats[map_name]
                k["total"]  += 1
                k["wins"]   += 1 if p.get("won") else 0
                k["kills"]  += p.get("kills",  0)
                k["deaths"] += p.get("deaths", 1)

    result = []
    for map_name, s in stats.items():
        wr = s["wins"] / s["total"] if s["total"] > 0 else 0.0
        kd = round(s["kills"] / max(s["deaths"], 1), 2)
        result.append({
            "map":    map_name,
            "wins":   s["wins"],
            "losses": s["total"] - s["wins"],
            "wr":     wr,
            "kd":     kd,
        })

    # Sort by matches played deAC, pad with zeroes for default maps
    for default_map in MAPS:
        if not any(r["map"] == default_map for r in result):
            result.append({"map": default_map, "wr": 0.0, "kd": 0.0})

    result.sort(key=lambda r: r["wr"], reverse=True)
    return result[:5]


def _get_season_start_ts():
    """Returns the started_at timestamp of the current active season, or 0 if none."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT started_at FROM seasons WHERE is_active=1 ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0


def get_player_recent_matches(user_id, limit=5, matches_table="matches"):
    """Returns list of booleans (True=win) for last N Default matches of the player in current season."""
    season_start = _get_season_start_ts()
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        f"SELECT players_json FROM {matches_table} WHERE status='registered' AND (league='default' OR league IS NULL) AND players_json IS NOT NULL AND finished_at >= %s ORDER BY finished_at DESC LIMIT 50",
        (season_start,)
    )
    rows = cur.fetchall()
    conn.close()

    recent = []
    for (pj,) in rows:
        if len(recent) >= limit:
            break
        try:
            players = json.loads(pj or "[]")
        except Exception:
            continue
        for p in players:
            if p.get("user_id") == user_id:
                recent.append(bool(p.get("won")))
                break
    return recent


def get_player_quals_recent_matches(user_id, limit=5, matches_table="matches"):
    """Returns list of booleans (True=win) for last N Quals matches of the player in current season."""
    season_start = _get_season_start_ts()
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        f"SELECT players_json FROM {matches_table} WHERE status='registered' AND league='quals' AND players_json IS NOT NULL AND finished_at >= %s ORDER BY finished_at DESC LIMIT 50",
        (season_start,)
    )
    rows = cur.fetchall()
    conn.close()

    recent = []
    for (pj,) in rows:
        if len(recent) >= limit:
            break
        try:
            players = json.loads(pj or "[]")
        except Exception:
            continue
        for p in players:
            if p.get("user_id") == user_id:
                recent.append(bool(p.get("won")))
                break
    return recent


def get_player_duo_recent_matches(user_id, limit=5, matches_table="matches"):
    """Returns list of booleans (True=win) for last N 2v2 matches of the player in current season."""
    season_start = _get_season_start_ts()
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        f"SELECT players_json FROM {matches_table} WHERE status='registered' AND league='2v2' AND players_json IS NOT NULL AND finished_at >= %s ORDER BY finished_at DESC LIMIT 50",
        (season_start,)
    )
    rows = cur.fetchall()
    conn.close()

    recent = []
    for (pj,) in rows:
        if len(recent) >= limit:
            break
        try:
            players = json.loads(pj or "[]")
        except Exception:
            continue
        for p in players:
            if p.get("user_id") == user_id:
                recent.append(bool(p.get("won")))
                break
    return recent


def get_player_duo_map_stats(user_id, matches_table="matches"):
    """Returns list of {"map": str, "wr": float, "kd": float} for each map the player played in 2v2."""
    season_start = _get_season_start_ts()
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        f"SELECT map_name, players_json FROM {matches_table} WHERE status='registered' AND league='2v2' AND players_json IS NOT NULL AND finished_at >= %s",
        (season_start,)
    )
    rows = cur.fetchall()
    conn.close()

    from collections import defaultdict
    stats = defaultdict(lambda: {"wins": 0, "total": 0, "kills": 0, "deaths": 0})
    for map_name, pj in rows:
        if not map_name:
            continue
        try:
            players = json.loads(pj or "[]")
        except Exception:
            continue
        for p in players:
            if p.get("user_id") == user_id:
                k = stats[map_name]
                k["total"]  += 1
                k["wins"]   += 1 if p.get("won") else 0
                k["kills"]  += p.get("kills",  0)
                k["deaths"] += p.get("deaths", 1)

    result = []
    for map_name, s in stats.items():
        wr = s["wins"] / s["total"] if s["total"] > 0 else 0.0
        kd = round(s["kills"] / max(s["deaths"], 1), 2)
        result.append({
            "map":    map_name,
            "wins":   s["wins"],
            "losses": s["total"] - s["wins"],
            "wr":     wr,
            "kd":     kd,
        })

    for default_map in MAPS:
        if not any(r["map"] == default_map for r in result):
            result.append({"map": default_map, "wr": 0.0, "kd": 0.0})

    result.sort(key=lambda r: r["wr"], reverse=True)
    return result[:5]


# ==================== ПРОМОКОДЫ ====================

def _rewards_to_str(rewards: list) -> str:
    """Человекочитаемое описание списка наград."""
    parts = []
    for r in rewards:
        t = r.get("type", "")
        if t == "coins":
            parts.append(f"💰 {r.get('value', 0)} AC")
        elif t == "premium":
            parts.append(f"👑 Premium {r.get('days', 30)} дн.")
        elif t == "quals":
            parts.append(f"⭐ Quals {r.get('days', 30)} дн.")
        else:
            parts.append(t)
    return " + ".join(parts) if parts else "—"

def create_promo_code(code, rewards: list, max_uses):
    """rewards = [{"type": "coins"|"premium"|"quals", "value": int, "days": int}, ...]
    Returns (True, "created") / (True, "reactivated") / (False, "exists_active") / (False, "error")
    """
    conn = _db()
    cur = conn.cursor()
    try:
        code_upper = code.upper()
        r0 = rewards[0] if rewards else {}
        rtype = r0.get("type", "coins")
        rvalue = r0.get("value", 0)
        rdays = r0.get("days", 30)
        rjson = json.dumps(rewards, ensure_ascii=False)

        cur.execute("SELECT id, is_active FROM promo_codes WHERE code=%s", (code_upper,))
        existing = cur.fetchone()

        if existing:
            ex_id, ex_active = existing
            if ex_active:
                return False, "exists_active"
            cur.execute(
                "UPDATE promo_codes SET reward_type=%s, reward_value=%s, max_uses=%s, uses=0, "
                "is_active=1, reward_days=%s, rewards_json=%s WHERE id=%s",
                (rtype, rvalue, max_uses, rdays, rjson, ex_id),
            )
            conn.commit()
            return True, "reactivated"
        else:
            cur.execute(
                "INSERT INTO promo_codes (code, reward_type, reward_value, max_uses, reward_days, rewards_json) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (code_upper, rtype, rvalue, max_uses, rdays, rjson),
            )
            conn.commit()
            return True, "created"
    except Exception as e:
        conn.rollback()
        print(f"[create_promo_code] Ошибка: {e}")
        return False, "error"
    finally:
        conn.close()

def _apply_single_reward(cur, table, uid, reward: dict, now: int) -> str:
    """Применяет одну награду, возвращает строку-описание."""
    t = reward.get("type", "")
    if t == "coins":
        v = reward.get("value", 0)
        cur.execute(f"UPDATE {table} SET coins=coins+%s WHERE user_id=%s", (v, uid))
        return f"💰 <b>{v} AC</b>"
    elif t == "premium":
        days = reward.get("days", 30)
        cur.execute(f"SELECT premium_until FROM {table} WHERE user_id=%s", (uid,))
        prow = cur.fetchone()
        base = max((prow[0] or 0), now) if prow else now
        new_until = base + days * 24 * 3600
        cur.execute(f"UPDATE {table} SET premium_until=%s WHERE user_id=%s", (new_until, uid))
        return f"👑 <b>Premium {days} дн.</b> (до {fmt_dt(new_until)})"
    elif t == "quals":
        days = reward.get("days", 30)
        cur.execute(f"SELECT quals_until FROM {table} WHERE user_id=%s", (uid,))
        qrow = cur.fetchone()
        base = max((qrow[0] or 0), now) if qrow else now
        new_until = base + days * 24 * 3600
        cur.execute(f"UPDATE {table} SET quals_until=%s, quals_access=1 WHERE user_id=%s", (new_until, uid))
        return f"⭐ <b>Quals {days} дн.</b> (до {fmt_dt(new_until)})"
    return ""

def use_promo_code(uid, code):
    conn = _db()
    cur = conn.cursor()
    code_upper = code.upper()
    cur.execute(
        "SELECT id, reward_type, reward_value, max_uses, uses, is_active, reward_days, rewards_json "
        "FROM promo_codes WHERE code=%s",
        (code_upper,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, "❌ Промокод не найден"
    pid, reward_type, reward_value, max_uses, uses, is_active, reward_days, rewards_json = row
    if not is_active:
        conn.close()
        return False, "❌ Промокод недействителен"
    if max_uses > 0 and uses >= max_uses:
        conn.close()
        return False, "❌ Промокод исчерпан"
    cur.execute("SELECT COUNT(*) FROM promo_uses WHERE user_id=%s AND code=%s", (uid, code_upper))
    if cur.fetchone()[0] > 0:
        conn.close()
        return False, "❌ Вы уже использовали этот промокод"
    # Парсим список наград (новый формат или старый)
    rewards = []
    if rewards_json:
        try:
            rewards = json.loads(rewards_json)
        except Exception:
            pass
    if not rewards:
        rewards = [{"type": reward_type, "value": reward_value or 0, "days": reward_days or 30}]
    table = get_user_table(uid)
    now = int(time.time())
    lines = []
    for r in rewards:
        deAC = _apply_single_reward(cur, table, uid, r, now)
        if deAC:
            lines.append(deAC)
    cur.execute("INSERT INTO promo_uses (user_id, code) VALUES (%s, %s)", (uid, code_upper))
    cur.execute("UPDATE promo_codes SET uses=uses+1 WHERE id=%s", (pid,))
    conn.commit()
    conn.close()
    rewards_text = "\n".join(f"• {l}" for l in lines)
    return True, f"🎁 <b>Промокод активирован!</b>\n\nВы получили:\n{rewards_text}"

def get_all_promo_codes():
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT code, reward_type, reward_value, max_uses, uses, is_active, reward_days, rewards_json "
        "FROM promo_codes ORDER BY id DESC"
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def deactivate_promo_code(code):
    conn = _db()
    cur = conn.cursor()
    cur.execute("UPDATE promo_codes SET is_active=0 WHERE code=%s", (code.upper(),))
    conn.commit()
    conn.close()


# ==================== МАГАЗИН ХЕЛПЕРЫ ====================
def get_shop_item(item_id):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, deACription, category, price, item_type FROM shop_items WHERE id=%s",
        (item_id,),
    )
    item = cur.fetchone()
    conn.close()
    return item

def get_shop_items_by_category(category):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, deACription, price, item_type FROM shop_items WHERE category=%s AND is_active=1",
        (category,),
    )
    items = cur.fetchall()
    conn.close()
    return items

def get_all_shop_items_list():
    """Returns a formatted string of all shop items with their IDs."""
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, category, price, item_type FROM shop_items WHERE is_active=1 ORDER BY category, id")
    items = cur.fetchall()
    conn.close()
    lines = []
    cur_cat = None
    cat_icons = {"decor": "🖼 Декор", "goods": "📦 Товары"}
    for item_id, name, category, price, item_type in items:
        if category != cur_cat:
            cur_cat = category
            lines.append(f"\n<b>{cat_icons.get(category, category)}</b>")
        lines.append(f"  <code>{item_id}</code> — {name} ({price} AC)")
    return "\n".join(lines)

def get_active_cosmetics(uid):
    """Returns (active_frame, active_banner, active_background) for a player."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "SELECT active_frame, active_banner, active_background FROM players WHERE user_id=%s",
            (uid,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return row[0], row[1], row[2]
    except Exception as e:
        print(f"[get_active_cosmetics] {e}")
    return None, None, None

def has_item_in_inventory(uid, item_id):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM inventory WHERE user_id=%s AND item_id=%s", (uid, item_id))
    count = cur.fetchone()[0]
    conn.close()
    return count > 0

def buy_item(uid, item_id):
    item = get_shop_item(item_id)
    if not item:
        return False, "❌ Товар не найден"
    price = item[4]
    item_type = item[5]
    p = get_player(uid)
    if not p:
        return False, "❌ Игрок не найден"
    if p[5] < price:
        return False, f"❌ Недостаточно SareCoin!\nНужно: {price} AC\nУ вас: {p[5]} AC"
    stackable = {"sticker", "unwarn", "x2coins", "rename"}
    # premium и quals обрабатываются напрямую (без инвентаря)
    if item_type in ("premium", "quals"):
        conn = _db()
        cur = conn.cursor()
        table = get_user_table(uid)
        cur.execute(f"UPDATE {table} SET coins=coins-%s WHERE user_id=%s", (price, uid))
        now = int(time.time())
        days30 = 30 * 24 * 3600
        if item_type == "premium":
            # Продлеваем, если уже есть активный
            cur.execute(f"SELECT premium_until FROM {table} WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            base = max(row[0] or 0, now)
            new_until = base + days30
            cur.execute(f"UPDATE {table} SET premium_until=%s WHERE user_id=%s", (new_until, uid))
            dt = fmt_dt(new_until)
            conn.commit(); conn.close()
            return True, f"👑 <b>Premium активирован на 30 дней!</b>\nДействует до: {dt}"
        else:  # quals
            cur.execute(f"SELECT quals_until FROM {table} WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            base = max(row[0] or 0, now)
            new_until = base + days30
            cur.execute(f"UPDATE {table} SET quals_until=%s, quals_access=1 WHERE user_id=%s", (new_until, uid))
            dt = fmt_dt(new_until)
            conn.commit(); conn.close()
            return True, f"⭐ <b>Quals доступ выдан на 30 дней!</b>\nДействует до: {dt}"
    if item_type not in stackable and has_item_in_inventory(uid, item_id):
        return False, "❌ Этот предмет уже есть в вашем инвентаре!"
    conn = _db()
    cur = conn.cursor()
    cur.execute("UPDATE players SET coins=coins-%s WHERE user_id=%s", (price, uid))
    cur.execute("INSERT INTO inventory (user_id, item_id) VALUES (%s, %s)", (uid, item_id))
    conn.commit()
    conn.close()
    return True, f"✅ Куплено: <b>{item[1]}</b>\nСписано: {price} AC\n\n💡 Активируйте предмет в 🎒 Инвентаре"

def get_inventory(uid):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """SELECT i.id, s.name, s.category, s.item_type, i.purchased_at, i.is_activated, s.id
           FROM inventory i JOIN shop_items s ON i.item_id=s.id
           WHERE i.user_id=%s ORDER BY i.purchased_at DESC""",
        (uid,),
    )
    items = cur.fetchall()
    conn.close()
    return items

def activate_inventory_item(inv_id, uid, item_type, item_name):
    conn = _db()
    cur = conn.cursor()
    if item_type == "unwarn":
        cur.execute("SELECT warns FROM players WHERE user_id=%s", (uid,))
        row = cur.fetchone()
        if row and row[0] > 0:
            cur.execute("UPDATE players SET warns=warns-1 WHERE user_id=%s", (uid,))
        else:
            conn.close()
            return False, "❌ У вас нет варнов для снятия"
    elif item_type == "rename":
        conn.close()
        return "rename", "✏️ Введите новый никнейм (2-20 символов):"
    elif item_type == "premium":
        table = get_user_table(uid)
        now = int(time.time())
        cur.execute(f"SELECT premium_until FROM {table} WHERE user_id=%s", (uid,))
        row = cur.fetchone()
        base = max(row[0] or 0, now)
        new_until = base + 30 * 24 * 3600
        cur.execute(f"UPDATE {table} SET premium_until=%s WHERE user_id=%s", (new_until, uid))
    elif item_type == "quals":
        table = get_user_table(uid)
        now = int(time.time())
        cur.execute(f"SELECT quals_until FROM {table} WHERE user_id=%s", (uid,))
        row = cur.fetchone()
        base = max(row[0] or 0, now)
        new_until = base + 30 * 24 * 3600
        cur.execute(f"UPDATE {table} SET quals_until=%s, quals_access=1 WHERE user_id=%s", (new_until, uid))
    elif item_type == "frame":
        cur.execute(
            """UPDATE inventory SET is_activated=0
               WHERE user_id=%s AND id IN (
                   SELECT i.id FROM inventory i
                   JOIN shop_items s ON i.item_id=s.id
                   WHERE i.user_id=%s AND s.item_type='frame' AND i.is_activated=1
               )""",
            (uid, uid),
        )
        cur.execute("UPDATE players SET active_frame=%s WHERE user_id=%s", (item_name, uid))
    elif item_type == "banner":
        cur.execute(
            """UPDATE inventory SET is_activated=0
               WHERE user_id=%s AND id IN (
                   SELECT i.id FROM inventory i
                   JOIN shop_items s ON i.item_id=s.id
                   WHERE i.user_id=%s AND s.item_type='banner' AND i.is_activated=1
               )""",
            (uid, uid),
        )
        cur.execute("UPDATE players SET active_banner=%s WHERE user_id=%s", (item_name, uid))
    elif item_type == "background":
        cur.execute(
            """UPDATE inventory SET is_activated=0
               WHERE user_id=%s AND id IN (
                   SELECT i.id FROM inventory i
                   JOIN shop_items s ON i.item_id=s.id
                   WHERE i.user_id=%s AND s.item_type='background' AND i.is_activated=1
               )""",
            (uid, uid),
        )
        cur.execute("UPDATE players SET active_background=%s WHERE user_id=%s", (item_name, uid))
    cur.execute(
        "UPDATE inventory SET is_activated=1, activated_at=%s WHERE id=%s",
        (int(time.time()), inv_id),
    )
    conn.commit()
    conn.close()
    return True, f"✅ Предмет <b>{item_name}</b> активирован!"


# ==================== ПАТИ ====================
def get_party_of(uid):
    pid = user_party.get(uid)
    return parties.get(pid) if pid else None

def get_party_max_size(party):
    for m in party["members"]:
        if has_active_premium(m):
            return 3
    return 2


# ==================== КАПИТАН ====================
def pick_captain(team):
    if not team:
        return None
    # Всегда предпочитаем живых игроков ботам: капитан должен баниь карты вручную
    real_in_team = [u for u in team if not is_bot_player(u)]
    effective_team = real_in_team if real_in_team else team
    party_leaders_in_team = []
    party_members_non_leader = set()
    for u in effective_team:
        party = get_party_of(u)
        if party and len(party["members"]) > 1:
            team_members_in_party = [m for m in party["members"] if m in effective_team]
            if len(team_members_in_party) > 1:
                if u == party["leader"]:
                    party_leaders_in_team.append(u)
                else:
                    party_members_non_leader.add(u)
    eligible = [u for u in effective_team if u not in party_members_non_leader]
    if not eligible:
        eligible = effective_team
    if party_leaders_in_team:
        leader = party_leaders_in_team[0]
        if random.random() < 0.60:
            return leader
        rest = [u for u in eligible if u != leader]
        if not rest:
            return leader
        weights = [1.07 if has_active_premium(u) else 1.0 for u in rest]
        return random.choices(rest, weights=weights, k=1)[0]
    weights = [1.07 if has_active_premium(u) else 1.0 for u in eligible]
    return random.choices(eligible, weights=weights, k=1)[0]


# ==================== ВСПОМОГАТЕЛЬНЫЕ УТИЛИТЫ ====================

def tg_link(uid, name):
    """Создаёт кликабельную ссылку на TG-профиль без превью."""
    return f'<a href="tg://user?id={uid}">{name}</a>'

def send_punishment_log(text):
    """Отправляет лог наказания в паблик (ветка, заданная /setlogtopic)."""
    if not LOG_CHAT_ID:
        return
    try:
        kw = {"parse_mode": "HTML", "disable_web_page_preview": True}
        if _dynamic_log_thread_id:
            kw["message_thread_id"] = _dynamic_log_thread_id
        bot.send_message(LOG_CHAT_ID, text, **kw)
    except Exception as e:
        print(f"Punishment log error: {e}")

def send_punishment_log_priv(admin_uid, text):
    """Отправляет лог наказания с указанием приватки администратора."""
    priv_display = get_user_private_display(admin_uid)
    send_punishment_log(f"🏠 <b>{priv_display}</b>\n\n{text}")

def send_result_log(text):
    """Отправляет результат матча в паблик (ветка, заданная /setresulttopic)."""
    if not LOG_CHAT_ID:
        return
    try:
        kw = {"parse_mode": "HTML", "disable_web_page_preview": True}
        tid = _dynamic_results_thread_id if _dynamic_results_thread_id else _dynamic_log_thread_id
        if tid:
            kw["message_thread_id"] = tid
        bot.send_message(LOG_CHAT_ID, text, **kw)
    except Exception as e:
        print(f"Result log error: {e}")

def send_error_log(context: str, error: Exception):
    """Отправляет лог ошибки администраторам и в LOG_CHAT."""
    import traceback as _tb
    tb_text = _tb.format_exc()
    short_tb = tb_text[-800:] if len(tb_text) > 800 else tb_text
    msg = (
        f"🔴 <b>Ошибка бота</b>\n"
        f"📍 <b>Место:</b> <code>{context}</code>\n"
        f"❗ <b>Ошибка:</b> <code>{type(error).__name__}: {str(error)[:300]}</code>\n"
        f"📋 <b>Трейс:</b>\n<pre>{short_tb}</pre>"
    )
    # Отправляем всем администраторам
    for _aid in ADMIN_IDS_LIST:
        try:
            bot.send_message(_aid, msg, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            pass
    # Дублируем в лог-чат если настроен
    if LOG_CHAT_ID:
        try:
            kw = {"parse_mode": "HTML", "disable_web_page_preview": True}
            if _dynamic_log_thread_id:
                kw["message_thread_id"] = _dynamic_log_thread_id
            bot.send_message(LOG_CHAT_ID, msg, **kw)
        except Exception:
            pass
    print(f"[ERROR][{context}] {type(error).__name__}: {error}")

def kick_from_lobby_if_present(uid):
    """Кикает игрока из лобби/фазы принятия если он там есть."""
    lobby_id = user_lobby.get(uid)
    if not lobby_id:
        return
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        user_lobby.pop(uid, None)
        return
    if uid in lobby.get("players", []):
        lobby["players"].remove(uid)
        lobby_player_messages.get(lobby_id, {}).pop(uid, None)
        accept_status_messages.get(lobby_id, {}).pop(uid, None)
        match_found_messages.get(lobby_id, {}).pop(uid, None)
        if not lobby["players"]:
            active_lobbies.pop(lobby_id, None)
        else:
            broadcast_lobby_update(lobby_id)
    user_lobby.pop(uid, None)
    try:
        bot.send_message(uid, "🚫 Вы были исключены из лобби.")
    except Exception:
        pass


# ==================== ПРОВЕРКА БЛОКИРОВОК ====================
def check_blocked(uid):
    if is_banned_check(uid):
        return "🚫 Вы заблокированы в боте."
    if is_on_check_db(uid):
        admin_uid = get_check_admin(uid)
        admin_name = "администратора"
        if admin_uid:
            ap = get_player(admin_uid)
            if ap:
                tg_u = ap[22] if len(ap) > 22 else ""
                admin_name = f"@{tg_u}" if tg_u else f"администратора (id:{admin_uid})"
        return (
            f"⚠️ <b>Вас вызвал на проверку {admin_name}</b>\n\n"
            "Доступ к боту ограничен до прохождения проверки.\nОбратитесь к администратору."
        )
    return None


# ==================== ГЛАВНОЕ МЕНЮ ====================
def main_menu(uid):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if uid in user_lobby:
        kb.add(
            types.InlineKeyboardButton("👤 Профиль", callback_data="profile"),
            types.InlineKeyboardButton("🔄 Вернуться в лобби", callback_data="rejoin_lobby"),
        )
    else:
        kb.add(
            types.InlineKeyboardButton("👤 Профиль", callback_data="profile"),
            types.InlineKeyboardButton("🎮 Найти матч", callback_data="find"),
        )
    kb.add(
        types.InlineKeyboardButton("🏆 Топ", callback_data="top"),
        types.InlineKeyboardButton("🛒 Магазин", callback_data="shop"),
        types.InlineKeyboardButton("🎒 Инвентарь", callback_data="inv"),
        types.InlineKeyboardButton("💳 Купить монеты", callback_data="buy_coins"),
        types.InlineKeyboardButton("🎁 Промокод", callback_data="promo"),
        types.InlineKeyboardButton("🎟 Тикет / Жалоба", callback_data="ticket_start"),
    )
    in_party = uid in user_party
    kb.add(types.InlineKeyboardButton(
        "👥 Моя пати" if in_party else "➕ Создать пати", callback_data="party_menu"
    ))
    if is_admin(uid):
        kb.add(
            types.InlineKeyboardButton("🤖 Добавить ботов", callback_data="add_bots_admin"),
            types.InlineKeyboardButton("⚙️ Админ панель", callback_data="admin_panel"),
        )
    elif is_game_reg_check(uid):
        kb.add(types.InlineKeyboardButton("📋 Регистрация матчей", callback_data="game_reg_panel"))
    if is_creator(uid):
        kb.add(types.InlineKeyboardButton("🔴 Креаторская панель", callback_data="creator_panel"))
    return kb


def main_menu_text(uid):
    p = get_player(uid)
    coins = p[7] if p and len(p) > 7 else 0
    return (
        f"⚡ <b>Actual FACEIT</b>\n"
        f"🏠 Приватка: <b>⚡ StandDarling</b>\n"
        f"🪙 Кошелёк: <b>{coins} AC</b>\n"
        f"🆔 Ваш TG ID: <code>{uid}</code>"
    )


@bot.message_handler(commands=["setlogtopic"])
def cmd_setlogtopic(msg):
    """Привязать текущую ветку как ветку логов наказаний. Только для главных админов."""
    uid = msg.from_user.id
    if uid not in ADMIN_IDS_LIST:
        return
    thread_id = msg.message_thread_id
    if not thread_id:
        bot.send_message(msg.chat.id, "❌ Команда должна быть отправлена внутри ветки (topic), а не в общем чате.")
        return
    global _dynamic_log_thread_id
    _dynamic_log_thread_id = thread_id
    set_setting("log_thread_id", thread_id)
    bot.send_message(
        msg.chat.id,
        f"✅ <b>Ветка логов наказаний</b> привязана!\n\nThread ID: <code>{thread_id}</code>\n\nСюда будут приходить: 🚫 баны, 🔇 муты, ⚠️ варны, ❌ отмены матчей.",
        parse_mode="HTML",
        message_thread_id=thread_id
    )


@bot.message_handler(commands=["setresulttopic"])
def cmd_setresulttopic(msg):
    """Привязать текущую ветку как ветку результатов матчей. Только для главных админов."""
    uid = msg.from_user.id
    if uid not in ADMIN_IDS_LIST:
        return
    thread_id = msg.message_thread_id
    if not thread_id:
        bot.send_message(msg.chat.id, "❌ Команда должна быть отправлена внутри ветки (topic), а не в общем чате.")
        return
    global _dynamic_results_thread_id
    _dynamic_results_thread_id = thread_id
    set_setting("results_thread_id", thread_id)
    bot.send_message(
        msg.chat.id,
        f"✅ <b>Ветка результатов матчей</b> привязана!\n\nThread ID: <code>{thread_id}</code>\n\nСюда будут приходить: 🏁 результаты всех зарегистрированных матчей.",
        parse_mode="HTML",
        message_thread_id=thread_id
    )


@bot.message_handler(commands=["topicsettings"])
def cmd_topicsettings(msg):
    """Показать текущие настройки веток. Только для главных админов."""
    uid = msg.from_user.id
    if uid not in ADMIN_IDS_LIST:
        return
    log_info = f"<code>{_dynamic_log_thread_id}</code>" if _dynamic_log_thread_id else "не задана"
    res_info = f"<code>{_dynamic_results_thread_id}</code>" if _dynamic_results_thread_id else f"не задана (используется ветка логов)"
    bot.send_message(
        msg.chat.id,
        f"⚙️ <b>Текущие настройки веток</b>\n\n"
        f"📋 Ветка логов (баны/муты/варны/отмены):\n{log_info}\n\n"
        f"🏁 Ветка результатов:\n{res_info}\n\n"
        f"<i>Зайди в нужную ветку и отправь /setlogtopic или /setresulttopic чтобы привязать.</i>",
        parse_mode="HTML"
    )


@bot.message_handler(commands=["revive_match"])
def cmd_revive_match(msg):
    """
    /revive_match КОД  — оживить матч по коду (напр. COBVVK1).
    Восстанавливает лобби в памяти и отправляет новую admin-карточку.
    Только для администраторов.
    """
    uid = msg.from_user.id
    if not is_admin(uid):
        return

    parts = msg.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(msg,
            "❌ Укажи код матча.\nПример: <code>/revive_match COBVVK1</code>",
            parse_mode="HTML")
        return

    match_code_arg = parts[1].strip().upper()

    conn = _db()
    cur  = conn.cursor()
    row_um = row_m = None
    try:
        # 1. Ищем в unregistered_matches (там есть team_ct_json / team_t_json)
        cur.execute(
            "SELECT match_id, match_code, league, device, map_name, "
            "players_json, team_ct_json, team_t_json, host_game_id, acreenshots_count "
            "FROM unregistered_matches WHERE UPPER(match_code)=%s LIMIT 1",
            (match_code_arg,),
        )
        row_um = cur.fetchone()

        # 2. Ищем в matches
        cur.execute(
            "SELECT match_id, match_code, league, device, map_name, "
            "players_json, started_at, COALESCE(private_key,'darling'), "
            "admin_thread_id, admin_msg_id, status "
            "FROM matches WHERE UPPER(match_code)=%s LIMIT 1",
            (match_code_arg,),
        )
        row_m = cur.fetchone()
    except Exception as _e:
        bot.reply_to(msg, f"❌ Ошибка БД: {_e}")
        conn.close()
        return
    finally:
        conn.close()

    if not row_um and not row_m:
        bot.reply_to(msg,
            f"❌ Матч <b>{match_code_arg}</b> не найден ни в matches, ни в unregistered_matches.",
            parse_mode="HTML")
        return

    # ── Собираем данные из доступных источников ───────────────────────────
    if row_um:
        match_id, match_code, league, device, map_name, \
            players_json, team_ct_json_s, team_t_json_s, host_game_id, AC_count = row_um
        private_key     = "darling"
        admin_thread_id = None
        admin_msg_id    = None
        if row_m:
            private_key     = row_m[7] or "darling"
            admin_thread_id = row_m[8]
            admin_msg_id    = row_m[9]
    else:
        match_id, match_code, league, device, map_name, \
            players_json, started_at, private_key, \
            admin_thread_id, admin_msg_id, _ = row_m
        team_ct_json_s = "[]"
        team_t_json_s  = "[]"
        host_game_id   = ""
        AC_count       = 0

    match_key = f"match_{match_id}"

    # ── Парсим команды ────────────────────────────────────────────────────
    try:
        team_ct = json.loads(team_ct_json_s or "[]")
        team_t  = json.loads(team_t_json_s  or "[]")
    except Exception:
        team_ct, team_t = [], []

    players_info = []
    try:
        players_info = json.loads(players_json or "[]")
    except Exception:
        pass

    # Если team_ct/team_t пустые — восстанавливаем из players_json
    if not team_ct and not team_t and players_info:
        for p in players_info:
            uid2 = p.get("user_id")
            if not uid2:
                continue
            if p.get("team") == "ct":
                team_ct.append(uid2)
            else:
                team_t.append(uid2)

    players = list(dict.fromkeys(team_ct + team_t))

    # ── Ищем хоста ───────────────────────────────────────────────────────
    _rv_priv_cfg = PRIVATE_CONFIG.get(private_key, PRIVATE_CONFIG["darling"])
    _rv_table    = _rv_priv_cfg["table"]
    host_uid     = next((u for u in team_ct if not is_bot_player(u)), None)
    if host_uid:
        host_p_row = get_player_from_table(host_uid, _rv_table) or get_player(host_uid)
        if host_p_row:
            host_name_rv    = host_p_row[1]
            host_game_id_rv = host_p_row[2] if not host_game_id else host_game_id
        else:
            host_name_rv    = str(host_uid)
            host_game_id_rv = host_game_id or "—"
    else:
        host_name_rv    = "—"
        host_game_id_rv = host_game_id or "—"

    # ── Строим лобби в памяти ─────────────────────────────────────────────
    lobby = {
        "match_id":          match_id,
        "match_code":        match_code or match_code_arg,
        "league":            league or "default",
        "device":            device or "",
        "map_name":          map_name or "",
        "status":            "active",
        "players":           players,
        "team_ct":           team_ct,
        "team_t":            team_t,
        "ACreenshots":       {},
        "ACreenshots_count": AC_count or 0,
        "reg_taken_by":      None,
        "match_key":         match_key,
        "started_at":        int(time.time()),
        "private":           private_key,
        "admin_thread_id":   admin_thread_id,
        "admin_msg_id":      admin_msg_id,
        "host_uid":          host_uid,
        "host_game_id":      host_game_id_rv,
    }
    running_matches[match_key] = lobby

    # Восстанавливаем awaiting_ACreenshot для игроков
    for p_uid in players:
        if not is_bot_player(p_uid):
            awaiting_ACreenshot[p_uid] = match_key

    # ── Обновляем unregistered_matches ───────────────────────────────────
    try:
        _pj  = json.dumps(players_info, ensure_ascii=False)
        _ctj = json.dumps(team_ct,      ensure_ascii=False)
        _tj  = json.dumps(team_t,       ensure_ascii=False)
        _conn2 = _db(); _cur2 = _conn2.cursor()
        _cur2.execute(
            """INSERT INTO unregistered_matches
               (match_id, match_code, league, device, map_name,
                players_json, team_ct_json, team_t_json, host_game_id, acreenshots_count, started_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (match_id) DO UPDATE SET
                   match_code        = EXCLUDED.match_code,
                   players_json      = EXCLUDED.players_json,
                   team_ct_json      = EXCLUDED.team_ct_json,
                   team_t_json       = EXCLUDED.team_t_json,
                   acreenshots_count = EXCLUDED.acreenshots_count""",
            (match_id, match_code or match_code_arg, league or "", device or "",
             map_name or "", _pj, _ctj, _tj, host_game_id_rv, AC_count or 0,
             int(time.time())),
        )
        _conn2.commit(); _conn2.close()
    except Exception as _ue:
        print(f"[revive_match] unregistered_matches update error: {_ue}")

    # ── Строим текст admin-карточки ───────────────────────────────────────
    _adm_priv_label = f"{_rv_priv_cfg['emoji']} {_rv_priv_cfg['display']}"

    def _rv_pline(idx, u):
        p2 = get_player_from_table(u, _rv_table) or get_player(u)
        num = NUMBER_EMOJI[idx] if idx < len(NUMBER_EMOJI) else f"{idx+1}."
        if p2:
            prem = " 👑" if (not p2[13] and has_active_premium(u)) else ""
            elo  = _resolve_display_elo(u, p2, _rv_table, league or "default")
            return f"{num} {p2[1]}{prem} | ID: <code>{u}</code> | ELO: {elo}"
        return f"{num} <code>{u}</code>"

    ct_lines = "\n".join([_rv_pline(i, u) for i, u in enumerate(team_ct)])
    t_lines  = "\n".join([_rv_pline(i, u) for i, u in enumerate(team_t)])

    match_text = (
        f"♻️ <b>МАТЧ #{match_code or match_code_arg} ОЖИВЛЁН</b>\n\n"
        f"🏠 Приватка: <b>{_adm_priv_label}</b>\n"
        f"🏷 Лига: {format_league(league or 'default')}\n"
        f"📱 Устройство: {(device or '').upper()}\n"
        f"🗺 Карта: <b>{map_name or '?'}</b>\n"
        f"👑 Хост: <b>{host_name_rv}</b> | Game ID: <code>{host_game_id_rv}</code>\n\n"
        f"💙 <b>Команда CT</b>\n{ct_lines}\n\n"
        f"🧡 <b>Команда T</b>\n{t_lines}"
    )

    # ── Отправляем новую admin-карточку ──────────────────────────────────
    kb_admin = _build_admin_match_kb(match_key, match_code or match_code_arg, AC_count or 0)

    if ADMIN_CHAT_ID:
        new_thread_id = admin_thread_id  # пробуем переиспользовать старую ветку
        if not new_thread_id:
            try:
                topic = bot.create_forum_topic(ADMIN_CHAT_ID, f"MATCH #{match_code or match_code_arg}")
                new_thread_id = topic.message_thread_id
            except Exception:
                pass

        lobby["admin_thread_id"] = new_thread_id
        try:
            send_kw = {"reply_markup": kb_admin, "parse_mode": "HTML"}
            if new_thread_id:
                send_kw["message_thread_id"] = new_thread_id
            sent = bot.send_message(ADMIN_CHAT_ID, match_text, **send_kw)
            lobby["admin_msg_id"] = sent.message_id
            try:
                bot.pin_chat_message(ADMIN_CHAT_ID, sent.message_id, disable_notification=True)
            except Exception:
                pass
        except Exception as _ae:
            print(f"[revive_match] admin send error: {_ae}")
            bot.reply_to(msg, f"⚠️ Лобби восстановлено в памяти, но не удалось отправить в admin-чат: {_ae}")
            return

    # ── Подтверждение тому, кто вызвал команду ───────────────────────────
    bot.reply_to(
        msg,
        f"✅ <b>Матч #{match_code or match_code_arg} оживлён!</b>\n\n"
        f"• Лобби восстановлено в памяти\n"
        f"• Карточка отправлена в admin-чат\n"
        f"• Игроки: {len(players)} (CT: {len(team_ct)}, T: {len(team_t)})\n"
        f"• Скриншоты: {AC_count or 0}",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["start"])
def cmd_start(msg):
    uid = msg.from_user.id
    try:
        if msg.from_user.username:
            update_tg_username(uid, msg.from_user.username)
        # Проверка обязательной подписки на каналы
        not_subbed = check_subACriptions(uid)
        if not_subbed:
            send_subACribe_message(uid)
            return
        # Убираем любую ReplyKeyboard
        try:
            rm = bot.send_message(uid, "…", reply_markup=types.ReplyKeyboardRemove())
            bot.delete_message(uid, rm.message_id)
        except Exception:
            pass
        # Автоматически устанавливаем приватку darling
        if uid not in user_private:
            user_private[uid] = "darling"
            save_user_private(uid, "darling")
        err = check_blocked(uid)
        if err:
            bot.send_message(uid, err, parse_mode="HTML")
            return
        if is_registered(uid):
            bot.send_message(uid, main_menu_text(uid), reply_markup=main_menu(uid), parse_mode="HTML")
            return
        user_flow[uid] = {"state": "nick", "bot_msgs": []}
        priv_name = get_user_private_display(uid)
        m = bot.send_message(
            uid,
            f"👋 Добро пожаловать в <b>{priv_name}</b>!\n\n<b>Шаг 1:</b> Введи свой никнейм (2-20 символов):",
            parse_mode="HTML",
        )
        user_flow[uid]["bot_msgs"].append(m.message_id)
    except Exception as e:
        print(f"[cmd_start] Критическая ошибка uid={uid}: {e}")
        try:
            bot.send_message(uid, "⚠️ Произошла ошибка. Попробуй ещё раз через несколько секунд.")
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def cb_check_sub(c):
    uid = c.from_user.id
    not_subbed = check_subACriptions(uid)
    if not_subbed:
        names = " и ".join(ch["name"] for ch in not_subbed)
        bot.answer_callback_query(
            c.id,
            f"❌ Вы не подписаны на: {names}. Подпишитесь и попробуйте снова.",
            show_alert=True,
        )
        return
    bot.answer_callback_query(c.id, "✅ Подписка подтверждена!")
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass
    # Убираем любую ReplyKeyboard
    try:
        rm = bot.send_message(uid, "…", reply_markup=types.ReplyKeyboardRemove())
        bot.delete_message(uid, rm.message_id)
    except Exception:
        pass
    # Автоматически устанавливаем приватку darling
    if uid not in user_private:
        user_private[uid] = "darling"
        save_user_private(uid, "darling")
    err = check_blocked(uid)
    if err:
        bot.send_message(uid, err)
        return
    if is_registered(uid):
        bot.send_message(uid, main_menu_text(uid), reply_markup=main_menu(uid), parse_mode="HTML")
        return
    user_flow[uid] = {"state": "nick", "bot_msgs": []}
    priv_name = get_user_private_display(uid)
    m = bot.send_message(
        uid,
        f"👋 Добро пожаловать в <b>{priv_name}</b>!\n\n<b>Шаг 1:</b> Введи свой никнейм (2-20 символов):",
        parse_mode="HTML",
    )
    user_flow[uid]["bot_msgs"].append(m.message_id)


@bot.callback_query_handler(func=lambda c: c.data == "back_main")
def cb_back_main(c):
    uid = c.from_user.id
    bot.answer_callback_query(c.id)
    try:
        bot.edit_message_text(main_menu_text(uid), c.message.chat.id, c.message.message_id,
                              reply_markup=main_menu(uid), parse_mode="HTML")
    except Exception:
        bot.send_message(uid, main_menu_text(uid), reply_markup=main_menu(uid), parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data == "rejoin_lobby")
def cb_rejoin_lobby(c):
    uid = c.from_user.id
    lobby_id = user_lobby.get(uid)
    if not lobby_id:
        bot.answer_callback_query(c.id, "❌ Вы не в лобби")
        bot.edit_message_text(main_menu_text(uid), c.message.chat.id, c.message.message_id, reply_markup=main_menu(uid), parse_mode="HTML")
        return
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby.get("status") != "waiting":
        bot.answer_callback_query(c.id, "❌ Лобби недоступно")
        bot.edit_message_text(main_menu_text(uid), c.message.chat.id, c.message.message_id, reply_markup=main_menu(uid), parse_mode="HTML")
        return
    text = build_lobby_text(lobby_id)
    kb = build_lobby_kb(lobby_id, uid)
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    if lobby_player_messages.get(lobby_id) is None:
        lobby_player_messages[lobby_id] = {}
    lobby_player_messages[lobby_id][uid] = (c.message.chat.id, c.message.message_id)
    bot.answer_callback_query(c.id)


# ==================== ПРОМОКОД (пользователь) ====================
@bot.callback_query_handler(func=lambda c: c.data == "promo")
def cb_promo(c):
    uid = c.from_user.id
    err = check_blocked(uid)
    if err:
        bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
        return
    if not is_registered(uid):
        bot.answer_callback_query(c.id, "❌ Сначала зарегистрируйтесь /start")
        return
    promo_flow[uid] = True
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="promo_cancel"))
    bot.send_message(uid, "🎁 Введите промокод:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "promo_cancel")
def cb_promo_cancel(c):
    uid = c.from_user.id
    promo_flow.pop(uid, None)
    bot.answer_callback_query(c.id)
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass

@bot.message_handler(func=lambda m: m.from_user.id in promo_flow and m.text is not None)
def handle_promo_input(msg):
    uid = msg.from_user.id
    promo_flow.pop(uid, None)
    code = msg.text.strip()
    ok, result_msg = use_promo_code(uid, code)
    p = get_player(uid)
    balance = f"\n💰 Баланс: <b>{p[5]} AC</b>" if p and ok else ""
    bot.send_message(uid, f"{result_msg}{balance}")


# ==================== РЕГИСТРАЦИЯ ====================
@bot.message_handler(func=lambda m: user_flow.get(m.from_user.id, {}).get("state") == "nick")
def reg_nick(msg):
    uid = msg.from_user.id
    nick = msg.text.strip()
    bot_msgs = user_flow[uid].get("bot_msgs", [])
    if not (2 <= len(nick) <= 20):
        bot.send_message(uid, "❌ Никнейм 2-20 символов")
        return
    if nick_taken(nick, uid=uid):
        bot.send_message(uid, "❌ <b>Этот никнейм уже занят!</b>\n\nЕсли это ваш никнейм — обратитесь к администратору.\nВведите другой никнейм:")
        return
    user_flow[uid] = {"state": "id", "nick": nick, "bot_msgs": bot_msgs}
    m = bot.send_message(uid, "<b>Шаг 2:</b> Введи игровой ID\n\nМожно: русские и английские буквы, цифры, <code>_</code> и <code>-</code>", parse_mode="HTML")
    user_flow[uid]["bot_msgs"].append(m.message_id)

@bot.message_handler(func=lambda m: user_flow.get(m.from_user.id, {}).get("state") == "id")
def reg_id(msg):
    uid = msg.from_user.id
    game_id = msg.text.strip()
    if not re.match(r'^[a-zA-ZА-Яа-яёЁ0-9_-]+$', game_id):
        bot.send_message(uid, "❌ Недопустимые символы! Только буквы, цифры, <code>_</code>, <code>-</code>", parse_mode="HTML")
        return
    if game_id_taken(game_id, uid=uid):
        bot.send_message(uid, "❌ <b>Этот Game ID уже занят!</b>\n\nВведите другой Game ID:", parse_mode="HTML")
        return
    user_flow[uid]["game_id"] = game_id
    user_flow[uid]["state"] = "device"
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row("MOBILE", "PC")
    m = bot.send_message(uid, "<b>Шаг 3:</b> Выбери устройство:", reply_markup=kb, parse_mode="HTML")
    user_flow[uid]["bot_msgs"].append(m.message_id)

@bot.message_handler(func=lambda m: user_flow.get(m.from_user.id, {}).get("state") == "device")
def reg_device(msg):
    uid = msg.from_user.id
    device = msg.text.strip()
    if device not in ("MOBILE", "PC"):
        bot.send_message(uid, "❌ Выбери MOBILE или PC")
        return
    data = user_flow.pop(uid)
    tg_u = msg.from_user.username or ""
    register_user(uid, data["nick"], data["game_id"], device, tg_u)
    # Удаляем все сообщения бота из процесса регистрации
    for mid in data.get("bot_msgs", []):
        try:
            bot.delete_message(uid, mid)
        except Exception:
            pass
    priv_name = get_user_private_display(uid)
    bot.send_message(
        uid,
        f"✅ Регистрация завершена!\n🏠 Приватка: <b>{priv_name}</b>\n\nНик: <b>{data['nick']}</b>\nGame ID: <code>{data['game_id']}</code>\nDevice: {device}",
        reply_markup=types.ReplyKeyboardRemove(),
        parse_mode="HTML",
    )
    bot.send_message(uid, main_menu_text(uid), reply_markup=main_menu(uid), parse_mode="HTML")


# ==================== СМЕНА ДАННЫХ ====================
@bot.callback_query_handler(func=lambda c: c.data in ("change_nick", "change_game_id"))
def cb_change_own(c):
    uid = c.from_user.id
    field = "nick" if c.data == "change_nick" else "game_id"
    change_flow[uid] = {"field": field}
    bot.answer_callback_query(c.id)
    if field == "nick":
        bot.send_message(uid, "✏️ Введите новый никнейм (2-20 символов):")
    else:
        bot.send_message(uid, "🎮 Введите новый Game ID:\n\nТолько буквы, цифры, <code>_</code> и <code>-</code>")

@bot.message_handler(func=lambda m: m.from_user.id in change_flow and "field" in change_flow.get(m.from_user.id, {}) and m.text is not None)
def handle_change_flow(msg):
    uid = msg.from_user.id
    if uid not in change_flow:
        return
    data = change_flow.pop(uid)
    field = data["field"]
    text = msg.text.strip()
    conn = _db()
    cur = conn.cursor()
    if field == "nick":
        if not (2 <= len(text) <= 20):
            bot.send_message(uid, "❌ Никнейм 2-20 символов.")
            conn.close()
            return
        if nick_taken(text, uid=uid, exclude_uid=uid):
            bot.send_message(uid, "❌ <b>Этот никнейм уже занят!</b>")
            conn.close()
            return
        table = get_user_table(uid)
        cur.execute(f"UPDATE {table} SET username=%s WHERE user_id=%s", (text, uid))
        conn.commit()
        conn.close()
        bot.send_message(uid, f"✅ Никнейм изменён на <b>{text}</b>!")
    elif field == "game_id":
        if not re.match(r'^[a-zA-ZА-Яа-яёЁ0-9_-]+$', text):
            bot.send_message(uid, "❌ Только буквы, цифры, <code>_</code> и <code>-</code>")
            conn.close()
            return
        if game_id_taken(text, uid=uid, exclude_uid=uid):
            bot.send_message(uid, "❌ <b>Этот Game ID уже занят!</b>")
            conn.close()
            return
        table = get_user_table(uid)
        cur.execute(f"UPDATE {table} SET game_id=%s WHERE user_id=%s", (text, uid))
        conn.commit()
        conn.close()
        bot.send_message(uid, f"✅ Game ID изменён на <code>{text}</code>!")
    elif field == "admin_nick":
        target_id = data.get("target_id")
        conn.close()
        if not target_id:
            return
        if not (2 <= len(text) <= 20):
            bot.send_message(uid, "❌ Никнейм 2-20 символов.")
            return
        if nick_taken(text, exclude_uid=target_id):
            bot.send_message(uid, f"❌ Никнейм <b>{text}</b> уже занят!")
            return
        c2 = _db(); c2cur = c2.cursor()
        c2cur.execute("UPDATE players SET username=%s WHERE user_id=%s", (text, target_id))
        c2.commit(); c2.close()
        bot.send_message(uid, f"✅ Никнейм игрока изменён на <b>{text}</b>!")
        try:
            bot.send_message(target_id, f"✏️ Администратор изменил ваш никнейм на <b>{text}</b>!")
        except Exception:
            pass
    elif field == "admin_id":
        target_id = data.get("target_id")
        conn.close()
        if not target_id:
            return
        if not re.match(r'^[a-zA-ZА-Яа-яёЁ0-9_-]+$', text):
            bot.send_message(uid, "❌ Только буквы, цифры, <code>_</code> и <code>-</code>")
            return
        if game_id_taken(text, exclude_uid=target_id):
            bot.send_message(uid, f"❌ Game ID <code>{text}</code> уже занят!")
            return
        c2 = _db(); c2cur = c2.cursor()
        c2cur.execute("UPDATE players SET game_id=%s WHERE user_id=%s", (text, target_id))
        c2.commit(); c2.close()
        bot.send_message(uid, f"✅ Game ID игрока изменён на <code>{text}</code>!")
        try:
            bot.send_message(target_id, f"🎮 Администратор изменил ваш Game ID на <code>{text}</code>!")
        except Exception:
            pass
    else:
        conn.close()


# ==================== ПРОФИЛЬ ====================
@bot.callback_query_handler(func=lambda c: c.data == "profile")
def cb_profile(c):
    uid = c.from_user.id
    p = get_current_player(uid)
    if not p:
        bot.edit_message_text("❌ Ошибка", c.message.chat.id, c.message.message_id)
        bot.answer_callback_query(c.id)
        return

    games   = p[6] + p[7]
    winrate = round(p[6] / games * 100, 1) if games > 0 else 0
    kd      = round(p[8] / p[9], 2) if p[9] > 0 else p[8]
    warns   = p[15] if len(p) > 15 else 0
    quals   = "✅" if (len(p) > 16 and p[16] == 1) else "❌"
    premium = has_active_premium(uid)
    crown   = " 👑 Premium" if premium else ""
    verified_badge = " ✅" if is_verified_check(uid) else ""
    lvl     = get_faceit_level(p[4])
    bar     = elo_bar(p[4], lvl)
    muted   = is_muted_check(uid)
    mute_text = ""
    if muted:
        mins = get_mute_remaining(uid) // 60
        mute_text = f"\n🔇 Мут: {mins} мин."

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✏️ Изменить ник",     callback_data="change_nick"),
        types.InlineKeyboardButton("🎮 Изменить Game ID", callback_data="change_game_id"),
    )
    extra_btns = []
    if has_quals_access(uid):
        extra_btns.append(types.InlineKeyboardButton("⭐ Quals профиль", callback_data="profile_quals"))
    extra_btns.append(types.InlineKeyboardButton("👥 2v2 профиль", callback_data="profile_duo"))
    kb.add(*extra_btns)
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))

    if CARDS_ENABLED:
        try:
            # rank among registered players in current private
            _priv_table = get_user_table(uid)
            all_p   = get_all_players(_priv_table)
            rank    = next((i+1 for i, row in enumerate(all_p) if row[0] == uid), len(all_p))
            league  = "DEFAULT"

            _matches_table = get_user_matches_table(uid)
            map_stats   = get_player_map_stats(uid, _matches_table)
            recent      = get_player_recent_matches(uid, limit=5, matches_table=_matches_table)
            quals_stats = get_player_quals_stats(uid, _priv_table)
            duo_stats   = get_player_duo_stats(uid, _priv_table)
            lb_data     = [
                (i+1, row[1], row[2], has_active_premium(row[0]), is_admin(row[0]), is_verified_check(row[0]))
                for i, row in enumerate(all_p[:3])
            ]
            mvp_count   = p[31] if len(p) > 31 else 0

            avatar_bytes = get_user_avatar(uid)
            active_frame, active_banner, active_background = get_active_cosmetics(uid)
            img_buf = generate_profile_card(
                username           = p[1]   or "Unknown",
                game_id            = p[2]   or "",
                user_id            = p[0],
                elo                = p[4],
                wins               = p[6],
                losses             = p[7],
                kills              = p[8],
                deaths             = p[9],
                assists            = p[10],
                is_premium         = premium,
                is_admin           = is_admin(uid),
                global_rank        = rank,
                league             = league,
                map_stats          = map_stats,
                recent             = recent,
                leaderboard        = lb_data,
                quals_stats        = quals_stats,
                mvp_count          = mvp_count,
                is_verified        = is_verified_check(uid),
                duo_stats          = duo_stats,
                avatar_bytes       = avatar_bytes,
                active_frame       = active_frame,
                active_banner      = active_banner,
                active_background  = active_background,
            )

            # delete old message, send photo with buttons
            try:
                bot.delete_message(c.message.chat.id, c.message.message_id)
            except Exception:
                pass

            caption = (
                f"👤 <b>{p[1]}</b>{verified_badge}{crown}  |  📊 ELO: <b>{p[4]}</b>  |  Lvl <b>{lvl}</b>\n"
                f"💰 Баланс: {p[5]} AC  ·  ⭐ Quals: {quals}  ·  ⚠️ Варны: {warns}/3{mute_text}"
            )
            bot.send_photo(
                c.message.chat.id,
                img_buf,
                caption    = caption,
                reply_markup = kb,
                parse_mode = "HTML",
            )
            bot.answer_callback_query(c.id)
            return
        except Exception as e:
            send_error_log("card_profile (Default)", e)

    # fallback text profile
    text = (
        f"👤 <b>{p[1]}</b>{verified_badge}{crown}\n"
        f"🆔 Telegram ID: <code>{p[0]}</code>\n"
        f"🎮 Game ID: <code>{p[2]}</code>\n"
        f"📱 Device: {p[3]}\n"
        f"📊 ELO: {p[4]} · Lvl {lvl}\n"
        f"{bar}\n"
        f"💰 Баланс: {p[5]} AC\n"
        f"⭐ Quals: {quals}\n"
        f"⚠️ Варны: {warns}/3{mute_text}\n\n"
        f"🏆 {p[6]}W · ❌ {p[7]}L · 📈 {winrate}%\n"
        f"🔫 K: {p[8]} · 💀 D: {p[9]} · 🤝 A: {p[10]} · K/D: {kd}"
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


# ==================== QUALS ПРОФИЛЬ ====================
@bot.callback_query_handler(func=lambda c: c.data == "profile_quals")
def cb_profile_quals(c):
    uid = c.from_user.id
    if not has_quals_access(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа к Quals лиге", show_alert=True)
        return
    _priv_table_q = get_user_table(uid)
    p = get_player_from_table(uid, _priv_table_q) or get_player(uid)
    if not p:
        bot.answer_callback_query(c.id)
        return

    qs = get_player_quals_stats(uid, _priv_table_q)
    q_elo    = qs["elo"]    if qs else 1000
    q_wins   = qs["wins"]   if qs else 0
    q_losses = qs["losses"] if qs else 0
    q_kills  = qs["kills"]  if qs else 0
    q_deaths = qs["deaths"] if qs else 0
    q_assists= qs["assists"]if qs else 0

    premium  = has_active_premium(uid)
    crown    = " 👑 Premium" if premium else ""
    lvl      = get_faceit_level(q_elo)

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 Default профиль", callback_data="profile"),
        types.InlineKeyboardButton("👥 2v2 профиль",     callback_data="profile_duo"),
        types.InlineKeyboardButton("🔙 Назад",           callback_data="back"),
    )

    if CARDS_ENABLED:
        try:
            _priv_table_quals = get_user_table(uid)
            _matches_table_quals = get_user_matches_table(uid)
            quals_list = get_quals_players(_priv_table_quals)
            q_rank     = next((i+1 for i, row in enumerate(quals_list) if row[0] == uid), len(quals_list))
            lb_data    = [
                (i+1, row[1], row[2], has_active_premium(row[0]), is_admin(row[0]), is_verified_check(row[0]))
                for i, row in enumerate(quals_list[:3])
            ]
            quals_recent = get_player_quals_recent_matches(uid, limit=5, matches_table=_matches_table_quals)
            q_mvp_count  = p[31] if len(p) > 31 else 0
            avatar_bytes = get_user_avatar(uid)
            active_frame, active_banner, active_background = get_active_cosmetics(uid)
            img_buf = generate_profile_card(
                username          = p[1] or "Unknown",
                game_id           = p[2] or "",
                user_id           = p[0],
                elo               = q_elo,
                wins              = q_wins,
                losses            = q_losses,
                kills             = q_kills,
                deaths            = q_deaths,
                assists           = q_assists,
                is_premium        = premium,
                is_admin          = is_admin(uid),
                global_rank       = q_rank,
                league            = "QUALS",
                map_stats         = [],
                recent            = quals_recent,
                leaderboard       = lb_data,
                quals_stats       = None,
                mvp_count         = q_mvp_count,
                is_verified       = is_verified_check(uid),
                avatar_bytes      = avatar_bytes,
                active_frame      = active_frame,
                active_banner     = active_banner,
                active_background = active_background,
            )
            try:
                bot.delete_message(c.message.chat.id, c.message.message_id)
            except Exception:
                pass
            q_games = q_wins + q_losses
            q_wr    = round(q_wins / q_games * 100, 1) if q_games > 0 else 0.0
            caption = (
                f"⭐ <b>{p[1]}</b>{crown}  |  Quals ELO: <b>{q_elo}</b>  |  Lvl <b>{lvl}</b>\n"
                f"🏆 {q_wins}W · ❌ {q_losses}L · 📈 {q_wr}%  |  Rank #{q_rank}"
            )
            bot.send_photo(c.message.chat.id, img_buf,
                           caption=caption, reply_markup=kb, parse_mode="HTML")
            bot.answer_callback_query(c.id)
            return
        except Exception as e:
            send_error_log("card_profile (Quals)", e)

    # fallback text
    q_games = q_wins + q_losses
    q_wr    = round(q_wins / q_games * 100, 1) if q_games > 0 else 0.0
    q_kd    = round(q_kills / q_deaths, 2) if q_deaths > 0 else float(q_kills)
    text = (
        f"⭐ <b>{p[1]}</b> — Quals профиль\n\n"
        f"📊 Quals ELO: <b>{q_elo}</b>  |  Lvl <b>{lvl}</b>\n"
        f"🏆 {q_wins}W · ❌ {q_losses}L · 📈 {q_wr}%\n"
        f"🔫 K: {q_kills} · 💀 D: {q_deaths} · 🤝 A: {q_assists} · K/D: {q_kd}"
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


# ==================== 2v2 ПРОФИЛЬ ====================
@bot.callback_query_handler(func=lambda c: c.data == "profile_duo")
def cb_profile_duo(c):
    uid = c.from_user.id
    p = get_current_player(uid)
    if not p:
        bot.answer_callback_query(c.id)
        return

    _priv_table = get_user_table(uid)
    ds = get_player_duo_stats(uid, _priv_table)
    d_elo    = ds["elo"]     if ds else 1000
    d_wins   = ds["wins"]    if ds else 0
    d_losses = ds["losses"]  if ds else 0
    d_kills  = ds["kills"]   if ds else 0
    d_deaths = ds["deaths"]  if ds else 0
    d_assists= ds["assists"] if ds else 0

    premium = has_active_premium(uid)
    crown   = " 👑 Premium" if premium else ""
    lvl     = get_faceit_level(d_elo)

    kb = types.InlineKeyboardMarkup(row_width=1)
    nav = [types.InlineKeyboardButton("📊 Default профиль", callback_data="profile")]
    if has_quals_access(uid):
        nav.append(types.InlineKeyboardButton("⭐ Quals профиль", callback_data="profile_quals"))
    nav.append(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    kb.add(*nav)

    if CARDS_ENABLED:
        try:
            duo_list = get_duo_players(_priv_table)
            duo_list = [r for r in duo_list if (r[3] or 0) + (r[4] or 0) > 0]
            d_rank   = next((i+1 for i, r in enumerate(duo_list) if r[0] == uid), len(duo_list) or 1)
            lb_data  = [
                (i+1, r[1], r[2], has_active_premium(r[0]), is_admin(r[0]), is_verified_check(r[0]))
                for i, r in enumerate(duo_list[:3])
            ]
            _matches_table_duo = get_user_matches_table(uid)
            duo_recent  = get_player_duo_recent_matches(uid, limit=5, matches_table=_matches_table_duo)
            mvp_count   = p[31] if len(p) > 31 else 0
            avatar_bytes = get_user_avatar(uid)
            active_frame, active_banner, active_background = get_active_cosmetics(uid)

            img_buf = generate_profile_card(
                username          = p[1] or "Unknown",
                game_id           = p[2] or "",
                user_id           = p[0],
                elo               = d_elo,
                wins              = d_wins,
                losses            = d_losses,
                kills             = d_kills,
                deaths            = d_deaths,
                assists           = d_assists,
                is_premium        = premium,
                is_admin          = is_admin(uid),
                global_rank       = d_rank,
                league            = "2V2",
                map_stats         = get_player_duo_map_stats(uid, _matches_table_duo),
                recent            = duo_recent,
                leaderboard       = lb_data,
                quals_stats       = None,
                mvp_count         = mvp_count,
                is_verified       = is_verified_check(uid),
                duo_stats         = None,
                avatar_bytes      = avatar_bytes,
                active_frame      = active_frame,
                active_banner     = active_banner,
                active_background = active_background,
            )
            try:
                bot.delete_message(c.message.chat.id, c.message.message_id)
            except Exception:
                pass
            d_games = d_wins + d_losses
            d_wr    = round(d_wins / d_games * 100, 1) if d_games > 0 else 0.0
            caption = (
                f"👥 <b>{p[1]}</b>{crown}  |  2v2 ELO: <b>{d_elo}</b>  |  Lvl <b>{lvl}</b>\n"
                f"🏆 {d_wins}W · ❌ {d_losses}L · 📈 {d_wr}%  |  Rank #{d_rank}"
            )
            bot.send_photo(c.message.chat.id, img_buf,
                           caption=caption, reply_markup=kb, parse_mode="HTML")
            bot.answer_callback_query(c.id)
            return
        except Exception as e:
            send_error_log("card_profile (2v2)", e)

    # fallback text
    d_games = d_wins + d_losses
    d_wr    = round(d_wins / d_games * 100, 1) if d_games > 0 else 0.0
    d_kd    = round(d_kills / d_deaths, 2) if d_deaths > 0 else float(d_kills)
    text = (
        f"👥 <b>{p[1]}</b> — 2v2 профиль\n\n"
        f"📊 2v2 ELO: <b>{d_elo}</b>  |  Lvl <b>{lvl}</b>\n"
        f"🏆 {d_wins}W · ❌ {d_losses}L · 📈 {d_wr}%\n"
        f"🔫 K: {d_kills} · 💀 D: {d_deaths} · 🤝 A: {d_assists} · K/D: {d_kd}"
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


# ==================== ТОП (меню выбора) ====================
@bot.callback_query_handler(func=lambda c: c.data == "top")
def cb_top(c):
    uid = c.from_user.id
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 Default — Топ по ELO",      callback_data="top_default"),
        types.InlineKeyboardButton("⭐ Quals — Топ квалификации",  callback_data="top_quals"),
        types.InlineKeyboardButton("👥 2v2 — Топ дуэлей",          callback_data="top_2v2"),
    )
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    try:
        bot.edit_message_text("🏆 <b>Выберите таблицу лидеров:</b>", c.message.chat.id,
                              c.message.message_id, reply_markup=kb, parse_mode="HTML")
    except Exception:
        try:
            bot.delete_message(c.message.chat.id, c.message.message_id)
        except Exception:
            pass
        bot.send_message(c.message.chat.id, "🏆 <b>Выберите таблицу лидеров:</b>",
                         reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "top_default")
def cb_top_default(c):
    uid = c.from_user.id
    priv_table   = get_user_table(uid)
    priv_display = get_user_private_display(uid)
    players = get_all_players(priv_table)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("⭐ Quals топ", callback_data="top_quals"),
        types.InlineKeyboardButton("👥 2v2 топ",   callback_data="top_2v2"),
        types.InlineKeyboardButton("🔙 Назад",     callback_data="back"),
    )
    if not players:
        bot.edit_message_text(f"🏆 <b>ТОП {priv_display}</b>\n\nИгроков нет.", c.message.chat.id,
                              c.message.message_id, reply_markup=kb, parse_mode="HTML")
        bot.answer_callback_query(c.id)
        return
    if CARDS_ENABLED:
        try:
            lb_players = []
            for i, p in enumerate(players[:10], 1):
                uid2, name, elo, wins, losses, kills, deaths, coins, banned, warns = p
                kd  = round(kills / deaths, 2) if deaths > 0 else float(kills)
                lvl = get_faceit_level(elo)
                lb_players.append({
                    "rank": i, "name": name, "elo": elo, "wins": wins,
                    "losses": losses, "kd": kd, "level": lvl, "uid": uid2,
                    "is_premium": has_active_premium(uid2), "is_admin": is_admin(uid2),
                    "is_verified": is_verified_check(uid2),
                })
            avatars = {}
            for _p2 in players[:10]:
                _av = get_user_avatar(_p2[0])
                if _av:
                    avatars[_p2[0]] = _av
            img_buf = generate_leaderboard_card(
                lb_players, title=f"📊 {priv_display} DEFAULT — TOP ELO", avatars=avatars
            )
            try:
                bot.delete_message(c.message.chat.id, c.message.message_id)
            except Exception:
                pass
            bot.send_photo(c.message.chat.id, img_buf,
                           caption="🏆 <b>ТОП ИГРОКОВ ПО ELO</b>",
                           reply_markup=kb, parse_mode="HTML")
            bot.answer_callback_query(c.id)
            return
        except Exception as e:
            print(f"[card_top_default] error: {e}")
    # fallback text
    text   = f"🏆 <b>ТОП {priv_display} ПО ELO</b>\n\n"
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, p in enumerate(players[:10], 1):
        uid2, name, elo, wins, losses, kills, deaths, coins, banned, warns = p
        games   = wins + losses
        winrate = round(wins / games * 100, 1) if games > 0 else 0
        kd      = round(kills / deaths, 2) if deaths > 0 else kills
        lvl     = get_faceit_level(elo)
        prem    = " 👑" if has_active_premium(uid2) else ""
        text   += f"{medals.get(i, f'{i}.')} <b>{name}</b>{prem} [Lvl {lvl}]\n   ELO: {elo} | {wins}W/{losses}L ({winrate}%) | K/D: {kd}\n\n"
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                              reply_markup=kb, parse_mode="HTML")
    except Exception:
        try:
            bot.delete_message(c.message.chat.id, c.message.message_id)
        except Exception:
            pass
        bot.send_message(c.message.chat.id, text, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "top_quals")
def cb_top_quals(c):
    uid = c.from_user.id
    priv_table   = get_user_table(uid)
    priv_display = get_user_private_display(uid)
    players = get_quals_players(priv_table)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 Default топ", callback_data="top_default"),
        types.InlineKeyboardButton("👥 2v2 топ",     callback_data="top_2v2"),
        types.InlineKeyboardButton("🔙 Назад",        callback_data="back"),
    )
    if not players:
        bot.edit_message_text(
            f"⭐ <b>QUALS ТОП {priv_display}</b>\n\nНет игроков с доступом к Quals.",
            c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML"
        )
        bot.answer_callback_query(c.id)
        return
    if CARDS_ENABLED:
        try:
            lb_players = []
            for i, row in enumerate(players[:10], 1):
                uid2, name, qelo, qw, ql, qk, qd, qa = row
                qkd = round(qk / qd, 2) if qd > 0 else float(qk)
                lvl = get_faceit_level(qelo or 1000)
                lb_players.append({
                    "rank": i, "name": name, "elo": qelo or 1000,
                    "wins": qw or 0, "losses": ql or 0, "kd": qkd, "level": lvl, "uid": uid2,
                    "is_premium": has_active_premium(uid2), "is_admin": is_admin(uid2),
                    "is_verified": is_verified_check(uid2),
                })
            q_avatars = {}
            for _qrow in players[:10]:
                _qav = get_user_avatar(_qrow[0])
                if _qav:
                    q_avatars[_qrow[0]] = _qav
            img_buf = generate_leaderboard_card(
                lb_players, title=f"⭐ {priv_display} QUALS — TOP ELO", avatars=q_avatars
            )
            try:
                bot.delete_message(c.message.chat.id, c.message.message_id)
            except Exception:
                pass
            bot.send_photo(c.message.chat.id, img_buf,
                           caption="⭐ <b>QUALS ТОП ИГРОКОВ</b>",
                           reply_markup=kb, parse_mode="HTML")
            bot.answer_callback_query(c.id)
            return
        except Exception as e:
            print(f"[card_top_quals] error: {e}")
    # fallback text
    text   = f"⭐ <b>QUALS ТОП {priv_display}</b>\n\n"
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, row in enumerate(players[:10], 1):
        uid2, name, qelo, qw, ql, qk, qd, qa = row
        games   = (qw or 0) + (ql or 0)
        winrate = round((qw or 0) / games * 100, 1) if games > 0 else 0
        kd      = round((qk or 0) / (qd or 1), 2)
        prem    = " 👑" if has_active_premium(uid2) else ""
        text   += f"{medals.get(i, f'{i}.')} <b>{name}</b>{prem}\n   Q.ELO: {qelo or 1000} | {qw}W/{ql}L ({winrate}%) | K/D: {kd}\n\n"
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                              reply_markup=kb, parse_mode="HTML")
    except Exception:
        try:
            bot.delete_message(c.message.chat.id, c.message.message_id)
        except Exception:
            pass
        bot.send_message(c.message.chat.id, text, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "top_2v2")
def cb_top_2v2(c):
    uid = c.from_user.id
    priv_table   = get_user_table(uid)
    priv_display = get_user_private_display(uid)
    players = get_duo_players(priv_table)
    # Фильтруем только тех, кто сыграл хотя бы 1 матч в 2v2
    players = [p for p in players if (p[3] or 0) + (p[4] or 0) > 0]
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 Default топ", callback_data="top_default"),
        types.InlineKeyboardButton("⭐ Quals топ",   callback_data="top_quals"),
        types.InlineKeyboardButton("🔙 Назад",        callback_data="back"),
    )
    if not players:
        try:
            bot.edit_message_text(
                f"👥 <b>2v2 ТОП {priv_display}</b>\n\nЕщё нет 2v2 матчей.",
                c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(c.message.chat.id,
                f"👥 <b>2v2 ТОП {priv_display}</b>\n\nЕщё нет 2v2 матчей.",
                reply_markup=kb, parse_mode="HTML")
        bot.answer_callback_query(c.id)
        return

    if CARDS_ENABLED:
        try:
            lb_players = []
            for i, row in enumerate(players[:10], 1):
                uid2, name, delo, dw, dl, dk, dd, da = row
                lb_players.append({
                    "rank":       i,
                    "name":       name or "Unknown",
                    "elo":        delo or 1000,
                    "wins":       dw or 0,
                    "losses":     dl or 0,
                    "kills":      dk or 0,
                    "deaths":     dd or 0,
                    "uid":        uid2,
                    "is_premium": has_active_premium(uid2),
                    "is_admin":   is_admin(uid2),
                })
            duo_avatars = {}
            for _drow in players[:10]:
                _dav = get_user_avatar(_drow[0])
                if _dav:
                    duo_avatars[_drow[0]] = _dav
            img_buf = generate_duo_leaderboard_card(
                lb_players,
                title=f"👥 {priv_display} 2v2 — TOP ELO",
                avatars=duo_avatars,
            )
            try:
                bot.delete_message(c.message.chat.id, c.message.message_id)
            except Exception:
                pass
            bot.send_photo(
                c.message.chat.id,
                img_buf,
                caption=f"👥 <b>2v2 ТОП {priv_display}</b>",
                reply_markup=kb,
                parse_mode="HTML",
            )
            bot.answer_callback_query(c.id)
            return
        except Exception as e:
            print(f"[card_top_2v2] error: {e}")

    text   = f"👥 <b>2v2 ТОП {priv_display}</b>\n\n"
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, row in enumerate(players[:10], 1):
        uid2, name, delo, dw, dl, dk, dd, da = row
        games   = (dw or 0) + (dl or 0)
        winrate = round((dw or 0) / games * 100, 1) if games > 0 else 0
        kd      = round((dk or 0) / (dd or 1), 2)
        prem    = " 👑" if has_active_premium(uid2) else ""
        text   += f"{medals.get(i, f'{i}.')} <b>{name}</b>{prem}\n   2v2 ELO: {delo or 1000} | {dw}W/{dl}L ({winrate}%) | K/D: {kd}\n\n"
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                              reply_markup=kb, parse_mode="HTML")
    except Exception:
        try:
            bot.delete_message(c.message.chat.id, c.message.message_id)
        except Exception:
            pass
        bot.send_message(c.message.chat.id, text, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


# ==================== НАЗАД ====================
@bot.callback_query_handler(func=lambda c: c.data == "back")
def cb_back(c):
    uid = c.from_user.id
    try:
        # Если сообщение — текст, редактируем его
        bot.edit_message_text(main_menu_text(uid), c.message.chat.id, c.message.message_id,
                              reply_markup=main_menu(uid), parse_mode="HTML")
    except Exception:
        # Если сообщение — фото (карточка профиля/топа), удаляем и шлём новое
        try:
            bot.delete_message(c.message.chat.id, c.message.message_id)
        except Exception:
            pass
        bot.send_message(c.message.chat.id, main_menu_text(uid),
                         reply_markup=main_menu(uid), parse_mode="HTML")
    bot.answer_callback_query(c.id)


# ==================== ЛОББИ ====================
def get_duo_elo_for_player(uid, table="players"):
    """Явно запрашивает duo_elo по имени колонки, без привязки к индексу SELECT *."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(f"SELECT duo_elo FROM {table} WHERE user_id=%s", (uid,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else 1000
    except Exception:
        return 1000

def get_quals_elo_for_player(uid, table="players"):
    """Явно запрашивает quals_elo по имени колонки, без привязки к индексу SELECT *."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(f"SELECT quals_elo FROM {table} WHERE user_id=%s", (uid,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else 1000
    except Exception:
        return 1000

def build_lobby_text(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return ""
    parts = lobby_id.split("_")
    # Format: private_league_device_slot
    if len(parts) >= 4:
        private_key, league, device, slot = parts[0], parts[1], parts[2], parts[3]
    else:
        private_key, league, device, slot = "darling", parts[0], parts[1], parts[2]
    priv_cfg  = PRIVATE_CONFIG.get(private_key, PRIVATE_CONFIG["darling"])
    priv_label = f"{priv_cfg['emoji']} {priv_cfg['display']}"
    text = (
        f"🎮 <b>Лобби #{slot} ({priv_label} / {league.upper()}/{device.upper()})</b>\n"
        f"👥 Игроков: {len(lobby['players'])}/{_lobby_max_size(league)}\n\n"
    )
    is_quals = (league == "quals")
    is_duo   = (league == "2v2")
    priv_table_name = priv_cfg["table"]
    for i, pid in enumerate(lobby["players"], 1):
        p = get_player_from_table(pid, priv_table_name) or get_player(pid)
        if p:
            icon = "🤖" if p[13] else "👤"
            prem = " 👑" if (not p[13] and has_active_premium(pid)) else ""
            if is_quals and not p[13]:
                display_elo = get_quals_elo_for_player(pid, priv_table_name)
            elif is_duo and not p[13]:
                display_elo = get_duo_elo_for_player(pid, priv_table_name)
            else:
                display_elo = p[4]
            text += f"{i}. {icon} {p[1]}{prem} [Lvl {get_faceit_level(display_elo)} | {display_elo} ELO]\n"
        else:
            text += f"{i}. {pid}\n"
    return text

def build_lobby_kb(lobby_id, uid):
    parts = lobby_id.split("_")
    # Format: private_league_device_slot
    league = parts[1] if len(parts) >= 4 else parts[0]
    kb = types.InlineKeyboardMarkup()
    lobby = active_lobbies.get(lobby_id)
    # Скрываем кнопку выхода во время фазы принятия
    if not lobby or lobby.get("status") != "accepting":
        kb.add(types.InlineKeyboardButton("🚪 Выйти из лобби", callback_data=f"leave_{lobby_id}"))
    kb.add(types.InlineKeyboardButton("🔙 К списку", callback_data=f"lobby_{league}"))
    return kb

def broadcast_lobby_update(lobby_id, exclude_uid=None):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return
    text = build_lobby_text(lobby_id)
    for pid, (cid, mid) in list(lobby_player_messages.get(lobby_id, {}).items()):
        if pid == exclude_uid or pid not in lobby.get("players", []):
            continue
        try:
            bot.edit_message_text(text, cid, mid, reply_markup=build_lobby_kb(lobby_id, pid))
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data == "find")
def cb_find(c):
    uid = c.from_user.id
    err = check_blocked(uid)
    if err:
        bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎮 Default", callback_data="lobby_default"),
        types.InlineKeyboardButton("⭐ Quals", callback_data="lobby_quals"),
        types.InlineKeyboardButton("👥 2v2", callback_data="lobby_2v2"),
        types.InlineKeyboardButton("🔙 Назад", callback_data="back"),
    )
    bot.edit_message_text("🎮 Выбери лигу:", c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("lobby_") and len(c.data.split("_")) == 2)
def cb_lobby(c):
    uid = c.from_user.id
    league = c.data.split("_")[1]
    private_key  = get_user_private(uid)
    priv_display = get_user_private_display(uid)
    if league == "quals" and not has_quals_access(uid):
        bot.answer_callback_query(c.id, "⭐ Доступ к QUALS закрыт!", show_alert=True)
        return
    max_size = _lobby_max_size(league)
    text = f"🎮 <b>ЛОББИ {priv_display} — {league.upper()}</b>\n\nPC и Mobile могут играть вместе\n\n"
    kb = types.InlineKeyboardMarkup(row_width=2)
    for slot in range(1, 6):
        m_cnt = len(active_lobbies.get(f"{private_key}_{league}_mobile_{slot}", {}).get("players", []))
        p_cnt = len(active_lobbies.get(f"{private_key}_{league}_pc_{slot}", {}).get("players", []))
        text += f"Лобби #{slot}: Mobile({m_cnt}/{max_size}) | PC({p_cnt}/{max_size})\n"
    for slot in range(1, 6):
        m_cnt = len(active_lobbies.get(f"{private_key}_{league}_mobile_{slot}", {}).get("players", []))
        p_cnt = len(active_lobbies.get(f"{private_key}_{league}_pc_{slot}", {}).get("players", []))
        kb.add(
            types.InlineKeyboardButton(f"M{slot}({m_cnt})", callback_data=f"join_{private_key}_{league}_mobile_{slot}"),
            types.InlineKeyboardButton(f"P{slot}({p_cnt})", callback_data=f"join_{private_key}_{league}_pc_{slot}"),
        )
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="find"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("join_"))
def cb_join(c):
    try:
        parts = c.data.split("_")
        if len(parts) < 5:
            bot.answer_callback_query(c.id, "❌ Ошибка формата")
            return
        private_key, league, device, slot = parts[1], parts[2], parts[3], int(parts[4])
        uid = c.from_user.id
        if c.from_user.username:
            update_tg_username(uid, c.from_user.username)
        err = check_blocked(uid)
        if err:
            bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
            return
        if league == "quals" and not has_quals_access(uid):
            bot.answer_callback_query(c.id, "⭐ Доступ к QUALS закрыт!", show_alert=True)
            return
        if not is_registered(uid):
            bot.answer_callback_query(c.id, "❌ Вы не зарегистрированы! Напишите /start")
            return
        if is_muted_check(uid):
            mins = get_mute_remaining(uid) // 60
            bot.answer_callback_query(c.id, f"🔇 Вы замучены! Осталось: {mins} мин.", show_alert=True)
            return
        lobby_id = f"{private_key}_{league}_{device}_{slot}"
        old = user_lobby.get(uid)
        if old and old in active_lobbies and uid in active_lobbies[old].get("players", []):
            active_lobbies[old]["players"].remove(uid)
            lobby_player_messages.get(old, {}).pop(uid, None)
            if not active_lobbies[old]["players"]:
                del active_lobbies[old]
                lobby_player_messages.pop(old, None)
            else:
                broadcast_lobby_update(old)
            user_lobby.pop(uid, None)
        if lobby_id not in active_lobbies:
            active_lobbies[lobby_id] = {"players": [], "league": league, "device": device, "slot": slot, "status": "waiting", "private": private_key}
        lobby = active_lobbies[lobby_id]
        if lobby["status"] != "waiting":
            bot.answer_callback_query(c.id, "❌ Лобби уже в игре!", show_alert=True)
            return
        if len(lobby["players"]) >= _lobby_max_size(league):
            bot.answer_callback_query(c.id, "❌ Лобби полное!", show_alert=True)
            return
        if uid in lobby["players"]:
            bot.answer_callback_query(c.id, "✅ Вы уже в этом лобби!")
            return
        party_obj = get_party_of(uid)
        party_leader = party_obj["leader"] if party_obj else None

        # Проверка: если в пати есть участники с другой приваткой — предупредить
        if party_obj and len(party_obj["members"]) > 1:
            different_priv_members = []
            for m in party_obj["members"]:
                if m != uid and get_user_private(m) != private_key:
                    mp = get_player(m)
                    mname = mp[1] if mp else str(m)
                    different_priv_members.append(mname)
            if different_priv_members:
                bot.answer_callback_query(
                    c.id,
                    f"⚠️ Члены пати играют в другой приватке! Попросите их сменить приватку: {', '.join(different_priv_members)}",
                    show_alert=True,
                )
                return

        # Пати больше не тянет участников автоматически — каждый заходит сам
        lobby["players"].append(uid)
        user_lobby[uid] = lobby_id
        if lobby_player_messages.get(lobby_id) is None:
            lobby_player_messages[lobby_id] = {}
        text = build_lobby_text(lobby_id)
        kb = build_lobby_kb(lobby_id, uid)
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
            lobby_player_messages[lobby_id][uid] = (c.message.chat.id, c.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(c.id, f"✅ Вы вошли в лобби #{slot}!")
        broadcast_lobby_update(lobby_id, exclude_uid=uid)
        if len(lobby["players"]) >= _lobby_max_size(league):
            start_accept_phase(lobby_id)
    except Exception as e:
        print(f"Join error: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")


@bot.callback_query_handler(func=lambda c: c.data.startswith("leave_"))
def cb_leave(c):
    uid = c.from_user.id
    lobby_id = c.data.split("leave_", 1)[1]
    lobby = active_lobbies.get(lobby_id)
    if lobby and lobby.get("status") == "accepting":
        bot.answer_callback_query(c.id, "❌ Нельзя выйти во время принятия матча!", show_alert=True)
        return
    if lobby and uid in lobby.get("players", []):
        lobby["players"].remove(uid)
        lobby_player_messages.get(lobby_id, {}).pop(uid, None)
        if not lobby["players"]:
            del active_lobbies[lobby_id]
            lobby_player_messages.pop(lobby_id, None)
        else:
            broadcast_lobby_update(lobby_id)
        user_lobby.pop(uid, None)
        bot.answer_callback_query(c.id, "✅ Вы вышли из лобби")
        bot.edit_message_text(main_menu_text(uid), c.message.chat.id, c.message.message_id, reply_markup=main_menu(uid), parse_mode="HTML")
    else:
        # Игрок не найден в лобби (лобби удалено или игрок уже вышел)
        # Всё равно чистим зависший user_lobby
        user_lobby.pop(uid, None)
        bot.answer_callback_query(c.id, "❌ Лобби недоступно")
        try:
            bot.edit_message_text(main_menu_text(uid), c.message.chat.id, c.message.message_id, reply_markup=main_menu(uid), parse_mode="HTML")
        except Exception:
            pass


# ==================== ФАЗА ПРИНЯТИЯ ====================
def build_accept_text(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return ""
    accepted = lobby.get("accepted", [])
    real_players = [u for u in lobby["players"] if not is_bot_player(u)]
    text = "🔔 <b>Матч найден! Статус принятия:</b>\n\n"
    for u in real_players:
        p = get_player_in_lobby(u, lobby)
        name = p[1] if p else str(u)
        prem = " 👑" if has_active_premium(u) else ""
        icon = "✅" if u in accepted else "⏳"
        text += f"{icon} {name}{prem}\n"
    accepted_cnt = len([u for u in accepted if not is_bot_player(u)])
    text += f"\n<b>{accepted_cnt}/{len(real_players)}</b> приняли"
    return text

def update_accept_status(lobby_id):
    msgs = accept_status_messages.get(lobby_id, {})
    if not msgs:
        return
    text = build_accept_text(lobby_id)
    for uid, (cid, mid) in list(msgs.items()):
        try:
            bot.edit_message_text(text, cid, mid)
        except Exception:
            pass

def delete_accept_status(lobby_id):
    msgs = accept_status_messages.pop(lobby_id, {})
    for uid, (cid, mid) in msgs.items():
        try:
            bot.delete_message(cid, mid)
        except Exception:
            pass

def delete_match_found(lobby_id):
    msgs = match_found_messages.pop(lobby_id, {})
    for uid, (cid, mid) in msgs.items():
        try:
            bot.delete_message(cid, mid)
        except Exception:
            pass

def delete_ban_status(lobby_id):
    msgs = ban_status_messages.pop(lobby_id, {})
    for uid, (cid, mid) in msgs.items():
        try:
            bot.delete_message(cid, mid)
        except Exception:
            pass

def start_accept_phase(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return
    lobby["status"] = "accepting"
    lobby["accepted"] = []
    lobby_player_messages.pop(lobby_id, None)
    accept_status_messages[lobby_id] = {}
    match_found_messages[lobby_id] = {}
    for uid in lobby["players"]:
        if is_bot_player(uid):
            lobby["accepted"].append(uid)
            continue
        try:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("✅ Принять матч", callback_data=f"accept_{lobby_id}"))
            sent = bot.send_message(
                uid,
                f"🔔 <b>Матч найден!</b>\n\n"
                f"🏷 Лига: {format_league(lobby.get('league','default'))}\n📱 Устройство: {lobby.get('device','').upper()}\n\n"
                f"⏱ У вас <b>{ACCEPT_TIMEOUT} секунд</b> чтобы принять.\nПри непринятии — предупреждение ⚠️",
                reply_markup=kb,
            )
            match_found_messages[lobby_id][uid] = (sent.chat.id, sent.message_id)
        except Exception:
            pass
    for uid in lobby["players"]:
        if is_bot_player(uid):
            continue
        try:
            text = build_accept_text(lobby_id)
            sent = bot.send_message(uid, text)
            accept_status_messages[lobby_id][uid] = (sent.chat.id, sent.message_id)
        except Exception:
            pass

    def check_accept():
        time.sleep(ACCEPT_TIMEOUT)
        lobby2 = active_lobbies.get(lobby_id)
        if not lobby2 or lobby2["status"] != "accepting":
            return
        not_accepted = [u for u in lobby2["players"] if u not in lobby2.get("accepted", []) and not is_bot_player(u)]
        delete_accept_status(lobby_id)
        delete_match_found(lobby_id)
        if not not_accepted:
            lobby2["status"] = "pre_mapban"
            threading.Thread(target=start_map_ban_phase, args=(lobby_id,), daemon=True).start()
            return
        for uid in not_accepted:
            warns = add_warn_to_player(uid)
            lobby2["players"].remove(uid)
            user_lobby.pop(uid, None)
            lobby_player_messages.get(lobby_id, {}).pop(uid, None)
            accept_status_messages.get(lobby_id, {}).pop(uid, None)
            try:
                if warns >= 3:
                    until = apply_mute(uid, hours=2)
                    dt = fmt_dt(until)
                    # Сбрасываем счётчик варнов после авто-мута
                    conn_r = _db(); cur_r = conn_r.cursor()
                    cur_r.execute("UPDATE players SET warns=0 WHERE user_id=%s", (uid,))
                    conn_r.commit(); conn_r.close()
                    prow = get_player(uid)
                    pname = prow[1] if prow else str(uid)
                    bot.send_message(uid,
                        f"⚠️ <b>Варн {warns}/3</b> за непринятие матча.\n"
                        f"🔇 Система выдала мут на 2 часа (до {dt}).\n"
                        f"⚠️ Счётчик варнов сброшен.\n❌ Вы исключены из лобби.",
                        parse_mode="HTML")
                    send_punishment_log(
                        f"🔇 <b>Авто-мут (система)</b>\n"
                        f"👤 Игрок: {tg_link(uid, pname)}\n"
                        f"📝 Причина: 3 варна за непринятие матча\n"
                        f"⏰ До: {dt}\n"
                        f"⚠️ Варны сброшены до 0"
                    )  # system auto-action, no admin uid — keep plain log
                else:
                    bot.send_message(uid, f"⚠️ Варн {warns}/3 за непринятие матча.\n❌ Вы исключены из лобби.")
                    try:
                        prow2 = get_player(uid)
                        pname2 = prow2[1] if prow2 else str(uid)
                        send_punishment_log(
                            f"⚠️ <b>Авто-варн (непринятие матча)</b>\n"
                            f"👤 Игрок: {tg_link(uid, pname2)}\n"
                            f"⚠️ Варны: {warns}/3\n"
                            f"📝 Причина: не принял матч за {ACCEPT_TIMEOUT} сек"
                        )
                    except Exception:
                        pass
            except Exception:
                pass
        _max_sz = _lobby_max_size(lobby2.get("league", "default"))
        if len(lobby2["players"]) >= _max_sz:
            lobby2["status"] = "pre_mapban"
            threading.Thread(target=start_map_ban_phase, args=(lobby_id,), daemon=True).start()
        elif not lobby2["players"]:
            lobby_player_messages.pop(lobby_id, None)
            ban_status_messages.pop(lobby_id, None)
            del active_lobbies[lobby_id]
        else:
            lobby2["status"] = "waiting"
            lobby2.pop("accepted", None)
            if lobby_player_messages.get(lobby_id) is None:
                lobby_player_messages[lobby_id] = {}
            lobby_text = build_lobby_text(lobby_id)
            cnt = len(lobby2["players"])
            for uid in lobby2["players"]:
                if is_bot_player(uid):
                    continue
                try:
                    bot.send_message(
                        uid,
                        f"⚠️ Игрок не принял матч и исключён.\n"
                        f"Вы остаётесь в очереди ({cnt}/{_max_sz}).",
                    )
                except Exception:
                    pass
                try:
                    kb = build_lobby_kb(lobby_id, uid)
                    sent = bot.send_message(uid, lobby_text, reply_markup=kb)
                    lobby_player_messages[lobby_id][uid] = (sent.chat.id, sent.message_id)
                except Exception:
                    pass

    threading.Thread(target=check_accept, daemon=True).start()


@bot.callback_query_handler(func=lambda c: c.data.startswith("accept_"))
def cb_accept(c):
    uid = c.from_user.id
    lobby_id = c.data.split("accept_", 1)[1]
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby["status"] not in ("accepting",):
        bot.answer_callback_query(c.id, "❌ Матч уже недоступен")
        return
    if uid not in lobby.get("accepted", []):
        lobby["accepted"].append(uid)
    bot.answer_callback_query(c.id, "✅ Принято!")
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        try:
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        except Exception:
            pass
    update_accept_status(lobby_id)
    if len(lobby["accepted"]) >= len(lobby["players"]) and lobby["status"] == "accepting":
        lobby["status"] = "pre_mapban"
        threading.Thread(target=start_map_ban_phase, args=(lobby_id,), daemon=True).start()


# ==================== БАН КАРТ ====================
def build_ban_status_text(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return ""
    ct_p = get_player_in_lobby(lobby.get("ct_captain"), lobby) if lobby.get("ct_captain") else None
    t_p  = get_player_in_lobby(lobby.get("t_captain"),  lobby) if lobby.get("t_captain")  else None
    ct_name = ct_p[1] if ct_p else "CT капитан"
    t_name  = t_p[1]  if t_p  else "T капитан"
    bans = lobby.get("map_bans", [])
    remaining = lobby.get("maps_remaining", [])
    turn = lobby.get("ban_turn", "ct")
    lines = [f"🗺 <b>Бан карт</b>", "", f"💙 CT: <b>{ct_name}</b>", f"🧡 T: <b>{t_name}</b>", ""]
    if bans:
        lines.append("🚫 <b>Забанено:</b>")
        for b in bans:
            lines.append(f"  {'💙' if b['team']=='ct' else '🧡'} {b['map']}")
        lines.append("")
    if remaining:
        lines.append("✅ <b>Остались:</b>")
        for m in remaining:
            lines.append(f"  • {m}")
        lines.append("")
        turn_name = ct_name if turn == "ct" else t_name
        lines.append(f"⏳ Ход: {'💙' if turn=='ct' else '🧡'} <b>{turn_name}</b>")
    else:
        lines.append(f"🗺 <b>Карта выбрана: {lobby.get('map_name', '?')}</b>")
    return "\n".join(lines)

def send_ban_status_to_all(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return
    text = build_ban_status_text(lobby_id)
    if ban_status_messages.get(lobby_id) is None:
        ban_status_messages[lobby_id] = {}
    for uid in lobby["players"]:
        if is_bot_player(uid):
            continue
        existing = ban_status_messages[lobby_id].get(uid)
        if existing:
            cid, mid = existing
            try:
                bot.edit_message_text(text, cid, mid)
                continue
            except Exception:
                pass
        try:
            sent = bot.send_message(uid, text)
            ban_status_messages[lobby_id][uid] = (sent.chat.id, sent.message_id)
        except Exception:
            pass

def delete_lobby_messages(lobby_id):
    msgs = lobby_player_messages.pop(lobby_id, {})
    for uid, (cid, mid) in msgs.items():
        try:
            bot.delete_message(cid, mid)
        except Exception:
            pass

BAN_TURN_TIMEOUT = 40    # секунды на бан карты для живого капитана
DRAFT_TURN_TIMEOUT = 45  # секунды на выбор игрока в драфте

def _assign_preliminary_teams_for_captain(players, team_sz):
    """Preliminary team split for captain selection.
    Spreads distinct party groups across CT/T so 2 parties don't end up in the same team.
    """
    player_set = set(players)
    placed = set()
    party_groups = []
    solos = []
    for uid in players:
        if uid in placed:
            continue
        party = get_party_of(uid)
        if party and len(party["members"]) > 1:
            grp = [m for m in party["members"] if m in player_set and m not in placed]
            if len(grp) > 1:
                party_groups.append(grp)
                for m in grp:
                    placed.add(m)
                continue
        solos.append(uid)
        placed.add(uid)

    random.shuffle(solos)
    ct_team, t_team = [], []

    # Alternate parties between CT and T to avoid both parties landing in one team
    for i, grp in enumerate(party_groups):
        dest = ct_team if i % 2 == 0 else t_team
        other = t_team if dest is ct_team else ct_team
        if len(dest) + len(grp) <= team_sz:
            dest.extend(grp)
        elif len(other) + len(grp) <= team_sz:
            other.extend(grp)
        else:
            for m in grp:
                if len(ct_team) < team_sz:
                    ct_team.append(m)
                elif len(t_team) < team_sz:
                    t_team.append(m)

    for uid in solos:
        if len(ct_team) < team_sz:
            ct_team.append(uid)
        elif len(t_team) < team_sz:
            t_team.append(uid)

    return ct_team, t_team


def start_map_ban_phase(lobby_id):
    try:
        _start_map_ban_phase_inner(lobby_id)
    except Exception as _exc:
        print(f"[start_map_ban_phase] ОШИБКА lobby={lobby_id}: {_exc}")
        import traceback; traceback.print_exc()

def _start_map_ban_phase_inner(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        print(f"[mapban] lobby {lobby_id} не найдено")
        return
    if lobby["status"] not in ("accepting", "pre_mapban"):
        print(f"[mapban] lobby {lobby_id} неверный статус: {lobby['status']}")
        return

    league = lobby.get("league", "default")
    team_sz = _lobby_team_size(league)

    print(f"[mapban] lobby={lobby_id} league={league} team_sz={team_sz} players={len(lobby['players'])}")

    lobby["status"] = "mapban"
    lobby["maps_remaining"] = _get_lobby_maps(lobby)
    lobby["map_bans"] = []
    lobby["ban_turn"] = "ct"
    lobby["ban_count"] = 0
    players = lobby["players"]
    max_sz = _lobby_max_size(league)  # 4 для 2v2, 10 для остальных

    print(f"[mapban] карты: {lobby['maps_remaining']}")

    delete_match_found(lobby_id)
    delete_accept_status(lobby_id)
    delete_lobby_messages(lobby_id)

    # Группируем игроков так, чтобы участники одной пати стояли рядом —
    # это гарантирует что пати не окажется разбита по разным половинам при сплите.
    def _group_by_party(player_list):
        placed = set()
        grouped = []
        player_set = set(player_list)
        for uid in player_list:
            if uid in placed:
                continue
            party_obj = get_party_of(uid)
            if party_obj and len(party_obj["members"]) > 1:
                grp = [m for m in party_obj["members"] if m in player_set and m not in placed]
                grouped.extend(grp)
                for m in grp:
                    placed.add(m)
            else:
                grouped.append(uid)
                placed.add(uid)
        return grouped

    ct_team, t_team = _assign_preliminary_teams_for_captain(players, team_sz)
    lobby["ct_captain"] = pick_captain(ct_team)
    lobby["t_captain"]  = pick_captain(t_team)

    print(f"[mapban] ct_cap={lobby['ct_captain']} t_cap={lobby['t_captain']}")

    # Сообщение всем игрокам о старте фазы бана (трекаем для последующего удаления)
    ban_notify_messages[lobby_id] = {}
    for _uid in players:
        if is_bot_player(_uid):
            continue
        try:
            _sent = bot.send_message(
                _uid,
                "🗺 <b>Фаза бана карт началась!</b>\nОжидайте хода капитана...",
                parse_mode="HTML",
            )
            ban_notify_messages[lobby_id][_uid] = (_sent.chat.id, _sent.message_id)
        except Exception:
            pass

    send_ban_status_to_all(lobby_id)
    _do_ban_turn(lobby_id)


def _do_ban_turn(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby["status"] != "mapban":
        print(f"[_do_ban_turn] пропуск: lobby={lobby_id} статус={lobby.get('status') if lobby else 'нет'}")
        return

    turn      = lobby["ban_turn"]
    ban_count = lobby.get("ban_count", 0)  # снимок — для защиты от повторного срабатывания таймера
    captain_uid = lobby["ct_captain"] if turn == "ct" else lobby["t_captain"]

    print(f"[_do_ban_turn] lobby={lobby_id} turn={turn} captain={captain_uid} ban_count={ban_count} remaining={lobby.get('maps_remaining')}")

    # Если капитан не определён — авто-баним через 1 сек
    if captain_uid is None:
        def _auto_ban_null():
            time.sleep(1)
            lobby2 = active_lobbies.get(lobby_id)
            if not lobby2 or lobby2["status"] != "mapban" or not lobby2.get("maps_remaining"):
                return
            _apply_ban(lobby_id, -1, random.choice(lobby2["maps_remaining"]))
        threading.Thread(target=_auto_ban_null, daemon=True).start()
        return

    if is_bot_player(captain_uid):
        def bot_auto_ban():
            time.sleep(random.uniform(3, 5))
            lobby2 = active_lobbies.get(lobby_id)
            if not lobby2 or lobby2["status"] != "mapban" or not lobby2.get("maps_remaining"):
                return
            # Проверяем что ход всё ещё за нами по счётчику
            if lobby2.get("ban_count", 0) != ban_count:
                return
            _apply_ban(lobby_id, captain_uid, random.choice(lobby2["maps_remaining"]))
        threading.Thread(target=bot_auto_ban, daemon=True).start()
    else:
        _send_ban_keyboard(lobby_id, captain_uid)
        # AFK-таймер: сравниваем ban_count, а не ban_turn — иначе 2-й ход того же капитана ложно срабатывает
        def captain_afk_timeout():
            time.sleep(BAN_TURN_TIMEOUT)
            lobby2 = active_lobbies.get(lobby_id)
            if not lobby2 or lobby2["status"] != "mapban":
                return
            if lobby2.get("ban_count", 0) != ban_count:
                return  # ход уже прошёл — капитан успел забанить
            remaining2 = lobby2.get("maps_remaining", [])
            if not remaining2:
                return
            chosen = random.choice(remaining2)
            try:
                bot.send_message(
                    captain_uid,
                    f"⏰ Время вышло ({BAN_TURN_TIMEOUT} сек)! Карта <b>{chosen}</b> забанена автоматически.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            _apply_ban(lobby_id, captain_uid, chosen)
        threading.Thread(target=captain_afk_timeout, daemon=True).start()

def _send_ban_keyboard(lobby_id, captain_uid):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return
    turn = lobby["ban_turn"]
    kb = types.InlineKeyboardMarkup(row_width=2)
    for m in lobby["maps_remaining"]:
        kb.add(types.InlineKeyboardButton(f"❌ {m}", callback_data=f"banmap_{lobby_id}_{m}"))
    try:
        sent = bot.send_message(
            captain_uid,
            f"{'💙' if turn=='ct' else '🧡'} <b>Твой ход — забань карту:</b>",
            reply_markup=kb,
        )
        ban_turn_messages[lobby_id] = (sent.chat.id, sent.message_id)
    except Exception as e:
        print(f"Ban keyboard error: {e}")

def _apply_ban(lobby_id, banner_uid, map_name):
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby["status"] != "mapban":
        return
    if map_name not in lobby["maps_remaining"]:
        return
    turn = lobby["ban_turn"]
    lobby["maps_remaining"].remove(map_name)
    lobby["map_bans"].append({"team": turn, "map": map_name})
    lobby["ban_count"] += 1
    if len(lobby["maps_remaining"]) == 1:
        lobby["map_name"] = lobby["maps_remaining"][0]
        send_ban_status_to_all(lobby_id)
        threading.Thread(target=lambda: (time.sleep(2), start_draft_phase(lobby_id)), daemon=True).start()
    else:
        lobby["ban_turn"] = "t" if turn == "ct" else "ct"
        send_ban_status_to_all(lobby_id)
        _do_ban_turn(lobby_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("banmap_"))
def cb_ban_map(c):
    uid = c.from_user.id
    raw = c.data[len("banmap_"):]
    raw_parts = raw.split("_")
    map_name = raw_parts[-1]
    lobby_id = "_".join(raw_parts[:-1])
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby["status"] != "mapban":
        bot.answer_callback_query(c.id, "❌ Фаза бана уже завершена")
        return
    turn = lobby["ban_turn"]
    expected_cap = lobby["ct_captain"] if turn == "ct" else lobby["t_captain"]
    if uid != expected_cap:
        bot.answer_callback_query(c.id, "❌ Сейчас не ваш ход!", show_alert=True)
        return
    if map_name not in lobby["maps_remaining"]:
        bot.answer_callback_query(c.id, "❌ Карта уже забанена")
        return
    bot.answer_callback_query(c.id, f"✅ {map_name} забанена!")
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass
    ban_turn_messages.pop(lobby_id, None)
    _apply_ban(lobby_id, uid, map_name)


# ==================== ДРАФТ ИГРОКОВ ====================

def _player_draft_label(uid, lobby):
    """Short one-line label for a draft pick button: Name [ELO | KD]."""
    priv_table = PRIVATE_CONFIG.get(lobby.get("private", "darling"), PRIVATE_CONFIG["darling"])["table"]
    league = lobby.get("league", "default")
    p = get_player_from_table(uid, priv_table) or get_player(uid)
    if not p:
        return str(uid)
    elo = _resolve_display_elo(uid, p, priv_table, league)
    lvl = get_faceit_level(elo)
    kills = p[8] if len(p) > 8 else 0
    deaths = p[9] if len(p) > 9 else 1
    kd = round(kills / max(deaths, 1), 2)
    return f"{p[1]} [Lv{lvl} | {elo} ELO | K/D {kd}]"


def _build_draft_units(lobby):
    """
    Return (units, ct_auto_uids, t_auto_uids).

    units: list of pick-units, each unit is a list of UIDs.
           A unit is a solo player OR an entire non-captain party group.
    ct_auto_uids: UIDs that auto-join CT (captain's own party members).
    t_auto_uids:  UIDs that auto-join T  (captain's own party members).
    """
    players = lobby["players"]
    player_set = set(players)
    ct_captain = lobby.get("ct_captain")
    t_captain  = lobby.get("t_captain")

    auto_assigned = set()
    if ct_captain:
        auto_assigned.add(ct_captain)
    if t_captain:
        auto_assigned.add(t_captain)

    ct_auto, t_auto = set(), set()
    for cap, auto_set in ((ct_captain, ct_auto), (t_captain, t_auto)):
        if not cap:
            continue
        party = get_party_of(cap)
        if party and len(party["members"]) > 1:
            for m in party["members"]:
                if m in player_set and m != cap:
                    auto_set.add(m)
                    auto_assigned.add(m)

    # Build pick units from the remaining (non-auto-assigned) players
    available = [u for u in players if u not in auto_assigned]
    placed = set()
    units = []
    for uid in available:
        if uid in placed:
            continue
        party = get_party_of(uid)
        if party and len(party["members"]) > 1:
            grp = [m for m in party["members"] if m in player_set and m not in auto_assigned and m not in placed]
            if len(grp) > 1:
                units.append(grp)
                for m in grp:
                    placed.add(m)
                continue
        units.append([uid])
        placed.add(uid)

    return units, ct_auto, t_auto


def _build_draft_status_text(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return ""
    draft = lobby.get("draft", {})
    ct_cap_uid = lobby.get("ct_captain")
    t_cap_uid  = lobby.get("t_captain")
    priv_table = PRIVATE_CONFIG.get(lobby.get("private", "darling"), PRIVATE_CONFIG["darling"])["table"]

    def pname(uid):
        p = get_player_from_table(uid, priv_table) or get_player(uid)
        return p[1] if p else str(uid)

    ct_team = draft.get("ct_team", [])
    t_team  = draft.get("t_team",  [])
    ct_lines = "\n".join(f"  {'👑 ' if u == ct_cap_uid else ''}{pname(u)}" for u in ct_team)
    t_lines  = "\n".join(f"  {'👑 ' if u == t_cap_uid  else ''}{pname(u)}" for u in t_team)

    turn = draft.get("turn", "ct")
    turn_cap  = ct_cap_uid if turn == "ct" else t_cap_uid
    turn_name = pname(turn_cap) if turn_cap else "?"

    units = draft.get("units", [])
    avail_parts = []
    for unit in units:
        if len(unit) == 1:
            avail_parts.append(f"  • {pname(unit[0])}")
        else:
            avail_parts.append(f"  • {'  +  '.join(pname(u) for u in unit)} (пати)")
    avail_text = "\n".join(avail_parts) if avail_parts else "  —"

    text = (
        f"👥 <b>Выбор игроков</b>\n\n"
        f"💙 <b>CT ({len(ct_team)})</b>:\n{ct_lines or '  (пусто)'}\n\n"
        f"🧡 <b>T ({len(t_team)})</b>:\n{t_lines or '  (пусто)'}\n\n"
    )
    if units:
        text += f"📋 <b>Доступны:</b>\n{avail_text}\n\n"
        text += f"⏳ Ход: {'💙' if turn == 'ct' else '🧡'} <b>{turn_name}</b>"
    else:
        text += "✅ Выбор завершён!"
    return text


def _send_draft_status_to_all(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return
    text = _build_draft_status_text(lobby_id)
    if "draft_status_msgs" not in lobby:
        lobby["draft_status_msgs"] = {}
    for uid in lobby["players"]:
        if is_bot_player(uid):
            continue
        existing = lobby["draft_status_msgs"].get(uid)
        if existing:
            cid, mid = existing
            try:
                bot.edit_message_text(text, cid, mid, parse_mode="HTML")
                continue
            except Exception:
                pass
        try:
            sent = bot.send_message(uid, text, parse_mode="HTML")
            lobby["draft_status_msgs"][uid] = (sent.chat.id, sent.message_id)
        except Exception:
            pass


def start_draft_phase(lobby_id):
    """Begin captain-pick draft after map ban."""
    try:
        _start_draft_phase_inner(lobby_id)
    except Exception as exc:
        print(f"[start_draft_phase] ОШИБКА lobby={lobby_id}: {exc}")
        import traceback; traceback.print_exc()
        # Fallback: just launch the match normally
        threading.Thread(target=lambda: launch_match(lobby_id), daemon=True).start()


def _start_draft_phase_inner(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return
    if lobby.get("status") not in ("mapban",):
        return

    # ── Чистим сообщения фазы бана ──────────────────────────────────────────
    # Уведомления "Фаза бана началась!"
    for _uid2, (_cid2, _mid2) in list(ban_notify_messages.pop(lobby_id, {}).items()):
        try:
            bot.delete_message(_cid2, _mid2)
        except Exception:
            pass
    # Статус-сообщения бана (у всех игроков)
    delete_ban_status(lobby_id)
    # Клавиатура бана (у текущего капитана)
    _ban_kb = ban_turn_messages.pop(lobby_id, None)
    if _ban_kb:
        try:
            bot.delete_message(_ban_kb[0], _ban_kb[1])
        except Exception:
            pass
    # ────────────────────────────────────────────────────────────────────────

    lobby["status"] = "draft"

    players = lobby["players"]
    league  = lobby.get("league", "default")
    team_sz = _lobby_team_size(league)

    ct_captain = lobby.get("ct_captain")
    t_captain  = lobby.get("t_captain")

    units, ct_auto, t_auto = _build_draft_units(lobby)

    ct_team = ([ct_captain] if ct_captain else []) + list(ct_auto)
    t_team  = ([t_captain]  if t_captain  else []) + list(t_auto)

    ct_needs = team_sz - len(ct_team)
    t_needs  = team_sz - len(t_team)

    # The team that already has fewer members picks first (catch-up)
    if len(ct_team) <= len(t_team):
        first_turn = "ct"
    else:
        first_turn = "t"

    lobby["draft"] = {
        "units":      units,
        "ct_team":    ct_team,
        "t_team":     t_team,
        "ct_needs":   ct_needs,
        "t_needs":    t_needs,
        "turn":       first_turn,
        "turn_count": 0,
        "lock":       threading.Lock(),   # guards concurrent pick application
        "finished":   False,              # idempotency guard for _finish_draft
    }
    lobby["draft_status_msgs"] = {}
    lobby["draft_kb_msgs"]     = {}

    # Inform all players (трекаем для удаления когда матч запустится)
    priv_table = PRIVATE_CONFIG.get(lobby.get("private", "darling"), PRIVATE_CONFIG["darling"])["table"]

    def pname(uid):
        p = get_player_from_table(uid, priv_table) or get_player(uid)
        return p[1] if p else str(uid)

    ct_cap_name = pname(ct_captain) if ct_captain else "?"
    t_cap_name  = pname(t_captain)  if t_captain  else "?"

    draft_notify_messages[lobby_id] = {}
    for uid in players:
        if is_bot_player(uid):
            continue
        try:
            _dsent = bot.send_message(
                uid,
                f"👥 <b>Фаза выбора игроков!</b>\n"
                f"🗺 Карта: <b>{lobby.get('map_name', '?')}</b>\n\n"
                f"💙 CT капитан: <b>{ct_cap_name}</b>\n"
                f"🧡 T  капитан: <b>{t_cap_name}</b>\n\n"
                f"Капитаны по очереди выбирают состав.",
                parse_mode="HTML",
            )
            draft_notify_messages[lobby_id][uid] = (_dsent.chat.id, _dsent.message_id)
        except Exception:
            pass

    _do_draft_turn(lobby_id)


def _send_draft_pick_keyboard(lobby_id, captain_uid, turn):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return
    draft = lobby.get("draft", {})
    units = draft.get("units", [])
    priv_table = PRIVATE_CONFIG.get(lobby.get("private", "darling"), PRIVATE_CONFIG["darling"])["table"]
    league     = lobby.get("league", "default")

    turn_count = draft.get("turn_count", 0)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, unit in enumerate(units):
        if len(unit) == 1:
            label = _player_draft_label(unit[0], lobby)
        else:
            # Party group
            parts_l = []
            elos = []
            for uid2 in unit:
                p = get_player_from_table(uid2, priv_table) or get_player(uid2)
                if p:
                    e = _resolve_display_elo(uid2, p, priv_table, league)
                    elos.append(e)
                    parts_l.append(p[1])
                else:
                    parts_l.append(str(uid2))
            avg_elo = round(sum(elos) / len(elos)) if elos else 1000
            label = f"👥 {' + '.join(parts_l)} [ELO ср. {avg_elo}]"
        # Embed turn_count nonce so stale keyboards from previous turns are rejected
        kb.add(types.InlineKeyboardButton(
            f"✅ {label}",
            callback_data=f"draftpick_{lobby_id}_{turn_count}_{i}",
        ))

    try:
        sent = bot.send_message(
            captain_uid,
            f"{'💙' if turn == 'ct' else '🧡'} <b>Твой ход — выбери игрока:</b>",
            reply_markup=kb,
            parse_mode="HTML",
        )
        lobby["draft_kb_msgs"][captain_uid] = (sent.chat.id, sent.message_id)
    except Exception as e:
        print(f"[draft keyboard error] {e}")


def _do_draft_turn(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby.get("status") != "draft":
        return

    draft     = lobby.get("draft", {})
    units     = draft.get("units", [])
    ct_needs  = draft.get("ct_needs", 0)
    t_needs   = draft.get("t_needs",  0)

    # Nothing left to draft
    if not units or (ct_needs <= 0 and t_needs <= 0):
        _finish_draft(lobby_id)
        return

    turn        = draft.get("turn", "ct")
    captain_uid = lobby.get("ct_captain") if turn == "ct" else lobby.get("t_captain")

    _send_draft_status_to_all(lobby_id)

    # --- Bot captain: auto-pick best unit by avg ELO ---
    if captain_uid and is_bot_player(captain_uid):
        def _bot_pick():
            time.sleep(random.uniform(2, 4))
            l2 = active_lobbies.get(lobby_id)
            if not l2 or l2.get("status") != "draft":
                return
            d2 = l2.get("draft", {})
            u2 = d2.get("units", [])
            if not u2:
                return
            priv_t = PRIVATE_CONFIG.get(l2.get("private","darling"), PRIVATE_CONFIG["darling"])["table"]
            league2 = l2.get("league", "default")

            def _unit_avg_elo(unit):
                s = 0
                for uid3 in unit:
                    p3 = get_player_from_table(uid3, priv_t) or get_player(uid3)
                    s += _resolve_display_elo(uid3, p3, priv_t, league2) if p3 else 1000
                return s / len(unit)

            best = max(u2, key=_unit_avg_elo)
            _apply_draft_pick(lobby_id, d2.get("turn", turn), best)
        threading.Thread(target=_bot_pick, daemon=True).start()

    # --- Human captain: send keyboard + AFK timer ---
    elif captain_uid:
        _send_draft_pick_keyboard(lobby_id, captain_uid, turn)
        snap_count = draft.get("turn_count", 0)

        def _afk_timeout(snap=snap_count, cap=captain_uid, t=turn):
            time.sleep(DRAFT_TURN_TIMEOUT)
            l2 = active_lobbies.get(lobby_id)
            if not l2 or l2.get("status") != "draft":
                return
            d2 = l2.get("draft", {})
            if d2.get("turn_count", 0) != snap:
                return  # someone already picked
            u2 = d2.get("units", [])
            if not u2:
                return
            chosen = random.choice(u2)
            try:
                bot.send_message(cap, f"⏰ Время вышло ({DRAFT_TURN_TIMEOUT} сек)! Игрок выбран автоматически.")
            except Exception:
                pass
            _apply_draft_pick(lobby_id, d2.get("turn", t), chosen)
        threading.Thread(target=_afk_timeout, daemon=True).start()

    # --- No captain: auto-fill ---
    else:
        if units:
            _apply_draft_pick(lobby_id, turn, units[0])


def _apply_draft_pick(lobby_id, team, unit):
    """Register a pick, advance the draft. Thread-safe via per-draft lock."""
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby.get("status") != "draft":
        return

    draft = lobby.get("draft", {})
    lock  = draft.get("lock")

    # Acquire the per-draft lock (non-blocking: second concurrent pick loses)
    if lock is not None:
        acquired = lock.acquire(blocking=False)
        if not acquired:
            return  # another thread is mid-pick; discard this one
    try:
        # Re-validate state under lock
        if not lobby or lobby.get("status") != "draft":
            return
        if draft.get("finished"):
            return

        unit_set = set(unit)

        # Verify the unit still exists (concurrent AFK + callback race)
        current_units = draft.get("units", [])
        if not any(set(u) == unit_set for u in current_units):
            return  # already picked by another thread

        # Remove chosen unit
        draft["units"] = [u for u in current_units if set(u) != unit_set]

        # Add to team
        if team == "ct":
            draft["ct_team"].extend(unit)
            draft["ct_needs"] = draft.get("ct_needs", 0) - len(unit)
        else:
            draft["t_team"].extend(unit)
            draft["t_needs"] = draft.get("t_needs", 0) - len(unit)

        draft["turn_count"] = draft.get("turn_count", 0) + 1

        # Delete the pick keyboard for the captain who just picked
        cap_uid = lobby.get("ct_captain") if team == "ct" else lobby.get("t_captain")
        kb_msg  = lobby.get("draft_kb_msgs", {}).pop(cap_uid, None)

        remaining_units = draft["units"]
        ct_needs = draft.get("ct_needs", 0)
        t_needs  = draft.get("t_needs",  0)

        do_finish = not remaining_units or (ct_needs <= 0 and t_needs <= 0)

        if not do_finish:
            # Give the next turn to whichever team needs MORE players (catch-up).
            # This handles the case where a party unit was just picked: the team
            # that received multiple players in one pick may now be ahead, so the
            # opposing team keeps picking until team sizes are equal again.
            if ct_needs > t_needs:
                next_turn = "ct"
            elif t_needs > ct_needs:
                next_turn = "t"
            else:
                # Teams need the same amount — normal alternation
                next_turn = "t" if team == "ct" else "ct"
            # Safety guard: skip any team that is already full
            if next_turn == "ct" and ct_needs <= 0:
                next_turn = "t"
            elif next_turn == "t" and t_needs <= 0:
                next_turn = "ct"
            draft["turn"] = next_turn

    finally:
        if lock is not None:
            lock.release()

    # Perform side-effects outside the lock
    if kb_msg:
        try:
            bot.delete_message(kb_msg[0], kb_msg[1])
        except Exception:
            pass

    if do_finish:
        _finish_draft(lobby_id)
    else:
        _do_draft_turn(lobby_id)


def _finish_draft(lobby_id):
    """All picks done — send result, then launch the match. Idempotent."""
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return

    draft = lobby.get("draft", {})
    # Guard against double-call from concurrent threads
    if draft.get("finished"):
        return
    draft["finished"] = True
    ct_team = list(draft.get("ct_team", []))
    t_team  = list(draft.get("t_team",  []))

    # Overflow safety: put leftover units anywhere they fit
    league  = lobby.get("league", "default")
    team_sz = _lobby_team_size(league)
    for unit in draft.get("units", []):
        for uid in unit:
            if len(ct_team) < team_sz:
                ct_team.append(uid)
            elif len(t_team) < team_sz:
                t_team.append(uid)

    # Store final teams so launch_match uses them
    lobby["draft_ct_team"] = ct_team
    lobby["draft_t_team"]  = t_team

    # Удаляем уведомление "Фаза выбора игроков!" и live-статус
    for _uid3, (_cid3, _mid3) in list(draft_notify_messages.pop(lobby_id, {}).items()):
        try:
            bot.delete_message(_cid3, _mid3)
        except Exception:
            pass
    for uid, (cid, mid) in list(lobby.get("draft_status_msgs", {}).items()):
        try:
            bot.delete_message(cid, mid)
        except Exception:
            pass
    lobby["draft_status_msgs"] = {}

    # Announce final rosters (трекаем для удаления при старте матча)
    priv_table = PRIVATE_CONFIG.get(lobby.get("private", "darling"), PRIVATE_CONFIG["darling"])["table"]

    def pname(uid):
        p = get_player_from_table(uid, priv_table) or get_player(uid)
        return p[1] if p else str(uid)

    ct_lines = "\n".join(f"  {pname(u)}" for u in ct_team)
    t_lines  = "\n".join(f"  {pname(u)}" for u in t_team)

    draft_final_messages[lobby_id] = {}
    for uid in lobby["players"]:
        if is_bot_player(uid):
            continue
        try:
            _fsent = bot.send_message(
                uid,
                f"✅ <b>Команды выбраны!</b>\n\n"
                f"💙 <b>CT:</b>\n{ct_lines}\n\n"
                f"🧡 <b>T:</b>\n{t_lines}\n\n"
                f"🗺 Карта: <b>{lobby.get('map_name', '?')}</b>",
                parse_mode="HTML",
            )
            draft_final_messages[lobby_id][uid] = (_fsent.chat.id, _fsent.message_id)
        except Exception:
            pass

    lobby["status"] = "pre_launch"
    threading.Thread(
        target=lambda: (time.sleep(2), launch_match(lobby_id)),
        daemon=True,
    ).start()


@bot.callback_query_handler(func=lambda c: c.data.startswith("draftpick_"))
def cb_draft_pick(c):
    uid = c.from_user.id
    raw = c.data[len("draftpick_"):]
    # Format: draftpick_{lobby_id}_{turn_count}_{unit_index}
    # lobby_id contains underscores → rsplit from right for the last 2 tokens
    parts = raw.rsplit("_", 2)
    if len(parts) < 3:
        bot.answer_callback_query(c.id, "❌ Ошибка формата")
        return
    lobby_id = parts[0]
    try:
        nonce    = int(parts[1])   # turn_count at the time keyboard was sent
        unit_idx = int(parts[2])
    except ValueError:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return

    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby.get("status") != "draft":
        bot.answer_callback_query(c.id, "❌ Фаза выбора уже завершена")
        return

    draft   = lobby.get("draft", {})
    turn    = draft.get("turn", "ct")
    cap_uid = lobby.get("ct_captain") if turn == "ct" else lobby.get("t_captain")

    if uid != cap_uid:
        bot.answer_callback_query(c.id, "❌ Сейчас не ваш ход!", show_alert=True)
        return

    # Reject stale keyboard from a previous turn
    if nonce != draft.get("turn_count", 0):
        bot.answer_callback_query(c.id, "⏰ Эта клавиатура устарела — ход уже был сделан")
        return

    units = draft.get("units", [])
    if unit_idx >= len(units):
        bot.answer_callback_query(c.id, "❌ Игрок уже был выбран — список обновился")
        return

    chosen = units[unit_idx]
    bot.answer_callback_query(c.id, "✅ Выбрано!")

    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass
    lobby.get("draft_kb_msgs", {}).pop(uid, None)

    _apply_draft_pick(lobby_id, turn, chosen)


# ==================== ЗАПУСК МАТЧА ====================
def _resolve_display_elo(uid, p, priv_table, league):
    """Возвращает правильный ELO в зависимости от лиги (duo_elo для 2v2, quals_elo для quals, elo для остальных)."""
    if p and p[13]:  # бот — всегда p[4]
        return p[4]
    if league == "2v2":
        return get_duo_elo_for_player(uid, priv_table or "players")
    if league == "quals":
        return get_quals_elo_for_player(uid, priv_table or "players")
    return p[4] if p else 1000

def pline(uid, priv_table=None, league=None):
    """Строчка игрока без ссылки (для обычных мест)."""
    p = (get_player_from_table(uid, priv_table) or get_player(uid)) if priv_table else get_player(uid)
    if p:
        icon = "🤖" if p[13] else "👤"
        prem = " 👑" if (not p[13] and has_active_premium(uid)) else ""
        elo  = _resolve_display_elo(uid, p, priv_table, league)
        return f"{icon} {p[1]}{prem} [Lvl {get_faceit_level(elo)} | {elo} ELO]"
    return str(uid)

def pline_link(uid, priv_table=None, league=None):
    """Строчка игрока с кликабельным именем — ведёт на TG-профиль (без превью ссылки)."""
    p = (get_player_from_table(uid, priv_table) or get_player(uid)) if priv_table else get_player(uid)
    if p:
        icon = "🤖" if p[13] else "👤"
        prem = " 👑" if (not p[13] and has_active_premium(uid)) else ""
        name = p[1]
        if not p[13]:  # Не бот
            name_linked = tg_link(uid, name)
        else:
            name_linked = name
        elo = _resolve_display_elo(uid, p, priv_table, league)
        return f"{icon} {name_linked}{prem} [Lvl {get_faceit_level(elo)} | {elo} ELO]"
    return str(uid)

def launch_match(lobby_id):
    try:
        _launch_match_inner(lobby_id)
    except Exception as _lm_exc:
        print(f"[launch_match] КРИТИЧЕСКАЯ ОШИБКА lobby={lobby_id}: {_lm_exc}")
        import traceback; traceback.print_exc()
        # Notify admins so the crash is not silent
        try:
            for _aid in ADMIN_IDS_LIST:
                bot.send_message(_aid, f"🚨 <b>launch_match ОШИБКА</b>\nЛобби: <code>{lobby_id}</code>\nОшибка: {_lm_exc}", parse_mode="HTML")
        except Exception:
            pass

def _launch_match_inner(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby["status"] not in (
        "accepting", "waiting", "mapban", "pre_mapban", "draft", "pre_launch"
    ):
        return
    # Удаляем "Команды выбраны!" перед отправкой сообщения о матче
    for _uid4, (_cid4, _mid4) in list(draft_final_messages.pop(lobby_id, {}).items()):
        try:
            bot.delete_message(_cid4, _mid4)
        except Exception:
            pass
    lobby["status"] = "active"
    if not lobby.get("map_name"):
        lobby["map_name"] = random.choice(MAPS)
    match_id = get_next_match_id()
    match_code = generate_match_code()
    lobby["match_id"] = match_id
    lobby["match_code"] = match_code
    lobby["ACreenshots_count"] = 0
    lobby["reg_taken_by"] = None
    match_key = f"match_{match_id}"
    lobby["match_key"] = match_key

    players = list(lobby["players"])

    ct_cap = lobby.get("ct_captain")
    t_cap  = lobby.get("t_captain")

    _team_sz = _lobby_team_size(lobby.get("league", "default"))

    # If the draft phase already determined teams, use them directly
    if lobby.get("draft_ct_team") and lobby.get("draft_t_team"):
        team_ct = list(lobby["draft_ct_team"])
        team_t  = list(lobby["draft_t_team"])
        # Safety: add any player not yet placed
        all_placed = set(team_ct + team_t)
        for u in players:
            if u not in all_placed:
                if len(team_ct) < _team_sz:
                    team_ct.append(u)
                else:
                    team_t.append(u)
    else:
        # ---- Fallback: original auto-assignment (no draft phase) ----
        placed, party_groups, solo_players = set(), [], []
        for uid2 in players:
            if uid2 in placed:
                continue
            p_obj = get_party_of(uid2)
            if p_obj and len(p_obj["members"]) > 1:
                grp = [m for m in p_obj["members"] if m in players and m not in placed]
                if grp:
                    party_groups.append(grp)
                    for m in grp:
                        placed.add(m)
            else:
                solo_players.append(uid2)
                placed.add(uid2)

        random.shuffle(party_groups)
        random.shuffle(solo_players)

        team_ct, team_t = [], []

        if ct_cap and ct_cap in players:
            team_ct.append(ct_cap)
            for grp in party_groups:
                if ct_cap in grp:
                    for m in grp:
                        if m != ct_cap and m not in team_ct:
                            team_ct.append(m)
                    break
        if t_cap and t_cap in players and t_cap not in team_ct:
            team_t.append(t_cap)
            for grp in party_groups:
                if t_cap in grp:
                    for m in grp:
                        if m != t_cap and m not in team_t and m not in team_ct:
                            team_t.append(m)
                    break

        already_placed = set(team_ct + team_t)

        for grp in party_groups:
            if all(m in already_placed for m in grp):
                continue
            remaining_grp = [m for m in grp if m not in already_placed]
            if not remaining_grp:
                continue
            if len(team_ct) + len(remaining_grp) <= _team_sz:
                team_ct.extend(remaining_grp)
            elif len(team_t) + len(remaining_grp) <= _team_sz:
                team_t.extend(remaining_grp)
            else:
                ct_free = _team_sz - len(team_ct)
                t_free  = _team_sz - len(team_t)
                if ct_free >= t_free:
                    team_ct.extend(remaining_grp)
                else:
                    team_t.extend(remaining_grp)
            for m in remaining_grp:
                already_placed.add(m)

        for uid2 in solo_players:
            if uid2 in already_placed:
                continue
            if len(team_ct) < _team_sz:
                team_ct.append(uid2)
            else:
                team_t.append(uid2)

        # Гарантируем ровно NvN (5v5 или 2v2)
        all_placed_set = set(team_ct + team_t)
        unplaced = [u for u in players if u not in all_placed_set]
        for u in unplaced:
            if len(team_ct) < _team_sz:
                team_ct.append(u)
            else:
                team_t.append(u)

        # Перебалансируем если нужно, не разбивая пати
        def _party_safe_move(src, dst, max_sz):
            while len(src) > max_sz and len(dst) < max_sz:
                moved = False
                for candidate in reversed(src):
                    p_obj = get_party_of(candidate)
                    if p_obj and len(p_obj["members"]) > 1:
                        party_in_src = [m for m in p_obj["members"] if m in src]
                        if len(party_in_src) > 1:
                            continue
                    src.remove(candidate)
                    dst.insert(0, candidate)
                    moved = True
                    break
                if not moved:
                    break

        _party_safe_move(team_ct, team_t, _team_sz)
        _party_safe_move(team_t, team_ct, _team_sz)

    lobby["team_ct"] = team_ct
    lobby["team_t"]  = team_t

    ct_captain_uid = lobby.get("ct_captain")
    if ct_captain_uid and not is_bot_player(ct_captain_uid) and ct_captain_uid in team_ct:
        host_uid = ct_captain_uid
    else:
        host_uid = next((u for u in team_ct if not is_bot_player(u)), None)
    _launch_priv_table = PRIVATE_CONFIG.get(lobby.get("private", "darling"), PRIVATE_CONFIG["darling"])["table"]
    host_p    = (get_player_from_table(host_uid, _launch_priv_table) or get_player(host_uid)) if host_uid else None
    host_game_id = host_p[2] if host_p else "—"
    host_name    = host_p[1] if host_p else "—"
    lobby["host_uid"]     = host_uid
    lobby["host_game_id"] = host_game_id

    _launch_league = lobby.get("league", "default")

    def admin_pline(idx, u):
        p = get_player_from_table(u, _launch_priv_table) or get_player(u)
        num = NUMBER_EMOJI[idx] if idx < len(NUMBER_EMOJI) else f"{idx+1}."
        if p:
            icon = "🤖" if p[13] else ""
            prem = " 👑" if (not p[13] and has_active_premium(u)) else ""
            elo  = _resolve_display_elo(u, p, _launch_priv_table, _launch_league)
            return f"{num} {icon}{p[1]}{prem} | ID: <code>{u}</code> | ELO: {elo}"
        return f"{num} <code>{u}</code>"

    ct_lines = "\n".join([admin_pline(i, u) for i, u in enumerate(team_ct)])
    t_lines  = "\n".join([admin_pline(i, u) for i, u in enumerate(team_t)])
    _adm_priv_cfg   = PRIVATE_CONFIG.get(lobby.get("private", "darling"), PRIVATE_CONFIG["darling"])
    _adm_priv_label = f"{_adm_priv_cfg['emoji']} {_adm_priv_cfg['display']}"
    match_text = (
        f"🎮 <b>МАТЧ #{match_code} НАЧАЛСЯ</b>\n\n"
        f"🏠 Приватка: <b>{_adm_priv_label}</b>\n"
        f"🏷 Лига: {format_league(lobby.get('league','default'))}\n📱 Устройство: {lobby.get('device','').upper()}\n"
        f"🗺 Карта: <b>{lobby.get('map_name','?')}</b>\n"
        f"👑 Хост: <b>{host_name}</b> | Game ID: <code>{host_game_id}</code>\n\n"
        f"💙 <b>Команда CT</b>\n{ct_lines}\n\n"
        f"🧡 <b>Команда T</b>\n{t_lines}"
    )

    if ADMIN_CHAT_ID:
        kb_admin = _build_admin_match_kb(match_key, match_code, 0)
        thread_id = None
        # Пытаемся создать ветку форума
        try:
            topic = bot.create_forum_topic(ADMIN_CHAT_ID, f"MATCH #{match_code}")
            thread_id = topic.message_thread_id
            print(f"✅ Ветка создана: MATCH #{match_code}, thread_id={thread_id}")
        except Exception as e:
            print(f"❌ create_forum_topic ОШИБКА (ADMIN_CHAT_ID={ADMIN_CHAT_ID}): {e}")
            # Уведомляем всех админов в личку об ошибке
            for aid in ADMIN_IDS_LIST:
                try:
                    bot.send_message(aid, f"⚠️ Не удалось создать ветку для MATCH #{match_code}.\nОшибка: {e}\n\nПроверьте что бот — администратор группы с правом управления темами.")
                except Exception:
                    pass

        lobby["admin_thread_id"] = thread_id
        try:
            send_kw = {"reply_markup": kb_admin, "parse_mode": "HTML"}
            if thread_id:
                send_kw["message_thread_id"] = thread_id
            sent = bot.send_message(ADMIN_CHAT_ID, match_text, **send_kw)
            lobby["admin_msg_id"] = sent.message_id
            try:
                bot.pin_chat_message(ADMIN_CHAT_ID, sent.message_id, disable_notification=True)
            except Exception:
                pass
        except Exception as e:
            print(f"Admin send_message error: {e}")

    for uid in players:
        if is_bot_player(uid):
            continue
        team = "💙 CT" if uid in team_ct else "🧡 T"
        # Используем pline_link для кликабельных ников (tg:// — нет превью)
        _p_priv_cfg  = PRIVATE_CONFIG.get(lobby.get("private", "darling"), PRIVATE_CONFIG["darling"])
        _p_priv_label = f"{_p_priv_cfg['emoji']} {_p_priv_cfg['display']}"
        player_text = (
            f"🎮 <b>МАТЧ #{match_code} НАЧАЛСЯ!</b>\n\n"
            f"🏠 Приватка: <b>{_p_priv_label}</b>\n"
            f"🗺 Карта: <b>{lobby['map_name']}</b>\n"
            f"👥 Ваша команда: <b>{team}</b>\n"
            f"👑 Хост: <b>{host_name}</b>\n"
            f"🎮 Game ID хоста: <code>{host_game_id}</code>\n\n"
            f"💙 <b>Команда CT</b>\n"
            + "\n".join([f"  {i+1}. {pline_link(u, _launch_priv_table, _launch_league)}" for i, u in enumerate(team_ct)])
            + f"\n\n🧡 <b>Команда T</b>\n"
            + "\n".join([f"  {i+1}. {pline_link(u, _launch_priv_table, _launch_league)}" for i, u in enumerate(team_t)])
            + f"\n\n📸 После матча нажми кнопку и отправь скриншот результатов."
        )
        kb_player = types.InlineKeyboardMarkup()
        kb_player.add(types.InlineKeyboardButton("📸 Отправить результаты", callback_data=f"send_result_{match_key}"))
        try:
            # disable_web_page_preview=True чтобы tg:// ссылки не давали превью
            sent_pm = bot.send_message(uid, player_text, reply_markup=kb_player, disable_web_page_preview=True)
            awaiting_ACreenshot[uid] = match_key
            if "player_start_msgs" not in lobby:
                lobby["player_start_msgs"] = {}
            lobby["player_start_msgs"][uid] = sent_pm.message_id
        except Exception:
            pass

    running_matches[match_key] = lobby
    # Сохраняем матч в БД при старте (статус 'active')
    # Retry up to 3 times with a short pause; notify admin on persistent failure
    _db_saved = False
    for _db_attempt in range(3):
        try:
            save_match_start(lobby)
            _db_saved = True
            break
        except Exception as _db_e:
            print(f"[save_match_start] попытка {_db_attempt+1}/3 ошибка: {_db_e}")
            if _db_attempt < 2:
                time.sleep(1)
    if not _db_saved:
        try:
            for _aid in ADMIN_IDS_LIST:
                bot.send_message(
                    _aid,
                    f"🚨 <b>Матч не сохранён в БД!</b>\n"
                    f"Матч: <code>{match_code}</code>\n"
                    f"Lobby: <code>{lobby_id}</code>\n"
                    f"Проверьте подключение к базе данных.",
                    parse_mode="HTML",
                )
        except Exception:
            pass
    parts = lobby_id.split("_")
    if len(parts) >= 4:
        # Format: private_league_device_slot
        private_r, league_r, device_r, slot_r = parts[0], parts[1], parts[2], parts[3]
        active_lobbies[lobby_id] = {"players": [], "league": league_r, "device": device_r, "slot": slot_r, "status": "waiting", "private": private_r}
    elif len(parts) >= 3:
        league_r, device_r, slot_r = parts[0], parts[1], parts[2]
        active_lobbies[lobby_id] = {"players": [], "league": league_r, "device": device_r, "slot": slot_r, "status": "waiting", "private": "darling"}
    else:
        active_lobbies.pop(lobby_id, None)

    lobby_player_messages.pop(lobby_id, None)
    delete_ban_status(lobby_id)
    delete_accept_status(lobby_id)
    delete_match_found(lobby_id)
    for uid in players:
        user_lobby.pop(uid, None)


def _build_admin_match_kb(match_key, match_code, ACreenshots_count, taken_by=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    if taken_by:
        p = get_player(taken_by)
        name = p[1] if p else str(taken_by)
        kb.add(
            types.InlineKeyboardButton(f"🔒 Регистрирует: {name} — ЗАНЯТО", callback_data="match_noop"),
            types.InlineKeyboardButton("🔓 Освободить (force)", callback_data=f"reg_release|{match_key}"),
            types.InlineKeyboardButton("🚫 Отказаться от регистрации", callback_data=f"reg_abandon|{match_key}"),
        )
    else:
        kb.add(types.InlineKeyboardButton(
            f"✅ Взять регистрацию #{match_code} ({ACreenshots_count}📸)",
            callback_data=f"reg_match|{match_key}",
        ))
    kb.add(
        types.InlineKeyboardButton("❌ Отменить матч",  callback_data=f"cancel_match|{match_key}"),
        types.InlineKeyboardButton("🔄 Перерегать",     callback_data=f"reregister_match|{match_key}"),
    )
    return kb


@bot.callback_query_handler(func=lambda c: c.data == "match_noop")
def cb_match_noop(c):
    """Заглушка для неактивных кнопок в карточке матча."""
    bot.answer_callback_query(c.id, "🔒 Матч уже взят в работу", show_alert=False)


# ==================== СКРИНШОТЫ ====================
@bot.callback_query_handler(func=lambda c: c.data.startswith("send_result_"))
def cb_send_result(c):
    uid = c.from_user.id
    match_key = c.data.split("send_result_", 1)[1]
    lobby = running_matches.get(match_key)
    if not lobby or lobby.get("status") != "active":
        bot.answer_callback_query(c.id, "❌ Матч уже завершён", show_alert=True)
        return
    awaiting_ACreenshot[uid] = match_key
    bot.answer_callback_query(c.id)
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    except Exception:
        pass
    sent_prompt = bot.send_message(uid, "📸 <b>Отправь скриншот прямо в этот чат</b>\n\nПрикрепи фото или документ:", parse_mode="HTML")
    # Сохраняем ID чтобы удалить после получения скриншота
    if "ACreenshot_prompt_msgs" not in (running_matches.get(awaiting_ACreenshot.get(uid)) or {}):
        lobby2 = running_matches.get(match_key)
        if lobby2 is not None:
            if "ACreenshot_prompt_msgs" not in lobby2:
                lobby2["ACreenshot_prompt_msgs"] = {}
            lobby2["ACreenshot_prompt_msgs"][uid] = sent_prompt.message_id


@bot.message_handler(content_types=["photo", "document"])
def handle_player_ACreenshot(msg):
    uid = msg.from_user.id
    # Принимаем скриншоты только из личных сообщений бота
    if msg.chat.type != "private":
        return
    # Если пользователь в процессе создания тикета — передаём туда
    if uid in ticket_flow and ticket_flow[uid].get("step") == "evidence":
        ticket_step_evidence(msg)
        return
    if not is_registered(uid) or is_bot_player(uid):
        return
    match_key = awaiting_ACreenshot.get(uid)
    if not match_key:
        return
    lobby = running_matches.get(match_key)
    if not lobby or lobby.get("status") != "active":
        awaiting_ACreenshot.pop(uid, None)
        return
    awaiting_ACreenshot.pop(uid, None)
    # Удаляем сообщение "Отправь скриншот прямо в этот чат"
    prompt_mid = lobby.get("ACreenshot_prompt_msgs", {}).pop(uid, None)
    if prompt_mid:
        try:
            bot.delete_message(uid, prompt_mid)
        except Exception:
            pass
    # Удаляем сообщение "МАТЧ НАЧАЛСЯ / команды" у этого игрока
    start_mid = lobby.get("player_start_msgs", {}).pop(uid, None)
    if start_mid:
        try:
            bot.delete_message(uid, start_mid)
        except Exception:
            pass
    p = get_player_in_lobby(uid, lobby)
    name = p[1] if p else str(uid)
    match_id = lobby.get("match_id", "?")
    lobby["ACreenshots_count"] = lobby.get("ACreenshots_count", 0) + 1
    AC = lobby["ACreenshots_count"]
    match_code = lobby.get("match_code", str(match_id))
    # Обновляем счётчик скриншотов в unregistered_matches
    try:
        _um_conn = _db()
        _um_cur  = _um_conn.cursor()
        _um_cur.execute(
            "UPDATE unregistered_matches SET ACreenshots_count=%s WHERE match_id=%s",
            (AC, match_id),
        )
        _um_conn.commit()
        _um_conn.close()
    except Exception as _um_e:
        print(f"[handle_player_ACreenshot] unregistered_matches update: {_um_e}")
    if ADMIN_CHAT_ID:
        try:
            caption = f"📸 {name} ({uid}) — Match #{match_code}"
            thread_id = lobby.get("admin_thread_id")
            kw = {"caption": caption}
            if thread_id:
                kw["message_thread_id"] = thread_id
            elif lobby.get("admin_msg_id"):
                kw["reply_to_message_id"] = lobby["admin_msg_id"]
            if msg.photo:
                bot.send_photo(ADMIN_CHAT_ID, msg.photo[-1].file_id, **kw)
            elif msg.document:
                bot.send_document(ADMIN_CHAT_ID, msg.document.file_id, **kw)
            if lobby.get("admin_msg_id"):
                new_kb = _build_admin_match_kb(match_key, match_code, AC, lobby.get("reg_taken_by"))
                edit_kw = {"reply_markup": new_kb}
                if thread_id:
                    edit_kw["message_thread_id"] = thread_id
                try:
                    bot.edit_message_reply_markup(ADMIN_CHAT_ID, lobby["admin_msg_id"], **edit_kw)
                except Exception:
                    pass
        except Exception as e:
            print(f"ACreenshot error: {e}")
    try:
        _AC_max = _lobby_max_size(lobby.get("league", "default"))
        bot.reply_to(msg, f"✅ Скриншот принят! Всего: {AC}/{_AC_max}")
    except Exception:
        pass


# ==================== РЕГИСТРАЦИЯ РЕЗУЛЬТАТОВ ====================
match_registration = {}

def reg_send(uid, text, **kwargs):
    data = match_registration.get(uid, {})
    chat_id   = data.get("reply_chat_id", uid)
    thread_id = data.get("reply_thread_id")
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    try:
        bot.send_message(chat_id, text, **kwargs)
    except Exception as _e:
        print(f"[reg_send error] chat_id={chat_id} thread_id={thread_id}: {_e}")
        try:
            bot.send_message(uid, text, **kwargs)
        except Exception as _e2:
            print(f"[reg_send fallback error] uid={uid}: {_e2}")


@bot.callback_query_handler(func=lambda c: c.data.startswith("reg_match|"))
def cb_reg_match(c):
    uid = c.from_user.id
    if not is_game_reg_check(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    match_key = c.data.split("|", 1)[1]
    lobby = running_matches.get(match_key)
    if not lobby or lobby.get("status") != "active":
        bot.answer_callback_query(c.id, "❌ Матч не найден или завершён", show_alert=True)
        return

    # ── Защита от двойного взятия (атомарная проверка + lock) ──────────────
    taken = lobby.get("reg_taken_by")
    if taken and taken != uid:
        p = get_player(taken)
        bot.answer_callback_query(c.id, f"🔒 Уже взято: {p[1] if p else taken}", show_alert=True)
        return
    # Проверяем, не ведёт ли этот же матч кто-то через match_registration
    for _adm, _rdata in list(match_registration.items()):
        if _rdata.get("match_key") == match_key and _adm != uid:
            _ap = get_player(_adm)
            bot.answer_callback_query(
                c.id,
                f"🔒 {_ap[1] if _ap else _adm} уже регистрирует этот матч",
                show_alert=True,
            )
            return
    # Блокируем — сначала записываем, потом отвечаем (чтобы вторая попытка сразу видела блок)
    lobby["reg_taken_by"] = uid
    match_id   = lobby.get("match_id", "?")
    match_code = lobby.get("match_code", str(match_id))
    AC = lobby.get("ACreenshots_count", 0)
    try:
        new_kb = _build_admin_match_kb(match_key, match_code, AC, taken_by=uid)
        thread_id = lobby.get("admin_thread_id")
        edit_kw = {"reply_markup": new_kb}
        if thread_id:
            edit_kw["message_thread_id"] = thread_id
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, **edit_kw)
    except Exception:
        pass
    bot.answer_callback_query(c.id, "✅ Регистрация захвачена!")

    # Уведомление в ветку: "Администратор X начал обработку матча"
    admin_p = get_player(uid)
    admin_name = admin_p[1] if admin_p else str(uid)
    thread_id = lobby.get("admin_thread_id")
    try:
        kw_notify = {"parse_mode": "HTML"}
        if thread_id:
            kw_notify["message_thread_id"] = thread_id
        bot.send_message(
            ADMIN_CHAT_ID,
            f"📝 Администратор <b>{admin_name}</b> начал обработку матча #{match_code}",
            **kw_notify,
        )
    except Exception:
        pass

    # Уведомляем всех игроков матча в личку
    all_match_players = list(lobby.get("team_ct", [])) + list(lobby.get("team_t", []))
    for player_uid in all_match_players:
        if is_bot_player(player_uid):
            continue
        try:
            bot.send_message(
                player_uid,
                f"📋 Администратор <b>{admin_name}</b> начал регистрацию матча <b>#{match_code}</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    def pln(uid2):
        p = get_player(uid2)
        return f"{p[1]} — <code>{uid2}</code>" if p else str(uid2)
    ct_list = "\n".join([pln(u) for u in lobby.get("team_ct", [])])
    t_list  = "\n".join([pln(u) for u in lobby.get("team_t",  [])])
    # Если есть ветка форума — регистрируем в ней, иначе — в личку админу
    admin_thread_id = lobby.get("admin_thread_id")
    if admin_thread_id:
        reply_chat_id   = ADMIN_CHAT_ID
        reply_thread_id = admin_thread_id
    else:
        reply_chat_id   = uid          # личка администратора
        reply_thread_id = None
    _reg_priv_cfg   = PRIVATE_CONFIG.get(lobby.get("private", "darling"), PRIVATE_CONFIG["darling"])
    _reg_priv_label = f"{_reg_priv_cfg['emoji']} {_reg_priv_cfg['display']}"
    instructions = (
        f"📋 <b>Регистрация матча #{match_code}</b>\n\n"
        f"🏠 Приватка: <b>{_reg_priv_label}</b>\n"
        f"🏷 Лига: {format_league(lobby.get('league','default'))} | 📱 {lobby.get('device','').upper()}\n\n"
        f"💙 <b>CT</b>\n{ct_list}\n\n🧡 <b>T</b>\n{t_list}\n\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"<b>Шаг 1/3</b> — Введи счёт матча:\n"
        f"Формат: <code>13:11</code>"
    )
    match_registration[uid] = {
        "match_key": match_key,
        "step": "ACore",
        "reply_chat_id": reply_chat_id,
        "reply_thread_id": reply_thread_id,
    }
    send_kw = {"parse_mode": "HTML"}
    if reply_thread_id:
        send_kw["message_thread_id"] = reply_thread_id
    bot.send_message(reply_chat_id, instructions, **send_kw)


@bot.callback_query_handler(func=lambda c: c.data.startswith("reg_release|"))
def cb_reg_release(c):
    """Force-release: любой game_reg может освободить занятый матч."""
    uid = c.from_user.id
    if not is_game_reg_check(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    match_key = c.data.split("|", 1)[1]
    lobby = running_matches.get(match_key)
    if not lobby:
        bot.answer_callback_query(c.id, "❌ Матч не найден")
        return
    prev_taker = lobby.get("reg_taken_by")
    lobby["reg_taken_by"] = None
    # Очищаем запись match_registration для того, кто реально взял матч
    for _adm in list(match_registration.keys()):
        if match_registration[_adm].get("match_key") == match_key:
            match_registration.pop(_adm, None)
    match_id   = lobby.get("match_id", "?")
    match_code = lobby.get("match_code", str(match_id))
    AC = lobby.get("ACreenshots_count", 0)
    try:
        new_kb = _build_admin_match_kb(match_key, match_code, AC, taken_by=None)
        thread_id = lobby.get("admin_thread_id")
        edit_kw = {"reply_markup": new_kb}
        if thread_id:
            edit_kw["message_thread_id"] = thread_id
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, **edit_kw)
    except Exception:
        pass
    admin_p = get_player(uid)
    admin_name = admin_p[1] if admin_p else str(uid)
    bot.answer_callback_query(c.id, "🔓 Регистрация освобождена")
    thread_id = lobby.get("admin_thread_id")
    try:
        kw = {"parse_mode": "HTML"}
        if thread_id:
            kw["message_thread_id"] = thread_id
        prev_p = get_player(prev_taker) if prev_taker else None
        prev_name = prev_p[1] if prev_p else str(prev_taker or "?")
        bot.send_message(
            ADMIN_CHAT_ID,
            f"🔓 <b>{admin_name}</b> освободил регистрацию матча #{match_code} (была у {prev_name})",
            **kw,
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("reg_abandon|"))
def cb_reg_abandon(c):
    """Регистратор отказывается от своей регистрации."""
    uid = c.from_user.id
    if not is_game_reg_check(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    match_key = c.data.split("|", 1)[1]
    lobby = running_matches.get(match_key)
    if not lobby:
        bot.answer_callback_query(c.id, "❌ Матч не найден")
        return
    taken = lobby.get("reg_taken_by")
    if taken and taken != uid and not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Это не ваша регистрация", show_alert=True)
        return
    lobby["reg_taken_by"] = None
    # Очищаем match_registration для регистратора этого матча (а не только для себя)
    for _adm in list(match_registration.keys()):
        if match_registration[_adm].get("match_key") == match_key:
            match_registration.pop(_adm, None)
    match_id   = lobby.get("match_id", "?")
    match_code = lobby.get("match_code", str(match_id))
    AC = lobby.get("ACreenshots_count", 0)
    try:
        new_kb = _build_admin_match_kb(match_key, match_code, AC, taken_by=None)
        thread_id = lobby.get("admin_thread_id")
        edit_kw = {"reply_markup": new_kb}
        if thread_id:
            edit_kw["message_thread_id"] = thread_id
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, **edit_kw)
    except Exception:
        pass
    bot.answer_callback_query(c.id, "🚫 Вы отказались от регистрации")
    # Уведомление в ветку
    admin_p = get_player(uid)
    admin_name = admin_p[1] if admin_p else str(uid)
    thread_id = lobby.get("admin_thread_id")
    try:
        kw_notify = {"parse_mode": "HTML"}
        if thread_id:
            kw_notify["message_thread_id"] = thread_id
        bot.send_message(
            ADMIN_CHAT_ID,
            f"🚫 Администратор <b>{admin_name}</b> отказался от регистрации матча #{match_code}",
            **kw_notify,
        )
    except Exception:
        pass


@bot.message_handler(func=lambda m: (
    m.from_user.id in match_registration
    and match_registration[m.from_user.id].get("step") == "ACore"
    and m.chat.id == match_registration[m.from_user.id].get("reply_chat_id", m.from_user.id)
))
def reg_step_ACore(msg):
    uid = msg.from_user.id
    if not is_game_reg_check(uid):
        return
    text = msg.text.strip() if msg.text else ""
    m = re.match(r'^(\d+)\s*[:\-]\s*(\d+)$', text)
    if not m:
        reg_send(uid, "❌ Неверный формат. Введи счёт: <code>13:11</code>", parse_mode="HTML")
        return
    ACore_w, ACore_l = int(m.group(1)), int(m.group(2))
    match_registration[uid]["ACore_w"] = ACore_w
    match_registration[uid]["ACore_l"] = ACore_l
    match_registration[uid]["step"] = "winner"
    match_key = match_registration[uid]["match_key"]
    lobby = running_matches.get(match_key)
    _rs_priv_table  = PRIVATE_CONFIG.get(lobby.get("private", "darling") if lobby else "darling", PRIVATE_CONFIG["darling"])["table"] if lobby else None
    _rs_league      = lobby.get("league", "default") if lobby else "default"
    ct_list = "\n".join([f"  {i+1}. {pline(u, _rs_priv_table, _rs_league)}" for i, u in enumerate((lobby.get("team_ct", []) if lobby else []))])
    t_list  = "\n".join([f"  {i+1}. {pline(u, _rs_priv_table, _rs_league)}" for i, u in enumerate((lobby.get("team_t",  []) if lobby else []))])
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("💙 CT победила", callback_data=f"reg_winner_ct|{match_key}"),
        types.InlineKeyboardButton("🧡 T победила",  callback_data=f"reg_winner_t|{match_key}"),
    )
    reply_chat_id   = match_registration[uid].get("reply_chat_id", uid)
    reply_thread_id = match_registration[uid].get("reply_thread_id")
    send_kw = {"reply_markup": kb, "parse_mode": "HTML"}
    if reply_thread_id:
        send_kw["message_thread_id"] = reply_thread_id
    bot.send_message(
        reply_chat_id,
        f"<b>Шаг 2/3</b> — Счёт принят: <b>{ACore_w}:{ACore_l}</b>\n\n"
        f"💙 CT:\n{ct_list}\n\n🧡 T:\n{t_list}\n\n"
        f"Кто победил?",
        **send_kw,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("reg_winner_"))
def reg_winner(c):
    uid = c.from_user.id
    if not is_game_reg_check(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    if uid not in match_registration or match_registration[uid].get("step") != "winner":
        bot.answer_callback_query(c.id, "❌ Нет активной регистрации")
        return
    raw = c.data[len("reg_winner_"):]
    parts = raw.split("|", 1)
    winner = parts[0]
    match_key = parts[1] if len(parts) > 1 else match_registration[uid]["match_key"]
    match_registration[uid]["winner"] = winner
    match_registration[uid]["step"] = "all_kills"
    bot.answer_callback_query(c.id)
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    except Exception:
        pass
    lobby = running_matches.get(match_key)
    ct_players = [u for u in (lobby.get("team_ct", []) if lobby else []) if not is_bot_player(u)]
    t_players  = [u for u in (lobby.get("team_t",  []) if lobby else []) if not is_bot_player(u)]
    match_registration[uid]["ct_players"] = ct_players
    match_registration[uid]["t_players"]  = t_players
    match_registration[uid]["kills_data"] = {}
    _reg_lobby = running_matches.get(match_key)
    _reg_priv_table = PRIVATE_CONFIG.get(_reg_lobby.get("private", "darling") if _reg_lobby else "darling", PRIVATE_CONFIG["darling"])["table"]
    _ask_all_kda(uid, ct_players, t_players, priv_table=_reg_priv_table)


def _ask_all_kda(reg_uid, ct_players, t_players, priv_table=None):
    all_players = ct_players + t_players
    lines = []
    for u in all_players:
        p = (get_player_from_table(u, priv_table) or get_player(u)) if priv_table else get_player(u)
        name = p[1] if p else str(u)
        team = "💙 CT" if u in ct_players else "🧡 T"
        lines.append(f"  {team} {name} — <code>{u}</code>")
    player_list = "\n".join(lines)
    all_count = len(all_players)
    example_parts = [f"{u} 20 5 3" for u in all_players[:3]]
    example = ", ".join(example_parts)
    reg_send(
        reg_uid,
        f"<b>Шаг 3/3 — Статистика игроков</b>\n\n"
        f"Список игроков:\n{player_list}\n\n"
        f"💡 Скопируй ID рядом с ником и введи <b>всех одной строкой</b>:\n"
        f"<code>ID K A D, ID K A D, ...</code>\n\n"
        f"🔫 K — киллы  🤝 A — помощи  💀 D — смерти\n\n"
        f"Пример:\n<code>{example}</code>\n\n"
        f"⚠️ Между цифрами — пробел, после каждого игрока — запятая\n"
        f"Нужно ввести всех {all_count} игроков.",
        parse_mode="HTML",
    )


def _parse_all_kda(text, all_players):
    known_ids = set(all_players)
    entries = [e.strip() for e in text.split(",") if e.strip()]
    if len(entries) != len(all_players):
        return None, f"❌ Нужно {len(all_players)} записей через запятую, ты ввёл {len(entries)}."
    result = {}
    seen_ids = set()
    for i, entry in enumerate(entries):
        parts = entry.split()
        if len(parts) != 4:
            return None, f"❌ Запись #{i+1}: нужно 4 значения через пробел — <code>ID K A D</code>."
        try:
            pid = int(parts[0])
            k, a, d = int(parts[1]), int(parts[2]), int(parts[3])
        except ValueError:
            return None, f"❌ Запись #{i+1}: только цифры."
        if pid not in known_ids:
            return None, f"❌ Запись #{i+1}: ID <code>{pid}</code> не найден в этом матче."
        if pid in seen_ids:
            return None, f"❌ Запись #{i+1}: ID <code>{pid}</code> уже введён."
        seen_ids.add(pid)
        result[pid] = {"kills": k, "assists": a, "deaths": d}
    return result, None


@bot.message_handler(func=lambda m: (
    m.from_user.id in match_registration
    and match_registration[m.from_user.id].get("step") == "all_kills"
    and m.chat.id == match_registration[m.from_user.id].get("reply_chat_id", m.from_user.id)
))
def reg_step_all_kills(msg):
    uid = msg.from_user.id
    if not is_game_reg_check(uid):
        return
    data = match_registration[uid]
    ct_players = data.get("ct_players", [])
    t_players  = data.get("t_players",  [])
    all_players = ct_players + t_players
    parsed, err = _parse_all_kda(msg.text.strip() if msg.text else "", all_players)
    if err:
        reg_send(uid, err, parse_mode="HTML")
        return
    data["kills_data"].update(parsed)
    match_key = data["match_key"]
    try:
        _finalize_match(uid, match_key)
    except Exception as _fe:
        import traceback
        tb = traceback.format_exc()
        print(f"[finalize_match ERROR] {_fe}\n{tb}")
        reg_send(uid, f"❌ <b>Ошибка при регистрации матча:</b>\n<code>{_fe}</code>", parse_mode="HTML")
        # Восстанавливаем данные чтобы можно было повторить
        if uid not in match_registration:
            match_registration[uid] = data
            match_registration[uid]["step"] = "all_kills"


def _cleanup_match_messages(lobby):
    """Удаляет мусорные сообщения после завершения/отмены матча."""
    # 1. Удаляем "МАТЧ НАЧАЛСЯ!" у каждого игрока в личке
    for uid, mid in list(lobby.get("player_start_msgs", {}).items()):
        try:
            bot.delete_message(uid, mid)
        except Exception:
            pass
    lobby.pop("player_start_msgs", None)

    # 2. Убираем кнопки с admin-сообщения "МАТЧ НАЧАЛСЯ" (не удаляем — полезно для контекста)
    admin_msg_id = lobby.get("admin_msg_id")
    if ADMIN_CHAT_ID and admin_msg_id:
        try:
            bot.edit_message_reply_markup(ADMIN_CHAT_ID, admin_msg_id, reply_markup=None)
        except Exception:
            pass

    # 3. Чистим awaiting_ACreenshot для всех игроков этого матча
    match_key = lobby.get("match_key")
    for uid in list(lobby.get("team_ct", [])) + list(lobby.get("team_t", [])):
        if awaiting_ACreenshot.get(uid) == match_key:
            awaiting_ACreenshot.pop(uid, None)


def _finalize_match(reg_uid, match_key):
    data = match_registration.pop(reg_uid, {})
    lobby = running_matches.get(match_key)
    if not lobby:
        reg_send(reg_uid, "❌ Матч не найден")
        return
    winner   = data.get("winner", "ct")
    ACore_w  = data.get("ACore_w", 0)
    ACore_l  = data.get("ACore_l", 0)
    kills_data = data.get("kills_data", {})
    team_ct  = lobby.get("team_ct", [])
    team_t   = lobby.get("team_t",  [])
    winner_team = team_ct if winner == "ct" else team_t
    loser_team  = team_t  if winner == "ct" else team_ct
    all_stats = {}
    is_quals_match = (lobby.get("league") == "quals")
    is_2v2_match   = (lobby.get("league") == "2v2")
    # Resolve private table for this match
    private_key = lobby.get("private", "darling")
    priv_cfg    = PRIVATE_CONFIG.get(private_key, PRIVATE_CONFIG["darling"])
    priv_table  = priv_cfg["table"]
    priv_label  = f"{priv_cfg['emoji']} {priv_cfg['display']}"
    conn = None
    try:
        conn = _db()
        cur = conn.cursor()
        for _uid in team_ct + team_t:
            if is_bot_player(_uid):
                continue
            won = _uid in winner_team
            kda = kills_data.get(_uid, {"kills": 0, "deaths": 0, "assists": 0})
            kills = kda["kills"]
            if is_2v2_match:
                # 2v2: >9 убийств → +17 ELO, ≤9 → +12 ELO
                if won:
                    elo_change = 17 if kills > 9 else 12
                    coins_reward = 15
                else:
                    elo_change = -12 if kills > 9 else -20
                    coins_reward = 4
            else:
                # 5v5 (Default / Quals): ≥12 убийств → +25/−15, иначе +17/−23
                if won:
                    elo_change = 25 if kills >= 12 else 17
                    coins_reward = 15
                else:
                    elo_change = -15 if kills >= 12 else -23
                    coins_reward = 4
            # Ensure player row exists in priv_table (defensive upsert for cross-private users)
            if priv_table != "players":
                _src = get_player(_uid)
                if _src:
                    try:
                        cur.execute(
                            f"""INSERT INTO {priv_table}
                                (user_id, username, game_id, device, registered, coins, elo,
                                 tg_username, is_admin, is_bot)
                                VALUES (%s, %s, %s, %s, 1, 100, 1000, %s, %s, %s)
                                ON CONFLICT (user_id) DO NOTHING""",
                            (_uid, _src[1], _src[2], _src[3],
                             _src[21] if len(_src) > 21 else "",
                             _src[11], _src[13]),
                        )
                    except Exception as _ue:
                        print(f"[upsert player in {priv_table}] {_ue}")

            p = get_player_from_table(_uid, priv_table) or get_player(_uid)
            if p:
                try:
                    prem = has_active_premium(_uid)
                    if prem:
                        if won:
                            elo_change = int(elo_change * 1.5)
                        coins_reward = int(coins_reward * 1.5)
                except Exception as _pe:
                    print(f"[premium check error] uid={_uid}: {_pe}")

            if is_2v2_match:
                # 2v2 матч: обновляем только duo-колонки + монеты
                if won:
                    cur.execute(
                        f"UPDATE {priv_table} SET duo_wins=duo_wins+1, "
                        "duo_elo=GREATEST(0, duo_elo+%s), "
                        "duo_kills=duo_kills+%s, duo_deaths=duo_deaths+%s, "
                        "duo_assists=duo_assists+%s, coins=coins+%s WHERE user_id=%s",
                        (elo_change, kda["kills"], kda["deaths"], kda["assists"], coins_reward, _uid),
                    )
                else:
                    cur.execute(
                        f"UPDATE {priv_table} SET duo_losses=duo_losses+1, "
                        "duo_elo=GREATEST(0, duo_elo+%s), "
                        "duo_kills=duo_kills+%s, duo_deaths=duo_deaths+%s, "
                        "duo_assists=duo_assists+%s, coins=coins+%s WHERE user_id=%s",
                        (elo_change, kda["kills"], kda["deaths"], kda["assists"], coins_reward, _uid),
                    )
            elif is_quals_match:
                # Quals матч: обновляем только quals-колонки + монеты
                if won:
                    cur.execute(
                        f"UPDATE {priv_table} SET quals_wins=quals_wins+1, "
                        "quals_elo=GREATEST(0, quals_elo+%s), "
                        "quals_kills=quals_kills+%s, quals_deaths=quals_deaths+%s, "
                        "quals_assists=quals_assists+%s, coins=coins+%s WHERE user_id=%s",
                        (elo_change, kda["kills"], kda["deaths"], kda["assists"], coins_reward, _uid),
                    )
                else:
                    cur.execute(
                        f"UPDATE {priv_table} SET quals_losses=quals_losses+1, "
                        "quals_elo=GREATEST(0, quals_elo+%s), "
                        "quals_kills=quals_kills+%s, quals_deaths=quals_deaths+%s, "
                        "quals_assists=quals_assists+%s, coins=coins+%s WHERE user_id=%s",
                        (elo_change, kda["kills"], kda["deaths"], kda["assists"], coins_reward, _uid),
                    )
            else:
                # Default матч: обновляем только default-колонки + монеты
                if won:
                    cur.execute(
                        f"UPDATE {priv_table} SET wins=wins+1, elo=GREATEST(0, elo+%s), kills=kills+%s, deaths=deaths+%s, assists=assists+%s, coins=coins+%s WHERE user_id=%s",
                        (elo_change, kda["kills"], kda["deaths"], kda["assists"], coins_reward, _uid),
                    )
                else:
                    cur.execute(
                        f"UPDATE {priv_table} SET losses=losses+1, elo=GREATEST(0, elo+%s), kills=kills+%s, deaths=deaths+%s, assists=assists+%s, coins=coins+%s WHERE user_id=%s",
                        (elo_change, kda["kills"], kda["deaths"], kda["assists"], coins_reward, _uid),
                    )
            all_stats[_uid] = {**kda, "won": won, "elo_change": elo_change, "coins_reward": coins_reward}
        conn.commit()
    except Exception as _dbe:
        print(f"[_finalize_match DB ERROR] {_dbe}")
        import traceback; traceback.print_exc()
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
    # ===== MVP: найти игрока с наибольшим количеством киллов и +1 к счётчику =====
    mvp_uid = None
    max_kills_mvp = -1
    for _uid, s in all_stats.items():
        if s["kills"] > max_kills_mvp:
            max_kills_mvp = s["kills"]
            mvp_uid = _uid
    if mvp_uid and max_kills_mvp >= 0:
        try:
            _mc = _db()
            _mcc = _mc.cursor()
            _mcc.execute(f"UPDATE {priv_table} SET mvp_count=mvp_count+1 WHERE user_id=%s", (mvp_uid,))
            _mc.commit()
            _mc.close()
        except Exception as _me:
            print(f"[mvp_count update] {_me}")

    _cleanup_match_messages(lobby)
    save_match_to_history(lobby, {"winner": winner, "ACore_w": ACore_w, "ACore_l": ACore_l}, all_stats)
    # Сохраняем данные для возможного отката при перерегистрации
    lobby["all_stats"]  = all_stats
    lobby["priv_table"] = priv_table
    lobby["mvp_uid"]    = mvp_uid
    # Оставляем лобби в running_matches со статусом "registered",
    # чтобы кнопка "🔄 Перерегать" могла найти матч и сбросить регистрацию.
    lobby["status"] = "registered"
    match_id = lobby.get("match_id", "?")
    winner_lines = []
    loser_lines  = []
    for uid, s in all_stats.items():
        p = get_player_from_table(uid, priv_table) or get_player(uid)
        name = p[1] if p else str(uid)
        sign = "+" if s["elo_change"] >= 0 else ""
        line = (
            f"👤 <b>{name}</b>\n"
            f"   🔫 Киллы: {s['kills']}  🤝 Помощи: {s['assists']}  💀 Смерти: {s['deaths']}\n"
            f"   📊 ELO: {sign}{s['elo_change']}  🪙 Коины: +{s['coins_reward']}"
        )
        if s["won"]:
            winner_lines.append(line)
        else:
            loser_lines.append(line)

    winner_team_label = "💙 CT" if winner == "ct" else "🧡 T"
    loser_team_label  = "🧡 T"  if winner == "ct" else "💙 CT"

    match_code = lobby.get("match_code", str(match_id))
    result_text = (
        f"🏁 <b>Матч #{match_code} завершён!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏠 Привате: <b>{priv_label}</b>\n"
        f"🗺 Карта: {lobby.get('map_name', '?')}  |  Счёт: <b>{ACore_w}:{ACore_l}</b>\n"
        f"🏷 Лига: {format_league(lobby.get('league','default'))}\n"
        f"🏆 Победитель: <b>{winner_team_label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✅ <b>{winner_team_label} — Победа</b>\n"
        + "\n\n".join(winner_lines)
        + f"\n\n❌ <b>{loser_team_label} — Поражение</b>\n"
        + "\n\n".join(loser_lines)
    )
    # ── Карточка результата матча ────────────────────────────────────────────
    _card_buf  = None
    _card_sent = False
    if CARDS_ENABLED:
        try:
            _players_ct, _players_t = [], []
            for _uid in team_ct:
                if _uid not in all_stats:
                    continue
                _s   = all_stats[_uid]
                _p2  = get_player_from_table(_uid, priv_table) or get_player(_uid)
                _players_ct.append({
                    "name":    _p2[1] if _p2 else str(_uid),
                    "kills":   _s["kills"],
                    "deaths":  _s["deaths"],
                    "assists": _s["assists"],
                    "elo":     _p2[4] if _p2 else 1000,
                })
            for _uid in team_t:
                if _uid not in all_stats:
                    continue
                _s   = all_stats[_uid]
                _p2  = get_player_from_table(_uid, priv_table) or get_player(_uid)
                _players_t.append({
                    "name":    _p2[1] if _p2 else str(_uid),
                    "kills":   _s["kills"],
                    "deaths":  _s["deaths"],
                    "assists": _s["assists"],
                    "elo":     _p2[4] if _p2 else 1000,
                })
            # Аватары игроков — сначала из кэша, потом из Telegram
            _avatars = {}
            for _uid in team_ct + team_t:
                _av = get_cached_avatar(_uid)
                if _av is None:
                    _av = get_user_avatar(_uid)
                    if _av:
                        cache_avatar(_uid, _av)
                if _av:
                    _avatars[_uid] = _av

            _card_buf = generate_match_result_card(
                match_code = match_code,
                map_name   = lobby.get("map_name", ""),
                winner     = winner,
                score_w    = ACore_w,
                score_l    = ACore_l,
                players_ct = _players_ct,
                players_t  = _players_t,
                league     = format_league(lobby.get("league", "default")),
                avatars    = _avatars if _avatars else None,
            )
            _caption = (
                f"🏁 <b>Матч #{match_code}</b>  |  "
                f"{lobby.get('map_name','?')}  |  {ACore_w}:{ACore_l}\n"
                f"🏆 Победитель: <b>{winner_team_label}</b>  "
                f"🏷 {format_league(lobby.get('league','default'))}"
            )
            # Отправляем карточку каждому игроку
            for _uid in all_stats:
                try:
                    _card_buf.seek(0)
                    bot.send_photo(_uid, _card_buf, caption=_caption, parse_mode="HTML")
                except Exception:
                    pass
            # Карточку + короткий текст в лог-канал
            if LOG_CHAT_ID:
                try:
                    _log_kw = {"parse_mode": "HTML"}
                    _tid = _dynamic_results_thread_id if _dynamic_results_thread_id else _dynamic_log_thread_id
                    if _tid:
                        _log_kw["message_thread_id"] = _tid
                    _card_buf.seek(0)
                    bot.send_photo(LOG_CHAT_ID, _card_buf, caption=_caption, **_log_kw)
                except Exception as _le:
                    print(f"[card_log] {_le}")
            _card_sent = True
        except Exception as _ce:
            print(f"[card_match_result] ошибка: {_ce}")

    # Текстовый fallback если карточка не сгенерирована
    if not _card_sent:
        for uid in all_stats:
            try:
                bot.send_message(uid, result_text, parse_mode="HTML")
            except Exception:
                pass
        send_result_log(
            f"🏁 <b>Матч #{match_code} завершён</b>\n"
            f"🗺 {lobby.get('map_name','?')} | Счёт: <b>{ACore_w}:{ACore_l}</b>\n"
            f"🏆 Победитель: <b>{winner_team_label}</b>\n"
            f"🏷 {format_league(lobby.get('league',''))}/{lobby.get('device','').upper()}"
        )
    # ─────────────────────────────────────────────────────────────────────────

    reg_send(reg_uid, f"✅ Матч #{match_code} зарегистрирован!\n\n{result_text}", parse_mode="HTML")

    # Отправляем "Матч Зарегистрирован" в ветку + закрываем тему
    if ADMIN_CHAT_ID and lobby.get("admin_thread_id"):
        thread_id = lobby["admin_thread_id"]
        try:
            # Кнопки для админов остаются (на случай перерегистрации/отмены)
            kb_done = types.InlineKeyboardMarkup(row_width=1)
            kb_done.add(
                types.InlineKeyboardButton("🔄 Перерегать",  callback_data=f"reregister_match|{match_key}"),
                types.InlineKeyboardButton("❌ Отменить",    callback_data=f"cancel_match|{match_key}"),
            )
            bot.send_message(
                ADMIN_CHAT_ID,
                f"✅ <b>Матч #{match_code} Зарегистрирован!</b>\n"
                f"🏆 Победитель: <b>{winner_team_label}</b> | {ACore_w}:{ACore_l}",
                message_thread_id=thread_id,
                reply_markup=kb_done,
                parse_mode="HTML",
            )
            bot.close_forum_topic(ADMIN_CHAT_ID, thread_id)
        except Exception as e:
            print(f"Close topic error: {e}")


@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel_match|"))
def cb_cancel_match(c):
    uid = c.from_user.id
    if not is_game_reg_check(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    match_key = c.data.split("|", 1)[1]
    lobby = running_matches.get(match_key)
    if not lobby:
        # Матч уже завершён, но ветка закрыта и нажали кнопку — ничего не делаем
        bot.answer_callback_query(c.id, "❌ Матч не найден")
        return
    # Запрашиваем причину отмены
    cancel_flow[uid] = {
        "match_key": match_key,
        "chat_id": c.message.chat.id,
        "thread_id": getattr(c.message, "message_thread_id", None),
        "msg_id": c.message.message_id,
    }
    bot.answer_callback_query(c.id)
    send_kw = {"parse_mode": "HTML"}
    thread_id = getattr(c.message, "message_thread_id", None)
    if thread_id:
        send_kw["message_thread_id"] = thread_id
    match_id   = lobby.get("match_id", "?")
    match_code = lobby.get("match_code", str(match_id))
    bot.send_message(
        c.message.chat.id,
        f"❌ <b>Отмена матча #{match_code}</b>\n\nВведите причину отмены:",
        **send_kw,
    )


@bot.message_handler(func=lambda m: m.from_user.id in cancel_flow and m.text is not None)
def handle_cancel_reason(msg):
    uid = msg.from_user.id
    if not is_game_reg_check(uid):
        cancel_flow.pop(uid, None)
        return
    data = cancel_flow.pop(uid)
    match_key = data["match_key"]
    reason = msg.text.strip()
    lobby = running_matches.get(match_key)
    if not lobby:
        bot.send_message(uid, "❌ Матч уже не существует")
        return
    match_id   = lobby.get("match_id", "?")
    match_code = lobby.get("match_code", str(match_id))
    for puid in lobby.get("team_ct", []) + lobby.get("team_t", []):
        if is_bot_player(puid):
            continue
        try:
            bot.send_message(puid, f"❌ <b>Матч #{match_code} отменён администратором.</b>\n\n📝 Причина: {reason}", parse_mode="HTML")
        except Exception:
            pass
    _cleanup_match_messages(lobby)
    lobby["status"] = "cancelled"
    running_matches.pop(match_key, None)
    # Сохраняем отменённый матч в БД
    try:
        save_match_cancelled(lobby, reason)
    except Exception as e:
        print(f"save_match_cancelled error: {e}")

    thread_id = data.get("thread_id")
    try:
        reply_kw = {"parse_mode": "HTML"}
        if thread_id:
            reply_kw["message_thread_id"] = thread_id
        bot.send_message(
            data["chat_id"],
            f"✅ Матч #{match_code} отменён.\n📝 Причина: <b>{reason}</b>",
            **reply_kw,
        )
        if thread_id:
            bot.close_forum_topic(ADMIN_CHAT_ID, thread_id)
    except Exception:
        pass

    # Лог отмены в паблик
    admin_p = get_player(uid)
    admin_name = admin_p[1] if admin_p else str(uid)
    send_punishment_log_priv(uid,
        f"❌ <b>Матч #{match_code} отменён</b>\n"
        f"👮 Администратор: {tg_link(uid, admin_name)}\n"
        f"📝 Причина: <b>{reason}</b>\n"
        f"🏷 {lobby.get('league','').upper()}/{lobby.get('device','').upper()}"
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("reregister_match|"))
def cb_reregister_match(c):
    uid = c.from_user.id
    if not is_game_reg_check(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    match_key = c.data.split("|", 1)[1]
    lobby = running_matches.get(match_key)
    if not lobby:
        bot.answer_callback_query(c.id, "❌ Матч не найден")
        return
    lobby["reg_taken_by"] = None
    match_registration.pop(uid, None)
    # Откатываем ранее начисленную статистику (ELO, wins/losses, kills, coins, MVP)
    rollback_match_stats(lobby)
    lobby["status"] = "active"
    match_id   = lobby.get("match_id", "?")
    match_code = lobby.get("match_code", str(match_id))
    # Сбрасываем статус в БД чтобы после рестарта бота матч восстановился корректно
    try:
        _rconn = _db()
        _rcur  = _rconn.cursor()
        _rcur.execute("UPDATE matches SET status='active' WHERE match_id=%s", (match_id,))
        _rconn.commit()
        _rconn.close()
    except Exception as _re:
        print(f"[reregister status reset] {_re}")
    bot.answer_callback_query(c.id, "🔄 Регистрация сброшена, статистика откатана")

    # Переоткрываем тему если была закрыта
    thread_id = lobby.get("admin_thread_id")
    if thread_id and ADMIN_CHAT_ID:
        try:
            bot.reopen_forum_topic(ADMIN_CHAT_ID, thread_id)
        except Exception:
            pass
        try:
            AC = lobby.get("ACreenshots_count", 0)
            new_kb = _build_admin_match_kb(match_key, match_code, AC, taken_by=None)
            kw = {"parse_mode": "HTML", "reply_markup": new_kb, "message_thread_id": thread_id}
            bot.send_message(ADMIN_CHAT_ID, f"🔄 Матч #{match_code} отправлен на перерегистрацию.", **kw)
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data == "noop")
def cb_noop(c):
    bot.answer_callback_query(c.id)


# ==================== МАГАЗИН ====================
@bot.callback_query_handler(func=lambda c: c.data == "shop")
def cb_shop(c):
    uid = c.from_user.id
    err = check_blocked(uid)
    if err:
        bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    for cat, name in CATEGORY_NAMES.items():
        if cat == "decor":
            kb.add(types.InlineKeyboardButton("🔧 Декор (Тех. работы)", callback_data="shop_cat_decor"))
        else:
            kb.add(types.InlineKeyboardButton(name, callback_data=f"shop_cat_{cat}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    bot.edit_message_text("🛒 <b>МАГАЗИН</b>\n\nВыберите категорию:", c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("shop_cat_"))
def cb_shop_category(c):
    uid = c.from_user.id
    category = c.data.split("shop_cat_")[1]
    # ДЕКОР — технические работы
    if category == "decor":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="shop"))
        bot.edit_message_text(
            "🔧 <b>Декор — Технические работы</b>\n\n"
            "⚙️ Раздел временно недоступен.\n"
            "Приносим извинения за неудобства!",
            c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML",
        )
        bot.answer_callback_query(c.id)
        return
    items = get_shop_items_by_category(category)
    p = get_player(uid)
    coins = p[5] if p else 0
    cat_name = CATEGORY_NAMES.get(category, category)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for item_id, name, deAC, price, item_type in items:
        owned = has_item_in_inventory(uid, item_id)
        label = f"{'✅ ' if owned else ''}{name} — {price} AC"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"shop_item_{item_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="shop"))
    bot.edit_message_text(
        f"{cat_name}\n💰 Баланс: <b>{coins} AC</b>\n\nВыберите товар:",
        c.message.chat.id, c.message.message_id, reply_markup=kb,
    )
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("shop_item_"))
def cb_shop_item(c):
    uid = c.from_user.id
    item_id = int(c.data.split("shop_item_")[1])
    item = get_shop_item(item_id)
    if not item:
        bot.answer_callback_query(c.id, "❌ Товар не найден")
        return
    _, name, deAC, category, price, item_type = item
    p = get_player(uid)
    coins = p[5] if p else 0
    owned = has_item_in_inventory(uid, item_id)
    icon = CATEGORY_ICONS.get(category, "")
    text = (
        f"{icon} <b>{name}</b>\n\n"
        f"📝 {deAC}\n"
        f"💰 Цена: <b>{price} AC</b>\n"
        f"💳 Ваш баланс: <b>{coins} AC</b>\n"
        + ("✅ Уже куплено\n" if owned else "")
    )
    kb = types.InlineKeyboardMarkup()
    if not owned or item_type in {"sticker", "unwarn", "x2coins", "rename"}:
        kb.add(types.InlineKeyboardButton(f"💳 Купить за {price} AC", callback_data=f"shop_buy_{item_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"shop_cat_{category}"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("shop_buy_"))
def cb_shop_buy(c):
    uid = c.from_user.id
    item_id = int(c.data.split("shop_buy_")[1])
    # Блокируем покупку декора — раздел на тех. работах
    _item_check = get_shop_item(item_id)
    if _item_check and _item_check[3] == "decor":
        bot.answer_callback_query(c.id, "🔧 Декор временно недоступен (тех. работы)", show_alert=True)
        return
    ok, msg = buy_item(uid, item_id)
    bot.answer_callback_query(c.id, msg[:200], show_alert=not ok)
    if ok:
        item = get_shop_item(item_id)
        if item:
            bot.edit_message_text(msg, c.message.chat.id, c.message.message_id)


# ==================== ИНВЕНТАРЬ ====================
@bot.callback_query_handler(func=lambda c: c.data == "inv")
def cb_inventory(c):
    uid = c.from_user.id
    err = check_blocked(uid)
    if err:
        bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
        return
    items = get_inventory(uid)
    if not items:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🛒 В магазин", callback_data="shop"))
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
        bot.edit_message_text("🎒 <b>Инвентарь пуст</b>", c.message.chat.id, c.message.message_id, reply_markup=kb)
        bot.answer_callback_query(c.id)
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    for inv_id, name, category, item_type, purchased_at, is_activated, shop_id in items:
        status = "✅ " if is_activated else ""
        kb.add(types.InlineKeyboardButton(f"{status}{name}", callback_data=f"inv_item_{inv_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    bot.edit_message_text("🎒 <b>Ваш инвентарь:</b>", c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("inv_item_"))
def cb_inv_item(c):
    uid = c.from_user.id
    inv_id = int(c.data.split("inv_item_")[1])
    items = get_inventory(uid)
    item = next((i for i in items if i[0] == inv_id), None)
    if not item:
        bot.answer_callback_query(c.id, "❌ Предмет не найден")
        return
    inv_id2, name, category, item_type, purchased_at, is_activated, shop_id = item
    dt = datetime.datetime.fromtimestamp(purchased_at).strftime("%d.%m.%Y") if purchased_at else "?"
    text = (
        f"🎒 <b>{name}</b>\n\n"
        f"Категория: {CATEGORY_NAMES.get(category, category)}\n"
        f"Куплено: {dt}\n"
        f"Статус: {'✅ Активировано' if is_activated else '⏳ Не активировано'}"
    )
    kb = types.InlineKeyboardMarkup()
    if not is_activated:
        kb.add(types.InlineKeyboardButton("⚡ Активировать", callback_data=f"inv_activate_{inv_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="inv"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("inv_activate_"))
def cb_inv_activate(c):
    uid = c.from_user.id
    inv_id = int(c.data.split("inv_activate_")[1])
    items = get_inventory(uid)
    item = next((i for i in items if i[0] == inv_id), None)
    if not item:
        bot.answer_callback_query(c.id, "❌ Предмет не найден")
        return
    inv_id2, name, category, item_type, purchased_at, is_activated, shop_id = item
    result, msg = activate_inventory_item(inv_id, uid, item_type, name)
    if result == "rename":
        rename_flow[uid] = inv_id
        bot.answer_callback_query(c.id)
        bot.send_message(uid, msg)
        return
    bot.answer_callback_query(c.id, msg[:200], show_alert=not result)
    if result:
        try:
            bot.edit_message_text(msg, c.message.chat.id, c.message.message_id)
        except Exception:
            pass


@bot.message_handler(func=lambda m: m.from_user.id in rename_flow and m.text is not None)
def handle_rename(msg):
    uid = msg.from_user.id
    inv_id = rename_flow.pop(uid)
    new_nick = msg.text.strip()
    if not (2 <= len(new_nick) <= 20):
        bot.send_message(uid, "❌ Никнейм 2-20 символов")
        return
    if nick_taken(new_nick, exclude_uid=uid):
        bot.send_message(uid, "❌ Этот никнейм уже занят!")
        return
    conn = _db()
    cur = conn.cursor()
    cur.execute("UPDATE players SET username=%s WHERE user_id=%s", (new_nick, uid))
    cur.execute(
        "UPDATE inventory SET is_activated=1, activated_at=%s WHERE id=%s",
        (int(time.time()), inv_id),
    )
    conn.commit()
    conn.close()
    bot.send_message(uid, f"✅ Никнейм изменён на <b>{new_nick}</b>!")


# ==================== ПАТИ ====================
@bot.callback_query_handler(func=lambda c: c.data == "party_menu")
def cb_party_menu(c):
    uid = c.from_user.id
    err = check_blocked(uid)
    if err:
        bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
        return
    party = get_party_of(uid)
    if party:
        members_text = "\n".join([
            f"  {'👑' if m == party['leader'] else '👤'} {get_player(m)[1] if get_player(m) else m}"
            for m in party["members"]
        ])
        max_size = get_party_max_size(party)
        text = f"👥 <b>Ваша пати</b> ({len(party['members'])}/{max_size}):\n{members_text}"
        kb = types.InlineKeyboardMarkup(row_width=1)
        if uid == party["leader"]:
            kb.add(
                types.InlineKeyboardButton("➕ Пригласить", callback_data="party_invite"),
                types.InlineKeyboardButton("🗑 Распустить пати", callback_data="party_disband"),
            )
        else:
            kb.add(types.InlineKeyboardButton("🚪 Покинуть пати", callback_data="party_leave"))
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    else:
        text = "👥 У вас нет пати.\nСоздайте пати или примите приглашение."
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("➕ Создать пати", callback_data="party_create"),
            types.InlineKeyboardButton("🔙 Назад", callback_data="back"),
        )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "party_create")
def cb_party_create(c):
    uid = c.from_user.id
    if uid in user_party:
        bot.answer_callback_query(c.id, "❌ Вы уже в пати")
        return
    party_id = f"party_{uid}_{int(time.time())}"
    parties[party_id] = {"leader": uid, "members": [uid]}
    user_party[uid] = party_id
    bot.answer_callback_query(c.id, "✅ Пати создана!")
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("👥 Управление пати", callback_data="party_menu"))
    bot.edit_message_text("✅ <b>Пати создана!</b>", c.message.chat.id, c.message.message_id, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "party_invite")
def cb_party_invite(c):
    uid = c.from_user.id
    party = get_party_of(uid)
    if not party or party["leader"] != uid:
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    max_size = get_party_max_size(party)
    if len(party["members"]) >= max_size:
        bot.answer_callback_query(c.id, f"❌ Пати полная ({max_size} чел.)", show_alert=True)
        return
    awaiting_party_invite[uid] = True
    bot.answer_callback_query(c.id)
    bot.send_message(uid, "👤 Введите Telegram ID или никнейм игрока для приглашения:")


@bot.message_handler(func=lambda m: m.from_user.id in awaiting_party_invite and m.text is not None)
def handle_party_invite(msg):
    uid = msg.from_user.id
    awaiting_party_invite.pop(uid, None)
    text = msg.text.strip()
    target = None
    if text.isdigit():
        target = get_player(int(text))
    else:
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM players WHERE username=%s AND is_bot=0", (text,))
        target = cur.fetchone()
        conn.close()
    if not target:
        bot.send_message(uid, "❌ Игрок не найден")
        return
    target_id = target[0]
    if target_id == uid:
        bot.send_message(uid, "❌ Нельзя пригласить себя")
        return
    if target_id in user_party:
        bot.send_message(uid, "❌ Игрок уже в пати")
        return
    party_id = user_party.get(uid)
    party = get_party_of(uid)
    if not party:
        bot.send_message(uid, "❌ У вас нет пати")
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Принять", callback_data=f"party_accept_{party_id}_{uid}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data="party_decline"),
    )
    try:
        inviter = get_player(uid)
        bot.send_message(
            target_id,
            f"👥 <b>{inviter[1] if inviter else uid}</b> приглашает вас в пати!\nМакс. размер: {get_party_max_size(party)}",
            reply_markup=kb,
        )
        bot.send_message(uid, f"✅ Приглашение отправлено игроку <b>{target[1]}</b>!")
    except Exception:
        bot.send_message(uid, "❌ Не удалось отправить приглашение")


@bot.callback_query_handler(func=lambda c: c.data.startswith("party_accept_"))
def cb_party_accept(c):
    uid = c.from_user.id
    raw = c.data[len("party_accept_"):]
    party_id = "_".join(raw.split("_")[:-1])
    party = parties.get(party_id)
    if not party:
        bot.answer_callback_query(c.id, "❌ Пати не существует")
        return
    if uid in user_party:
        bot.answer_callback_query(c.id, "❌ Вы уже в пати")
        return
    max_size = get_party_max_size(party)
    if len(party["members"]) >= max_size:
        bot.answer_callback_query(c.id, "❌ Пати уже полная", show_alert=True)
        return
    party["members"].append(uid)
    user_party[uid] = party_id
    bot.answer_callback_query(c.id, "✅ Вы вступили в пати!")
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    except Exception:
        pass
    p = get_player(uid)
    for m in party["members"]:
        if m == uid:
            continue
        try:
            bot.send_message(m, f"✅ <b>{p[1] if p else uid}</b> вступил в пати!")
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data == "party_decline")
def cb_party_decline(c):
    bot.answer_callback_query(c.id, "❌ Приглашение отклонено")
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data == "party_leave")
def cb_party_leave(c):
    uid = c.from_user.id
    party = get_party_of(uid)
    if not party:
        bot.answer_callback_query(c.id, "❌ Вы не в пати")
        return
    party_id = user_party.pop(uid)
    party["members"].remove(uid)
    if not party["members"]:
        parties.pop(party_id, None)
    elif party["leader"] == uid:
        party["leader"] = party["members"][0]
    bot.answer_callback_query(c.id, "✅ Вы покинули пати")
    bot.edit_message_text(main_menu_text(uid), c.message.chat.id, c.message.message_id, reply_markup=main_menu(uid), parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data == "party_disband")
def cb_party_disband(c):
    uid = c.from_user.id
    party = get_party_of(uid)
    if not party or party["leader"] != uid:
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    party_id = user_party.get(uid)
    for m in list(party["members"]):
        user_party.pop(m, None)
        if m != uid:
            try:
                bot.send_message(m, "👥 Пати распущена лидером.")
            except Exception:
                pass
    parties.pop(party_id, None)
    bot.answer_callback_query(c.id, "✅ Пати распущена")
    bot.edit_message_text(main_menu_text(uid), c.message.chat.id, c.message.message_id, reply_markup=main_menu(uid), parse_mode="HTML")


# ==================== ПОКУПКА МОНЕТ ====================
@bot.callback_query_handler(func=lambda c: c.data == "buy_coins")
def cb_buy_coins(c):
    uid = c.from_user.id
    err = check_blocked(uid)
    if err:
        bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
        return
    if not is_registered(uid):
        bot.answer_callback_query(c.id, "❌ Сначала зарегистрируйтесь /start")
        return
    p = get_player(uid)
    coins = p[5] if p else 0
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, (name, coins_amount, stars, price_label) in enumerate(COIN_PACKAGES):
        kb.add(types.InlineKeyboardButton(f"⭐ {name}: {coins_amount} AC — {stars} Stars ({price_label})", callback_data=f"buy_pkg_{i}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    bot.edit_message_text(
        f"💳 <b>КУПИТЬ SareCoin</b>\n💰 Баланс: <b>{coins} AC</b>\n\n⭐ Telegram Stars\nВыберите пакет:",
        c.message.chat.id, c.message.message_id, reply_markup=kb,
    )
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_pkg_"))
def cb_buy_package(c):
    uid = c.from_user.id
    pkg_idx = int(c.data.split("buy_pkg_")[1])
    if pkg_idx < 0 or pkg_idx >= len(COIN_PACKAGES):
        bot.answer_callback_query(c.id, "❌ Пакет не найден")
        return
    name, coins_amount, stars, price_label = COIN_PACKAGES[pkg_idx]
    bot.answer_callback_query(c.id)
    try:
        bot.send_invoice(
            chat_id=uid,
            title=f"💰 {coins_amount} SareCoin",
            deACription=f"Пакет «{name}»: {coins_amount} AC для Actual FACEIT",
            invoice_payload=f"coins_{pkg_idx}_{uid}",
            provider_token="",
            currency="XTR",
            prices=[types.LabeledPrice(label=f"{coins_amount} AC", amount=stars)],
            start_parameter=f"buy_coins_{pkg_idx}",
        )
    except Exception as e:
        bot.send_message(uid, f"❌ Ошибка создания счёта: {e}")


@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(query):
    bot.answer_pre_checkout_query(query.id, ok=True)


@bot.message_handler(content_types=["successful_payment"])
def successful_payment(msg):
    uid = msg.from_user.id
    payload = msg.successful_payment.invoice_payload
    try:
        _, pkg_idx_str, _ = payload.split("_", 2)
        name, coins_amount, stars, _ = COIN_PACKAGES[int(pkg_idx_str)]
        add_coins_to_player(uid, coins_amount)
        p = get_player(uid)
        bot.send_message(uid, f"✅ <b>Оплата прошла!</b>\n💰 Начислено: <b>{coins_amount} AC</b>\n💳 Баланс: <b>{p[5] if p else '?'} AC</b>")
        if ADMIN_ID:
            bot.send_message(ADMIN_ID, f"💳 Покупка!\nПользователь: {uid}\nПакет: {name} ({coins_amount} AC)\nОплачено: {stars} Stars")
    except Exception as e:
        bot.send_message(uid, f"✅ Оплата получена, монеты будут начислены вручную. Ошибка: {e}")


# ==================== РЕДАКТИРОВАНИЕ СТАТЫ ====================
STAT_FIELDS = {
    "kills":   ("kills",   "🔫 Убийства"),
    "deaths":  ("deaths",  "💀 Смерти"),
    "assists": ("assists", "🤝 Ассисты"),
    "wins":    ("wins",    "🏆 Победы"),
    "losses":  ("losses",  "❌ Поражения"),
    "coins":   ("coins",   "💰 Монеты"),
    "elo":     ("elo",     "📊 ELO"),
}

QUALS_STAT_FIELDS = {
    "quals_elo":     ("quals_elo",     "⭐ Quals ELO"),
    "quals_wins":    ("quals_wins",    "🏆 Quals Победы"),
    "quals_losses":  ("quals_losses",  "❌ Quals Поражения"),
    "quals_kills":   ("quals_kills",   "🔫 Quals Убийства"),
    "quals_deaths":  ("quals_deaths",  "💀 Quals Смерти"),
    "quals_assists": ("quals_assists", "🤝 Quals Ассисты"),
}

DUO_STAT_FIELDS = {
    "duo_elo":     ("duo_elo",     "👥 2v2 ELO"),
    "duo_wins":    ("duo_wins",    "🏆 2v2 Победы"),
    "duo_losses":  ("duo_losses",  "❌ 2v2 Поражения"),
    "duo_kills":   ("duo_kills",   "🔫 2v2 Убийства"),
    "duo_deaths":  ("duo_deaths",  "💀 2v2 Смерти"),
    "duo_assists": ("duo_assists", "🤝 2v2 Ассисты"),
}

@bot.callback_query_handler(func=lambda c: c.data.startswith("editstatlg_"))
def cb_editstat_league(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    parts = c.data.split("_", 3)
    league_type = parts[1]
    target_id   = int(parts[2])
    p = get_player(target_id)
    if not p:
        bot.answer_callback_query(c.id, "❌ Игрок не найден")
        return
    bot.answer_callback_query(c.id)
    if league_type == "duo":
        fields = DUO_STAT_FIELDS
    elif league_type == "quals":
        fields = QUALS_STAT_FIELDS
    else:
        fields = STAT_FIELDS
    kb = types.InlineKeyboardMarkup(row_width=2)
    for field_key, (db_f, label) in fields.items():
        kb.add(types.InlineKeyboardButton(f"✏️ {label}", callback_data=f"editstat_{field_key}_{target_id}"))
    if league_type == "duo":
        league_label = "👥 2v2"
    elif league_type == "quals":
        league_label = "⭐ Quals"
    else:
        league_label = "📊 Default"
    bot.send_message(uid, f"📈 {league_label} — Стата <b>{p[1]}</b>:", parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("editstat_"))
def cb_editstat_pick(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    parts = c.data.split("_")
    if parts[1] == "league":
        return
    # Поддержка quals-полей: "editstat_quals_elo_123" → field="quals_elo", target_id=123
    # Поддержка duo-полей:  "editstat_duo_elo_123"   → field="duo_elo",   target_id=123
    if parts[1] == "quals" and len(parts) >= 4:
        field     = f"quals_{parts[2]}"
        target_id = int(parts[3])
    elif parts[1] == "duo" and len(parts) >= 4:
        field     = f"duo_{parts[2]}"
        target_id = int(parts[3])
    else:
        field     = parts[1]
        target_id = int(parts[2])
    p = get_player(target_id)
    if not p:
        bot.answer_callback_query(c.id, "❌ Игрок не найден")
        return
    editstat_flow[uid] = {"field": field, "target_id": target_id}
    bot.answer_callback_query(c.id)
    all_fields = {**STAT_FIELDS, **QUALS_STAT_FIELDS, **DUO_STAT_FIELDS}
    _, label = all_fields.get(field, (field, field))
    bot.send_message(uid, f"✏️ Введите новое значение для <b>{label}</b> игрока <b>{p[1]}</b>:", parse_mode="HTML")


@bot.message_handler(func=lambda m: m.from_user.id in editstat_flow and m.text is not None)
def handle_editstat_flow(msg):
    uid = msg.from_user.id
    if not is_admin(uid):
        return
    data = editstat_flow.pop(uid)
    field = data["field"]
    target_id = data["target_id"]
    p = get_player(target_id)
    if not p:
        bot.send_message(uid, "❌ Игрок не найден")
        return
    try:
        value = int(msg.text.strip())
        if value < 0:
            raise ValueError
    except ValueError:
        bot.send_message(uid, "❌ Введите целое неотрицательное число")
        return
    all_fields = {**STAT_FIELDS, **QUALS_STAT_FIELDS, **DUO_STAT_FIELDS}
    db_field, label = all_fields.get(field, (field, field))
    conn = _db()
    cur = conn.cursor()
    cur.execute(f"UPDATE players SET {db_field}=%s WHERE user_id=%s AND registered=1", (value, target_id))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    if affected == 0:
        bot.send_message(uid, f"❌ Игрок <code>{target_id}</code> не найден в базе (не зарегистрирован).", parse_mode="HTML")
        return
    bot.send_message(uid, f"✅ <b>{label}</b> игрока <b>{p[1]}</b> изменено на <b>{value}</b>!", parse_mode="HTML")
    try:
        bot.send_message(target_id, f"✏️ Администратор изменил вашу статистику (<b>{label}</b>: {value}).", parse_mode="HTML")
    except Exception:
        pass


# ==================== АДМИН ПАНЕЛЬ ====================
@bot.callback_query_handler(func=lambda c: c.data == "admin_panel")
def cb_admin_panel(c):
    uid = c.from_user.id
    try:
        if not is_admin(uid):
            bot.answer_callback_query(c.id, "❌ Нет доступа")
            return
        players = get_all_players()
        active_count = sum(1 for l in running_matches.values() if l.get("status") == "active")
        text = (
            f"⚙️ <b>АДМИН ПАНЕЛЬ</b>\n\n"
            f"👥 Игроков: <b>{len(players)}</b>\n🎮 Лобби: <b>{len(active_lobbies)}</b>\n"
            f"🔴 Матчей: <b>{active_count}</b>\n\nВыберите действие:"
        )
        kb = types.InlineKeyboardMarkup(row_width=1)

        def _btn(label, cb, restrict_key=None):
            if restrict_key and is_admin_restricted(uid, restrict_key):
                return
            kb.add(types.InlineKeyboardButton(label, callback_data=cb))

        kb.add(types.InlineKeyboardButton("👥 Список игроков",       callback_data="admin_players"))
        kb.add(types.InlineKeyboardButton("🔍 Поиск по нику/ID",    callback_data="admin_search"))
        kb.add(types.InlineKeyboardButton("🔍 Поиск по Game ID",    callback_data="admin_search_gameid"))
        _btn("💰 Выдать монеты",        "admin_give_coins",     "give_coins")
        _btn("📊 Изменить ELO",         "admin_set_elo",        "set_elo")
        kb.add(types.InlineKeyboardButton("✏️ Изм. ник игрока",     callback_data="admin_change_nick"))
        kb.add(types.InlineKeyboardButton("🎮 Изм. Game ID игрока", callback_data="admin_change_gid"))
        _btn("📈 Редактировать стату",  "admin_edit_stats",     "edit_stats")
        _btn("⚠️ Выдать варн",          "admin_warn",           "warn")
        _btn("➖ Снять варн",           "admin_unwarn",         "warn")
        _btn("🔇 Мут",                  "admin_mute",           "mute")
        _btn("🔊 Размутить",            "admin_unmute",         "mute")
        _btn("🔎 Вызвать на проверку",  "admin_check",          "check")
        _btn("✅ Снять проверку",       "admin_uncheck",        "check")
        _btn("🚫 Бан / Разбан",         "admin_ban",            "ban")
        _btn("👑 Выдать/Снять админку", "admin_give_admin",     "give_admin")
        _btn("🎮 Роль Гейм Рег",       "admin_give_game_reg",  "give_game_reg")
        _btn("⭐ Quals доступ",         "admin_quals_access",   "quals_access")
        _btn("🎁 Выдать предмет",       "admin_give_item",      "give_coins")
        _btn("🎁 Промокоды",            "admin_promos",         "promos")
        _btn("🎮 Управление матчами",   "admin_matches",        "matches")
        _btn("📋 История матчей",       "admin_match_history",  "matches")
        _btn("📢 Рассылка",             "admin_broadcast",      "broadcast")
        kb.add(types.InlineKeyboardButton("🎟 Открытые тикеты",     callback_data="admin_tickets"))
        _btn("✅ Синяя галочка",        "admin_give_verified",  "give_verified")
        _btn("🏆 Управление сезонами",  "admin_seasons",        "seasons")
        kb.add(types.InlineKeyboardButton("🔙 Назад",               callback_data="back"))

        bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                              reply_markup=kb, parse_mode="HTML")
        bot.answer_callback_query(c.id)
    except Exception as e:
        print(f"[cb_admin_panel] Ошибка uid={uid}: {e}")
        try:
            bot.answer_callback_query(c.id, "⚠️ Ошибка. Попробуй ещё раз.", show_alert=True)
        except Exception:
            pass


# ==================== ПРОМОКОДЫ (АДМИН) ====================

def _promo_rewards_summary(rewards: list) -> str:
    lines = []
    for i, r in enumerate(rewards, 1):
        t = r.get("type", "")
        if t == "coins":
            lines.append(f"  {i}. 💰 {r.get('value', 0)} AC")
        elif t == "premium":
            lines.append(f"  {i}. 👑 Premium {r.get('days', 30)} дн.")
        elif t == "quals":
            lines.append(f"  {i}. ⭐ Quals {r.get('days', 30)} дн.")
    return "\n".join(lines) if lines else "  (пусто)"

def _send_promo_reward_type_kb(uid, data):
    """Отправляет клавиатуру выбора типа следующей награды."""
    existing = _promo_rewards_summary(data.get("rewards", []))
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("💰 Монеты",  callback_data="promo_reward_coins"),
        types.InlineKeyboardButton("👑 Premium", callback_data="promo_reward_premium"),
        types.InlineKeyboardButton("⭐ Quals",   callback_data="promo_reward_quals"),
    )
    text = (
        f"🎁 <b>Создание промокода</b> <code>{data.get('code','')}</code>\n\n"
        f"Уже добавлено:\n{existing}\n\n"
        "Выберите тип <b>следующей</b> награды:"
    )
    sent = bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
    data["_last_bot_msg"] = sent.message_id

def _send_promo_add_more_kb(uid, data):
    """После добавления награды — спрашиваем, добавить ещё?"""
    existing = _promo_rewards_summary(data.get("rewards", []))
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("➕ Добавить ещё",  callback_data="promo_add_more"),
        types.InlineKeyboardButton("✅ Готово",         callback_data="promo_finalize"),
    )
    text = (
        f"🎁 <b>Промокод</b> <code>{data.get('code','')}</code>\n\n"
        f"Добавленные награды:\n{existing}\n\n"
        "Добавить ещё одну награду или завершить?"
    )
    sent = bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
    data["_last_bot_msg"] = sent.message_id

@bot.callback_query_handler(func=lambda c: c.data == "admin_promos")
def cb_admin_promos(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    codes = get_all_promo_codes()
    text = "🎁 <b>ПРОМОКОДЫ</b>\n\n"
    if codes:
        for row in codes:
            code, rtype, rval, max_uses, uses, is_active, rdays, rjson = row
            status = "✅" if is_active else "❌"
            max_str = f"/{max_uses}" if max_uses > 0 else "/∞"
            rewards = []
            if rjson:
                try:
                    rewards = json.loads(rjson)
                except Exception:
                    pass
            if not rewards:
                rewards = [{"type": rtype, "value": rval or 0, "days": rdays or 30}]
            rtype_str = _rewards_to_str(rewards)
            text += f"{status} <code>{code}</code> — {rtype_str} | {uses}{max_str} исп.\n"
    else:
        text += "Промокодов нет."
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("➕ Создать промокод",        callback_data="admin_promo_create"),
        types.InlineKeyboardButton("❌ Деактивировать промокод", callback_data="admin_promo_deactivate"),
        types.InlineKeyboardButton("🔙 Назад",                   callback_data="admin_panel"),
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "admin_promo_create")
def cb_admin_promo_create(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    promo_admin_flow[uid] = {"step": "code", "rewards": []}
    bot.answer_callback_query(c.id)
    sent = bot.send_message(
        uid,
        "🎁 <b>Создание промокода</b>\n\n"
        "<b>Шаг 1</b> — Введите код промокода (только буквы и цифры):\n"
        "Пример: <code>SARE2025</code>",
        parse_mode="HTML",
    )
    promo_admin_flow[uid]["_last_bot_msg"] = sent.message_id


@bot.callback_query_handler(func=lambda c: c.data == "admin_promo_deactivate")
def cb_admin_promo_deactivate(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    promo_admin_flow[uid] = {"step": "deactivate"}
    bot.answer_callback_query(c.id)
    bot.send_message(uid, "❌ Введите код промокода для деактивации:")


@bot.callback_query_handler(func=lambda c: c.data.startswith("promo_reward_"))
def cb_promo_reward_type(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    if uid not in promo_admin_flow:
        bot.answer_callback_query(c.id, "❌ Сессия не найдена")
        return
    reward_type = c.data.split("promo_reward_")[1]
    data = promo_admin_flow[uid]
    data["_cur_reward_type"] = reward_type
    bot.answer_callback_query(c.id)
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass
    data.pop("_last_bot_msg", None)
    if reward_type == "coins":
        data["step"] = "value"
        sent = bot.send_message(uid, "💰 Сколько монет выдавать?", parse_mode="HTML")
        data["_last_bot_msg"] = sent.message_id
    elif reward_type in ("premium", "quals"):
        data["step"] = "days"
        label = "👑 Premium" if reward_type == "premium" else "⭐ Quals"
        sent = bot.send_message(
            uid,
            f"На сколько дней выдавать {label}? (например: 7, 30, 90)",
            parse_mode="HTML",
        )
        data["_last_bot_msg"] = sent.message_id


@bot.callback_query_handler(func=lambda c: c.data == "promo_add_more")
def cb_promo_add_more(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    if uid not in promo_admin_flow:
        bot.answer_callback_query(c.id, "❌ Сессия не найдена")
        return
    bot.answer_callback_query(c.id)
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass
    data = promo_admin_flow[uid]
    data["step"] = "reward_type"
    _send_promo_reward_type_kb(uid, data)


@bot.callback_query_handler(func=lambda c: c.data == "promo_finalize")
def cb_promo_finalize(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    if uid not in promo_admin_flow:
        bot.answer_callback_query(c.id, "❌ Сессия не найдена")
        return
    bot.answer_callback_query(c.id)
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass
    data = promo_admin_flow[uid]
    data["step"] = "max_uses"
    sent = bot.send_message(
        uid,
        "🔢 Сколько раз можно использовать промокод?\n<i>(0 = неограничено)</i>",
        parse_mode="HTML",
    )
    data["_last_bot_msg"] = sent.message_id


def _promo_delete_prev(uid, msg_id=None):
    data = promo_admin_flow.get(uid, {})
    last = data.pop("_last_bot_msg", None)
    if last:
        try:
            bot.delete_message(uid, last)
        except Exception:
            pass
    if msg_id:
        try:
            bot.delete_message(uid, msg_id)
        except Exception:
            pass


@bot.message_handler(func=lambda m: m.from_user.id in promo_admin_flow and m.text is not None)
def handle_promo_admin_flow(msg):
    uid = msg.from_user.id
    if not is_admin(uid):
        return
    data = promo_admin_flow.get(uid, {})
    step = data.get("step")
    text = msg.text.strip()

    if step == "code":
        if not re.match(r'^[A-Za-z0-9]+$', text):
            bot.send_message(uid, "❌ Только буквы и цифры. Попробуйте снова:")
            return
        _promo_delete_prev(uid, msg.message_id)
        data["code"] = text.upper()
        data["step"] = "reward_type"
        data.setdefault("rewards", [])
        _send_promo_reward_type_kb(uid, data)

    elif step == "value":
        try:
            value = int(text)
            if value <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(uid, "❌ Введите целое число больше 0")
            return
        _promo_delete_prev(uid, msg.message_id)
        data.setdefault("rewards", []).append({
            "type": data.pop("_cur_reward_type", "coins"),
            "value": value,
            "days": 30,
        })
        data["step"] = "add_more"
        _send_promo_add_more_kb(uid, data)

    elif step == "days":
        try:
            days = int(text)
            if days <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(uid, "❌ Введите целое число больше 0 (например: 30)")
            return
        _promo_delete_prev(uid, msg.message_id)
        data.setdefault("rewards", []).append({
            "type": data.pop("_cur_reward_type", "premium"),
            "value": 0,
            "days": days,
        })
        data["step"] = "add_more"
        _send_promo_add_more_kb(uid, data)

    elif step == "max_uses":
        try:
            max_uses = int(text)
        except ValueError:
            bot.send_message(uid, "❌ Введите число")
            return
        _promo_delete_prev(uid, msg.message_id)
        code = data["code"]
        rewards = data.get("rewards", [])
        promo_admin_flow.pop(uid, None)
        ok, reason = create_promo_code(code, rewards, max_uses)
        if ok:
            max_str = f"{max_uses}" if max_uses > 0 else "неограничено"
            reward_str = _rewards_to_str(rewards)
            label = "♻️ <b>Промокод переактивирован!</b>" if reason == "reactivated" else "✅ <b>Промокод создан!</b>"
            bot.send_message(
                uid,
                f"{label}\n\n"
                f"Код: <code>{code}</code>\n"
                f"Награды: {reward_str}\n"
                f"Использований: {max_str}",
                parse_mode="HTML",
            )
        elif reason == "exists_active":
            bot.send_message(uid, f"❌ Промокод <code>{code}</code> уже существует и активен!", parse_mode="HTML")
        else:
            bot.send_message(uid, f"❌ Ошибка при создании промокода <code>{code}</code>. Проверьте логи.", parse_mode="HTML")

    elif step == "deactivate":
        _promo_delete_prev(uid, msg.message_id)
        promo_admin_flow.pop(uid, None)
        deactivate_promo_code(text)
        bot.send_message(uid, f"✅ Промокод <code>{text.upper()}</code> деактивирован.", parse_mode="HTML")


# ==================== УПРАВЛЕНИЕ МАТЧАМИ (АДМИН) ====================
@bot.callback_query_handler(func=lambda c: c.data == "admin_matches")
def cb_admin_matches(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    active = [(mk, l) for mk, l in running_matches.items() if l.get("status") == "active"]
    if not active:
        text = "🎮 <b>Управление матчами</b>\n\nАктивных матчей нет."
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
        bot.answer_callback_query(c.id)
        return
    text = "🎮 <b>Активные матчи</b>\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)
    for mk, l in active:
        mid = l.get("match_id", "?")
        AC = l.get("ACreenshots_count", 0)
        text += f"• Match #{mid} | 📸{AC}\n"
        kb.add(types.InlineKeyboardButton(f"⚙️ Match #{mid}", callback_data=f"admin_match_manage_{mk}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_match_manage_"))
def cb_admin_match_manage(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    match_key = c.data[len("admin_match_manage_"):]
    lobby = running_matches.get(match_key)
    if not lobby:
        bot.answer_callback_query(c.id, "❌ Матч не найден")
        return
    match_id   = lobby.get("match_id", "?")
    match_code = lobby.get("match_code", str(match_id))
    AC = lobby.get("ACreenshots_count", 0)
    text = (
        f"⚙️ <b>Match #{match_code}</b>\n🏷 {lobby.get('league','').upper()}/{lobby.get('device','').upper()}\n"
        f"🗺 {lobby.get('map_name','?')}\n📸 {AC}"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔄 Перерегать",  callback_data=f"reregister_match|{match_key}"),
        types.InlineKeyboardButton("❌ Отменить",    callback_data=f"cancel_match|{match_key}"),
        types.InlineKeyboardButton("🔙 Назад",       callback_data="admin_matches"),
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "admin_players")
def cb_admin_players(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    players = get_all_players()
    text = "👥 <b>СПИСОК ИГРОКОВ</b>\n\n"
    for p in players[:20]:
        uid2, name, elo, wins, losses, kills, deaths, coins, banned, warns = p
        ban_mark = " 🚫" if banned else ""
        warn_mark = f" ⚠️{warns}" if warns > 0 else ""
        prem = " 👑" if has_active_premium(uid2) else ""
        text += f"• <b>{name}</b>{prem}{ban_mark}{warn_mark} | ELO: {elo} | {wins}W/{losses}L\n"
    if len(players) > 20:
        text += f"\n<i>...и ещё {len(players)-20}</i>"
    if not players:
        text += "Игроков нет."
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "admin_match_history")
def cb_admin_match_history(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    matches = get_match_history(10)
    text = "📋 <b>ИСТОРИЯ МАТЧЕЙ</b>\n\nМатчей нет." if not matches else "📋 <b>ИСТОРИЯ МАТЧЕЙ</b>\n\n"
    for row in matches:
        match_id, league, device, map_name, winner, ACore_w, ACore_l, finished_at = row
        dt = datetime.datetime.fromtimestamp(finished_at).strftime("%d.%m %H:%M") if finished_at else "?"
        winner_str = "💙 CT" if winner == "ct" else "🧡 T"
        text += f"🔢 <b>Match ID {match_id}</b> | {dt}\n   {league.upper()}/{device.upper()} | {map_name}\n   {winner_str} | {ACore_w}:{ACore_l}\n\n"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


_RESTRICT_MAP = {
    "give_coins":    "give_coins",
    "set_elo":       "set_elo",
    "warn":          "warn",
    "unwarn":        "warn",
    "broadcast":     "broadcast",
    "give_admin":    "give_admin",
    "quals_access":  "quals_access",
    "give_game_reg": "give_game_reg",
    "mute":          "mute",
    "unmute":        "mute",
    "check":         "check",
    "uncheck":       "check",
    "change_nick":   None,
    "change_gid":    None,
    "edit_stats":    "edit_stats",
    "give_verified": "give_verified",
}

@bot.callback_query_handler(func=lambda c: c.data in [
    "admin_search", "admin_search_gameid", "admin_give_coins", "admin_set_elo",
    "admin_warn", "admin_broadcast", "admin_give_admin",
    "admin_quals_access", "admin_give_game_reg", "admin_mute", "admin_unmute",
    "admin_check", "admin_uncheck", "admin_change_nick", "admin_change_gid",
    "admin_edit_stats", "admin_give_verified", "admin_give_item",
])
def cb_admin_action(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    action = c.data.split("admin_")[1]
    restrict_key = _RESTRICT_MAP.get(action)
    if restrict_key and is_admin_restricted(uid, restrict_key):
        bot.answer_callback_query(c.id, "❌ Доступ к этой функции ограничен", show_alert=True)
        return
    prompts = {
        "search":         "🔍 Введите Telegram ID или никнейм:",
        "search_gameid":  "🔍 Введите Game ID игрока:",
        "give_coins":     "💰 Формат: <code>USER_ID КОЛИЧЕСТВО</code>",
        "set_elo":        "📊 Формат: <code>USER_ID НОВОЕ_ELO</code>",
        "change_nick":    "✏️ Введите Telegram ID для смены ника:",
        "change_gid":     "🎮 Введите Telegram ID для смены Game ID:",
        "warn":           "⚠️ Введите Telegram ID или никнейм:",
        "unwarn":         "➖ Снять варн — введите Telegram ID или никнейм:",
        "broadcast":      "📢 Введите текст рассылки:",
        "give_admin":     "👑 Введите Telegram ID или никнейм:",
        "quals_access":   "⭐ Введите Telegram ID или никнейм:",
        "give_game_reg":  "🎮 Введите Telegram ID или никнейм:",
        "mute":           "🔇 Введите Telegram ID или никнейм:",
        "unmute":         "🔊 Введите Telegram ID или никнейм:",
        "check":          "🔎 Введите Telegram ID или никнейм:",
        "uncheck":        "✅ Введите Telegram ID или никнейм:",
        "edit_stats":     "📈 Введите Telegram ID или никнейм игрока:",
        "give_verified":  "✅ Введите Telegram ID или никнейм (выдать/снять синюю галочку):",
        "give_item":      "🎁 Формат: <code>USER_ID ITEM_ID</code>\n\nСписок предметов из магазина — используйте ID из БД.\nПример: <code>123456789 3</code>",
    }
    if action == "give_item":
        prompt = "🎁 Введите Telegram ID или никнейм игрока, которому выдать предмет:"
    else:
        prompt = prompts.get(action, "Введите данные:")
    admin_action[uid] = action
    bot.answer_callback_query(c.id)
    bot.send_message(uid, prompt, parse_mode="HTML")


# Бан обрабатывается отдельно через ban_flow (мульти-шаг)
@bot.callback_query_handler(func=lambda c: c.data == "admin_ban")
def cb_admin_ban_start(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    if is_admin_restricted(uid, "ban"):
        bot.answer_callback_query(c.id, "❌ Доступ к этой функции ограничен", show_alert=True)
        return
    ban_flow[uid] = {"step": "target"}
    bot.answer_callback_query(c.id)
    bot.send_message(uid, "🚫 <b>Шаг 1/3</b> — Введите Telegram ID или никнейм игрока:", parse_mode="HTML")


@bot.message_handler(func=lambda m: m.from_user.id in admin_action and m.text is not None)
def handle_admin_action(msg):
    uid = msg.from_user.id
    if not is_admin(uid):
        return
    action = admin_action.pop(uid)
    text = msg.text.strip()

    def find_player_by_input(inp):
        if inp.isdigit():
            return get_player(int(inp))
        conn2 = _db()
        cur2 = conn2.cursor()
        cur2.execute("SELECT * FROM players WHERE username=%s AND is_bot=0", (inp,))
        row = cur2.fetchone()
        conn2.close()
        return row

    if action == "search":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        games = p[6] + p[7]
        winrate = round(p[6] / games * 100, 1) if games > 0 else 0
        kd = round(p[8] / p[9], 2) if p[9] > 0 else p[8]
        tg_u = p[22] if len(p) > 22 else ""
        # Получаем 2v2 статистику
        duo = get_player_duo_stats(p[0])
        duo_line = ""
        if duo:
            duo_games = duo["wins"] + duo["losses"]
            duo_wr = round(duo["wins"] / duo_games * 100, 1) if duo_games > 0 else 0
            duo_kd = round(duo["kills"] / duo["deaths"], 2) if duo["deaths"] > 0 else duo["kills"]
            duo_line = (
                f"👥 2v2 ELO: {duo['elo']} | {duo['wins']}W/{duo['losses']}L ({duo_wr}%) | K/D: {duo_kd}\n"
            )
        resp = (
            f"👤 <b>{p[1]}</b>\n🆔 TG: <code>{p[0]}</code>\n"
            f"🐦 @{tg_u}\n🎮 Game ID: <code>{p[2]}</code>\n📱 {p[3]}\n"
            f"📊 ELO: {p[4]} | 💰 {p[5]} AC\n"
            f"🏆 {p[6]}W/{p[7]}L ({winrate}%) | K/D: {kd}\n"
            f"{duo_line}"
            f"⚠️ Варны: {p[15] if len(p)>15 else 0} | 🚫 Бан: {'Да' if p[14] else 'Нет'}\n"
            f"👑 Админ: {'Да' if p[11] else 'Нет'} | 🔇 Мут: {'Да' if p[18] else 'Нет'}"
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        target_id = p[0]
        _verif_now = is_verified_check(target_id)
        _verif_lbl = "❎ Снять галочку" if _verif_now else "✅ Выдать галочку"
        kb.add(
            types.InlineKeyboardButton("🚫 Бан/Разбан",    callback_data=f"admin_do_ban_{target_id}"),
            types.InlineKeyboardButton("⚠️ Варн",           callback_data=f"admin_do_warn_{target_id}"),
            types.InlineKeyboardButton("➖ Снять варн",     callback_data=f"admin_do_unwarn_{target_id}"),
            types.InlineKeyboardButton("🔇 Мут",            callback_data=f"admin_do_mute_{target_id}"),
            types.InlineKeyboardButton("🔊 Размутить",      callback_data=f"admin_do_unmute_{target_id}"),
            types.InlineKeyboardButton("🔎 Проверка",       callback_data=f"admin_do_check_{target_id}"),
            types.InlineKeyboardButton("✅ Снять проверку", callback_data=f"admin_do_uncheck_{target_id}"),
            types.InlineKeyboardButton("👑 Дать/Снять адм", callback_data=f"admin_do_give_admin_{target_id}"),
            types.InlineKeyboardButton("⭐ Quals",           callback_data=f"admin_do_quals_{target_id}"),
            types.InlineKeyboardButton(_verif_lbl,          callback_data=f"admin_do_toggle_verified_{target_id}"),
            types.InlineKeyboardButton("🎁 Выдать предмет", callback_data=f"admin_do_give_item_{target_id}"),
        )
        p_target = get_player(target_id)
        has_q = p_target and has_quals_access(target_id)
        kb.add(types.InlineKeyboardButton("✏️ Default стата", callback_data=f"editstatlg_default_{target_id}"))
        if has_q:
            kb.add(types.InlineKeyboardButton("⭐ Quals стата", callback_data=f"editstatlg_quals_{target_id}"))
        kb.add(types.InlineKeyboardButton("👥 2v2 стата", callback_data=f"editstatlg_duo_{target_id}"))
        bot.send_message(uid, resp, parse_mode="HTML", reply_markup=kb)

    elif action == "search_gameid":
        p = get_player_by_game_id(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        bot.send_message(uid, f"👤 <b>{p[1]}</b>\n🆔 TG: <code>{p[0]}</code>\n🎮 Game ID: <code>{p[2]}</code>\n📊 ELO: {p[4]}", parse_mode="HTML")

    elif action == "give_coins":
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].lstrip("-").isdigit():
            bot.send_message(uid, "❌ Формат: USER_ID КОЛИЧЕСТВО")
            return
        target_id, amount = int(parts[0]), int(parts[1])
        add_coins_to_player(target_id, amount)
        bot.send_message(uid, f"✅ Выдано {amount} AC игроку <code>{target_id}</code>", parse_mode="HTML")
        try:
            bot.send_message(target_id, f"💰 Вам начислено <b>{amount} AC</b> администратором!", parse_mode="HTML")
        except Exception:
            pass

    elif action == "set_elo":
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            bot.send_message(uid, "❌ Формат: USER_ID НОВОЕ_ELO")
            return
        target_id, new_elo = int(parts[0]), int(parts[1])
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET elo=%s WHERE user_id=%s AND registered=1", (new_elo, target_id))
        affected = cur.rowcount
        conn.commit(); conn.close()
        if affected == 0:
            bot.send_message(uid, f"❌ Игрок <code>{target_id}</code> не найден в базе (не зарегистрирован).", parse_mode="HTML")
        else:
            bot.send_message(uid, f"✅ ELO игрока <code>{target_id}</code> изменено на <b>{new_elo}</b>", parse_mode="HTML")

    elif action == "change_nick":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        change_flow[uid] = {"field": "admin_nick", "target_id": p[0]}
        bot.send_message(uid, f"✏️ Введите новый никнейм для <b>{p[1]}</b>:", parse_mode="HTML")

    elif action == "change_gid":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        change_flow[uid] = {"field": "admin_id", "target_id": p[0]}
        bot.send_message(uid, f"🎮 Введите новый Game ID для <b>{p[1]}</b>:", parse_mode="HTML")

    elif action == "warn":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        warn_flow[uid] = {"step": "reason", "target_id": p[0], "target_name": p[1]}
        bot.send_message(uid, f"⚠️ <b>Шаг 2/2</b> — Причина варна для <b>{p[1]}</b>:\n\nВведите причину:", parse_mode="HTML")
        return

    elif action == "unwarn":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        cur_warns = p[15] if len(p) > 15 else 0
        new_warns = max(0, cur_warns - 1)
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET warns=%s WHERE user_id=%s", (new_warns, p[0]))
        conn.commit(); conn.close()
        bot.send_message(uid, f"➖ Варн снят с игрока <b>{p[1]}</b>. Итого: {new_warns}/3", parse_mode="HTML")
        try:
            bot.send_message(p[0], f"➖ Один варн снят администратором. Осталось: {new_warns}/3")
        except Exception:
            pass
        admin_p = get_player(uid)
        admin_name = admin_p[1] if admin_p else str(uid)
        send_punishment_log_priv(uid,
            f"➖ <b>Снятие варна</b>\n"
            f"👮 Снял: {tg_link(uid, admin_name)}\n"
            f"👤 Игрок: {tg_link(p[0], p[1])}\n"
            f"📊 Осталось: {new_warns}/3"
        )

    elif action == "broadcast":
        players = get_all_players()
        sent_count = 0
        for row in players:
            try:
                bot.send_message(row[0], f"📢 <b>Сообщение от администрации:</b>\n\n{text}", parse_mode="HTML")
                sent_count += 1
                time.sleep(0.05)
            except Exception:
                pass
        bot.send_message(uid, f"✅ Рассылка отправлена {sent_count}/{len(players)} игрокам")

    elif action == "give_admin":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        new_val = 0 if p[11] else 1
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET is_admin=%s WHERE user_id=%s", (new_val, p[0]))
        conn.commit(); conn.close()
        status = "выдана" if new_val else "снята"
        bot.send_message(uid, f"✅ Админка {status} игроку <b>{p[1]}</b>", parse_mode="HTML")

    elif action == "quals_access":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        cur_val = p[16] if len(p) > 16 else 0
        new_val = 0 if cur_val else 1
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET quals_access=%s WHERE user_id=%s", (new_val, p[0]))
        conn.commit(); conn.close()
        status = "выдан" if new_val else "снят"
        bot.send_message(uid, f"✅ Quals доступ {status} игроку <b>{p[1]}</b>", parse_mode="HTML")
        try:
            bot.send_message(p[0], f"{'⭐ Вам выдан доступ к QUALS!' if new_val else '❌ Ваш доступ к QUALS снят.'}")
        except Exception:
            pass

    elif action == "give_game_reg":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        cur_val = p[17] if len(p) > 17 else 0
        new_val = 0 if cur_val else 1
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET is_game_reg=%s WHERE user_id=%s", (new_val, p[0]))
        conn.commit(); conn.close()
        status = "выдана" if new_val else "снята"
        bot.send_message(uid, f"✅ Роль Гейм Рег {status} игроку <b>{p[1]}</b>", parse_mode="HTML")

    elif action == "mute":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        mute_flow[uid] = {"step": "duration", "target_id": p[0], "target_name": p[1]}
        bot.send_message(
            uid,
            f"🔇 <b>Шаг 2/3</b> — Срок мута для <b>{p[1]}</b>:\n\n"
            f"Введите количество часов (например: <code>2</code>, <code>24</code>, <code>72</code>):",
            parse_mode="HTML"
        )
        return

    elif action == "unmute":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET is_muted=0, mute_until=0 WHERE user_id=%s", (p[0],))
        conn.commit(); conn.close()
        bot.send_message(uid, f"🔊 Мут снят с игрока <b>{p[1]}</b>", parse_mode="HTML")
        try:
            bot.send_message(p[0], "🔊 Ваш мут снят администратором.")
        except Exception:
            pass
        admin_p = get_player(uid)
        admin_name = admin_p[1] if admin_p else str(uid)
        send_punishment_log_priv(uid,
            f"🔊 <b>Размут</b>\n"
            f"👮 Снял: {tg_link(uid, admin_name)}\n"
            f"👤 Игрок: {tg_link(p[0], p[1])}"
        )

    elif action == "check":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET is_on_check=1, check_admin_id=%s WHERE user_id=%s", (uid, p[0]))
        conn.commit(); conn.close()
        bot.send_message(uid, f"🔎 Игрок <b>{p[1]}</b> вызван на проверку", parse_mode="HTML")
        admin_p = get_player(uid)
        admin_name = admin_p[1] if admin_p else str(uid)
        tg_u = admin_p[22] if admin_p and len(admin_p) > 22 else ""
        try:
            bot.send_message(
                p[0],
                f"⚠️ <b>Вас вызвал на проверку {'@'+tg_u if tg_u else 'администратор'}!</b>\n\nОбратитесь к администратору.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        send_punishment_log_priv(uid,
            f"🔎 <b>Вызов на проверку</b>\n"
            f"👮 Вызвал: {tg_link(uid, admin_name)}\n"
            f"👤 Игрок: {tg_link(p[0], p[1])}"
        )

    elif action == "uncheck":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET is_on_check=0, check_admin_id=0 WHERE user_id=%s", (p[0],))
        conn.commit(); conn.close()
        bot.send_message(uid, f"✅ Проверка снята с игрока <b>{p[1]}</b>", parse_mode="HTML")
        try:
            bot.send_message(p[0], "✅ Проверка снята. Доступ к боту восстановлен.")
        except Exception:
            pass
        admin_p = get_player(uid)
        admin_name = admin_p[1] if admin_p else str(uid)
        send_punishment_log_priv(uid,
            f"✅ <b>Снятие проверки</b>\n"
            f"👮 Снял: {tg_link(uid, admin_name)}\n"
            f"👤 Игрок: {tg_link(p[0], p[1])}"
        )

    elif action == "edit_stats":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        has_q = has_quals_access(p[0])
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(types.InlineKeyboardButton("✏️ Default стата", callback_data=f"editstatlg_default_{p[0]}"))
        if has_q:
            kb.add(types.InlineKeyboardButton("⭐ Quals стата", callback_data=f"editstatlg_quals_{p[0]}"))
        kb.add(types.InlineKeyboardButton("👥 2v2 стата", callback_data=f"editstatlg_duo_{p[0]}"))
        bot.send_message(uid, f"📈 Редактирование статы <b>{p[1]}</b>:", parse_mode="HTML", reply_markup=kb)

    elif action == "give_verified":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        target_id = p[0]
        cur_val = is_verified_check(target_id)
        new_val = 0 if cur_val else 1
        conn_v = _db(); cur_v = conn_v.cursor()
        cur_v.execute("UPDATE players SET is_verified=%s WHERE user_id=%s", (new_val, target_id))
        conn_v.commit(); conn_v.close()
        status_txt = "✅ Синяя галочка выдана" if new_val else "❎ Синяя галочка снята"
        bot.send_message(uid, f"{status_txt}: <b>{p[1]}</b>", parse_mode="HTML")
        try:
            if new_val:
                bot.send_message(target_id, "✅ Вам выдана <b>синяя галочка</b> верификации!", parse_mode="HTML")
            else:
                bot.send_message(target_id, "❎ Ваша синяя галочка верификации была снята администратором.")
        except Exception:
            pass

    elif action == "give_item":
        # Find player by text (ID or nickname) then show item keyboard
        def _find_p(inp):
            if inp.isdigit():
                return get_player(int(inp))
            c3 = _db(); cr3 = c3.cursor()
            cr3.execute("SELECT * FROM players WHERE username=%s AND is_bot=0", (inp,))
            row3 = cr3.fetchone(); c3.close()
            return row3
        target_p = _find_p(text)
        if not target_p:
            bot.send_message(uid, "❌ Игрок не найден. Введите TG ID или никнейм:")
            return
        target_id2 = target_p[0]
        bot.send_message(
            uid,
            f"🎁 <b>Выдача предмета</b> для <b>{target_p[1]}</b>\n\nВыберите предмет из списка ниже:",
            parse_mode="HTML",
            reply_markup=_build_shop_items_kb(target_id2),
        )
        return


# ==================== ВЫДАЧА ПРЕДМЕТА — INLINE КНОПКИ ====================

@bot.callback_query_handler(func=lambda c: c.data == "admin_noop")
def cb_admin_noop(c):
    bot.answer_callback_query(c.id)


def _do_admin_give_item(admin_uid: int, target_id: int, item_id: int):
    """Shared helper: give item_id to target_id, notify both, log."""
    p = get_player(target_id)
    if not p:
        bot.send_message(admin_uid, "❌ Игрок не найден")
        return
    item = get_shop_item(item_id)
    if not item:
        bot.send_message(admin_uid, "❌ Предмет не найден")
        return
    _, item_name, _, _, _, item_type = item
    stackable = {"sticker", "unwarn", "x2coins", "rename"}
    if item_type not in stackable:
        ck = _db(); ckc = ck.cursor()
        ckc.execute("SELECT COUNT(*) FROM inventory WHERE user_id=%s AND item_id=%s",
                    (target_id, item_id))
        already = ckc.fetchone()[0]; ck.close()
        if already > 0:
            bot.send_message(admin_uid,
                f"❌ У игрока <b>{p[1]}</b> уже есть <b>{item_name}</b>",
                parse_mode="HTML")
            return
    cg = _db(); cgc = cg.cursor()
    cgc.execute("INSERT INTO inventory (user_id, item_id) VALUES (%s, %s)",
                (target_id, item_id))
    cg.commit(); cg.close()
    admin_p = get_player(admin_uid)
    admin_name = admin_p[1] if admin_p else str(admin_uid)
    bot.send_message(admin_uid,
        f"✅ Предмет <b>{item_name}</b> выдан игроку <b>{p[1]}</b>",
        parse_mode="HTML")
    try:
        bot.send_message(target_id,
            f"🎁 Администратор выдал вам предмет: <b>{item_name}</b>\n\n"
            f"💡 Активируйте его в 🎒 Инвентаре", parse_mode="HTML")
    except Exception:
        pass
    send_punishment_log_priv(admin_uid,
        f"🎁 <b>Выдача предмета</b>\n"
        f"👮 Выдал: {tg_link(admin_uid, admin_name)}\n"
        f"👤 Игрок: {tg_link(target_id, p[1])}\n"
        f"🎮 Предмет: {item_name}"
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_gi_"))
def cb_admin_give_item_pick(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    try:
        # Format: adm_gi_{target_id}_{item_id}
        parts = c.data.split("_")
        target_id = int(parts[2])
        item_id   = int(parts[3])
    except (IndexError, ValueError):
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return
    bot.answer_callback_query(c.id)
    _do_admin_give_item(uid, target_id, item_id)


# ==================== МУЛЬТИШаговый БАН ====================
@bot.message_handler(func=lambda m: m.from_user.id in ban_flow and m.text is not None)
def handle_ban_flow(msg):
    uid = msg.from_user.id
    if not is_admin(uid):
        ban_flow.pop(uid, None)
        return
    data = ban_flow.get(uid, {})
    step = data.get("step")
    text = msg.text.strip()

    def find_player_by_input(inp):
        if inp.isdigit():
            return get_player(int(inp))
        conn2 = _db()
        cur2 = conn2.cursor()
        cur2.execute("SELECT * FROM players WHERE username=%s AND is_bot=0", (inp,))
        row = cur2.fetchone()
        conn2.close()
        return row

    if step == "target":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден. Введите TG ID или никнейм:")
            return
        data["target_id"] = p[0]
        data["target_name"] = p[1]
        data["is_banned"] = p[14]
        data["step"] = "duration"
        if p[14]:
            # Уже забанен — разбаниваем
            conn = _db(); cur = conn.cursor()
            cur.execute("UPDATE players SET is_banned=0, ban_reason='', ban_until=0 WHERE user_id=%s", (p[0],))
            conn.commit(); conn.close()
            ban_flow.pop(uid, None)
            bot.send_message(uid, f"✅ Игрок <b>{p[1]}</b> разблокирован.", parse_mode="HTML")
            try:
                bot.send_message(p[0], "✅ Вы разблокированы.")
            except Exception:
                pass
            admin_p = get_player(uid)
            admin_name = admin_p[1] if admin_p else str(uid)
            send_punishment_log_priv(uid,
                f"✅ <b>Разбан</b>\n"
                f"👮 Выдал: {tg_link(uid, admin_name)}\n"
                f"👤 Разбанен: {tg_link(p[0], p[1])}"
            )
        else:
            bot.send_message(
                uid,
                f"🚫 <b>Шаг 2/3</b> — Срок бана для <b>{p[1]}</b>\n\n"
                f"Введите количество дней или <code>0</code> для перманентного бана:",
                parse_mode="HTML",
            )

    elif step == "duration":
        try:
            days = int(text)
            if days < 0:
                raise ValueError
        except ValueError:
            bot.send_message(uid, "❌ Введите число дней (0 = навсегда)")
            return
        data["duration_days"] = days
        data["step"] = "reason"
        bot.send_message(
            uid,
            f"🚫 <b>Шаг 3/3</b> — Причина бана:\n\nВведите причину:",
            parse_mode="HTML",
        )

    elif step == "reason":
        reason = text
        target_id = data["target_id"]
        target_name = data["target_name"]
        days = data.get("duration_days", 0)
        ban_flow.pop(uid, None)

        until = int(time.time()) + days * 24 * 3600 if days > 0 else 0

        conn = _db(); cur = conn.cursor()
        cur.execute(
            "UPDATE players SET is_banned=1, ban_reason=%s, ban_until=%s WHERE user_id=%s",
            (reason, until, target_id),
        )
        conn.commit(); conn.close()

        duration_str = f"{days} дн." if days > 0 else "Навсегда"
        bot.send_message(
            uid,
            f"✅ Игрок <b>{target_name}</b> заблокирован.\n"
            f"⏰ Срок: {duration_str}\n"
            f"📝 Причина: {reason}",
            parse_mode="HTML",
        )
        try:
            bot.send_message(
                target_id,
                f"🚫 <b>Вы заблокированы.</b>\n⏰ Срок: {duration_str}\n📝 Причина: {reason}",
                parse_mode="HTML",
            )
        except Exception:
            pass

        kick_from_lobby_if_present(target_id)

        admin_p = get_player(uid)
        admin_name = admin_p[1] if admin_p else str(uid)
        send_punishment_log_priv(uid,
            f"🚫 <b>Бан</b>\n"
            f"👮 Выдал: {tg_link(uid, admin_name)}\n"
            f"👤 Выдано: {tg_link(target_id, target_name)}\n"
            f"⏰ Срок: {duration_str}\n"
            f"📝 Причина: {reason}"
        )


# ==================== МУЛЬТИ-ШАГОВЫЙ ВАРН ====================
@bot.message_handler(func=lambda m: m.from_user.id in warn_flow and m.text is not None)
def handle_warn_flow(msg):
    uid = msg.from_user.id
    if not is_admin(uid):
        warn_flow.pop(uid, None)
        return
    data = warn_flow.pop(uid)
    target_id   = data["target_id"]
    target_name = data["target_name"]
    reason      = msg.text.strip()

    warns = add_warn_to_player(target_id)
    admin_p     = get_player(uid)
    admin_name  = admin_p[1] if admin_p else str(uid)

    # Проверка: 3-й варн → авто-мут 2ч + сброс счётчика
    if warns >= 3:
        until = apply_mute(target_id, hours=2)
        dt = fmt_dt(until)
        conn_r = _db(); cur_r = conn_r.cursor()
        cur_r.execute("UPDATE players SET warns=0 WHERE user_id=%s", (target_id,))
        conn_r.commit(); conn_r.close()
        bot.send_message(uid,
            f"⚠️ Варн <b>{target_name}</b> — {warns}/3\n"
            f"📝 Причина: {reason}\n"
            f"🔇 <b>Авто-мут выдан на 2 часа</b> (до {dt})\n"
            f"⚠️ Счётчик варнов сброшен.",
            parse_mode="HTML")
        try:
            bot.send_message(target_id,
                f"⚠️ <b>Варн выдан администратором.</b>\n📝 Причина: {reason}\n"
                f"📊 Итого: {warns}/3\n\n"
                f"🔇 <b>Автоматический мут на 2 часа</b> (до {dt}) за 3 варна.\n⚠️ Счётчик сброшен.",
                parse_mode="HTML")
        except Exception:
            pass
        send_punishment_log_priv(uid,
            f"⚠️ <b>Варн + Авто-мут (3/3)</b>\n"
            f"👮 Выдал: {tg_link(uid, admin_name)}\n"
            f"👤 Игрок: {tg_link(target_id, target_name)}\n"
            f"📝 Причина: {reason}\n"
            f"🔇 Мут до: {dt} | ⚠️ Варны сброшены до 0"
        )
    else:
        bot.send_message(uid,
            f"⚠️ Варн выдан <b>{target_name}</b>. Итого: {warns}/3\n📝 Причина: {reason}",
            parse_mode="HTML")
        try:
            bot.send_message(target_id,
                f"⚠️ <b>Вам выдан варн от администратора.</b>\n"
                f"📝 Причина: {reason}\n📊 Итого: {warns}/3",
                parse_mode="HTML")
        except Exception:
            pass
        send_punishment_log_priv(uid,
            f"⚠️ <b>Варн</b>\n"
            f"👮 Выдал: {tg_link(uid, admin_name)}\n"
            f"👤 Игрок: {tg_link(target_id, target_name)}\n"
            f"📝 Причина: {reason}\n"
            f"📊 Итого: {warns}/3"
        )


# ==================== МУЛЬТИ-ШАГОВЫЙ МУТ ====================
@bot.message_handler(func=lambda m: m.from_user.id in mute_flow and m.text is not None)
def handle_mute_flow(msg):
    uid = msg.from_user.id
    if not is_admin(uid):
        mute_flow.pop(uid, None)
        return
    data = mute_flow.get(uid, {})
    step = data.get("step")
    text = msg.text.strip()

    if step == "duration":
        try:
            hours = int(text)
            if hours <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(uid, "❌ Введите число часов больше 0 (например: 2, 24, 72)")
            return
        data["hours"] = hours
        data["step"]  = "reason"
        bot.send_message(
            uid,
            f"🔇 <b>Шаг 3/3</b> — Причина мута для <b>{data['target_name']}</b>:\n\nВведите причину:",
            parse_mode="HTML"
        )

    elif step == "reason":
        reason      = text
        target_id   = data["target_id"]
        target_name = data["target_name"]
        hours       = data.get("hours", 2)
        mute_flow.pop(uid, None)

        until = apply_mute(target_id, hours=hours)
        dt    = fmt_dt(until)
        kick_from_lobby_if_present(target_id)

        hrs_str = f"{hours} ч."
        bot.send_message(uid,
            f"🔇 Игрок <b>{target_name}</b> замучен на {hrs_str}\n"
            f"⏰ До: {dt}\n📝 Причина: {reason}",
            parse_mode="HTML")
        try:
            bot.send_message(target_id,
                f"🔇 <b>Вам выдан мут администратором.</b>\n"
                f"⏰ Срок: {hrs_str} (до {dt})\n📝 Причина: {reason}",
                parse_mode="HTML")
        except Exception:
            pass

        admin_p    = get_player(uid)
        admin_name = admin_p[1] if admin_p else str(uid)
        send_punishment_log_priv(uid,
            f"🔇 <b>Мут</b>\n"
            f"👮 Выдал: {tg_link(uid, admin_name)}\n"
            f"👤 Игрок: {tg_link(target_id, target_name)}\n"
            f"⏰ Срок: {hrs_str} (до {dt})\n"
            f"📝 Причина: {reason}"
        )


# ==================== БЫСТРЫЕ ДЕЙСТВИЯ (кнопки из поиска) ====================
@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_do_"))
def cb_admin_do_action(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    parts = c.data.split("_")
    action = "_".join(parts[2:-1])
    target_id = int(parts[-1])
    p = get_player(target_id)
    if not p:
        bot.answer_callback_query(c.id, "❌ Игрок не найден")
        return
    conn = _db()
    cur = conn.cursor()
    msg_text = ""
    if action == "ban":
        # Запускаем ban_flow
        conn.close()
        ban_flow[uid] = {"step": "duration", "target_id": target_id, "target_name": p[1], "is_banned": p[14]}
        if p[14]:
            # Разбаниваем
            ban_flow.pop(uid, None)
            c2 = _db(); c2cur = c2.cursor()
            c2cur.execute("UPDATE players SET is_banned=0, ban_reason='', ban_until=0 WHERE user_id=%s", (target_id,))
            c2.commit(); c2.close()
            bot.answer_callback_query(c.id, f"✅ Разблокирован: {p[1]}", show_alert=True)
            try:
                bot.send_message(target_id, "✅ Вы разблокированы.")
            except Exception:
                pass
            admin_p = get_player(uid)
            admin_name = admin_p[1] if admin_p else str(uid)
            send_punishment_log_priv(uid,
                f"✅ <b>Разбан</b>\n"
                f"👮 Выдал: {tg_link(uid, admin_name)}\n"
                f"👤 Разбанен: {tg_link(target_id, p[1])}"
            )
        else:
            bot.answer_callback_query(c.id, f"🚫 Начат бан {p[1]}", show_alert=True)
            bot.send_message(
                uid,
                f"🚫 <b>Шаг 2/3</b> — Срок бана для <b>{p[1]}</b>\n\n"
                f"Введите количество дней (0 = навсегда):",
                parse_mode="HTML",
            )
        return
    elif action == "warn":
        conn.close()
        warn_flow[uid] = {"step": "reason", "target_id": target_id, "target_name": p[1]}
        bot.answer_callback_query(c.id, f"⚠️ Введите причину варна для {p[1]}", show_alert=True)
        bot.send_message(uid, f"⚠️ <b>Шаг 2/2</b> — Причина варна для <b>{p[1]}</b>:\n\nВведите причину:", parse_mode="HTML")
        return
    elif action == "unwarn":
        cur_warns = p[15] if len(p) > 15 else 0
        new_warns = max(0, cur_warns - 1)
        cur.execute("UPDATE players SET warns=%s WHERE user_id=%s", (new_warns, target_id))
        conn.commit(); conn.close()
        bot.answer_callback_query(c.id, f"➖ Варн снят: {p[1]} ({new_warns}/3)", show_alert=True)
        try:
            bot.send_message(target_id, f"➖ Один варн снят администратором. Осталось: {new_warns}/3")
        except Exception:
            pass
        admin_p = get_player(uid)
        admin_name = admin_p[1] if admin_p else str(uid)
        send_punishment_log_priv(uid,
            f"➖ <b>Снятие варна</b>\n"
            f"👮 Снял: {tg_link(uid, admin_name)}\n"
            f"👤 Игрок: {tg_link(target_id, p[1])}\n"
            f"📊 Осталось: {new_warns}/3"
        )
        return
    elif action == "mute":
        conn.close()
        mute_flow[uid] = {"step": "duration", "target_id": target_id, "target_name": p[1]}
        bot.answer_callback_query(c.id, f"🔇 Введите срок мута для {p[1]}", show_alert=True)
        bot.send_message(
            uid,
            f"🔇 <b>Шаг 2/3</b> — Срок мута для <b>{p[1]}</b>:\n\n"
            f"Введите количество часов (например: <code>2</code>, <code>24</code>, <code>72</code>):",
            parse_mode="HTML"
        )
        return
    elif action == "unmute":
        cur.execute("UPDATE players SET is_muted=0, mute_until=0 WHERE user_id=%s", (target_id,))
        conn.commit(); conn.close()
        bot.answer_callback_query(c.id, f"🔊 Мут снят: {p[1]}", show_alert=True)
        try:
            bot.send_message(target_id, "🔊 Ваш мут снят администратором.")
        except Exception:
            pass
        admin_p = get_player(uid)
        admin_name = admin_p[1] if admin_p else str(uid)
        send_punishment_log_priv(uid,
            f"🔊 <b>Размут</b>\n"
            f"👮 Снял: {tg_link(uid, admin_name)}\n"
            f"👤 Игрок: {tg_link(target_id, p[1])}"
        )
        return
    elif action == "check":
        cur.execute("UPDATE players SET is_on_check=1, check_admin_id=%s WHERE user_id=%s", (uid, target_id))
        conn.commit(); conn.close()
        bot.answer_callback_query(c.id, f"🔎 На проверке: {p[1]}", show_alert=True)
        try:
            bot.send_message(target_id, f"⚠️ <b>Вас вызвали на проверку!</b>\n\nОбратитесь к администратору.", parse_mode="HTML")
        except Exception:
            pass
        admin_p = get_player(uid)
        admin_name = admin_p[1] if admin_p else str(uid)
        send_punishment_log_priv(uid,
            f"🔎 <b>Вызов на проверку</b>\n"
            f"👮 Вызвал: {tg_link(uid, admin_name)}\n"
            f"👤 Игрок: {tg_link(target_id, p[1])}"
        )
        return
    elif action == "uncheck":
        cur.execute("UPDATE players SET is_on_check=0, check_admin_id=0 WHERE user_id=%s", (target_id,))
        conn.commit(); conn.close()
        bot.answer_callback_query(c.id, f"✅ Проверка снята: {p[1]}", show_alert=True)
        try:
            bot.send_message(target_id, "✅ Проверка снята. Доступ восстановлен.")
        except Exception:
            pass
        admin_p = get_player(uid)
        admin_name = admin_p[1] if admin_p else str(uid)
        send_punishment_log_priv(uid,
            f"✅ <b>Снятие проверки</b>\n"
            f"👮 Снял: {tg_link(uid, admin_name)}\n"
            f"👤 Игрок: {tg_link(target_id, p[1])}"
        )
        return
    elif action == "give_admin":
        new_val = 0 if p[11] else 1
        cur.execute("UPDATE players SET is_admin=%s WHERE user_id=%s", (new_val, target_id))
        msg_text = f"{'👑 Админка выдана' if new_val else '❌ Админка снята'}: {p[1]}"
    elif action == "quals":
        cur_val = p[16] if len(p) > 16 else 0
        new_val = 0 if cur_val else 1
        cur.execute("UPDATE players SET quals_access=%s WHERE user_id=%s", (new_val, target_id))
        msg_text = f"{'⭐ Quals выдан' if new_val else '❌ Quals снят'}: {p[1]}"
    elif action == "give_item":
        conn.close()
        bot.answer_callback_query(c.id, f"🎁 Выбор предмета для {p[1]}", show_alert=False)
        bot.send_message(
            uid,
            f"🎁 <b>Выдача предмета</b> для <b>{p[1]}</b>\n\n"
            f"Выберите предмет из списка ниже:",
            parse_mode="HTML",
            reply_markup=_build_shop_items_kb(target_id),
        )
        return
    elif action == "toggle_verified":
        cur_val = is_verified_check(target_id)
        new_val = 0 if cur_val else 1
        cur.execute("UPDATE players SET is_verified=%s WHERE user_id=%s", (new_val, target_id))
        msg_text = f"{'✅ Галочка выдана' if new_val else '❎ Галочка снята'}: {p[1]}"
        try:
            if new_val:
                bot.send_message(target_id, "✅ Вам выдана <b>синяя галочка</b> верификации!", parse_mode="HTML")
            else:
                bot.send_message(target_id, "❎ Ваша синяя галочка верификации была снята администратором.")
        except Exception:
            pass
    else:
        conn.close()
        bot.answer_callback_query(c.id, "❌ Неизвестное действие")
        return
    conn.commit()
    conn.close()
    bot.answer_callback_query(c.id, msg_text, show_alert=True)


# ==================== ДОБАВЛЕНИЕ БОТОВ (АДМИН) ====================
@bot.callback_query_handler(func=lambda c: c.data == "add_bots_admin")
def cb_add_bots(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    for lobby_id, lobby in active_lobbies.items():
        _msz = _lobby_max_size(lobby.get("league", "default"))
        if lobby.get("status") == "waiting" and len(lobby["players"]) < _msz:
            slots = _msz - len(lobby["players"])
            kb.add(types.InlineKeyboardButton(
                f"Лобби {lobby_id} ({len(lobby['players'])}/{_msz}) — добавить {slots} ботов",
                callback_data=f"fill_bots_{lobby_id}",
            ))
    if not kb.keyboard:
        bot.answer_callback_query(c.id, "❌ Нет доступных лобби", show_alert=True)
        return
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    bot.edit_message_text("🤖 Выберите лобби для заполнения ботами:", c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("fill_bots_"))
def cb_fill_bots(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    lobby_id = c.data[len("fill_bots_"):]
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby["status"] != "waiting":
        bot.answer_callback_query(c.id, "❌ Лобби недоступно")
        return
    bots = get_bots()
    _msz_fill = _lobby_max_size(lobby.get("league", "default"))
    needed = _msz_fill - len(lobby["players"])
    available_bots = [b for b in bots if b[0] not in lobby["players"]]
    fill = random.sample(available_bots, min(needed, len(available_bots)))
    for b_id, _ in fill:
        lobby["players"].append(b_id)
    bot.answer_callback_query(c.id, f"✅ Добавлено {len(fill)} ботов")
    if len(lobby["players"]) >= _msz_fill:
        start_accept_phase(lobby_id)
    else:
        broadcast_lobby_update(lobby_id)


# ==================== GAME REG ПАНЕЛЬ ====================
@bot.callback_query_handler(func=lambda c: c.data == "game_reg_panel")
def cb_game_reg_panel(c):
    uid = c.from_user.id
    if not is_game_reg_check(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    active = [(mk, l) for mk, l in running_matches.items() if l.get("status") == "active"]
    if not active:
        text = "📋 <b>Регистрация матчей</b>\n\nАктивных матчей нет."
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
        bot.answer_callback_query(c.id)
        return
    text = "📋 <b>Активные матчи</b>\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)
    for mk, l in active:
        mid = l.get("match_id", "?")
        AC = l.get("ACreenshots_count", 0)
        _gr_priv_cfg   = PRIVATE_CONFIG.get(l.get("private", "darling"), PRIVATE_CONFIG["darling"])
        _gr_priv_label = f"{_gr_priv_cfg['emoji']} {_gr_priv_cfg['display']}"
        text += f"• Match #{mid} | {_gr_priv_label} | 📸{AC}\n"
        kb.add(types.InlineKeyboardButton(f"📝 Зарегистрировать Match #{mid}", callback_data=f"reg_match|{mk}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


# ==================== АВТО-РАЗБАН / АВТО-РАЗМУТ ====================
def auto_unban_loop():
    """Фоновый поток: каждые 30 минут снимает истёкшие баны и муты."""
    while True:
        try:
            now = int(time.time())
            conn = _db()
            cur = conn.cursor()

            # --- Авто-разбан (временный бан) ---
            cur.execute(
                "SELECT user_id, username FROM players WHERE is_banned=1 AND ban_until > 0 AND ban_until <= %s",
                (now,)
            )
            expired_bans = cur.fetchall()
            for (uid, uname) in expired_bans:
                cur.execute(
                    "UPDATE players SET is_banned=0, ban_until=0, ban_reason='' WHERE user_id=%s",
                    (uid,)
                )
                conn.commit()
                # Уведомить игрока
                try:
                    bot.send_message(
                        uid,
                        "✅ <b>Ваш бан снят!</b>\n\nСрок блокировки истёк. Добро пожаловать обратно.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                # Лог в ветку
                send_punishment_log(
                    f"✅ <b>Авто-разбан</b>\n"
                    f"👤 Игрок: <b>{uname}</b> (id: <code>{uid}</code>)\n"
                    f"📅 Срок бана истёк."
                )

            # --- Авто-размут ---
            cur.execute(
                "SELECT user_id, username FROM players WHERE is_muted=1 AND mute_until > 0 AND mute_until <= %s",
                (now,)
            )
            expired_mutes = cur.fetchall()
            for (uid, uname) in expired_mutes:
                cur.execute(
                    "UPDATE players SET is_muted=0, mute_until=0 WHERE user_id=%s",
                    (uid,)
                )
                conn.commit()
                try:
                    bot.send_message(
                        uid,
                        "🔊 <b>Мут снят!</b>\n\nВы снова можете общаться.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                send_punishment_log(
                    f"🔊 <b>Авто-размут</b>\n"
                    f"👤 Игрок: <b>{uname}</b> (id: <code>{uid}</code>)\n"
                    f"📅 Срок мута истёк."
                )

            # --- Авто-снятие Premium и Quals по всем приваткам ---
            total_expired_premiums = 0
            total_expired_quals    = 0
            all_priv_tables = list({cfg["table"] for cfg in PRIVATE_CONFIG.values()})
            for priv_table in all_priv_tables:
                try:
                    # Premium
                    cur.execute(
                        f"SELECT user_id, username FROM {priv_table} WHERE premium_until > 0 AND premium_until <= %s",
                        (now,)
                    )
                    expired_premiums = cur.fetchall()
                    for (puid, puname) in expired_premiums:
                        cur.execute(
                            f"UPDATE {priv_table} SET premium_until=0 WHERE user_id=%s",
                            (puid,)
                        )
                        conn.commit()
                        total_expired_premiums += 1
                        try:
                            bot.send_message(
                                puid,
                                "👑 <b>Ваш Premium истёк.</b>\n\nВы можете продлить его в 🛒 Магазине.",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass

                    # Quals
                    cur.execute(
                        f"SELECT user_id, username FROM {priv_table} WHERE quals_until > 0 AND quals_until <= %s AND quals_access=1",
                        (now,)
                    )
                    expired_quals = cur.fetchall()
                    for (puid, puname) in expired_quals:
                        cur.execute(
                            f"UPDATE {priv_table} SET quals_until=0, quals_access=0 WHERE user_id=%s",
                            (puid,)
                        )
                        conn.commit()
                        total_expired_quals += 1
                        try:
                            bot.send_message(
                                puid,
                                "⭐ <b>Ваш доступ к QUALS истёк.</b>\n\nВы можете продлить его в 🛒 Магазине.",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                except Exception as _tbl_err:
                    print(f"[auto_unban] Ошибка таблицы {priv_table}: {_tbl_err}")

            conn.close()

            if expired_bans or expired_mutes:
                print(f"[auto_unban] Снято банов: {len(expired_bans)}, мутов: {len(expired_mutes)}")
            if total_expired_premiums or total_expired_quals:
                print(f"[auto_unban] Premium истёк: {total_expired_premiums}, Quals истёк: {total_expired_quals}")

        except Exception as e:
            print(f"[auto_unban] Ошибка: {e}")

        time.sleep(30 * 60)  # проверка каждые 30 минут


# ==================== СЕЗОНЫ — АДМИН ПАНЕЛЬ ====================

@bot.callback_query_handler(func=lambda c: c.data == "admin_seasons")
def cb_admin_seasons(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    season = get_current_season()
    all_seasons = get_all_seasons()

    if season:
        sid, snum, sname, started_at = season
        dt_start = datetime.datetime.fromtimestamp(started_at).strftime("%d.%m.%Y %H:%M")
        text = (
            f"🏆 <b>УПРАВЛЕНИЕ СЕЗОНАМИ</b>\n\n"
            f"📅 Текущий сезон: <b>{sname}</b>\n"
            f"🔢 Номер: <b>{snum}</b>\n"
            f"🕐 Начат: <b>{dt_start}</b>\n\n"
            f"📊 Всего сезонов: <b>{len(all_seasons)}</b>\n\n"
            f"⚠️ <i>Сброс сезона обнуляет ELO и статистику всех игроков, "
            f"предварительно сохраняя их в архив.</i>"
        )
    else:
        text = "🏆 <b>УПРАВЛЕНИЕ СЕЗОНАМИ</b>\n\nСезон не найден."

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔄 Сбросить сезон",        callback_data="admin_season_reset_confirm"),
        types.InlineKeyboardButton("📜 История сезонов",        callback_data="admin_season_history"),
    )
    if season:
        kb.add(types.InlineKeyboardButton(f"🏅 Топ сезона #{season[1]}", callback_data=f"admin_season_top|{season[0]}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "admin_season_reset_confirm")
def cb_admin_season_reset_confirm(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    season = get_current_season()
    sname = season[2] if season else "текущий сезон"
    snum  = season[1] if season else 1

    text = (
        f"⚠️ <b>ПОДТВЕРЖДЕНИЕ СБРОСА СЕЗОНА</b>\n\n"
        f"Вы собираетесь завершить <b>{sname}</b> и начать <b>Сезон {snum + 1}</b>.\n\n"
        f"<b>Что произойдёт:</b>\n"
        f"• Статистика всех игроков сохранится в архив сезона\n"
        f"• ELO всех игроков сбросится до <b>1000</b>\n"
        f"• Победы, поражения, убийства, смерти, MVP — обнулятся\n"
        f"• Монеты игроков <b>не будут тронуты</b>\n"
        f"• Инвентарь и покупки <b>не будут тронуты</b>\n\n"
        f"❗ <b>Это действие необратимо!</b>\n"
        f"Вы уверены?"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Да, сбросить",  callback_data="admin_season_reset_execute"),
        types.InlineKeyboardButton("❌ Отмена",         callback_data="admin_seasons"),
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "admin_season_reset_execute")
def cb_admin_season_reset_execute(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    bot.answer_callback_query(c.id, "⏳ Выполняется сброс...")
    try:
        new_season_number, players_count = reset_season(uid)
        admin_p = get_player(uid)
        admin_name = admin_p[1] if admin_p else str(uid)
        text = (
            f"✅ <b>СЕЗОН СБРОШЕН!</b>\n\n"
            f"🏆 Начат <b>Сезон {new_season_number}</b>\n"
            f"👥 Архивировано игроков: <b>{players_count}</b>\n"
            f"👮 Администратор: <b>{admin_name}</b>\n\n"
            f"Все игроки начинают с ELO 1000."
        )
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("🏆 К сезонам", callback_data="admin_seasons"),
            types.InlineKeyboardButton("🔙 Админ панель", callback_data="admin_panel"),
        )
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")

        # Уведомить всех администраторов
        notif_text = (
            f"🏆 <b>СЕЗОН СБРОШЕН!</b>\n\n"
            f"Начат <b>Сезон {new_season_number}</b>\n"
            f"Архивировано: {players_count} игроков\n"
            f"Выполнил: <b>{admin_name}</b>"
        )
        for admin_id in ADMIN_IDS_LIST:
            if admin_id != uid:
                try:
                    bot.send_message(admin_id, notif_text, parse_mode="HTML")
                except Exception:
                    pass

    except Exception as e:
        bot.send_message(
            c.message.chat.id,
            f"❌ <b>Ошибка при сбросе сезона:</b>\n<code>{e}</code>",
            parse_mode="HTML"
        )


@bot.callback_query_handler(func=lambda c: c.data == "admin_season_history")
def cb_admin_season_history(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    seasons = get_all_seasons()
    text = "📜 <b>ИСТОРИЯ СЕЗОНОВ</b>\n\n"
    if not seasons:
        text += "Сезонов нет."
    else:
        for s in seasons:
            sid, snum, sname, started_at, ended_at, is_active = s
            dt_start = datetime.datetime.fromtimestamp(started_at).strftime("%d.%m.%Y") if started_at else "?"
            if is_active:
                text += f"🟢 <b>{sname}</b> — начат {dt_start} <i>(активный)</i>\n"
            else:
                dt_end = datetime.datetime.fromtimestamp(ended_at).strftime("%d.%m.%Y") if ended_at else "?"
                text += f"⚫ <b>{sname}</b> — {dt_start} → {dt_end}\n"

    kb = types.InlineKeyboardMarkup(row_width=1)
    for s in seasons:
        sid, snum, sname, started_at, ended_at, is_active = s
        if not is_active:
            kb.add(types.InlineKeyboardButton(f"🏅 Топ: {sname}", callback_data=f"admin_season_top|{sid}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_seasons"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_season_top|"))
def cb_admin_season_top(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    season_id = int(c.data.split("|", 1)[1])
    top = get_season_top(season_id)

    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT season_number, name FROM seasons WHERE id=%s", (season_id,))
    srow = cur.fetchone()
    conn.close()
    sname = srow[1] if srow else f"Сезон {season_id}"

    text = f"🏅 <b>ТОП — {sname}</b>\n\n"
    if not top:
        text += "Нет данных."
    else:
        medals = ["🥇", "🥈", "🥉"]
        for i, row in enumerate(top):
            uname, elo, wins, losses, kills, deaths, mvp = row
            medal = medals[i] if i < 3 else f"{i+1}."
            kd = f"{kills/deaths:.2f}" if deaths else "—"
            text += (
                f"{medal} <b>{uname}</b>\n"
                f"   ELO: {elo} | {wins}W/{losses}L | K/D: {kd}"
                + (f" | MVP: {mvp}" if mvp else "") + "\n"
            )

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_season_history"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


# ==================== ТИКЕТЫ — АДМИН ПАНЕЛЬ ====================

@bot.callback_query_handler(func=lambda c: c.data == "admin_tickets")
def cb_admin_tickets(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    tickets = get_open_tickets()
    if not tickets:
        text = "🎟 <b>ОТКРЫТЫЕ ТИКЕТЫ</b>\n\n✅ Нет открытых тикетов."
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
        bot.answer_callback_query(c.id)
        return

    text = f"🎟 <b>ОТКРЫТЫЕ ТИКЕТЫ</b> ({len(tickets)})\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)
    for row in tickets[:15]:
        tid, ticket_code, user_id, match_code, reason, accused_name, status, created_at = row
        p = get_player(user_id)
        reporter = p[1] if p else str(user_id)
        mc = f"#{match_code}" if match_code else "без матча"
        short_reason = reason[:30] + "…" if len(reason) > 30 else reason
        dt = datetime.datetime.fromtimestamp(created_at).strftime("%d.%m %H:%M") if created_at else "?"
        text += f"<b>{ticket_code}</b> | {dt}\n👤 {reporter} | 🎮 {mc}\n📝 {short_reason}\n\n"
        kb.add(types.InlineKeyboardButton(
            f"🎟 {ticket_code} — {reporter[:15]}",
            callback_data=f"admin_ticket_view|{ticket_code}"
        ))
    if len(tickets) > 15:
        text += f"<i>...и ещё {len(tickets)-15}</i>\n"
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_ticket_view|"))
def cb_admin_ticket_view(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    ticket_code = c.data.split("|", 1)[1]
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT ticket_code, user_id, match_code, reason, evidence_file_id, accused_id, accused_name, status, created_at FROM tickets WHERE ticket_code=%s",
        (ticket_code,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        bot.answer_callback_query(c.id, "❌ Тикет не найден", show_alert=True)
        return
    tcode, user_id, match_code, reason, evidence_file_id, accused_id, accused_name, status, created_at = row
    p = get_player(user_id)
    reporter = p[1] if p else str(user_id)
    mc = f"#{match_code}" if match_code else "не указан"
    acc = accused_name if accused_name else "не указан"
    dt = datetime.datetime.fromtimestamp(created_at).strftime("%d.%m.%Y %H:%M") if created_at else "?"
    text = (
        f"🎟 <b>Тикет {tcode}</b>\n\n"
        f"📅 Дата: {dt}\n"
        f"👤 От: <b>{reporter}</b> (<code>{user_id}</code>)\n"
        f"🎮 Матч: <b>{mc}</b>\n"
        f"📝 Причина: {reason}\n"
        f"🎯 Обвиняемый: {acc}\n"
        f"📌 Статус: <b>{status}</b>"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    if status == "open":
        kb.add(
            types.InlineKeyboardButton("✅ Закрыть (решено)", callback_data=f"ticket_close|{tcode}"),
            types.InlineKeyboardButton("❌ Отклонить",        callback_data=f"ticket_reject|{tcode}"),
        )
    kb.add(types.InlineKeyboardButton("🔙 К тикетам", callback_data="admin_tickets"))
    bot.answer_callback_query(c.id)
    if evidence_file_id:
        try:
            bot.send_photo(c.message.chat.id, evidence_file_id, caption=text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
    except Exception:
        bot.send_message(c.message.chat.id, text, reply_markup=kb, parse_mode="HTML")


# ==================== ТИКЕТЫ / ЖАЛОБЫ ====================

def _ticket_cancel_kb():
    """Клавиатура для отмены тикета (используется на каждом шаге)."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("❌ Отменить тикет", callback_data="ticket_cancel"))
    return kb

def _ticket_back_kb():
    """Клавиатура с кнопками Назад и Отмена."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("◀️ Назад",            callback_data="ticket_back"),
        types.InlineKeyboardButton("❌ Отменить тикет",   callback_data="ticket_cancel"),
    )
    return kb


@bot.callback_query_handler(func=lambda c: c.data == "ticket_cancel")
def cb_ticket_cancel(c):
    uid = c.from_user.id
    ticket_flow.pop(uid, None)
    bot.answer_callback_query(c.id, "❌ Тикет отменён")
    try:
        bot.edit_message_text("❌ Создание тикета отменено.", c.message.chat.id, c.message.message_id)
    except Exception:
        pass
    bot.send_message(uid, "🏠 Главное меню:", reply_markup=main_menu(uid))


@bot.callback_query_handler(func=lambda c: c.data == "ticket_back")
def cb_ticket_back(c):
    uid = c.from_user.id
    data = ticket_flow.get(uid)
    if not data:
        bot.answer_callback_query(c.id, "❌ Нет активного тикета", show_alert=True)
        return
    step = data.get("step", "match_code")
    bot.answer_callback_query(c.id)
    if step == "reason":
        # Вернуться к шагу match_code
        ticket_flow[uid]["step"] = "match_code"
        ticket_flow[uid].pop("match_code", None)
        try:
            bot.edit_message_text(
                "🎟 <b>СОЗДАНИЕ ТИКЕТА</b>\n\n"
                "<b>Шаг 1/4</b> — Введите код матча (#XXXXXXX) к которому относится жалоба:\n"
                "<i>Если жалоба не связана с конкретным матчем — напишите <code>нет</code></i>",
                c.message.chat.id, c.message.message_id,
                parse_mode="HTML", reply_markup=_ticket_cancel_kb(),
            )
        except Exception:
            bot.send_message(uid,
                "🎟 <b>Шаг 1/4</b> — Введите код матча:",
                parse_mode="HTML", reply_markup=_ticket_cancel_kb(),
            )
    elif step == "evidence":
        # Вернуться к шагу reason
        ticket_flow[uid]["step"] = "reason"
        ticket_flow[uid].pop("reason", None)
        try:
            bot.edit_message_text(
                "🎟 <b>Шаг 2/4</b> — Опишите причину жалобы подробно:\n"
                "<i>(читы, токсик, AFK, нечестная игра и т.д.)</i>",
                c.message.chat.id, c.message.message_id,
                parse_mode="HTML", reply_markup=_ticket_back_kb(),
            )
        except Exception:
            bot.send_message(uid,
                "🎟 <b>Шаг 2/4</b> — Опишите причину жалобы:",
                parse_mode="HTML", reply_markup=_ticket_back_kb(),
            )
    elif step == "accused":
        # Вернуться к шагу evidence
        ticket_flow[uid]["step"] = "evidence"
        ticket_flow[uid].pop("evidence_file_id", None)
        try:
            bot.edit_message_text(
                "🎟 <b>Шаг 3/4</b> — Отправьте доказательство:\n"
                "📷 Фото / скриншот, или напишите <code>нет</code> если доказательств нет.",
                c.message.chat.id, c.message.message_id,
                parse_mode="HTML", reply_markup=_ticket_back_kb(),
            )
        except Exception:
            bot.send_message(uid,
                "🎟 <b>Шаг 3/4</b> — Отправьте доказательство:",
                parse_mode="HTML", reply_markup=_ticket_back_kb(),
            )
    else:
        # На первом шаге — просто отмена
        ticket_flow.pop(uid, None)
        try:
            bot.edit_message_text("❌ Создание тикета отменено.", c.message.chat.id, c.message.message_id)
        except Exception:
            bot.send_message(uid, "❌ Создание тикета отменено.")


@bot.callback_query_handler(func=lambda c: c.data == "ticket_start")
def cb_ticket_start(c):
    uid = c.from_user.id
    err = check_blocked(uid)
    if err:
        bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
        return
    if not is_registered(uid):
        bot.answer_callback_query(c.id, "❌ Сначала зарегистрируйтесь", show_alert=True)
        return
    ticket_flow[uid] = {"step": "match_code"}
    bot.answer_callback_query(c.id)
    try:
        bot.edit_message_text(
            "🎟 <b>СОЗДАНИЕ ТИКЕТА</b>\n\n"
            "<b>Шаг 1/4</b> — Введите код матча (#XXXXXXX) к которому относится жалоба:\n"
            "<i>Если жалоба не связана с конкретным матчем — напишите <code>нет</code></i>",
            c.message.chat.id, c.message.message_id,
            parse_mode="HTML", reply_markup=_ticket_cancel_kb(),
        )
    except Exception:
        bot.send_message(
            uid,
            "🎟 <b>СОЗДАНИЕ ТИКЕТА</b>\n\n"
            "<b>Шаг 1/4</b> — Введите код матча (#XXXXXXX) к которому относится жалоба:\n"
            "<i>Если жалоба не связана с конкретным матчем — напишите <code>нет</code></i>",
            parse_mode="HTML", reply_markup=_ticket_cancel_kb(),
        )


@bot.message_handler(func=lambda m: m.from_user.id in ticket_flow and ticket_flow[m.from_user.id].get("step") == "match_code")
def ticket_step_match_code(msg):
    uid = msg.from_user.id
    text = msg.text.strip() if msg.text else ""
    match_code = "" if text.lower() in ("нет", "no", "-") else text.lstrip("#").upper()
    ticket_flow[uid]["match_code"] = match_code
    ticket_flow[uid]["step"] = "reason"
    bot.send_message(
        uid,
        "🎟 <b>Шаг 2/4</b> — Опишите причину жалобы подробно:\n"
        "<i>(читы, токсик, AFK, нечестная игра и т.д.)</i>",
        parse_mode="HTML", reply_markup=_ticket_back_kb(),
    )


@bot.message_handler(
    func=lambda m: m.from_user.id in ticket_flow and ticket_flow[m.from_user.id].get("step") == "reason",
    content_types=["text"],
)
def ticket_step_reason(msg):
    uid = msg.from_user.id
    reason = msg.text.strip()
    if len(reason) < 5:
        bot.send_message(uid, "⚠️ Слишком коротко. Опишите подробнее:", reply_markup=_ticket_back_kb())
        return
    ticket_flow[uid]["reason"] = reason
    ticket_flow[uid]["step"] = "evidence"
    bot.send_message(
        uid,
        "🎟 <b>Шаг 3/4</b> — Отправьте доказательство:\n"
        "📷 Фото / скриншот, или напишите <code>нет</code> если доказательств нет.",
        parse_mode="HTML", reply_markup=_ticket_back_kb(),
    )


@bot.message_handler(
    func=lambda m: m.from_user.id in ticket_flow and ticket_flow[m.from_user.id].get("step") == "evidence",
    content_types=["photo", "document", "text"],
)
def ticket_step_evidence(msg):
    uid = msg.from_user.id
    evidence_file_id = ""
    if msg.photo:
        evidence_file_id = msg.photo[-1].file_id
    elif msg.document:
        evidence_file_id = msg.document.file_id
    ticket_flow[uid]["evidence_file_id"] = evidence_file_id
    ticket_flow[uid]["step"] = "accused"
    bot.send_message(
        uid,
        "🎟 <b>Шаг 4/4</b> — Введите @username или ник игрока, на которого жалоба:\n"
        "<i>Если неизвестен — напишите <code>нет</code></i>",
        parse_mode="HTML", reply_markup=_ticket_back_kb(),
    )


@bot.message_handler(
    func=lambda m: m.from_user.id in ticket_flow and ticket_flow[m.from_user.id].get("step") == "accused",
    content_types=["text"],
)
def ticket_step_accused(msg):
    uid = msg.from_user.id
    accused_text = msg.text.strip() if msg.text else ""
    accused_name = "" if accused_text.lower() in ("нет", "no", "-") else accused_text

    data = ticket_flow.pop(uid, {})
    match_code      = data.get("match_code", "")
    reason          = data.get("reason", "")
    evidence_file_id = data.get("evidence_file_id", "")

    # Ищем accused_id по username если есть
    accused_id = None
    if accused_name.startswith("@"):
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM players WHERE tg_username=%s", (accused_name.lstrip("@"),))
        row = cur.fetchone()
        conn.close()
        if row:
            accused_id = row[0]

    ticket_code = create_ticket(uid, match_code, reason, evidence_file_id, accused_id, accused_name)
    if not ticket_code:
        bot.send_message(uid, "❌ Ошибка создания тикета. Попробуйте позже.", reply_markup=main_menu(uid))
        return

    p = get_player(uid)
    reporter_name = p[1] if p else str(uid)
    mc_text = f"<code>#{match_code}</code>" if match_code else "не указан"
    accused_text_display = accused_name if accused_name else "не указан"

    # Уведомить пользователя
    bot.send_message(
        uid,
        f"✅ <b>Тикет {ticket_code} создан!</b>\n\n"
        f"🎮 Матч: {mc_text}\n"
        f"📝 Причина: {reason}\n"
        f"🎯 Обвиняемый: {accused_text_display}\n\n"
        f"Администраторы рассмотрят тикет в ближайшее время.",
        parse_mode="HTML",
        reply_markup=main_menu(uid),
    )

    # Уведомить всех администраторов
    kb_admin = types.InlineKeyboardMarkup(row_width=2)
    kb_admin.add(
        types.InlineKeyboardButton("✅ Закрыть (решено)", callback_data=f"ticket_close|{ticket_code}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"ticket_reject|{ticket_code}"),
    )
    admin_text = (
        f"🎟 <b>Новый тикет {ticket_code}</b>\n\n"
        f"👤 От: <b>{reporter_name}</b> (<code>{uid}</code>)\n"
        f"🎮 Матч: {mc_text}\n"
        f"📝 Причина: {reason}\n"
        f"🎯 Обвиняемый: {accused_text_display}\n"
    )
    for admin_id in ADMIN_IDS_LIST:
        try:
            if evidence_file_id:
                bot.send_photo(admin_id, evidence_file_id, caption=admin_text, reply_markup=kb_admin, parse_mode="HTML")
            else:
                bot.send_message(admin_id, admin_text, reply_markup=kb_admin, parse_mode="HTML")
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("ticket_close|") or c.data.startswith("ticket_reject|"))
def cb_ticket_action(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа", show_alert=True)
        return
    parts = c.data.split("|", 1)
    action      = parts[0]    # "ticket_close" or "ticket_reject"
    ticket_code = parts[1]

    new_status  = "closed"   if action == "ticket_close"  else "rejected"
    close_reason = "Решено администратором" if action == "ticket_close" else "Отклонено администратором"

    row = close_ticket(ticket_code, uid, close_reason, new_status)

    bot.answer_callback_query(c.id, "✅ Тикет обновлён")
    admin_p = get_player(uid)
    admin_name = admin_p[1] if admin_p else str(uid)

    # Редактируем сообщение у этого администратора
    status_emoji = "✅" if action == "ticket_close" else "❌"
    try:
        original_text = c.message.caption or c.message.text or ""
        new_text = original_text + f"\n\n{status_emoji} <b>{close_reason}</b>\n👮 Администратор: {admin_name}"
        if c.message.caption:
            bot.edit_message_caption(new_text, c.message.chat.id, c.message.message_id, parse_mode="HTML")
        else:
            bot.edit_message_text(new_text, c.message.chat.id, c.message.message_id, parse_mode="HTML")
    except Exception:
        pass

    # Уведомить пользователя о решении
    if row:
        reporter_uid, match_code, reason = row
        mc_text = f"<code>#{match_code}</code>" if match_code else "не указан"
        if action == "ticket_close":
            user_msg = (
                f"✅ <b>Тикет {ticket_code} закрыт (решено)</b>\n\n"
                f"🎮 Матч: {mc_text}\n"
                f"📝 Ваша жалоба рассмотрена и принята.\n"
                f"👮 Администратор: <b>{admin_name}</b>"
            )
        else:
            user_msg = (
                f"❌ <b>Тикет {ticket_code} отклонён</b>\n\n"
                f"🎮 Матч: {mc_text}\n"
                f"📝 Ваша жалоба отклонена.\n"
                f"👮 Администратор: <b>{admin_name}</b>"
            )
        try:
            bot.send_message(reporter_uid, user_msg, parse_mode="HTML")
        except Exception:
            pass


# ==================== КРЕАТОРСКАЯ ПАНЕЛЬ ====================

_RESTRICTABLE = [
    ("give_coins",    "💰 Выдача монет"),
    ("set_elo",       "📊 Изменение ELO"),
    ("edit_stats",    "📈 Редактирование статы"),
    ("warn",          "⚠️ Варны / Снятие варнов"),
    ("mute",          "🔇 Мут / Размут"),
    ("check",         "🔎 Проверка / Снятие"),
    ("ban",           "🚫 Бан / Разбан"),
    ("give_admin",    "👑 Выдача/Снятие админки"),
    ("give_game_reg", "🎮 Роль Гейм Рег"),
    ("quals_access",  "⭐ Quals доступ"),
    ("promos",        "🎁 Промокоды"),
    ("broadcast",     "📢 Рассылка"),
    ("give_verified", "✅ Синяя галочка"),
    ("seasons",       "🏆 Сезоны"),
    ("matches",       "🎮 Матчи"),
]


def _get_admin_logs(limit=25):
    try:
        conn = _db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT al.admin_id, p.username, al.action, al.target_id, al.details, al.created_at "
            "FROM admin_logs al LEFT JOIN players p ON p.user_id = al.admin_id "
            "ORDER BY al.created_at DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"[_get_admin_logs] {e}")
        return []


def _get_admin_list():
    try:
        conn = _db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT user_id, username FROM players WHERE is_admin=1 AND is_bot=0 ORDER BY username"
        )
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"[_get_admin_list] {e}")
        return []


def _get_admin_restrictions_set(admin_id):
    try:
        conn = _db()
        cur  = conn.cursor()
        cur.execute("SELECT action FROM admin_restrictions WHERE admin_id=%s", (admin_id,))
        rows = cur.fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception as e:
        print(f"[_get_admin_restrictions_set] {e}")
        return set()


def _reset_player_stats(target_uid, table="players"):
    conn = _db()
    cur  = conn.cursor()
    cur.execute(
        f"""UPDATE {table} SET
            elo=1000, wins=0, losses=0, kills=0, deaths=0, assists=0, mvp_count=0,
            quals_elo=1000, quals_wins=0, quals_losses=0,
            quals_kills=0, quals_deaths=0, quals_assists=0,
            duo_elo=1000, duo_wins=0, duo_losses=0,
            duo_kills=0, duo_deaths=0, duo_assists=0
        WHERE user_id=%s""",
        (target_uid,),
    )
    conn.commit()
    conn.close()


def _reset_all_stats(table="players"):
    conn = _db()
    cur  = conn.cursor()
    cur.execute(
        f"""UPDATE {table} SET
            elo=1000, wins=0, losses=0, kills=0, deaths=0, assists=0, mvp_count=0,
            quals_elo=1000, quals_wins=0, quals_losses=0,
            quals_kills=0, quals_deaths=0, quals_assists=0,
            duo_elo=1000, duo_wins=0, duo_losses=0,
            duo_kills=0, duo_deaths=0, duo_assists=0
        WHERE is_bot=0"""
    )
    conn.commit()
    conn.close()


def _creator_panel_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📊 Статистика бота",          callback_data="creator_botstats"),
        types.InlineKeyboardButton("📋 Логи админов",             callback_data="creator_logs"),
    )
    kb.add(
        types.InlineKeyboardButton("📢 Рассылка",                 callback_data="creator_broadcast"),
        types.InlineKeyboardButton("🎁 Выдать Premium",           callback_data="creator_give_premium"),
    )
    kb.add(
        types.InlineKeyboardButton("✅ Верификация игрока",       callback_data="creator_verify_player"),
        types.InlineKeyboardButton("🛡️ Управление админами",     callback_data="creator_manage_admins"),
    )
    kb.add(
        types.InlineKeyboardButton("🏆 Новый сезон",              callback_data="creator_new_season"),
        types.InlineKeyboardButton("💰 Монеты игроку",            callback_data="creator_give_coins"),
    )
    kb.add(
        types.InlineKeyboardButton("🧹 Обнулить стату всех",      callback_data="creator_reset_all"),
        types.InlineKeyboardButton("👤 Обнулить стату игрока",    callback_data="creator_reset_player"),
    )
    kb.add(
        types.InlineKeyboardButton("🔒 Ограничения для админов",  callback_data="creator_restrict_menu"),
    )
    kb.add(
        types.InlineKeyboardButton("🔙 Назад",                    callback_data="back"),
    )
    return kb


@bot.callback_query_handler(func=lambda c: c.data == "creator_panel")
def cb_creator_panel(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    text = (
        "🔴 <b>КРЕАТОРСКАЯ ПАНЕЛЬ</b>\n\n"
        "📊 Статистика бота · 📋 Логи админов\n"
        "📢 Рассылка игрокам · 🎁 Выдать Premium\n"
        "✅ Верификация · 🛡️ Управление админами\n"
        "🏆 Новый сезон · 💰 Монеты игроку\n"
        "🧹 Обнулить стату всех · 👤 Обнулить игрока\n"
        "🔒 Ограничения для администраторов"
    )
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                              reply_markup=_creator_panel_kb(), parse_mode="HTML")
    except Exception:
        bot.send_message(c.message.chat.id, text,
                         reply_markup=_creator_panel_kb(), parse_mode="HTML")
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "creator_logs")
def cb_creator_logs(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    rows = _get_admin_logs(limit=25)
    if not rows:
        text = "📋 <b>Логи администраторов</b>\n\nЛогов нет."
    else:
        lines = ["📋 <b>Логи администраторов</b> (последние 25)\n"]
        for r in rows:
            admin_id, admin_name, action, target_id, details, created_at = r
            dt    = fmt_dt(int(created_at)) if created_at else "—"
            t_str = f" → {target_id}" if target_id else ""
            d_str = f" | {str(details)[:40]}" if details else ""
            lines.append(f"<code>{dt}</code> | <b>{admin_name or admin_id}</b>: {action}{t_str}{d_str}")
        text = "\n".join(lines)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="creator_panel"))
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                              reply_markup=kb, parse_mode="HTML")
    except Exception:
        bot.send_message(c.message.chat.id, text, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "creator_reset_all")
def cb_creator_reset_all(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Да, обнулить всех", callback_data="creator_reset_all_exec"),
        types.InlineKeyboardButton("❌ Отмена",            callback_data="creator_panel"),
    )
    bot.edit_message_text(
        "⚠️ <b>ПОДТВЕРЖДЕНИЕ</b>\n\n"
        "Вы уверены, что хотите обнулить статистику <b>ВСЕХ</b> игроков?\n"
        "Это действие необратимо!\n\n"
        "(ELO сбрасывается к 1000, все матч-стата обнуляется)",
        c.message.chat.id, c.message.message_id,
        reply_markup=kb, parse_mode="HTML",
    )
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "creator_reset_all_exec")
def cb_creator_reset_all_exec(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    try:
        _reset_all_stats("players")
        log_admin_action(uid, "reset_all_stats", details="Обнуление статы всех игроков")
        bot.answer_callback_query(c.id, "✅ Статистика всех игроков обнулена!", show_alert=True)
        bot.edit_message_text(
            "✅ <b>Статистика всех игроков успешно обнулена.</b>",
            c.message.chat.id, c.message.message_id,
            reply_markup=_creator_panel_kb(), parse_mode="HTML",
        )
    except Exception as e:
        bot.answer_callback_query(c.id, f"❌ Ошибка: {e}", show_alert=True)


@bot.callback_query_handler(func=lambda c: c.data == "creator_reset_player")
def cb_creator_reset_player(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    creator_flow[uid] = {"step": "reset_player"}
    bot.answer_callback_query(c.id)
    bot.send_message(uid, "👤 Введите Telegram ID или никнейм игрока для обнуления статы:")


@bot.callback_query_handler(func=lambda c: c.data == "creator_restrict_menu")
def cb_creator_restrict_menu(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    admins = _get_admin_list()
    non_creator = [(a_uid, a_name) for a_uid, a_name in admins if a_uid != CREATOR_ID]
    if not non_creator:
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="creator_panel"))
        bot.edit_message_text(
            "🔒 <b>Ограничения для админов</b>\n\nАдминов нет.",
            c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML",
        )
        bot.answer_callback_query(c.id)
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    for a_uid, a_name in non_creator:
        kb.add(types.InlineKeyboardButton(
            f"👤 {a_name or a_uid}",
            callback_data=f"creator_restrict_admin_{a_uid}",
        ))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="creator_panel"))
    bot.edit_message_text(
        "🔒 <b>Ограничения для администраторов</b>\n\nВыберите админа для настройки:",
        c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML",
    )
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("creator_restrict_admin_"))
def cb_creator_restrict_admin(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    try:
        target_admin = int(c.data.replace("creator_restrict_admin_", ""))
    except ValueError:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return
    target_p    = get_player(target_admin)
    target_name = target_p[1] if target_p else str(target_admin)
    restrictions = _get_admin_restrictions_set(target_admin)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for action_key, action_label in _RESTRICTABLE:
        is_blocked = action_key in restrictions
        status = "🚫" if is_blocked else "✅"
        kb.add(types.InlineKeyboardButton(
            f"{status} {action_label}",
            callback_data=f"creator_toggle_{target_admin}_{action_key}",
        ))
    kb.add(types.InlineKeyboardButton("🔙 К списку", callback_data="creator_restrict_menu"))
    active     = [lbl for key, lbl in _RESTRICTABLE if key in restrictions]
    blocked_str = ("\n".join(f"  • {l}" for l in active)) if active else "  нет"
    text = (
        f"🔒 <b>Ограничения для {target_name}</b>\n\n"
        f"Сейчас заблокировано:\n{blocked_str}\n\n"
        "🚫 = запрещено  |  ✅ = разрешено"
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("creator_toggle_"))
def cb_creator_toggle_restriction(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    # Format: creator_toggle_{admin_id}_{action_key}
    # action_key may contain underACores, so split from left only twice after prefix
    suffix = c.data[len("creator_toggle_"):]
    parts  = suffix.split("_", 1)
    if len(parts) < 2:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return
    try:
        target_admin = int(parts[0])
        action_key   = parts[1]
    except ValueError:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return
    restrictions = _get_admin_restrictions_set(target_admin)
    try:
        conn = _db()
        cur  = conn.cursor()
        if action_key in restrictions:
            cur.execute(
                "DELETE FROM admin_restrictions WHERE admin_id=%s AND action=%s",
                (target_admin, action_key),
            )
            new_state = "разрешено ✅"
        else:
            cur.execute(
                "INSERT INTO admin_restrictions (admin_id, action) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (target_admin, action_key),
            )
            new_state = "запрещено 🚫"
        conn.commit()
        conn.close()
        log_admin_action(uid, "toggle_restriction", target_id=target_admin,
                         details=f"{action_key} -> {new_state}")
        bot.answer_callback_query(c.id, f"{action_key}: {new_state}", show_alert=False)
    except Exception as e:
        bot.answer_callback_query(c.id, f"❌ Ошибка: {e}", show_alert=True)
        return
    # Refresh the page
    target_p    = get_player(target_admin)
    target_name = target_p[1] if target_p else str(target_admin)
    restrictions = _get_admin_restrictions_set(target_admin)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for a_key, a_label in _RESTRICTABLE:
        is_blocked = a_key in restrictions
        status = "🚫" if is_blocked else "✅"
        kb.add(types.InlineKeyboardButton(
            f"{status} {a_label}",
            callback_data=f"creator_toggle_{target_admin}_{a_key}",
        ))
    kb.add(types.InlineKeyboardButton("🔙 К списку", callback_data="creator_restrict_menu"))
    active     = [lbl for key, lbl in _RESTRICTABLE if key in restrictions]
    blocked_str = ("\n".join(f"  • {l}" for l in active)) if active else "  нет"
    text = (
        f"🔒 <b>Ограничения для {target_name}</b>\n\n"
        f"Сейчас заблокировано:\n{blocked_str}\n\n"
        "🚫 = запрещено  |  ✅ = разрешено"
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                          reply_markup=kb, parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("creator_reset_exec_"))
def cb_creator_reset_exec(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    try:
        t_uid = int(c.data.replace("creator_reset_exec_", ""))
    except ValueError:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return
    target_p = get_player(t_uid)
    t_name   = target_p[1] if target_p else str(t_uid)
    try:
        _reset_player_stats(t_uid, "players")
        log_admin_action(uid, "reset_player_stats", target_id=t_uid, details=t_name)
        bot.answer_callback_query(c.id, f"✅ Стата {t_name} обнулена!", show_alert=True)
        bot.edit_message_text(
            f"✅ <b>Статистика игрока {t_name} успешно обнулена.</b>",
            c.message.chat.id, c.message.message_id,
            reply_markup=_creator_panel_kb(), parse_mode="HTML",
        )
    except Exception as e:
        bot.answer_callback_query(c.id, f"❌ Ошибка: {e}", show_alert=True)


@bot.message_handler(func=lambda m: m.from_user.id in creator_flow and m.text is not None)
def handle_creator_flow(msg):
    uid  = msg.from_user.id
    if not is_creator(uid):
        creator_flow.pop(uid, None)
        return
    flow = creator_flow.get(uid, {})
    step = flow.get("step")

    # Делегируем расширенные шаги
    _extended_steps = {
        "broadcast", "broadcast_confirm",
        "give_premium_id", "give_premium_days",
        "verify_player",
        "promote_admin",
        "give_coins_id", "give_coins_amount",
    }
    if step in _extended_steps:
        _handle_creator_flow_extended(msg, uid, flow, step)
        return

    if step == "reset_player":
        creator_flow.pop(uid, None)
        inp = msg.text.strip()
        target_p = None
        if inp.isdigit():
            target_p = get_player(int(inp))
        else:
            try:
                conn2 = _db()
                cur2  = conn2.cursor()
                cur2.execute(
                    "SELECT * FROM players WHERE LOWER(username)=LOWER(%s) AND is_bot=0", (inp,)
                )
                target_p = cur2.fetchone()
                conn2.close()
            except Exception:
                pass
        if not target_p:
            bot.send_message(uid, "❌ Игрок не найден. Попробуйте ещё раз.")
            return
        t_uid  = target_p[0]
        t_name = target_p[1] or str(t_uid)
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Да, обнулить", callback_data=f"creator_reset_exec_{t_uid}"),
            types.InlineKeyboardButton("❌ Отмена",        callback_data="creator_panel"),
        )
        bot.send_message(
            uid,
            f"⚠️ Обнулить статистику игрока <b>{t_name}</b> (ID: <code>{t_uid}</code>)?\n"
            "ELO → 1000, все матч-стата → 0. Это необратимо!",
            reply_markup=kb, parse_mode="HTML",
        )


# ==================== CREATOR: СТАТИСТИКА БОТА ====================
@bot.callback_query_handler(func=lambda c: c.data == "creator_botstats")
def cb_creator_botstats(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    try:
        conn = _db()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM players WHERE is_bot=0")
        total_players = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM players WHERE is_bot=0 AND registered=1")
        reg_players = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM players WHERE premium_until > %s AND is_bot=0", (int(time.time()),))
        premium_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM players WHERE is_banned=1 AND is_bot=0")
        banned_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM players WHERE is_admin=1 AND is_bot=0")
        admin_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM players WHERE is_verified=1 AND is_bot=0")
        verified_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM players WHERE quals_access=1 AND is_bot=0")
        quals_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM matches")
        total_matches = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM promo_codes WHERE is_active=1")
        promos = cur.fetchone()[0]
        cur.execute("SELECT SUM(coins) FROM players WHERE is_bot=0")
        total_coins = cur.fetchone()[0] or 0
        cur.execute("SELECT season_number FROM seasons WHERE is_active=1 ORDER BY id DESC LIMIT 1")
        row_s = cur.fetchone()
        season_num = row_s[0] if row_s else 1
        conn.close()
        text = (
            "📊 <b>СТАТИСТИКА БОТА</b>\n\n"
            f"👥 Всего игроков: <b>{total_players}</b> (рег: {reg_players})\n"
            f"👑 Premium: <b>{premium_count}</b>\n"
            f"🛡️ Администраторов: <b>{admin_count}</b>\n"
            f"✅ Верифицированных: <b>{verified_count}</b>\n"
            f"⭐ Quals доступ: <b>{quals_count}</b>\n"
            f"🚫 Забанено: <b>{banned_count}</b>\n"
            f"⚔️ Матчей сыграно: <b>{total_matches}</b>\n"
            f"🎫 Активных промокодов: <b>{promos}</b>\n"
            f"💰 Монет в обороте: <b>{total_coins:,}</b>\n"
            f"🏆 Текущий сезон: <b>#{season_num}</b>"
        )
    except Exception as e:
        text = f"❌ Ошибка получения статистики: {e}"
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="creator_panel"))
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                              reply_markup=kb, parse_mode="HTML")
    except Exception:
        bot.send_message(c.message.chat.id, text, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


# ==================== CREATOR: РАССЫЛКА ====================
@bot.callback_query_handler(func=lambda c: c.data == "creator_broadcast")
def cb_creator_broadcast(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    creator_flow[uid] = {"step": "broadcast"}
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="creator_panel"))
    bot.send_message(
        uid,
        "📢 <b>Рассылка всем игрокам</b>\n\n"
        "Введите текст сообщения. Поддерживается HTML-разметка.\n"
        "Сообщение будет отправлено всем зарегистрированным игрокам.",
        reply_markup=kb, parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("creator_broadcast_confirm_"))
def cb_creator_broadcast_confirm(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    text_key = c.data.replace("creator_broadcast_confirm_", "")
    flow = creator_flow.get(uid, {})
    broadcast_text = flow.get("broadcast_text", "")
    if not broadcast_text:
        bot.answer_callback_query(c.id, "❌ Текст не найден")
        return
    creator_flow.pop(uid, None)
    bot.answer_callback_query(c.id)
    try:
        conn = _db()
        cur  = conn.cursor()
        cur.execute("SELECT user_id FROM players WHERE is_bot=0 AND registered=1")
        player_ids = [row[0] for row in cur.fetchall()]
        conn.close()
    except Exception as e:
        bot.send_message(uid, f"❌ Ошибка БД: {e}")
        return
    sent = 0
    failed = 0
    for pid in player_ids:
        try:
            bot.send_message(pid, f"📢 <b>Сообщение от администрации:</b>\n\n{broadcast_text}",
                             parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.05)
    log_admin_action(uid, "broadcast", details=f"Отправлено: {sent}, ошибок: {failed}")
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 В панель", callback_data="creator_panel"))
    bot.send_message(uid,
        f"✅ <b>Рассылка завершена</b>\n\nОтправлено: <b>{sent}</b>\nОшибок: <b>{failed}</b>",
        reply_markup=kb, parse_mode="HTML")


# ==================== CREATOR: ВЫДАТЬ PREMIUM ====================
@bot.callback_query_handler(func=lambda c: c.data == "creator_give_premium")
def cb_creator_give_premium(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    creator_flow[uid] = {"step": "give_premium_id"}
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="creator_panel"))
    bot.send_message(uid, "🎁 Введите Telegram ID или ник игрока для выдачи Premium:",
                     reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("creator_premium_exec_"))
def cb_creator_premium_exec(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    parts = c.data.replace("creator_premium_exec_", "").split("_")
    if len(parts) < 2:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return
    try:
        t_uid = int(parts[0])
        days  = int(parts[1])
    except ValueError:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return
    target_p = get_player(t_uid)
    t_name = target_p[1] if target_p else str(t_uid)
    try:
        conn = _db()
        cur  = conn.cursor()
        cur.execute("SELECT premium_until FROM players WHERE user_id=%s", (t_uid,))
        row = cur.fetchone()
        now = int(time.time())
        current_until = row[0] if (row and row[0] and row[0] > now) else now
        new_until = current_until + days * 86400
        cur.execute("UPDATE players SET premium_until=%s WHERE user_id=%s", (new_until, t_uid))
        conn.commit()
        conn.close()
        log_admin_action(uid, "give_premium", target_id=t_uid,
                         details=f"{t_name} +{days}д")
        bot.answer_callback_query(c.id, f"✅ Premium выдан {t_name} на {days} дней!", show_alert=True)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 В панель", callback_data="creator_panel"))
        bot.edit_message_text(
            f"✅ <b>Premium выдан игроку {t_name}</b> на <b>{days} дней</b>.",
            c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
        try:
            bot.send_message(t_uid,
                f"🎉 Вам выдан <b>Premium статус</b> на <b>{days} дней</b>!\n"
                "Наслаждайтесь привилегиями 👑", parse_mode="HTML")
        except Exception:
            pass
    except Exception as e:
        bot.answer_callback_query(c.id, f"❌ Ошибка: {e}", show_alert=True)


# ==================== CREATOR: ВЕРИФИКАЦИЯ ====================
@bot.callback_query_handler(func=lambda c: c.data == "creator_verify_player")
def cb_creator_verify_player(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    creator_flow[uid] = {"step": "verify_player"}
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="creator_panel"))
    bot.send_message(uid,
        "✅ <b>Верификация игрока</b>\n\nВведите Telegram ID или ник игрока:",
        reply_markup=kb, parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("creator_verify_exec_"))
def cb_creator_verify_exec(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    parts = c.data.replace("creator_verify_exec_", "").split("_")
    if len(parts) < 2:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return
    try:
        t_uid  = int(parts[0])
        action = parts[1]
    except ValueError:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return
    target_p = get_player(t_uid)
    t_name = target_p[1] if target_p else str(t_uid)
    new_val = 1 if action == "add" else 0
    try:
        conn = _db()
        cur  = conn.cursor()
        cur.execute("UPDATE players SET is_verified=%s WHERE user_id=%s", (new_val, t_uid))
        conn.commit()
        conn.close()
        label = "выдана ✅" if new_val else "снята ❌"
        log_admin_action(uid, "toggle_verify", target_id=t_uid, details=f"{t_name} -> {label}")
        bot.answer_callback_query(c.id, f"Верификация {label} для {t_name}", show_alert=True)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 В панель", callback_data="creator_panel"))
        bot.edit_message_text(
            f"✅ Верификация <b>{label}</b> игроку <b>{t_name}</b>.",
            c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
        if new_val:
            try:
                bot.send_message(t_uid,
                    "✅ Вам выдана <b>синяя галочка верификации</b>!\n"
                    "Теперь она отображается рядом с вашим ником.", parse_mode="HTML")
            except Exception:
                pass
    except Exception as e:
        bot.answer_callback_query(c.id, f"❌ Ошибка: {e}", show_alert=True)


# ==================== CREATOR: УПРАВЛЕНИЕ АДМИНАМИ ====================
@bot.callback_query_handler(func=lambda c: c.data == "creator_manage_admins")
def cb_creator_manage_admins(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    admins = _get_admin_list()
    non_creator_admins = [(a_uid, a_name) for a_uid, a_name in admins if a_uid != CREATOR_ID]
    text = "🛡️ <b>Управление администраторами</b>\n\n"
    if non_creator_admins:
        text += "Текущие администраторы:\n"
        for a_uid, a_name in non_creator_admins:
            text += f"  • {a_name or a_uid} (<code>{a_uid}</code>)\n"
    else:
        text += "Администраторов нет.\n"
    text += "\nДействия:"
    kb = types.InlineKeyboardMarkup(row_width=1)
    for a_uid, a_name in non_creator_admins:
        kb.add(types.InlineKeyboardButton(
            f"❌ Снять {a_name or a_uid}",
            callback_data=f"creator_demote_{a_uid}",
        ))
    kb.add(types.InlineKeyboardButton("➕ Назначить нового админа", callback_data="creator_promote"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="creator_panel"))
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                              reply_markup=kb, parse_mode="HTML")
    except Exception:
        bot.send_message(c.message.chat.id, text, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "creator_promote")
def cb_creator_promote(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    creator_flow[uid] = {"step": "promote_admin"}
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="creator_manage_admins"))
    bot.send_message(uid, "🛡️ Введите Telegram ID игрока для назначения администратором:",
                     reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("creator_demote_"))
def cb_creator_demote(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    try:
        t_uid = int(c.data.replace("creator_demote_", ""))
    except ValueError:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return
    target_p = get_player(t_uid)
    t_name = target_p[1] if target_p else str(t_uid)
    try:
        conn = _db()
        cur  = conn.cursor()
        cur.execute("UPDATE players SET is_admin=0 WHERE user_id=%s", (t_uid,))
        conn.commit()
        conn.close()
        if t_uid in ADMIN_IDS_LIST:
            ADMIN_IDS_LIST.remove(t_uid)
        log_admin_action(uid, "demote_admin", target_id=t_uid, details=t_name)
        bot.answer_callback_query(c.id, f"✅ {t_name} снят с должности администратора", show_alert=True)
    except Exception as e:
        bot.answer_callback_query(c.id, f"❌ Ошибка: {e}", show_alert=True)
        return
    cb_creator_manage_admins(c)


# ==================== CREATOR: МОНЕТЫ ИГРОКУ ====================
@bot.callback_query_handler(func=lambda c: c.data == "creator_give_coins")
def cb_creator_give_coins(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    creator_flow[uid] = {"step": "give_coins_id"}
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="creator_panel"))
    bot.send_message(uid,
        "💰 <b>Изменение монет игрока</b>\n\nВведите Telegram ID или ник игрока:",
        reply_markup=kb, parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("creator_coins_exec_"))
def cb_creator_coins_exec(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    parts = c.data.replace("creator_coins_exec_", "").split("_")
    if len(parts) < 2:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return
    try:
        t_uid  = int(parts[0])
        amount = int(parts[1])
    except ValueError:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return
    target_p = get_player(t_uid)
    t_name = target_p[1] if target_p else str(t_uid)
    try:
        conn = _db()
        cur  = conn.cursor()
        cur.execute("UPDATE players SET coins = GREATEST(0, coins + %s) WHERE user_id=%s", (amount, t_uid))
        cur.execute("SELECT coins FROM players WHERE user_id=%s", (t_uid,))
        new_bal = cur.fetchone()[0]
        conn.commit()
        conn.close()
        sign = "+" if amount >= 0 else ""
        log_admin_action(uid, "give_coins", target_id=t_uid,
                         details=f"{t_name} {sign}{amount} AC")
        bot.answer_callback_query(c.id,
            f"✅ {t_name}: {sign}{amount} AC. Баланс: {new_bal} AC", show_alert=True)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 В панель", callback_data="creator_panel"))
        bot.edit_message_text(
            f"✅ Игроку <b>{t_name}</b>: <b>{sign}{amount} AC</b>\nНовый баланс: <b>{new_bal} AC</b>",
            c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
        try:
            action_word = "начислено" if amount >= 0 else "списано"
            bot.send_message(t_uid,
                f"💰 Вам {action_word} <b>{abs(amount)} AC</b> администратором.\n"
                f"Ваш баланс: <b>{new_bal} AC</b>", parse_mode="HTML")
        except Exception:
            pass
    except Exception as e:
        bot.answer_callback_query(c.id, f"❌ Ошибка: {e}", show_alert=True)


# ==================== CREATOR: НОВЫЙ СЕЗОН ====================
@bot.callback_query_handler(func=lambda c: c.data == "creator_new_season")
def cb_creator_new_season(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    try:
        conn = _db()
        cur  = conn.cursor()
        cur.execute("SELECT season_number FROM seasons WHERE is_active=1 ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        current_season = row[0] if row else 1
    except Exception:
        current_season = 1
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            f"✅ Да, начать сезон #{current_season + 1}",
            callback_data=f"creator_new_season_exec_{current_season + 1}",
        ),
        types.InlineKeyboardButton("❌ Отмена", callback_data="creator_panel"),
    )
    bot.edit_message_text(
        f"🏆 <b>Новый сезон</b>\n\n"
        f"Текущий сезон: <b>#{current_season}</b>\n\n"
        "⚠️ При запуске нового сезона:\n"
        "• Текущая статистика всех игроков будет сохранена в архив\n"
        "• ELO сброситься к 1000, вся матч-стата обнулится\n"
        "• Это действие необратимо!\n\n"
        f"Запустить сезон <b>#{current_season + 1}</b>?",
        c.message.chat.id, c.message.message_id,
        reply_markup=kb, parse_mode="HTML",
    )
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("creator_new_season_exec_"))
def cb_creator_new_season_exec(c):
    uid = c.from_user.id
    if not is_creator(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    try:
        new_season_num = int(c.data.replace("creator_new_season_exec_", ""))
    except ValueError:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return
    try:
        conn = _db()
        cur  = conn.cursor()
        cur.execute("SELECT id FROM seasons WHERE is_active=1 ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if row:
            season_id = row[0]
            cur.execute("SELECT user_id, username, elo, wins, losses, kills, deaths, assists, "
                        "quals_wins, quals_losses, quals_kills, quals_deaths, quals_assists, "
                        "quals_elo, mvp_count FROM players WHERE is_bot=0")
            players_snap = cur.fetchall()
            for ps in players_snap:
                cur.execute("""
                    INSERT INTO season_player_history
                    (season_id, season_number, user_id, username, elo, wins, losses, kills,
                     deaths, assists, quals_wins, quals_losses, quals_kills, quals_deaths,
                     quals_assists, quals_elo, mvp_count)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (season_id, new_season_num - 1, *ps))
            cur.execute("UPDATE seasons SET is_active=0, ended_at=%s, reset_by=%s WHERE id=%s",
                        (int(time.time()), uid, season_id))
        cur.execute(
            "INSERT INTO seasons (season_number, name, is_active) VALUES (%s, %s, 1)",
            (new_season_num, f"Сезон {new_season_num}"),
        )
        _reset_all_stats("players")
        conn.commit()
        conn.close()
        log_admin_action(uid, "new_season", details=f"Сезон #{new_season_num} начат")
        bot.answer_callback_query(c.id,
            f"✅ Сезон #{new_season_num} начат! Статистика сохранена.", show_alert=True)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 В панель", callback_data="creator_panel"))
        bot.edit_message_text(
            f"🏆 <b>Сезон #{new_season_num} успешно начат!</b>\n\n"
            "Статистика предыдущего сезона сохранена в архиве.\n"
            "Все игроки начинают с ELO 1000.",
            c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        bot.answer_callback_query(c.id, f"❌ Ошибка: {e}", show_alert=True)


# ==================== CREATOR FLOW: расширенный обработчик текста ====================
def _handle_creator_flow_extended(msg, uid, flow, step):
    """Возвращает True если шаг обработан, иначе False."""
    inp = msg.text.strip()

    def _find_player_by_inp(inp):
        if inp.isdigit():
            return get_player(int(inp))
        try:
            conn2 = _db()
            cur2  = conn2.cursor()
            cur2.execute("SELECT * FROM players WHERE LOWER(username)=LOWER(%s) AND is_bot=0", (inp,))
            p = cur2.fetchone()
            conn2.close()
            return p
        except Exception:
            return None

    if step == "broadcast":
        creator_flow[uid] = {"step": "broadcast_confirm", "broadcast_text": inp}
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Отправить", callback_data=f"creator_broadcast_confirm_ok"),
            types.InlineKeyboardButton("❌ Отмена",    callback_data="creator_panel"),
        )
        bot.send_message(uid,
            f"📢 <b>Предпросмотр рассылки:</b>\n\n{inp}\n\n"
            "Отправить это сообщение всем игрокам?",
            reply_markup=kb, parse_mode="HTML")
        return True

    if step == "broadcast_confirm":
        return True

    if step == "give_premium_id":
        target_p = _find_player_by_inp(inp)
        if not target_p:
            bot.send_message(uid, "❌ Игрок не найден. Попробуйте ещё раз.")
            return True
        t_uid  = target_p[0]
        t_name = target_p[1] or str(t_uid)
        creator_flow[uid] = {"step": "give_premium_days", "target_id": t_uid, "target_name": t_name}
        bot.send_message(uid,
            f"🎁 Игрок: <b>{t_name}</b>\n\nНа сколько дней выдать Premium? (введите число):",
            parse_mode="HTML")
        return True

    if step == "give_premium_days":
        if not inp.lstrip("-").isdigit():
            bot.send_message(uid, "❌ Введите число дней.")
            return True
        days = int(inp)
        if days <= 0:
            bot.send_message(uid, "❌ Количество дней должно быть больше 0.")
            return True
        t_uid  = flow.get("target_id")
        t_name = flow.get("target_name", str(t_uid))
        creator_flow.pop(uid, None)
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Выдать",  callback_data=f"creator_premium_exec_{t_uid}_{days}"),
            types.InlineKeyboardButton("❌ Отмена",  callback_data="creator_panel"),
        )
        bot.send_message(uid,
            f"🎁 Выдать <b>{days} дней</b> Premium игроку <b>{t_name}</b>?",
            reply_markup=kb, parse_mode="HTML")
        return True

    if step == "verify_player":
        target_p = _find_player_by_inp(inp)
        if not target_p:
            bot.send_message(uid, "❌ Игрок не найден.")
            return True
        t_uid    = target_p[0]
        t_name   = target_p[1] or str(t_uid)
        is_vf    = bool(target_p[27]) if len(target_p) > 27 else False
        creator_flow.pop(uid, None)
        kb = types.InlineKeyboardMarkup(row_width=2)
        if is_vf:
            kb.add(
                types.InlineKeyboardButton("❌ Снять верификацию", callback_data=f"creator_verify_exec_{t_uid}_remove"),
                types.InlineKeyboardButton("🔙 Отмена",            callback_data="creator_panel"),
            )
            status_text = "✅ уже верифицирован"
        else:
            kb.add(
                types.InlineKeyboardButton("✅ Выдать верификацию", callback_data=f"creator_verify_exec_{t_uid}_add"),
                types.InlineKeyboardButton("🔙 Отмена",             callback_data="creator_panel"),
            )
            status_text = "❌ не верифицирован"
        bot.send_message(uid,
            f"✅ Игрок: <b>{t_name}</b>\nСтатус: {status_text}",
            reply_markup=kb, parse_mode="HTML")
        return True

    if step == "promote_admin":
        target_p = _find_player_by_inp(inp)
        if not target_p:
            bot.send_message(uid, "❌ Игрок не найден.")
            return True
        t_uid  = target_p[0]
        t_name = target_p[1] or str(t_uid)
        creator_flow.pop(uid, None)
        try:
            conn = _db()
            cur  = conn.cursor()
            cur.execute("UPDATE players SET is_admin=1 WHERE user_id=%s", (t_uid,))
            conn.commit()
            conn.close()
            if t_uid not in ADMIN_IDS_LIST:
                ADMIN_IDS_LIST.append(t_uid)
            log_admin_action(uid, "promote_admin", target_id=t_uid, details=t_name)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("🔙 В панель", callback_data="creator_panel"))
            bot.send_message(uid,
                f"✅ <b>{t_name}</b> назначен администратором!",
                reply_markup=kb, parse_mode="HTML")
            try:
                bot.send_message(t_uid, "🛡️ Вы были назначены <b>администратором</b>!", parse_mode="HTML")
            except Exception:
                pass
        except Exception as e:
            bot.send_message(uid, f"❌ Ошибка: {e}")
        return True

    if step == "give_coins_id":
        target_p = _find_player_by_inp(inp)
        if not target_p:
            bot.send_message(uid, "❌ Игрок не найден.")
            return True
        t_uid  = target_p[0]
        t_name = target_p[1] or str(t_uid)
        creator_flow[uid] = {"step": "give_coins_amount", "target_id": t_uid, "target_name": t_name}
        bot.send_message(uid,
            f"💰 Игрок: <b>{t_name}</b>\n\n"
            "Введите количество монет (положительное — начислить, отрицательное — списать):",
            parse_mode="HTML")
        return True

    if step == "give_coins_amount":
        if not inp.lstrip("-").isdigit():
            bot.send_message(uid, "❌ Введите число.")
            return True
        amount = int(inp)
        t_uid  = flow.get("target_id")
        t_name = flow.get("target_name", str(t_uid))
        creator_flow.pop(uid, None)
        sign = "+" if amount >= 0 else ""
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"creator_coins_exec_{t_uid}_{amount}"),
            types.InlineKeyboardButton("❌ Отмена",      callback_data="creator_panel"),
        )
        bot.send_message(uid,
            f"💰 Игроку <b>{t_name}</b>: <b>{sign}{amount} AC</b>\nПодтвердить?",
            reply_markup=kb, parse_mode="HTML")
        return True

    return False


# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    print("🚀 Инициализация БД...")
    init_db()
    print("⚙️ Загрузка настроек веток...")
    load_dynamic_settings()
    print("🏠 Загрузка приваток пользователей...")
    load_user_privates()
    print("♻️ Восстановление активных матчей...")
    restore_active_matches()
    print("⏰ Запуск авто-разбана...")
    threading.Thread(target=auto_unban_loop, daemon=True).start()
    print("🗑️ Запуск авто-удаления сообщений...")
    threading.Thread(target=_auto_delete_loop, daemon=True).start()
    print(f"✅ Бот запущен! ADMIN_CHAT_ID={ADMIN_CHAT_ID} | ADMIN_IDS={ADMIN_IDS_LIST}")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
