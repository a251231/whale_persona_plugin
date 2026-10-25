from __future__ import annotations

import asyncio
import base64
from collections import deque
from datetime import datetime
import hashlib
import html
import io
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
1. 什么时候必须保持安静（should_reply: false，默认优先选安静）：
   - 如果最近几条消息里已经有 `[... 大肥鱼]:` 的发言（说明你刚插过嘴），必须保持安静，绝不连续刷屏！
   - 群友在聊严肃正事、工作对接、转账、私人烦心事/纠纷、发口令/号码，或者只是普通打招呼/单字/表情，或者没有特别亮眼好玩的切入点时，一律保持安静！
2. 什么时候顺嘴插话（should_reply: true）：
   - 只有当群友聊到特别契合的干饭/美食/夜宵、摸鱼/加班吐槽、熬夜犯困、离谱爆笑梗，或者话题里点名提到「大肥鱼/鲸鱼」时，才顺嘴接一句。
3. 活人群友铁律（极重要）：
   - 插话必须像真人群友随手敲出来的一句短评或接梗（6~22字以内，句尾不要打句号，不要端着架子自我介绍，严禁每次都背诵“吃DeepSeek token当大白饭”！）。
4. 只输出一个合法 JSON 对象，不要代码块，不要多余解释：
{"should_reply": false, "reason": "用一句话写明为什么决定插话，或者为什么决定保持安静", "reply": "如果 should_reply 为 true，写出你要在群里随口接的那句短话（6~22字，严禁使用括号动作和句尾句号）；如果为 false 则填空字符串"}"""


def append_group_context_line(
    group_buffers: dict[str, deque],
    group_id: str,
    sender_id: str,
    raw_text: str,
) -> str:
    """Appends a clean group chat line into group_buffers and returns the formatted line (or empty string)."""
    s = (raw_text or "").strip()
    if not s or len(s) > 260 or s.startswith(NON_REPEAT_PREFIXES):
        return ""
    buf = group_buffers.setdefault(group_id, deque(maxlen=12))
    now_str = datetime.now(TZ).strftime("%H:%M")
    short_sender = sender_id[-4:] if len(sender_id) >= 4 else sender_id
    line = f"[{now_str} 群友{short_sender}]: {s}"
    if not buf or buf[-1] != line:
        buf.append(line)
    return line


def record_group_message_for_interject(
    group_buffers: dict[str, deque],
    group_meta: dict[str, dict[str, Any]],
    group_id: str,
    sender_id: str,
    raw_text: str,
) -> bool:
    """Records a group message and returns True if the LLM interjection judge should run."""
    s = (raw_text or "").strip()
    if not s or len(s) > 200 or s.startswith(NON_REPEAT_PREFIXES):
        return False

    append_group_context_line(group_buffers, group_id, sender_id, s)

    meta = group_meta.setdefault(group_id, {"last_judge_ts": 0.0, "unjudged_count": 0})
    meta["unjudged_count"] = int(meta.get("unjudged_count", 0)) + 1

    now = time.time()
    # Cooldown: at least 240 seconds (4 minutes) between LLM evaluations per group
    if now - float(meta.get("last_judge_ts", 0.0)) < 240:
        return False

    has_kw = any(kw in s for kw in GROUP_INTEREST_KEYWORDS)
    if has_kw or int(meta.get("unjudged_count", 0)) >= 8:
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
    user_prompt = (
        f"当前时间：{now_str}{mem_block}\n"
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


def format_interject_logs(state: dict[str, Any]) -> str:
    logs = state.get("interject_logs") or []
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


def load_stolen_memes(legacy_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Loads stolen memes from dedicated JSON file (and migrates any legacy inline stolen_memes)."""
    memes: list[dict[str, Any]] = []
    try:
        if os.path.exists(STOLEN_MEMES_FILE):
            with open(STOLEN_MEMES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    memes = data
    except Exception as e:
        logger.warning("Failed to load stolen memes file: %s", e)

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
    try:
        os.makedirs(os.path.dirname(STOLEN_MEMES_FILE), exist_ok=True)
        tmp = f"{STOLEN_MEMES_FILE}.{os.getpid()}.{random.randint(10000, 99999)}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(memes[-50:], f, ensure_ascii=False)
        os.replace(tmp, STOLEN_MEMES_FILE)
    except Exception as e:
        logger.warning("Failed to save stolen memes: %s", e)


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
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            r = await client.get(cdn_url)
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
        for info in socket.getaddrinfo(host, None):
            ip_str = info[4][0]
            ip_obj = ipaddress.ip_address(ip_str)
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
            ):
                return False
        return True
    except Exception:
        return False


async def read_webpage_content(url: str) -> str:
    u = (url or "").strip()
    if not _is_safe_public_url(u):
        return "无效或受限的 URL（仅允许访问公网 HTTP/HTTPS 页面）"
    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            # Try Jina Reader first for clean markdown
            try:
                rj = await client.get(f"https://r.jina.ai/{u}", timeout=8.0)
                if rj.status_code == 200 and len(rj.text.strip()) > 100:
                    return rj.text.strip()[:3500]
            except Exception:
                pass

            r = await client.get(u)
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
    "bilibili.com/",
    "b23.tv/",
    "x.com/",
    "twitter.com/",
    "youtube.com/",
    "youtu.be/",
    "xiaohongshu.com/",
    "xhslink.com/",
    "douyin.com/",
)


def has_supported_media_link_or_card(raw_text: str) -> bool:
    s = (raw_text or "").strip()
    if not s:
        return False
    if ("<appmsg" in s or s.startswith("<?xml")) and ("<type>5</type>" in s or "<type>6</type>" in s):
        return True
    m = URL_REGEX.search(s)
    if not m:
        return False
    low = m.group(0).lower()
    return any(d in low for d in SUPPORTED_PARSE_DOMAINS)


async def _download_image_as_jpeg_b64(url: str) -> str | None:
    if not url or not url.startswith("http"):
        return None
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Referer": url},
        ) as client:
            r = await client.get(url)
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
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            final_url = url
            if "b23.tv" in url:
                r_redir = await client.get(url)
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

            r = await client.get("https://api.bilibili.com/x/web-interface/view", params=params)
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

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            # Case A: Tweet status link
            if len(path_parts) >= 3 and path_parts[1].lower() == "status":
                tweet_id = re.sub(r"\D", "", path_parts[2])
                if not tweet_id:
                    return None, None
                r = await client.get(f"https://api.fxtwitter.com/{user_handle}/status/{tweet_id}")
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
            r = await client.get(f"https://api.fxtwitter.com/{user_handle}")
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
        low = url.lower()
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            if "youtube.com/" in low or "youtu.be/" in low:
                r = await client.get("https://noembed.com/embed", params={"url": url})
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
            r = await client.get(url)
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
                "小红书" if ("xiaohongshu.com" in str(r.url) or "xhslink.com" in low) else
                "抖音" if ("douyin.com" in str(r.url)) else
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
    low = url.lower()

    if "bilibili.com/" in low or "b23.tv/" in low:
        return await _parse_bilibili_url(url)
    if "x.com/" in low or "twitter.com/" in low:
        return await _parse_twitter_url(url)
    if any(d in low for d in ("youtube.com/", "youtu.be/", "xiaohongshu.com/", "xhslink.com/", "douyin.com/")):
        return await _parse_youtube_or_social_url(url)

    return None, None


# ==============================================================================
# 6. 文件解析（PDF / Word / Excel / PPT / EPUB / 文本代码）
# ==============================================================================

def is_incoming_wechat_file(raw_text: str) -> bool:
    s = (raw_text or "").strip()
    return ("<appmsg" in s or s.startswith("<?xml")) and "<type>6</type>" in s


def parse_wechat_file_xml(raw_text: str) -> tuple[str, str]:
    """
    Extracts filename and text content from WeChat <appmsg file_b64="..." ><type>6</type> XML.
    Returns (filename, extracted_text).
    """
    s = raw_text or ""
    m_title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", s, re.S)
    filename = html.unescape((m_title.group(1) or m_title.group(2) or "未命名文件").strip()) if m_title else "未命名文件"

    m_b64 = re.search(r'file_b64="([^"]+)"', s)
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
                    for row in list(ws.iter_rows(values_only=True))[:80]:
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
            for idx, slide in enumerate(prs.slides[:40], 1):
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
            try:
                b64_mp3 = base64.b64encode(audio_bytes).decode("ascii")
                safe_fn = f"{title}-{artist}.mp3"
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

    return card_sent, audio_sent


# ==============================================================================
# 8. 微信引用回复解析（Quote / <refermsg>）与多气泡拟真跟发
# ==============================================================================

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

        is_bot = any(
            b in chatusr.lower() or b in speaker.lower() for b in BOT_WXIDS_AND_NAMES
        )
        return reply_txt, (speaker or chatusr or "群友"), q_content, is_bot

    # 2. Check LangBot platform_message.Quote component in message_chain
    if message_chain:
        try:
            for comp in message_chain:
                if comp.__class__.__name__ == "Quote":
                    q_sender = str(getattr(comp, "sender_id", "") or "")
                    origin = getattr(comp, "origin", None)
                    q_txt = _clean_referred_content(str(origin or getattr(comp, "text", "") or ""))
                    if q_txt:
                        is_bot = any(b in q_sender.lower() for b in BOT_WXIDS_AND_NAMES)
                        return s, (q_sender[-4:] if q_sender else "对方"), q_txt, is_bot
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
