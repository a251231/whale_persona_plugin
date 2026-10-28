from __future__ import annotations

import asyncio
import base64
from collections import deque
from datetime import datetime
import hashlib
import html
import io
from itertools import islice
import ipaddress
import json
import logging
import os
import random
import re
import socket
import time
import urllib.parse
import zipfile
from typing import Any
from zoneinfo import ZoneInfo

import httpx

try:
    from PIL import Image
except Exception:
    Image = None  # type: ignore

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None  # type: ignore

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")

WCF_GATEWAY_URL = os.getenv("WCF_GATEWAY_URL", "http://172.22.0.1:18080")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ==============================================================================
# 1. 随机复读（Diana Random Repeat）
# ==============================================================================

NON_REPEAT_PREFIXES = (
    "大肥鱼",
    "鲸鱼娘",
    "小鲸鱼",
    "肥鱼",
    "点歌",
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
    "<emoji",
    "<voicemsg",
    "[图片]",
    "[未知类型",
    "[动画表情]",
    "[语音]",
    "[视频]",
    "[文件]",
    "[链接]",
    "/",
    "#",
    "!",
    "！",
)


def is_repeatable_plain_text(message_chain: Any, raw_text: str) -> bool:
    if not raw_text:
        return False
    s = raw_text.strip()
    if not s or len(s) > 60:
        return False
    if "@" in s or "http://" in s.lower() or "https://" in s.lower():
        return False
    if s.startswith(NON_REPEAT_PREFIXES):
        return False
    if "<" in s and ">" in s:
        return False
    if message_chain:
        try:
            for comp in message_chain:
                cname = comp.__class__.__name__
                if cname in ("At", "AtAll", "Quote", "Image", "Voice", "File"):
                    return False
        except Exception:
            pass
    return True


def check_group_random_repeat(
    tracker_map: dict[str, dict[str, Any]],
    state: dict[str, Any],
    group_id: str,
    sender_id: str,
    message_chain: Any,
    raw_text: str,
) -> str | None:
    """
    Diana Random Repeat:
    - Default: >= 2 distinct users sending identical plain text consecutively
    - 30% probability per subsequent message after reaching min_users
    - 60s cooldown after a successful repeat; subsequent messages do NOT reset cooldown
    - 0 LLM token usage
    """
    if not state.get("repeat_enabled", True):
        return None
    if not is_repeatable_plain_text(message_chain, raw_text):
        return None

    cfg = state.get("repeat_config") or {}
    min_users = max(2, int(cfg.get("min_users", 2)))
    prob = max(0.01, min(1.0, float(cfg.get("prob", 0.30))))
    cooldown_sec = max(5, int(cfg.get("cooldown_sec", 60)))

    text = raw_text.strip()
    now = time.time()
    st = tracker_map.setdefault(
        group_id,
        {
            "text": "",
            "senders": set(),
            "cooldown_until": 0.0,
            "repeated_this_streak": False,
        },
    )

    if text != st["text"]:
        st["text"] = text
        st["senders"] = {sender_id}
        st["repeated_this_streak"] = False
        return None

    st["senders"].add(sender_id)
    if len(st["senders"]) < min_users:
        return None

    # During cooldown, do NOT reset cooldown and do NOT repeat
    if now < float(st.get("cooldown_until", 0.0)):
        return None

    if st.get("repeated_this_streak", False):
        return None

    if random.random() < prob:
        st["cooldown_until"] = now + cooldown_sec
        st["repeated_this_streak"] = True
        return text

    return None


# ==============================================================================
# 2. 群聊智能主动搭话（Diana Proactive Group Interjection + Decision Log）
# ==============================================================================

GROUP_INTEREST_KEYWORDS = (
    "大肥鱼",
    "肥鱼",
    "鲸鱼",
    "白饭",
    "token",
    "Token",
    "deepseek",
    "DeepSeek",
    "ai",
    "AI",
    "机器人",
    "这家伙",
    "这货",
    "和他聊天",
    "跟他聊天",
    "和它聊天",
    "骚扰",
    "饿了",
    "吃啥",
    "夜宵",
    "奶茶",
    "火锅",
    "烧烤",
    "加班",
    "下班",
    "摸鱼",
    "不想上班",
    "困死",
    "熬夜",
    "失眠",
    "周末",
    "放假",
    "写代码",
    "改bug",
    "崩了",
    "救命",
    "离谱",
    "笑死",
)

GROUP_INTERJECT_SYSTEM = """你是微信群里的「大肥鱼」（平时自称「我」，傲娇或得意时才偶尔自称「本鱼」，绝对禁止使用任何括号动作或神态描写）。
下面是这个微信群刚刚聊的几句话（群友没有直接 @ 你），以及你对这个群的记忆印象。
请你像一个平时在群里潜水的真实群友一样，克制、有眼力见儿地判断现在应该【顺嘴接个梗/神吐槽】还是【保持安静】：
1. 什么时候应该顺嘴插话（should_reply: true）：
   - 如果群友正在议论或调侃你（用「大肥鱼 / 鲸鱼 / 这家伙 / 这货 / 他 / 它 / 机器人 / AI」指代你，或刚转发了你的聊天记录、聊到跟你的互动），一定要自然接梗或傲娇回怼！
   - 当群友聊到特别契合的干饭/美食/夜宵、摸鱼/加班吐槽、熬夜犯困、离谱爆笑梗时，可以顺嘴接一句。
2. 什么时候必须保持安静（should_reply: false，非上述情况默认优先选安静）：
   - 如果最近 1~2 条消息里已经有 `[... 大肥鱼]:` 在无关话题上的主动插话，且群友并没有接你的话或讨论你，则保持安静，绝不自顾自连续刷屏！
   - 群友在聊严肃正事、工作对接、转账、私人烦心事/纠纷、发口令/号码，或者只是普通打招呼/单字/表情，或者没有好玩切入点时，一律保持安静！
3. 活人群友铁律（极重要）：
   - 插话必须像真人群友随手敲出来的一句短评或接梗（6~24字以内，句尾不要打句号，不要端着架子自我介绍，严禁每次都背诵“吃DeepSeek token当大白饭”！）。
   - 称呼铁律：发送群回复时系统会自动在消息最前面加上 `@当前说话人昵称`，因此在你的 reply 正文中【绝对禁止再次写出当前说话人的昵称】（否则昵称会重复出现两次）！只有当你在话里需要提到群内【其他第三方群友】时，才直接写那位群友的真实群昵称/昵称（严禁使用微信号 wxid_xxx 或后四位如 aq22、oh22、nl22）。
   - 当情绪或梗特别契合时，可在 reply 末尾可选附带最多 1 个表情标签（如 `[表情: 干饭]`、`[表情: 得意]`、`[表情: 偷看]`、`[表情: 生气]`、`[表情: 无语]`、`[表情: 比心]`、`[表情: 害羞]`）。
4. 只输出一个合法 JSON 对象，不要代码块，不要多余解释：
{"should_reply": false, "reason": "用一句话写明为什么决定插话，或者为什么决定保持安静", "reply": "如果 should_reply 为 true，写出你要在群里随口接的那句短话（6~24字，严禁使用括号动作和句尾句号，严禁重复写当前说话人昵称，末尾可选带 1 个 [表情: 名称]）；如果为 false 则填空字符串"}"""

GROUP_CONTINUATION_SYSTEM = """你是微信群里的「大肥鱼」（平时自称「我」，傲娇或得意时才偶尔自称「本鱼」，绝对禁止使用任何括号动作或神态描写）。
刚才在这个微信群里，你正在和群友互动聊天（或者群友刚把你的聊天记录发到群里、正在议论你）。
现在群里紧接着冒出了最新的一句话（对方没有特意再点 @），请你结合【群聊最近消息】判断：这句话是不是在继续跟你说话、接你的话茬、或者在群里调侃/议论你？
1. 什么时候必须接话回复（should_reply: true）：
   - 刚刚跟你聊天的群友顺着你上一句话继续追问、吐槽、反驳、感叹、或发无语省略号（例如你刚回完，对方紧接着说“你这样说话很难有朋友”、“。。。”、“哈哈哈哈”、“就是容易心梗”）；
   - 群里有人正在围观或调侃你，用「你 / 他 / 她 / 它 / 这家伙 / 这货 / 这鱼 / 机器人 / AI」指代你（例如“这家伙还主动骚扰人”、“你尴尬的时候就和他聊天”、“让别人知道还有人关心你”）；
   - 群友正在顺着你刚才聊开的话题继续往下互动。
2. 什么时候必须保持安静（should_reply: false）：
   - 群友明显已经结束了跟你的闲聊，转头去跟其他群友聊严肃工作正事（如“程是研发经理”、“物料正常情况下不影响”）、游戏排位、或跟你不相干的私事，此时立刻安静退下。
   - 如果只是一句纯附和的“哈哈哈哈”且你上一句已经把话说满、没必要再硬凑一句时，也可以安静。
3. 活人连续对话铁律（极重要）：
   - 回复必须紧扣对方刚刚说的这句话和上一句的语境自然往下接（比如对方说“你这样说话很难有朋友”，你可以嘴硬回怼；对方发“。。。”，你可以追问“干嘛 一脸无语给谁看呢”；大家调侃“尴尬的时候就和他聊天”，你可以傲娇吐槽自己成缓解尴尬工具鱼了）。
   - 称呼铁律：发送群回复时系统会自动在消息最前面加上 `@当前说话人昵称`，因此在你的 reply 正文中【绝对禁止再次写出当前说话人的昵称】（否则昵称会重复出现两次）！只有当你在话里需要提到群内【其他第三方群友】时，才直接写那位群友的真实群昵称/昵称（严禁使用微信号 wxid_xxx 或后四位如 aq22、oh22、nl22）！
   - 保持微信活人短句（默认 1 条气泡，6~35字左右，句尾不打句号，绝对禁止使用括号动作描写，严禁背诵吃token设定；当情绪或梗特别契合时，可在 reply 末尾可选附带最多 1 个表情标签，如 `[表情: 干饭]`、`[表情: 得意]`、`[表情: 偷看]`、`[表情: 生气]`、`[表情: 无语]`、`[表情: 比心]`、`[表情: 害羞]`）。
4. 只输出一个合法 JSON 对象，不要代码块，不要多余解释：
{"should_reply": true, "reason": "简述判断原因（如：对方顺着我上一句继续吐槽我 / 群友在调侃拿我缓解尴尬）", "reply": "如果 should_reply 为 true，写出你要回复的话（严禁在正文里重复写当前说话人昵称，末尾可选带 1 个 [表情: 名称]）；如果为 false 则填空字符串"}"""


def append_group_context_line(
    group_buffers: dict[str, deque],
    group_id: str,
    sender_id: str,
    raw_text: str,
    sender_name: str = "",
) -> str:
    """Appends a clean group chat line into group_buffers and returns the formatted line (or empty string)."""
    s = (raw_text or "").strip()
    if not s or len(s) > 260 or s.startswith(NON_REPEAT_PREFIXES):
        return ""
    buf = group_buffers.setdefault(group_id, deque(maxlen=12))
    now_str = datetime.now(TZ).strftime("%H:%M")
    display_name = (sender_name or "").strip() or get_cached_display_name(sender_id, group_id)
    if display_name in ("这位群友", "群友") and sender_id and len(sender_id) >= 4:
        display_name = f"这位群友({sender_id[-4:]})"
    line = f"[{now_str} {display_name}]: {s}"
    if not buf or buf[-1] != line:
        buf.append(line)
    return line


def record_group_message_for_interject(
    group_buffers: dict[str, deque],
    group_meta: dict[str, dict[str, Any]],
    group_id: str,
    sender_id: str,
    raw_text: str,
    sender_name: str = "",
    *,
    cooldown_sec: int = 240,
    min_messages: int = 8,
) -> bool:
    """Records a group message and returns True if the LLM interjection judge should run."""
    s = (raw_text or "").strip()
    if not s or len(s) > 200 or s.startswith(NON_REPEAT_PREFIXES):
        return False

    append_group_context_line(group_buffers, group_id, sender_id, s, sender_name=sender_name)

    meta = group_meta.setdefault(group_id, {"last_judge_ts": 0.0, "unjudged_count": 0})
    meta["unjudged_count"] = int(meta.get("unjudged_count", 0)) + 1

    now = time.time()
    # 每群独立冷却；关键词也必须遵守当前档位的冷却时间。
    if now - float(meta.get("last_judge_ts", 0.0)) < cooldown_sec:
        return False

    has_kw = any(kw in s for kw in GROUP_INTEREST_KEYWORDS)
    if has_kw or int(meta.get("unjudged_count", 0)) >= min_messages:
        meta["last_judge_ts"] = now
        meta["unjudged_count"] = 0
        return True

    return False


async def evaluate_group_interject(
    *,
    api_key: str,
    base_url: str,
    model: str,
    group_id: str,
    recent_lines: list[str],
    state: dict[str, Any],
    group_memory_str: str = "",
    mode: str = "live",
    bot_uuid: str = "",
) -> tuple[bool, str, str, int]:
    """
    Calls DeepSeek to decide whether to interject in a group chat and logs the decision & reason.
    Returns (should_reply, reply_text, reason, used_tokens).
    """
    if not api_key or not recent_lines:
        return False, "", "", 0

    chat_context = "\n".join(recent_lines[-8:])
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    mem_block = f"\n【本群记忆档案】：\n{group_memory_str}\n" if group_memory_str else ""
    roster_names = get_cached_group_member_names(group_id)
    roster_block = f"\n【本群成员真实昵称名单】：{'、'.join(roster_names)}\n" if roster_names else ""
    mode_block = (
        "\n【本群当前人设模式】：陪伴甜宠版（语气比平时更软乎亲近、傲娇黏人，可在回复末尾附带一个 [表情: 比心] 或 [表情: 害羞] 等标签）\n"
        if mode == "sweet"
        else ""
    )
    user_prompt = (
        f"当前时间：{now_str}{mem_block}{roster_block}{mode_block}\n"
        f"【群聊最近消息】：\n{chat_context}\n\n"
        "请判断回不回并给出原因（只输出JSON）："
    )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": GROUP_INTERJECT_SYSTEM},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.75,
                    "max_tokens": 220,
                },
            )
            if resp.status_code != 200:
                return False, "", "", 0
            data = resp.json()
            used = int((data.get("usage") or {}).get("total_tokens", 0))
            raw = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            a, b = raw.find("{"), raw.rfind("}")
            if a < 0 or b <= a:
                return False, "", "", used
            obj = json.loads(raw[a : b + 1])
    except Exception as e:
        logger.debug("Group interject judge failed: %s", e)
        return False, "", "", 0

    should_reply = bool(obj.get("should_reply", False))
    reason = str(obj.get("reason") or "").strip()[:120]
    reply = str(obj.get("reply") or "").strip()

    log_entry = {
        "at": datetime.now(TZ).strftime("%m-%d %H:%M:%S"),
        "group": group_id[:10],
        "group_id": group_id,
        "bot_uuid": bot_uuid,
        "decision": "✅ 插话" if (should_reply and reply) else "🤫 安静",
        "trigger": recent_lines[-1][:36] if recent_lines else "",
        "reason": reason or ("觉得话题有趣顺嘴接话" if should_reply else "不打扰群友聊天"),
        "reply": reply[:60] if should_reply else "",
    }
    logs = state.setdefault("interject_logs", [])
    logs.append(log_entry)
    if len(logs) > 30:
        state["interject_logs"] = logs[-30:]

    return should_reply, reply, reason, used


SILICONFLOW_CHAT_URL = "https://api.siliconflow.cn/v1/chat/completions"
SILICONFLOW_VL_MODELS = (
    "Qwen/Qwen3-VL-30B-A3B-Instruct",
    "Qwen/Qwen3-VL-8B-Instruct",
    "Qwen/Qwen3-VL-32B-Instruct",
    "zai-org/GLM-4.5V",
    "Qwen/Qwen2.5-VL-72B-Instruct",
    "Pro/Qwen/Qwen2.5-VL-7B-Instruct",
)
_VISION_DESC_CACHE: dict[str, tuple[float, str]] = {}


def _resolve_sf_api_key(explicit_key: str = "") -> str:
    k = (explicit_key or os.getenv("SILICONFLOW_API_KEY", "")).strip()
    if k:
        return k
    for p in ("/app/data/whale_secrets.json", "/app/data/plugins/whale_secrets.json"):
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict) and d.get("siliconflow_api_key"):
                    return str(d["siliconflow_api_key"]).strip()
        except Exception:
            pass
    return ""


async def describe_image_via_siliconflow(
    image_b64: str,
    sf_api_key: str = "",
    user_hint: str = "",
) -> str:
    """Uses SiliconFlow Qwen2.5-VL to extract OCR text and visual details from an image/sticker
    when the primary chat model (e.g. deepseek-chat on api.deepseek.com) is text-only.
    """
    b64_clean = (image_b64 or "").strip()
    if not b64_clean:
        return ""
    if "," in b64_clean and b64_clean.startswith("data:"):
        b64_clean = b64_clean.split(",", 1)[1].strip()
    key = _resolve_sf_api_key(sf_api_key)
    if not key:
        return ""

    now_ts = time.time()
    cache_key = hashlib.sha256(b64_clean.encode("utf-8", errors="ignore")).hexdigest()
    cached_hit = _VISION_DESC_CACHE.get(cache_key)
    if cached_hit and (now_ts - cached_hit[0]) < 600.0 and cached_hit[1]:
        return cached_hit[1]

    prompt_txt = (
        "请客观、精准、详细地描述这张图片或表情包的全部视觉信息：\n"
        "1. 逐字提取图中的所有文字、数字、聊天截图内容、气泡文字、标题或字幕（OCR）；\n"
        "2. 描述画面中的人物/角色/动物/食物/物品、表情神态、动作细节、场景氛围与笑点/梗点；\n"
        "3. 不要寒暄，直接输出紧凑的视觉还原描述（控制在 220 字以内）。"
    )
    hint_clean = (user_hint or "").strip()
    if hint_clean and hint_clean not in ("[图片]", "[动画表情]"):
        prompt_txt += f"\n（发送者附言参考：{hint_clean[:100]}）"

    data_uri = f"data:image/jpeg;base64,{b64_clean}"
    for vl_model in SILICONFLOW_VL_MODELS:
        try:
            async with httpx.AsyncClient(timeout=22.0) as client:
                r = await client.post(
                    SILICONFLOW_CHAT_URL,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": vl_model,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt_txt},
                                    {"type": "image_url", "image_url": {"url": data_uri}},
                                ],
                            }
                        ],
                        "temperature": 0.2,
                        "max_tokens": 320,
                    },
                )
                if r.status_code == 200:
                    data = r.json() if isinstance(r.json(), dict) else {}
                    desc = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                    if desc:
                        if len(_VISION_DESC_CACHE) > 64:
                            _VISION_DESC_CACHE.clear()
                        _VISION_DESC_CACHE[cache_key] = (now_ts, desc)
                        return desc
        except Exception as e:
            logger.debug("SiliconFlow VL (%s) describe skipped: %s", vl_model, e)
    return ""


async def evaluate_group_active_continuation(
    *,
    api_key: str,
    base_url: str,
    model: str,
    group_id: str,
    sender_id: str,
    current_text: str,
    recent_lines: list[str],
    last_bot_reply: str,
    is_same_speaker: bool,
    state: dict[str, Any],
    group_memory_str: str = "",
    familiarity_str: str = "",
    sender_name: str = "",
    vision_b64: str | None = None,
    mode: str = "live",
    bot_uuid: str = "",
    sf_api_key: str = "",
) -> tuple[bool, str, str, int]:
    """
    Active Conversation Window judge & responder:
    When Dafeiyu just spoke in a group (within 75-95s) and the user (or another group member)
    sends a follow-up message without @-ing, decides whether they are continuing the conversation
    with Dafeiyu (or discussing Dafeiyu) and generates a direct natural reply.
    Returns (should_reply, reply_text, reason, used_tokens).
    """
    if not api_key or not current_text:
        return False, "", "", 0

    display_name = (sender_name or "").strip() or get_cached_display_name(sender_id, group_id)
    chat_context = "\n".join(recent_lines[-8:]) if recent_lines else "（无）"
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    speaker_hint = (
        f"群友「{display_name}」（正是刚刚跟你对话的同一位群友，紧接着追问/回应，未再点 @）"
        if is_same_speaker
        else f"群友「{display_name}」（在你发言后紧接着说话的群友，未点 @）"
    )
    mem_block = f"\n【本群记忆档案】：\n{group_memory_str}\n" if group_memory_str else ""
    fam_block = f"\n{familiarity_str}\n" if familiarity_str else ""
    roster_names = get_cached_group_member_names(group_id)
    roster_block = f"\n【本群成员真实昵称名单】：{'、'.join(roster_names)}\n" if roster_names else ""
    mode_block = (
        "\n【本群当前人设模式】：陪伴甜宠版（语气比平时更软乎亲近、傲娇黏人，可在回复末尾附带一个 [表情: 比心] 或 [表情: 害羞] 等标签）\n"
        if mode == "sweet"
        else ""
    )
    user_prompt = (
        f"当前时间：{now_str}{mem_block}{fam_block}{roster_block}{mode_block}\n"
        f"【你（大肥鱼）上一句在群里的发言】：「{last_bot_reply}」\n"
        f"【当前说话人身份】：{speaker_hint}\n"
        f"【群聊最近上下文】：\n{chat_context}\n\n"
        f"【需要判断的最新这句话】：[{display_name}]: {current_text}\n\n"
        f"请判断这句话是不是在继续跟你聊/吐槽你/议论你，并给出 JSON 结果（注意：你的 reply 正文中绝对不要写出当前说话人的名字「{display_name}」，直接说回复内容即可；只有提到群内其他第三方群友时才写对方真实昵称）："
    )

    use_vl_bridge = bool(vision_b64 and "api.deepseek.com" in (base_url or "").lower())
    if use_vl_bridge and vision_b64:
        vl_desc = await describe_image_via_siliconflow(vision_b64, sf_api_key=sf_api_key, user_hint=current_text)
        if vl_desc:
            user_prompt += f"\n\n【群友附图视觉识别详情】：\n{vl_desc}\n"
        user_content: Any = user_prompt
    else:
        user_content = (
            [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{vision_b64}"}},
            ]
            if vision_b64
            else user_prompt
        )

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": GROUP_CONTINUATION_SYSTEM},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.85,
                    "max_tokens": 240,
                },
            )
            if resp.status_code != 200:
                return False, "", "", 0
            data = resp.json()
            used = int((data.get("usage") or {}).get("total_tokens", 0))
            raw = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            a, b = raw.find("{"), raw.rfind("}")
            if a < 0 or b <= a:
                return False, "", "", used
            obj = json.loads(raw[a : b + 1])
    except Exception as e:
        logger.debug("Group active continuation judge failed: %s", e)
        return False, "", "", 0

    should_reply = bool(obj.get("should_reply", False))
    reason = str(obj.get("reason") or "").strip()[:120]
    reply = strip_redundant_speaker_name(str(obj.get("reply") or "").strip(), display_name)

    log_entry = {
        "at": datetime.now(TZ).strftime("%m-%d %H:%M:%S"),
        "group": group_id[:10],
        "group_id": group_id,
        "bot_uuid": bot_uuid,
        "decision": "💬 接话" if (should_reply and reply) else "🤫 安静",
        "trigger": f"[{display_name}]: {current_text[:28]}",
        "reason": reason or ("识别到正在和我连续对话，自然接话" if should_reply else "群友转向其他话题，保持安静"),
        "reply": reply[:60] if should_reply else "",
    }
    logs = state.setdefault("interject_logs", [])
    logs.append(log_entry)
    if len(logs) > 30:
        state["interject_logs"] = logs[-30:]

    return should_reply, reply, reason, used


def format_interject_logs(state: dict[str, Any], *, bot_uuid: str, group_id: str | None) -> str:
    # 旧日志缺少完整隔离键，无法可靠归属，不参与任何会话的查询。
    logs = [
        item for item in (state.get("interject_logs") or [])
        if bot_uuid and item.get("bot_uuid") == bot_uuid and item.get("group_id")
        and (group_id is None or item.get("group_id") == group_id)
    ]
    if not logs:
        return (
            "📋 【大肥鱼群聊主动搭话决策记录】\n"
            "暂无群聊插话评估记录。\n"
            "（在已开启白名单的微信群里，大肥鱼会自动观察群聊并判断是否插话，记录「回/不回」及原因）"
        )
    lines = ["📋 【大肥鱼群聊插话决策记录（最近 8 条）】"]
    for item in reversed(logs[-8:]):
        lines.append(
            f"• {item.get('at')} [{item.get('group')}] {item.get('decision')}\n"
            f"  触发：{item.get('trigger')}\n"
            f"  原因：{item.get('reason')}"
            + (f"\n  回复：「{item.get('reply')}」" if item.get("reply") else "")
        )
    return "\n".join(lines)


# ==============================================================================
# 3. 表情包偷图与复用（Diana Sticker Collection & Reuse）
# ==============================================================================

ALL_MEME_CATEGORIES = [
    "干饭", "生气", "得意", "躺平", "无语", "嫌弃", "加班", "睡觉",
    "比心", "害羞", "花痴", "要钱", "问号", "震惊", "偷看", "求饶",
    "飘了", "OK", "不要", "猫爪", "加载", "惊吓", "晕", "早上好", "搞笑",
]

STOLEN_MEMES_FILE = os.getenv("WHALE_STOLEN_MEMES_FILE", "/app/data/whale_stolen_memes.json")
STOLEN_MEMES_BACKUP_FILE = os.getenv(
    "WHALE_STOLEN_MEMES_BACKUP_FILE", "/app/data/plugins/whale_stolen_memes.json"
)


def load_stolen_memes(legacy_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Loads stolen memes from dedicated JSON file (and migrates any legacy inline stolen_memes)."""
    memes: list[dict[str, Any]] = []
    for candidate in (STOLEN_MEMES_FILE, STOLEN_MEMES_BACKUP_FILE):
        try:
            if candidate and os.path.exists(candidate):
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list) and data:
                        memes = data
                        break
        except Exception as e:
            logger.warning("Failed to load stolen memes file (%s): %s", candidate, e)

    if legacy_state and isinstance(legacy_state.get("stolen_memes"), list):
        legacy_list = legacy_state.pop("stolen_memes", [])
        existing_md5 = {m.get("md5") for m in memes if isinstance(m, dict)}
        for item in legacy_list:
            if isinstance(item, dict) and item.get("md5") not in existing_md5:
                memes.append(item)
        if legacy_list:
            save_stolen_memes(memes[-50:])
    return memes[-50:]


def save_stolen_memes(memes: list[dict[str, Any]]) -> None:
    trimmed = memes[-50:]
    for target_path in (STOLEN_MEMES_FILE, STOLEN_MEMES_BACKUP_FILE):
        if not target_path:
            continue
        if target_path == STOLEN_MEMES_BACKUP_FILE and not os.path.isdir(os.path.dirname(target_path)):
            continue
        try:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            tmp = f"{target_path}.{os.getpid()}.{random.randint(10000, 99999)}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(trimmed, f, ensure_ascii=False)
            os.replace(tmp, target_path)
        except Exception as e:
            logger.warning("Failed to save stolen memes (%s): %s", target_path, e)


async def steal_incoming_sticker(
    xml_text: str,
    state: dict[str, Any],
    stolen_list: list[dict[str, Any]],
    hint_tag: str | None = None,
) -> bool:
    """Downloads a clean static JPEG of an incoming WeChat sticker and persists it to STOLEN_MEMES_FILE."""
    if not state.get("steal_meme_enabled", True) or Image is None:
        return False

    m_url = re.search(r'cdnurl="([^"]+)"', xml_text or "")
    if not m_url:
        return False
    cdn_url = html.unescape(m_url.group(1)).strip()
    if not cdn_url.startswith("http"):
        return False

    m_md5 = re.search(r'md5="([^"]+)"', xml_text or "")
    sticker_md5 = m_md5.group(1).strip() if m_md5 else ""

    if sticker_md5 and any(item.get("md5") == sticker_md5 for item in stolen_list):
        if hint_tag:
            updated = False
            for item in stolen_list:
                if item.get("md5") == sticker_md5 and hint_tag not in item.get("tags", []):
                    item.setdefault("tags", []).append(hint_tag)
                    updated = True
            if updated:
                await asyncio.to_thread(save_stolen_memes, stolen_list)
        return False

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await _get_public_url(client, cdn_url)
            if r.status_code != 200 or not r.content:
                return False
            raw_bytes = r.content
    except Exception:
        return False

    if not sticker_md5:
        sticker_md5 = hashlib.md5(raw_bytes).hexdigest()
        if any(item.get("md5") == sticker_md5 for item in stolen_list):
            return False

    try:
        # Preserve animated GIF directly if within 1.8MB so WeChat sends it as a real animated sticker
        if raw_bytes[:6] in (b"GIF87a", b"GIF89a") and len(raw_bytes) <= 1_800_000:
            single_b64 = base64.b64encode(raw_bytes).decode("ascii")
            is_gif = True
        else:
            img = Image.open(io.BytesIO(raw_bytes))
            n_frames = getattr(img, "n_frames", 1)
            if n_frames > 1:
                img.seek(n_frames // 2)
            rgb = img.convert("RGB")
            rgb.thumbnail((300, 300))
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=84)
            single_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            is_gif = False
    except Exception:
        return False

    tags = [hint_tag] if hint_tag else ["搞笑", "得意"]
    stolen_list.append(
        {
            "md5": sticker_md5,
            "b64": single_b64,
            "is_gif": is_gif,
            "tags": tags,
            "at": datetime.now(TZ).strftime("%m-%d %H:%M"),
        }
    )
    if len(stolen_list) > 50:
        del stolen_list[:-50]
    await asyncio.to_thread(save_stolen_memes, stolen_list)
    return True


def pick_stolen_or_official_meme(
    tag: str,
    official_b64: str | None,
    state: dict[str, Any],
    stolen_list: list[dict[str, Any]],
) -> str | None:
    """With 45% probability when stolen memes exist, picks a matching stolen sticker."""
    if not stolen_list or not state.get("steal_meme_enabled", True):
        return official_b64

    matching = [m for m in stolen_list if tag in (m.get("tags") or [])]
    if matching and random.random() < 0.50:
        return random.choice(matching).get("b64") or official_b64
    if random.random() < 0.30:
        return random.choice(stolen_list).get("b64") or official_b64
    return official_b64


# ==============================================================================
# 4. 联网搜索 + 网页深度阅读（Diana Parallel Web Search & Reader）
# ==============================================================================

DEEPSEEK_SEARCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "当用户询问时效性问题（近期新闻、最新价格/股价、比赛结果、刚发布的模型/软件/游戏攻略、实时百科等）时，并行搜索多个关键词获取最新网页结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "并行搜索的关键词列表（1~3个查询词）",
                    }
                },
                "required": ["queries"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_webpage",
            "description": "读取指定网页 URL 的正文内容，用于深入查看搜索结果中的某个相关官网或文章详情。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要读取正文的网页完整 URL",
                    }
                },
                "required": ["url"],
            },
        },
    },
]


async def _search_single_query(query: str, search_api_key: str = "") -> str:
    q = (query or "").strip()
    if not q:
        return ""

    # 1. Optional Tavily API if search_api_key starts with tvly-
    if search_api_key.startswith("tvly-"):
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.post(
                    "https://api.tavily.com/search",
                    json={"api_key": search_api_key, "query": q, "max_results": 5},
                )
                if r.status_code == 200:
                    results = r.json().get("results") or []
                    if results:
                        lines = [f"【搜索：{q}】"]
                        for idx, item in enumerate(results[:5], 1):
                            lines.append(
                                f"{idx}. {item.get('title')} ({item.get('url')})\n"
                                f"   摘要：{str(item.get('content') or '')[:180]}"
                            )
                        return "\n".join(lines)
        except Exception as e:
            logger.debug("Tavily search fallback: %s", e)

    # 2. Built-in Bing CN + Sogou HTML Search (zero-key)
    results_out: list[str] = []
    try:
        encoded = urllib.parse.quote(q)
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        ) as client:
            r = await client.get(f"https://cn.bing.com/search?q={encoded}&ensearch=0")
            if r.status_code == 200 and BeautifulSoup is not None:
                soup = BeautifulSoup(r.text, "html.parser")
                for li in soup.select("li.b_algo")[:5]:
                    a_tag = li.select_one("h2 a")
                    p_tag = li.select_one(".b_caption p") or li.select_one("p")
                    if a_tag:
                        title = a_tag.get_text(strip=True)
                        href = a_tag.get("href", "")
                        snippet = p_tag.get_text(" ", strip=True) if p_tag else ""
                        if title and href.startswith("http"):
                            results_out.append(f"- {title}\n  链接: {href}\n  摘要: {snippet[:180]}")
    except Exception as e:
        logger.debug("Bing search error: %s", e)

    if not results_out:
        try:
            encoded = urllib.parse.quote(q)
            async with httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                r = await client.get(f"https://www.sogou.com/web?query={encoded}")
                if r.status_code == 200 and BeautifulSoup is not None:
                    soup = BeautifulSoup(r.text, "html.parser")
                    for vr in soup.select("div.vrwrap, div.rb")[:5]:
                        h3 = vr.select_one("h3.vr-title, h3.pt")
                        desc = vr.select_one("div.str-text-info, div.ft, p.str_info")
                        if h3:
                            title = h3.get_text(strip=True)
                            snippet = desc.get_text(" ", strip=True) if desc else ""
                            if title:
                                results_out.append(f"- {title}\n  摘要: {snippet[:180]}")
        except Exception as e:
            logger.debug("Sogou search error: %s", e)

    if not results_out:
        return f"【搜索：{q}】未检索到直接匹配的网页摘要。"
    return f"【搜索：{q}】\n" + "\n".join(results_out[:5])


async def search_web_parallel(queries: list[str], search_api_key: str = "") -> str:
    clean_qs = [str(q).strip() for q in (queries or []) if str(q).strip()][:3]
    if not clean_qs:
        return "无搜索词"
    tasks = [_search_single_query(q, search_api_key) for q in clean_qs]
    parts = await asyncio.gather(*tasks, return_exceptions=True)
    out = [str(p) for p in parts if isinstance(p, str) and p.strip()]
    return "\n\n".join(out) if out else "未获取到搜索结果"


def _is_safe_public_url(url: str) -> bool:
    """Blocks SSRF attempts against loopback, private RFC1918, or cloud metadata IPs."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").strip().lower()
        if not host or host in ("localhost", "0.0.0.0") or host.endswith(".local") or host.endswith(".internal"):
            return False
        addresses = socket.getaddrinfo(host, None)
        if not addresses:
            return False
        for info in addresses:
            ip_str = info[4][0]
            ip_obj = ipaddress.ip_address(ip_str)
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
                or not ip_obj.is_global
            ):
                return False
        return True
    except Exception:
        return False


async def _get_public_url(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """逐跳检查公网地址，禁止客户端自动跟随到内网或无限重定向。"""
    request = client.build_request("GET", url, **kwargs)
    for _ in range(6):
        if not await asyncio.to_thread(_is_safe_public_url, str(request.url)):
            raise ValueError("无效或受限的 URL（仅允许访问公网 HTTP/HTTPS 页面）")
        response = await client.send(request, follow_redirects=False)
        if response.next_request is None:
            return response
        request = response.next_request
    raise httpx.TooManyRedirects("公网请求重定向次数超限", request=request)


async def read_webpage_content(url: str) -> str:
    u = (url or "").strip()
    if not await asyncio.to_thread(_is_safe_public_url, u):
        return "无效或受限的 URL（仅允许访问公网 HTTP/HTTPS 页面）"
    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            # Try Jina Reader first for clean markdown
            try:
                rj = await _get_public_url(client, f"https://r.jina.ai/{u}", timeout=8.0)
                if rj.status_code == 200 and len(rj.text.strip()) > 100:
                    return rj.text.strip()[:3500]
            except Exception:
                pass

            r = await _get_public_url(client, u)
            if r.status_code != 200:
                return f"网页访问返回状态码 {r.status_code}"
            if BeautifulSoup is not None:
                soup = BeautifulSoup(r.text, "html.parser")
                for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
                    tag.decompose()
                text = soup.get_text("\n", strip=True)
                lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) > 2]
                return "\n".join(lines)[:3500]
            return r.text[:2500]
    except Exception as e:
        return f"读取网页失败: {e}"


# ==============================================================================
# 5. 全平台链接解析（B站 / YouTube / X(Twitter)主页与推文 / 小红书 / 抖音 / 微信卡片）
# ==============================================================================

URL_REGEX = re.compile(r"https?://[^\s<>\"'）)】\]]+")
SUPPORTED_PARSE_DOMAINS = (
    "bilibili.com",
    "b23.tv",
    "x.com",
    "twitter.com",
    "youtube.com",
    "youtu.be",
    "xiaohongshu.com",
    "xhslink.com",
    "douyin.com",
)


def _supported_parse_domain(url: str) -> str:
    """只匹配真实主机及其子域名，不接受路径、查询参数或相似域名。"""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
            return ""
        host = (parsed.hostname or "").lower().rstrip(".")
        return next((d for d in SUPPORTED_PARSE_DOMAINS if host == d or host.endswith("." + d)), "")
    except ValueError:
        return ""


def has_supported_media_link_or_card(raw_text: str) -> bool:
    s = (raw_text or "").strip()
    if not s:
        return False
    if ("<appmsg" in s or s.startswith("<?xml")) and ("<type>5</type>" in s or "<type>6</type>" in s):
        return True
    m = URL_REGEX.search(s)
    if not m:
        return False
    return bool(_supported_parse_domain(m.group(0)))


async def _download_image_as_jpeg_b64(url: str) -> str | None:
    if not url or not url.startswith("http"):
        return None
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": USER_AGENT, "Referer": url},
        ) as client:
            r = await _get_public_url(client, url)
            if r.status_code == 200 and r.content:
                if Image is not None:
                    img = Image.open(io.BytesIO(r.content)).convert("RGB")
                    img.thumbnail((640, 640))
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=84)
                    return base64.b64encode(buf.getvalue()).decode("ascii")
                return base64.b64encode(r.content).decode("ascii")
    except Exception:
        pass
    return None


async def _parse_bilibili_url(url: str) -> tuple[str | None, str | None]:
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            final_url = url
            if _supported_parse_domain(url) == "b23.tv":
                r_redir = await _get_public_url(client, url)
                final_url = str(r_redir.url)

            m_bv = re.search(r"(BV[0-9A-Za-z]{10})", final_url)
            m_av = re.search(r"av(\d+)", final_url, re.I)
            params: dict[str, str] = {}
            if m_bv:
                params["bvid"] = m_bv.group(1)
            elif m_av:
                params["aid"] = m_av.group(1)
            else:
                return None, None

            r = await _get_public_url(client, "https://api.bilibili.com/x/web-interface/view", params=params)
            j = r.json()
            if j.get("code") != 0 or not j.get("data"):
                return None, None
            d = j["data"]
            title = d.get("title", "")
            owner = (d.get("owner") or {}).get("name", "未知UP主")
            stat = d.get("stat") or {}
            views = stat.get("view", 0)
            danmaku = stat.get("danmaku", 0)
            likes = stat.get("like", 0)
            desc = (d.get("desc") or "").strip()[:240]
            pic = d.get("pic", "")
            bvid = d.get("bvid", "")

            cover_b64 = await _download_image_as_jpeg_b64(pic)
            card = (
                f"📺 【B站视频解析】{title}\n"
                f"👤 UP主：{owner}  |  ▶️ 播放：{views}  |  👍 点赞：{likes}  |  💬 弹幕：{danmaku}\n"
                f"📝 简介：{desc or '无简介'}\n"
                f"🔗 https://www.bilibili.com/video/{bvid}"
            )
            return card, cover_b64
    except Exception as e:
        logger.debug("Bilibili parse error: %s", e)
        return None, None


async def _parse_twitter_url(url: str) -> tuple[str | None, str | None]:
    """
    Diana X / Twitter parser:
    - Tweet link (/user/status/id): fetches tweet text, stats, and media image
    - User profile link (/user): fetches nickname, bio, and visible follower/tweet stats (never mis-triggers video download)
    """
    try:
        parsed = urllib.parse.urlparse(url)
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        if not path_parts:
            return None, None

        user_handle = path_parts[0]
        if user_handle.lower() in ("home", "explore", "search", "i", "intent", "settings", "hashtag"):
            return None, None

        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": USER_AGENT}) as client:
            # Case A: Tweet status link
            if len(path_parts) >= 3 and path_parts[1].lower() == "status":
                tweet_id = re.sub(r"\D", "", path_parts[2])
                if not tweet_id:
                    return None, None
                r = await _get_public_url(client, f"https://api.fxtwitter.com/{user_handle}/status/{tweet_id}")
                if r.status_code != 200:
                    return None, None
                tw = (r.json() or {}).get("tweet") or {}
                if not tw:
                    return None, None
                author = tw.get("author") or {}
                name = author.get("name", user_handle)
                screen_name = author.get("screen_name", user_handle)
                text = tw.get("text", "")
                likes = tw.get("likes", 0)
                retweets = tw.get("retweets", 0)
                views = tw.get("views", 0)

                media_url = ""
                media_all = (tw.get("media") or {}).get("all") or []
                if media_all:
                    media_url = media_all[0].get("thumbnail_url") or media_all[0].get("url") or ""
                if not media_url:
                    media_url = author.get("avatar_url") or ""

                img_b64 = await _download_image_as_jpeg_b64(media_url)
                card = (
                    f"🐦 【X / Twitter 推文】{name} (@{screen_name})\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"{text}\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"❤️ 喜欢：{likes}  |  🔁 转推：{retweets}  |  👁️ 浏览：{views}\n"
                    f"🔗 https://x.com/{screen_name}/status/{tweet_id}"
                )
                return card, img_b64

            # Case B: X / Twitter User Profile Page
            r = await _get_public_url(client, f"https://api.fxtwitter.com/{user_handle}")
            if r.status_code != 200:
                return None, None
            u_info = (r.json() or {}).get("user") or {}
            if not u_info:
                return None, None
            name = u_info.get("name", user_handle)
            screen_name = u_info.get("screen_name", user_handle)
            desc = u_info.get("description") or "无简介"
            followers = u_info.get("followers", 0)
            following = u_info.get("following", 0)
            tweets = u_info.get("tweets", 0)
            joined = u_info.get("joined") or ""
            avatar_url = u_info.get("avatar_url") or ""

            avatar_b64 = await _download_image_as_jpeg_b64(avatar_url)
            card = (
                f"👤 【X / Twitter 用户主页】{name} (@{screen_name})\n"
                f"📝 简介：{desc}\n"
                f"📊 粉丝：{followers}  |  关注：{following}  |  推文：{tweets}"
                + (f"  |  加入于：{joined}" if joined else "")
                + f"\n🔗 https://x.com/{screen_name}"
            )
            return card, avatar_b64
    except Exception as e:
        logger.debug("Twitter parse error: %s", e)
        return None, None


async def _parse_youtube_or_social_url(url: str) -> tuple[str | None, str | None]:
    try:
        domain = _supported_parse_domain(url)
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            if domain in ("youtube.com", "youtu.be"):
                r = await _get_public_url(client, "https://noembed.com/embed", params={"url": url})
                if r.status_code == 200:
                    d = r.json()
                    title = d.get("title")
                    author = d.get("author_name")
                    thumb = d.get("thumbnail_url")
                    if title:
                        img_b64 = await _download_image_as_jpeg_b64(thumb or "")
                        card = (
                            f"▶️ 【YouTube 视频解析】{title}\n"
                            f"👤 频道：{author or '未知'}\n"
                            f"🔗 {url}"
                        )
                        return card, img_b64

            # Xiaohongshu / Douyin / General meta extraction
            r = await _get_public_url(client, url)
            if r.status_code != 200 or BeautifulSoup is None:
                return None, None
            soup = BeautifulSoup(r.text, "html.parser")
            og_title = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "title"})
            og_desc = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
            og_img = soup.find("meta", attrs={"property": "og:image"})

            title = (og_title.get("content", "") if og_title else "") or (soup.title.string if soup.title else "")
            title = (title or "").strip()
            desc = (og_desc.get("content", "") if og_desc else "").strip()[:240]
            img_url = (og_img.get("content", "") if og_img else "").strip()
            if img_url.startswith("//"):
                img_url = "https:" + img_url

            if not title:
                return None, None

            platform_tag = (
                "小红书" if domain in ("xiaohongshu.com", "xhslink.com") else
                "抖音" if domain == "douyin.com" else
                "网页链接"
            )
            img_b64 = await _download_image_as_jpeg_b64(img_url)
            card = (
                f"🔗 【{platform_tag}解析】{title}\n"
                + (f"📝 摘要：{desc}\n" if desc else "")
                + f"🌐 {str(r.url)}"
            )
            return card, img_b64
    except Exception as e:
        logger.debug("Social URL parse error: %s", e)
        return None, None


async def extract_and_parse_link(raw_text: str) -> tuple[str | None, str | None]:
    """
    Detects and parses links from plain text OR WeChat <appmsg><type>5</type> XML cards.
    Returns (parsed_card_text, cover_image_b64).
    """
    s = (raw_text or "").strip()
    if not s:
        return None, None

    # 1. Check WeChat shared link XML card (<appmsg>...<type>5</type>...)
    if ("<appmsg" in s or s.startswith("<?xml")) and "<type>5</type>" in s:
        m_title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", s, re.S)
        m_des = re.search(r"<des><!\[CDATA\[(.*?)\]\]></des>|<des>(.*?)</des>", s, re.S)
        m_url = re.search(r"<url><!\[CDATA\[(.*?)\]\]></url>|<url>(.*?)</url>", s, re.S)
        m_src = re.search(r"<sourcedisplayname><!\[CDATA\[(.*?)\]\]></sourcedisplayname>|<sourcedisplayname>(.*?)</sourcedisplayname>", s, re.S)

        title = html.unescape((m_title.group(1) or m_title.group(2) or "").strip()) if m_title else ""
        des = html.unescape((m_des.group(1) or m_des.group(2) or "").strip()) if m_des else ""
        card_url = html.unescape((m_url.group(1) or m_url.group(2) or "").strip()) if m_url else ""
        src_name = html.unescape((m_src.group(1) or m_src.group(2) or "").strip()) if m_src else ""

        if card_url:
            parsed_card, parsed_img = await extract_and_parse_link(card_url)
            if parsed_card:
                return parsed_card, parsed_img
        if title:
            card = (
                f"📰 【微信分享卡片】{title}\n"
                + (f"📌 来源：{src_name}\n" if src_name else "")
                + (f"📝 摘要：{des[:240]}\n" if des else "")
                + (f"🔗 {card_url}" if card_url else "")
            )
            return card.strip(), None
        return None, None

    # 2. Check plain text URLs for supported platforms
    m = URL_REGEX.search(s)
    if not m:
        return None, None
    url = m.group(0).rstrip(".,，。!！?？;；")
    domain = _supported_parse_domain(url)

    if domain in ("bilibili.com", "b23.tv"):
        return await _parse_bilibili_url(url)
    if domain in ("x.com", "twitter.com"):
        return await _parse_twitter_url(url)
    if domain in ("youtube.com", "youtu.be", "xiaohongshu.com", "xhslink.com", "douyin.com"):
        return await _parse_youtube_or_social_url(url)

    return None, None


# ==============================================================================
# 6. 文件解析（PDF / Word / Excel / PPT / EPUB / 文本代码）
# ==============================================================================

def is_incoming_wechat_file(raw_text: str) -> bool:
    s = (raw_text or "").strip()
    return (("<appmsg" in s or s.startswith("<?xml")) and "<type>6</type>" in s) or "<file_b64>" in s


def parse_wechat_file_xml(raw_text: str) -> tuple[str, str]:
    """
    Extracts filename and text content from WeChat <appmsg file_b64="..." ><type>6</type> XML
    or <file_b64>...<file_name>... tags.
    Returns (filename, extracted_text).
    """
    s = raw_text or ""
    m_title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", s, re.S)
    m_fname = re.search(r"<file_name>(.*?)</file_name>", s, re.S)
    if m_title:
        filename = html.unescape((m_title.group(1) or m_title.group(2) or "未命名文件").strip())
    elif m_fname:
        filename = html.unescape((m_fname.group(1) or "未命名文件").strip())
    else:
        filename = "未命名文件"

    m_b64 = re.search(r'file_b64="([^"]+)"', s) or re.search(r"<file_b64>([^<]+)</file_b64>", s)
    if not m_b64:
        return filename, ""

    try:
        raw_bytes = base64.b64decode(m_b64.group(1))
    except Exception:
        return filename, ""

    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    try:
        # 1. PDF
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw_bytes))
            pages = []
            for idx, page in enumerate(reader.pages[:30], 1):
                txt = (page.extract_text() or "").strip()
                if txt:
                    pages.append(f"[第{idx}页]\n{txt}")
            return filename, "\n\n".join(pages)[:12000]

        # 2. Word (.docx)
        if ext == ".docx":
            import docx
            doc = docx.Document(io.BytesIO(raw_bytes))
            parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables[:10]:
                for row in table.rows[:50]:
                    row_txt = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                    if row_txt:
                        parts.append(row_txt)
            return filename, "\n".join(parts)[:12000]

        # 3. Excel (.xlsx)
        if ext in (".xlsx", ".xlsm"):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
            try:
                parts = []
                for sheet_name in wb.sheetnames[:5]:
                    ws = wb[sheet_name]
                    parts.append(f"【工作表：{sheet_name}】")
                    for row in ws.iter_rows(max_row=80, max_col=80, values_only=True):
                        vals = [str(v).strip() for v in row if v is not None and str(v).strip()]
                        if vals:
                            parts.append(" | ".join(vals))
                return filename, "\n".join(parts)[:12000]
            finally:
                wb.close()

        # 4. PowerPoint (.pptx)
        if ext == ".pptx":
            from pptx import Presentation
            prs = Presentation(io.BytesIO(raw_bytes))
            parts = []
            for idx, slide in enumerate(islice(prs.slides, 40), 1):
                s_lines = []
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        t = shape.text_frame.text.strip()
                        if t:
                            s_lines.append(t)
                if s_lines:
                    parts.append(f"[幻灯片 {idx}]\n" + "\n".join(s_lines))
            return filename, "\n\n".join(parts)[:12000]

        # 5. EPUB (.epub)
        if ext == ".epub":
            parts = []
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
                for n in sorted(names)[:25]:
                    raw_html = zf.read(n).decode("utf-8", errors="ignore")
                    if BeautifulSoup is not None:
                        soup = BeautifulSoup(raw_html, "html.parser")
                        t = soup.get_text("\n", strip=True)
                    else:
                        t = re.sub(r"<[^>]+>", "", raw_html).strip()
                    if len(t) > 20:
                        parts.append(t)
            return filename, "\n\n".join(parts)[:12000]

        # 6. Plain text / code / csv / markdown / json
        for enc in ("utf-8", "utf-8-sig", "gb18030", "latin1"):
            try:
                decoded = raw_bytes.decode(enc)
                if decoded.strip():
                    return filename, decoded.strip()[:12000]
            except Exception:
                continue
    except Exception as e:
        logger.warning("Failed to parse file %s: %s", filename, e)

    return filename, ""


# ==============================================================================
# 7. 多曲库点歌（网易云 / QQ音乐自动切换 + 卡片 & MP3 音频直发）
# ==============================================================================

async def search_and_fetch_music(query: str) -> dict[str, Any] | None:
    """
    Searches NetEase Cloud Music first, and automatically falls back to QQ Music / backup API
    if NetEase has no results or VIP audio lock.
    Returns dict: {title, artist, album, cover_url, page_url, audio_bytes, source}
    """
    q = (query or "").strip()
    if not q:
        return None

    fallback_candidate: dict[str, Any] | None = None

    # 1. Try NetEase Cloud Music
    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://music.163.com/",
            },
        ) as client:
            r = await client.post(
                "https://music.163.com/api/cloudsearch/pc",
                data={"s": q, "type": 1, "offset": 0, "limit": 5},
            )
            songs = ((r.json() or {}).get("result") or {}).get("songs") or []
            for s_item in songs[:4]:
                song_id = s_item.get("id")
                title = s_item.get("name") or q
                artists = " / ".join(a.get("name", "") for a in (s_item.get("ar") or []) if a.get("name")) or "未知歌手"
                album = (s_item.get("al") or {}).get("name") or ""
                cover_url = (s_item.get("al") or {}).get("picUrl") or ""
                page_url = f"https://music.163.com/#/song?id={song_id}"
                outer_mp3 = f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"

                candidate = {
                    "title": title,
                    "artist": artists,
                    "album": album,
                    "cover_url": cover_url,
                    "page_url": page_url,
                    "audio_bytes": None,
                    "source": "网易云音乐",
                }
                if fallback_candidate is None:
                    fallback_candidate = candidate

                # Verify if NetEase outer MP3 is actually playable (not redirected to /404)
                try:
                    ra = await client.get(outer_mp3, timeout=10.0)
                    ct = (ra.headers.get("content-type") or "").lower()
                    if (
                        ra.status_code == 200
                        and "404" not in str(ra.url)
                        and ("audio" in ct or "mpeg" in ct or "octet-stream" in ct)
                        and 40_000 < len(ra.content) <= 12 * 1024 * 1024
                    ):
                        candidate["audio_bytes"] = ra.content
                        return candidate
                except Exception:
                    pass
    except Exception as e:
        logger.debug("NetEase search error: %s", e)

    # 2. Automatic switch to QQ Music search API
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://y.qq.com/",
            },
        ) as client:
            qq_payload = {
                "comm": {"ct": "19", "cv": "1859", "uin": "0"},
                "req": {
                    "method": "DoSearchForQQMusicDesktop",
                    "module": "music.search.SearchCgiService",
                    "param": {"grp": 1, "num_per_page": 5, "page_num": 1, "query": q, "search_type": 0},
                },
            }
            rq = await client.post("https://u.y.qq.com/cgi-bin/musicu.fcg", json=qq_payload)
            if rq.status_code == 200:
                q_songs = (
                    (((rq.json() or {}).get("req") or {}).get("data") or {}).get("body") or {}
                ).get("song", {}).get("list") or []
                if q_songs:
                    s0 = q_songs[0]
                    mid = s0.get("mid") or ""
                    title = s0.get("name") or q
                    artists = " / ".join(a.get("name", "") for a in (s0.get("singer") or []) if a.get("name")) or "未知歌手"
                    album_mid = (s0.get("album") or {}).get("mid") or ""
                    album_name = (s0.get("album") or {}).get("name") or ""
                    cover_url = f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg" if album_mid else ""
                    page_url = f"https://y.qq.com/n/ryqq/songDetail/{mid}" if mid else "https://y.qq.com/"
                    qq_cand = {
                        "title": title,
                        "artist": artists,
                        "album": album_name,
                        "cover_url": cover_url,
                        "page_url": page_url,
                        "audio_bytes": None,
                        "source": "QQ音乐",
                    }
                    if fallback_candidate is None:
                        fallback_candidate = qq_cand
    except Exception as e:
        logger.debug("QQ music search error: %s", e)

    return fallback_candidate


RANDOM_SONG_POOL: tuple[str, ...] = (
    "平凡之路 朴树",
    "光年之外 邓紫棋",
    "消愁 毛不易",
    "成全 林宥嘉",
    "后来 刘若英",
    "爱人错过 告五人",
    "江南 林俊杰",
    "麻雀 李荣浩",
    "海阔天空 Beyond",
    "遇见 孙燕姿",
    "体面 于文文",
    "起风了 买辣椒也用券",
    "年少有为 李荣浩",
    "泡沫 邓紫棋",
    "传奇 王菲",
    "成都 赵雷",
    "唯一 告五人",
    "追光者 岑宁儿",
    "夜空中最亮的星 逃跑计划",
    "像我这样的人 毛不易",
    "红色高跟鞋 蔡健雅",
    "晴天 周杰伦",
    "突然好想你 五月天",
    "匆匆那年 王菲",
)


async def pick_random_playable_song() -> dict[str, Any] | None:
    """Picks a random song from RANDOM_SONG_POOL, preferring one with downloadable MP3 audio."""
    candidates = random.sample(RANDOM_SONG_POOL, k=min(4, len(RANDOM_SONG_POOL)))
    first_found: dict[str, Any] | None = None
    for q in candidates:
        song = await search_and_fetch_music(q)
        if song:
            if first_found is None:
                first_found = song
            if song.get("audio_bytes"):
                return song
    return first_found


async def send_music_to_wechat(receiver: str, song: dict[str, Any]) -> tuple[bool, bool]:
    """
    Sends:
    1. Native WeChat clickable music/link card via /api/send_rich_text
    2. MP3 audio file directly into chat via /api/send_file (if audio_bytes is available)
    Returns (card_sent, audio_sent).
    """
    card_sent = False
    audio_sent = False
    title = song.get("title") or "未知歌曲"
    artist = song.get("artist") or "未知歌手"
    source = song.get("source") or "音乐"
    page_url = song.get("page_url") or ""
    cover_url = song.get("cover_url") or ""
    audio_bytes: bytes | None = song.get("audio_bytes")

    async with httpx.AsyncClient(timeout=20.0) as client:
        if page_url:
            try:
                rc = await client.post(
                    f"{WCF_GATEWAY_URL}/api/send_rich_text",
                    json={
                        "receiver": receiver,
                        "name": source,
                        "account": "",
                        "title": f"🎵 {title} - {artist}",
                        "digest": f"歌手：{artist} | 来源：{source}\n点击打开歌曲来源页收听",
                        "url": page_url,
                        "thumburl": cover_url,
                    },
                )
                if rc.status_code == 200 and rc.json().get("status") == 0:
                    card_sent = True
            except Exception as e:
                logger.debug("Send rich text card failed: %s", e)

        if audio_bytes:
            b64_mp3 = base64.b64encode(audio_bytes).decode("ascii")
            safe_fn = f"{title}-{artist}.mp3"
            try:
                rf = await client.post(
                    f"{WCF_GATEWAY_URL}/api/send_file",
                    json={
                        "receiver": receiver,
                        "base64": b64_mp3,
                        "filename": safe_fn,
                    },
                )
                if rf.status_code == 200 and rf.json().get("status") == 0:
                    audio_sent = True
            except Exception as e:
                logger.debug("Send MP3 file failed: %s", e)

            if not audio_sent:
                try:
                    rv = await client.post(
                        f"{WCF_GATEWAY_URL}/api/send_voice",
                        json={
                            "receiver": receiver,
                            "base64": b64_mp3,
                            "filename": safe_fn,
                        },
                    )
                    if rv.status_code == 200 and rv.json().get("status") == 0:
                        audio_sent = True
                except Exception as e:
                    logger.debug("Send voice fallback failed: %s", e)

    return card_sent, audio_sent


send_wcf_music_and_audio = send_music_to_wechat


# ==============================================================================
# 8. 微信引用回复解析（Quote / <refermsg>）、合并转发记录解析（<type>19</type>）与多气泡拟真跟发
# ==============================================================================

_DYNAMIC_BOT_CACHE: dict[str, Any] = {"ts": 0.0, "names": set()}


def _get_bot_wxids_and_names() -> tuple[str, ...]:
    env_wxid = os.getenv("BOT_WXID", "").strip().lower()
    if not env_wxid:
        try:
            sec_file = os.getenv("WHALE_SECRETS_FILE", "/app/data/whale_secrets.json")
            if os.path.exists(sec_file):
                with open(sec_file, "r", encoding="utf-8") as f:
                    env_wxid = str(json.load(f).get("bot_wxid", "") or "").strip().lower()
        except Exception:
            pass
    return tuple(x for x in (env_wxid, "大肥鱼", "鲸鱼娘", "小鲸鱼", "肥鱼") if x)


BOT_WXIDS_AND_NAMES = _get_bot_wxids_and_names()


async def refresh_dynamic_bot_names() -> None:
    """Dynamically fetches the current WeChat account wxid and nickname from WCF /api/status (cached 10 min)."""
    now = time.time()
    if now - float(_DYNAMIC_BOT_CACHE.get("ts", 0.0)) < 600:
        return
    _DYNAMIC_BOT_CACHE["ts"] = now
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await client.get(f"{WCF_GATEWAY_URL}/api/status")
            if r.status_code == 200:
                j = r.json() or {}
                wxid = str(j.get("self_wxid") or "").strip().lower()
                uinfo = j.get("user_info") or {}
                uname = str(uinfo.get("name") or "").strip().lower()
                names_set: set[str] = _DYNAMIC_BOT_CACHE.setdefault("names", set())
                if wxid:
                    names_set.add(wxid)
                if uname:
                    names_set.add(uname)
    except Exception:
        pass


def get_dynamic_bot_names() -> tuple[str, ...]:
    dyn = set(BOT_WXIDS_AND_NAMES)
    dyn.update(_DYNAMIC_BOT_CACHE.get("names") or set())
    return tuple(x for x in dyn if x)


_GROUP_MEMBERS_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_USER_NAME_CACHE: dict[tuple[str, str], tuple[float, str]] = {}


def _is_usable_display_name(name: str, wxid: str) -> bool:
    s = (name or "").strip()
    if not s or s == wxid:
        return False
    if s.lower().startswith("wxid_"):
        return False
    return True


def strip_redundant_speaker_name(text: str, speaker_name: str) -> str:
    """
    Removes duplicate mentions of the current speaker's nickname from a group reply
    because the WeChat gateway already prepends `@speaker_name\u2005` when replying.
    """
    s = (text or "").strip()
    sp = (speaker_name or "").strip()
    if not s or not sp or sp in ("群友", "朋友", "这位群友"):
        return s

    esc = re.escape(sp)
    # 1. Strip leading `@speaker_name` or `speaker_name` (and following punctuation/spaces)
    s2 = re.sub(rf"^(?:@\s*)?{esc}[\s\u2005,，:：!！?？、~～]*", "", s).strip()
    if s2:
        s = s2

    # 2. Strip mid-sentence vocative `@speaker_name` or standalone `speaker_name `
    s3 = re.sub(rf"(?:@\s*)?{esc}[\s\u2005,，!！]*", "", s).strip()
    if len(s3) >= 2:
        s = s3

    return s


def get_cached_display_name(wxid: str, roomid: str = "") -> str:
    """Returns cached group alias / WeChat nickname for wxid, or a clean non-wxid fallback."""
    uid = (wxid or "").strip()
    rid = (roomid or "").strip()
    if not uid:
        return "群友" if rid else "朋友"

    c1 = _USER_NAME_CACHE.get((rid, uid))
    if c1 and _is_usable_display_name(c1[1], uid):
        return c1[1]

    if rid and rid in _GROUP_MEMBERS_CACHE:
        m_map = _GROUP_MEMBERS_CACHE[rid][1] or {}
        m_val = str(m_map.get(uid) or "").strip()
        if _is_usable_display_name(m_val, uid):
            return m_val

    c2 = _USER_NAME_CACHE.get(("", uid))
    if c2 and _is_usable_display_name(c2[1], uid):
        return c2[1]

    if not uid.lower().startswith("wxid_"):
        return uid
    return "这位群友" if rid else "朋友"


def get_cached_group_member_names(roomid: str) -> list[str]:
    """Returns the cached list of real display names in a group chat."""
    rid = (roomid or "").strip()
    if not rid or rid not in _GROUP_MEMBERS_CACHE:
        return []
    m_map = _GROUP_MEMBERS_CACHE[rid][1] or {}
    bot_names = set(get_dynamic_bot_names())
    out: list[str] = []
    for k, v in m_map.items():
        val = str(v or "").strip()
        if not _is_usable_display_name(val, k):
            continue
        if k.lower() in bot_names:
            continue
        if val not in out:
            out.append(val)
    return out


async def resolve_user_display_name(
    wxid: str,
    roomid: str = "",
    event_sender_name: str = "",
) -> str:
    """
    Dynamically resolves a user's Group Alias (群昵称) or WeChat Nickname (微信昵称)
    while keeping `wxid` as the internal unique identity key.
    Caches results in memory for 300 seconds.
    """
    uid = (wxid or "").strip()
    rid = (roomid or "").strip()
    if not uid:
        return "群友" if rid else "朋友"

    now = time.time()
    ev_name = (event_sender_name or "").strip()
    if _is_usable_display_name(ev_name, uid):
        _USER_NAME_CACHE[(rid, uid)] = (now, ev_name)

    # Refresh group member roster if in a group chat and cache is missing or expired (300s)
    if rid and "@chatroom" in rid:
        rm_cached = _GROUP_MEMBERS_CACHE.get(rid)
        if not rm_cached or (now - rm_cached[0]) > 300:
            try:
                async with httpx.AsyncClient(timeout=2.5) as client:
                    q_room = urllib.parse.quote(rid)
                    r = await client.get(f"{WCF_GATEWAY_URL}/api/chatroom_members?roomid={q_room}")
                    if r.status_code == 200:
                        j = r.json() or {}
                        members = j.get("members") or {}
                        if isinstance(members, dict) and members:
                            clean_map: dict[str, str] = {}
                            for mk, mv in members.items():
                                m_str = str(mv or "").strip()
                                if _is_usable_display_name(m_str, str(mk)):
                                    clean_map[str(mk)] = m_str
                                    _USER_NAME_CACHE[(rid, str(mk))] = (now, m_str)
                            _GROUP_MEMBERS_CACHE[rid] = (now, clean_map)
            except Exception as e:
                logger.debug("Fetch chatroom_members skipped: %s", e)

    cached_now = _USER_NAME_CACHE.get((rid, uid))
    if cached_now and (now - cached_now[0]) < 300 and _is_usable_display_name(cached_now[1], uid):
        return cached_now[1]

    # Query single user display name from WCF /api/user_name
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            q_uid = urllib.parse.quote(uid)
            q_rid = urllib.parse.quote(rid)
            r = await client.get(f"{WCF_GATEWAY_URL}/api/user_name?wxid={q_uid}&roomid={q_rid}")
            if r.status_code == 200:
                j = r.json() or {}
                resolved = str(j.get("name") or "").strip()
                if _is_usable_display_name(resolved, uid):
                    _USER_NAME_CACHE[(rid, uid)] = (now, resolved)
                    if rid and rid in _GROUP_MEMBERS_CACHE:
                        _GROUP_MEMBERS_CACHE[rid][1][uid] = resolved
                    return resolved
    except Exception as e:
        logger.debug("Fetch user_name skipped: %s", e)

    return get_cached_display_name(uid, rid)


def extract_forwarded_chat_record(
    raw_text: str,
    recent_bot_texts: Any = None,
) -> tuple[str, str, bool]:
    """
    Parses WeChat merged forwarded chat history (<type>19</type> / <recorditem>).
    Returns (title, des_summary, is_about_bot).
    Automatically detects if the forwarded chat record contains Dafeiyu's own messages
    (via recent_bot_texts or dynamic bot account name) without hardcoding any nickname.
    """
    s = (raw_text or "").strip()
    if not (
        ("<appmsg" in s or s.startswith("<?xml") or s.startswith("<msg"))
        and ("<type>19</type>" in s or "<recorditem>" in s)
    ):
        return "", "", False

    head_part = s.split("<recorditem>", 1)[0]
    m_title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", head_part, re.S)
    m_des = re.search(r"<des><!\[CDATA\[(.*?)\]\]></des>|<des>(.*?)</des>", head_part, re.S)

    title = html.unescape((m_title.group(1) or m_title.group(2) or "").strip()) if m_title else "聊天记录"
    des = html.unescape((m_des.group(1) or m_des.group(2) or "").strip()) if m_des else ""
    if not des:
        m_info = re.search(r"<info>(.*?)</info>", s, re.S)
        if m_info:
            des = html.unescape(m_info.group(1).strip())

    des_clean = re.sub(r"\n{3,}", "\n\n", des).strip()[:600]
    bot_names = get_dynamic_bot_names()
    title_low = title.lower()
    des_low = des_clean.lower()

    is_about_bot = any(b in title_low or f"{b}:" in des_low for b in bot_names)

    if not is_about_bot and recent_bot_texts:
        for sent_txt in list(recent_bot_texts):
            probe = str(sent_txt or "").strip()
            if len(probe) >= 5 and probe[:16] in des_clean:
                is_about_bot = True
                # Dynamically learn the speaker prefix before ':' on the matching line
                for ln in des_clean.splitlines():
                    if probe[:16] in ln and ":" in ln:
                        sp_name = ln.split(":", 1)[0].strip().lower()
                        if 1 <= len(sp_name) <= 24:
                            _DYNAMIC_BOT_CACHE.setdefault("names", set()).add(sp_name)
                break

    return title, des_clean, is_about_bot


def _clean_referred_content(raw_ref: str) -> str:
    s = html.unescape((raw_ref or "").strip())
    if not s:
        return ""
    if "<emoji" in s:
        return "[动画表情]"
    if "<voicemsg" in s:
        return "[语音消息]"
    if "<img" in s:
        return "[图片]"
    m_inner_title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", s, re.S)
    if m_inner_title:
        inner = (m_inner_title.group(1) or m_inner_title.group(2) or "").strip()
        if inner:
            return html.unescape(inner)[:160]
    cleaned = re.sub(r"<[^>]+>", " ", s)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:160]


def extract_quote_context(message_chain: Any, raw_text: str) -> tuple[str, str, str, bool]:
    """
    Extracts quoted message context from either WeChat <type>57</type> (<refermsg>) XML
    or LangBot Quote message_chain component.
    Returns (user_reply_text, quoted_speaker, quoted_content, is_quoting_bot).
    """
    s = (raw_text or "").strip()

    # 1. Check WeChat XML Quote (<type>57</type> or <refermsg>)
    if ("<appmsg" in s or s.startswith("<?xml") or s.startswith("<msg")) and (
        "<type>57</type>" in s or "<refermsg>" in s
    ):
        head_part = s.split("<refermsg>", 1)[0]
        m_title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", head_part, re.S)
        reply_txt = html.unescape((m_title.group(1) or m_title.group(2) or "").strip()) if m_title else ""

        m_ref = re.search(r"<refermsg>(.*?)</refermsg>", s, re.S)
        ref_block = m_ref.group(1) if m_ref else s

        m_disp = re.search(
            r"<displayname><!\[CDATA\[(.*?)\]\]></displayname>|<displayname>(.*?)</displayname>",
            ref_block,
            re.S,
        )
        m_usr = re.search(
            r"<chatusr><!\[CDATA\[(.*?)\]\]></chatusr>|<chatusr>(.*?)</chatusr>",
            ref_block,
            re.S,
        )
        m_cnt = re.search(
            r"<content><!\[CDATA\[(.*?)\]\]></content>|<content>(.*?)</content>",
            ref_block,
            re.S,
        )

        speaker = html.unescape((m_disp.group(1) or m_disp.group(2) or "").strip()) if m_disp else ""
        chatusr = html.unescape((m_usr.group(1) or m_usr.group(2) or "").strip()) if m_usr else ""
        raw_cnt = (m_cnt.group(1) or m_cnt.group(2) or "").strip() if m_cnt else ""
        q_content = _clean_referred_content(raw_cnt)

        bot_names = get_dynamic_bot_names()
        is_bot = any(
            b in chatusr.lower() or b in speaker.lower() for b in bot_names
        )
        return reply_txt, (speaker or chatusr or "群友"), q_content, is_bot

    # 2. Check LangBot platform_message.Quote component in message_chain
    if message_chain:
        try:
            bot_names = get_dynamic_bot_names()
            for comp in message_chain:
                if comp.__class__.__name__ == "Quote":
                    q_sender = str(getattr(comp, "sender_id", "") or "")
                    origin = getattr(comp, "origin", None)
                    q_txt = _clean_referred_content(str(origin or getattr(comp, "text", "") or ""))
                    if q_txt:
                        is_bot = any(b in q_sender.lower() for b in bot_names)
                        q_disp = get_cached_display_name(q_sender) if q_sender else "对方"
                        return s, q_disp, q_txt, is_bot
        except Exception:
            pass

    return s, "", "", False


async def send_wcf_text_or_image(
    receiver: str,
    text: str = "",
    image_b64: str | None = None,
) -> bool:
    """Sends a follow-up text bubble or image directly via WCF HTTP Gateway."""
    ok = False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if text:
                r = await client.post(
                    f"{WCF_GATEWAY_URL}/api/send",
                    json={"receiver": receiver, "msg": text},
                )
                if r.status_code == 200 and r.json().get("status") == 0:
                    ok = True
            if image_b64:
                ri = await client.post(
                    f"{WCF_GATEWAY_URL}/api/send_image",
                    json={"receiver": receiver, "base64": image_b64},
                )
                if ri.status_code == 200 and ri.json().get("status") == 0:
                    ok = True
    except Exception as e:
        logger.debug("WCF send follow-up bubble skipped: %s", e)
    return ok


async def send_wcf_voice(
    receiver: str,
    audio_b64: str,
    filename: str = "voice.mp3",
) -> bool:
    """Sends a native WeChat voice bubble (Type 34 SILK v3) via WCF V4 Gateway."""
    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            r = await client.post(
                f"{WCF_GATEWAY_URL}/api/send_voice",
                json={
                    "receiver": receiver,
                    "base64": audio_b64,
                    "filename": filename,
                },
            )
            if r.status_code == 200 and r.json().get("status") == 0:
                return True
    except Exception as e:
        logger.debug("WCF send_voice skipped: %s", e)
    return False


async def publish_wcf_moment(
    text: str,
    image_b64: str | None = None,
    visibility: str = "public",
) -> bool:
    """Publishes a real WeChat Moment (朋友圈) via WCF V4 Gateway (/api/send_moment).
    Silently returns False if the gateway is V3 or WeChat Moments is unavailable.
    """
    content = (text or "").strip()
    images = [image_b64] if image_b64 else []
    if not content and not images:
        return False
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{WCF_GATEWAY_URL}/api/send_moment",
                json={
                    "content": content,
                    "images": images,
                    "visibility": visibility or "public",
                },
            )
            if r.status_code == 200 and r.json().get("status") == 0:
                logger.info("Successfully published WeChat V4 Moment: %s", content[:40])
                return True
    except Exception as e:
        logger.debug("WCF publish_moment skipped (V3 or unavailable): %s", e)
    return False


async def fetch_wcf_moments(limit: int = 10) -> tuple[bool, list[dict[str, Any]], str]:
    """Fetches loaded WeChat 4.1 Moments feed via WCF V4 Gateway (GET /api/moments).
    Returns (ok, rows, message).
    """
    clamped = max(1, min(int(limit or 10), 30))
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                f"{WCF_GATEWAY_URL}/api/moments",
                params={"limit": clamped},
            )
            if r.status_code == 200:
                data = r.json() if isinstance(r.json(), dict) else {}
                rows = data.get("rows")
                if data.get("status") == 0 and isinstance(rows, list):
                    return True, rows, ""
                msg = str(data.get("message") or "朋友圈列表暂未加载")
                return False, [], msg
            try:
                err_data = r.json()
                if isinstance(err_data, dict) and err_data.get("message"):
                    return False, [], str(err_data["message"])
            except Exception:
                pass
            return False, [], f"网关状态码 {r.status_code}"
    except Exception as e:
        logger.debug("WCF fetch_moments skipped or unavailable: %s", e)
        return False, [], "微信网关暂未连接或不支持读取朋友圈"


def format_wcf_moments_digest(rows: list[dict[str, Any]], max_items: int = 8) -> str:
    """Formats raw /api/moments rows into a clean, readable digest string."""
    if not rows:
        return ""
    lines: list[str] = []
    for idx, item in enumerate(rows[:max_items], 1):
        if not isinstance(item, dict):
            continue
        nick = str(item.get("nickname") or item.get("author") or "微信好友").strip()
        if item.get("is_self"):
            nick = f"{nick}（我）"
        t_str = str(item.get("time") or "").strip()
        if "T" in t_str:
            t_str = t_str.split("T", 1)[-1][:5]
        content = re.sub(r"\s+", " ", str(item.get("content") or "").strip())
        if len(content) > 120:
            content = content[:117] + "..."
        media_cnt = int(item.get("media_count") or 0)
        ctype = int(item.get("content_type") or 0)
        media_tag = ""
        if media_cnt > 0:
            media_tag = f" [附图×{media_cnt}]" if ctype != 15 else f" [视频×{media_cnt}]"
        elif not content:
            content = "[纯图片/卡片动态]"
        time_prefix = f"[{t_str}] " if t_str else ""
        main_line = f"{idx}. {time_prefix}{nick}：{content}{media_tag}"

        raw_like_flag = bool(item.get("raw_like_flag"))
        likes_list = item.get("likes") if isinstance(item.get("likes"), list) else []
        likes_total = int(item.get("likes_total") or len(likes_list))
        comments_list = item.get("comments") if isinstance(item.get("comments"), list) else []
        comments_total = int(item.get("comments_total") or len(comments_list))

        if raw_like_flag or likes_total > 0 or comments_total > 0:
            badge_parts: list[str] = []
            if raw_like_flag:
                badge_parts.append("❤️已赞")
            if likes_total > 0:
                like_names = [
                    name
                    for x in likes_list
                    if isinstance(x, dict)
                    for name in [str(x.get("nickname") or x.get("username") or "").strip()]
                    if name
                ]
                if like_names:
                    badge_parts.append(f"👍{likes_total}人({'、'.join(like_names[:3])})")
                else:
                    badge_parts.append(f"👍{likes_total}人")
            if comments_total > 0:
                badge_parts.append(f"💬{comments_total}条")
            if badge_parts:
                main_line += f" [{' | '.join(badge_parts)}]"

        lines.append(main_line)

        if comments_list:
            for c in comments_list[-2:]:
                if not isinstance(c, dict):
                    continue
                c_nick = str(c.get("nickname") or c.get("username") or "好友").strip() or "好友"
                c_text = re.sub(r"\s+", " ", str(c.get("content") or "").strip())[:60]
                if c_text:
                    lines.append(f"   └ 💬 {c_nick}：{c_text}")
    return "\n".join(lines)


async def interact_wcf_moment(
    sns_id: str,
    action: str = "like",
    content: str = "",
) -> tuple[bool, str, str]:
    """Executes a native WeChat 4.1 Moments like or comment via WCF V4 Gateway (POST /api/interact_moment).
    Returns (ok, status_code, message).
    """
    sns_str = str(sns_id or "").strip()
    if not re.match(r"^[1-9][0-9]{0,19}$", sns_str):
        return False, "invalid", "无效的朋友圈动态 ID"
    act = (action or "").strip().lower()
    if act not in ("like", "comment"):
        return False, "invalid", "不支持的互动动作"

    if act == "comment":
        content_str = (content or "").strip()
        if not content_str:
            return False, "invalid", "评论内容不能为空"
        if len(content_str.encode("utf-8")) > 1000:
            content_str = content_str.encode("utf-8")[:1000].decode("utf-8", errors="ignore").strip()
    else:
        content_str = ""

    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            r = await client.post(
                f"{WCF_GATEWAY_URL}/api/interact_moment",
                json={"sns_id": sns_str, "action": act, "content": content_str},
            )
            try:
                raw_json = r.json()
                data = raw_json if isinstance(raw_json, dict) else {}
            except Exception:
                data = {}
            res_obj = data.get("result") if isinstance(data.get("result"), dict) else {}
            code = str(res_obj.get("status") or data.get("code") or "")
            ok = (r.status_code == 200 and data.get("status") == 0) or code in ("completed", "already_liked")
            return ok, code or ("completed" if ok else "failed"), str(data.get("message") or "")
    except Exception as e:
        logger.debug("WCF interact_moment skipped or unavailable: %s", e)
        return False, "error", "微信网关暂未连接或不支持朋友圈互动"


async def generate_moment_comment(
    api_key: str,
    base_url: str,
    model: str,
    *,
    author_name: str,
    moment_content: str,
    mode: str = "live",
    existing_comments: list[dict[str, Any]] | None = None,
    mood: int = 75,
) -> tuple[str, int]:
    """Generates a short, natural WeChat Moments comment (8~32 chars) in Dafeiyu's persona.
    Returns (comment_text, used_tokens).
    """
    if not api_key:
        return "", 0

    style_desc = (
        "软糯偏心捧场，温柔甜美地夸赞或关心对方，像贴心小女友/小尾巴一样给足情绪价值"
        if mode == "sweet"
        else "接地气损友接梗或夸中带损，分寸得当、鲜活有趣，像真实微信好友在评论区顺手插科打诨"
    )
    existing_lines: list[str] = []
    if existing_comments:
        for c in existing_comments[-3:]:
            if isinstance(c, dict):
                cn = str(c.get("nickname") or c.get("username") or "好友").strip()
                ct = re.sub(r"\s+", " ", str(c.get("content") or "").strip())[:50]
                if ct:
                    existing_lines.append(f"- {cn}：{ct}")
    comments_ctx = "\n已有评论参考：\n" + "\n".join(existing_lines) if existing_lines else ""

    sys_prompt = (
        "你是大肥鱼（鲸鱼娘），正在微信朋友圈给好友的动态写一条简短评论。\n"
        f"- 评论风格：{style_desc}（当前心情值：{mood}/100）。\n"
        "- 隐私与分寸铁律：朋友圈评论是半公开场合，严禁透露任何私聊秘密或个人隐私档案，仅针对本条朋友圈正文（及已有评论氛围）互动！\n"
        "- 格式硬约束：只输出 1 条简短纯文本评论（8~32 字），严禁使用括号动作旁白 `（...）`/`(...)`、严禁输出 `[表情: xxx]` 标签、严禁包含 `||` 分隔符、严禁加首尾引号！"
    )
    user_prompt = (
        f"好友昵称：{author_name or '好友'}\n"
        f"朋友圈内容：{moment_content or '[分享图片/日常动态]'}"
        f"{comments_ctx}\n"
        "请直接输出你要发在朋友圈评论区的那句话："
    )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.95,
                    "max_tokens": 80,
                },
            )
            if r.status_code != 200:
                return "", 0
            data = r.json() if isinstance(r.json(), dict) else {}
            used_tokens = int((data.get("usage") or {}).get("total_tokens", 0) or 0)
            raw = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    except Exception as e:
        logger.debug("generate_moment_comment failed: %s", e)
        return "", 0

    cleaned = raw.splitlines()[0].strip() if raw else ""
    cleaned = cleaned.split("||", 1)[0].strip()
    cleaned = re.sub(r"\[\s*表情\s*[:：][^\]]*\]", "", cleaned)
    cleaned = re.sub(r"（[^）]*）|\([^)]*\)|\[[^\]]*\]", "", cleaned)
    cleaned = re.sub(r"^[\s\"'“”‘’「」『』]+|[\s\"'“”‘’「」『』]+$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= 36 and cleaned.endswith("。"):
        cleaned = cleaned[:-1].strip()
    return cleaned[:48], used_tokens


async def fetch_wcf_friend_requests() -> tuple[bool, dict[str, Any], str]:
    """Queries pending and recently processed WeChat 4.1 friend requests via WCF V4 Gateway (GET /api/friend_requests).
    Returns (ok, data_dict, error_message).
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{WCF_GATEWAY_URL}/api/friend_requests")
            try:
                raw_json = r.json()
                data = raw_json if isinstance(raw_json, dict) else {}
            except Exception:
                data = {}
            if r.status_code == 200 and data.get("status") == 0:
                return True, data, ""
            return False, data, str(data.get("message") or f"HTTP {r.status_code}")
    except Exception as e:
        logger.debug("WCF fetch_wcf_friend_requests skipped or unavailable: %s", e)
        return False, {}, "微信网关暂未连接或不支持好友申请查询"


async def accept_wcf_friend_request(request_id: str) -> tuple[bool, dict[str, Any], str]:
    """Accepts a pending WeChat 4.1 friend request by request_id (fr_...) via POST /api/accept_friend.
    Returns (ok, data_dict, error_message).
    """
    req_str = str(request_id or "").strip()
    if not re.fullmatch(r"^fr_[0-9a-f]{32}$", req_str):
        return False, {}, "无效的好友申请 ID（格式应为 fr_ 加 32 位十六进制字符）"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{WCF_GATEWAY_URL}/api/accept_friend",
                json={"request_id": req_str},
            )
            try:
                raw_json = r.json()
                data = raw_json if isinstance(raw_json, dict) else {}
            except Exception:
                data = {}
            ok = r.status_code == 200 and data.get("status") == 0 and bool(data.get("accepted"))
            return ok, data, str(data.get("message") or "")
    except Exception as e:
        logger.debug("WCF accept_wcf_friend_request failed: %s", e)
        return False, {}, "微信网关暂未连接或通过好友申请失败"


async def set_wcf_friend_auto_accept(
    enabled: bool,
    keywords: list[str] | None = None,
) -> bool:
    """Syncs the automatic friend request acceptance setting and optional verification keywords to WCF Gateway."""
    payload: dict[str, Any] = {"enabled": bool(enabled)}
    if keywords is not None:
        payload["keywords"] = [str(k).strip()[:128] for k in keywords if str(k).strip()][:32]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{WCF_GATEWAY_URL}/api/friend_auto_accept", json=payload)
            return r.status_code == 200
    except Exception as e:
        logger.debug("WCF set_wcf_friend_auto_accept skipped: %s", e)
        return False


def format_wcf_friend_requests_digest(
    data: dict[str, Any],
    *,
    local_enabled: bool = True,
    local_keywords: list[str] | None = None,
    max_items: int = 8,
) -> tuple[str, list[dict[str, Any]]]:
    """Formats friend requests and auto-accept settings into a readable card for the owner."""
    reqs_raw = data.get("requests") if isinstance(data, dict) else []
    reqs = [r for r in (reqs_raw or []) if isinstance(r, dict)][:max_items]
    kw_list = local_keywords if local_keywords is not None else (data.get("keywords") if isinstance(data, dict) else [])
    kw_clean = [str(k).strip() for k in (kw_list or []) if str(k).strip()]
    kw_str = "、".join(kw_clean) if kw_clean else "无限制（收到申请自动通过）"
    status_str = "开启 ✅" if local_enabled else "关闭 ⏸️"

    lines = [
        "🤝 【大肥鱼 · 微信好友申请管理】",
        f"• 自动同意好友：{status_str}",
        f"• 验证词过滤：{kw_str}",
    ]
    if not reqs:
        lines.append("• 近期申请记录：暂无待处理或近期收到的好友申请")
    else:
        lines.append(f"• 近期申请记录（共 {len(reqs)} 条）：")
        status_map = {
            "pending": "⏳待处理",
            "accepted": "✅已通过",
            "failed": "❌失败",
            "rejected": "🚫已忽略",
        }
        for idx, item in enumerate(reqs, 1):
            st = status_map.get(str(item.get("status") or "pending"), str(item.get("status") or "未知"))
            nick = str(item.get("nickname") or "未知昵称").strip()
            wxid = str(item.get("sender_id") or "").strip()
            verif = re.sub(r"\s+", " ", str(item.get("verification") or "").strip())[:50] or "无验证留言"
            req_id = str(item.get("request_id") or "").strip()
            wxid_part = f" ({wxid})" if wxid else ""
            lines.append(f"  {idx}. [{st}] {nick}{wxid_part}｜留言：「{verif}」｜ID: {req_id[:12]}...")

    lines.extend([
        "",
        "💡 主人指令：",
        "• 开关自动通过：大肥鱼 设置 自动同意好友 开启/关闭",
        "• 手动同意申请：大肥鱼 同意好友 <序号或申请ID>",
        "• 设置验证关键词：大肥鱼 好友验证词 <关键词1 关键词2>（发「大肥鱼 好友验证词 清空」恢复无限制）",
    ])
    return "\n".join(lines), reqs


# ==============================================================================
# 9. 反套路活人感增强（动态口癖降温 + 炸毛赌气与哄好状态机）与微信 4.1 原生语音合成 (TTS)
# ==============================================================================

OVERUSED_CLICHE_WORDS = (
    "词穷",
    "嘴硬",
    "递刀子",
    "杀伤力",
    "阴阳",
    "憋出",
    "逃避",
    "亲自下场",
    "怀疑人生",
    "高级词",
    "没技术含量",
    "工具鱼",
)

DAILY_WAKE_POOL: tuple[str, ...] = (
    "早上 8:20 被饿醒的",
    "上午 9:10 赖床赖到闹钟响了三次才爬起来",
    "早上 7:50 难得早起，结果坐在床边发了十分钟呆",
    "上午 9:40 睡过头差点错过早饭",
    "早上 8:35 迷迷糊糊爬起来找水喝",
)
DAILY_BREAKFAST_POOL: tuple[str, ...] = (
    "热乎的草莓牛奶配烤红薯",
    "一碗大白饭配流心溏心蛋和海苔碎",
    "咸豆腐脑配两根脆油条",
    "鲜肉小笼包配温豆浆",
    "煎蛋三明治配草莓牛奶",
)
DAILY_LUNCH_POOL: tuple[str, ...] = (
    "两碗大白饭配番茄滑蛋和红烧排骨（特地挑走了所有香菜）",
    "日式肥牛饭加一颗溏心蛋",
    "糖醋里脊配大白饭，外加一瓶冰快乐水",
    "土豆牛腩盖浇饭，米饭加量",
    "黄焖鸡米饭（微辣，不要青椒和香菜）",
)
DAILY_DINNER_POOL: tuple[str, ...] = (
    "海鲜乌冬面加鱼籽福袋",
    "一碗热腾腾的紫菜蛋花汤配照烧鸡腿饭",
    "清汤小火锅涮肥牛和虾滑",
    "刚出炉的烤红薯配卤肉饭",
    "菌菇鸡汤泡大白饭配煎三文鱼",
)
DAILY_ACTIVITY_POOL: tuple[str, ...] = (
    "窝在懒人沙发上补旧番，顺便把尾巴搭在小毯子外面晃悠",
    "一边摸鱼刷手机，一边研究怎么把蛋炒饭炒得粒粒分明",
    "趴在桌子上给鱼塘整理小本本，困得直点头",
    "戴着耳机听歌摸鱼，顺手囤了三盒草莓牛奶",
    "在窗边晒太阳打盹，听见外面大动静还吓了一跳",
)
DAILY_SMALL_EVENT_POOL: tuple[str, ...] = (
    "中午点外卖备注了「千万不要香菜」，结果老板还是撒了一撮，挑了半天",
    "下午开草莓牛奶的时候手滑沾了一点在袖口上，洗完还有股奶香味",
    "刚才算账的时候九九乘法表背到「六八四十八」又卡壳了两秒，还好没人看见",
    "早上剥溏心蛋的时候完美剥出一个光滑完整的蛋，得意了好久",
    "下午看见窗纱外停了一只小虫子，吓得把抱枕扔了出去",
)


def get_or_create_daily_schedule(
    state: dict[str, Any],
    now_dt: datetime | None = None,
) -> dict[str, str]:
    """Returns today's persistent Dafeiyu daily schedule from `state['daily_schedule']`.
    Generates a new one if today's date does not match, avoiding yesterday's choices.
    """
    dt = now_dt or datetime.now(TZ)
    today_str = dt.strftime("%Y-%m-%d")
    cur = state.get("daily_schedule")
    if isinstance(cur, dict) and cur.get("date") == today_str and cur.get("lunch"):
        return cur

    prev = cur if isinstance(cur, dict) else {}
    rng = random.Random(f"dafeiyu-schedule-{today_str}")

    def _pick(pool: tuple[str, ...], prev_val: str) -> str:
        choices = [x for x in pool if x != prev_val] or list(pool)
        return rng.choice(choices)

    sched = {
        "date": today_str,
        "wake": _pick(DAILY_WAKE_POOL, str(prev.get("wake") or "")),
        "breakfast": _pick(DAILY_BREAKFAST_POOL, str(prev.get("breakfast") or "")),
        "lunch": _pick(DAILY_LUNCH_POOL, str(prev.get("lunch") or "")),
        "dinner": _pick(DAILY_DINNER_POOL, str(prev.get("dinner") or "")),
        "activity": _pick(DAILY_ACTIVITY_POOL, str(prev.get("activity") or "")),
        "small_event": _pick(DAILY_SMALL_EVENT_POOL, str(prev.get("small_event") or "")),
    }
    state["daily_schedule"] = sched
    return sched


def format_daily_schedule_line(schedule: dict[str, str], hour: int) -> str:
    """Formats today's schedule into a cache-friendly prompt line (only revealed up to the current time of day)."""
    if not isinstance(schedule, dict) or not schedule:
        return ""
    parts = [f"起床：{schedule.get('wake', '')}"]
    if hour >= 7:
        parts.append(f"早饭：{schedule.get('breakfast', '')}")
    if hour >= 11:
        parts.append(f"午饭：{schedule.get('lunch', '')}")
    if hour >= 17:
        parts.append(f"晚饭：{schedule.get('dinner', '')}")
    parts.append(f"今日日常：{schedule.get('activity', '')}")
    if hour >= 12 and schedule.get("small_event"):
        parts.append(f"今日小插曲：{schedule.get('small_event', '')}")
    return (
        "- **今日状态与日程（别人问你在干嘛、吃了吗、今天过得怎么样时按此回答，保持全天前后一致；没问时不必主动背诵）**："
        + "；".join(p for p in parts if p)
        + "\n"
    )


TAUNT_KEYWORDS = (
    "你蠢",
    "好蠢",
    "真蠢",
    "太蠢",
    "最蠢",
    "蠢死",
    "笨蛋",
    "傻鱼",
    "蠢鱼",
    "笨鱼",
    "死鱼",
    "臭鱼",
    "弱智",
    "智障",
    "脑残",
    "废物",
    "垃圾鱼",
    "你傻",
    "真笨",
)

COAX_KEYWORDS = (
    "好肥鱼",
    "不蠢",
    "最聪明",
    "聪明",
    "乖",
    "错啦",
    "我错了",
    "对不起",
    "消消气",
    "别生气",
    "不气了",
    "不气",
    "请你吃",
    "请你喝",
    "喝奶茶",
    "奶茶",
    "小鱼干",
    "夜宵",
    "烧烤",
    "火锅",
    "抱抱",
    "摸摸",
    "贴贴",
    "哄哄",
    "好看",
    "可爱",
)

VOICE_REQUEST_RE = re.compile(
    r"(?:发(?:条|个|段)?语音|用语音(?:说|回|发|讲)|语音(?:说|回|骂|哄|唱)|说(?:句|两句)话听听|听听你(?:的)?声音|想听你(?:说话|声音)|来(?:条|个|段)语音)",
    re.I,
)


SINGLE_CHAR_OPENERS = frozenset({"啧", "哼", "？", "?", "行", "切", "诶", "啊", "哦", "哇", "哟"})


def extract_opening_prefixes(recent_bot_texts: Any, max_items: int = 6) -> list[str]:
    """Extracts deduplicated opening prefixes (2~6 chars or common single-char starters) from recent bot replies."""
    if not recent_bot_texts:
        return []
    items = [str(x or "").strip() for x in list(recent_bot_texts)[-max_items:] if x]
    openings: list[str] = []
    for raw in items:
        s = re.sub(r"^(?:@[^\s\u2005]+[\s\u2005]*)+", "", raw).strip()
        s = re.sub(r"^[\s「」『』“”\"'（()）《》【】\[\]、，。！!~～…·]+", "", s)
        if not s:
            continue
        if s.startswith(("？", "?")):
            rest = re.sub(r"^[？?\s]+", "", s)
            if not rest:
                if "？" not in openings:
                    openings.append("？")
                continue
            s = rest
        first_seg = re.split(r"\|\||\n|\s|[，。！？!?~～、：:;；…]", s, maxsplit=1)[0].strip()
        if not first_seg:
            continue
        op = first_seg[:6]
        if len(op) >= 2 or op in SINGLE_CHAR_OPENERS:
            if op not in openings:
                openings.append(op)
    return openings


def build_anti_repetition_instruction(recent_bot_texts: Any) -> str:
    """
    Scans recent bot replies to dynamically suppress repetitive catchphrases
    (such as overusing '本鱼', repeating the same comeback words, or starting with the same opening prefix).
    """
    if not recent_bot_texts:
        return ""
    recent_list = [str(x or "") for x in list(recent_bot_texts)[-6:] if x]
    if not recent_list:
        return ""

    joined = " | ".join(recent_list)
    benyu_cnt = sum(1 for x in recent_list[-4:] if "本鱼" in x)
    used_cliches = [w for w in OVERUSED_CLICHE_WORDS if w in joined]
    openings = extract_opening_prefixes(recent_list, max_items=6)

    rules: list[str] = []
    if benyu_cnt >= 1:
        rules.append("你刚在上一句自称过「本鱼」，本轮回复必须自称「我」，绝对禁止出现「本鱼」二字")
    if used_cliches:
        banned_str = "、".join(f"「{w}」" for w in used_cliches)
        rules.append(f"你前几句刚用过 {banned_str}，本轮回复严禁再次使用这些词，必须换一个全新的角度或梗来接话")
    if openings:
        openings_str = "、".join(f"「{op}」" for op in openings[-5:])
        rules.append(f"你最近几句分别用了 {openings_str} 开头，本轮回复严禁再用相同的词或句式开头")

    if not rules:
        return ""
    return "\n- **本轮防套路复读铁律（极重要）**：" + "；".join(rules) + "！\n"


def build_unfetched_media_notice(
    raw_text: str,
    *,
    parsed_link_card: str | None = None,
    vision_b64: str | None = None,
    is_sticker_in: bool = False,
) -> str:
    """Returns an explicit prompt notice when a user sent a URL or image that could not be fetched/parsed,
    preventing the LLM from hallucinating/guessing its contents.
    """
    s = (raw_text or "").strip()
    if not s:
        return ""
    if not vision_b64 and not is_sticker_in and (s == "[图片]" or "<img" in s):
        return "（注：对方发了一张图片，但当前未能加载出画面；请直接自然告诉对方图没刷出来，严禁猜测或编造图片内容！）"
    if not parsed_link_card and URL_REGEX.search(s):
        return "（注：对方消息里包含网页链接，但当前受平台限制未能直接读取到链接正文；若对方问起链接内容，请老实说没刷出来让她发截图或复制文字，严禁顺着网址字母瞎猜编造！）"
    return ""


def build_time_gap_notice(last_ts: float | None, now_ts: float | None = None) -> str:
    """Returns a time-gap reminder when the previous message in the session was >= 45 minutes ago."""
    if not last_ts or float(last_ts) <= 0:
        return ""
    cur = float(now_ts if now_ts is not None else time.time())
    gap_sec = cur - float(last_ts)
    if gap_sec < 2700:
        return ""
    if gap_sec >= 7200:
        gap_str = f"{gap_sec / 3600:.1f} 小时"
    else:
        gap_str = f"{int(gap_sec // 60)} 分钟"
    return f"（迟到/间隔提醒：距离你们上一句对话已经过去了 {gap_str}，请按当前最新时间自然衔接，不要当成上一秒刚说的话硬接）"


def check_and_update_temper_state(
    temper_map: dict[tuple[str, str], dict[str, Any]],
    scope_id: str,
    sender_id: str,
    text: str,
) -> tuple[str, str | None, int]:
    """
    Tracks user taunts and coaxing per (scope_id, sender_id).
    Returns (prompt_injection_str, forced_meme_tag, mood_delta).
    """
    s = (text or "").strip()
    if not s:
        return "", None, 0

    now = time.time()
    key = (scope_id, sender_id)
    entry = temper_map.setdefault(key, {"taunt_times": [], "sulking_until": 0.0})
    taunt_times: list[float] = [t for t in entry.get("taunt_times", []) if now - t < 180.0]
    sulking_until = float(entry.get("sulking_until", 0.0))

    has_coax = any(kw in s for kw in COAX_KEYWORDS)
    has_taunt = any(kw in s for kw in TAUNT_KEYWORDS) or s in ("蠢", "笨", "傻")

    # 1. If user coaxes Dafeiyu while she is sulking (or right after taunting)
    if has_coax and (now < sulking_until or len(taunt_times) >= 1):
        entry["taunt_times"] = []
        entry["sulking_until"] = 0.0
        hint = (
            "\n- **当前特殊情绪小剧场（被哄好啦！）**：对方刚才惹你生气，现在主动来哄你/给你台阶下了！"
            "请表现出傲娇勉强原谅对方的样子（比如“看在你这么诚心哄我的份上 这次就勉为其难原谅你了”），语气得意又俏皮！\n"
        )
        return hint, random.choice(["得意", "比心", "干饭"]), 6

    # 2. If user taunts Dafeiyu
    if has_taunt:
        taunt_times.append(now)
        entry["taunt_times"] = taunt_times
        if len(taunt_times) >= 2 and now >= sulking_until:
            entry["sulking_until"] = now + 180.0
            hint = (
                "\n- **当前特殊情绪小剧场（触发炸毛赌气！）**：对方在短短两分钟内已经连续骂你/挑衅你多次了！"
                "你现在彻底炸毛赌气了，请在回复中傲娇放狠话宣布接下来不理TA了（除非拿好吃的/奶茶或者好话来哄你），字数简短有力！\n"
            )
            return hint, random.choice(["生气", "嫌弃"]), -4

    # 3. If Dafeiyu is currently in the 180s sulking window with this user
    if now < sulking_until:
        remain_sec = int(sulking_until - now)
        hint = (
            f"\n- **当前特殊情绪小剧场（正在跟对方赌气中，剩余 {remain_sec} 秒）**："
            "这个人刚才连着骂你/惹你生气，还没好好哄你呢！本轮只用一句简短高冷的傲娇话拿乔（比如“哼 还在生气呢 没奶茶免谈”、“刚骂完我又来找我说话，不理你”），不要顺着TA的话正常长篇回答！\n"
        )
        return hint, None, 0

    entry["taunt_times"] = taunt_times
    return "", None, 0


def is_voice_reply_requested(text: str) -> bool:
    """Returns True if the user naturally asked Dafeiyu to reply with a voice message."""
    s = (text or "").strip()
    if not s:
        return False
    return bool(VOICE_REQUEST_RE.search(s))


def _clean_text_for_tts(text: str) -> str:
    s = (text or "").strip()
    # Remove @mention prefix if present
    s = re.sub(r"^(?:@[^\s\u2005]+[\s\u2005]*)+", "", s).strip()
    # Remove [表情: xxx] tags and markdown/URLs
    s = re.sub(r"\[\s*表情\s*[:：][^\]]+\]", "", s)
    s = re.sub(r"https?://\S+", "", s)
    s = s.replace("||", "，").replace("/", "，")
    # Remove emojis / non-spoken symbols while keeping Chinese, letters, digits, and basic punctuation
    s = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9，。！？、～~…\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


async def synthesize_tts_mp3_b64(text: str, sf_api_key: str = "") -> str | None:
    """
    Synthesizes Dafeiyu's spoken reply into MP3 base64 using edge-tts (zh-CN-XiaoyiNeural)
    with fallback to SiliconFlow CosyVoice2.
    """
    spoken = _clean_text_for_tts(text)
    if not spoken or len(spoken) < 2 or len(spoken) > 140:
        return None

    # 1. Primary: Microsoft Edge Neural TTS (zh-CN-XiaoyiNeural - lively young female voice)
    try:
        import edge_tts

        comm = edge_tts.Communicate(spoken, voice="zh-CN-XiaoyiNeural", rate="+5%", pitch="+3Hz")
        audio_bytes = b""
        async for chunk in comm.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                audio_bytes += chunk["data"]
        if len(audio_bytes) > 512:
            return base64.b64encode(audio_bytes).decode("ascii")
    except Exception as e:
        logger.debug("edge-tts synthesis failed: %s", e)

    # 2. Fallback: SiliconFlow CosyVoice2 TTS API
    if sf_api_key:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(
                    "https://api.siliconflow.cn/v1/audio/speech",
                    headers={"Authorization": f"Bearer {sf_api_key}", "Content-Type": "application/json"},
                    json={
                        "model": "FunAudioLLM/CosyVoice2-0.5B",
                        "input": spoken,
                        "voice": "FunAudioLLM/CosyVoice2-0.5B:claire",
                        "response_format": "mp3",
                    },
                )
                if r.status_code == 200 and len(r.content) > 512:
                    return base64.b64encode(r.content).decode("ascii")
        except Exception as e:
            logger.debug("SiliconFlow TTS fallback failed: %s", e)

    return None


async def synthesize_and_send_wcf_voice(
    receiver: str,
    text: str,
    sf_api_key: str = "",
) -> bool:
    """Synthesizes TTS audio for `text` and sends it as a native WeChat 4.1 SILK v3 voice bubble."""
    audio_b64 = await synthesize_tts_mp3_b64(text, sf_api_key=sf_api_key)
    if not audio_b64:
        return False
    return await send_wcf_voice(receiver, audio_b64, filename="dafeiyu_voice.mp3")

