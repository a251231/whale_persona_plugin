"""Command and wake routing: owner authorization helpers, owner moments/friend commands,
group wake/mute detection and the unified prefix command dispatcher.

Extracted from ``components/event_listeners/whale_listener.py`` / ``diana_features.py``
(v0.5.2 A-12). Those modules remain the public compatibility facades: tests and callers keep
patching ``whale_listener`` / ``diana_features`` attributes, which the facades propagate here.
"""
from __future__ import annotations

import json
import langbot_plugin.api.entities.builtin.platform.message as platform_message
import logging
import random
import re
import time
from datetime import datetime
from typing import Any


from langbot_plugin.api.entities import context

from . import df  # live facade proxies (see whale_core.__init__)
from .http_gateway import (
    TZ,
)
from .mood_memory import (
    DIMS,
    _add_dedup_fact,
    _clamp,
    _dedup_facts_in_place,
)
from .state_repo import (
    DEFAULT_API_KEY,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_OWNER_IDS,
    DEFAULT_SF_API_KEY,
)
from .prompts import (
    LIVE_PERSONA_BASE,
    MEME_MAP,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CMD_WAKE_PREFIX_RE",
    "CommandMixin",
    "GROUP_MUTE_EXPLICIT_RE",
    "GROUP_MUTE_PRONOUN_RE",
    "GROUP_MUTE_RE",
    "GROUP_WAKE_RE",
    "IGNORE_PREFIXES",
    "NATURAL_RANDOM_MUSIC_RE",
    "NATURAL_SONG_QUERY_RE",
    "NON_SONG_WORDS",
    "SMS_OR_PHONE_RE",
    "STRIP_TRIGGER_RE",
    "WOKEN_MUTE_RE",
]


# ==============================================================================
# from whale_listener.py
# ==============================================================================
IGNORE_PREFIXES = (
    "问道",
    "道道",
    "vivo",
    "VIVO",
    "Vivo",
    "群聊回复",
    "积分",
    "绑定",
    "签到",
    "补签",
    "周报",
    "道缘",
    "刷道",
    "推广",
    "商店",
    "兑换",
    "<msg",
    "<?xml",
    "<sysmsg",
    "[图片]",
    "[未知类型",
    "[动画表情]",
    "[语音]",
    "[视频]",
    "[文件]",
    "[链接]",
)
SMS_OR_PHONE_RE = re.compile(r"^\d{4,11}$")
STRIP_TRIGGER_RE = re.compile(r"^(?:@[^\s\u2005]+[\s\u2005]*)*(?:ai|AI|大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设)?[,，:：\s]*")
GROUP_WAKE_RE = re.compile(r"(?:^(?:大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设)(?:[,，:：\s]|$))|(?:(?:大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设)[,，:：？\?\!\s]*$)")
GROUP_MUTE_EXPLICIT_RE = re.compile(
    r"(?:大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设).*(?:闭嘴|别说话|禁止.*说话|少插嘴|安静点|别吵|退下|闭麦)|^(?:大肥鱼\s*)?本群禁止你说话$"
)
GROUP_MUTE_PRONOUN_RE = re.compile(
    r"它.*(?:闭嘴|别说话|禁止.*说话|少插嘴|安静点|别吵|退下|闭麦)"
)
GROUP_MUTE_RE = re.compile(
    r"(?:大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设|它).*(?:闭嘴|别说话|禁止.*说话|少插嘴|安静点|别吵|退下|闭麦)|^(?:大肥鱼\s*)?本群禁止你说话$"
)
WOKEN_MUTE_RE = re.compile(
    r"^(?:请你?|你|给我|先)?(?:闭嘴|别说话|安静(?:点|会儿|一下)?|少插嘴|别吵|退下|闭麦|禁止说话|本群禁止你说话)[！!。~～\s]*$"
)
NATURAL_RANDOM_MUSIC_RE = re.compile(
    r"^(?:大肥鱼[,，:：\s]*|鲸鱼娘[,，:：\s]*|小鲸鱼[,，:：\s]*|鲸鱼人设[,，:：\s]*)?(?:我想听歌|想听歌|来一?首歌|放一?首歌|唱一?首歌|播放音乐|随便来一?首歌|我要播放音乐.*)$"
)
NATURAL_SONG_QUERY_RE = re.compile(
    r"^(?:大肥鱼[,，:：\s]*|鲸鱼娘[,，:：\s]*|小鲸鱼[,，:：\s]*|鲸鱼人设[,，:：\s]*)?(?:我想听|想听|给我放|给我来|来一?首|放一?首|播放|点一?首|唱一?首)\s*(?:歌\s*[:：]?\s*)?《?([^》，。！？\?\!\s]{1,24})》?$"
)
NON_SONG_WORDS = ("你说", "你讲", "故事", "笑话", "八卦", "语音", "声音", "大肥鱼", "鲸鱼", "啥", "什么", "歌", "音乐")
CMD_WAKE_PREFIX_RE = re.compile(r"^(?:大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设)\s+(.+)$")


class CommandMixin:
    """``WhaleListener`` methods for commands (mixed into the listener class)."""

    def _is_configured_owner(self, sender_id: str) -> bool:
        if not sender_id:
            return False
        owner_ids_raw = self._get_config_str("owner_ids", DEFAULT_OWNER_IDS).strip()
        if not owner_ids_raw:
            return False
        owners = set(re.split(r"[,，;；\s]+", owner_ids_raw)) - {""}
        return sender_id in owners

    def _format_fish_pond_wall(self, max_items: int = 5) -> tuple[list[str], str | None]:
        moments_list = self._state.get("moments") or []
        if not moments_list:
            return ["📸 大肥鱼的鱼塘朋友圈动态墙目前还是空的哦~"], None
        recent = moments_list[-max_items:]
        lines = [f"📸 大肥鱼的鱼塘朋友圈动态墙（最近 {len(recent)} 条）：\n"]
        for m_item in reversed(recent):
            lines.append(
                f"#{m_item.get('id')} [{m_item.get('at')}] (心情 {m_item.get('mood')}/100 | 🤍 {m_item.get('likes', 0)})\n"
                f"「{m_item.get('text')}」 [配图:{m_item.get('meme')}]\n"
            )
        last_meme = self._resolve_meme_b64(str(recent[-1].get("meme", "干饭")))
        return lines, (last_meme[1] if last_meme else None)

    def _owner_denied_id_hint(self, sender_id: str) -> str:
        owner_ids_raw = self._get_config_str("owner_ids", DEFAULT_OWNER_IDS).strip()
        if not owner_ids_raw and sender_id:
            return f"你当前的账号 ID 为：{sender_id}（请号主将其填入插件配置 owner_ids）"
        return ""

    async def _browse_and_roast_moments(
        self, *, is_group: bool, sender_id: str, session_key: str = ""
    ) -> tuple[str, str | None]:
        """Reads loaded WeChat 4.1 Moments feed in owner private chat and generates Dafeiyu commentary;
        strictly isolates group chats and non-owners to internal fish-pond moments wall.
        """
        if is_group:
            wall_lines, last_meme_b64 = self._format_fish_pond_wall(max_items=5)
            wall_lines.append(
                "💡 群聊里只展示大肥鱼的鱼塘动态墙哦；想让我帮你刷真实微信朋友圈，请由主人在私聊里发送「大肥鱼 刷朋友圈」~"
            )
            return "\n".join(wall_lines), last_meme_b64

        if not self._is_configured_owner(sender_id):
            wall_lines, _ = self._format_fish_pond_wall(max_items=3)
            id_hint = self._owner_denied_id_hint(sender_id)
            extra_line = f"\n{id_hint}" if id_hint else ""
            msg = (
                "哼，真实微信朋友圈只有主人才能让我帮忙刷哦~"
                f"{extra_line}\n给你看看我的鱼塘动态墙吧！\n\n" + "\n".join(wall_lines)
            )
            return msg, None

        ok_m, rows, err_msg = await df.fetch_wcf_moments(limit=8)
        if not ok_m or not rows:
            fallback_lines = [
                f"🐟 刚刚探头去瞄了一眼微信朋友圈，不过暂时没读到列表（{err_msg or '列表为空'}）。",
                "💡 小贴士：请先在电脑微信点开一下「朋友圈」窗口让列表加载，再发「大肥鱼 刷朋友圈」我就能帮你实时锐评啦！",
            ]
            if self._state.get("moments"):
                wall_lines, _ = self._format_fish_pond_wall(max_items=3)
                fallback_lines.append("\n" + "\n".join(wall_lines))
            res_meme = self._resolve_meme_b64("偷看")
            return "\n".join(fallback_lines), (res_meme[1] if res_meme else None)

        if session_key:
            self._lru_set(self._owner_moments_cache, session_key, list(rows[:8]))
        self._cached_wcf_moments = (time.time(), list(rows))

        replied_list = await self._check_and_reply_moment_comments(rows, max_replies=2)
        replied_note = ""
        if replied_list:
            replied_lines = [
                f"• 回评 {r['nick']}（「{r['comment']}」）：{r['reply']}" for r in replied_list
            ]
            replied_note = "\n\n💬 顺手回复了朋友圈新评论：\n" + "\n".join(replied_lines)

        digest = df.format_wcf_moments_digest(rows, max_items=8)
        interact_tip = (
            f"{replied_note}\n\n💡 可发「大肥鱼 点赞朋友圈 1」或「大肥鱼 评论朋友圈 1 [内容]」让我直接去微信朋友圈点赞/神评！"
        )
        res_default = self._resolve_meme_b64("偷看")
        default_meme_b64 = res_default[1] if res_default else None
        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        if api_key:
            sys_prompt = (
                f"{LIVE_PERSONA_BASE}\n\n"
                "现在主人让你帮忙刷一下微信朋友圈。请以「大肥鱼」（毒舌傲娇又护短的小鲸鱼娘）的口吻，"
                "对下面刷到的朋友圈动态做一段「朋友圈锐评」：挑 2~4 条最有意思的动态吐槽或点评，"
                "控制在 120 字以内，不要使用 Markdown 标题或列表符号，不要加括号动作旁白，"
                "可在最末尾附带一个 `[表情: 偷看]` 或 `[表情: 得意]` 等表情标签。"
            )
            user_prompt = f"刚刚刷到的最新微信朋友圈列表：\n{digest}\n\n请给出你的大肥鱼锐评："
            try:
                raw_roast = await self._call_deepseek(
                    sys_prompt, user_prompt, temperature=0.95, max_tokens=260
                )
                if raw_roast:
                    roast_clean, _, roast_meme_b64 = self._extract_text_and_meme(
                        self._clean_reply(raw_roast)
                    )
                    roast_clean = roast_clean.replace("||", " ").strip()
                    if roast_clean:
                        return (
                            f"📱 刚刚帮你刷了最新微信朋友圈：\n{digest}\n\n🐟 大肥鱼锐评：\n{roast_clean}{interact_tip}",
                            roast_meme_b64 or default_meme_b64,
                        )
            except Exception as e:
                logger.warning("Browse moments DeepSeek roast failed: %s", e)

        return (
            f"📱 刚刚帮你刷了最新微信朋友圈：\n{digest}\n\n🐟 啧，大家今天动态还挺热闹嘛！{interact_tip}",
            default_meme_b64,
        )

    async def _handle_owner_moment_interaction_cmd(
        self,
        *,
        is_group: bool,
        sender_id: str,
        session_key: str,
        action: str,
        index_num: int,
        custom_comment: str = "",
    ) -> tuple[str, str | None]:
        """Handles owner-only commands to like or comment on a real WeChat 4.1 Moment by index (#1~#8)."""
        if is_group:
            return "💡 群聊里不能直接操作真实微信朋友圈哦，请由主人在私聊中使用~", None

        if not self._is_configured_owner(sender_id):
            id_hint = self._owner_denied_id_hint(sender_id)
            suffix = f"\n{id_hint}" if id_hint else ""
            return f"哼，只有主人才能让我去真实微信朋友圈点赞或评论哦~{suffix}", None

        rows = self._owner_moments_cache.get(session_key)
        if not rows:
            ok_m, fetched_rows, err_msg = await df.fetch_wcf_moments(limit=8)
            if not ok_m or not fetched_rows:
                return f"🐟 暂时没读到朋友圈列表（{err_msg or '列表为空'}），请先发「大肥鱼 刷朋友圈」加载后再试~", None
            rows = list(fetched_rows[:8])
            if session_key:
                self._lru_set(self._owner_moments_cache, session_key, rows)

        if index_num < 1 or index_num > len(rows):
            return f"当前只缓存了最近 {len(rows)} 条朋友圈，请输入 1~{len(rows)} 之间的序号哦~", None

        target_row = rows[index_num - 1]
        sns_id = str(target_row.get("sns_id") or "").strip()
        nick = str(target_row.get("nickname") or target_row.get("author") or "微信好友").strip()
        m_content = str(target_row.get("content") or "").strip()
        if not sns_id:
            return f"⚠️ 第 #{index_num} 条动态缺少有效的朋友圈 ID，请重新发送「大肥鱼 刷朋友圈」刷新列表。", None

        if action == "like":
            ok, status_code, msg = await df.interact_wcf_moment(sns_id, "like")
            if ok or status_code == "already_liked":
                async with self._state_lock:
                    interacted = self._state.setdefault("interacted_moments", {})
                    rec = interacted.setdefault(sns_id, {"author": str(target_row.get("author") or ""), "ts": int(time.time())})
                    rec["liked"] = True
                    self._mark_state_dirty()
            if status_code == "already_liked":
                return f"❤️ 第 #{index_num} 条（{nick}）的朋友圈刚才已经点过赞啦，再点就把赞取消了，本鱼才不干呢！", None
            if ok:
                res_meme = self._resolve_meme_b64("比心") or self._resolve_meme_b64("得意")
                return f"👍 已帮你在微信朋友圈给 #{index_num}「{nick}」的动态点上真赞啦！", (res_meme[1] if res_meme else None)
            return f"⚠️ 给 #{index_num}「{nick}」点赞未成功（{msg or status_code}），请确认电脑微信已打开过朋友圈列表。", None

        comment_txt = (custom_comment or "").strip()
        if not comment_txt:
            api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
            base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
            model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))
            u = self._get_user_state(sender_id)
            gen_txt, used_tok = await df.generate_moment_comment(
                api_key,
                base_url,
                model,
                author_name=nick,
                moment_content=m_content,
                mode=self._settings_for(sender_id)["mode"],
                existing_comments=target_row.get("comments") if isinstance(target_row.get("comments"), list) else None,
                mood=int(u.get("mood", 75)),
            )
            if used_tok:
                await self._record_tokens_safe(used_tok)
            comment_txt = gen_txt.strip()

        if not comment_txt:
            return "⚠️ 暂时没能生成评论内容，你可以直接带上文字再试一次，例如：「大肥鱼 评论朋友圈 1 周末愉快！」", None

        ok, status_code, msg = await df.interact_wcf_moment(sns_id, "comment", comment_txt)
        if ok:
            async with self._state_lock:
                interacted = self._state.setdefault("interacted_moments", {})
                rec = interacted.setdefault(sns_id, {"author": str(target_row.get("author") or ""), "ts": int(time.time())})
                rec["commented"] = True
                rec["comment"] = comment_txt
                self._mark_state_dirty()
            res_meme = self._resolve_meme_b64("得意")
            return (
                f"💬 已在微信朋友圈 #{index_num}「{nick}」动态下发表评论：\n「{comment_txt}」",
                res_meme[1] if res_meme else None,
            )
        return f"⚠️ 评论 #{index_num}「{nick}」的朋友圈未成功（{msg or status_code}），拟好的评论是：「{comment_txt}」", None

    async def _handle_owner_friend_request_cmd(
        self,
        *,
        is_group: bool,
        sender_id: str,
        session_key: str,
        action: str,
        arg: str = "",
    ) -> tuple[str, str | None]:
        """Handles owner-only commands for viewing, accepting, or configuring WeChat 4.1 friend requests."""
        if is_group:
            return "💡 群聊里不能管理好友申请哦，请由主人在私聊中使用~", None

        if not self._is_configured_owner(sender_id):
            id_hint = self._owner_denied_id_hint(sender_id)
            suffix = f"\n{id_hint}" if id_hint else ""
            return f"哼，只有主人才能管理大肥鱼的微信好友申请哦~{suffix}", None

        local_enabled = bool(self._state.get("auto_accept_friend_enabled", True))
        local_kw = list(self._state.get("friend_verify_keywords") or [])

        if action == "keywords":
            raw_arg = (arg or "").strip()
            if raw_arg in ("清空", "无", "关闭", "全部", "clear", "none"):
                new_kw: list[str] = []
            else:
                new_kw = [w.strip()[:64] for w in re.split(r"[,，;；\s]+", raw_arg) if w.strip()][:16]
            async with self._state_lock:
                self._state["friend_verify_keywords"] = new_kw
                self._mark_state_dirty(immediate=True)
            await df.set_wcf_friend_auto_accept(local_enabled, new_kw)
            if not new_kw:
                return "✅ 已清空好友验证关键词过滤，当前所有入站好友申请将自动通过（需保持自动同意好友开启）。", None
            return f"✅ 已设置好友申请验证关键词：{'、'.join(new_kw)}（仅验证留言包含任一关键词时自动通过）。", None

        if action == "list":
            ok, data, err_msg = await df.fetch_wcf_friend_requests()
            if not ok and not data:
                return (
                    f"🤝 【大肥鱼 · 微信好友申请管理】\n"
                    f"• 自动同意好友：{'开启 ✅' if local_enabled else '关闭 ⏸️'}\n"
                    f"• 验证词过滤：{'、'.join(local_kw) if local_kw else '无限制（收到申请自动通过）'}\n"
                    f"• 网关状态：{err_msg or '暂未连接'}",
                    None,
                )
            digest, reqs = df.format_wcf_friend_requests_digest(
                data, local_enabled=local_enabled, local_keywords=local_kw
            )
            if session_key:
                self._lru_set(self._owner_friend_requests_cache, session_key, reqs)
            return digest, None

        # action == "accept"
        target_arg = (arg or "").strip()
        req_id = ""
        target_row: dict[str, Any] = {}
        cached_reqs = self._owner_friend_requests_cache.get(session_key) or []
        if not cached_reqs:
            ok_f, f_data, _ = await df.fetch_wcf_friend_requests()
            if ok_f and isinstance(f_data, dict):
                cached_reqs = [r for r in (f_data.get("requests") or []) if isinstance(r, dict)][:8]
                if session_key:
                    self._lru_set(self._owner_friend_requests_cache, session_key, cached_reqs)

        if target_arg.isdigit():
            idx = int(target_arg)
            if 1 <= idx <= len(cached_reqs):
                target_row = cached_reqs[idx - 1]
                req_id = str(target_row.get("request_id") or "").strip()
            else:
                return f"⚠️ 当前共有 {len(cached_reqs)} 条好友申请记录，请先发「大肥鱼 好友申请」确认序号后再试~", None
        else:
            req_id = target_arg
            for r in cached_reqs:
                if str(r.get("request_id") or "").startswith(req_id):
                    req_id = str(r.get("request_id") or "")
                    target_row = r
                    break

        if not req_id:
            return "⚠️ 未找到对应的好友申请 ID，请先发送「大肥鱼 好友申请」查看列表~", None

        ok, acc_data, err_msg = await df.accept_wcf_friend_request(req_id)
        nick = str(acc_data.get("nickname") or target_row.get("nickname") or "新好友").strip()
        f_wxid = str(acc_data.get("sender_id") or target_row.get("sender_id") or "").strip()
        verif = str(target_row.get("verification") or "").strip()
        if ok:
            await self._greet_newly_accepted_friend(req_id, f_wxid, nick, verif)
            res_meme = self._resolve_meme_b64("比心") or self._resolve_meme_b64("得意")
            return (
                f"✅ 已通过「{nick}」的好友申请" + (f"（{f_wxid}），并主动发消息打过招呼啦！" if f_wxid else "！"),
                res_meme[1] if res_meme else None,
            )
        return f"⚠️ 通过好友申请失败（{err_msg or '申请可能已过期或已处理'}）。", None

    @staticmethod
    def _should_ignore_plugin_command(text: str) -> bool:
        if not text:
            return True
        stripped = text.strip()
        if not stripped:
            return True
        if (
            stripped in ("[图片]", "[动画表情]", "[语音]", "[语音消息]")
            or "<emoji " in stripped
            or stripped.startswith("<msg><emoji")
            or "<voicemsg " in stripped
            or stripped.startswith("<msg><voicemsg")
            or "<img_b64>" in stripped
            or "<file_b64" in stripped
            or "<type>5</type>" in stripped
            or "<type>6</type>" in stripped
            or "<type>19</type>" in stripped
            or "<recorditem>" in stripped
            or "<type>57</type>" in stripped
            or "<refermsg>" in stripped
        ):
            return False
        if stripped.startswith(IGNORE_PREFIXES):
            return True
        if SMS_OR_PHONE_RE.fullmatch(stripped):
            return True
        if stripped.startswith("{") and stripped.endswith("}"):
            return True
        if "token" in stripped.lower() and ("device" in stripped.lower() or "openid" in stripped.lower()):
            return True
        return False

    def _record_group_bot_activity(self, group_id: str, speaker_id: str, reply_text: str) -> None:
        now = time.time()
        conv = self._group_active_conv.get(group_id)
        active_speakers: set[str] = (
            set(conv.get("active_speakers", set()))
            if isinstance(conv, dict) and (now - float(conv.get("last_bot_ts", 0.0))) < 180.0
            else set()
        )
        if speaker_id:
            active_speakers.add(speaker_id)
        self._lru_set(
            self._group_active_conv,
            group_id,
            {
                "last_bot_ts": now,
                "last_speaker_id": speaker_id,
                "active_speakers": active_speakers,
                "last_bot_reply": (reply_text or "").strip()[:120],
            },
        )
        self._lru_set(self._group_interject_meta, group_id, {"last_judge_ts": now, "unjudged_count": 0})
        self._record_bot_sent(reply_text, group_id)

    def _should_check_active_continuation(
        self,
        group_id: str,
        sender_id: str,
        msg_chain: Any,
        text: str,
        raw_source_obj: Any = None,
        *,
        session_key: str,
    ) -> bool:
        settings = self._settings_for(group_id, is_group=True)
        if not settings["enabled"] or not settings["continuation_enabled"] or settings["explicit_only"]:
            return False
        s = (text or "").strip()
        if not s or len(s) > 200 or s.startswith(df.NON_REPEAT_PREFIXES):
            return False
        if self._should_ignore_plugin_command(s):
            return False
        # Skip if the message explicitly @-mentions another group member (and not the bot)
        has_at_comp = False
        if msg_chain:
            try:
                has_at_comp = any(isinstance(c, platform_message.At) for c in msg_chain)
            except Exception:
                has_at_comp = False
        if (s.startswith("@") or has_at_comp) and not self._is_group_woken(msg_chain, s, raw_source_obj):
            return False
        conv = self._group_active_conv.get(session_key)
        if not isinstance(conv, dict):
            return False
        elapsed = time.time() - float(conv.get("last_bot_ts", 0.0))
        if elapsed < 0 or elapsed > 180.0:
            return False
        last_speaker = str(conv.get("last_speaker_id", ""))
        active_speakers: set[str] = conv.get("active_speakers") or set()
        # 1) Golden Window (75s): the same person (or active participant) who just chatted with Dafeiyu follows up
        if (sender_id == last_speaker or sender_id in active_speakers) and elapsed <= 75.0:
            return True
        # 2) Hot Group Window (20s): another group member speaks right after Dafeiyu spoke
        if elapsed <= 20.0:
            return True
        # 3) Discussion / Referent Window (up to 90s): group members explicitly discussing Dafeiyu
        discussion_kws = (
            "这家伙", "这货", "这鱼", "肥鱼", "鲸鱼", "鲸鱼娘", "小鲸鱼",
            "机器人", "AI", "ai", "人机",
        )
        if elapsed <= 90.0 and any(kw in s for kw in discussion_kws):
            return True
        return False

    @staticmethod
    def _is_group_woken(message_chain: Any, raw_text: str, raw_source_obj: Any = None) -> bool:
        if isinstance(raw_source_obj, dict) and bool(raw_source_obj.get("is_at")):
            return True
        bot_names = tuple(str(n).strip().lower() for n in df.get_dynamic_bot_names() if str(n).strip())
        has_other_at = False
        if message_chain:
            try:
                for comp in message_chain:
                    if isinstance(comp, platform_message.At):
                        target = str(
                            getattr(comp, "target", "")
                            or getattr(comp, "member_id", "")
                            or getattr(comp, "user_id", "")
                            or ""
                        ).strip().lower()
                        display = str(
                            getattr(comp, "display", "")
                            or getattr(comp, "name", "")
                            or getattr(comp, "nickname", "")
                            or ""
                        ).strip().lstrip("@").lower()
                        if not target and not display:
                            return True
                        if (
                            target in bot_names
                            or display in bot_names
                            or any(w in display for w in ("大肥鱼", "鲸鱼娘", "小鲸鱼", "肥鱼"))
                        ):
                            return True
                        has_other_at = True
            except Exception:
                pass
        s = (raw_text or "").strip()
        m_lead = re.match(r"^(?:@[^\s\u2005]+[\s\u2005]*)+", s)
        if m_lead:
            leading_mentions = re.findall(r"@([^\s\u2005,，:：]+)", m_lead.group(0))
            for m_name in leading_mentions:
                if m_name.lower() in bot_names or any(w in m_name for w in ("大肥鱼", "鲸鱼娘", "小鲸鱼", "肥鱼")):
                    return True
                has_other_at = True
        s_no_at = re.sub(r"^(?:@[^\s\u2005]+[\s\u2005]*)+", "", s).strip()
        if has_other_at:
            return bool(re.match(r"^(?:大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设)(?:[,，:：\s]|$)", s_no_at))
        if GROUP_WAKE_RE.match(s) or GROUP_WAKE_RE.match(s_no_at):
            return True
        for bn in bot_names:
            if len(bn) >= 2 and not bn.startswith("wxid_"):
                esc_bn = re.escape(bn)
                if re.match(
                    rf"(?:^(?:{esc_bn})(?:[,，:：\s]|$))|(?:(?:{esc_bn})[,，:：？\?\!\s]*$)",
                    s_no_at,
                    re.IGNORECASE,
                ):
                    return True
        return False

    @staticmethod
    def _is_explicit_at_wake(message_chain: Any, raw_text: str, raw_source_obj: Any = None) -> bool:
        """True only for explicit platform `is_at`, bot `At` component, or `@<bot>` mention."""
        if isinstance(raw_source_obj, dict) and bool(raw_source_obj.get("is_at")):
            return True
        bot_names = tuple(str(n).strip().lower() for n in df.get_dynamic_bot_names() if str(n).strip())
        if message_chain:
            try:
                for comp in message_chain:
                    if isinstance(comp, platform_message.At):
                        target = str(
                            getattr(comp, "target", "")
                            or getattr(comp, "member_id", "")
                            or getattr(comp, "user_id", "")
                            or ""
                        ).strip().lower()
                        display = str(
                            getattr(comp, "display", "")
                            or getattr(comp, "name", "")
                            or getattr(comp, "nickname", "")
                            or ""
                        ).strip().lstrip("@").lower()
                        if not target and not display:
                            return True
                        if (
                            target in bot_names
                            or display in bot_names
                            or any(w in display for w in ("大肥鱼", "鲸鱼娘", "小鲸鱼", "鲸鱼人设", "肥鱼"))
                        ):
                            return True
            except Exception:
                pass
        s = (raw_text or "").strip()
        for m_name in re.findall(r"@([^\s\u2005,，:：]+)", s):
            if m_name.lower() in bot_names or any(w in m_name for w in ("大肥鱼", "鲸鱼娘", "小鲸鱼", "鲸鱼人设", "肥鱼")):
                return True
        return False

    @classmethod
    def _is_explicit_unmute_wake(cls, message_chain: Any, raw_text: str, raw_source_obj: Any = None) -> bool:
        """During 30-min mute cooldown, only unmute on explicit @bot or leading `^(?:@|<bot_name>)`."""
        if cls._is_explicit_at_wake(message_chain, raw_text, raw_source_obj):
            return True
        s = (raw_text or "").strip()
        s_no_at = re.sub(r"^(?:@[^\s\u2005]+[\s\u2005]*)+", "", s).strip()
        bot_names = tuple(
            str(n).strip()
            for n in (*df.get_dynamic_bot_names(), "大肥鱼", "鲸鱼娘", "小鲸鱼", "鲸鱼人设")
            if str(n).strip() and not str(n).strip().lower().startswith("wxid_")
        )
        for bn in bot_names:
            if len(bn) < 2:
                continue
            esc_bn = re.escape(bn)
            if re.match(rf"^(?:{esc_bn})(?:[,，:：\s]|$)", s_no_at, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _strip_wake_prefix(text: str) -> str:
        s = STRIP_TRIGGER_RE.sub("", text or "").strip()
        bot_names = set(df.get_dynamic_bot_names()) | {"大肥鱼", "鲸鱼娘", "小鲸鱼", "鲸鱼人设", "肥鱼"}
        for bn in bot_names:
            bn_str = str(bn).strip()
            if not bn_str:
                continue
            esc = re.escape(bn_str)
            # Strip @<bot_name> or @<bot_wxid> anywhere in the message (e.g. trailing "锐评一下 @Free")
            s = re.sub(rf"@(?:{esc})(?:[\s\u2005,，:：]+|$)", " ", s, flags=re.IGNORECASE).strip()
            if len(bn_str) >= 2 and not bn_str.lower().startswith("wxid_"):
                s = re.sub(
                    rf"^(?:{esc})(?:[,，:：\s]+|$)",
                    "",
                    s,
                    flags=re.IGNORECASE,
                ).strip()
        return s


    async def _dispatch_prefix_command(
        self,
        event_ctx: context.EventContext,
        *,
        is_group: bool,
        launcher_id: str,
        sender_id: str,
        session_key: str,
        doc_key: tuple[str, str, str],
        bot_uuid: str,
        settings: dict[str, Any],
        cmd_norm: str,
        cmd_body: str,
        has_cmd_wake: bool,
        effective_text: str,
    ) -> tuple[bool, str]:
        """Unified prefix command dispatcher (``大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设`` + sub-command).

        Covers voice, memes, moments (strict owner auth for real WeChat Moments), friend requests,
        fish-pond likes, manual speak-up, mood panel, memory (记住 / 忘掉 / 整理档案 / 记忆 / 失忆),
        balance, interjection logs, meme library, music and session reset.

        Returns ``(handled, effective_text)``. ``handled`` means a reply was produced (or the turn is
        otherwise finished) and the chat pipeline must stop; ``effective_text`` may be rewritten, e.g.
        the group ``搭话`` command turns into a prompt for the normal reply path.
        """
        # 1. 手动语音（设置开关已由统一设置入口处理）
        if has_cmd_wake and cmd_body.startswith("语音 "):
            tts_txt = re.sub(r"^语音\s*", "", cmd_body).strip()
            if tts_txt:
                sf_key = self._get_config_str("siliconflow_api_key", DEFAULT_SF_API_KEY)
                target_recv = launcher_id if is_group else sender_id
                ok_v = await self._synthesize_and_send_voice(target_recv, tts_txt, sf_api_key=sf_key)
                if ok_v:
                    self._record_bot_sent(tts_txt, session_key)
                    event_ctx.prevent_default()
                else:
                    self._reply(event_ctx, tts_txt)
                return True, effective_text

        # 2. Meme / Sticker commands (yyh-001/dsh-meme 24 Dafeiyu memes)
        if has_cmd_wake and cmd_body in ("表情包", "斗图", "表情"):
            all_tags = "、".join(MEME_MAP.keys())
            pick_tag = random.choice(list(self._memes_b64.keys())) if self._memes_b64 else "干饭"
            b64 = self._memes_b64.get(pick_tag)
            msg = (
                f"🎨 大肥鱼官方表情包图库（dsh-meme 24张）\n"
                f"随机投喂一张：【{pick_tag}】\n\n"
                f"📌 全部可用表情（共 {len(MEME_MAP)} 款）：\n{all_tags}\n\n"
                f"💡 发送「大肥鱼 表情 <名称>」（如：大肥鱼 表情 干饭）即可指定发送；日常聊天也会根据心情自动配图！"
            )
            self._reply(event_ctx, msg, b64)
            return True, effective_text

        if has_cmd_wake and (cmd_body.startswith("表情 ") or cmd_body.startswith("发图 ")):
            q_name = re.sub(r"^(?:表情|发图)\s*", "", cmd_body).strip()
            resolved = self._resolve_meme_b64(q_name)
            if resolved:
                tag_hit, b64 = resolved
                self._reply(event_ctx, "", b64)
            else:
                all_tags = "、".join(MEME_MAP.keys())
                self._reply(event_ctx, f"没找到「{q_name}」这张表情包哦，目前有这 24 张：{all_tags}")
            return True, effective_text

        # 3. Moments / Cyber Feed commands (strict U-01 owner auth for real WeChat Moments)
        if has_cmd_wake and (
            cmd_body in ("发朋友圈", "写朋友圈")
            or cmd_body.startswith("发朋友圈 ")
            or cmd_body.startswith("写朋友圈 ")
        ):
            if is_group:
                self._reply(
                    event_ctx,
                    "真实微信朋友圈发布仅限号主在私聊里操作哦~ 群里看鱼塘假想动态可发「大肥鱼 朋友圈」。",
                )
                return True, effective_text
            if not self._is_configured_owner(sender_id):
                id_hint = self._owner_denied_id_hint(sender_id)
                if id_hint:
                    self._reply(
                        event_ctx,
                        f"发真实朋友圈仅限主人私聊可用，目前插件配置尚未填写 owner_ids。{id_hint}",
                    )
                else:
                    self._reply(event_ctx, "哼，发真实朋友圈可是主人的专属权限，外人使唤不动本鱼~")
                return True, effective_text
            topic = re.sub(
                r"^(?:@[^\s\u2005]+[\s\u2005]*)*(?:大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设)\s*(?:发朋友圈|写朋友圈)\s*",
                "",
                effective_text.strip(),
            ).strip()
            card_txt, meme_b64 = await self._generate_and_post_moment(topic)
            self._reply(event_ctx, card_txt, meme_b64)
            return True, effective_text

        if (has_cmd_wake and cmd_body in ("朋友圈", "鱼塘朋友圈")) or cmd_norm == "鱼塘朋友圈":
            lines, meme_b64 = self._format_fish_pond_wall()
            lines.append("💡 发「大肥鱼 点赞鱼塘」（或「大肥鱼 点赞」）给鱼塘动态点赞；号主私聊发「大肥鱼 发朋友圈 [主题]」可发真实朋友圈！")
            self._reply(event_ctx, "\n".join(lines), meme_b64 if settings["meme_enabled"] else None)
            return True, effective_text

        if (has_cmd_wake and cmd_body in ("刷朋友圈", "看朋友圈", "看看朋友圈")) or cmd_norm == "刷朋友圈":
            msg_txt, meme_b64 = await self._browse_and_roast_moments(
                is_group=is_group, sender_id=sender_id, session_key=session_key
            )
            self._reply(event_ctx, msg_txt, meme_b64 if settings["meme_enabled"] else None)
            return True, effective_text

        m_like_idx = (
            re.match(r"^(?:点赞朋友圈|赞朋友圈|给朋友圈点赞)\s+(\d+)$", cmd_body)
            if has_cmd_wake
            else None
        )
        if m_like_idx:
            msg_txt, meme_b64 = await self._handle_owner_moment_interaction_cmd(
                is_group=is_group,
                sender_id=sender_id,
                session_key=session_key,
                action="like",
                index_num=int(m_like_idx.group(1)),
            )
            self._reply(event_ctx, msg_txt, meme_b64 if settings["meme_enabled"] else None)
            return True, effective_text

        m_comment_idx = (
            re.match(r"^(?:评论朋友圈|评朋友圈|回复朋友圈)\s+(\d+)(?:\s+(.+))?$", cmd_body)
            if has_cmd_wake
            else None
        )
        if m_comment_idx:
            msg_txt, meme_b64 = await self._handle_owner_moment_interaction_cmd(
                is_group=is_group,
                sender_id=sender_id,
                session_key=session_key,
                action="comment",
                index_num=int(m_comment_idx.group(1)),
                custom_comment=(m_comment_idx.group(2) or "").strip(),
            )
            self._reply(event_ctx, msg_txt, meme_b64 if settings["meme_enabled"] else None)
            return True, effective_text

        if has_cmd_wake and cmd_body in ("好友申请", "查看好友申请", "好友请求"):
            msg_txt, meme_b64 = await self._handle_owner_friend_request_cmd(
                is_group=is_group,
                sender_id=sender_id,
                session_key=session_key,
                action="list",
            )
            self._reply(event_ctx, msg_txt, meme_b64 if settings["meme_enabled"] else None)
            return True, effective_text

        m_accept_friend = (
            re.match(r"^(?:同意好友|通过好友|接受好友|同意好友申请|通过好友申请)\s+(\S+)$", cmd_body)
            if has_cmd_wake
            else None
        )
        if m_accept_friend:
            msg_txt, meme_b64 = await self._handle_owner_friend_request_cmd(
                is_group=is_group,
                sender_id=sender_id,
                session_key=session_key,
                action="accept",
                arg=m_accept_friend.group(1).strip(),
            )
            self._reply(event_ctx, msg_txt, meme_b64 if settings["meme_enabled"] else None)
            return True, effective_text

        m_friend_kw = (
            re.match(r"^(?:好友验证词|好友关键词|自动好友关键词)\s+(.+)$", cmd_body)
            if has_cmd_wake
            else None
        )
        if m_friend_kw:
            msg_txt, meme_b64 = await self._handle_owner_friend_request_cmd(
                is_group=is_group,
                sender_id=sender_id,
                session_key=session_key,
                action="keywords",
                arg=m_friend_kw.group(1).strip(),
            )
            self._reply(event_ctx, msg_txt, meme_b64 if settings["meme_enabled"] else None)
            return True, effective_text

        # U-06: Disambiguate "点赞朋友圈" (no index) from Fish Pond "点赞" / "点赞鱼塘"
        if has_cmd_wake and cmd_body in ("点赞朋友圈", "赞朋友圈", "给朋友圈点赞"):
            self._reply(
                event_ctx,
                "如果要给刷到的微信好友朋友圈点赞，请带上编号：私聊发「大肥鱼 点赞朋友圈 1」；如果要给大肥鱼的鱼塘动态点赞，请发「大肥鱼 点赞鱼塘」或「大肥鱼 点赞」~",
            )
            return True, effective_text

        if has_cmd_wake and cmd_body in ("点赞", "点赞鱼塘", "给鱼塘点赞"):
            async with self._state_lock:
                moments_list = self._state.setdefault("moments", [])
                if is_group:
                    g = self._get_group_state(launcher_id)
                    g["mood"] = int(_clamp(int(g.get("mood", 75)) + 3, 25, 100))
                else:
                    u = self._get_user_state(sender_id)
                    u["mood"] = int(_clamp(int(u.get("mood", 70)) + 3, 15, 100))
                    dims = u.setdefault("dims", {})
                    dims["happy"] = int(_clamp(int(dims.get("happy", 0)) + 5, 0, 100))
                    dims["sweet"] = int(_clamp(int(dims.get("sweet", 0)) + 4, 0, 100))
                    u["lastReason"] = "对方专门给我的鱼塘朋友圈点了赞，心里美滋滋的"
                    u["updatedAt"] = int(time.time() * 1000)
                if moments_list:
                    moments_list[-1]["likes"] = int(moments_list[-1].get("likes", 0)) + 1
                    likes_cnt = moments_list[-1]["likes"]
                    m_id = moments_list[-1].get("id", 1)
                else:
                    likes_cnt = 1
                    m_id = 1
                self._mark_state_dirty(immediate=True)
            resolved = self._resolve_meme_b64("得意")
            self._reply(
                event_ctx,
                f"算你有眼光，朋友圈动态 #{m_id} 收到你的赞啦（当前 {likes_cnt} 赞），心情 +3",
                resolved[1] if resolved else None,
            )
            return True, effective_text

        if has_cmd_wake and cmd_body in ("搭话", "找我说话") and is_group:
            effective_text = "大肥鱼 请结合本群的公开话题和群聊档案，主动跟大家搭句话"
        elif has_cmd_wake and cmd_body in ("搭话", "找我说话"):
            target_recv = sender_id
            reply_su, meme_b64 = await self._trigger_speak_up_for_user(sender_id, manual=True)
            send_meme = meme_b64 if settings["meme_enabled"] else None
            if reply_su:
                bubbles = self._split_reply_bubbles(reply_su)
                if len(bubbles) > 1:
                    self._reply(event_ctx, bubbles[0], None)
                    self._start_session_task(session_key, self._send_followup_bubbles(target_recv, bubbles[1:], send_meme, bot_uuid=bot_uuid))
                else:
                    self._reply(event_ctx, bubbles[0] if bubbles else reply_su, send_meme)
            else:
                self._reply(event_ctx, "这会儿还没配置好钥匙（DeepSeek API Key），或者正犯困呢，等会儿再找你唠")
            return True, effective_text

        # 4. Mood panel command (dsh-feiyu-mood) + U-11 Intimacy Stage & Daily Schedule Summary
        if (has_cmd_wake and cmd_body == "心情") or cmd_norm == "鱼的心情":
            if is_group:
                g = self._get_group_state(launcher_id)
                g_mood = int(g.get("mood", 75))
                now_dt_mood = datetime.now(TZ)
                sched_dict = await self._get_or_update_daily_schedule(now_dt_mood)
                sched_line = df.format_daily_schedule_line(sched_dict, now_dt_mood.hour)
                msg = (
                    f"🐟 大肥鱼在本群当前气氛心情：{g_mood}/100（{self._mood_label(g_mood)}）\n"
                    f"💭 本群状态：{g.get('lastReason', '在群里看大家聊天')}\n"
                    + (f"📅 {sched_line}\n" if sched_line else "")
                    + "（私聊发「大肥鱼 心情」可查看你与大肥鱼的个人八维情绪面板）"
                )
                self._reply(event_ctx, msg)
                return True, effective_text
            u = self._get_user_state(sender_id)
            mood = int(u.get("mood", 70))
            dims = u.get("dims", {})
            dim_lines = []
            for k, label in DIMS:
                val = int(dims.get(k, 0))
                bars = "█" * (val // 10) + "░" * (10 - (val // 10))
                dim_lines.append(f"  {label}：{bars} {val}")
            raw_arch_mood = u.get("archive")
            archive = raw_arch_mood if isinstance(raw_arch_mood, dict) else {}
            facts = u.get("facts") or []
            stable_prof = u.get("stable_profile") or []
            events_list = u.get("events") or []
            total_mem = (
                len(facts)
                + len(stable_prof)
                + len(events_list)
                + sum(
                    len(v) if isinstance(v, list) else (1 if v else 0)
                    for v in archive.values()
                )
            )
            if mood < 35:
                intimacy_stage = "冷战防御（说话别太冲哦）"
            elif total_mem >= 8 or (total_mem >= 4 and mood >= 85):
                intimacy_stage = "偏爱熟透 / 嘴硬心软"
            elif total_mem >= 2 or mood >= 75:
                intimacy_stage = "熟络上心 / 偶尔撒娇"
            else:
                intimacy_stage = "慢热试探 / 嘴硬观察"
            now_dt_mood = datetime.now(TZ)
            sched_dict = await self._get_or_update_daily_schedule(now_dt_mood)
            sched_line = df.format_daily_schedule_line(sched_dict, now_dt_mood.hour)
            tl = u.get("timeline", [])[-3:]
            tl_lines = [f"  - [{item.get('at')}] {item.get('mood')} ({item.get('delta'):+d})：{item.get('reason')}" for item in reversed(tl)]
            msg = (
                f"🐟 大肥鱼当前心情：{mood}/100（{self._mood_label(mood)}）\n"
                f"💞 亲密阶段：{intimacy_stage}（已攒 {total_mem} 条记忆）\n"
                + (f"📅 {sched_line}\n" if sched_line else "")
                + f"💭 心底状态：{u.get('lastReason', '平平常常')}\n\n"
                f"📊 八维情绪（dsh-feiyu-mood）：\n" + "\n".join(dim_lines)
            )
            if tl_lines:
                msg += "\n\n🕒 最近情绪波动：\n" + "\n".join(tl_lines)
            self._reply(event_ctx, msg)
            return True, effective_text

        # 5. Memory & Archive Consolidation commands (Group Archive in Group Chat vs Personal 8-Chapter Archive in Private Chat)
        if has_cmd_wake and (cmd_body.startswith("记住 ") or cmd_body.startswith("记住：") or cmd_body.startswith("记住:")):
            fact_to_add = re.sub(
                r"^(?:@[^\s\u2005]+[\s\u2005]*)*(?:大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设)\s*记住[：:\s]*",
                "",
                effective_text.strip(),
            ).strip()
            if fact_to_add:
                async with self._state_lock:
                    if is_group:
                        g = self._get_group_state(launcher_id)
                        _add_dedup_fact(g.setdefault("facts", []), fact_to_add, max_len=25)
                    else:
                        u = self._get_user_state(sender_id)
                        u["hasPrivateChat"] = True
                        if _add_dedup_fact(u.setdefault("facts", []), fact_to_add, max_len=25):
                            u["factsSinceArchive"] = int(u.get("factsSinceArchive", 0)) + 1
                    self._mark_state_dirty(immediate=True)
                if not is_group:
                    self._flush_memory_recall_cache(sender_id)
                if is_group:
                    self._reply(event_ctx, f"记进咱们群的群聊档案里啦：「{fact_to_add}」")
                else:
                    self._reply(event_ctx, f"记下了记下了：「{fact_to_add}」，我记性好着呢")
                return True, effective_text

        m_forget = (
            re.match(r"^(?:忘掉|忘记|删除记忆|忘掉记忆)[：:\s]*(.+)$", cmd_body)
            if has_cmd_wake
            else None
        )
        if m_forget:
            kw = re.sub(
                r"^(?:@[^\s\u2005]+[\s\u2005]*)*(?:大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设)\s*(?:忘掉|忘记|删除记忆|忘掉记忆)[：:\s]*",
                "",
                effective_text.strip(),
            ).strip() or m_forget.group(1).strip()
            kw_l = kw.lower()
            removed_cnt = 0

            def _entry_contains_kw(entry: Any) -> bool:
                if isinstance(entry, dict):
                    txt = " ".join(
                        str(entry.get(k) or "")
                        for k in ("text", "content", "fact", "date", "at", "summary")
                    )
                    if not txt.strip():
                        txt = json.dumps(entry, ensure_ascii=False)
                    return kw_l in txt.lower()
                return kw_l in str(entry).lower()

            async with self._state_lock:
                if is_group:
                    g = self._get_group_state(launcher_id)
                    for list_key in ("facts", "events"):
                        old_items = g.get(list_key)
                        if isinstance(old_items, list):
                            kept_items = [x for x in old_items if not _entry_contains_kw(x)]
                            removed_cnt += len(old_items) - len(kept_items)
                            g[list_key] = kept_items
                    imps = g.get("member_impressions")
                    if isinstance(imps, dict):
                        sender_imp = imps.get(sender_id)
                        if isinstance(sender_imp, dict) and isinstance(sender_imp.get("tags"), list):
                            old_tags = sender_imp["tags"]
                            kept_tags = [t for t in old_tags if kw_l not in str(t).lower()]
                            removed_cnt += len(old_tags) - len(kept_tags)
                            sender_imp["tags"] = kept_tags
                else:
                    u = self._get_user_state(sender_id)
                    for list_key in ("facts", "events", "stable_profile", "memories"):
                        old_items = u.get(list_key)
                        if isinstance(old_items, list):
                            kept_items = [x for x in old_items if not _entry_contains_kw(x)]
                            removed_cnt += len(old_items) - len(kept_items)
                            u[list_key] = kept_items
                    raw_arch = u.get("archive")
                    archive = dict(raw_arch) if isinstance(raw_arch, dict) else {}
                    arch_changed = False
                    for ak, av in list(archive.items()):
                        if isinstance(av, list):
                            kept_arch = [item for item in av if not _entry_contains_kw(item)]
                            diff_arch = len(av) - len(kept_arch)
                            if diff_arch > 0:
                                removed_cnt += diff_arch
                                archive[ak] = kept_arch
                                arch_changed = True
                        elif isinstance(av, str) and kw_l in av.lower():
                            clauses = [c for c in re.split(r"[；;\n。]+", av) if c.strip() and kw_l not in c.lower()]
                            archive[ak] = "；".join(clauses)
                            removed_cnt += 1
                            arch_changed = True
                    if arch_changed:
                        u["archive"] = archive
                if removed_cnt > 0:
                    self._mark_state_dirty(immediate=True)
            if removed_cnt > 0:
                if not is_group:
                    self._flush_memory_recall_cache(sender_id)
                self._reply(event_ctx, f"好嘞，已经把包含「{kw}」的 {removed_cnt} 条相关记忆划掉啦~")
            else:
                self._reply(event_ctx, f"翻遍记忆本也没找到包含「{kw}」的记录呀（如果想全清空可用「大肥鱼 失忆」）")
            return True, effective_text

        if has_cmd_wake and cmd_body in ("整理档案", "生成档案", "重整档案"):
            if is_group:
                async with self._state_lock:
                    g = self._get_group_state(launcher_id)
                    g["facts"] = _dedup_facts_in_place(list(g.get("facts") or []))
                    self._mark_state_dirty(immediate=True)
                self._reply(event_ctx, self._format_group_archive_card(launcher_id, g))
                return True, effective_text
            api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
            base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
            model = self._get_config_str("deepseek_model", DEFAULT_MODEL)
            _, card_msg = await self._consolidate_user_archive(
                api_key=api_key,
                base_url=base_url,
                model=model,
                user_id=sender_id,
            )
            self._reply(event_ctx, card_msg)
            return True, effective_text

        if (has_cmd_wake and cmd_body in ("记忆", "档案", "群记忆", "群档案")) or cmd_norm == "鱼的记忆":
            if is_group:
                g = self._get_group_state(launcher_id)
                self._reply(event_ctx, self._format_group_archive_card(launcher_id, g))
                return True, effective_text
            u = self._get_user_state(sender_id)
            self._reply(event_ctx, self._format_archive_card(u))
            return True, effective_text

        if has_cmd_wake and cmd_body == "失忆":
            async with self._state_lock:
                self._reset_session(session_key, doc_key)
                if is_group:
                    self._state.setdefault("groups", {}).pop(launcher_id, None)
                else:
                    self._state.get("users", {}).pop(sender_id, None)
                self._mark_state_dirty(immediate=True)
            if is_group:
                self._reply(event_ctx, "行，本群的群聊档案和刚才聊的话已经清空重置啦（大家的私聊记忆不受影响）")
            else:
                self._reply(event_ctx, "行，关于你的长期档案、八章节记忆和刚才聊的话全都清空重置啦，咱们重新认识")
            return True, effective_text

        # 6. Token / Balance command (dsh-whale-widget)
        if has_cmd_wake and cmd_body in ("余额", "粮仓", "饭量"):
            api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
            base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
            bal = await self._get_balance_str(api_key, base_url)
            stats = await self._record_tokens_safe(0)
            msg = (
                f"🍚 大肥鱼粮仓看板（dsh-whale-widget）\n"
                f"• DeepSeek 实时余额：{bal}\n"
                f"• 上一口偷吃白饭：{stats.get('last_tokens', 0)} tokens\n"
                f"• 今日累计干饭：{stats.get('today_tokens', 0)} tokens\n"
                f"• 历史总计干饭：{stats.get('total_tokens', 0)} tokens"
            )
            self._reply(event_ctx, msg)
            return True, effective_text

        # 6.5 Diana 扩展：插话记录、表情库、音乐与搜索
        if has_cmd_wake and cmd_body in ("搭话记录", "插话记录", "决策记录"):
            if not is_group and not self._is_configured_owner(sender_id):
                id_hint = self._owner_denied_id_hint(sender_id)
                extra = f"\n{id_hint}" if id_hint else ""
                self._reply(event_ctx, f"请在群内查看本群搭话记录；跨群记录仅主人私聊可查看{extra}")
            else:
                self._reply(event_ctx, df.format_interject_logs(
                    self._state, bot_uuid=bot_uuid, group_id=launcher_id if is_group else None,
                ))
            return True, effective_text

        if has_cmd_wake and cmd_body in ("表情库", "偷图库"):
            stolen = self._stolen_memes
            if not stolen:
                self._reply(event_ctx, "📦 自带 24 张官方表情包，目前偷图库里还没攒下群友的表情包呢，快发几张表情包喂给我！")
                return True, effective_text
            sample = random.choice(stolen)
            tags_summary = "、".join(
                f"{m.get('at', '')}[{','.join(m.get('tags') or ['搞笑'])}]" for m in stolen[-6:]
            )
            msg = (
                f"📦 【大肥鱼表情包收藏库】\n"
                f"• 官方内置：{len(self._memes_b64)} 张 | 偷图收藏：{len(stolen)}/50 张\n"
                f"• 最近偷图：{tags_summary}\n"
                f"（下面随机甩一张从你们这儿偷学来的表情包！）"
            )
            self._reply(event_ctx, msg, sample.get("b64"))
            return True, effective_text

        # Diana Feature 7: Multi-library Music Player (Explicit command + Natural Language song requests + Random song)
        is_explicit_music = (
            (has_cmd_wake and (cmd_body.startswith("点歌 ") or cmd_body.startswith("来首歌 ")))
            or cmd_norm.startswith("点歌 ")
            or cmd_norm.startswith("来首歌 ")
        )
        is_random_music = bool(NATURAL_RANDOM_MUSIC_RE.match(cmd_norm))
        m_nat_song = None if (is_explicit_music or is_random_music) else NATURAL_SONG_QUERY_RE.match(cmd_norm)
        nat_song_name = (m_nat_song.group(1).strip() if m_nat_song else "")
        if nat_song_name and any(w in nat_song_name for w in NON_SONG_WORDS):
            nat_song_name = ""

        if is_explicit_music or is_random_music or nat_song_name:
            if is_random_music:
                song = await df.pick_random_playable_song()
                song_q = "随机歌曲"
            else:
                song_q = (
                    nat_song_name
                    if nat_song_name
                    else re.sub(
                        r"^(?:(?:大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设)\s*)?(?:点歌|来首歌)\s*",
                        "",
                        cmd_norm,
                    ).strip()
                )
                if not song_q:
                    song = await df.pick_random_playable_song()
                else:
                    song = await df.search_and_fetch_music(song_q)
            if not song:
                self._reply(event_ctx, f"翻遍了网易云和 QQ 音乐都没搜到「{song_q}」，换个歌名或者加上歌手名再试试？")
                return True, effective_text
            target_recv = launcher_id if is_group else sender_id
            card_ok, audio_ok = await df.send_music_to_wechat(target_recv, song)
            cover_b64 = await df._download_image_as_jpeg_b64(song.get("cover_url") or "")
            audio_note = (
                "✅ 已同步将歌曲 MP3 音频文件直接发到聊天窗口，点开就能听！"
                if audio_ok
                else f"⚠️ 该曲目受版权限制无法直接提取 MP3 音频流，已为你提供 {song.get('source')} 歌曲来源页链接。"
            )
            prefix_note = "🎲 顺手给你随机挑了一首好听的：\n" if is_random_music else ""
            msg = (
                f"{prefix_note}🎵 【大肥鱼点歌台 · {song.get('source')}】\n"
                f"• 歌名：{song.get('title')}\n"
                f"• 歌手：{song.get('artist')}\n"
                + (f"• 专辑：{song.get('album')}\n" if song.get("album") else "")
                + f"• 链接：{song.get('page_url')}\n"
                + f"{audio_note}"
            )
            self._reply(event_ctx, msg, None if card_ok else cover_b64)
            return True, effective_text

        # 7. 当前对话重置
        if (has_cmd_wake and cmd_body in ("重置", "清除记忆", "重置对话")) or cmd_norm in ("清除记忆", "重置对话"):
            self._reset_session(session_key, doc_key)
            self._reply(event_ctx, "行啦，刚才聊的这几句翻篇了！长期档案还留着呢（要彻底清空请发「大肥鱼 失忆」），想聊啥直说")
            return True, effective_text

        return False, effective_text
