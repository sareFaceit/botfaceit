"""
card_client.py
HTTP-клиент для сервиса генерации карточек (card-generator).

Переменные среды:
  CARD_SERVICE_URL — URL сервиса, например http://localhost:8080
                     Если не задан — CARDS_ENABLED = False, все функции возвращают None.

Использование:
  from card_client import (
      generate_profile_card,
      generate_leaderboard_card,
      generate_match_result_card,
      generate_duo_leaderboard_card,
      CARDS_ENABLED,
      cache_avatar,
      get_cached_avatar,
  )
"""

from __future__ import annotations

import base64
import io
import os
import threading
from typing import Optional

import requests

# ─── Конфиг ───────────────────────────────────────────────────────────────────
CARD_SERVICE_URL: str = (
    os.environ.get("CARD_GENERATOR_URL")
    or os.environ.get("CARD_SERVICE_URL")
    or ""
).rstrip("/")
CARDS_ENABLED: bool = bool(CARD_SERVICE_URL)

_TIMEOUT = 30  # секунд на запрос

# ─── Кэш аватаров в памяти ────────────────────────────────────────────────────
_avatar_cache: dict[int, bytes] = {}
_avatar_lock = threading.Lock()


def cache_avatar(uid: int, avatar_bytes: bytes) -> None:
    """Сохраняет аватар пользователя в кэш."""
    with _avatar_lock:
        _avatar_cache[uid] = avatar_bytes


def get_cached_avatar(uid: int) -> Optional[bytes]:
    """Возвращает кэшированный аватар или None."""
    with _avatar_lock:
        return _avatar_cache.get(uid)


# ─── Вспомогательные функции ──────────────────────────────────────────────────
def _b64(data: Optional[bytes]) -> Optional[str]:
    if not data:
        return None
    return base64.b64encode(data).decode()


def _avatars_to_b64(avatars: Optional[dict]) -> Optional[dict]:
    if not avatars:
        return None
    result = {}
    for uid, data in avatars.items():
        encoded = _b64(data)
        if encoded:
            result[str(uid)] = encoded
    return result or None


def _post(endpoint: str, payload: dict) -> io.BytesIO:
    """Отправляет POST-запрос к сервису и возвращает BytesIO с PNG."""
    url = f"{CARD_SERVICE_URL}{endpoint}"
    resp = requests.post(url, json=payload, timeout=_TIMEOUT)
    resp.raise_for_status()
    return io.BytesIO(resp.content)


# ─── Public API ───────────────────────────────────────────────────────────────
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
    map_stats: Optional[list] = None,
    recent: Optional[list] = None,
    leaderboard: Optional[list] = None,
    quals_stats=None,
    mvp_count: int = 0,
    is_verified: bool = False,
    duo_stats=None,
    avatar_bytes: Optional[bytes] = None,
    active_frame=None,
    active_banner=None,
    active_background=None,
    **_kw,
) -> io.BytesIO:
    payload = {
        "username":         username,
        "game_id":          game_id,
        "user_id":          user_id,
        "elo":              elo,
        "wins":             wins,
        "losses":           losses,
        "kills":            kills,
        "deaths":           deaths,
        "assists":          assists,
        "is_premium":       is_premium,
        "is_admin":         is_admin,
        "global_rank":      global_rank,
        "league":           league,
        "map_stats":        map_stats,
        "recent":           recent,
        "leaderboard":      leaderboard,
        "quals_stats":      quals_stats,
        "mvp_count":        mvp_count,
        "is_verified":      is_verified,
        "duo_stats":        duo_stats,
        "avatar_bytes":     _b64(avatar_bytes),
        "active_frame":     active_frame,
        "active_banner":    active_banner,
        "active_background":active_background,
    }
    return _post("/profile", payload)


def generate_leaderboard_card(
    players: list,
    title: str = "ЛУЧШИЕ ИГРОКИ",
    avatars: Optional[dict] = None,
) -> io.BytesIO:
    payload = {
        "players": players,
        "title":   title,
        "avatars": _avatars_to_b64(avatars),
    }
    return _post("/leaderboard", payload)


def generate_duo_leaderboard_card(
    players: list,
    title: str = "2v2 ТОП",
    avatars: Optional[dict] = None,
) -> io.BytesIO:
    payload = {
        "players": players,
        "title":   title,
        "avatars": _avatars_to_b64(avatars),
    }
    return _post("/duo_leaderboard", payload)


def generate_match_result_card(
    match_code: str = "",
    map_name: str = "",
    winner: str = "ct",
    score_w: int = 0,
    score_l: int = 0,
    players_ct: Optional[list] = None,
    players_t: Optional[list] = None,
    league: str = "Default",
    avatars: Optional[dict] = None,
) -> io.BytesIO:
    payload = {
        "match_code": match_code,
        "map_name":   map_name,
        "winner":     winner,
        "score_w":    score_w,
        "score_l":    score_l,
        "players_ct": players_ct,
        "players_t":  players_t,
        "league":     league,
        "avatars":    _avatars_to_b64(avatars),
    }
    return _post("/match_result", payload)
