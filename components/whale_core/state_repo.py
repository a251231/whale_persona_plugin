"""Persistent state repository: secrets cache, state file load/save with ``_save_seq`` selection,
debounced atomic writes, session locks/tasks and bounded LRU runtime caches.

Extracted from ``components/event_listeners/whale_listener.py`` / ``diana_features.py``
(v0.5.2 A-12). Those modules remain the public compatibility facades: tests and callers keep
patching ``whale_listener`` / ``diana_features`` attributes, which the facades propagate here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import threading
import time
from datetime import datetime
from typing import Any, Coroutine

import httpx

from langbot_plugin.api.entities import context

from . import ui  # live facade proxies (see whale_core.__init__)
from .http_gateway import (
    TZ,
)
from .mood_memory import (
    DIMS,
    _clamp,
    _dedup_facts_in_place,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_API_KEY",
    "DEFAULT_BASE_URL",
    "DEFAULT_BOT_UUID",
    "DEFAULT_MODEL",
    "DEFAULT_OWNER_IDS",
    "DEFAULT_SF_API_KEY",
    "SECRETS_BACKUP_FILE",
    "SECRETS_FILE",
    "STATE_BACKUP_FILE",
    "STATE_FILE",
    "StateRepoMixin",
]


# ==============================================================================
# from whale_listener.py
# ==============================================================================
SECRETS_FILE = os.getenv("WHALE_SECRETS_FILE", "/app/data/whale_secrets.json")
SECRETS_BACKUP_FILE = os.getenv("WHALE_SECRETS_BACKUP_FILE", "/app/data/plugins/whale_secrets.json")
_SECRETS_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "paths": (),
    "mtime": None,
    "data": {},
}


def _load_local_secrets() -> dict[str, str]:
    now = time.time()
    paths = (SECRETS_FILE, SECRETS_BACKUP_FILE)
    if _SECRETS_CACHE["paths"] == paths and (now - float(_SECRETS_CACHE["ts"])) < 60.0:
        return dict(_SECRETS_CACHE["data"])
    sig_items: list[tuple[str, float | None]] = []
    for candidate in paths:
        try:
            st = os.stat(candidate) if candidate else None
            sig_items.append((candidate, st.st_mtime if st else None))
        except OSError:
            sig_items.append((candidate, None))
    sig_tuple = tuple(sig_items)
    if (
        _SECRETS_CACHE["paths"] == paths
        and _SECRETS_CACHE["mtime"] == sig_tuple
        and float(_SECRETS_CACHE["ts"]) > 0.0
    ):
        _SECRETS_CACHE["ts"] = now
        return dict(_SECRETS_CACHE["data"])
    loaded: dict[str, str] = {}
    for candidate in paths:
        try:
            if candidate and os.path.exists(candidate):
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and data:
                        loaded = {str(k): str(v) for k, v in data.items() if v}
                        break
        except Exception:
            pass
    _SECRETS_CACHE["ts"] = now
    _SECRETS_CACHE["paths"] = paths
    _SECRETS_CACHE["mtime"] = sig_tuple
    _SECRETS_CACHE["data"] = loaded
    return dict(loaded)
_LOCAL_SECRETS = _load_local_secrets()
DEFAULT_API_KEY = os.getenv("DEEPSEEK_API_KEY", "") or _LOCAL_SECRETS.get("deepseek_api_key", "")
DEFAULT_SF_API_KEY = os.getenv("SILICONFLOW_API_KEY", "") or _LOCAL_SECRETS.get("siliconflow_api_key", "")
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BOT_UUID = os.getenv("TARGET_BOT_UUID", "") or _LOCAL_SECRETS.get("target_bot_uuid", "")
DEFAULT_OWNER_IDS = os.getenv("OWNER_IDS", "") or _LOCAL_SECRETS.get("owner_ids", "")
STATE_FILE = os.getenv("WHALE_STATE_FILE", "/app/data/whale_persona_state.json")
STATE_BACKUP_FILE = os.getenv("WHALE_STATE_BACKUP_FILE", "/app/data/plugins/whale_persona_state.json")


def _default_user_state() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    return {
        "mood": 72,
        "dims": {k: (15 if k in ("happy", "calm") else 0) for k, _ in DIMS},
        "lastReason": "刚搬进微信，心情放松踏实",
        "lastConfidence": 85,
        "lastNotedMood": 72,
        "kind": "",
        "updatedAt": now_ms,
        "lastDecayAt": now_ms,
        "timeline": [],
        "facts": [],
        "events": [],
        "archive": {},
        "factsSinceArchive": 0,
        "lastChatAt": now_ms,
        "lastSpeakUpAt": 0,
        "speakUpCountSinceReply": 0,
        "missStreak": 0,
        "hasPrivateChat": False,
    }


def _default_group_state() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    return {
        "mood": 75,
        "lastReason": "在群里潜水看大家聊天，气氛挺热闹",
        "updatedAt": now_ms,
        "lastDecayAt": now_ms,
        "facts": [],
        "events": [],
        "member_impressions": {},
        "lastChatAt": now_ms,
        "turnsSinceMeme": 99,
        "lastMemeAt": 0,
    }


class StateRepoMixin:
    """``WhaleListener`` methods for state repo (mixed into the listener class)."""

    @classmethod
    def _lru_set(cls, cache: dict[Any, Any], key: Any, value: Any, maxsize: int = 512) -> Any:
        if key in cache:
            cache.pop(key, None)
        cache[key] = value
        while len(cache) > maxsize:
            cache.pop(next(iter(cache)), None)
        return value

    @staticmethod
    def _lru_touch(cache: dict[Any, Any], key: Any) -> None:
        if key in cache:
            cache[key] = cache.pop(key)

    @classmethod
    def _trim_cache_lru(cls, cache: dict[Any, Any], maxsize: int = 512) -> None:
        while len(cache) > maxsize:
            cache.pop(next(iter(cache)), None)

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_key, asyncio.Lock())

    def _start_session_task(self, session_key: str, coroutine: Coroutine[Any, Any, Any]) -> None:
        tasks = self._session_tasks.setdefault(session_key, set())
        task = asyncio.create_task(coroutine)
        tasks.add(task)

        def _cleanup_done(done_task: asyncio.Task[Any]) -> None:
            cur = self._session_tasks.get(session_key)
            if cur is not None:
                cur.discard(done_task)
                if not cur:
                    self._session_tasks.pop(session_key, None)

        task.add_done_callback(_cleanup_done)

    def _track_bg_task(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    @staticmethod
    def _is_valid_wechat_uid(uid: str) -> bool:
        s = (uid or "").strip()
        return bool(s and len(s) <= 127 and s.isascii() and not any(c.isspace() for c in s))

    def _load_persistent_state(self) -> dict[str, Any]:
        best_data: dict[str, Any] | None = None
        best_score: tuple[int, int] = (-1, -1)
        for idx, candidate in enumerate((STATE_FILE, STATE_BACKUP_FILE)):
            try:
                if candidate and os.path.exists(candidate):
                    with open(candidate, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict):
                            try:
                                seq = int(data.get("_save_seq", 0) or 0)
                            except (TypeError, ValueError):
                                seq = 0
                            score = (seq, 1 if idx == 0 else 0)
                            if best_data is None or score > best_score:
                                best_data = data
                                best_score = score
            except Exception as e:
                logger.warning("Failed to load whale state (%s): %s", candidate, e)

        if best_data is not None:
            data = best_data
            data.setdefault("meme_enabled", True)
            data.setdefault("moments", [])
            data.setdefault("enabled_groups", [])
            data.setdefault("groups", {})
            data.setdefault("repeat_enabled", True)
            data.setdefault("repeat_config", {"min_users": 2, "prob": 0.30, "cooldown_sec": 60})
            data.setdefault("group_interject_enabled", True)
            data.setdefault("interject_logs", [])
            data.setdefault("steal_meme_enabled", True)
            data.setdefault("web_search_enabled", True)
            data.setdefault("voice_reply_enabled", True)
            data.setdefault("auto_moments_enabled", False)
            data.setdefault("moments_auto_interact_enabled", False)
            data.setdefault("auto_accept_friend_enabled", True)
            data.setdefault("friend_verify_keywords", [])
            data.setdefault("accepted_friend_requests", {})
            data.setdefault("interacted_moments", {})
            data.setdefault("replied_moment_comments", {})
            data.setdefault("self_moments_history", list(data.get("moments") or [])[-30:])
            data.setdefault("_save_seq", 0)
            users_map = data.get("users") or {}
            for uid in list(users_map.keys()):
                u_obj = users_map[uid]
                if not isinstance(u_obj, dict) or not self._is_valid_wechat_uid(str(uid)):
                    users_map.pop(uid, None)
                    continue
                u_obj.setdefault("speakUpCountSinceReply", 0)
                if u_obj.get("facts"):
                    u_obj.setdefault("hasPrivateChat", True)
                    raw_facts_in = [
                        str(x.get("text") or x.get("content") or x.get("fact") or "").strip()
                        if isinstance(x, dict)
                        else str(x or "").strip()
                        for x in (u_obj.get("facts") or [])
                    ]
                    u_obj["facts"] = _dedup_facts_in_place([x for x in raw_facts_in if x])
                elif not u_obj.get("hasPrivateChat", False):
                    # Clean up non-private group-only user stubs
                    users_map.pop(uid, None)
                    continue
                raw_u_dims = u_obj.get("dims")
                if isinstance(raw_u_dims, dict):
                    for dim_k, _ in DIMS:
                        try:
                            raw_u_dims[dim_k] = int(
                                _clamp(round(float(raw_u_dims.get(dim_k, 0) or 0)), 0, 100)
                            )
                        except (TypeError, ValueError):
                            raw_u_dims[dim_k] = 0
                else:
                    u_obj["dims"] = {dim_k: (15 if dim_k in ("happy", "calm") else 0) for dim_k, _ in DIMS}
            for gid in data.get("enabled_groups", []):
                if gid not in data["groups"]:
                    data["groups"][gid] = _default_group_state()
            return data

        return {
            "enabled": True,
            "mode": "live",
            "speak_up_enabled": True,
            "meme_enabled": True,
            "enabled_groups": [],
            "groups": {},
            "repeat_enabled": True,
            "repeat_config": {"min_users": 2, "prob": 0.30, "cooldown_sec": 60},
            "group_interject_enabled": True,
            "interject_logs": [],
            "steal_meme_enabled": True,
            "web_search_enabled": True,
            "voice_reply_enabled": True,
            "auto_moments_enabled": False,
            "moments_auto_interact_enabled": False,
            "auto_accept_friend_enabled": True,
            "friend_verify_keywords": [],
            "accepted_friend_requests": {},
            "moments": [],
            "self_moments_history": [],
            "interacted_moments": {},
            "replied_moment_comments": {},
            "token_stats": {"date": "", "today_tokens": 0, "last_tokens": 0, "total_tokens": 0},
            "_save_seq": 0,
            "users": {},
        }

    def _save_persistent_state(self) -> None:
        io_lock = getattr(self, "_state_io_lock", None)
        if io_lock is None:
            io_lock = threading.Lock()
            self._state_io_lock = io_lock
        with io_lock:
            self._state["_save_seq"] = int(self._state.get("_save_seq", 0) or 0) + 1
            payload_str = ""
            for attempt in range(4):
                try:
                    payload_str = json.dumps(self._state, ensure_ascii=False, indent=2)
                    break
                except RuntimeError:
                    if attempt == 3:
                        raise
                    time.sleep(0.005)
            if not payload_str:
                return
            for target_path in (STATE_FILE, STATE_BACKUP_FILE):
                if not target_path:
                    continue
                if target_path == STATE_BACKUP_FILE and not os.path.isdir(os.path.dirname(target_path)):
                    continue
                tmp = ""
                try:
                    parent = os.path.dirname(target_path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    tmp = f"{target_path}.{os.getpid()}.{threading.get_ident()}.{random.randint(10000, 99999)}.tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.write(payload_str)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, target_path)
                    tmp = ""
                except Exception as e:
                    logger.warning("Failed to save whale state (%s): %s", target_path, e)
                finally:
                    if tmp and os.path.exists(tmp):
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass

    async def _flush_state_async(self) -> None:
        try:
            while self._state_dirty:
                self._state_dirty = False
                self._save_need_recheck = False
                if "_save_persistent_state" in self.__dict__:
                    self._save_persistent_state()
                else:
                    await asyncio.to_thread(self._save_persistent_state)
                if not self._save_need_recheck:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Async persistent state flush failed: %s", e)

    def _on_save_debounce_timer(self) -> None:
        self._save_debounce_handle = None
        if not self._state_dirty:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._state_dirty = False
            self._save_persistent_state()
            return
        if self._save_flush_task is None or self._save_flush_task.done():
            self._save_flush_task = loop.create_task(self._flush_state_async())
        else:
            self._save_need_recheck = True

    def _mark_state_dirty(self, *, immediate: bool = False) -> None:
        self._state_dirty = True
        if "_save_persistent_state" in self.__dict__:
            self._state_dirty = False
            self._save_persistent_state()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._state_dirty = False
            self._save_persistent_state()
            return
        if immediate:
            if self._save_debounce_handle is not None:
                self._save_debounce_handle.cancel()
                self._save_debounce_handle = None
            if self._save_flush_task is None or self._save_flush_task.done():
                self._save_flush_task = loop.create_task(self._flush_state_async())
            else:
                self._save_need_recheck = True
            return
        if self._save_debounce_handle is None:
            self._save_debounce_handle = loop.call_later(1.5, self._on_save_debounce_timer)

    def _get_user_state(self, user_id: str) -> dict[str, Any]:
        users = self._state.setdefault("users", {})
        if user_id not in users or not isinstance(users[user_id], dict):
            users[user_id] = _default_user_state()
        u = users[user_id]
        u.setdefault("archive", {})
        u.setdefault("factsSinceArchive", 0)
        u.setdefault("speakUpCountSinceReply", 0)
        raw_dims = u.get("dims")
        if not isinstance(raw_dims, dict):
            u["dims"] = {k: (15 if k in ("happy", "calm") else 0) for k, _ in DIMS}
        else:
            for k, _ in DIMS:
                raw_dims.setdefault(k, 0)
        self._apply_mood_decay(u)
        return u

    def _settings_for(self, target_id: str, *, is_group: bool = False) -> dict[str, Any]:
        return ui.effective_settings(self._state, ui.scope_key(is_group, target_id))

    def _get_group_state(self, group_id: str) -> dict[str, Any]:
        groups = self._state.setdefault("groups", {})
        if group_id not in groups or not isinstance(groups[group_id], dict):
            groups[group_id] = _default_group_state()
        g = groups[group_id]
        g.setdefault("facts", [])
        g.setdefault("events", [])
        g.setdefault("member_impressions", {})
        now_ms = int(time.time() * 1000)
        last_decay = int(g.get("lastDecayAt") or now_ms)
        age_min = max(0.0, (now_ms - last_decay) / 60000.0)
        if age_min >= 5.0:
            km = 1.0 - math.exp(-(math.log(2.0) * age_min) / (12.0 * 60.0))
            cur_mood = float(g.get("mood", 75))
            g["mood"] = int(round(cur_mood + (75.0 - cur_mood) * km))
            g["lastDecayAt"] = now_ms
        return g

    async def _get_balance_str(self, api_key: str, base_url: str) -> str:
        if not api_key:
            return "未配置"
        now = time.time()
        cache_key = (api_key[:8], base_url.rstrip("/"))
        cached_ts, cached_val = self._balance_cache
        if getattr(self, "_balance_cache_key", None) == cache_key and cached_val and (now - cached_ts) < 300:
            return cached_val
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    f"{base_url}/user/balance",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if r.status_code == 200:
                    b_info = (r.json().get("balance_infos") or [{}])[0]
                    bal = b_info.get("total_balance")
                    cur = b_info.get("currency", "CNY")
                    if bal is not None:
                        symbol = "¥" if cur == "CNY" else f"{cur} "
                        val = f"{symbol}{bal}"
                        self._balance_cache = (now, val)
                        self._balance_cache_key = cache_key
                        return val
        except Exception:
            pass
        neg_val = cached_val if (getattr(self, "_balance_cache_key", None) == cache_key and cached_val) else "未知"
        self._balance_cache = (now, neg_val)
        self._balance_cache_key = cache_key
        return neg_val

    def _record_tokens(self, used_tokens: int) -> dict[str, Any]:
        today_str = datetime.now(TZ).strftime("%Y-%m-%d")
        stats = self._state.setdefault(
            "token_stats",
            {"date": today_str, "today_tokens": 0, "last_tokens": 0, "total_tokens": 0},
        )
        if stats.get("date") != today_str:
            stats["date"] = today_str
            stats["today_tokens"] = 0
        if used_tokens > 0:
            stats["last_tokens"] = used_tokens
            stats["today_tokens"] = int(stats.get("today_tokens", 0)) + used_tokens
            stats["total_tokens"] = int(stats.get("total_tokens", 0)) + used_tokens
        return stats

    async def _record_tokens_safe(self, used_tokens: int) -> dict[str, Any]:
        async with self._state_lock:
            return dict(self._record_tokens(used_tokens))

    def _get_config_str(self, key: str, default: str) -> str:
        try:
            self._clear_ended_invocation()
            cfg = self.plugin.get_config() or {}
            val = str(cfg.get(key, "") or "").strip()
            if val:
                return val
        except Exception:
            pass
        if default:
            return default
        return str(_load_local_secrets().get(key, "") or "").strip()

    def _get_config_int(self, key: str, default: int) -> int:
        try:
            self._clear_ended_invocation()
            cfg = self.plugin.get_config() or {}
            return int(cfg.get(key, default))
        except Exception:
            return default

    async def _resolve_bot_uuid(self, event_ctx: context.EventContext) -> str:
        target_bot_uuid = self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID)
        launcher_id = str(getattr(event_ctx.event, "launcher_id", "") or "")
        sender_id = str(getattr(event_ctx.event, "sender_id", "") or "")
        if target_bot_uuid and (
            launcher_id.startswith(("oc_", "ou_", "on_"))
            or sender_id.startswith(("ou_", "on_"))
        ):
            return "feishu_ignored"
        try:
            bot_uuid = await event_ctx.get_bot_uuid()
            if bot_uuid:
                return str(bot_uuid)
        except Exception:
            pass
        query = getattr(event_ctx.event, "query", None)
        return str(getattr(query, "bot_uuid", "") or "")
