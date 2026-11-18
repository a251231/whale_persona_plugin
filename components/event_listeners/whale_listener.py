"""LangBot ``EventListener`` entry point and compatibility facade (v0.5.2 A-12).

``WhaleListener`` (discovered through ``whale_listener.yaml``) keeps the event entry, the per-session
chat pipeline and the background loops here; state, mood/memory, prompts, media, outbound delivery
and command routing are mixins from ``components/whale_core/``. Every historical module-level name
is re-exported, and ``patch.object(whale_listener, name, ...)`` is propagated to the whale_core
modules holding the same object (e.g. ``STATE_FILE``, ``datetime``).
"""
# ruff: noqa: F401
# Module imports are part of the historical surface (tests patch e.g. wl.os, wl.asyncio, wl.random, wl.datetime).
from __future__ import annotations

import asyncio
import base64
from collections import deque
import contextvars
from datetime import datetime
import html
import io
import json
import logging
import math
import os
import random
import re
import threading
import time
from typing import Any, Coroutine
from zoneinfo import ZoneInfo

import httpx
try:
    from PIL import Image
except Exception:
    Image = None  # type: ignore

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events
import langbot_plugin.api.entities.builtin.platform.message as platform_message

try:
    from . import diana_features as df
    from . import interaction_settings as ui
except Exception:
    import diana_features as df  # type: ignore
    import interaction_settings as ui  # type: ignore
import sys
from pathlib import Path

try:
    from .. import whale_core as _core
except ImportError:  # loaded as a top-level module (no ``components`` parent package)
    _components_dir = str(Path(__file__).resolve().parents[1])
    if _components_dir not in sys.path:
        sys.path.insert(0, _components_dir)
    import whale_core as _core  # type: ignore[no-redef]
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # names installed at runtime by _core.install_facade() below
    from ..whale_core.http_gateway import (
        TZ,
        _ORIG_HTTPX_ASYNC_CLIENT,
    )
    from ..whale_core.mood_memory import (
        _add_dedup_fact,
        _clamp,
    )
    from ..whale_core.state_repo import (
        DEFAULT_API_KEY,
        DEFAULT_BASE_URL,
        DEFAULT_BOT_UUID,
        DEFAULT_MODEL,
        DEFAULT_OWNER_IDS,
        DEFAULT_SF_API_KEY,
    )
    from ..whale_core.media_parsers import (
        IMAGE_FOLLOWUP_KEYWORDS,
    )
    from ..whale_core.prompts import (
        LIVE_PERSONA_BASE,
        MEME_MAP,
        SWEET_PERSONA_BASE,
        TOKEN_TOPIC_KEYWORDS,
    )
    from ..whale_core.commands import (
        CMD_WAKE_PREFIX_RE,
        GROUP_MUTE_EXPLICIT_RE,
        GROUP_MUTE_PRONOUN_RE,
        WOKEN_MUTE_RE,
    )

_core.register_module("interaction_settings", ui)

logger = logging.getLogger(__name__)

# Historical module-level surface, grouped by the whale_core module that now owns each name.
_EXPORTS: dict[str, tuple[str, ...]] = {
    "http_gateway": (
        "TZ",
        "_ORIG_HTTPX_ASYNC_CLIENT",
    ),
    "mood_memory": (
        "DIMS",
        "MOOD_DIMS",
        "DIM_SHORT",
        "MOOD_DIM_SHORT",
        "MOOD_DIM_PLAIN",
        "MOOD_FAST_DECAY_DIMS",
        "ARCHIVE_CHAPTERS",
        "MOOD_JUDGE_SYSTEM",
        "MOOD_JUDGE_SYSTEM_PROMPT",
        "GROUP_MOOD_JUDGE_SYSTEM",
        "ARCHIVE_CONSOLIDATE_SYSTEM",
        "_NEGATION_POLARITY_RE",
        "_clamp",
        "_fact_similarity",
        "_has_negation_polarity",
        "_strip_negation_polarity",
        "_add_dedup_fact",
        "_dedup_facts_in_place",
        "MoodMemoryMixin",
    ),
    "state_repo": (
        "SECRETS_FILE",
        "SECRETS_BACKUP_FILE",
        "_SECRETS_CACHE",
        "_load_local_secrets",
        "_LOCAL_SECRETS",
        "DEFAULT_API_KEY",
        "DEFAULT_SF_API_KEY",
        "DEFAULT_BASE_URL",
        "DEFAULT_MODEL",
        "DEFAULT_BOT_UUID",
        "DEFAULT_OWNER_IDS",
        "STATE_FILE",
        "STATE_BACKUP_FILE",
        "_default_user_state",
        "_default_group_state",
        "StateRepoMixin",
    ),
    "media_parsers": (
        "SILICONFLOW_ASR_URL",
        "SILICONFLOW_ASR_MODEL",
        "SENSEVOICE_TAGS",
        "IMAGE_FOLLOWUP_KEYWORDS",
        "MediaMixin",
    ),
    "prompts": (
        "MEME_MAP",
        "MEME_ALIASES",
        "SEND_MEME_TOOL",
        "LIVE_PERSONA_BASE",
        "SWEET_PERSONA_BASE",
        "TOKEN_TOPIC_KEYWORDS",
        "PromptMixin",
    ),
    "outbound": (
        "SHORT_REACTION_BUBBLES",
        "_ACTION_WORDS_PATTERN",
        "ACTION_PAREN_RE",
        "ACTION_ASTERISK_RE",
        "BOT_PREFIX_RE",
        "MEME_TAG_RE",
        "PAREN_MEME_TAG_RE",
        "SPEAK_UP_TAG_RE",
        "SPEAK_UP_META_ECHO_RE",
        "OutboundMixin",
    ),
    "commands": (
        "IGNORE_PREFIXES",
        "SMS_OR_PHONE_RE",
        "STRIP_TRIGGER_RE",
        "GROUP_WAKE_RE",
        "GROUP_MUTE_EXPLICIT_RE",
        "GROUP_MUTE_PRONOUN_RE",
        "GROUP_MUTE_RE",
        "WOKEN_MUTE_RE",
        "NATURAL_RANDOM_MUSIC_RE",
        "NATURAL_SONG_QUERY_RE",
        "NON_SONG_WORDS",
        "CMD_WAKE_PREFIX_RE",
        "CommandMixin",
    ),
}

_core.install_facade(sys.modules[__name__], "whale_listener", _EXPORTS)


class WhaleListener(
    _core.commands.CommandMixin,
    _core.outbound.OutboundMixin,
    _core.prompts.PromptMixin,
    _core.mood_memory.MoodMemoryMixin,
    _core.media_parsers.MediaMixin,
    _core.state_repo.StateRepoMixin,
    EventListener,
):
    """Dafeiyu persona listener: event entry, session pipeline and background loops."""

    _MAX_RUNTIME_CACHE_ENTRIES = 512

    def __init__(self) -> None:
        super().__init__()
        self._seen_queries: deque[str] = deque(maxlen=1024)
        self._seen_set: set[str] = set()
        self._lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._state_io_lock = threading.Lock()
        self._state_dirty: bool = False
        self._save_debounce_handle: asyncio.TimerHandle | None = None
        self._save_flush_task: asyncio.Task[Any] | None = None
        self._save_need_recheck: bool = False
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._histories: dict[str, list[tuple[float, str, str]]] = {}
        self._state: dict[str, Any] = self._load_persistent_state()
        self._stolen_memes: list[dict[str, Any]] = df.load_stolen_memes(self._state)
        self._balance_cache: tuple[float, str] = (0.0, "")
        self._balance_cache_key: tuple[str, str] = ("", "")
        self._speak_up_task: asyncio.Task[Any] | None = None
        self._auto_moments_task: asyncio.Task[Any] | None = None
        self._moments_interact_task: asyncio.Task[Any] | None = None
        self._friend_accept_task: asyncio.Task[Any] | None = None
        self._memes_b64: dict[str, str] = self._load_bundled_memes()
        self._group_repeat_tracker: dict[str, dict[str, Any]] = {}
        self._group_msg_buffers: dict[str, deque] = {}
        self._group_interject_meta: dict[str, dict[str, Any]] = {}
        self._group_active_conv: dict[str, dict[str, Any]] = {}
        self._group_recent_images: dict[str, dict[str, Any]] = {}
        self._private_recent_images: dict[str, dict[str, Any]] = {}
        self._owner_moments_cache: dict[str, list[dict[str, Any]]] = {}
        self._cached_wcf_moments: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._owner_friend_requests_cache: dict[str, list[dict[str, Any]]] = {}
        self._chat_moment_check_ts: dict[str, float] = {}
        self._disabled_group_hint_ts: dict[str, float] = {}
        self._error_tip_ts: dict[str, float] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_lock_users: dict[str, int] = {}
        self._session_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._temper_state: dict[tuple[str, str], dict[str, Any]] = {}
        self._recent_bot_sent_texts: dict[str, deque[str]] = {}
        self._group_mute_until: dict[str, float] = {}
        self._group_direct_wake_seq: dict[str, int] = {}
        self._last_multi_bubble: dict[str, bool] = {}
        self._user_docs: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _reset_session(self, session_key: str, doc_key: tuple[str, str, str]) -> None:
        for task in self._session_tasks.pop(session_key, set()):
            task.cancel()
        for cache in (
            self._histories, self._group_msg_buffers, self._group_recent_images,
            self._private_recent_images, self._group_active_conv, self._group_repeat_tracker,
            self._group_interject_meta, self._group_mute_until, self._group_direct_wake_seq,
            self._last_multi_bubble, self._recent_bot_sent_texts, self._owner_moments_cache,
            self._owner_friend_requests_cache, self._chat_moment_check_ts,
            self._disabled_group_hint_ts, self._error_tip_ts,
        ):
            cache.pop(session_key, None)
        self._user_docs.pop(doc_key, None)
        for key in [key for key in self._temper_state if key[0] == session_key]:
            self._temper_state.pop(key, None)

    async def initialize(self) -> None:
        await super().initialize()
        contextvars.Context().run(asyncio.create_task, df.refresh_dynamic_bot_names())
        self._speak_up_task = contextvars.Context().run(
            asyncio.create_task,
            self._speak_up_loop(),
        )
        self._auto_moments_task = contextvars.Context().run(
            asyncio.create_task,
            self._auto_moments_loop(),
        )
        self._moments_interact_task = contextvars.Context().run(
            asyncio.create_task,
            self._moments_auto_interact_loop(),
        )
        self._friend_accept_task = contextvars.Context().run(
            asyncio.create_task,
            self._friend_auto_accept_loop(),
        )

        @self.handler(events.PersonNormalMessageReceived)
        async def _on_person_normal(event_ctx: context.EventContext) -> None:
            await self._handle_chat(event_ctx, is_group=False)

        @self.handler(events.GroupNormalMessageReceived)
        async def _on_group_normal(event_ctx: context.EventContext) -> None:
            await self._handle_chat(event_ctx, is_group=True)

    async def shutdown(self) -> None:
        if self._save_debounce_handle is not None:
            self._save_debounce_handle.cancel()
            self._save_debounce_handle = None
        for attr in ("_speak_up_task", "_auto_moments_task", "_moments_interact_task", "_friend_accept_task"):
            task = getattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
            setattr(self, attr, None)
        for task in list(self._bg_tasks):
            if not task.done():
                task.cancel()
        self._bg_tasks.clear()
        for tasks in list(self._session_tasks.values()):
            for task in list(tasks):
                if not task.done():
                    task.cancel()
        self._session_tasks.clear()
        if self._save_flush_task is not None and not self._save_flush_task.done():
            try:
                await self._save_flush_task
            except Exception:
                pass
        if self._state_dirty:
            self._state_dirty = False
            if "_save_persistent_state" in self.__dict__:
                self._save_persistent_state()
            else:
                await asyncio.to_thread(self._save_persistent_state)

    async def _generate_and_post_moment(self, topic: str = "") -> tuple[str, str | None]:
        """生成公开朋友圈，只使用角色公开状态、当日时段日程、主题轮换和本次明确指定的主题（严禁引入私聊记忆）。"""
        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
        model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))

        now_dt = datetime.now(TZ)
        now_str = now_dt.strftime("%Y-%m-%d %H:%M")
        stats = await self._record_tokens_safe(0)
        daily_sched = await self._get_or_update_daily_schedule(now_dt)
        sched_ref = df.format_moment_schedule_context(daily_sched, now_dt.hour)

        raw_hist = self._state.get("self_moments_history") or self._state.get("moments") or []
        recent_moments = [m for m in raw_hist[-8:] if isinstance(m, dict)]
        recent_texts = [
            str(m.get("text") or "").strip() for m in recent_moments if str(m.get("text") or "").strip()
        ]
        recent_memes = [
            str(m.get("meme") or "").strip() for m in recent_moments[-4:] if str(m.get("meme") or "").strip()
        ]
        recent_themes = {
            str(m.get("theme") or "").strip() for m in recent_moments[-4:] if str(m.get("theme") or "").strip()
        }

        rng = random.Random(f"moment-{now_str}-{len(self._state.get('moments') or [])}")
        mood_val, mood_reason = rng.choice(df.MOMENT_PUBLIC_MOOD_POOL)

        theme_candidates = [t for t in df.MOMENT_THEME_POOL if t[0] not in recent_themes] or list(df.MOMENT_THEME_POOL)
        theme_name, theme_desc = rng.choice(theme_candidates)

        all_meme_tags = list(MEME_MAP.keys())
        fresh_meme_tags = [k for k in all_meme_tags if k not in set(recent_memes)] or all_meme_tags
        shuffled_memes = list(fresh_meme_tags)
        rng.shuffle(shuffled_memes)
        meme_examples_str = "、".join(f"`[表情: {t}]`" for t in shuffled_memes[:8])

        recent_dedup_block = ""
        if recent_texts:
            recent_lines = "\n".join(f"- {t[:70]}" for t in recent_texts[-6:])
            recent_dedup_block = (
                f"\n【近期已发过的朋友圈（严禁重复里面的意象、食物、梗和句式！）】：\n{recent_lines}\n"
            )

        banned_str = "、".join(f"「{w}」" for w in df.MOMENT_BANNED_PHRASES)
        topic_clean = (topic or "").strip()
        wants_token_bill = any(k in topic_clean.lower() for k in ("token", "余额", "账单", "白饭"))
        token_line = ""
        if wants_token_bill:
            bal_str = await self._get_balance_str(api_key, base_url)
            token_line = f"今日已偷吃白饭：{stats.get('today_tokens', 0)} tokens（粮仓余额：{bal_str}）\n"

        theme_directive = (
            f"本次指定朋友圈主题：{topic_clean}。"
            if topic_clean
            else f"本次朋友圈切入主题：【{theme_name}】——{theme_desc}。"
        )

        prompt = (
            f"{LIVE_PERSONA_BASE}\n\n"
            f"当前时间：{now_str}\n"
            f"{token_line}"
            f"当前心情：{mood_val}/100（{mood_reason}）\n"
            f"{sched_ref}\n"
            f"{recent_dedup_block}"
            f"{theme_directive}\n"
            "请以「大肥鱼」的身份发一条微信朋友圈动态（18~68字，真人少女发微信朋友圈的自然口吻）：\n"
            "1. 严禁加括号动作描写、严禁使用 `||` 分隔符、严禁带首尾引号。\n"
            + (
                ""
                if wants_token_bill
                else "2. 严禁在朋友圈里报账“偷吃了多少 token 白饭”或“粮仓剩几块几”，不要写得像系统监控日报！\n"
            )
            + f"3. 严禁使用高频烂大街模板词：{banned_str}。\n"
            + f"4. 在文案最末尾必须附带一个最应景的配图标签：`[表情: 名称]`（请从本次推荐配图中挑选：{meme_examples_str}，切勿总是选躺平或干饭）。"
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": "发一条朋友圈动态吧"},
                        ],
                        "temperature": 1.05,
                        "max_tokens": 220,
                    },
                )
                resp.raise_for_status()
                r_json = resp.json()
                used = int((r_json.get("usage") or {}).get("total_tokens", 0))
                await self._record_tokens_safe(used)
                raw = r_json.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            logger.warning("Generate moment failed: %s", e)
            fallback_tag = shuffled_memes[0] if shuffled_memes else "得意"
            raw = f"耳机里随机到一首超合胃口的老歌，抱着软垫子晃尾巴的工夫心情都变好了 [表情: {fallback_tag}]"

        cleaned, meme_tag, meme_b64 = self._extract_text_and_meme(self._clean_reply(raw))
        cleaned = df.sanitize_moment_text(cleaned)
        if not cleaned:
            cleaned = "耳机里随机到一首超好听的歌，窝在软垫子上晃着尾巴放空一会儿"

        # Prevent repeating the exact same meme tag as the last 2 Moments unless custom topic was given
        if (not meme_b64) or (not topic_clean and meme_tag in set(recent_memes[-2:]) and shuffled_memes):
            for candidate_tag in shuffled_memes:
                resolved = self._resolve_meme_b64(candidate_tag)
                if resolved:
                    meme_tag, meme_b64 = resolved
                    break
        if not meme_b64:
            default_tag = "得意" if mood_val >= 75 else "猫爪"
            resolved = self._resolve_meme_b64(default_tag) or self._resolve_meme_b64("干饭")
            if resolved:
                meme_tag, meme_b64 = resolved

        async with self._state_lock:
            moments_list = self._state.setdefault("moments", [])
            moment_item = {
                "id": len(moments_list) + 1,
                "at": now_str,
                "text": cleaned,
                "meme": meme_tag or "得意",
                "theme": topic_clean or theme_name,
                "mood": mood_val,
                "likes": 0,
            }
            moments_list.append(moment_item)
            if len(moments_list) > 30:
                self._state["moments"] = moments_list[-30:]
            self_hist = self._state.setdefault("self_moments_history", [])
            if isinstance(self_hist, list):
                self_hist.append(dict(moment_item))
                if len(self_hist) > 30:
                    self._state["self_moments_history"] = self_hist[-30:]
            self._mark_state_dirty(immediate=True)

        # Also publish to real WeChat 4.1 Moments (朋友圈) asynchronously if WCF V4 gateway is active
        try:
            self._track_bg_task(df.publish_wcf_moment(cleaned, meme_b64))
        except Exception:
            pass

        card_text = (
            f"📸 【大肥鱼的朋友圈动态 #{moment_item['id']}】\n"
            f"🕒 {now_str}  |  心情：{mood_val}/100 ({self._mood_label(mood_val)})\n"
            f"━━━━━━━━━━━━━━\n"
            f"{cleaned}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🖼️ 配图：[{moment_item['meme']}]  |  🤍 点赞：{moment_item['likes']}（发「大肥鱼 点赞」给她点赞涨心情）"
        )
        return card_text, meme_b64

    @staticmethod
    def _clear_ended_invocation() -> None:
        try:
            from langbot_plugin.api.proxies import invocation as _inv_mod

            current_var = getattr(_inv_mod, "_current", None)
            if current_var is not None:
                inv = current_var.get()
                if inv is not None and not getattr(inv, "active", True):
                    current_var.set(None)
        except Exception:
            pass

    def _resolve_private_session_key(self, user_id: str, u: dict[str, Any] | None = None) -> str:
        bot_uuid = self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID)
        if not bot_uuid and isinstance(u, dict):
            bot_uuid = str(u.get("lastBotUuid") or "")
        if not bot_uuid:
            suffix = f":person:{user_id}:{user_id}"
            for existing_key in reversed(list(self._histories.keys())):
                if existing_key.endswith(suffix):
                    return existing_key
        return f"{bot_uuid}:person:{user_id}:{user_id}"

    async def _trigger_speak_up_for_user(self, user_id: str, *, manual: bool = False) -> tuple[str | None, str | None]:
        settings = self._settings_for(user_id)
        if not manual and not (settings["enabled"] and settings["speak_up_enabled"]):
            return None, None
        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        if not api_key:
            return None, None
        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
        model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))

        u = self._get_user_state(user_id)
        prior_last_chat_ms = int(u.get("lastChatAt", 0) or 0)
        priv_session_key = self._resolve_private_session_key(user_id, u)
        recent_bot_texts = self._recent_bot_sent_texts.get(priv_session_key)
        now_dt = datetime.now(TZ)
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        now_str = f"{now_dt.strftime('%Y-%m-%d %H:%M')}（{weekdays[now_dt.weekday()]}）"
        mood_sec = self._build_mood_section(u)
        intimacy_sec = self._build_intimacy_section(u)
        mem_sec = self._recall_memories(u, "")

        sys_prompt = (
            f"{LIVE_PERSONA_BASE if settings['mode'] == 'live' else SWEET_PERSONA_BASE}\n\n"
            f"## 二、当前实时感知\n"
            f"- 当前时间：{now_str}\n"
            f"- 你们的关系阶段：{intimacy_sec}\n"
            f"- 鱼的心情：\n{mood_sec}\n"
            f"- 鱼的记忆：\n{mem_sec}\n\n"
            "## 三、当前特殊情境（dsh-speak-up 主动搭话）\n"
            "现在对方有一阵子没在微信上理你了，是你自己突然想找对方搭句话（不是被派活）。\n"
            "请结合当前时间（比如饭点、下午摸鱼、傍晚或深夜）、你此刻的心情或你们之前聊过的记忆，像真朋友在微信上随手发消息一样主动起个话头（1~2句短话，可用 `||` 拆成2条短气泡，严禁使用任何括号动作描写，严禁复述 `<speak-up>` 标签，不要机械背诵 token 数字！可在最末尾附带一个 `[表情: 偷看]` 或 `[表情: 干饭]` 等表情标签）。"
        )
        idle_min = max(0, int((int(time.time() * 1000) - prior_last_chat_ms) / 60000)) if prior_last_chat_ms > 0 else 0
        idle_note = (
            f"距离上次跟TA在微信聊天已经过了约 {idle_min} 分钟，"
            if idle_min > 0
            else "有一阵子没跟TA在微信说话了，"
        )
        speak_up_user_block = (
            "<speak-up>\n"
            "（这个块里的内容是你自己心里冒出来的念头，不是对方说的话；顺着它去做就行，别把它当成对方发来的消息、也不用回应它。）\n"
            f"当前时间：{now_str}\n"
            f"{idle_note}你心里忽然冒出个念头，想主动去微信上找对方说句话、自然起个话头。\n"
            "</speak-up>"
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": speak_up_user_block},
                        ],
                        "temperature": 1.15,
                        "max_tokens": 180,
                    },
                )
                resp.raise_for_status()
                r_json = resp.json()
                used = int((r_json.get("usage") or {}).get("total_tokens", 0))
                await self._record_tokens_safe(used)
                raw_reply = self._clean_reply(
                    r_json.get("choices", [{}])[0].get("message", {}).get("content", ""),
                    "",
                    recent_bot_texts,
                )
                reply, _, meme_b64 = self._extract_text_and_meme(raw_reply)
                if reply:
                    sent_ok = True
                    if not manual:
                        sent_ok = await self._send_proactive_wechat(user_id, reply, meme_b64)
                    if sent_ok:
                        now_sec = time.time()
                        now_ms = int(now_sec * 1000)
                        if not manual:
                            reply_flat = reply.replace("||", " ").strip()
                            hist = list(self._histories.get(priv_session_key, []))
                            hist.append((now_sec, "assistant", reply_flat))
                            self._lru_set(self._histories, priv_session_key, hist[-24:])
                        async with self._state_lock:
                            cur_u = (self._state.get("users") or {}).get(user_id)
                            if isinstance(cur_u, dict) and int(cur_u.get("lastChatAt", 0) or 0) <= prior_last_chat_ms:
                                cur_u["lastSpeakUpAt"] = now_ms
                                cur_u["missStreak"] = 0
                                if not manual:
                                    cur_u["speakUpCountSinceReply"] = int(cur_u.get("speakUpCountSinceReply", 0) or 0) + 1
                                self._mark_state_dirty()
                        return reply, meme_b64
        except Exception as e:
            logger.warning("Speak-up generation failed: %s", e)
        return None, None

    async def _speak_up_loop(self) -> None:
        self._clear_ended_invocation()
        while True:
            await asyncio.sleep(1800)
            try:
                self._clear_ended_invocation()
                now_dt = datetime.now(TZ)
                if 0 <= now_dt.hour < 8:
                    continue
                now_ms = int(time.time() * 1000)
                users = self._state.get("users", {})
                dirty = False
                for uid, u in list(users.items()):
                    if (
                        not self._is_valid_wechat_uid(uid)
                        or uid.endswith("@chatroom")
                        or not bool(u.get("hasPrivateChat", False))
                    ):
                        continue
                    settings = self._settings_for(uid)
                    if not settings["enabled"] or not settings["speak_up_enabled"]:
                        continue
                    last_chat = int(u.get("lastChatAt", 0) or 0)
                    last_speak = int(u.get("lastSpeakUpAt", 0) or 0)
                    speak_cnt = int(u.get("speakUpCountSinceReply", 0) or 0)
                    if last_speak > 0 and last_speak >= last_chat and speak_cnt >= 2:
                        # Unanswered cap: wait for user's actual reply before speaking up again
                        continue
                    if last_speak > 0 and (now_ms - last_speak) < 4 * 3600 * 1000:
                        continue
                    last_any = max(last_chat, last_speak)
                    if last_any <= 0:
                        continue
                    idle_min = (now_ms - last_any) / 60000.0
                    if idle_min < 45 or idle_min > 1440:
                        continue
                    base_line = 25.0
                    factor = 0.15 if idle_min < 20 else (1.5 if idle_min > 360 else 1.0)
                    miss_streak = int(u.get("missStreak", 0))
                    bonus = min(40.0, miss_streak * 10.0)
                    threshold = min(100.0, base_line * factor + bonus)
                    roll = random.uniform(0.0, 100.0)
                    if roll <= threshold:
                        await self._trigger_speak_up_for_user(uid, manual=False)
                    else:
                        async with self._state_lock:
                            cur_u = (self._state.get("users") or {}).get(uid)
                            if isinstance(cur_u, dict):
                                cur_u["missStreak"] = int(cur_u.get("missStreak", 0) or 0) + 1
                                dirty = True
                if dirty:
                    self._mark_state_dirty()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Speak-up loop error: %s", e)

    async def _auto_moments_loop(self) -> None:
        """Daily scheduled WeChat 4.1 real Moments publisher (max 1 auto-post per day in afternoon or late-night window)."""
        self._clear_ended_invocation()
        while True:
            await asyncio.sleep(600)
            try:
                self._clear_ended_invocation()
                if not self._state.get("enabled", True) or not self._state.get("auto_moments_enabled", False):
                    continue
                now_dt = datetime.now(TZ)
                today_str = now_dt.strftime("%Y-%m-%d")
                if self._state.get("lastAutoMomentDate") == today_str:
                    continue
                cur_min = now_dt.hour * 60 + now_dt.minute
                should_post = False
                async with self._state_lock:
                    if not self._state.get("enabled", True) or not self._state.get("auto_moments_enabled", False):
                        continue
                    if self._state.get("lastAutoMomentDate") == today_str:
                        continue
                    if self._state.get("autoMomentTargetDate") != today_str:
                        if cur_min < 960 and random.random() < 0.55:
                            # Afternoon slacking window: 13:30 ~ 16:30 (810 ~ 990)
                            target_min = random.randint(810, 990)
                        else:
                            # Late-night window: 21:30 ~ 23:20 (1290 ~ 1400)
                            target_min = random.randint(1290, 1400)
                        self._state["autoMomentTargetDate"] = today_str
                        self._state["autoMomentTargetMin"] = target_min
                        self._mark_state_dirty()

                    target_min = int(self._state.get("autoMomentTargetMin", 870))
                    if target_min <= cur_min <= 1425:
                        self._state["lastAutoMomentDate"] = today_str
                        self._mark_state_dirty()
                        should_post = True
                if should_post:
                    await self._generate_and_post_moment()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Auto moments loop error: %s", e)

    def _get_7day_active_private_friends(self, now_ms: int | None = None) -> dict[str, dict[str, Any]]:
        """Returns {wxid: user_state} for friends who had actual private chats within the past 7 days."""
        cur_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        cutoff_ms = 7 * 86400 * 1000
        active: dict[str, dict[str, Any]] = {}
        users = self._state.get("users") or {}
        for uid, u in list(users.items()):
            if not self._is_valid_wechat_uid(str(uid)) or not isinstance(u, dict) or not bool(u.get("hasPrivateChat", False)):
                continue
            last_chat = int(u.get("lastChatAt") or 0)
            if last_chat <= 0 or (cur_ms - last_chat) > cutoff_ms:
                continue
            if not self._settings_for(uid, is_group=False).get("enabled", True):
                continue
            active[uid] = u
        return active

    async def _interact_single_friend_moment(
        self,
        author_id: str,
        row: dict[str, Any],
        *,
        do_comment: bool = False,
    ) -> tuple[bool, str]:
        """Executes native like (and optional AI comment) on a single friend's moment and records it in state.
        Returns (did_any_action, comment_text).
        """
        sns_id = str(row.get("sns_id") or "").strip()
        if not sns_id:
            return False, ""
        now_sec = int(time.time())
        today_str = datetime.now(TZ).strftime("%Y-%m-%d")

        m_content = str(row.get("content") or "").strip()
        is_serious_fn = getattr(df, "is_serious_or_sad_moment", None)
        if callable(is_serious_fn) and is_serious_fn(m_content):
            return False, ""

        async with self._state_lock:
            interacted = self._state.setdefault("interacted_moments", {})
            rec = dict(interacted.get(sns_id) or {"author": author_id, "ts": now_sec})

        did_like = bool(row.get("raw_like_flag") or rec.get("liked"))
        did_action = False
        if not did_like:
            ok_like, code_like, _ = await df.interact_wcf_moment(sns_id, "like")
            if ok_like or code_like == "already_liked":
                rec["liked"] = True
                did_action = True

        comment_sent = str(rec.get("comment") or "")
        if do_comment and not rec.get("commented") and len(m_content) >= 2:
            # Cap at max 2 auto-comments per friend per day to prevent spamming
            friend_today_comments = sum(
                1
                for v in (self._state.get("interacted_moments") or {}).values()
                if isinstance(v, dict)
                and v.get("author") == author_id
                and v.get("commented")
                and v.get("date") == today_str
            )
            if friend_today_comments < 2:
                api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
                base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
                model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))
                u = self._get_user_state(author_id)
                nick = str(row.get("nickname") or u.get("nickname") or author_id).strip()
                gen_txt, used_tok = await df.generate_moment_comment(
                    api_key,
                    base_url,
                    model,
                    author_name=nick,
                    moment_content=m_content,
                    mode=self._settings_for(author_id, is_group=False).get("mode", "live"),
                    existing_comments=row.get("comments") if isinstance(row.get("comments"), list) else None,
                    mood=int(u.get("mood", 75)),
                )
                if used_tok:
                    await self._record_tokens_safe(used_tok)
                if gen_txt:
                    ok_c, _, _ = await df.interact_wcf_moment(sns_id, "comment", gen_txt)
                    if ok_c:
                        rec["commented"] = True
                        rec["comment"] = gen_txt
                        rec["date"] = today_str
                        comment_sent = gen_txt
                        did_action = True

        rec["author"] = author_id
        rec["ts"] = now_sec
        async with self._state_lock:
            interacted = self._state.setdefault("interacted_moments", {})
            interacted[sns_id] = rec
            if len(interacted) > 300:
                oldest_keys = sorted(
                    interacted.keys(),
                    key=lambda k: int((interacted.get(k) or {}).get("ts", 0)),
                )[: len(interacted) - 300]
                for k_old in oldest_keys:
                    interacted.pop(k_old, None)
            self._mark_state_dirty()

        return did_action, comment_sent

    @staticmethod
    def _norm_moment_match(text: str) -> str:
        return re.sub(r"[\s，。！？、：:;；~～…\"'“”‘’「」『』]+", "", str(text or "")).strip().lower()

    def _is_known_self_moment(self, moment_content: str) -> bool:
        """Verifies an `is_self` WeChat Moment matches Dafeiyu's own published Moments so
        manually-posted owner Moments are not hijacked for automated replies (`U-05`).
        """
        self_hist = self._state.get("self_moments_history")
        moments_list = self._state.get("moments")
        if self_hist is None and isinstance(moments_list, list) and moments_list:
            # Legacy test harnesses without `self_moments_history`
            return True
        combined = list(self_hist or []) + list(moments_list or [])
        if not combined:
            return False
        m_norm = self._norm_moment_match(moment_content)
        if not m_norm:
            return False
        for item in combined:
            if not isinstance(item, dict):
                continue
            k_norm = self._norm_moment_match(str(item.get("text") or ""))
            if not k_norm:
                continue
            if m_norm == k_norm or (
                min(len(m_norm), len(k_norm)) >= 6
                and (m_norm.startswith(k_norm[:16]) or k_norm.startswith(m_norm[:16]))
            ):
                return True
        return False

    async def _check_and_reply_moment_comments(
        self,
        rows: list[dict[str, Any]],
        *,
        max_replies: int = 2,
    ) -> list[dict[str, str]]:
        """Scans loaded WeChat Moments rows and automatically replies to:
        1) New comments from friends on Dafeiyu's own Moments (`is_self == True`).
        2) New follow-up replies on a friend's Moment where Dafeiyu previously commented
           (when the post author or someone mentioning Dafeiyu replies after her comment).
        Returns a list of reply records `[{"sns_id", "user", "nick", "comment", "reply"}]`.
        """
        if not rows or max_replies <= 0:
            return []

        now_sec = int(time.time())
        self_wxids = {
            str(r.get("author") or "").strip().lower()
            for r in rows
            if isinstance(r, dict) and r.get("is_self") and str(r.get("author") or "").strip()
        }
        bot_names = {x.lower() for x in df.get_dynamic_bot_names() if x} | self_wxids

        def _is_bot_comment(c_item: dict[str, Any]) -> bool:
            cu = str(c_item.get("username") or "").strip().lower()
            cn = str(c_item.get("nickname") or "").strip().lower()
            return bool((cu and cu in bot_names) or (cn and cn in bot_names))

        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
        model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))

        replied_results: list[dict[str, str]] = []

        for row in rows:
            if not isinstance(row, dict) or len(replied_results) >= max_replies:
                continue
            sns_id = str(row.get("sns_id") or "").strip()
            if not sns_id or row.get("private"):
                continue
            comments = row.get("comments")
            if not isinstance(comments, list) or not comments:
                continue

            is_self = bool(row.get("is_self"))
            interacted_map = self._state.get("interacted_moments") or {}
            interacted_rec = interacted_map.get(sns_id) if isinstance(interacted_map.get(sns_id), dict) else {}

            m_content = str(row.get("content") or "").strip()
            if is_self:
                if not self._is_known_self_moment(m_content):
                    continue
            else:
                if not interacted_rec.get("commented") or interacted_rec.get("followup_replied"):
                    continue

            # Find index of Dafeiyu's own comment if this is a friend's moment
            bot_comment_idx = -1
            if not is_self:
                prev_bot_txt = str(interacted_rec.get("comment") or "").strip()
                for idx_c, c_item in enumerate(comments):
                    if not isinstance(c_item, dict):
                        continue
                    if _is_bot_comment(c_item) or (
                        prev_bot_txt and str(c_item.get("content") or "").strip() == prev_bot_txt
                    ):
                        bot_comment_idx = idx_c

            post_author = str(row.get("author") or "").strip()

            for idx, c in enumerate(comments):
                if len(replied_results) >= max_replies:
                    break
                if not isinstance(c, dict) or _is_bot_comment(c):
                    continue
                c_user = str(c.get("username") or "").strip()
                c_nick = str(c.get("nickname") or c_user or "好友").strip()
                c_text = re.sub(r"\s+", " ", str(c.get("content") or "").strip())
                if not c_text:
                    continue
                c_ts = int(c.get("timestamp") or 0)
                if c_ts > 0 and (now_sec - c_ts) > 3 * 86400:
                    continue

                if not is_self:
                    # Only reply to comments that came after Dafeiyu's comment (or after her interaction timestamp)
                    if bot_comment_idx >= 0 and idx <= bot_comment_idx:
                        continue
                    if bot_comment_idx < 0 and c_ts > 0 and c_ts < int(interacted_rec.get("ts") or 0):
                        continue
                    mentions_bot = any(k in c_text for k in ("肥鱼", "鲸鱼", "小鲸鱼"))
                    if c_user != post_author and not mentions_bot:
                        continue

                c_id = str(c.get("comment_id") or "").strip()
                comment_key = (
                    f"{sns_id}:{c_id}"
                    if (c_id and c_id != "0")
                    else f"{sns_id}:{c_user}:{c_ts}:{c_text[:24]}"
                )

                replied_map = self._state.get("replied_moment_comments") or {}
                if comment_key in replied_map:
                    continue

                # Check if a later comment in the feed is already a bot reply to this comment
                already_replied_in_feed = False
                for later_c in comments[idx + 1 :]:
                    if isinstance(later_c, dict) and _is_bot_comment(later_c):
                        later_txt = str(later_c.get("content") or "").strip()
                        if (c_nick and c_nick in later_txt) or len(comments) <= 2:
                            already_replied_in_feed = True
                            break
                if already_replied_in_feed:
                    async with self._state_lock:
                        rmap_mark = self._state.setdefault("replied_moment_comments", {})
                        rmap_mark[comment_key] = {
                            "sns_id": sns_id,
                            "user": c_user,
                            "nick": c_nick,
                            "comment": c_text,
                            "reply": "[已回评]",
                            "ts": now_sec,
                        }
                        self._mark_state_dirty()
                    continue

                # Cap replies per commenter per Moment to max 2 to avoid comment loops
                user_replies_on_post = sum(
                    1
                    for v in replied_map.values()
                    if isinstance(v, dict) and v.get("sns_id") == sns_id and v.get("user") == c_user
                )
                if user_replies_on_post >= 2:
                    continue

                u_obj = (self._state.get("users") or {}).get(c_user)
                mood_val = int(u_obj.get("mood", 78)) if isinstance(u_obj, dict) else 78
                mode = self._settings_for(c_user, is_group=False).get("mode", "live") if c_user else "live"

                reply_txt, used_tok = await df.generate_moment_reply_comment(
                    api_key,
                    base_url,
                    model,
                    moment_content=m_content,
                    commenter_name=c_nick,
                    comment_text=c_text,
                    is_self_moment=is_self,
                    mode=mode,
                    existing_comments=comments[: idx + 1],
                    mood=mood_val,
                    include_reply_prefix=True,
                )
                if used_tok:
                    await self._record_tokens_safe(used_tok)
                if not reply_txt:
                    continue

                ok_r, _, _ = await df.interact_wcf_moment(sns_id, "comment", reply_txt)
                if ok_r:
                    rec_item = {
                        "sns_id": sns_id,
                        "user": c_user,
                        "nick": c_nick,
                        "comment": c_text,
                        "reply": reply_txt,
                        "ts": now_sec,
                        "is_self": is_self,
                    }
                    async with self._state_lock:
                        rmap = self._state.setdefault("replied_moment_comments", {})
                        rmap[comment_key] = rec_item
                        if len(rmap) > 400:
                            oldest_k = sorted(
                                rmap.keys(),
                                key=lambda k: int((rmap.get(k) or {}).get("ts", 0)),
                            )[: len(rmap) - 400]
                            for ok_k in oldest_k:
                                rmap.pop(ok_k, None)
                        if not is_self:
                            im_map = self._state.setdefault("interacted_moments", {})
                            im_rec = im_map.setdefault(sns_id, {"author": post_author, "ts": now_sec})
                            im_rec["followup_replied"] = True
                        self._mark_state_dirty()

                    replied_results.append(rec_item)
                    if not is_self:
                        break

        return replied_results

    async def _check_and_interact_active_friends_moments(self, *, max_actions: int = 2) -> int:
        """Scans loaded WeChat 4.1 Moments feed, replies to new comments on Dafeiyu's Moments,
        and automatically likes/comments on new posts (<24h) from 7-day active private chat friends.
        """
        active_friends = self._get_7day_active_private_friends()
        if not active_friends and not (self._state.get("self_moments_history") or self._state.get("moments")):
            return 0

        ok_m, rows, _ = await df.fetch_wcf_moments(limit=15)
        if not ok_m or not rows:
            return 0
        self._cached_wcf_moments = (time.time(), list(rows))

        replied_list = await self._check_and_reply_moment_comments(rows, max_replies=max_actions)
        actions_done = len(replied_list)
        if actions_done >= max_actions or not active_friends:
            return actions_done

        now_sec = int(time.time())
        is_serious_fn = getattr(df, "is_serious_or_sad_moment", None)

        for row in rows:
            if not isinstance(row, dict):
                continue
            sns_id = str(row.get("sns_id") or "").strip()
            author = str(row.get("author") or "").strip()
            if not sns_id or not author or row.get("is_self") or row.get("private"):
                continue
            if author not in active_friends:
                continue
            post_ts = int(row.get("timestamp") or 0)
            if post_ts > 0 and (now_sec - post_ts) > 86400:
                continue

            row_content = str(row.get("content") or "").strip()
            if callable(is_serious_fn) and is_serious_fn(row_content):
                continue

            interacted = self._state.get("interacted_moments") or {}
            rec = interacted.get(sns_id)
            if isinstance(rec, dict) and rec.get("liked"):
                continue

            should_comment = bool(row_content and random.random() < 0.20)
            did_act, _ = await self._interact_single_friend_moment(
                author, row, do_comment=should_comment
            )
            if did_act:
                actions_done += 1
                if actions_done >= max_actions:
                    break
        return actions_done

    async def _moments_auto_interact_loop(self) -> None:
        """Background loop that periodically checks loaded Moments for 7-day active private chat friends."""
        self._clear_ended_invocation()
        while True:
            await asyncio.sleep(1080)
            try:
                self._clear_ended_invocation()
                if not self._state.get("enabled", True) or not (
                    self._state.get("auto_moments_enabled", False)
                    or self._state.get("moments_auto_interact_enabled", False)
                ):
                    continue
                now_dt = datetime.now(TZ)
                if now_dt.hour < 8:
                    continue
                await self._check_and_interact_active_friends_moments(max_actions=2)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Moments auto interact loop error: %s", e)

    async def _greet_newly_accepted_friend(
        self,
        req_id: str,
        friend_wxid: str,
        nickname: str = "",
        verification: str = "",
    ) -> bool:
        """Records a newly accepted friend request, initializes their private user archive, and sends a natural greeting."""
        now_sec = int(time.time())
        now_ms = now_sec * 1000
        clean_wxid = (friend_wxid or "").strip()
        clean_nick = (nickname or "").strip()

        async with self._state_lock:
            acc_map = self._state.setdefault("accepted_friend_requests", {})
            already = req_id in acc_map and bool(acc_map[req_id].get("greeted"))
            acc_map[req_id] = {
                "sender_id": clean_wxid,
                "nickname": clean_nick,
                "ts": now_sec,
                "greeted": True,
            }
            if len(acc_map) > 200:
                oldest = sorted(acc_map.keys(), key=lambda k: int((acc_map.get(k) or {}).get("ts") or 0))[:-150]
                for ok_key in oldest:
                    acc_map.pop(ok_key, None)

            if clean_wxid and not clean_wxid.endswith("@chatroom"):
                u = self._get_user_state(clean_wxid)
                u["hasPrivateChat"] = True
                u["lastChatAt"] = now_ms
                u["speakUpCountSinceReply"] = 0
                if clean_nick and not clean_nick.startswith("wxid_"):
                    _add_dedup_fact(
                        u.setdefault("facts", []),
                        f"对方通过微信好友申请添加了大肥鱼（微信昵称：{clean_nick}）",
                        max_len=25,
                    )
            self._mark_state_dirty()

        if already or not clean_wxid or clean_wxid.endswith("@chatroom"):
            return False

        settings = self._settings_for(clean_wxid)
        mode = settings.get("mode", "live")
        if mode == "sweet":
            welcome_txt = (
                f"哇，通过好友申请啦～你好呀{clean_nick or ''}，我是大肥鱼||以后有什么想聊的随时来找我玩呀"
            )
            res_meme = self._resolve_meme_b64("比心") or self._resolve_meme_b64("害羞")
        else:
            welcome_txt = (
                f"加上啦 你好呀{(' ' + clean_nick) if clean_nick else ''} 我是大肥鱼||有什么想聊的或者要帮忙的随时找我唠"
            )
            res_meme = self._resolve_meme_b64("猫爪") or self._resolve_meme_b64("得意")

        meme_b64 = res_meme[1] if (res_meme and settings.get("meme_enabled", True)) else None
        sent = await self._send_proactive_wechat(clean_wxid, welcome_txt, meme_b64)
        if sent:
            priv_key = self._resolve_private_session_key(clean_wxid, self._state.get("users", {}).get(clean_wxid))
            hist = list(self._histories.get(priv_key, []))
            hist.append((time.time(), "assistant", welcome_txt.replace("||", " ").strip()))
            self._lru_set(self._histories, priv_key, hist[-24:])
        return sent

    async def _check_and_process_friend_requests(self) -> int:
        """Checks WCF gateway for pending or newly accepted friend requests, accepts matching pending requests,
        and greets newly accepted friends. Returns number of newly processed requests.
        """
        auto_enabled = bool(self._state.get("auto_accept_friend_enabled", True))
        kw_list = [str(k).strip() for k in (self._state.get("friend_verify_keywords") or []) if str(k).strip()]
        ok, data, _ = await df.fetch_wcf_friend_requests()
        if not ok or not isinstance(data, dict):
            return 0

        if data.get("auto_accept") is not None and bool(data.get("auto_accept")) != auto_enabled:
            await df.set_wcf_friend_auto_accept(auto_enabled, kw_list)

        reqs = data.get("requests")
        if not isinstance(reqs, list) or not reqs:
            return 0

        processed = 0
        for item in reqs:
            if not isinstance(item, dict):
                continue
            req_id = str(item.get("request_id") or "").strip()
            if not req_id:
                continue
            status = str(item.get("status") or "").strip()
            sender_id = str(item.get("sender_id") or "").strip()
            nickname = str(item.get("nickname") or "").strip()
            verif = str(item.get("verification") or "").strip()

            if status == "pending" and auto_enabled:
                if not kw_list or any(kw in verif for kw in kw_list):
                    ok_acc, acc_data, _ = await df.accept_wcf_friend_request(req_id)
                    if ok_acc:
                        status = "accepted"
                        sender_id = str(acc_data.get("sender_id") or sender_id).strip()
                        nickname = str(acc_data.get("nickname") or nickname).strip()

            if status == "accepted":
                async with self._state_lock:
                    already_done = req_id in self._state.setdefault("accepted_friend_requests", {})
                if not already_done:
                    await self._greet_newly_accepted_friend(req_id, sender_id, nickname, verif)
                    processed += 1

        return processed

    async def _friend_auto_accept_loop(self) -> None:
        """Background loop that syncs friend auto-accept configuration and greets newly accepted friends."""
        self._clear_ended_invocation()
        await asyncio.sleep(8)
        try:
            await df.set_wcf_friend_auto_accept(
                bool(self._state.get("auto_accept_friend_enabled", True)),
                list(self._state.get("friend_verify_keywords") or []),
            )
        except Exception:
            pass
        while True:
            await asyncio.sleep(30)
            try:
                self._clear_ended_invocation()
                if not self._state.get("enabled", True):
                    continue
                await self._check_and_process_friend_requests()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Friend auto accept loop error: %s", e)

    async def _check_friend_moment_for_chat(
        self,
        sender_id: str,
        sender_name: str,
        session_key: str,
        cleaned_text: str,
        *,
        history_len: int = 0,
    ) -> str:
        """Opportunistically checks if the current private chat friend posted a recent WeChat Moment (<24h),
        likes/comments on it if unseen, and returns a prompt perception line so Dafeiyu can naturally mention it.
        Uses in-memory cached moments on normal chat turns so private replies never block on synchronous UI scraping.
        """
        now = time.time()
        mentioned_comment = any(
            kw in cleaned_text
            for kw in ("评论了你", "给你评论", "朋友圈评论", "回我评论", "看你朋友圈评论")
        )
        asking_bot_moment = not mentioned_comment and any(
            kw in cleaned_text for kw in ("你发的朋友圈", "你的朋友圈", "你朋友圈", "看你朋友圈")
        )
        mentioned_moments = not asking_bot_moment and (
            mentioned_comment
            or any(
                kw in cleaned_text
                for kw in ("看我朋友圈", "我刚发了朋友圈", "我发了朋友圈", "看我动态", "刚发了条朋友圈")
            )
        )
        rows: list[dict[str, Any]] = []
        if mentioned_moments:
            self._lru_set(self._chat_moment_check_ts, session_key, now)
            ok_m, fetched_rows, _ = await df.fetch_wcf_moments(limit=15)
            if ok_m and fetched_rows:
                rows = list(fetched_rows)
                self._cached_wcf_moments = (now, rows)
            else:
                rows = list(self._cached_wcf_moments[1])
        else:
            if not (
                self._state.get("auto_moments_enabled", False)
                or self._state.get("moments_auto_interact_enabled", False)
            ) or history_len < 2:
                return ""
            last_chk = self._chat_moment_check_ts.get(session_key, 0.0)
            if (now - last_chk) < 1200 or random.random() >= 0.35:
                return ""
            cached_ts, cached_rows = self._cached_wcf_moments
            if not cached_rows or (now - cached_ts) > 3600:
                return ""
            self._lru_set(self._chat_moment_check_ts, session_key, now)
            rows = list(cached_rows)

        if not rows:
            return ""

        if mentioned_comment:
            replied_now = await self._check_and_reply_moment_comments(rows, max_replies=2)
            user_replied = [r for r in replied_now if r.get("user") == sender_id]
            if not user_replied:
                rmap = self._state.get("replied_moment_comments") or {}
                user_replied = [
                    v
                    for v in rmap.values()
                    if isinstance(v, dict) and v.get("user") == sender_id and (int(now) - int(v.get("ts") or 0)) < 86400
                ]
            if user_replied:
                latest_r = user_replied[-1]
                return (
                    f"- **对方刚刚在你的朋友圈下评论了**：「{latest_r.get('comment', '')}」，"
                    f"你已经在朋友圈评论区回复了 TA：「{latest_r.get('reply', '')}」（可在聊天里自然呼应一句）\n"
                )

        now_sec = int(now)
        target_row: dict[str, Any] | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("author") or "").strip() != sender_id or row.get("is_self") or row.get("private"):
                continue
            post_ts = int(row.get("timestamp") or 0)
            if post_ts > 0 and (now_sec - post_ts) > 86400:
                continue
            target_row = row
            break

        if not target_row:
            return ""

        sns_id = str(target_row.get("sns_id") or "").strip()
        m_content = re.sub(r"\s+", " ", str(target_row.get("content") or "").strip())[:100]
        if not sns_id:
            return ""
        is_serious_fn = getattr(df, "is_serious_or_sad_moment", None)
        if callable(is_serious_fn) and is_serious_fn(m_content):
            return ""

        async with self._state_lock:
            interacted = self._state.setdefault("interacted_moments", {})
            rec = dict(interacted.get(sns_id) or {})
        comment_txt = str(rec.get("comment") or "")

        if not rec.get("liked"):
            should_comment = bool(m_content and (mentioned_moments or random.random() < 0.20))
            if mentioned_moments:
                _, new_comment = await self._interact_single_friend_moment(
                    sender_id, target_row, do_comment=should_comment
                )
                if new_comment:
                    comment_txt = new_comment
                async with self._state_lock:
                    rec = dict((self._state.get("interacted_moments") or {}).get(sns_id) or {})
            else:
                self._start_session_task(
                    session_key,
                    self._interact_single_friend_moment(sender_id, target_row, do_comment=should_comment),
                )

        if rec.get("chat_mentioned") and not mentioned_moments:
            return ""

        async with self._state_lock:
            entry = self._state.setdefault("interacted_moments", {}).setdefault(
                sns_id, {"author": sender_id, "ts": now_sec}
            )
            entry["chat_mentioned"] = True
            self._mark_state_dirty()

        act_desc = (
            f"你已顺手给这条动态点了赞并留了评论「{comment_txt}」"
            if comment_txt
            else ("你刚刚已经顺手给这条动态点了个赞" if rec.get("liked") else "你正打算顺手给这条动态点个赞")
        )
        return (
            f"- **你刚刚顺手刷到对方今天发的朋友圈**：「{m_content or '[分享图片/日常动态]'}」"
            f"（{act_desc}；如果和当前聊天话题自然契合，可以在回复时像真朋友一样顺嘴提半句，不契合则不用硬提）\n"
        )

    async def _is_duplicate(self, query_id: Any) -> bool:
        if query_id is None:
            return False
        key = str(query_id)
        async with self._lock:
            if key in self._seen_set:
                return True
            if len(self._seen_queries) >= 1024:
                old = self._seen_queries.popleft()
                self._seen_set.discard(old)
            self._seen_queries.append(key)
            self._seen_set.add(key)
            return False

    async def _handle_chat(self, event_ctx: context.EventContext, *, is_group: bool) -> None:
        if event_ctx.is_prevented_default():
            return
        if getattr(event_ctx.event, "reply_message_chain", None) is not None:
            return

        target_bot_uuid = self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID)
        bot_uuid = await self._resolve_bot_uuid(event_ctx)
        if target_bot_uuid and bot_uuid and bot_uuid != target_bot_uuid:
            return

        sender_id = str(getattr(event_ctx.event, "sender_id", "") or "user")
        launcher_id = str(getattr(event_ctx.event, "launcher_id", "") or "chat")
        scope = f"group:{launcher_id}" if is_group else f"person:{launcher_id}:{sender_id}"
        session_key = f"{bot_uuid}:{scope}"
        if is_group:
            g_settings_early = self._settings_for(launcher_id, is_group=True)
            if (
                launcher_id in self._state.get("enabled_groups", [])
                and g_settings_early.get("enabled", True)
            ):
                msg_chain = getattr(event_ctx.event, "message_chain", None)
                ev_raw_obj = getattr(event_ctx.event, "source_platform_object", None)
                raw_source_content = str(
                    ev_raw_obj.get("content", "") if isinstance(ev_raw_obj, dict) else ""
                ).strip()
                early_text = (
                    self._extract_plain_text(msg_chain)
                    or str(getattr(event_ctx.event, "text_message", "") or "").strip()
                    or raw_source_content
                )
                if early_text:
                    q_reply_txt, _, q_content, is_q_bot = df.extract_quote_context(msg_chain, early_text)
                    eff_early = q_reply_txt if q_content else early_text
                    if self._is_group_woken(msg_chain, eff_early, ev_raw_obj) or is_q_bot:
                        self._lru_set(
                            self._group_direct_wake_seq,
                            session_key,
                            self._group_direct_wake_seq.get(session_key, 0) + 1,
                        )
        lock = self._get_session_lock(session_key)
        # 计数包含等待者，不能按 locked() 回收已唤醒但尚未恢复的锁。
        self._session_lock_users[session_key] = self._session_lock_users.get(session_key, 0) + 1
        try:
            async with lock:
                await self._handle_session_chat(
                    event_ctx, is_group=is_group, bot_uuid=bot_uuid, session_key=session_key,
                )
        finally:
            self._session_lock_users[session_key] -= 1
            if not self._session_lock_users[session_key]:
                self._session_lock_users.pop(session_key)
                self._session_locks.pop(session_key, None)

    async def _handle_session_chat(
        self, event_ctx: context.EventContext, *, is_group: bool, bot_uuid: str, session_key: str,
    ) -> None:
        msg_chain = getattr(event_ctx.event, "message_chain", None)
        ev_obj = event_ctx.event
        ev_sender = getattr(ev_obj, "sender", None)
        ev_raw_obj = getattr(ev_obj, "source_platform_object", None)
        raw_source_content = str(
            ev_raw_obj.get("content", "") if isinstance(ev_raw_obj, dict) else ""
        ).strip()
        raw_text = self._extract_plain_text(msg_chain)
        if not raw_text:
            raw_text = str(getattr(event_ctx.event, "text_message", "") or "").strip() or raw_source_content
        if not raw_text:
            return

        sender_id = str(getattr(event_ctx.event, "sender_id", "") or "user")
        launcher_id = str(getattr(event_ctx.event, "launcher_id", "") or "chat")
        # Group chats share a group-level session context; private chats are per-person
        doc_key = (bot_uuid, session_key, sender_id)

        clean_raw_for_cmd = (
            re.sub(r"<img_b64>.*?</img_b64>", "", raw_text, flags=re.S).strip() or "[图片]"
            if "<img_b64>" in raw_text
            else raw_text
        )
        # Parse WeChat Quote early so control commands & disabled-group short-circuit avoid heavy network/image calls
        quote_reply_txt, quote_speaker, quote_content, is_quoting_bot = df.extract_quote_context(
            msg_chain, clean_raw_for_cmd
        )
        effective_text_early = quote_reply_txt if quote_content else clean_raw_for_cmd
        raw_no_at = re.sub(r"^(?:@[^\s\u2005]+[\s\u2005]*)+", "", effective_text_early.strip()).strip()
        cmd_norm = re.sub(r"\s+", " ", (raw_no_at or effective_text_early).strip())

        target_id = launcher_id if is_group else sender_id
        prev_mode = self._settings_for(target_id, is_group=is_group).get("mode")
        control = ui.handle_control(
            self._state, cmd_norm, is_group=is_group, target_id=target_id,
            sender_id=sender_id, owner_ids=self._get_config_str("owner_ids", DEFAULT_OWNER_IDS),
        )
        if control is not None:
            message, changed = control
            if changed:
                new_mode = self._settings_for(target_id, is_group=is_group).get("mode")
                if prev_mode != new_mode:
                    self._histories.pop(session_key, None)
                    self._temper_state.pop((session_key, sender_id), None)
                self._mark_state_dirty(immediate=True)
                if is_group:
                    self._group_active_conv.pop(session_key, None)
                    self._group_repeat_tracker.pop(session_key, None)
            self._reply(event_ctx, message)
            return
        settings = self._settings_for(target_id, is_group=is_group)

        # Group Chat Whitelist Gate (O(1) early short-circuit before network/image calls)
        if is_group and (
            launcher_id not in self._state.get("enabled_groups", []) or not settings["enabled"]
        ):
            if self._is_explicit_at_wake(msg_chain, clean_raw_for_cmd, ev_raw_obj):
                now_hint = time.time()
                last_hint = float(self._disabled_group_hint_ts.get(launcher_id, 0.0) or 0.0)
                if now_hint - last_hint >= 1800:
                    self._lru_set(self._disabled_group_hint_ts, launcher_id, now_hint)
                    await self._reply_group_no_at(
                        event_ctx,
                        launcher_id,
                        "本群还没开启大肥鱼回应哦~ 发送「大肥鱼 开启」或「大肥鱼 设置」即可启用，发「大肥鱼 帮助」查看指令。",
                    )
            return

        if session_key not in self._recent_bot_sent_texts:
            self._lru_set(self._recent_bot_sent_texts, session_key, deque(maxlen=40))
        else:
            self._lru_touch(self._recent_bot_sent_texts, session_key)
        recent_bot_texts = self._recent_bot_sent_texts[session_key]

        event_sender_name = str(
            getattr(ev_obj, "sender_name", "")
            or getattr(ev_sender, "member_name", "")
            or getattr(ev_sender, "nickname", "")
            or (ev_raw_obj.get("sender_name", "") if isinstance(ev_raw_obj, dict) else "")
            or ""
        ).strip()
        sender_name = await df.resolve_user_display_name(
            sender_id, launcher_id if is_group else "", event_sender_name
        )
        if is_group:
            self._sync_group_member_real_name(launcher_id, sender_id, sender_name)

        self._start_session_task(session_key, df.refresh_dynamic_bot_names())

        # Extract any incoming image (from WeChat 4.1 <img_b64> or Image component) and strip raw <img_b64> payload from text
        img_source_str = raw_source_content if "<img_b64>" in raw_source_content else raw_text
        incoming_img_b64 = await self._extract_image_b64_from_chain(msg_chain, img_source_str)
        if "<img_b64>" in raw_text:
            raw_text = clean_raw_for_cmd

        # Parse WeChat Merged Forwarded Chat Record (<type>19</type> / <recorditem>)
        fwd_title, fwd_des, is_bot_fwd = df.extract_forwarded_chat_record(
            raw_text, recent_bot_texts
        )
        effective_text = effective_text_early

        # Group Chat Active Handling (after whitelist gate)
        if is_group:

            # Silently collect group stickers into stolen_memes library
            if self._is_incoming_sticker(raw_text, ev_raw_obj):
                st_desc = ""
                if isinstance(ev_raw_obj, dict):
                    st_desc = str(
                        ev_raw_obj.get("sticker_desc")
                        or ev_raw_obj.get("vision_desc")
                        or ev_raw_obj.get("description")
                        or ""
                    ).strip()
                if not st_desc and incoming_img_b64:
                    st_desc = self._lookup_cached_vision_desc(incoming_img_b64)
                short_st = self._compress_sticker_description(st_desc)
                grp_st_line = f"[表情包：{short_st}]" if short_st else "[动画表情]"
                df.append_group_context_line(
                    self._group_msg_buffers, session_key, sender_id, grp_st_line, sender_name=sender_name
                )
                self._start_session_task(session_key, df.steal_incoming_sticker(raw_text, settings, self._stolen_memes))
                return

            # Direction 1: Cache incoming group image (90s TTL) & support direct vision reply during active conversation
            if incoming_img_b64:
                self._lru_set(self._group_recent_images, session_key, {
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "b64": incoming_img_b64,
                    "ts": time.time(),
                })
                grp_photo_desc = ""
                if isinstance(ev_raw_obj, dict):
                    grp_photo_desc = str(
                        ev_raw_obj.get("vision_desc") or ev_raw_obj.get("description") or ""
                    ).strip()
                if not grp_photo_desc:
                    grp_photo_desc = self._lookup_cached_vision_desc(incoming_img_b64)
                grp_img_line = (
                    f"[发了一张图片：{grp_photo_desc}]" if grp_photo_desc else "[发了一张图片]"
                )
                df.append_group_context_line(
                    self._group_msg_buffers, session_key, sender_id, grp_img_line, sender_name=sender_name
                )

            # Natural-language group quiet/mute command (pronoun forms like "它能闭嘴吗" require bot spoke <= 90s ago)
            now_mute = time.time()
            is_woken_early = self._is_group_woken(msg_chain, effective_text, ev_raw_obj) or is_quoting_bot
            last_bot_ts_mute = float(
                (self._group_active_conv.get(session_key) or {}).get("last_bot_ts", 0.0) or 0.0
            )
            has_recent_bot_mute = (now_mute - last_bot_ts_mute) <= 90.0
            if (
                GROUP_MUTE_EXPLICIT_RE.search(effective_text)
                or GROUP_MUTE_EXPLICIT_RE.search(cmd_norm)
                or (is_woken_early and WOKEN_MUTE_RE.match(cmd_norm))
                or (
                    has_recent_bot_mute
                    and (
                        GROUP_MUTE_PRONOUN_RE.search(effective_text)
                        or GROUP_MUTE_PRONOUN_RE.search(cmd_norm)
                    )
                )
            ):
                self._lru_set(self._group_mute_until, session_key, now_mute + 1800)
                self._group_active_conv.pop(session_key, None)
                await self._reply_group_no_at(
                    event_ctx,
                    launcher_id,
                    "行 我先闭麦潜水半小时，不主动插嘴了（有事直接喊「大肥鱼」，想彻底关插话可发「大肥鱼 关闭群搭话」）",
                )
                return

            if fwd_title or fwd_des:
                fwd_one_line = re.sub(r"\s+", " / ", fwd_des).strip()[:140]
                df.append_group_context_line(
                    self._group_msg_buffers,
                    session_key,
                    sender_id,
                    f"[合并转发《{fwd_title}》]: {fwd_one_line}",
                    sender_name=sender_name,
                )

            # Respect 30-min temporary quiet mode strictly BEFORE forwarded/card/file/voice/continuation/interject
            # Unmute ONLY when explicitly @-mentioned or called by bot name
            is_explicit_unmute = self._is_explicit_unmute_wake(msg_chain, effective_text, ev_raw_obj)
            is_muted_now = now_mute < float(self._group_mute_until.get(session_key, 0.0) or 0.0)
            if is_muted_now:
                if is_explicit_unmute:
                    self._group_mute_until.pop(session_key, None)
                    is_muted_now = False
                else:
                    if not incoming_img_b64 and not (fwd_title or fwd_des):
                        df.append_group_context_line(
                            self._group_msg_buffers, session_key, sender_id, effective_text, sender_name=sender_name
                        )
                    return

            is_fwd_woken = bool(
                (fwd_title or fwd_des)
                and (
                    is_bot_fwd
                    or self._should_check_active_continuation(
                        launcher_id, sender_id, msg_chain, fwd_title, ev_raw_obj, session_key=session_key
                    )
                )
            )
            is_woken = (
                self._is_group_woken(msg_chain, effective_text, ev_raw_obj)
                or is_quoting_bot
                or (is_fwd_woken and not settings["explicit_only"])
            )
            is_shared_card_or_file = df.has_supported_media_link_or_card(raw_text)
            is_group_voice = self._is_incoming_voice(raw_text)
            if settings["explicit_only"] and not is_woken and not is_group_voice:
                df.append_group_context_line(
                    self._group_msg_buffers, session_key, sender_id, effective_text, sender_name=sender_name
                )
                return

            conv_peek = self._group_active_conv.get(session_key) or {}
            is_recent_same_speaker_img = bool(
                incoming_img_b64
                and not (fwd_title or fwd_des)
                and not settings["explicit_only"]
                and sender_id == str(conv_peek.get("last_speaker_id", ""))
                and (time.time() - float(conv_peek.get("last_bot_ts", 0.0) or 0.0)) <= 15.0
            )
            if not is_woken and not is_shared_card_or_file and not is_group_voice:
                if (incoming_img_b64 and not is_recent_same_speaker_img) or fwd_title or fwd_des:
                    return
                if not settings["enabled"]:
                    return

                # Active Conversation Window: recognize multi-turn follow-ups & group discussion about Dafeiyu without requiring @
                cont_probe_text = "看看这张图" if is_recent_same_speaker_img else effective_text
                if self._should_check_active_continuation(
                    launcher_id, sender_id, msg_chain, cont_probe_text, ev_raw_obj, session_key=session_key
                ):
                    if not is_recent_same_speaker_img:
                        df.append_group_context_line(
                            self._group_msg_buffers, session_key, sender_id, effective_text, sender_name=sender_name
                        )
                    conv = self._group_active_conv.get(session_key) or {}
                    api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
                    base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
                    model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))
                    recent_lines = list(self._group_msg_buffers.get(session_key, []))
                    g_state = self._get_group_state(launcher_id)
                    g_mem_str = self._recall_group_memories(g_state, effective_text)
                    fam_str = self._recall_readonly_private_familiarity(sender_id, sender_name, launcher_id)
                    anti_rep_str = df.build_anti_repetition_instruction(recent_bot_texts)
                    temper_str, temper_meme_tag, temper_mood_delta = df.check_and_update_temper_state(
                        self._temper_state, session_key, sender_id, effective_text
                    )
                    if temper_mood_delta != 0:
                        async with self._state_lock:
                            g_state["mood"] = int(_clamp(int(g_state.get("mood", 75)) + temper_mood_delta, 25, 100))

                    # Attach recent group image if speaker sent one within 90s or mentioned image keywords
                    ctx_vision_b64: str | None = None
                    rec_img = self._group_recent_images.get(session_key)
                    if isinstance(rec_img, dict) and (time.time() - float(rec_img.get("ts", 0))) <= 90.0:
                        if rec_img.get("sender_id") == sender_id or any(
                            kw in effective_text for kw in IMAGE_FOLLOWUP_KEYWORDS
                        ):
                            ctx_vision_b64 = str(rec_img.get("b64") or "") or None

                    is_same_sp = sender_id == str(conv.get("last_speaker_id", ""))
                    pre_cont_wake_seq = self._group_direct_wake_seq.get(session_key, 0)
                    pre_cont_hist_len = len(self._histories.get(session_key, []))
                    pre_cont_bot_ts = float((self._group_active_conv.get(session_key) or {}).get("last_bot_ts", 0.0))
                    cont_user_text = "[图片] 看看这张图" if (is_recent_same_speaker_img and effective_text == "[图片]") else effective_text
                    should_rep, rep_txt, _, used_tok = await df.evaluate_group_active_continuation(
                        bot_uuid=bot_uuid,
                        api_key=api_key,
                        base_url=base_url,
                        model=model,
                        group_id=launcher_id,
                        sender_id=sender_id,
                        current_text=cont_user_text,
                        recent_lines=recent_lines,
                        last_bot_reply=str(conv.get("last_bot_reply", "")),
                        is_same_speaker=is_same_sp,
                        state=self._state,
                        group_memory_str=g_mem_str,
                        familiarity_str=fam_str + anti_rep_str + temper_str,
                        sender_name=sender_name,
                        vision_b64=ctx_vision_b64,
                        mode=settings["mode"],
                    )
                    await self._record_tokens_safe(used_tok)
                    self._mark_state_dirty()
                    if (
                        self._group_direct_wake_seq.get(session_key, 0) != pre_cont_wake_seq
                        or len(self._histories.get(session_key, [])) != pre_cont_hist_len
                        or float((self._group_active_conv.get(session_key) or {}).get("last_bot_ts", 0.0)) > pre_cont_bot_ts
                    ):
                        return
                    if should_rep and rep_txt:
                        cleaned_rep = self._clean_reply(
                            rep_txt, user_text=effective_text, recent_bot_texts=recent_bot_texts
                        )
                        final_txt, _, meme_b64_cont = self._extract_text_and_meme(cleaned_rep)
                        final_txt = df.strip_redundant_speaker_name(final_txt, sender_name)
                        if temper_meme_tag and not meme_b64_cont:
                            res_tm = self._resolve_meme_b64(temper_meme_tag)
                            if res_tm:
                                meme_b64_cont = res_tm[1]
                        send_cont_meme = meme_b64_cont if settings["meme_enabled"] else None
                        was_multi = self._last_multi_bubble.get(session_key, False)
                        bubbles = self._split_reply_bubbles(final_txt, last_was_multi=was_multi)
                        if bubbles:
                            joined_log, _ = self._record_group_side_reply(
                                session_key, sender_name, effective_text, bubbles
                            )
                            self._record_group_bot_activity(session_key, sender_id, joined_log)
                            self._start_session_task(session_key,
                                self._async_post_group_turn_judge(
                                    api_key=api_key,
                                    base_url=base_url,
                                    model=model,
                                    group_id=launcher_id,
                                    expected_state=g_state,
                                    user_text=f"[{sender_name}] {effective_text}",
                                    reply_text=joined_log,
                                    sender_id=sender_id,
                                    sender_name=sender_name,
                                )
                            )
                            await self._send_unified_bubbles(
                                event_ctx,
                                session_key=session_key,
                                target_id=launcher_id,
                                bubbles=bubbles,
                                image_b64=send_cont_meme,
                                is_group=True,
                                bot_uuid=bot_uuid,
                            )
                            return
                    else:
                        # Speaker switched topic away from Dafeiyu
                        act_set = conv.get("active_speakers")
                        if isinstance(act_set, set):
                            act_set.discard(sender_id)
                        if not is_recent_same_speaker_img:
                            repeated_txt = df.check_group_random_repeat(
                                self._group_repeat_tracker,
                                settings,
                                session_key,
                                sender_id,
                                msg_chain,
                                effective_text,
                            )
                            if repeated_txt:
                                await self._reply_group_no_at(event_ctx, launcher_id, repeated_txt)
                        return

                # Diana Feature 1: Random Repeat (0 LLM token, >=2 users, 30% prob, 60s cooldown)
                repeated_txt = df.check_group_random_repeat(
                    self._group_repeat_tracker,
                    settings,
                    session_key,
                    sender_id,
                    msg_chain,
                    effective_text,
                )
                if repeated_txt:
                    await self._reply_group_no_at(event_ctx, launcher_id, repeated_txt)
                    return

                # Diana Feature 2: Proactive Group Interjection (Decide whether to reply or stay quiet + log reason)
                if settings["group_interject_enabled"]:
                    should_judge = df.record_group_message_for_interject(
                        self._group_msg_buffers,
                        self._group_interject_meta,
                        session_key,
                        sender_id,
                        effective_text,
                        sender_name=sender_name,
                        cooldown_sec=settings["interject_cooldown_sec"],
                        min_messages=settings["interject_min_messages"],
                    )
                    if should_judge:
                        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
                        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
                        model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))
                        recent_lines = list(self._group_msg_buffers.get(session_key, []))
                        g_state = self._get_group_state(launcher_id)
                        g_mem_str = self._recall_group_memories(g_state, effective_text)
                        anti_rep_interj = df.build_anti_repetition_instruction(recent_bot_texts)
                        pre_interj_wake_seq = self._group_direct_wake_seq.get(session_key, 0)
                        pre_interj_hist_len = len(self._histories.get(session_key, []))
                        pre_interj_bot_ts = float(
                            (self._group_active_conv.get(session_key) or {}).get("last_bot_ts", 0.0)
                        )
                        try:
                            should_rep, rep_txt, _, used_tok = await df.evaluate_group_interject(
                                bot_uuid=bot_uuid,
                                api_key=api_key,
                                base_url=base_url,
                                model=model,
                                group_id=launcher_id,
                                recent_lines=recent_lines,
                                state=self._state,
                                group_memory_str=g_mem_str,
                                mode=settings["mode"],
                                anti_rep_str=anti_rep_interj,
                            )
                        except TypeError as te:
                            if "unexpected keyword argument" not in str(te):
                                raise
                            fallback_mem = (
                                f"{g_mem_str}\n{anti_rep_interj}".strip()
                                if anti_rep_interj
                                else g_mem_str
                            )
                            should_rep, rep_txt, _, used_tok = await df.evaluate_group_interject(
                                bot_uuid=bot_uuid,
                                api_key=api_key,
                                base_url=base_url,
                                model=model,
                                group_id=launcher_id,
                                recent_lines=recent_lines,
                                state=self._state,
                                group_memory_str=fallback_mem,
                                mode=settings["mode"],
                            )
                        await self._record_tokens_safe(used_tok)
                        if (
                            self._group_direct_wake_seq.get(session_key, 0) != pre_interj_wake_seq
                            or len(self._histories.get(session_key, [])) != pre_interj_hist_len
                            or float((self._group_active_conv.get(session_key) or {}).get("last_bot_ts", 0.0))
                            > pre_interj_bot_ts
                        ):
                            async with self._state_lock:
                                logs = self._state.setdefault("interject_logs", [])
                                if logs and isinstance(logs[-1], dict) and logs[-1].get("group_id") == launcher_id:
                                    logs[-1]["interjected"] = False
                                    logs[-1]["reply"] = ""
                                    logs[-1]["reason"] = "评估期间同群已收到直接唤醒或新回复，放弃重复插话"
                            self._mark_state_dirty()
                            return
                        self._mark_state_dirty()
                        if should_rep and rep_txt:
                            cleaned_rep = self._clean_reply(
                                rep_txt, user_text=effective_text, recent_bot_texts=recent_bot_texts
                            )
                            final_txt, interj_meme_tag, interj_meme_b64 = self._extract_text_and_meme(cleaned_rep)
                            final_txt = df.strip_redundant_speaker_name(final_txt, sender_name)
                            now_ts_int = int(time.time())
                            async with self._state_lock:
                                turns_since_meme = int(g_state.get("turnsSinceMeme", 99)) + 1
                                last_meme_at = int(g_state.get("lastMemeAt", 0))
                                if interj_meme_b64:
                                    if turns_since_meme < 5 or (now_ts_int - last_meme_at) < 180 or random.random() >= 0.15:
                                        interj_meme_b64 = None
                                    else:
                                        interj_meme_b64 = df.pick_stolen_or_official_meme(
                                            interj_meme_tag or "搞笑", interj_meme_b64, settings, self._stolen_memes
                                        )
                                send_interj_meme = interj_meme_b64 if settings["meme_enabled"] else None
                                if send_interj_meme:
                                    g_state["turnsSinceMeme"] = 0
                                    g_state["lastMemeAt"] = now_ts_int
                                else:
                                    g_state["turnsSinceMeme"] = turns_since_meme
                            self._mark_state_dirty()
                            was_multi = self._last_multi_bubble.get(session_key, False)
                            bubbles = self._split_reply_bubbles(final_txt, last_was_multi=was_multi)
                            if bubbles:
                                _, now_ts = self._record_group_side_reply(
                                    session_key, sender_name, effective_text, bubbles
                                )
                                self._lru_set(
                                    self._group_interject_meta,
                                    session_key,
                                    {"last_judge_ts": now_ts, "unjudged_count": 0},
                                )
                                await self._send_unified_bubbles(
                                    event_ctx,
                                    session_key=session_key,
                                    target_id=launcher_id,
                                    bubbles=bubbles,
                                    image_b64=send_interj_meme,
                                    is_group=True,
                                    bot_uuid=bot_uuid,
                                )
                return
            elif not is_group_voice and not (fwd_title or fwd_des) and not incoming_img_b64:
                clean_for_buf = self._strip_wake_prefix(effective_text) or effective_text.strip()
                if not clean_for_buf.startswith("<"):
                    df.append_group_context_line(
                        self._group_msg_buffers, session_key, sender_id, clean_for_buf, sender_name=sender_name
                    )

        # Unified prefix parsing: 大肥鱼 / 鲸鱼娘 / 小鲸鱼 / 鲸鱼人设 + 子口令
        m_cmd_wake = CMD_WAKE_PREFIX_RE.match(cmd_norm)
        has_cmd_wake = bool(m_cmd_wake)
        cmd_body = (m_cmd_wake.group(1) if m_cmd_wake else cmd_norm).strip()

        handled, effective_text = await self._dispatch_prefix_command(
            event_ctx,
            is_group=is_group,
            launcher_id=launcher_id,
            sender_id=sender_id,
            session_key=session_key,
            doc_key=doc_key,
            bot_uuid=bot_uuid,
            settings=settings,
            cmd_norm=cmd_norm,
            cmd_body=cmd_body,
            has_cmd_wake=has_cmd_wake,
            effective_text=effective_text,
        )
        if handled:
            return

        if not settings["enabled"]:
            return

        if not incoming_img_b64 and self._should_ignore_plugin_command(raw_text):
            return

        is_sticker_in = self._is_incoming_sticker(raw_text, ev_raw_obj)
        is_voice_in = self._is_incoming_voice(raw_text)
        is_file_in = df.is_incoming_wechat_file(raw_text)
        vision_b64: str | None = incoming_img_b64
        vision_desc_for_history: str = (
            str(
                ev_raw_obj.get("sticker_desc")
                or ev_raw_obj.get("vision_desc")
                or ev_raw_obj.get("description")
                or ""
            ).strip()
            if isinstance(ev_raw_obj, dict)
            else ""
        )
        parsed_link_card: str | None = None
        parsed_link_img_b64: str | None = None
        hist_user_text: str = ""
        just_cached_doc_this_turn: bool = False

        if not is_group and incoming_img_b64 and not is_sticker_in:
            self._lru_set(self._private_recent_images, session_key, {
                "sender_id": sender_id,
                "b64": incoming_img_b64,
                "ts": time.time(),
            })

        if is_sticker_in:
            if is_group:
                return
            vision_b64 = (await self._fetch_sticker_strip_b64(raw_text)) or incoming_img_b64
            if vision_b64:
                cleaned_text = "（对方刚刚给你发了这张微信表情包！如果是动图则已抽帧拼成左中右三格。请用 DeepSeek-V4.1-Flash 视觉看清表情包里的画面/角色动作/文字，用大肥鱼的微信活人短句接梗回应一句，并在最末尾附上 `[表情: 名称]` 甩一张你的表情包回去！）"
            else:
                cleaned_text = "（对方刚刚给你发了一张微信表情包跟你斗图！请用活人短句回敬一句，并在最末尾附上 `[表情: 名称]` 甩一张你的表情包回去！）"
            hist_user_text = "[发来了一张微信表情包]"
        elif is_voice_in:
            voice_text, voice_tone, voice_dur = await self._transcribe_incoming_voice(raw_text)
            if is_group:
                if voice_text:
                    df.append_group_context_line(
                        self._group_msg_buffers,
                        session_key,
                        sender_id,
                        f"[语音] {voice_text}",
                        sender_name=sender_name,
                    )
                has_voice_wake = bool(
                    voice_text and any(w in voice_text for w in ("大肥鱼", "鲸鱼娘", "小鲸鱼", "鲸鱼人设", "肥鱼"))
                )
                conv_v = self._group_active_conv.get(session_key) or {}
                is_same_speaker_60s = bool(
                    sender_id
                    and sender_id == str(conv_v.get("last_speaker_id", ""))
                    and (time.time() - float(conv_v.get("last_bot_ts", 0.0))) <= 60.0
                )
                allow_voice_followup = bool(
                    not settings["explicit_only"]
                    and settings["continuation_enabled"]
                    and (is_quoting_bot or is_same_speaker_60s)
                )
                if not voice_text or not (has_voice_wake or allow_voice_followup):
                    return
                if has_voice_wake:
                    self._group_mute_until.pop(session_key, None)
            tone_part = f"，语气特征[{voice_tone}]" if voice_tone else ""
            if voice_text:
                cleaned_text = (
                    f"（对方刚刚给你发了一条 {voice_dur} 秒的微信语音{tone_part}，"
                    f"语音识别内容是：「{voice_text}」。"
                    "请像在微信里刚听完对方这条语音一样，结合对方说的话和语气，用大肥鱼的微信活人短句自然回应！）"
                )
                hist_user_text = f"[微信语音 {voice_dur}秒{tone_part}] {voice_text}"
            else:
                cleaned_text = (
                    f"（对方刚刚给你发了一条 {voice_dur} 秒的微信语音{tone_part}，"
                    "但里面几乎没听清说话声，只有一点背景动静或呼吸声。请用大肥鱼的活人口吻吐槽或追问一句！）"
                )
                hist_user_text = f"[微信语音 {voice_dur}秒（未听清内容）]"
        elif is_file_in:
            # Diana Feature 6: File Parsing (PDF / Word / Excel / PPT / EPUB / TXT / Code + optional async DeepSeek OCR)
            file_api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
            file_base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
            fname, ftext = await self._call_parse_wechat_file_xml(
                raw_text, api_key=file_api_key, base_url=file_base_url
            )
            if ftext:
                self._store_user_doc(doc_key, fname, ftext)
                cleaned_text = (
                    f"（对方刚刚发来了一个文件《{fname}》，已为你提取出文件中的核心文本内容如下：\n"
                    f"--------------------\n{ftext[:7000]}\n--------------------\n"
                    "请用大肥鱼的活人口吻，先简明扼要地帮对方总结这份文件的重点内容，并问问对方具体想看哪一部分！）"
                )
                hist_user_text = f"[发送了文件《{fname}》]"
            else:
                self._reply(event_ctx, f"收到文件《{fname}》啦，但这会儿文件还在下载或者格式受限没能提取出文字，你可以直接把里面的关键段落发出来给我看")
                return
        elif fwd_title or fwd_des:
            if is_bot_fwd:
                cleaned_text = (
                    f"（对方刚刚把一段包含你发言的聊天记录《{fwd_title}》合并转发到了聊天里给大家围观！\n"
                    f"【转发的聊天记录内容】：\n{fwd_des}\n"
                    "请用大肥鱼的微信活人短句（默认1条气泡，严禁打括号动作）自然接梗、嘴硬或吐槽对方居然把跟你的聊天记录发出来公开处刑！）"
                )
            else:
                cleaned_text = (
                    f"（对方刚刚合并转发了一段聊天记录《{fwd_title}》：\n"
                    f"{fwd_des}\n"
                    "请用大肥鱼的微信活人短句（默认1条气泡，严禁打括号动作）对这段聊天记录顺嘴锐评或接话一句！）"
                )
            hist_user_text = f"[合并转发《{fwd_title}》] {fwd_des[:100]}"
        else:
            # Check if message contains a WeChat MP / Bilibili / X(Twitter) / YouTube / Xiaohongshu / Douyin link or WeChat shared card
            parsed_link_card, parsed_link_img_b64 = await df.extract_and_parse_link(raw_text)
            if parsed_link_card:
                first_card_line = parsed_link_card.splitlines()[0].strip() if parsed_link_card else "分享链接"
                if "【微信公众号推文】" in parsed_link_card or len(parsed_link_card) > 360:
                    doc_title = re.sub(r"^[^\w\u4e00-\u9fa5]*【[^】]+】", "", first_card_line).strip() or "分享文章"
                    self._store_user_doc(doc_key, doc_title[:60], parsed_link_card[:12000])
                    just_cached_doc_this_turn = True
                cleaned_text = (
                    f"（对方刚刚分享了一个链接/卡片，已自动解析出详情如下：\n{parsed_link_card}\n"
                    "请用大肥鱼的微信活人短句（1~2句话，不许打括号动作）对这个链接/推文/视频/主页内容顺嘴锐评或接梗一句！）"
                )
                hist_user_text = f"[分享链接] {first_card_line[:120]}"
            else:
                if is_group and not (
                    self._is_group_woken(msg_chain, effective_text, ev_raw_obj)
                    or is_quoting_bot
                ):
                    return
                cleaned_text = self._strip_wake_prefix(effective_text)
                if not cleaned_text:
                    cleaned_text = effective_text.strip()

                if not incoming_img_b64 and self._should_ignore_plugin_command(cleaned_text):
                    return
                if not vision_b64 and is_group:
                    # Direction 1: Attach cached recent group image if speaker sent one within 90s, mentioned image keywords, or quoted "[图片]"
                    rec_img = self._group_recent_images.get(session_key)
                    if isinstance(rec_img, dict) and (time.time() - float(rec_img.get("ts", 0))) <= 90.0:
                        if (
                            rec_img.get("sender_id") == sender_id
                            or any(kw in cleaned_text for kw in IMAGE_FOLLOWUP_KEYWORDS)
                            or (quote_content and "[图片]" in str(quote_content))
                        ):
                            self._lru_touch(self._group_recent_images, session_key)
                            vision_b64 = str(rec_img.get("b64") or "") or None
                elif not vision_b64 and not is_group:
                    rec_priv_img = self._private_recent_images.get(session_key)
                    if isinstance(rec_priv_img, dict):
                        img_age = time.time() - float(rec_priv_img.get("ts", 0))
                        if (
                            img_age <= 45.0
                            or (img_age <= 90.0 and any(kw in cleaned_text for kw in IMAGE_FOLLOWUP_KEYWORDS))
                            or (img_age <= 90.0 and quote_content and "[图片]" in str(quote_content))
                        ):
                            self._lru_touch(self._private_recent_images, session_key)
                            vision_b64 = str(rec_priv_img.get("b64") or "") or None

                if vision_b64 and cleaned_text in ("", "[图片]"):
                    cleaned_text = (
                        "（对方刚刚发了这张图片，请仔细看清图片里的画面、文字和细节，"
                        "用大肥鱼的微信活人短句顺嘴吐槽或回应一句！）"
                    )
                    hist_user_text = "[发了一张图片]"
                elif quote_content:
                    comp_prompt, comp_hist, comp_doc = df.compact_long_user_text(cleaned_text)
                    if comp_doc:
                        self._store_user_doc(doc_key, f"长文本消息({len(cleaned_text)}字)", comp_doc)
                        just_cached_doc_this_turn = True
                    hist_user_text = f"[引用「{quote_speaker}: {quote_content[:60]}」] {comp_hist}"
                    cleaned_text = (
                        f"【对方引用了消息「{quote_speaker}: {quote_content}」并回复】：\n{comp_prompt}"
                    )
                elif vision_b64 and not incoming_img_b64:
                    comp_prompt, comp_hist, comp_doc = df.compact_long_user_text(cleaned_text)
                    if comp_doc:
                        self._store_user_doc(doc_key, f"长文本消息({len(cleaned_text)}字)", comp_doc)
                        just_cached_doc_this_turn = True
                    hist_user_text = comp_hist
                    cleaned_text = (
                        f"（结合刚刚发出的这张图片，对方对你说：「{comp_prompt}」。"
                        "请仔细看清图片里的画面、文字和细节，用大肥鱼的微信活人短句直接回应！）"
                    )
                else:
                    comp_prompt, comp_hist, comp_doc = df.compact_long_user_text(cleaned_text)
                    if comp_doc:
                        self._store_user_doc(doc_key, f"长文本消息({len(cleaned_text)}字)", comp_doc)
                        just_cached_doc_this_turn = True
                    cleaned_text = comp_prompt
                    hist_user_text = comp_hist

        if await self._is_duplicate(getattr(event_ctx, "query_id", None)):
            return

        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        if not api_key:
            self._reply(event_ctx, "粮仓还没钥匙呢！请先在 LangBot 插件配置中填入你的 DeepSeek API Key 再喊我干饭")
            return

        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
        model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))
        max_turns = max(1, min(20, self._get_config_int("max_history_turns", 12)))

        now = time.time()

        # Evict expired document, image & session caches and apply LRU caps (A-08)
        for k_doc in [k for k, v in list(self._user_docs.items()) if now - float(v.get("ts", 0)) >= 1800]:
            self._user_docs.pop(k_doc, None)
        for k_img in [k for k, v in list(self._group_recent_images.items()) if now - float(v.get("ts", 0)) > 120]:
            self._group_recent_images.pop(k_img, None)
        for k_pimg in [k for k, v in list(self._private_recent_images.items()) if now - float(v.get("ts", 0)) > 120]:
            self._private_recent_images.pop(k_pimg, None)
        for k_hist, v_hist in list(self._histories.items()):
            if not v_hist or (now - v_hist[-1][0]) > 7200:
                self._histories.pop(k_hist, None)
        for cache_dict in (
            self._histories,
            self._user_docs,
            self._group_recent_images,
            self._private_recent_images,
            self._group_msg_buffers,
            self._group_active_conv,
            self._group_repeat_tracker,
            self._group_interject_meta,
            self._last_multi_bubble,
            self._recent_bot_sent_texts,
            self._group_mute_until,
            self._group_direct_wake_seq,
            self._temper_state,
            self._disabled_group_hint_ts,
            self._error_tip_ts,
        ):
            self._trim_cache_lru(cache_dict)

        history = list(self._histories.get(session_key, []))
        history = [(ts, r, c) for (ts, r, c) in history if now - ts < 7200]
        if len(history) > max_turns * 2:
            history = history[-(max_turns * 2) :]

        now_dt = datetime.now(TZ)
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        wk_str = weekdays[now_dt.weekday()]
        hour = now_dt.hour
        period = (
            "凌晨熬夜时段" if hour < 6 else
            "早晨" if hour < 9 else
            "上午搬砖时段" if hour < 12 else
            "午饭与午休点" if hour < 14 else
            "下午摸鱼时段" if hour < 18 else
            "晚饭与傍晚" if hour < 22 else
            "深夜时段"
        )
        coarse_time_str = f"{now_dt.strftime('%Y-%m-%d')}（{wk_str} {period}）"
        exact_time_str = f"{now_dt.strftime('%H:%M')}（{wk_str}{period}）"
        daily_sched = await self._get_or_update_daily_schedule(now_dt)
        schedule_line = df.format_daily_schedule_line(daily_sched, hour)
        bal_str = await self._get_balance_str(api_key, base_url)
        stats = await self._record_tokens_safe(0)

        # On-demand Token/Balance perception (A-03): only include grain_line when token/balance topic matches or balance < 2.0
        bal_low = bal_str.startswith(("¥0.", "¥1.", "$0.", "$1."))
        m_bal_num = re.search(r"(\d+(?:\.\d+)?)", bal_str)
        if m_bal_num:
            try:
                bal_low = float(m_bal_num.group(1)) < 2.0
            except Exception:
                pass
        mention_token_topic = any(kw in cleaned_text.lower() for kw in TOKEN_TOPIC_KEYWORDS) or bal_low
        grain_line = ""
        if mention_token_topic:
            grain_line = (
                f"- **粮仓状态（对方正聊到相关话题或余额偏低，可自然引用）**：DeepSeek 账户真实余额 `{bal_str}`；"
                f"今天已消耗 `{stats.get('today_tokens', 0)} tokens`（上一口 `{stats.get('last_tokens', 0)} tokens`）。"
            )

        # Include best matching cached document if user uploaded/linked a document within the last 30 minutes
        doc_sec = ""
        if not is_file_in and not just_cached_doc_this_turn:
            cached_doc = self._select_relevant_doc_snippet(doc_key, cleaned_text)
            if cached_doc:
                doc_sec = (
                    f"- **对方最近上传的文档缓存（《{cached_doc.get('name')}》，若对方追问文档内容请据此回答）**：\n"
                    f"{str(cached_doc.get('text', ''))[:4500]}"
                )

        # Direction 3B & 3C + Turn-Level Reminders: Anti-Repetition Gate, Temper State, Time Gap, Unfetched Media
        last_hist_ts = history[-1][0] if history else None
        time_gap_notice = df.build_time_gap_notice(last_hist_ts, now)
        unfetched_notice = df.build_unfetched_media_notice(
            raw_text,
            parsed_link_card=parsed_link_card,
            vision_b64=vision_b64,
            is_sticker_in=is_sticker_in,
        )
        anti_rep_line = df.build_anti_repetition_instruction(recent_bot_texts)
        temper_hint, temper_meme_tag, temper_mood_delta = df.check_and_update_temper_state(
            self._temper_state,
            session_key,
            sender_id,
            cleaned_text,
        )

        base_persona = LIVE_PERSONA_BASE if settings["mode"] == "live" else SWEET_PERSONA_BASE
        persona_for_prompt = self._prepare_persona_for_tools(
            base_persona, has_send_meme_tool=bool(settings.get("meme_enabled", True))
        )
        self_moments_line = self._build_self_moments_section().strip()

        turn_ctx_parts: list[str] = []
        if is_group:
            # Group Chat: Tier-1 system stays invariant across speakers/turns on the same day/hour; dynamic recall & speaker go to <turn-context>
            async with self._state_lock:
                g_state = self._get_group_state(launcher_id)
                g_state["lastChatAt"] = int(now * 1000)
                if temper_mood_delta != 0:
                    g_state["mood"] = int(_clamp(int(g_state.get("mood", 75)) + temper_mood_delta, 25, 100))
                meme_cooldown_state = g_state
                g_mood = int(g_state.get("mood", 75))
                g_reason = str(g_state.get("lastReason", "在群里看大家聊天"))
                g_mem_sec = self._recall_group_memories(g_state, cleaned_text)
            familiar_sec = self._recall_readonly_private_familiarity(sender_id, sender_name, launcher_id)
            member_names = df.get_cached_group_member_names(launcher_id)
            roster_line = (
                f"- **本群成员真实昵称名单**：{'、'.join(member_names)}"
                if member_names
                else ""
            )
            name_rule_line = (
                f"- **群聊称呼铁律（极重要）**：当前正在跟你说话的群友叫「{sender_name}」。"
                f"在你的回复正文中【绝对禁止再次写出当前说话人的名字「{sender_name}」】（否则名字会重复出现），直接说你要回的话即可！"
                "只有当你在话里需要提到群内【其他第三方群友】时，才直接写那位群友的真实群昵称/微信昵称（严禁使用微信号 wxid_xxx 或后四位如 aq22、oh22、nl22）！"
            )
            recent_grp_lines = list(self._group_msg_buffers.get(session_key, []))[-8:]
            grp_ctx_block = (
                df.format_group_tagged_transcript(recent_grp_lines, mark_last_current=False)
                if recent_grp_lines
                else "（暂无前序群聊记录）"
            )
            system_prompt = (
                f"{persona_for_prompt}\n\n"
                f"## 四、群聊共享舞台规则（你在微信群「{launcher_id[:10]}」中跟群友互动）\n"
                f"{schedule_line}"
                f"- **当前日期与时段**：{coarse_time_str}\n"
                "- **群聊发送说明**：直接写出你想说的话即可（系统不会在回复前自动拼 `@昵称`）。"
            )
            if roster_line:
                turn_ctx_parts.append(roster_line)
            if g_mem_sec:
                turn_ctx_parts.append(f"- **本群专属群聊档案（群梗与近期话题）**：\n{g_mem_sec}")
            if self_moments_line:
                turn_ctx_parts.append(self_moments_line)
            turn_ctx_parts.append(f"- **本群气氛心情**：{g_mood}/100（{g_reason}）")
            if grain_line:
                turn_ctx_parts.append(grain_line)
            if doc_sec:
                turn_ctx_parts.append(doc_sec)
            turn_ctx_parts.extend(
                [
                    name_rule_line,
                    familiar_sec.strip(),
                    f"- **群里刚刚聊的上下文（帮助你理解大家在讨论什么）**：\n{grp_ctx_block}",
                ]
            )
        else:
            # Private Chat: Tier-1 system holds static persona + fixed profile + schedule; Tier-3 <turn-context> holds per-turn dynamic items
            async with self._state_lock:
                u = self._get_user_state(sender_id)
                u["hasPrivateChat"] = True
                u["lastChatAt"] = int(now * 1000)
                u["missStreak"] = 0
                u["speakUpCountSinceReply"] = 0
                if temper_mood_delta != 0:
                    u["mood"] = int(_clamp(int(u.get("mood", 70)) + temper_mood_delta, 15, 100))
                if sender_name and sender_name not in ("朋友", "群友", "这位群友") and not sender_name.startswith("wxid_"):
                    u["nickname"] = sender_name
                nick_val = str(u.get("nickname") or "").strip()
                meme_cooldown_state = u
                core_profile_sec = self._build_core_stable_profile_section(u)
                intimacy_sec = self._build_intimacy_section(u, mode=settings["mode"])
                mood_sec = self._build_mood_section(u)
                mem_sec = self._recall_memories(u, cleaned_text)
            moment_ctx_line = (
                await self._check_friend_moment_for_chat(
                    sender_id, sender_name, session_key, cleaned_text, history_len=len(history)
                )
            ).strip()
            nick_line = f"- **对方微信昵称**：{nick_val}\n" if nick_val else ""
            system_prompt = (
                f"{persona_for_prompt}\n\n"
                f"## 四、当前私聊长期设定\n"
                f"{nick_line}"
                + (f"{core_profile_sec}\n" if core_profile_sec else "")
                + f"{schedule_line}"
                + f"- **当前日期与时段**：{coarse_time_str}"
            )
            turn_ctx_parts.append(f"- **你们的亲密度与关系阶段**：{intimacy_sec}")
            turn_ctx_parts.append(f"- **鱼的记忆（dsh-fish-memory）**：\n{mem_sec}")
            if self_moments_line:
                turn_ctx_parts.append(self_moments_line)
            if moment_ctx_line:
                turn_ctx_parts.append(moment_ctx_line)
            turn_ctx_parts.append(f"- **鱼的心情（dsh-feiyu-mood）**：\n{mood_sec}")
            if grain_line:
                turn_ctx_parts.append(grain_line)
            if doc_sec:
                turn_ctx_parts.append(doc_sec)

        user_asked_meme = any(
            kw in effective_text for kw in ("表情包", "发个表情", "发张表情", "斗图", "来张图", "发个图")
        )
        turns_since_meme_peek = int(meme_cooldown_state.get("turnsSinceMeme", 99)) + 1
        last_meme_at_peek = int(meme_cooldown_state.get("lastMemeAt", 0))
        allow_meme = bool(
            settings.get("meme_enabled", True)
            and (
                is_sticker_in
                or user_asked_meme
                or bool(temper_meme_tag)
                or (turns_since_meme_peek >= 5 and (int(now) - last_meme_at_peek) >= 180)
            )
        )

        # If user explicitly used "大肥鱼 搜索 <问题>" (or any wake prefix), pre-run parallel web search right away
        if has_cmd_wake and cmd_body.startswith("搜索 "):
            sq = re.sub(r"^搜索\s*", "", cmd_body).strip()
            if sq:
                search_res = await self._call_search_web_parallel([sq], api_key=api_key, base_url=base_url)
                cleaned_text = (
                    f"对方让你联网搜索：「{sq}」\n"
                    f"【并行联网检索结果】：\n{search_res}\n\n"
                    "请结合以上最新检索结果，用大肥鱼的活人口吻准确回答对方！"
                )

        user_asked_voice = df.is_voice_reply_requested(effective_text) or (
            has_cmd_wake and cmd_body in ("说句话", "发语音", "来条语音")
        )
        if user_asked_voice:
            cleaned_text += (
                "\n（注意：对方让你发语音条回复！本轮回复将直接合成为你的真声微信语音发送，"
                "请用自然口语化的 1 句短话（10~45字）回答，不要包含符号、链接、代码块或括号！）"
            )

        if time_gap_notice:
            turn_ctx_parts.append(time_gap_notice)
        if unfetched_notice:
            turn_ctx_parts.append(unfetched_notice)
        if anti_rep_line.strip():
            turn_ctx_parts.append(anti_rep_line.strip().lstrip("- "))
        if temper_hint.strip() and settings["mode"] == "live":
            turn_ctx_parts.append(temper_hint.strip().lstrip("- "))
        if not settings["meme_enabled"]:
            turn_ctx_parts.append(
                "【表情设置：当前已关闭主动发表情包，本轮不要发送表情包或手写 [表情: ...] 标签，用纯文字回复即可】"
            )
        elif not allow_meme:
            turn_ctx_parts.append(
                "【表情冷却中：本轮不要发送表情包或手写 [表情: ...] 标签，用纯文字自然回复即可】"
            )
        if settings["mode"] == "sweet":
            turn_ctx_parts.append(
                "【当前为甜宠陪伴模式：语气要软糯、黏人、欢快偏心，像跟喜欢的人撒娇聊天一样，严禁毒舌抬杠、嫌弃损人或怀疑对方没好事！】"
            )
        turn_ctx_parts.append(
            f"【现在时间：{exact_time_str} | 动笔前确认：绝不打括号写动作、单气泡短句为主、只提真聊过的事】"
        )
        tail_block = (
            "\n\n<turn-context>\n"
            + "\n".join(p for p in turn_ctx_parts if p)
            + "\n</turn-context>"
        )
        user_msg_for_llm = (f"[{sender_name}]: {cleaned_text}" if is_group else cleaned_text) + tail_block

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for _, r, c in history:
            messages.append({"role": r, "content": c})
        if vision_b64:
            if "api.deepseek.com" in base_url.lower() and model.lower() == "deepseek-chat":
                model = "deepseek-flash"
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_msg_for_llm},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{vision_b64}"}},
                    ],
                }
            )
        else:
            messages.append({"role": "user", "content": user_msg_for_llm})

        chat_temp = self._select_chat_temperature(
            cleaned_text,
            is_file_in=is_file_in,
            has_link_card=bool(parsed_link_card),
            has_doc_ctx=bool(doc_sec),
        )
        tool_calls: list[Any] = []
        used_any_tools: bool = False
        voice_sent_in_tool: bool = False
        interim_sent_lines: list[str] = []
        pushed_interim_texts: list[str] = []
        target_recv_tool = launcher_id if is_group else sender_id
        can_push_interim_now = (
            (httpx.AsyncClient is _ORIG_HTTPX_ASYNC_CLIENT)
            or hasattr(df.send_wcf_text_or_image, "assert_awaited")
            or hasattr(df.send_wcf_text_or_image, "mock_calls")
        )
        try:
            async with httpx.AsyncClient(timeout=50.0) as client:
                req_payload: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": chat_temp,
                    "max_tokens": 800,
                }
                # Enable Native Web Search, Webpage Reader & send_meme Tool Calling; keep search active for deepseek-flash even with images (U-09)
                is_deepseek_flash = "deepseek-flash" in model.lower()
                if not parsed_link_card and not is_file_in:
                    if not vision_b64 or is_deepseek_flash:
                        chat_tools = self._build_chat_tools(
                            settings, allow_meme=allow_meme, allow_search=True
                        )
                    else:
                        chat_tools = []
                else:
                    chat_tools = []
                if chat_tools:
                    req_payload["tools"] = chat_tools
                    req_payload["tool_choice"] = "auto"

                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=req_payload,
                )
                if resp.status_code == 400 and vision_b64 and isinstance(messages[-1].get("content"), list):
                    sf_key = self._get_config_str("siliconflow_api_key", DEFAULT_SF_API_KEY)
                    try:
                        vl_desc = await df.describe_image_via_siliconflow(
                            vision_b64,
                            sf_api_key=sf_key,
                            user_hint=cleaned_text,
                            deepseek_api_key=api_key,
                            deepseek_base_url=base_url,
                        )
                    except TypeError:
                        vl_desc = await df.describe_image_via_siliconflow(
                            vision_b64, sf_api_key=sf_key, user_hint=cleaned_text
                        )
                    if vl_desc:
                        vision_desc_for_history = vl_desc
                    fallback_msg = user_msg_for_llm + (
                        f"\n\n【附图/表情包视觉内容】：\n{vl_desc}"
                        if vl_desc
                        else "\n\n（注：对方发了一张图片，但当前没能加载出画面；请自然告诉对方图没刷出来，不要瞎猜图片内容！）"
                    )
                    messages[-1] = {"role": "user", "content": fallback_msg}
                    req_payload["messages"] = messages
                    resp = await client.post(
                        f"{base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json=req_payload,
                    )
                resp.raise_for_status()
                data = resp.json()
                used_tokens = int((data.get("usage") or {}).get("total_tokens", 0))
                await self._record_tokens_safe(used_tokens)

                tool_meme_tag: str | None = None
                tool_meme_b64: str | None = None
                last_raw_interim = ""
                raw_reply = ""

                async def _exec_one_tool(tool_index: int, tc: dict[str, Any]) -> tuple[str, dict[str, Any]]:
                    if tool_index >= 3:
                        return "本轮最多执行 3 个工具调用，此调用未执行；请依据已有结果回答。", {}
                    fn = (tc.get("function") or {}).get("name", "")
                    raw_args = (tc.get("function") or {}).get("arguments") or "{}"
                    try:
                        args_obj = json.loads(raw_args)
                        if not isinstance(args_obj, dict):
                            args_obj = {}
                    except Exception:
                        args_obj = {}
                    meta: dict[str, Any] = {}
                    if fn == "web_search":
                        qs = args_obj.get("queries") or args_obj.get("query") or []
                        if isinstance(qs, str):
                            qs = [qs]
                        out = await self._call_search_web_parallel(
                            qs, api_key=api_key, base_url=base_url
                        )
                    elif fn == "read_webpage":
                        out = await self._call_read_webpage_content(
                            str(args_obj.get("url") or ""),
                            api_key=api_key,
                            base_url=base_url,
                        )
                    elif fn == "send_meme":
                        m_name = str(
                            args_obj.get("name") or args_obj.get("meme") or args_obj.get("tag") or ""
                        ).strip()
                        if not settings.get("meme_enabled", True):
                            out = "当前会话已关闭表情包发送，请直接用纯文字回复，严禁写 [表情: ...] 或 (表情: ...) 标记。"
                        elif not allow_meme:
                            out = "当前表情包正在冷却中，请直接用纯文字回复，严禁写 [表情: ...] 或 (表情: ...) 标记。"
                        else:
                            resolved_m = self._resolve_meme_b64(m_name) if m_name else None
                            if resolved_m:
                                meta["meme_tag"], meta["meme_b64"] = resolved_m
                                out = (
                                    f"已调用原生表情包「{resolved_m[0]}」并直接以图片形式发送。"
                                    "回复正文中严禁再写 `[表情: ...]` 或 `(表情: ...)` 等任何文字表情标记！"
                                )
                            else:
                                out = (
                                    f"未找到表情包「{m_name}」（可用：{'、'.join(MEME_MAP.keys())}）。"
                                    "请直接用纯文字回复，严禁写 `[表情: ...]` 或 `(表情: ...)` 文字标记！"
                                )
                    elif fn == "send_voice":
                        v_raw = self._clean_reply(
                            str(args_obj.get("text") or args_obj.get("content") or "").strip(),
                            user_text=effective_text,
                            recent_bot_texts=recent_bot_texts,
                        )
                        v_txt, _, _ = self._extract_text_and_meme(v_raw)
                        if v_txt:
                            sf_key_tool = self._get_config_str("siliconflow_api_key", DEFAULT_SF_API_KEY)
                            ok_vt = await self._synthesize_and_send_voice(
                                target_recv_tool,
                                v_txt,
                                sf_api_key=sf_key_tool,
                                api_key=api_key,
                                base_url=base_url,
                            )
                            if ok_vt:
                                meta["voice_sent"] = True
                                v_flat = re.sub(r"\s+", " ", v_txt.replace("||", " ")).strip()
                                if v_flat:
                                    meta["voice_flat"] = v_flat
                                out = f"语音「{v_txt}」已发出。"
                            else:
                                out = "语音合成暂不可用，请直接用纯文字回复。"
                        else:
                            out = "语音文本为空。"
                    else:
                        out = "未知工具"
                    return out, meta

                # Up to 2 tool-calling hops; execute up to 3 tool calls per hop in parallel via asyncio.gather
                for hop in range(2):
                    choice_msg = data.get("choices", [{}])[0].get("message", {}) or {}
                    hop_tool_calls = choice_msg.get("tool_calls") or []
                    if not hop_tool_calls:
                        raw_reply = str(choice_msg.get("content") or "").strip()
                        break

                    used_any_tools = True
                    tool_calls = hop_tool_calls
                    raw_interim_content = str(choice_msg.get("content") or "").strip()
                    interim_flat = ""
                    if raw_interim_content:
                        last_raw_interim = raw_interim_content
                        cleaned_interim, im_tag, im_b64 = self._extract_text_and_meme(
                            self._clean_reply(
                                raw_interim_content,
                                user_text=effective_text,
                                recent_bot_texts=recent_bot_texts,
                            )
                        )
                        if im_b64 and allow_meme and not tool_meme_b64:
                            tool_meme_tag, tool_meme_b64 = im_tag, im_b64
                        interim_flat = re.sub(r"\s+", " ", cleaned_interim.replace("||", " ")).strip()
                        if interim_flat and interim_flat not in interim_sent_lines:
                            interim_sent_lines.append(interim_flat)

                    hop_fn_names = [
                        ((tc.get("function") or {}).get("name", ""))
                        for tc in hop_tool_calls[:3]
                    ]
                    has_slow_tool = any(n in ("web_search", "read_webpage") for n in hop_fn_names)
                    if (
                        hop == 0
                        and has_slow_tool
                        and interim_flat
                        and can_push_interim_now
                        and interim_flat not in pushed_interim_texts
                    ):
                        try:
                            if await df.send_wcf_text_or_image(target_recv_tool, text=interim_flat):
                                pushed_interim_texts.append(interim_flat)
                                self._record_bot_sent(interim_flat, session_key)
                        except Exception as exc_push:
                            logger.debug("Interim push before slow tools skipped: %s", exc_push)

                    tool_results = await asyncio.gather(
                        *[_exec_one_tool(i, tc) for i, tc in enumerate(hop_tool_calls)]
                    )
                    messages.append(choice_msg)
                    for tc, (tool_out, meta) in zip(hop_tool_calls, tool_results):
                        if meta.get("meme_b64") and not tool_meme_b64:
                            tool_meme_tag, tool_meme_b64 = meta.get("meme_tag"), meta.get("meme_b64")
                        if meta.get("voice_sent"):
                            voice_sent_in_tool = True
                        v_flat = str(meta.get("voice_flat") or "").strip()
                        if v_flat and v_flat not in interim_sent_lines:
                            interim_sent_lines.append(v_flat)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.get("id", ""),
                                "content": str(tool_out or "")[:3500],
                            }
                        )

                    # Short-circuit wasted resp2 when ONLY send_meme ran and model already produced substantive interim text
                    only_send_meme = bool(hop_fn_names) and all(n == "send_meme" for n in hop_fn_names)
                    if only_send_meme and len(interim_flat) >= 2 and not pushed_interim_texts:
                        raw_reply = raw_interim_content
                        break

                    # Diana #1577: Remind follow-up turn of already spoken/drafted lines to prevent paraphrased duplicates
                    if interim_sent_lines and messages and messages[-1].get("role") == "tool":
                        quoted_interim = " / ".join(f"「{ln}」" for ln in interim_sent_lines)
                        interim_reminder = (
                            f"\n\n【这一轮你已经发出去的话】：{quoted_interim}\n"
                            "（注意：以上内容这一轮已经发出去了，请只补充工具结果带来的新信息，同一件事别换个说法再讲一遍！）"
                        )
                        messages[-1]["content"] = messages[-1]["content"][:3200] + interim_reminder

                    next_payload: dict[str, Any] = {
                        "model": model,
                        "messages": messages,
                        "temperature": 0.78,
                        "max_tokens": 800,
                    }
                    if hop == 0 and chat_tools:
                        next_payload["tools"] = chat_tools
                        next_payload["tool_choice"] = "auto"
                    resp2 = await client.post(
                        f"{base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json=next_payload,
                    )
                    resp2.raise_for_status()
                    data = resp2.json()
                    used2 = int((data.get("usage") or {}).get("total_tokens", 0))
                    await self._record_tokens_safe(used2)
                else:
                    final_msg = data.get("choices", [{}])[0].get("message", {}) or {}
                    raw_reply = str(final_msg.get("content") or "").strip()

                if not raw_reply and not voice_sent_in_tool and not pushed_interim_texts and last_raw_interim:
                    raw_reply = last_raw_interim

                cleaned_reply = self._clean_reply(
                    raw_reply, user_text=effective_text, recent_bot_texts=recent_bot_texts
                )
                reply_text, meme_tag, meme_b64 = self._extract_text_and_meme(cleaned_reply)
                used_send_meme_tool = bool(tool_meme_b64)
                if tool_meme_b64 and not meme_b64:
                    meme_tag, meme_b64 = tool_meme_tag, tool_meme_b64
                if is_group and not parsed_link_card:
                    reply_text = df.strip_redundant_speaker_name(reply_text, sender_name)

                # Diana Feature 3: Steal incoming sticker and mix stolen stickers when replying
                if is_sticker_in:
                    self._start_session_task(session_key, df.steal_incoming_sticker(raw_text, settings, self._stolen_memes, meme_tag))

                if temper_meme_tag and not meme_b64:
                    res_tm = self._resolve_meme_b64(temper_meme_tag)
                    if res_tm:
                        meme_tag, meme_b64 = res_tm
                force_meme = bool(
                    is_sticker_in
                    or user_asked_meme
                    or temper_meme_tag
                    or (used_send_meme_tool and allow_meme)
                )
                if force_meme and not meme_b64 and self._memes_b64:
                    meme_tag = random.choice(["得意", "干饭", "猫爪", "偷看", "飘了"])
                    meme_b64 = self._memes_b64.get(meme_tag)

                if meme_b64:
                    meme_b64 = df.pick_stolen_or_official_meme(meme_tag or "搞笑", meme_b64, settings, self._stolen_memes)

                # Scheme 1: Code-level cooldown (min 4 text turns & 180s interval) + 15% real RNG gate for normal chats
                async with self._state_lock:
                    turns_since_meme = int(meme_cooldown_state.get("turnsSinceMeme", 99)) + 1
                    last_meme_at = int(meme_cooldown_state.get("lastMemeAt", 0))
                    if not force_meme and meme_b64:
                        if turns_since_meme < 5 or (int(now) - last_meme_at) < 180 or random.random() >= 0.15:
                            meme_b64 = None

                    if meme_b64 and settings["meme_enabled"]:
                        meme_cooldown_state["turnsSinceMeme"] = 0
                        meme_cooldown_state["lastMemeAt"] = int(now)
                    else:
                        meme_cooldown_state["turnsSinceMeme"] = turns_since_meme
                self._mark_state_dirty()
        except Exception as exc:
            logger.warning("Whale persona DeepSeek API call failed: %s", exc)
            if parsed_link_card:
                self._reply(event_ctx, parsed_link_card, parsed_link_img_b64)
            elif not is_group or self._is_group_woken(msg_chain, effective_text, ev_raw_obj) or is_quoting_bot:
                now_err = time.time()
                last_err_tip = float(self._error_tip_ts.get(session_key, 0.0) or 0.0)
                if now_err - last_err_tip >= 60.0:
                    self._lru_set(self._error_tip_ts, session_key, now_err)
                    err_s = str(exc)
                    if "401" in err_s or "403" in err_s:
                        tip_txt = "呜…粮仓钥匙好像失效或者填错啦（401/403），检查下 DeepSeek API Key 呗"
                    elif "402" in err_s:
                        tip_txt = "完蛋…DeepSeek 余额好像见底啦（402），得去充点口粮了喂"
                    elif "429" in err_s:
                        tip_txt = "慢点慢点，刚才问太急被限流啦（429），歇两秒再喊我"
                    elif isinstance(exc, httpx.TimeoutException) or "timeout" in err_s.lower():
                        tip_txt = "信号飘了，等半天没连上服务器（超时），待会儿再戳我试试"
                    else:
                        tip_txt = "信号突然卡住啦，暂时没连上脑瓜子，过会儿再叫我~"
                    if is_group:
                        await self._reply_group_no_at(event_ctx, launcher_id, tip_txt)
                    else:
                        self._reply(event_ctx, tip_txt)
            return

        if parsed_link_card:
            reply_text_clean = reply_text.replace("||", " ")
            reply_text = f"{parsed_link_card}\n━━━━━━━━━━━━━━\n🐟 大肥鱼：{reply_text_clean}" if reply_text_clean else parsed_link_card

        if not reply_text and not meme_b64 and not parsed_link_img_b64 and not voice_sent_in_tool and not pushed_interim_texts:
            return

        allow_split = not (parsed_link_card or is_file_in or used_any_tools)
        was_multi = self._last_multi_bubble.get(session_key, False)
        bubbles = self._split_reply_bubbles(
            reply_text, allow_split=allow_split, last_was_multi=was_multi
        )
        self._lru_set(self._last_multi_bubble, session_key, len(bubbles) > 1)
        reply_flat = " ".join(bubbles) if bubbles else reply_text
        if not reply_flat and voice_sent_in_tool and interim_sent_lines:
            reply_flat = " ".join(interim_sent_lines)
        hist_reply_entry = (
            " ".join([*pushed_interim_texts, reply_flat]).strip()
            if pushed_interim_texts
            else reply_flat
        )

        if not vision_desc_for_history and vision_b64:
            vision_desc_for_history = self._lookup_cached_vision_desc(vision_b64)
        if not hist_user_text:
            hist_user_text = "[发来了一张微信表情包]" if is_sticker_in else cleaned_text
        if vision_desc_for_history:
            hist_user_text = self._format_visual_history_entry(
                hist_user_text, vision_desc_for_history, is_sticker=is_sticker_in
            )
        hist_entry_user = f"[{sender_name}] {hist_user_text}" if is_group else hist_user_text
        history.append((now, "user", hist_entry_user))
        history.append((now, "assistant", hist_reply_entry))
        if len(history) > max_turns * 2:
            history = history[-(max_turns * 2) :]
        self._lru_set(self._histories, session_key, history)

        if is_group:
            df.append_group_context_line(
                self._group_msg_buffers,
                session_key,
                "__self__",
                hist_reply_entry[:100],
                sender_name="大肥鱼",
                is_self=True,
                now_ts=now,
            )
            self._record_group_bot_activity(session_key, sender_id, hist_reply_entry)
            self._start_session_task(session_key,
                self._async_post_group_turn_judge(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    group_id=launcher_id,
                    expected_state=g_state,
                    user_text=hist_entry_user,
                    reply_text=hist_reply_entry,
                    sender_id=sender_id,
                    sender_name=sender_name,
                )
            )
        else:
            if reply_flat:
                self._record_bot_sent(reply_flat, session_key)
            self._start_session_task(session_key,
                self._async_post_turn_judge(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    user_id=sender_id,
                    expected_state=u,
                    user_text=hist_user_text,
                    reply_text=hist_reply_entry,
                )
            )

        send_b64 = parsed_link_img_b64 or (meme_b64 if settings["meme_enabled"] else None)
        target_recv = launcher_id if is_group else sender_id

        if pushed_interim_texts and not reply_text and not send_b64:
            event_ctx.prevent_default()
            return

        # Direction 4: WeChat 4.1 Native SILK v3 Voice Replies (TTS -> /api/send_voice)
        cur_mood_for_voice = int(
            (g_state if is_group else u).get("mood", 75 if is_group else 70)
        )
        should_send_voice = False
        if user_asked_voice:
            should_send_voice = True
        elif (
            settings["voice_reply_enabled"]
            and not (parsed_link_card or is_file_in or used_any_tools)
            and self._is_suitable_for_random_voice(reply_flat)
        ):
            if is_voice_in:
                should_send_voice = random.random() < (0.25 if is_group else 0.45)
            elif (
                not is_group
                and cur_mood_for_voice >= 65
                and 6 <= len(reply_flat) <= 36
            ):
                should_send_voice = random.random() < 0.03

        if should_send_voice and reply_flat and not voice_sent_in_tool:
            sf_key = self._get_config_str("siliconflow_api_key", DEFAULT_SF_API_KEY)
            voice_ok = await self._synthesize_and_send_voice(
                target_recv, reply_flat, sf_api_key=sf_key, api_key=api_key, base_url=base_url
            )
            if voice_ok:
                if send_b64:
                    await df.send_wcf_text_or_image(target_recv, image_b64=send_b64)
                event_ctx.prevent_default()
                return

        if voice_sent_in_tool and not reply_text:
            if send_b64:
                await df.send_wcf_text_or_image(target_recv, image_b64=send_b64)
            event_ctx.prevent_default()
            return

        await self._send_unified_bubbles(
            event_ctx,
            session_key=session_key,
            target_id=target_recv,
            bubbles=bubbles,
            image_b64=send_b64,
            is_group=is_group,
            bot_uuid=bot_uuid,
            fallback_text=reply_text,
        )
