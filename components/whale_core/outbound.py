"""Outbound delivery: reply cleaning/anti-echo guard, multi-bubble splitting, memes, voice and the
unified WCF-or-LangBot bubble dispatcher.

Extracted from ``components/event_listeners/whale_listener.py`` / ``diana_features.py``
(v0.5.2 A-12). Those modules remain the public compatibility facades: tests and callers keep
patching ``whale_listener`` / ``diana_features`` attributes, which the facades propagate here.
"""
from __future__ import annotations

import asyncio
import base64
import langbot_plugin.api.entities.builtin.platform.message as platform_message
import logging
import os
import re
import time
from collections import deque
from typing import Any


from langbot_plugin.api.entities import context

from . import df  # live facade proxies (see whale_core.__init__)
from .state_repo import (
    DEFAULT_API_KEY,
    DEFAULT_BASE_URL,
    DEFAULT_BOT_UUID,
    DEFAULT_SF_API_KEY,
)
from .prompts import (
    MEME_ALIASES,
    MEME_MAP,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ACTION_ASTERISK_RE",
    "ACTION_PAREN_RE",
    "BOT_PREFIX_RE",
    "INLINE_REPLY_ORDINAL_RE",
    "MD_BULLET_LINE_RE",
    "MEME_TAG_RE",
    "OutboundMixin",
    "PAREN_MEME_TAG_RE",
    "SHORT_REACTION_BUBBLES",
    "SPEAK_UP_META_ECHO_RE",
    "SPEAK_UP_TAG_RE",
    "plain_markdown_bullets",
]


# ==============================================================================
# from diana_features.py
# ==============================================================================
MD_BULLET_LINE_RE = re.compile(r"^(\s*)(?:[-*•])\s+(.+)$")
INLINE_REPLY_ORDINAL_RE = re.compile(r"^第\s*[0-9一二三四五六七八九十百千万]+\s*[步代条项点层阶段章节部分]")


def plain_markdown_bullets(text: str) -> str:
    """Converts top-level Markdown unordered list bullets (- / * / •) into plain-text
    numbered items (1. 2. 3.), while stripping the bullet symbol without double-numbering
    when an item already begins with '第 N 步/代/条' (Diana #1388).
    """
    if not text or "\n" not in text and not text.lstrip().startswith(("- ", "* ", "• ")):
        return text
    lines = text.split("\n")
    number = 0
    for i, line in enumerate(lines):
        m = MD_BULLET_LINE_RE.match(line)
        if not m:
            if not line.strip() or line == line.lstrip(" \t"):
                number = 0
            continue
        indent, item = m.group(1), m.group(2).strip()
        if indent:
            lines[i] = f"{indent}- {item}"
            continue
        if INLINE_REPLY_ORDINAL_RE.match(item):
            lines[i] = item
            continue
        number += 1
        lines[i] = f"{number}. {item}"
    return "\n".join(lines)


# ==============================================================================
# from whale_listener.py
# ==============================================================================
SHORT_REACTION_BUBBLES = frozenset(
    ("？", "?", "？？", "笑死", "不是", "不是吧", "行", "行吧", "好家伙", "啧", "切", "哼", "哇", "卧槽", "天呐", "服了", "救命")
)
_ACTION_WORDS_PATTERN = (
    r"抬头|饭碗|米饭|尾巴|腮帮|喷水|水柱|水花|扒拉|低头|哼了|拍了拍|鼓起|嚼着|咽下|擦了擦|抱起|"
    r"翻了个白眼|叹气|打了个哈欠|歪头|叉腰|戳了戳|鱼鳍|冒泡|吐泡泡|嘟囔|小声|默念|观察|嫌弃|得意|"
    r"委屈|脸红|捂脸|擦汗|星星眼|敲碗|缩回|探头|瞄了|瞥了|清嗓|挠头|摊手|抱臂|晃了晃|眯起|瞪大"
)
ACTION_PAREN_RE = re.compile(rf"[（(][^）)\n]{{0,40}}(?:{_ACTION_WORDS_PATTERN})[^）)\n]*[）)]\s*")
ACTION_ASTERISK_RE = re.compile(rf"(?<!\*)\*[^*\n]{{0,36}}(?:{_ACTION_WORDS_PATTERN})[^*\n]*\*(?!\*)\s*")
BOT_PREFIX_RE = re.compile(
    r"^(?:\[\d{1,2}:\d{2}\s+[^\]]+\]\s*[:：]?\s*|\[?(?:大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设|本鱼)\]?\s*[:：]\s*)+"
)
MEME_TAG_RE = re.compile(r"\[(?:表情|表情包|meme)\s*[:：]\s*([^\]\s]{1,12})\s*\]", re.IGNORECASE)
PAREN_MEME_TAG_RE = re.compile(r"[（(](?:表情|表情包|meme)\s*[:：]\s*([^）)\s]{1,12})\s*[）)]", re.IGNORECASE)
SPEAK_UP_TAG_RE = re.compile(r"</?speak-up\b[^>]*>", re.IGNORECASE)
SPEAK_UP_META_ECHO_RE = re.compile(r"[（(]这个块里的内容是你自己心里冒出来的念头[^）)\n]*[）)]\s*")


class OutboundMixin:
    """``WhaleListener`` methods for outbound (mixed into the listener class)."""

    def _load_bundled_memes(self) -> dict[str, str]:
        loaded: dict[str, str] = {}
        here = os.path.dirname(os.path.abspath(__file__))
        meme_dir = os.path.abspath(os.path.join(here, "..", "..", "assets", "memes"))
        for tag_name, (filename, _) in MEME_MAP.items():
            fpath = os.path.join(meme_dir, filename)
            if os.path.exists(fpath):
                try:
                    with open(fpath, "rb") as f:
                        loaded[tag_name] = base64.b64encode(f.read()).decode("ascii")
                except Exception as e:
                    logger.warning("Failed to load meme %s: %s", fpath, e)
        logger.info("Loaded %d bundled Dafeiyu memes from %s", len(loaded), meme_dir)
        return loaded

    def _resolve_meme_b64(self, name: str) -> tuple[str, str] | None:
        key = name.strip()
        if key in self._memes_b64:
            return key, self._memes_b64[key]
        if key in MEME_ALIASES:
            mapped = MEME_ALIASES[key]
            if mapped in self._memes_b64:
                return mapped, self._memes_b64[mapped]
        for k, b64 in self._memes_b64.items():
            if k in key or key in k:
                return k, b64
        return None

    @staticmethod
    def _sanitize_reply_echo_and_catchphrases(
        reply: str,
        user_text: str = "",
        recent_bot_texts: Any = None,
    ) -> str:
        s = (reply or "").strip()
        if not s:
            return ""
        # 1) Strip leading verbatim echo of user's message opener/catchphrase (len >= 4)
        u_clean = re.sub(r"【对方引用了消息[\s\S]*?并回复】：\s*", "", user_text or "").strip()
        u_clean = re.sub(r"^(?:@[^\s\u2005]+[\s\u2005]*)*(?:大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设)[,，:：\s]*", "", u_clean).strip()
        if u_clean and not u_clean.startswith(("（", "(", "<", "http", "[")):
            cand_segments: list[str] = []
            if 4 <= len(u_clean) <= 36:
                cand_segments.append(u_clean)
            for seg in re.split(r"[，。！？!?、：:;；\s\n~～…]+", u_clean):
                seg = seg.strip("「」『』\"'“”()（）")
                if len(seg) >= 4 and not seg.startswith(("http", "[")):
                    cand_segments.append(seg)
            # Longest match first
            for seg in sorted(set(cand_segments), key=len, reverse=True):
                esc = re.escape(seg)
                m_echo = re.match(
                    rf"^[「『\"“]?\s*{esc}\s*[」』\"”]?[，,。！!？?、：:\s\|]+(.*)$",
                    s,
                    re.S,
                )
                if m_echo:
                    rest = m_echo.group(1).strip()
                    rest_no_meme = PAREN_MEME_TAG_RE.sub("", MEME_TAG_RE.sub("", rest)).strip()
                    if len(rest_no_meme) >= 2:
                        s = rest
                        break

        # 2) Deduplicate consecutive catchphrases within this reply & cap repetitions across recent turns
        s = re.sub(r"(哼|切)([，,！!\s]*\1)+([，,！!\s]*)", r"\1\3", s)
        s = re.sub(r"(笨蛋)([，,！!\s]*\1)+", r"\1", s)
        recent_list = [str(x or "") for x in list(recent_bot_texts or [])[-4:] if x]
        recent_benyu = sum(1 for x in recent_list[-3:] if "本鱼" in x)
        if s.count("本鱼") >= 2 or (recent_benyu >= 1 and "本鱼" in s):
            first_seen = recent_benyu >= 1
            out_parts: list[str] = []
            idx = 0
            while idx < len(s):
                pos = s.find("本鱼", idx)
                if pos < 0:
                    out_parts.append(s[idx:])
                    break
                out_parts.append(s[idx:pos])
                if not first_seen:
                    out_parts.append("本鱼")
                    first_seen = True
                else:
                    out_parts.append("我")
                idx = pos + 2
            s = "".join(out_parts)

        if recent_list:
            last_two = recent_list[-2:]
            for starter in ("哼", "切", "笨蛋"):
                if any(re.match(rf"^(?:{re.escape(starter)})(?:[，,！!\s\|]|$)", prev.strip()) for prev in last_two):
                    m_st = re.match(rf"^{re.escape(starter)}[，,！!\s\|]+(.*)$", s, re.S)
                    if m_st and len(PAREN_MEME_TAG_RE.sub("", MEME_TAG_RE.sub("", m_st.group(1))).strip()) >= 2:
                        s = m_st.group(1).strip()
                        break

        return s.strip()

    @staticmethod
    def _clean_reply(
        text: str,
        user_text: str = "",
        recent_bot_texts: Any = None,
    ) -> str:
        raw_no_tag = SPEAK_UP_META_ECHO_RE.sub("", SPEAK_UP_TAG_RE.sub("", text or "")).strip()
        s = BOT_PREFIX_RE.sub("", raw_no_tag).strip()
        s = ACTION_PAREN_RE.sub("", s)
        s = ACTION_ASTERISK_RE.sub("", s).strip()
        s = df.plain_markdown_bullets(s)
        cleaned = s if s else raw_no_tag
        if user_text or recent_bot_texts is not None:
            cleaned = OutboundMixin._sanitize_reply_echo_and_catchphrases(cleaned, user_text, recent_bot_texts)
        if len(cleaned) <= 45 and cleaned.endswith("。") and not cleaned.endswith("。。"):
            cleaned = cleaned[:-1].strip()
        return cleaned

    @staticmethod
    def _is_suitable_for_random_voice(reply_flat: str) -> bool:
        if not reply_flat:
            return False
        if "大肥鱼 " in reply_flat or "鲸鱼娘 " in reply_flat:
            return False
        low = reply_flat.lower()
        if "http" in low or "www." in low:
            return False
        if "```" in reply_flat or "`" in reply_flat or "\n" in reply_flat:
            return False
        if re.search(r"\d{4,}", reply_flat) is not None:
            return False
        if re.search(r"[A-Za-z_]{5,}", reply_flat) is not None:
            return False
        return True

    @staticmethod
    def _format_data_uri_b64(image_b64: str | None) -> str | None:
        if not image_b64:
            return None
        s = image_b64.strip()
        if not s:
            return None
        if s.startswith("data:image/"):
            return s
        if s.startswith("R0lGOD"):
            return f"data:image/gif;base64,{s}"
        if s.startswith("iVBOR"):
            return f"data:image/png;base64,{s}"
        return f"data:image/jpeg;base64,{s}"

    @staticmethod
    def _split_reply_bubbles(
        text: str,
        *,
        allow_split: bool = True,
        last_was_multi: bool = False,
    ) -> list[str]:
        s = (text or "").strip()
        if not s:
            return []
        if (
            not allow_split
            or "```" in s
            or len(s) > 160
            or "\n- " in s
            or "\n• " in s
            or "\n1." in s
        ):
            return [s.replace("||", " ")]

        if "||" in s:
            raw_parts = [p.strip() for p in s.split("||") if p.strip()]
        else:
            lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
            if len(lines) == 2 and all(len(ln) <= 36 for ln in lines):
                raw_parts = lines
            else:
                raw_parts = [s]

        bubbles: list[str] = []
        for p in raw_parts:
            cp = p[:-1].strip() if (len(p) <= 45 and p.endswith("。") and not p.endswith("。。")) else p
            if cp:
                bubbles.append(cp)
        if not bubbles:
            return [s]

        # Cap at 2 bubbles max
        if len(bubbles) > 2:
            bubbles = [bubbles[0], " ".join(bubbles[1:])]

        # Anti-mechanical rhythm gate:
        # 1) Never split into 2 bubbles on two consecutive turns in the same session
        # 2) Never send a standalone '?' bubble when followed by a second bubble
        # 3) Merge very short 2-part splits (<16 chars total) unless the first bubble is a short reaction word
        if len(bubbles) == 2:
            if bubbles[0] in ("？", "?", "？？", "!", "！", "…", "..."):
                return [bubbles[1]]
            total_chars = len(bubbles[0]) + len(bubbles[1])
            is_reaction_lead = bubbles[0] in SHORT_REACTION_BUBBLES
            if last_was_multi or (total_chars < 16 and not is_reaction_lead):
                return [f"{bubbles[0]} {bubbles[1]}"]

        return bubbles

    async def _send_followup_bubbles(
        self,
        target_id: str,
        bubbles: list[str],
        final_image_b64: str | None = None,
        *,
        is_group: bool = False,
        bot_uuid: str,
    ) -> None:
        target_type = "group" if is_group else "person"
        for b_txt in bubbles:
            delay = min(2.0, max(0.75, len(b_txt) * 0.065))
            await asyncio.sleep(delay)
            sent_wcf = await df.send_wcf_text_or_image(target_id, text=b_txt)
            if not sent_wcf:
                try:
                    chain = platform_message.MessageChain([platform_message.Plain(text=b_txt)])
                    await self.plugin.send_message(
                        bot_uuid=bot_uuid,
                        target_type=target_type,
                        target_id=target_id,
                        message_chain=chain,
                    )
                except Exception as e:
                    logger.debug("Fallback send_message text failed: %s", e)

        if final_image_b64:
            await asyncio.sleep(0.65)
            sent_img = await df.send_wcf_text_or_image(target_id, image_b64=final_image_b64)
            if not sent_img:
                try:
                    uri_b64 = self._format_data_uri_b64(final_image_b64) or final_image_b64
                    chain = platform_message.MessageChain([platform_message.Image(base64=uri_b64)])
                    await self.plugin.send_message(
                        bot_uuid=bot_uuid,
                        target_type=target_type,
                        target_id=target_id,
                        message_chain=chain,
                    )
                except Exception as e:
                    logger.debug("Fallback send_message image failed: %s", e)

    def _extract_text_and_meme(self, text: str) -> tuple[str, str | None, str | None]:
        meme_tag: str | None = None
        meme_b64: str | None = None
        for pattern in (MEME_TAG_RE, PAREN_MEME_TAG_RE):
            if meme_b64 is not None:
                break
            for m in pattern.finditer(text or ""):
                raw_tag = m.group(1).strip()
                resolved = self._resolve_meme_b64(raw_tag)
                if resolved:
                    meme_tag, meme_b64 = resolved
                    break
        cleaned = PAREN_MEME_TAG_RE.sub("", MEME_TAG_RE.sub("", text or "")).strip()
        return cleaned, meme_tag, meme_b64

    async def _synthesize_tts_mp3(
        self,
        text: str,
        *,
        sf_api_key: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> str | None:
        sf_key = (
            sf_api_key
            if sf_api_key is not None
            else self._get_config_str("siliconflow_api_key", DEFAULT_SF_API_KEY)
        )
        ds_key = (
            api_key
            if api_key is not None
            else self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        )
        ds_url = (
            base_url
            if base_url is not None
            else self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL)
        ).rstrip("/")
        try:
            return await df.synthesize_tts_mp3_b64(
                text,
                sf_api_key=sf_key,
                deepseek_api_key=ds_key,
                deepseek_base_url=ds_url,
            )
        except TypeError:
            return await df.synthesize_tts_mp3_b64(text, sf_api_key=sf_key)

    async def _synthesize_and_send_voice(
        self,
        receiver: str,
        text: str,
        *,
        sf_api_key: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> bool:
        sf_key = (
            sf_api_key
            if sf_api_key is not None
            else self._get_config_str("siliconflow_api_key", DEFAULT_SF_API_KEY)
        )
        ds_key = (
            api_key
            if api_key is not None
            else self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        )
        ds_url = (
            base_url
            if base_url is not None
            else self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL)
        ).rstrip("/")
        try:
            return await df.synthesize_and_send_wcf_voice(
                receiver,
                text,
                sf_api_key=sf_key,
                deepseek_api_key=ds_key,
                deepseek_base_url=ds_url,
            )
        except TypeError:
            return await df.synthesize_and_send_wcf_voice(receiver, text, sf_api_key=sf_key)

    async def _send_proactive_wechat(self, target_id: str, text: str, image_b64: str | None = None) -> bool:
        self._clear_ended_invocation()
        clean_text = SPEAK_UP_META_ECHO_RE.sub("", SPEAK_UP_TAG_RE.sub("", text or "")).strip()
        bubbles = self._split_reply_bubbles(clean_text) if clean_text else []
        send_img = image_b64 if self._settings_for(target_id)["meme_enabled"] else None
        if not bubbles and not send_img:
            return False
        if bubbles:
            bot_uuid = self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID)
            self._record_bot_sent(" || ".join(bubbles), f"{bot_uuid}:person:{target_id}:{target_id}")

        # Prefer direct WCF gateway delivery (supports multi-bubble typing rhythm reliably in background tasks)
        if bubbles:
            ok_first = await df.send_wcf_text_or_image(
                target_id, bubbles[0], send_img if len(bubbles) == 1 else None
            )
            if ok_first:
                if len(bubbles) > 1:
                    await self._send_followup_bubbles(
                        target_id, bubbles[1:], send_img,
                        bot_uuid=self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID),
                    )
                return True
        elif send_img:
            if await df.send_wcf_text_or_image(target_id, "", send_img):
                return True

        # Fallback to LangBot plugin send_message API
        try:
            joined_text = "\n".join(bubbles) if bubbles else ""
            comps: list[Any] = []
            if joined_text:
                comps.append(platform_message.Plain(text=joined_text))
            if send_img:
                uri_img = self._format_data_uri_b64(send_img) or send_img
                comps.append(platform_message.Image(base64=uri_img))
            if not comps:
                return False
            chain = platform_message.MessageChain(comps)
            await self.plugin.send_message(
                bot_uuid=self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID),
                target_type="person",
                target_id=target_id,
                message_chain=chain,
            )
            return True
        except Exception:
            return False

    def _record_bot_sent(self, text: str, session_key: str) -> None:
        clean = (text or "").strip()
        if not clean:
            return
        q = self._recent_bot_sent_texts.get(session_key)
        if q is None:
            q = self._lru_set(self._recent_bot_sent_texts, session_key, deque(maxlen=40))
        else:
            self._lru_touch(self._recent_bot_sent_texts, session_key)
        for part in re.split(r"\|\||/", clean):
            p = part.strip()
            if len(p) >= 4:
                q.append(p[:80])

    @staticmethod
    def _reply(event_ctx: context.EventContext, text: str = "", image_b64: str | None = None) -> None:
        comps: list[Any] = []
        if text:
            comps.append(platform_message.Plain(text=text))
        if image_b64:
            uri_b64 = OutboundMixin._format_data_uri_b64(image_b64) or image_b64
            comps.append(platform_message.Image(base64=uri_b64))
        if not comps:
            return
        event_ctx.event.reply_message_chain = platform_message.MessageChain(comps)
        event_ctx.prevent_default()

    async def _reply_group_no_at(
        self,
        event_ctx: context.EventContext,
        group_id: str,
        text: str = "",
        image_b64: str | None = None,
    ) -> None:
        """Sends a group reply directly via WCF gateway without @-mentioning the speaker."""
        if not text and not image_b64:
            return
        ok = await df.send_wcf_text_or_image(group_id, text=text, image_b64=image_b64)
        if ok:
            event_ctx.prevent_default()
        else:
            self._reply(event_ctx, text, image_b64)


    def _record_group_side_reply(
        self,
        session_key: str,
        sender_name: str,
        user_text: str,
        bubbles: list[str],
    ) -> tuple[str, float]:
        """Shared bookkeeping for unprompted group replies (continuation / interjection).

        Records the multi-bubble flag, the user/assistant history pair, the group context line and
        the anti-repeat ledger. Returns ``(joined_log, now_ts)``.
        """
        self._lru_set(self._last_multi_bubble, session_key, len(bubbles) > 1)
        joined_log = " ".join(bubbles)
        now_ts = time.time()
        hist = list(self._histories.get(session_key, []))
        hist.append((now_ts, "user", f"[{sender_name}] {user_text}"))
        hist.append((now_ts, "assistant", joined_log))
        self._lru_set(self._histories, session_key, hist[-24:])
        df.append_group_context_line(
            self._group_msg_buffers,
            session_key,
            "__self__",
            joined_log,
            sender_name="大肥鱼",
            is_self=True,
            now_ts=now_ts,
        )
        self._record_bot_sent(joined_log, session_key)
        return joined_log, now_ts

    async def _send_unified_bubbles(
        self,
        event_ctx: context.EventContext,
        *,
        session_key: str,
        target_id: str,
        bubbles: list[str],
        image_b64: str | None = None,
        is_group: bool,
        bot_uuid: str = "",
        fallback_text: str = "",
    ) -> None:
        """Unified multi-bubble delivery for group continuation, group interjection and main replies.

        * The first bubble goes out immediately: groups via ``self._reply_group_no_at`` (WCF direct
          send, never an ``@nickname`` prefix, LangBot reply as fallback), private chats via
          ``self._reply``.
        * A single bubble carries the image/meme; with several bubbles the image is deferred to the
          end of the follow-up task so it never interrupts the text rhythm.
        * Remaining bubbles are paced by ``self._send_followup_bubbles`` as a session task.

        The hooks are looked up on ``self`` so tests that patch ``_reply_group_no_at`` /
        ``_send_followup_bubbles`` on the listener instance keep intercepting delivery.
        """
        first_txt = bubbles[0] if bubbles else fallback_text
        first_img = image_b64 if len(bubbles) <= 1 else None
        if is_group:
            await self._reply_group_no_at(event_ctx, target_id, first_txt, first_img)
        else:
            self._reply(event_ctx, first_txt, first_img)
        if len(bubbles) > 1:
            self._start_session_task(
                session_key,
                self._send_followup_bubbles(
                    target_id,
                    bubbles[1:],
                    image_b64,
                    is_group=is_group,
                    bot_uuid=bot_uuid,
                ),
            )
