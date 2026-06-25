"""
card_client.py — HTTP-клиент генератора карточек.

Заменяет прямые импорты card_generator_html / card_generator.
Переменная окружения:
  CARD_GENERATOR_URL=https://card-generator-xxxx.up.railway.app

Все функции возвращают io.BytesIO с PNG — точно как оригинальные модули.

Дополнительно:
  - Кэш аватаров в памяти (TTL 5 минут) — аватары не скачиваются повторно
  - Автоматический ретрай при сбое сети (1 повтор через 2 секунды)
"""
from __future__ import annotations

import base64
import io
import os
import time
import threading
from typing import Optional

import requests

CARD_URL = os.environ.get("CARD_GENERATOR_URL", "").rstrip("/")

CARDS_ENABLED: bool = bool(CARD_URL)

_TIMEOUT    = 30   # секунд ожидания ответа
_RETRY_WAIT = 2    # секунд между попытками
_CACHE_TTL  = 300  # секунд хранения аватара в кэше (5 минут)

# ──────────────────────────────────────────────────────────────────────────────
# Кэш аватаров: {uid -> (avatar_bytes, expires_at)}
# ──────────────────────────────────────────────────────────────────────────────
_avatar_cache: dict = {}
_cache_lock = threading.Lock()


def cache_avatar(uid: int, avatar_bytes: bytes) -> None:
    """Сохранить аватар в кэш."""
    with _cache_lock:
        _avatar_cache[uid] = (avatar_bytes, time.time() + _CACHE_TTL)


def get_cached_avatar(uid: int) -> Optional[bytes]:
    """Вернуть аватар из кэша или None если устарел / не найден."""
    with _cache_lock:
        entry = _avatar_cache.get(uid)
        if entry is None:
            return None
        avatar_bytes, expires_at = entry
        if time.time() > expires_at:
            del _avatar_cache[uid]
            return None
        return avatar_bytes


def _evict_expired_cache() -> None:
    """Удалить устаревшие записи (вызывается лениво при записи)."""
    now = time.time()
    with _cache_lock:
        expired = [uid for uid, (_, exp) in _avatar_cache.items() if now > exp]
        for uid in expired:
            del _avatar_cache[uid]


# ──────────────────────────────────────────────────────────────────────────────
# HTTP-клиент
# ──────────────────────────────────────────────────────────────────────────────

def _post(endpoint: str, payload: dict) -> io.BytesIO:
    """POST-запрос с одним ретраем при сбое сети."""
    url = f"{CARD_URL}/{endpoint}"
    for attempt in range(2):
        try:
            resp = requests.post(url, json=payload, timeout=_TIMEOUT)
            resp.raise_for_status()
            return io.BytesIO(resp.content)
        except requests.exceptions.RequestException as e:
            if attempt == 0:
                print(f"[card_client] ошибка (попытка 1): {e} — повтор через {_RETRY_WAIT}с")
                time.sleep(_RETRY_WAIT)
            else:
                raise


def _encode_avatar(avatar_bytes: Optional[bytes]) -> Optional[str]:
    """bytes → base64-строка для передачи через JSON."""
    if not avatar_bytes:
        return None
    return base64.b64encode(avatar_bytes).decode()


def _encode_avatars(avatars: Optional[dict]) -> Optional[dict]:
    """Словарь {uid: bytes} → {str(uid): base64str}."""
    if not avatars:
        return None
    return {
        str(uid): base64.b64encode(av).decode()
        for uid, av in avatars.items() if av
    }


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — те же сигнатуры что и в card_generator_html.py
# ──────────────────────────────────────────────────────────────────────────────

def generate_profile_card(
    username: str = "Unknown",
    game_id: str = "",
    user_id: int = 0,
    elo: int = 1000,
    wins: int = 0,
    losses: int = 0,
    kills: int = 0,
    deaths: int = 0,
    assists: int = 0,
    is_premium: bool = False,
    is_admin: bool = False,
    global_rank: int = 0,
    league: str = "default",
    map_stats: list = None,
    recent: list = None,
    leaderboard: list = None,
    quals_stats=None,
    mvp_count: int = 0,
    is_verified: bool = False,
    duo_stats=None,
    avatar_bytes: bytes = None,
    active_frame=None,
    active_banner=None,
    active_background=None,
) -> io.BytesIO:
    _evict_expired_cache()
    payload = dict(
        username=username, game_id=game_id, user_id=user_id,
        elo=elo, wins=wins, losses=losses, kills=kills,
        deaths=deaths, assists=assists, is_premium=is_premium,
        is_admin=is_admin, global_rank=global_rank, league=league,
        map_stats=map_stats or [], recent=recent or [],
        leaderboard=leaderboard or [],
        mvp_count=mvp_count, is_verified=is_verified,
        active_frame=active_frame, active_banner=active_banner,
        active_background=active_background,
        avatar_bytes=_encode_avatar(avatar_bytes),
    )
    return _post("profile", payload)


def generate_leaderboard_card(
    players: list,
    title: str = "ЛУЧШИЕ ИГРОКИ",
    avatars: dict = None,
) -> io.BytesIO:
    payload = dict(players=players, title=title, avatars=_encode_avatars(avatars))
    return _post("leaderboard", payload)


def generate_duo_leaderboard_card(
    players: list,
    title: str = "2v2 ТОП",
    avatars: dict = None,
) -> io.BytesIO:
    payload = dict(players=players, title=title, avatars=_encode_avatars(avatars))
    return _post("duo_leaderboard", payload)


def generate_match_result_card(
    match_code: str = "",
    map_name: str = "",
    winner: str = "ct",
    score_w: int = 0,
    score_l: int = 0,
    players_ct: list = None,
    players_t: list = None,
    league: str = "Default",
    avatars: dict = None,
) -> io.BytesIO:
    payload = dict(
        match_code=match_code, map_name=map_name, winner=winner,
        score_w=score_w, score_l=score_l,
        players_ct=players_ct or [], players_t=players_t or [],
        league=league, avatars=_encode_avatars(avatars),
    )
    return _post("match_result", payload)
