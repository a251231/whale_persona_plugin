"""Inbound media and content parsing: ZIP-safe documents + DeepSeek OCR, async PIL/GIF handling,
link/live-room/HTML parsers, web search, music search/delivery, TTS/ASR and meme stealing.

Extracted from ``components/event_listeners/whale_listener.py`` / ``diana_features.py``
(v0.5.2 A-12). Those modules remain the public compatibility facades: tests and callers keep
patching ``whale_listener`` / ``diana_features`` attributes, which the facades propagate here.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import io
import itertools
import json
import langbot_plugin.api.entities.builtin.platform.message as platform_message
import logging
import os
import random
import re
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.parse
import zipfile
from collections import OrderedDict
from datetime import datetime
from itertools import islice
from typing import Any, Optional

import httpx

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None  # type: ignore

try:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 20_000_000
except Exception:
    Image = None  # type: ignore

from . import df  # live facade proxies (see whale_core.__init__)
from .http_gateway import (
    DOUYIN_MOBILE_USER_AGENT,
    TZ,
    USER_AGENT,
    WCF_GATEWAY_URL,
    _WCF_BREAKER,
    _borrow_client,
    _get_public_url,
    _is_safe_public_url,
    _lru_ttl_get,
    _lru_ttl_set,
    _read_secrets_json_cached,
    _sanitize_public_error,
    _wcf_timeout,
    deepseek_tool_endpoint,
    get_cached_display_name,
    get_dynamic_bot_names,
    is_deepseek_api_host,
    send_wcf_voice,
)
from .state_repo import (
    DEFAULT_API_KEY,
    DEFAULT_BASE_URL,
    DEFAULT_SF_API_KEY,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ALL_MEME_CATEGORIES",
    "BILIBILI_LIVE_PATH_RE",
    "BINARY_UNSUPPORTED_FILE_EXTENSIONS",
    "COVER_DOWNLOAD_TIMEOUT_SEC",
    "DEEPSEEK_FETCH_CACHE_MAX",
    "DEEPSEEK_FETCH_CACHE_TTL_SEC",
    "DEEPSEEK_OCR_BLANK_RE",
    "DEEPSEEK_OCR_BOX_BLOCK_RE",
    "DEEPSEEK_OCR_COORDS_RE",
    "DEEPSEEK_OCR_TOKEN_RE",
    "DEEPSEEK_SEARCH_TOOLS",
    "DOUYIN_LIVE_REFLOW_PATH_RE",
    "DOUYIN_LIVE_WEB_PATH_RE",
    "DOUYIN_SITE_LIVE_PATH_RE",
    "IMAGE_FOLLOWUP_KEYWORDS",
    "MAX_DEEPSEEK_ASR_BYTES",
    "MAX_DEEPSEEK_OCR_BYTES",
    "MAX_PARSEABLE_FILE_BYTES",
    "MAX_PARSEABLE_FILE_XML_CHARS",
    "MediaMixin",
    "RANDOM_SONG_POOL",
    "SENSEVOICE_TAGS",
    "SILICONFLOW_ASR_MODEL",
    "SILICONFLOW_ASR_URL",
    "SILICONFLOW_CHAT_URL",
    "SILICONFLOW_VL_MODELS",
    "STOLEN_MEMES_BACKUP_FILE",
    "STOLEN_MEMES_FILE",
    "SUPPORTED_PARSE_DOMAINS",
    "URL_REGEX",
    "VISION_DESC_CACHE_MAX",
    "VISION_DESC_CACHE_TTL_SEC",
    "VOICE_REQUEST_RE",
    "XIAOHONGSHU_STATE_RE",
    "bilibili_live_room_id",
    "clean_deepseek_ocr_text",
    "compact_long_user_text",
    "describe_image_via_siliconflow",
    "douyin_live_target",
    "extract_and_parse_link",
    "extract_cookie_field_value",
    "extract_forwarded_chat_record",
    "extract_quote_context",
    "extract_xiaohongshu_note_from_state",
    "fetch_webpage_via_deepseek",
    "has_supported_media_link_or_card",
    "is_incoming_wechat_file",
    "is_voice_reply_requested",
    "load_stolen_memes",
    "normalize_qq_music_cookie",
    "ocr_via_deepseek",
    "parse_bilibili_live_url",
    "parse_douyin_live_url",
    "parse_wechat_file_xml",
    "parse_wechat_file_xml_async",
    "parse_wechat_mp_html",
    "parse_xiaohongshu_state",
    "pick_random_playable_song",
    "pick_stolen_or_official_meme",
    "read_webpage_content",
    "save_stolen_memes",
    "search_and_fetch_music",
    "search_via_deepseek",
    "search_web_parallel",
    "send_music_to_wechat",
    "send_wcf_music_and_audio",
    "steal_incoming_sticker",
    "synthesize_and_send_wcf_voice",
    "synthesize_tts_mp3_b64",
    "synthesize_tts_via_deepseek",
    "transcribe_audio_via_deepseek",
    "truncate_sticker_history_gist",
    "wrap_pcm24k_to_wav",
    "xiaohongshu_state_json",
    "xiaohongshu_video_candidate_urls",
]


# ==============================================================================
# from diana_features.py
# ==============================================================================
SILICONFLOW_CHAT_URL = "https://api.siliconflow.cn/v1/chat/completions"
SILICONFLOW_VL_MODELS = (
    "Qwen/Qwen3-VL-30B-A3B-Instruct",
    "Qwen/Qwen3-VL-8B-Instruct",
)
_VISION_DESC_CACHE: OrderedDict[str, tuple[float, str]] = OrderedDict()
VISION_DESC_CACHE_MAX = 128
VISION_DESC_CACHE_TTL_SEC = 600.0


def _resolve_sf_api_key(explicit_key: str = "") -> str:
    k = (explicit_key or os.getenv("SILICONFLOW_API_KEY", "")).strip()
    if k:
        return k
    candidates = [
        os.getenv("WHALE_SECRETS_FILE", "/app/data/whale_secrets.json"),
        os.getenv("WHALE_SECRETS_BACKUP_FILE", "/app/data/plugins/whale_secrets.json"),
        "/app/data/whale_secrets.json",
        "/app/data/plugins/whale_secrets.json",
    ]
    seen: set[str] = set()
    for p in candidates:
        if not p or p in seen:
            continue
        seen.add(p)
        d = _read_secrets_json_cached(p)
        if d.get("siliconflow_api_key"):
            return str(d["siliconflow_api_key"]).strip()
    return ""


async def describe_image_via_siliconflow(
    image_b64: str,
    sf_api_key: str = "",
    user_hint: str = "",
) -> str:
    """Uses SiliconFlow Qwen3-VL to extract OCR text and visual details from an image/sticker
    when the primary chat model (e.g. non-multimodal endpoint) is text-only.
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
    cached_hit = _lru_ttl_get(_VISION_DESC_CACHE, cache_key, VISION_DESC_CACHE_TTL_SEC, now_ts)
    if cached_hit:
        return cached_hit

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
            async with _borrow_client("api", timeout=12.0) as client:
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
                    timeout=12.0,
                )
                if r.status_code in (401, 402, 403):
                    logger.warning("SiliconFlow VL aborted early on HTTP %s", r.status_code)
                    return ""
                if r.status_code == 200:
                    data = r.json() if isinstance(r.json(), dict) else {}
                    desc = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                    if desc:
                        _lru_ttl_set(_VISION_DESC_CACHE, cache_key, desc, VISION_DESC_CACHE_MAX, now_ts)
                        return desc
        except Exception as e:
            logger.debug("SiliconFlow VL (%s) describe skipped: %s", vl_model, e)
    return ""

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
_STOLEN_MEMES_LOCK = asyncio.Lock()
_STOLEN_GIF_MAX_BYTES = 600 * 1024


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
        tmp = ""
        try:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            tmp = f"{target_path}.{os.getpid()}.{random.randint(10000, 99999)}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(trimmed, f, ensure_ascii=False)
            os.replace(tmp, target_path)
            tmp = ""
        except Exception as e:
            logger.warning("Failed to save stolen memes (%s): %s", target_path, e)
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def _encode_stolen_sticker_sync(raw_bytes: bytes) -> tuple[str, bool] | None:
    if Image is None or not raw_bytes:
        return None
    if raw_bytes[:6] in (b"GIF87a", b"GIF89a") and len(raw_bytes) <= _STOLEN_GIF_MAX_BYTES:
        return base64.b64encode(raw_bytes).decode("ascii"), True
    img = Image.open(io.BytesIO(raw_bytes))
    if img.width * img.height > 20_000_000:
        raise ValueError("Sticker image dimensions exceed safety limit")
    n_frames = getattr(img, "n_frames", 1)
    if n_frames > 1:
        img.seek(n_frames // 2)
    rgb = img.convert("RGB")
    rgb.thumbnail((300, 300))
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=84)
    return base64.b64encode(buf.getvalue()).decode("ascii"), False


async def steal_incoming_sticker(
    xml_text: str,
    state: dict[str, Any],
    stolen_list: list[dict[str, Any]],
    hint_tag: str | None = None,
) -> bool:
    """Downloads a clean static JPEG (or compact <=600KiB GIF) of an incoming WeChat sticker and persists it."""
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

    if sticker_md5:
        async with _STOLEN_MEMES_LOCK:
            if any(item.get("md5") == sticker_md5 for item in stolen_list):
                if hint_tag:
                    updated = False
                    for item in stolen_list:
                        if item.get("md5") == sticker_md5 and hint_tag not in item.get("tags", []):
                            item.setdefault("tags", []).append(hint_tag)
                            updated = True
                    if updated:
                        await asyncio.to_thread(save_stolen_memes, list(stolen_list))
                return False

    try:
        async with _borrow_client("public", timeout=12.0) as client:
            r = await _get_public_url(client, cdn_url, max_bytes=5 * 1024 * 1024, timeout=12.0)
            if r.status_code != 200 or not r.content:
                return False
            raw_bytes = r.content
    except Exception:
        return False

    if not sticker_md5:
        sticker_md5 = hashlib.md5(raw_bytes).hexdigest()

    try:
        encoded = await asyncio.to_thread(_encode_stolen_sticker_sync, raw_bytes)
        if not encoded:
            return False
        single_b64, is_gif = encoded
    except Exception:
        return False

    tags = [hint_tag] if hint_tag else ["搞笑", "得意"]
    async with _STOLEN_MEMES_LOCK:
        if any(item.get("md5") == sticker_md5 for item in stolen_list):
            return False
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
        await asyncio.to_thread(save_stolen_memes, list(stolen_list))
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


def truncate_sticker_history_gist(description: str, max_chars: int = 120) -> str:
    """Compacts a sticker description into a single-line gist truncated to `max_chars` characters
    for conversation history, avoiding multi-line visual descriptions in history turns (Diana #1544).
    """
    compact = re.sub(r"\s+", " ", (description or "").strip())
    if not compact:
        return ""
    limit = max(1, int(max_chars or 120))
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "…"
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


def _parse_bing_search_html_sync(html_body: str) -> list[str]:
    if BeautifulSoup is None or not html_body:
        return []
    out: list[str] = []
    soup = BeautifulSoup(html_body, "html.parser")
    for li in soup.select("li.b_algo")[:5]:
        a_tag = li.select_one("h2 a")
        p_tag = li.select_one(".b_caption p") or li.select_one("p")
        if a_tag:
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            snippet = p_tag.get_text(" ", strip=True) if p_tag else ""
            if title and href.startswith("http"):
                out.append(f"- {title}\n  链接: {href}\n  摘要: {snippet[:180]}")
    return out


def _parse_sogou_search_html_sync(html_body: str) -> list[str]:
    if BeautifulSoup is None or not html_body:
        return []
    out: list[str] = []
    soup = BeautifulSoup(html_body, "html.parser")
    for vr in soup.select("div.vrwrap, div.rb")[:5]:
        h3 = vr.select_one("h3.vr-title, h3.pt")
        desc = vr.select_one("div.str-text-info, div.ft, p.str_info")
        if h3:
            title = h3.get_text(strip=True)
            snippet = desc.get_text(" ", strip=True) if desc else ""
            if title:
                out.append(f"- {title}\n  摘要: {snippet[:180]}")
    return out


async def _search_single_query(query: str, search_api_key: str = "") -> str:
    q = (query or "").strip()
    if not q:
        return ""

    # 1. Optional Tavily API if search_api_key starts with tvly-
    if search_api_key.startswith("tvly-"):
        try:
            async with _borrow_client("api", timeout=12.0) as client:
                r = await client.post(
                    "https://api.tavily.com/search",
                    json={"api_key": search_api_key, "query": q, "max_results": 5},
                    timeout=12.0,
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

    # 2. Built-in Bing CN + Sogou HTML Search (zero-key, SSRF-checked & size-capped)
    results_out: list[str] = []
    try:
        encoded = urllib.parse.quote(q)
        async with _borrow_client("public", timeout=10.0) as client:
            r = await _get_public_url(
                client,
                f"https://cn.bing.com/search?q={encoded}&ensearch=0",
                max_bytes=2 * 1024 * 1024,
                timeout=10.0,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            if r.status_code == 200 and BeautifulSoup is not None:
                results_out = await asyncio.to_thread(_parse_bing_search_html_sync, r.text)
    except Exception as e:
        logger.debug("Bing search error: %s", e)

    if not results_out:
        try:
            encoded = urllib.parse.quote(q)
            async with _borrow_client("public", timeout=10.0) as client:
                r = await _get_public_url(
                    client,
                    f"https://www.sogou.com/web?query={encoded}",
                    max_bytes=2 * 1024 * 1024,
                    timeout=10.0,
                    headers={"User-Agent": USER_AGENT},
                )
                if r.status_code == 200 and BeautifulSoup is not None:
                    results_out = await asyncio.to_thread(_parse_sogou_search_html_sync, r.text)
        except Exception as e:
            logger.debug("Sogou search error: %s", e)

    if not results_out:
        return f"【搜索：{q}】未检索到直接匹配的网页摘要。"
    return f"【搜索：{q}】\n" + "\n".join(results_out[:5])


async def search_via_deepseek(
    query: str,
    deepseek_api_key: str,
    deepseek_base_url: str = "https://api.deepseek.com",
    count: int = 5,
) -> str:
    """Queries DeepSeek `/v1/tools/search` and formats results preferring `summary` over `snippet` (Diana #1557)."""
    q = (query or "").strip()[:128]
    key = (deepseek_api_key or "").strip()
    if not q or not key:
        return ""
    clamped_count = max(1, min(int(count or 5), 50))
    endpoint = deepseek_tool_endpoint(deepseek_base_url, "search")
    try:
        async with _borrow_client("api", timeout=15.0) as client:
            r = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={"query": q, "count": clamped_count},
                timeout=15.0,
            )
            if r.status_code != 200:
                return ""
            payload = r.json()
            results = (payload.get("results") if isinstance(payload, dict) else None) or []
            if not isinstance(results, list) or not results:
                return ""
            lines = [f"【搜索：{q}】"]
            for idx, item in enumerate(results[:clamped_count], 1):
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip() or "无标题"
                url_str = str(item.get("url") or "").strip()
                summary = str(item.get("summary") or "").strip() or str(item.get("snippet") or "").strip()
                date_str = str(item.get("date") or "").strip()
                head = (
                    f"{idx}. {title}"
                    + (f" ({url_str})" if url_str else "")
                    + (f" [{date_str[:10]}]" if date_str else "")
                )
                if summary:
                    lines.append(f"{head}\n   摘要：{summary[:600]}")
                else:
                    lines.append(head)
            return "\n".join(lines) if len(lines) > 1 else ""
    except Exception as e:
        logger.debug("DeepSeek search fallback skipped: %s", e)
        return ""


async def search_web_parallel(
    queries: list[str],
    search_api_key: str = "",
    *,
    deepseek_api_key: str = "",
    deepseek_base_url: str = "",
) -> str:
    clean_qs = [str(q).strip() for q in (queries or []) if str(q).strip()][:3]
    if not clean_qs:
        return "无搜索词"
    tasks = [_search_single_query(q, search_api_key) for q in clean_qs]
    parts = list(await asyncio.gather(*tasks, return_exceptions=True))

    if (deepseek_api_key or "").strip() and is_deepseek_api_host(deepseek_base_url):
        missed_indices = [
            idx
            for idx, p in enumerate(parts)
            if (not isinstance(p, str)) or (not p.strip()) or ("未检索到直接匹配的网页摘要" in p)
        ]
        if missed_indices:
            ds_tasks = [
                search_via_deepseek(
                    clean_qs[idx],
                    deepseek_api_key=deepseek_api_key,
                    deepseek_base_url=deepseek_base_url or "https://api.deepseek.com",
                )
                for idx in missed_indices
            ]
            ds_results = await asyncio.gather(*ds_tasks, return_exceptions=True)
            for idx, ds_res in zip(missed_indices, ds_results):
                if isinstance(ds_res, str) and ds_res.strip():
                    parts[idx] = ds_res

    out = [str(p) for p in parts if isinstance(p, str) and p.strip()]
    return "\n\n".join(out) if out else "未获取到搜索结果"
_DEEPSEEK_FETCH_CACHE: OrderedDict[str, tuple[float, str]] = OrderedDict()
DEEPSEEK_FETCH_CACHE_MAX = 128
DEEPSEEK_FETCH_CACHE_TTL_SEC = 900.0


async def fetch_webpage_via_deepseek(
    url: str,
    deepseek_api_key: str,
    deepseek_base_url: str = "https://api.deepseek.com",
) -> str:
    """Fetches clean webpage Markdown via DeepSeek `/v1/tools/fetch` with SSRF check and 15-min cache (Diana #1557)."""
    u = (url or "").strip()
    key = (deepseek_api_key or "").strip()
    if not u or not key:
        return ""
    if not await asyncio.to_thread(_is_safe_public_url, u):
        return ""

    endpoint = deepseek_tool_endpoint(deepseek_base_url, "fetch")
    cache_key = f"{endpoint}|{u}"
    now_ts = time.time()
    cached = _lru_ttl_get(_DEEPSEEK_FETCH_CACHE, cache_key, DEEPSEEK_FETCH_CACHE_TTL_SEC, now_ts)
    if cached:
        return cached

    try:
        async with _borrow_client("api", timeout=25.0) as client:
            r = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={"url": u},
                timeout=25.0,
            )
            if r.status_code < 200 or r.status_code >= 300 or len(r.content) > 2 * 1024 * 1024:
                return ""
            data = r.json()
            if not isinstance(data, dict):
                return ""
            content = str(data.get("content") or "").strip()
            if not content:
                return ""
            title = re.sub(r"\s+", " ", str(data.get("title") or "").strip())
            formatted = f"标题: {title}\n\n{content[:3500]}" if title else content[:3500]
            _lru_ttl_set(_DEEPSEEK_FETCH_CACHE, cache_key, formatted, DEEPSEEK_FETCH_CACHE_MAX, now_ts)
            return formatted
    except Exception as e:
        logger.debug("DeepSeek webpage fetch fallback skipped: %s", e)
        return ""


def _extract_webpage_text_sync(html_body: str) -> str:
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html_body, "html.parser")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
        lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) > 2]
        return "\n".join(lines)[:3500]
    return (html_body or "")[:2500]


async def read_webpage_content(
    url: str,
    *,
    deepseek_api_key: str = "",
    deepseek_base_url: str = "",
) -> str:
    u = (url or "").strip()
    if not await asyncio.to_thread(_is_safe_public_url, u):
        return "无效或受限的 URL（仅允许访问公网 HTTP/HTTPS 页面）"

    use_ds_fallback = bool((deepseek_api_key or "").strip() and is_deepseek_api_host(deepseek_base_url))
    fallback_err_msg = ""
    extracted_short = ""

    try:
        async with _borrow_client("public", timeout=12.0) as client:
            r = await _get_public_url(
                client,
                u,
                max_bytes=2 * 1024 * 1024,
                timeout=12.0,
                headers={"User-Agent": USER_AGENT},
            )
            if r.status_code != 200:
                fallback_err_msg = f"网页访问返回状态码 {r.status_code}"
            else:
                extracted = await asyncio.to_thread(_extract_webpage_text_sync, r.text)
                if len(extracted.strip()) >= 60 or not use_ds_fallback:
                    return extracted
                extracted_short = extracted
    except ValueError as ve:
        return f"读取网页失败: {_sanitize_public_error(ve)}"
    except Exception as e:
        fallback_err_msg = f"读取网页失败: {_sanitize_public_error(e)}"

    if use_ds_fallback:
        ds_content = await fetch_webpage_via_deepseek(
            u,
            deepseek_api_key=deepseek_api_key,
            deepseek_base_url=deepseek_base_url or "https://api.deepseek.com",
        )
        if ds_content:
            return ds_content

    if fallback_err_msg:
        return fallback_err_msg
    return extracted_short

# ==============================================================================
# 5. 全平台链接解析（B站视频与直播 / YouTube / X(Twitter) / 小红书 / 抖音视频与直播 / 微信卡片）
# ==============================================================================

URL_REGEX = re.compile(r"https?://[^\s<>\"'）)】\]]+")
SUPPORTED_PARSE_DOMAINS = (
    "live.bilibili.com",
    "bilibili.com",
    "b23.tv",
    "x.com",
    "twitter.com",
    "youtube.com",
    "youtu.be",
    "xiaohongshu.com",
    "xhslink.com",
    "webcast.amemv.com",
    "webcast.douyin.com",
    "live.douyin.com",
    "douyin.com",
    "mp.weixin.qq.com",
)
BILIBILI_LIVE_PATH_RE = re.compile(r"^/(?:h5/|blanc/)?([0-9]+)/?$")
DOUYIN_LIVE_WEB_PATH_RE = re.compile(r"^/([0-9]+)/?$")
DOUYIN_LIVE_REFLOW_PATH_RE = re.compile(r"/webcast/reflow/([0-9]+)")
DOUYIN_SITE_LIVE_PATH_RE = re.compile(r"^/(?:follow|root)/live/([0-9]+)/?$")
COVER_DOWNLOAD_TIMEOUT_SEC: float = 8.0


def _host_matches_domain(host: str, domain: str) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    d = (domain or "").strip().lower().rstrip(".")
    return bool(h and d and (h == d or h.endswith("." + d)))


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


def bilibili_live_room_id(url: str) -> str:
    """Extracts the numeric room ID from a `live.bilibili.com` room URL (Diana #1563)."""
    try:
        parsed = urllib.parse.urlparse((url or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        return ""
    if not _host_matches_domain(parsed.hostname or "", "live.bilibili.com"):
        return ""
    m = BILIBILI_LIVE_PATH_RE.match(parsed.path or "")
    return m.group(1) if m else ""


def douyin_live_target(url: str) -> tuple[str, str]:
    """Recognizes Douyin live room URLs, returning `(room_id, web_rid)` (Diana #1563)."""
    try:
        parsed = urllib.parse.urlparse((url or "").strip())
    except ValueError:
        return "", ""
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        return "", ""
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    path = parsed.path or ""

    if _host_matches_domain(host, "webcast.amemv.com") or _host_matches_domain(host, "webcast.douyin.com"):
        m = DOUYIN_LIVE_REFLOW_PATH_RE.search(path)
        if m:
            return m.group(1), ""
        qs = urllib.parse.parse_qs(parsed.query)
        rid_vals = qs.get("roomId") or qs.get("room_id") or []
        rid = (rid_vals[0] if rid_vals else "").strip()
        if re.fullmatch(r"[0-9]+", rid):
            return rid, ""
    elif _host_matches_domain(host, "live.douyin.com"):
        m = DOUYIN_LIVE_WEB_PATH_RE.match(path)
        if m:
            return "", m.group(1)
    elif _host_matches_domain(host, "douyin.com"):
        m = DOUYIN_SITE_LIVE_PATH_RE.match(path)
        if m:
            return "", m.group(1)
    return "", ""


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


def _encode_image_to_jpeg_b64_sync(raw_bytes: bytes) -> str:
    if Image is not None:
        img = Image.open(io.BytesIO(raw_bytes))
        if img.width * img.height > 20_000_000:
            raise ValueError("Image dimensions exceed safety limit")
        rgb = img.convert("RGB")
        rgb.thumbnail((640, 640))
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=84)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    return base64.b64encode(raw_bytes).decode("ascii")


async def _download_image_as_jpeg_b64(url: str) -> str | None:
    if not url or not url.startswith("http"):
        return None
    try:
        async with _borrow_client("public", timeout=10.0) as client:
            r = await _get_public_url(
                client,
                url,
                max_bytes=5 * 1024 * 1024,
                timeout=10.0,
                headers={"User-Agent": USER_AGENT, "Referer": url},
            )
            if r.status_code == 200 and r.content:
                return await asyncio.to_thread(_encode_image_to_jpeg_b64_sync, r.content)
    except Exception:
        pass
    return None


async def _download_cover_preserving_metadata(url: str) -> str | None:
    """Downloads cover/media preview image with a bounded timeout, catching any timeout/error
    so already-parsed link metadata is never discarded (Diana #1583).
    """
    if not url:
        return None
    try:
        return await asyncio.wait_for(
            _download_image_as_jpeg_b64(url),
            timeout=COVER_DOWNLOAD_TIMEOUT_SEC,
        )
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug("Cover download skipped/timed out, preserving parsed metadata: %s", e)
        return None


async def parse_bilibili_live_url(url: str) -> tuple[str | None, str | None]:
    """Parses a `live.bilibili.com` room link into title, anchor, status, area, popularity, and cover (Diana #1563)."""
    room_id = bilibili_live_room_id(url)
    if not room_id:
        return None, None
    try:
        referer = f"https://live.bilibili.com/{room_id}"
        bili_headers = {"User-Agent": USER_AGENT, "Referer": referer}
        clean_sess = extract_cookie_field_value(os.getenv("BILIBILI_SESSDATA", ""), "SESSDATA")
        if clean_sess:
            bili_headers["Cookie"] = f"SESSDATA={clean_sess}"
        async with _borrow_client("public", timeout=10.0) as client:
            q_room = urllib.parse.quote(room_id)
            info_url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={q_room}"
            r_room = await _get_public_url(
                client, info_url, max_bytes=2 * 1024 * 1024, timeout=10.0, headers=bili_headers
            )
            if r_room.status_code != 200:
                return None, None
            room_json = r_room.json()
            if not isinstance(room_json, dict) or room_json.get("code") != 0 or not isinstance(room_json.get("data"), dict):
                return None, None
            data = room_json["data"]
            if not data:
                return None, None

            uname = str(data.get("uname") or "").strip()
            try:
                anchor_url = f"https://api.live.bilibili.com/live_user/v1/UserInfo/get_anchor_in_room?roomid={q_room}"
                r_anc = await _get_public_url(
                    client, anchor_url, max_bytes=2 * 1024 * 1024, timeout=10.0, headers=bili_headers
                )
                if r_anc.status_code == 200:
                    anc_json = r_anc.json()
                    if isinstance(anc_json, dict) and anc_json.get("code") == 0:
                        anc_uname = str((((anc_json.get("data") or {}).get("info") or {}).get("uname") or "")).strip()
                        if anc_uname:
                            uname = anc_uname
            except (asyncio.TimeoutError, Exception) as anc_err:
                logger.debug("Bilibili live anchor lookup skipped: %s", anc_err)

        title = str(data.get("title") or "").strip() or "直播间"
        try:
            live_status = int(data.get("live_status") or 0)
        except Exception:
            live_status = 0
        if live_status == 1:
            status_text = "直播中"
        elif live_status == 2:
            status_text = "未开播（轮播中）"
        else:
            status_text = "未开播"

        parent_area = str(data.get("parent_area_name") or "").strip()
        area_name = str(data.get("area_name") or "").strip()
        if parent_area and area_name and parent_area != area_name:
            area_str = f"{parent_area} · {area_name}"
        else:
            area_str = area_name or parent_area

        try:
            online = int(data.get("online") or 0)
        except Exception:
            online = 0

        lines = [f"[B站直播] {title}"]
        if uname:
            lines.append(f"主播：{uname}")
        lines.append(f"状态：{status_text}")
        if area_str:
            lines.append(f"分区：{area_str}")
        if live_status == 1 and online > 0:
            lines.append(f"人气：{online}")
        summary_text = "\n".join(lines)

        cover_url = str(data.get("user_cover") or data.get("keyframe") or "").strip()
        if cover_url.startswith("//"):
            cover_url = "https:" + cover_url
        cover_b64 = await _download_cover_preserving_metadata(cover_url)
        return summary_text, cover_b64
    except Exception as e:
        logger.debug("Bilibili live parse error: %s", e)
        return None, None


async def parse_douyin_live_url(url: str) -> tuple[str | None, str | None]:
    """Parses Douyin live room links (`webcast.amemv.com` reflow or `live.douyin.com` web_rid) (Diana #1563)."""
    try:
        room_id, web_rid = douyin_live_target(url)
        async with _borrow_client("public", timeout=10.0) as client:
            if not room_id and not web_rid and _supported_parse_domain(url) == "douyin.com":
                r_redir = await _get_public_url(
                    client, url, max_bytes=2 * 1024 * 1024, timeout=10.0, headers={"User-Agent": USER_AGENT}
                )
                room_id, web_rid = douyin_live_target(str(r_redir.url))
            if not room_id and not web_rid:
                return None, None

            room: dict[str, Any] | None = None
            fallback_anchor = ""

            if room_id:
                q_room = urllib.parse.quote(room_id)
                reflow_url = (
                    f"https://webcast.amemv.com/webcast/room/reflow/info/"
                    f"?type_id=0&live_id=1&room_id={q_room}&app_id=1128"
                )
                r = await _get_public_url(
                    client,
                    reflow_url,
                    max_bytes=2 * 1024 * 1024,
                    timeout=10.0,
                    headers={
                        "User-Agent": DOUYIN_MOBILE_USER_AGENT,
                        "Referer": "https://www.douyin.com/",
                    },
                )
                if r.status_code != 200:
                    return None, None
                payload = r.json()
                if not isinstance(payload, dict) or int(payload.get("status_code") or 0) != 0:
                    return None, None
                cand_room = (payload.get("data") or {}).get("room")
                if isinstance(cand_room, dict) and cand_room:
                    room = cand_room
            else:
                raw_ck = (os.getenv("DOUYIN_CK", "") or os.getenv("DIANA_DOUYIN_CK", "")).strip()
                cookie_hdr = ""
                ttwid = ""
                if raw_ck:
                    if "=" in raw_ck or ";" in raw_ck:
                        ttwid = extract_cookie_field_value(raw_ck, "ttwid")
                        cookie_hdr = re.sub(r"^cookie\s*:\s*", "", raw_ck, flags=re.I).strip()
                    else:
                        ttwid = raw_ck.strip('"').strip("'")
                        cookie_hdr = f"ttwid={ttwid}"

                if not ttwid:
                    r_home = await _get_public_url(
                        client,
                        "https://live.douyin.com/",
                        max_bytes=2 * 1024 * 1024,
                        timeout=10.0,
                        headers={"User-Agent": USER_AGENT, "Referer": "https://live.douyin.com/"},
                    )
                    ttwid = (r_home.cookies.get("ttwid") or "").strip()
                    if not ttwid:
                        for sc_val in r_home.headers.get_list("set-cookie"):
                            m_tt = re.search(r"(?:^|[;\s,])ttwid=([^;\s,]+)", sc_val)
                            if m_tt:
                                ttwid = m_tt.group(1).strip()
                                break
                    if ttwid:
                        cookie_hdr = f"{cookie_hdr}; ttwid={ttwid}".lstrip("; ").strip()

                enter_headers: dict[str, str] = {
                    "User-Agent": USER_AGENT,
                    "Referer": "https://live.douyin.com/",
                }
                if cookie_hdr:
                    enter_headers["Cookie"] = cookie_hdr
                q_wrid = urllib.parse.quote(web_rid)
                enter_url = (
                    f"https://live.douyin.com/webcast/room/web/enter/"
                    f"?aid=6383&app_name=douyin_web&live_id=1&device_platform=web&language=zh-CN&web_rid={q_wrid}"
                )
                r = await _get_public_url(
                    client, enter_url, max_bytes=2 * 1024 * 1024, timeout=10.0, headers=enter_headers
                )
                if r.status_code != 200:
                    return None, None
                payload = r.json()
                if not isinstance(payload, dict) or int(payload.get("status_code") or 0) != 0:
                    return None, None
                data_sec = payload.get("data") or {}
                if isinstance(data_sec, dict):
                    fallback_anchor = str((data_sec.get("user") or {}).get("nickname") or "").strip()
                    rooms_list = data_sec.get("data")
                    if isinstance(rooms_list, list) and rooms_list and isinstance(rooms_list[0], dict):
                        room = rooms_list[0]
                    elif isinstance(data_sec.get("room"), dict) and data_sec.get("room"):
                        room = data_sec["room"]

            if not room:
                return None, None

        title = str(room.get("title") or "").strip() or "直播间"
        anchor = str((room.get("owner") or {}).get("nickname") or fallback_anchor or "").strip()
        try:
            is_live = int(room.get("status") or 0) == 2
        except Exception:
            is_live = False
        status_text = "直播中" if is_live else "未开播"
        try:
            online = int(room.get("user_count") or 0)
        except Exception:
            online = 0

        lines = [f"[抖音直播] {title}"]
        if anchor:
            lines.append(f"主播：{anchor}")
        lines.append(f"状态：{status_text}")
        if is_live and online > 0:
            lines.append(f"人气：{online}")
        summary_text = "\n".join(lines)

        url_list = (room.get("cover") or {}).get("url_list") or []
        cover_url = next((str(u).strip() for u in url_list if isinstance(u, str) and str(u).strip()), "")
        if cover_url.startswith("//"):
            cover_url = "https:" + cover_url
        cover_b64 = await _download_cover_preserving_metadata(cover_url)
        return summary_text, cover_b64
    except Exception as e:
        logger.debug("Douyin live parse error: %s", e)
        return None, None


async def _parse_bilibili_url(url: str, sessdata: str = "") -> tuple[str | None, str | None]:
    try:
        if bilibili_live_room_id(url):
            return await parse_bilibili_live_url(url)

        bili_headers = {"User-Agent": USER_AGENT}
        clean_sess = extract_cookie_field_value(sessdata or os.getenv("BILIBILI_SESSDATA", ""), "SESSDATA")
        if clean_sess:
            bili_headers["Cookie"] = f"SESSDATA={clean_sess}"
        async with _borrow_client("public", timeout=10.0) as client:
            final_url = url
            if _supported_parse_domain(url) == "b23.tv":
                r_redir = await _get_public_url(
                    client, url, max_bytes=2 * 1024 * 1024, timeout=10.0, headers=bili_headers
                )
                final_url = str(r_redir.url)

            if bilibili_live_room_id(final_url):
                return await parse_bilibili_live_url(final_url)

            m_bv = re.search(r"(BV[0-9A-Za-z]{10})", final_url)
            m_av = re.search(r"av(\d+)", final_url, re.I)
            params: dict[str, str] = {}
            if m_bv:
                params["bvid"] = m_bv.group(1)
            elif m_av:
                params["aid"] = m_av.group(1)
            else:
                return None, None

            r = await _get_public_url(
                client,
                "https://api.bilibili.com/x/web-interface/view",
                max_bytes=2 * 1024 * 1024,
                params=params,
                timeout=10.0,
                headers=bili_headers,
            )
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

        card = (
            f"📺 【B站视频解析】{title}\n"
            f"👤 UP主：{owner}  |  ▶️ 播放：{views}  |  👍 点赞：{likes}  |  💬 弹幕：{danmaku}\n"
            f"📝 简介：{desc or '无简介'}\n"
            f"🔗 https://www.bilibili.com/video/{bvid}"
        )
        cover_b64 = await _download_cover_preserving_metadata(pic)
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

        async with _borrow_client("public", timeout=10.0) as client:
            # Case A: Tweet status link
            if len(path_parts) >= 3 and path_parts[1].lower() == "status":
                tweet_id = re.sub(r"\D", "", path_parts[2])
                if not tweet_id:
                    return None, None
                r = await _get_public_url(
                    client,
                    f"https://api.fxtwitter.com/{user_handle}/status/{tweet_id}",
                    max_bytes=2 * 1024 * 1024,
                    timeout=10.0,
                    headers={"User-Agent": USER_AGENT},
                )
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

                card = (
                    f"🐦 【X / Twitter 推文】{name} (@{screen_name})\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"{text}\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"❤️ 喜欢：{likes}  |  🔁 转推：{retweets}  |  👁️ 浏览：{views}\n"
                    f"🔗 https://x.com/{screen_name}/status/{tweet_id}"
                )
                img_b64 = await _download_cover_preserving_metadata(media_url)
                return card, img_b64

            # Case B: X / Twitter User Profile Page
            r = await _get_public_url(
                client,
                f"https://api.fxtwitter.com/{user_handle}",
                max_bytes=2 * 1024 * 1024,
                timeout=10.0,
                headers={"User-Agent": USER_AGENT},
            )
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

            card = (
                f"👤 【X / Twitter 用户主页】{name} (@{screen_name})\n"
                f"📝 简介：{desc}\n"
                f"📊 粉丝：{followers}  |  关注：{following}  |  推文：{tweets}"
                + (f"  |  加入于：{joined}" if joined else "")
                + f"\n🔗 https://x.com/{screen_name}"
            )
            avatar_b64 = await _download_cover_preserving_metadata(avatar_url)
            return card, avatar_b64
    except Exception as e:
        logger.debug("Twitter parse error: %s", e)
        return None, None
XIAOHONGSHU_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*(?:;</script>|</script>)",
    re.S,
)


def _is_js_ident_char(ch: str) -> bool:
    return ch == "_" or ch == "$" or ch.isalnum()


def xiaohongshu_state_json(literal: str) -> str:
    """Converts Xiaohongshu's JS object literal in window.__INITIAL_STATE__ into valid JSON.
    Replaces outside-string `undefined` with `null` and `new Map(...)` / `new Set(...)` with
    their inner array (`[]` when empty), while preserving string contents verbatim (Diana #1419).
    """
    out: list[str] = []
    in_string = False
    escaped = False
    n = len(literal)
    i = 0
    while i < n:
        c = literal[i]
        if in_string:
            out.append(c)
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            i += 1
            continue

        rest = literal[i:]
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
        elif rest.startswith("new Map(") or rest.startswith("new Set("):
            i += len("new Map(")
            if i < n and literal[i] == ")":
                out.append("[]")
                i += 1
        elif c == ")":
            # Closing paren outside strings belongs to new Map(...) / new Set(...)
            i += 1
        elif (
            rest.startswith("undefined")
            and (i == 0 or not _is_js_ident_char(literal[i - 1]))
            and (len(rest) == len("undefined") or not _is_js_ident_char(rest[len("undefined")]))
        ):
            out.append("null")
            i += len("undefined")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse_xiaohongshu_state(html_body: str) -> dict[str, Any] | None:
    """Extracts and parses window.__INITIAL_STATE__ from Xiaohongshu HTML."""
    m = XIAOHONGSHU_STATE_RE.search(html_body or "")
    if not m:
        return None
    try:
        parsed = json.loads(xiaohongshu_state_json(m.group(1)))
        return parsed if isinstance(parsed, dict) else None
    except Exception as e:
        logger.debug("parse_xiaohongshu_state failed: %s", e)
        return None


def xiaohongshu_video_candidate_urls(note: dict[str, Any]) -> list[str]:
    """Extracts candidate video stream URLs from a Xiaohongshu note, including stream.EF4 (Diana #1419)."""
    if not isinstance(note, dict):
        return []
    stream = (((note.get("video") or {}).get("media") or {}).get("stream")) or {}
    if not isinstance(stream, dict):
        return []
    urls: list[str] = []
    for key in ("h264", "EF4", "h265", "av1"):
        entries = stream.get(key)
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            master = str(item.get("masterUrl") or "").strip()
            if master and master not in urls:
                urls.append(master)
            for b_url in item.get("backupUrls") or []:
                b_str = str(b_url or "").strip()
                if b_str and b_str not in urls:
                    urls.append(b_str)
    return urls


def extract_xiaohongshu_note_from_state(
    state: dict[str, Any], note_id: str = ""
) -> dict[str, Any] | None:
    """Extracts the primary note object from Xiaohongshu window.__INITIAL_STATE__."""
    if not isinstance(state, dict):
        return None
    note_sec = state.get("note") or {}
    if not isinstance(note_sec, dict):
        return None
    detail_map = note_sec.get("noteDetailMap") or {}
    if isinstance(detail_map, dict) and detail_map:
        if note_id and isinstance(detail_map.get(note_id), dict):
            n_obj = detail_map[note_id].get("note")
            if isinstance(n_obj, dict) and n_obj:
                return n_obj
        for entry in detail_map.values():
            if isinstance(entry, dict) and isinstance(entry.get("note"), dict) and entry["note"]:
                return entry["note"]
    if isinstance(note_sec.get("note"), dict) and note_sec["note"]:
        return note_sec["note"]
    return None


def _parse_social_html_sync(
    html_body: str, final_social_url: str, domain: str
) -> tuple[str | None, str]:
    if domain in ("xiaohongshu.com", "xhslink.com"):
        xhs_state = parse_xiaohongshu_state(html_body)
        if xhs_state:
            m_nid = re.search(r"/(?:explore|discovery/item)/([0-9a-zA-Z]+)", final_social_url)
            xhs_note = extract_xiaohongshu_note_from_state(
                xhs_state, m_nid.group(1) if m_nid else ""
            )
            if xhs_note:
                n_title = str(xhs_note.get("title") or "").strip()
                n_desc = str(xhs_note.get("desc") or "").strip()[:240]
                n_user = str((xhs_note.get("user") or {}).get("nickname") or "").strip()
                img_list = xhs_note.get("imageList") or []
                first_img = ""
                if isinstance(img_list, list) and img_list and isinstance(img_list[0], dict):
                    first_img = str(
                        img_list[0].get("urlDefault")
                        or img_list[0].get("urlPre")
                        or img_list[0].get("url")
                        or ""
                    ).strip()
                if first_img.startswith("//"):
                    first_img = "https:" + first_img
                vid_urls = xiaohongshu_video_candidate_urls(xhs_note)
                headline = n_title or (n_desc.splitlines()[0][:48] if n_desc else "")
                if headline:
                    card = (
                        f"🔗 【小红书解析】{headline}\n"
                        + (f"👤 作者：{n_user}\n" if n_user else "")
                        + (f"📝 摘要：{n_desc}\n" if n_desc else "")
                        + (f"🎬 视频流：已识别 ({len(vid_urls)} 路)\n" if vid_urls else "")
                        + f"🌐 {final_social_url}"
                    )
                    return card, first_img

    if BeautifulSoup is None:
        return None, ""
    soup = BeautifulSoup(html_body, "html.parser")
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
        return None, ""

    platform_tag = (
        "小红书" if domain in ("xiaohongshu.com", "xhslink.com") else
        "抖音" if domain in ("douyin.com", "live.douyin.com", "webcast.douyin.com", "webcast.amemv.com") else
        "网页链接"
    )
    card = (
        f"🔗 【{platform_tag}解析】{title}\n"
        + (f"📝 摘要：{desc}\n" if desc else "")
        + f"🌐 {final_social_url}"
    )
    return card, img_url


async def _parse_youtube_or_social_url(url: str) -> tuple[str | None, str | None]:
    try:
        dy_room_id, dy_web_rid = douyin_live_target(url)
        if dy_room_id or dy_web_rid:
            return await parse_douyin_live_url(url)

        domain = _supported_parse_domain(url)
        async with _borrow_client("public", timeout=10.0) as client:
            if domain in ("youtube.com", "youtu.be"):
                r = await _get_public_url(
                    client,
                    "https://noembed.com/embed",
                    max_bytes=2 * 1024 * 1024,
                    params={"url": url},
                    timeout=10.0,
                    headers={"User-Agent": USER_AGENT},
                )
                if r.status_code == 200:
                    d = r.json()
                    title = d.get("title")
                    author = d.get("author_name")
                    thumb = d.get("thumbnail_url")
                    if title:
                        card = (
                            f"▶️ 【YouTube 视频解析】{title}\n"
                            f"👤 频道：{author or '未知'}\n"
                            f"🔗 {url}"
                        )
                        img_b64 = await _download_cover_preserving_metadata(thumb or "")
                        return card, img_b64

            # Xiaohongshu / Douyin / General meta extraction
            r = await _get_public_url(
                client,
                url,
                max_bytes=2 * 1024 * 1024,
                timeout=10.0,
                headers={"User-Agent": USER_AGENT},
            )
            final_social_url = str(r.url)
            redir_room_id, redir_web_rid = douyin_live_target(final_social_url)
            if redir_room_id or redir_web_rid:
                return await parse_douyin_live_url(final_social_url)

            if r.status_code != 200:
                return None, None

            card, img_url = await asyncio.to_thread(
                _parse_social_html_sync, r.text, final_social_url, domain
            )
            if not card:
                return None, None
            img_b64 = await _download_cover_preserving_metadata(img_url) if img_url else None
            return card, img_b64
    except Exception as e:
        logger.debug("Social URL parse error: %s", e)
        return None, None


class _WeChatArticleHTMLParser( html.parser.HTMLParser if hasattr(html, "parser") else object ):  # type: ignore[misc]
    pass


def _extract_clean_html_blocks(fragment: str) -> str:
    """Extracts structured plain text from HTML (such as WeChat #js_content),
    ignoring hidden elements, scripts, styles, and SVG noise (inspired by helper's articles.py).
    """
    from html.parser import HTMLParser

    hidden_tags = {"script", "style", "noscript", "iframe", "object", "svg", "math", "template", "head"}
    block_tags = {"p", "div", "section", "article", "blockquote", "ul", "ol", "table", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    class _Cleaner(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self.hidden_stack: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            t = tag.lower()
            attr_map = {k.lower(): (v or "") for k, v in attrs if k}
            style_str = attr_map.get("style", "").lower().replace(" ", "")
            is_hidden_style = (
                "display:none" in style_str
                or "visibility:hidden" in style_str
                or "opacity:0" in style_str
            )
            if t in hidden_tags or is_hidden_style or self.hidden_stack:
                self.hidden_stack.append(t)
                return
            if t in block_tags:
                self.parts.append("\n")
            elif t == "br":
                self.parts.append("\n")
            elif t == "li":
                self.parts.append("\n• ")

        def handle_endtag(self, tag: str) -> None:
            t = tag.lower()
            if self.hidden_stack:
                if t in self.hidden_stack:
                    idx = len(self.hidden_stack) - 1 - self.hidden_stack[::-1].index(t)
                    del self.hidden_stack[idx:]
                return
            if t in block_tags:
                self.parts.append("\n")

        def handle_data(self, data: str) -> None:
            if not self.hidden_stack and data:
                self.parts.append(data)

    cleaner = _Cleaner()
    try:
        cleaner.feed(fragment)
        cleaner.close()
    except Exception:
        pass
    raw_out = "".join(cleaner.parts).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in raw_out.split("\n")]
    non_empty = [ln for ln in lines if ln]
    return "\n".join(non_empty)


def parse_wechat_mp_html(html_body: str, url: str = "") -> dict[str, str] | None:
    """Extracts title, official account name, digest, body text, and cover image URL
    from a WeChat Official Account (`mp.weixin.qq.com`) HTML page.
    """
    raw = (html_body or "").strip()
    if not raw:
        return None

    title = ""
    author = ""
    digest = ""
    cover_url = ""
    body_text = ""

    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(raw, "html.parser")
            og_title = soup.find("meta", attrs={"property": "og:title"})
            og_desc = soup.find("meta", attrs={"property": "og:description"})
            og_img = soup.find("meta", attrs={"property": "og:image"})
            if og_title and og_title.get("content"):
                title = html.unescape(str(og_title.get("content") or "")).strip()
            if og_desc and og_desc.get("content"):
                digest = html.unescape(str(og_desc.get("content") or "")).strip()
            if og_img and og_img.get("content"):
                cover_url = html.unescape(str(og_img.get("content") or "")).strip()

            if not title:
                act_node = soup.find(id="activity-name")
                if act_node:
                    title = act_node.get_text(" ", strip=True)
            name_node = soup.find(id="js_name") or soup.find(class_="profile_nickname")
            if name_node:
                author = name_node.get_text(" ", strip=True)

            content_node = soup.find(id="js_content") or soup.find(class_="rich_media_content")
            if content_node:
                body_text = _extract_clean_html_blocks(str(content_node))
        except Exception as e:
            logger.debug("BeautifulSoup mp.weixin parse fallback: %s", e)

    if not title:
        m_t = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            raw,
            re.I,
        ) or re.search(r'var\s+msg_title\s*=\s*["\']([^"\']+)["\']', raw)
        if m_t:
            title = html.unescape(m_t.group(1)).strip()

    if not author:
        m_a = re.search(
            r'id=["\']js_name["\'][^>]*>(.*?)</a>',
            raw,
            re.I | re.S,
        ) or re.search(r'var\s+nickname\s*=\s*(?:htmlDecode\()?["\']([^"\']+)["\']', raw)
        if m_a:
            author = re.sub(r"<[^>]+>", "", html.unescape(m_a.group(1))).strip()

    if not digest:
        m_d = re.search(
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
            raw,
            re.I,
        )
        if m_d:
            digest = html.unescape(m_d.group(1)).strip()

    if not cover_url:
        m_c = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            raw,
            re.I,
        ) or re.search(r'var\s+msg_cdn_url\s*=\s*["\']([^"\']+)["\']', raw)
        if m_c:
            cover_url = html.unescape(m_c.group(1)).strip()

    if not body_text:
        m_js = re.search(
            r'<div[^>]+id=["\']js_content["\'][^>]*>(.*?)</div>\s*(?:<script|<div[^>]+id=["\']js_)',
            raw,
            re.I | re.S,
        )
        if m_js:
            body_text = _extract_clean_html_blocks(m_js.group(1))
        else:
            body_text = _extract_clean_html_blocks(raw)

    if cover_url.startswith("//"):
        cover_url = "https:" + cover_url

    title = re.sub(r"\s+", " ", title).strip()
    author = re.sub(r"\s+", " ", author).strip()
    digest = re.sub(r"\s+", " ", digest).strip()[:260]
    body_text = body_text.strip()

    if not title and not body_text:
        return None

    return {
        "title": title or "微信公众号文章",
        "author": author,
        "digest": digest,
        "content": body_text[:3600],
        "cover_url": cover_url,
        "url": url,
    }


async def _parse_wechat_mp_url(url: str) -> tuple[str | None, str | None]:
    """Fetches and parses a WeChat Official Account (`mp.weixin.qq.com`) article URL."""
    try:
        async with _borrow_client("public", timeout=12.0) as client:
            r = await _get_public_url(
                client,
                url,
                max_bytes=2 * 1024 * 1024,
                timeout=12.0,
                headers={"User-Agent": USER_AGENT, "Referer": "https://mp.weixin.qq.com/"},
            )
            if r.status_code != 200:
                return None, None
            info = await asyncio.to_thread(parse_wechat_mp_html, r.text, str(r.url))
            if not info:
                return None, None
            title = info.get("title") or "微信公众号文章"
            author = info.get("author") or ""
            digest = info.get("digest") or ""
            content = info.get("content") or ""
            cover_url = info.get("cover_url") or ""
            final_url = info.get("url") or url

            # If anti-bot/verification page returned no real title and no body, skip
            if title == "微信公众号文章" and len(content) < 20:
                return None, None

            card_lines = [f"📰 【微信公众号推文】{title}"]
            if author:
                card_lines.append(f"📌 公众号：{author}")
            if digest and digest not in content[:300]:
                card_lines.append(f"📝 摘要：{digest}")
            if content:
                card_lines.append(f"📖 正文内容：\n{content[:2800]}")
            card_lines.append(f"🔗 {final_url}")
            summary_text = "\n".join(card_lines)
        img_b64 = await _download_cover_preserving_metadata(cover_url) if cover_url else None
        return summary_text, img_b64
    except Exception as e:
        logger.debug("WeChat MP URL parse error: %s", e)
        return None, None


async def extract_and_parse_link(raw_text: str, *, _depth: int = 0) -> tuple[str | None, str | None]:
    """
    Detects and parses links from plain text OR WeChat <appmsg><type>5</type> XML cards.
    Returns (parsed_card_text, cover_image_b64).
    """
    s = (raw_text or "").strip()
    if not s or _depth > 1:
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

        if card_url and _depth < 1:
            parsed_card, parsed_img = await extract_and_parse_link(card_url, _depth=_depth + 1)
            if parsed_card:
                if src_name and "📌 公众号：" not in parsed_card and "📌 来源：" not in parsed_card:
                    parsed_card = parsed_card.replace("\n", f"\n📌 来源：{src_name}\n", 1)
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

    if domain == "mp.weixin.qq.com":
        return await _parse_wechat_mp_url(url)
    if domain in ("live.bilibili.com", "bilibili.com", "b23.tv"):
        return await _parse_bilibili_url(url)
    if domain in ("x.com", "twitter.com"):
        return await _parse_twitter_url(url)
    if domain in (
        "youtube.com",
        "youtu.be",
        "xiaohongshu.com",
        "xhslink.com",
        "webcast.amemv.com",
        "webcast.douyin.com",
        "live.douyin.com",
        "douyin.com",
    ):
        return await _parse_youtube_or_social_url(url)

    return None, None

# ==============================================================================
# 6. 文件解析（PDF / Word / Excel / PPT / EPUB / 文本代码）与 V7 超长文本保护
# ==============================================================================

MAX_PARSEABLE_FILE_BYTES = 35 * 1024 * 1024  # 35 MiB
MAX_PARSEABLE_FILE_XML_CHARS = 52 * 1024 * 1024  # 52 MiB base64 XML payload cap
_PLAIN_TEXT_DECODE_CAP_BYTES = 98304  # 96 KiB decode window for 12k char excerpt
BINARY_UNSUPPORTED_FILE_EXTENSIONS = frozenset({
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz",
    ".exe", ".dll", ".msi", ".dmg", ".pkg", ".apk", ".ipa", ".iso", ".bin", ".so", ".dylib",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".psd", ".heic",
    ".doc", ".xls", ".ppt",
})


def _is_likely_binary_bytes(raw_bytes: bytes) -> bool:
    """Checks whether raw bytes look like binary data (null bytes or high control-char ratio)."""
    if not raw_bytes:
        return False
    sample = raw_bytes[:4096]
    if b"\x00" in sample:
        return True
    ctrl_count = sum(1 for b in sample if b < 32 and b not in (9, 10, 13))
    return (ctrl_count / len(sample)) > 0.08


def _validate_zip_safety(
    zf: zipfile.ZipFile,
    max_entries: int = 2000,
    max_single_bytes: int = 20 * 1024 * 1024,
    max_total_bytes: int = 50 * 1024 * 1024,
) -> bool:
    """Validates Zip archive entry count and declared uncompressed sizes to guard against Zip Bombs."""
    infos = zf.infolist()
    if len(infos) > max_entries:
        return False
    total_size = 0
    for info in infos:
        if info.file_size < 0 or info.file_size > max_single_bytes:
            return False
        total_size += info.file_size
        if total_size > max_total_bytes:
            return False
    return True


def compact_long_user_text(
    text: str,
    *,
    threshold: int = 1500,
    head_chars: int = 3200,
    tail_chars: int = 600,
    doc_max_chars: int = 12000,
    hist_preview_chars: int = 220,
) -> tuple[str, str, str | None]:
    """Compacts V7 long incoming text messages (1,500 ~ 262,143 bytes):
    - Archives up to `doc_max_chars` into session document cache so follow-up questions work seamlessly.
    - Returns `(prompt_text, hist_text, archived_doc_text)` where `hist_text` is a compact summary
      to prevent multi-turn conversation history token explosion.
    """
    s = (text or "").strip()
    if len(s) <= threshold:
        return s, s, None

    archived_doc_text = s[:doc_max_chars]
    total_len = len(s)
    if total_len > (head_chars + tail_chars):
        omitted = total_len - head_chars - tail_chars
        excerpt = (
            f"{s[:head_chars]}\n\n"
            f"... [中间省略 {omitted} 字，完整内容已存入当前会话文档缓存] ...\n\n"
            f"{s[-tail_chars:]}"
        )
    else:
        excerpt = s

    prompt_text = (
        f"（对方刚刚发来了一段共 {total_len} 字的长文本，已自动归档至会话文档缓存供后续追问）：\n"
        f"{excerpt}"
    )
    preview = re.sub(r"\s+", " ", s[:hist_preview_chars]).strip()
    hist_text = f"{preview}... [长文本共 {total_len} 字，已存入文档缓存]"
    return prompt_text, hist_text, archived_doc_text


def is_incoming_wechat_file(raw_text: str) -> bool:
    s = (raw_text or "").strip()
    return (("<appmsg" in s or s.startswith("<?xml")) and "<type>6</type>" in s) or "<file_b64>" in s


def _extract_file_metadata_and_bytes(raw_text: str) -> tuple[str, str, bytes | None]:
    """Extracts filename, lowercase extension, and decoded bytes in a single pass with strict size caps."""
    s = raw_text or ""
    head = s[:8192]
    m_title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", head, re.S)
    m_fname = re.search(r"<file_name>(.*?)</file_name>", head, re.S)
    if m_title:
        filename = html.unescape((m_title.group(1) or m_title.group(2) or "未命名文件").strip())
    elif m_fname:
        filename = html.unescape((m_fname.group(1) or "未命名文件").strip())
    else:
        filename = "未命名文件"

    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if len(s) > MAX_PARSEABLE_FILE_XML_CHARS:
        return filename, ext, None
    if ext in BINARY_UNSUPPORTED_FILE_EXTENSIONS:
        return filename, ext, None

    m_size = re.search(r'file_size="(\d+)"', head) or re.search(r"<totallen>(\d+)</totallen>", head)
    if m_size:
        try:
            declared_size = int(m_size.group(1))
            if declared_size > MAX_PARSEABLE_FILE_BYTES:
                return filename, ext, None
        except Exception:
            pass

    m_b64 = re.search(r'file_b64="([^"]+)"', s) or re.search(r"<file_b64>([^<]+)</file_b64>", s)
    if not m_b64:
        return filename, ext, None

    b64_payload = m_b64.group(1).strip()
    if len(b64_payload) > int(MAX_PARSEABLE_FILE_BYTES * 1.4):
        return filename, ext, None

    try:
        raw_bytes = base64.b64decode(b64_payload)
    except Exception:
        return filename, ext, None

    if not raw_bytes or len(raw_bytes) > MAX_PARSEABLE_FILE_BYTES:
        return filename, ext, None
    return filename, ext, raw_bytes


def _decode_text_bounded(raw_bytes: bytes) -> str:
    """Decodes at most `_PLAIN_TEXT_DECODE_CAP_BYTES` bytes without cutting a trailing multibyte char."""
    sample = raw_bytes[:_PLAIN_TEXT_DECODE_CAP_BYTES]
    truncated = len(raw_bytes) > len(sample)
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            decoded = sample.decode(enc)
            if decoded.strip():
                return decoded.strip()[:12000]
        except UnicodeDecodeError as ude:
            if truncated and ude.end == len(sample) and ude.start >= len(sample) - 4:
                try:
                    decoded = sample[: ude.start].decode(enc)
                    if decoded.strip():
                        return decoded.strip()[:12000]
                except Exception:
                    continue
        except Exception:
            continue
    return ""


def _decode_and_parse_file_b64(raw_text: str) -> tuple[str, str, bytes | None]:
    """Single-pass XML file parser returning `(filename, parsed_text, raw_bytes_or_none_if_binary)`."""
    filename, ext, raw_bytes = _extract_file_metadata_and_bytes(raw_text)
    if raw_bytes is None:
        return filename, "", None

    try:
        if ext in (".docx", ".xlsx", ".xlsm", ".pptx", ".epub"):
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf_check:
                if not _validate_zip_safety(zf_check):
                    return filename, "[文件解析失败：压缩包解压后体积或条目数超出安全上限]", None
                if ext == ".epub":
                    parts: list[str] = []
                    names = [n for n in zf_check.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
                    for n in sorted(names)[:25]:
                        with zf_check.open(n) as entry_f:
                            raw_html = entry_f.read(20 * 1024 * 1024).decode("utf-8", errors="ignore")
                        if BeautifulSoup is not None:
                            soup = BeautifulSoup(raw_html, "html.parser")
                            t = soup.get_text("\n", strip=True)
                        else:
                            t = re.sub(r"<[^>]+>", "", raw_html).strip()
                        if len(t) > 20:
                            parts.append(t)
                    return filename, "\n\n".join(parts)[:12000], raw_bytes

        # 1. PDF
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw_bytes))
            pages = []
            for idx, page in enumerate(reader.pages[:30], 1):
                txt = (page.extract_text() or "").strip()
                if txt:
                    pages.append(f"[第{idx}页]\n{txt}")
            return filename, "\n\n".join(pages)[:12000], raw_bytes

        # 2. Word (.docx)
        if ext == ".docx":
            import docx
            doc = docx.Document(io.BytesIO(raw_bytes))
            parts = [p.text.strip() for p in itertools.islice(doc.paragraphs, 600) if p.text.strip()]
            for table in doc.tables[:10]:
                for row in table.rows[:50]:
                    row_txt = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                    if row_txt:
                        parts.append(row_txt)
            return filename, "\n".join(parts)[:12000], raw_bytes

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
                return filename, "\n".join(parts)[:12000], raw_bytes
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
            return filename, "\n\n".join(parts)[:12000], raw_bytes

        # 5. Plain text / code / csv / markdown / json (reject binary payloads)
        if _is_likely_binary_bytes(raw_bytes):
            return filename, "", None
        decoded_txt = _decode_text_bounded(raw_bytes)
        if decoded_txt:
            return filename, decoded_txt, raw_bytes
        return filename, "", None
    except Exception as e:
        logger.warning("Failed to parse file %s: %s", filename, e)

    return filename, "", None


def parse_wechat_file_xml(raw_text: str) -> tuple[str, str]:
    """
    Extracts filename and text content from WeChat <appmsg file_b64="..." ><type>6</type> XML
    or <file_b64>...<file_name>... tags.
    Enforces MAX_PARSEABLE_FILE_BYTES (35 MiB) and skips binary/archive formats safely.
    Returns (filename, extracted_text).
    """
    filename, extracted, _ = _decode_and_parse_file_b64(raw_text)
    return filename, extracted
_ORIG_PARSE_WECHAT_FILE_XML = parse_wechat_file_xml
DEEPSEEK_OCR_BOX_BLOCK_RE = re.compile(r"<[|｜](?:det|box)[|｜]>[\s\S]*?<[|｜]/(?:det|box)[|｜]>")
DEEPSEEK_OCR_TOKEN_RE = re.compile(r"<[|｜]/?[A-Za-z_]+[|｜]>")
DEEPSEEK_OCR_COORDS_RE = re.compile(r"\[\[\s*\d+(?:\s*,\s*\d+){3}\s*\]\]")
DEEPSEEK_OCR_BLANK_RE = re.compile(r"\n{3,}")
MAX_DEEPSEEK_OCR_BYTES = 100 * 1024 * 1024


def clean_deepseek_ocr_text(text: str) -> str:
    """Strips DeepSeek OCR layout coordinate blocks and `<｜ref｜>` tokens while preserving text (Diana #1557)."""
    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = DEEPSEEK_OCR_BOX_BLOCK_RE.sub("", cleaned)
    cleaned = DEEPSEEK_OCR_TOKEN_RE.sub("", cleaned)
    cleaned = DEEPSEEK_OCR_COORDS_RE.sub("", cleaned)
    lines = [ln.rstrip(" \t") for ln in cleaned.split("\n")]
    return DEEPSEEK_OCR_BLANK_RE.sub("\n\n", "\n".join(lines)).strip()


async def ocr_via_deepseek(
    data: bytes,
    filename: str,
    deepseek_api_key: str,
    deepseek_base_url: str = "https://api.deepseek.com",
) -> str:
    """Uploads an image or scanned document to DeepSeek `/v1/tools/ocr` (`purpose=text_recognition`) (Diana #1557)."""
    key = (deepseek_api_key or "").strip()
    if not data or not key or len(data) > MAX_DEEPSEEK_OCR_BYTES:
        return ""
    safe_name = os.path.basename((filename or "").strip()) or "document.pdf"
    endpoint = deepseek_tool_endpoint(deepseek_base_url, "ocr")
    try:
        async with _borrow_client("api", timeout=200.0) as client:
            r = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                },
                data={"purpose": "text_recognition"},
                files={"file": (safe_name, data, "application/octet-stream")},
                timeout=200.0,
            )
            if r.status_code != 200 or not r.content:
                return ""
            raw_body = r.content.decode("utf-8", errors="replace").lstrip()
            if not raw_body:
                return ""
            parsed = json.loads(raw_body)
            if not isinstance(parsed, dict) or parsed.get("error"):
                return ""
            pages = parsed.get("pages") or []
            if not isinstance(pages, list):
                return ""
            cleaned_pages: list[str] = []
            for page in pages:
                if not isinstance(page, dict):
                    continue
                page_txt = clean_deepseek_ocr_text(str(page.get("text") or ""))
                if page_txt:
                    cleaned_pages.append(page_txt)
            return "\n\n".join(cleaned_pages)
    except Exception as e:
        logger.debug("DeepSeek OCR failed for %s: %s", safe_name, e)
        return ""


async def parse_wechat_file_xml_async(
    raw_text: str,
    *,
    deepseek_api_key: str = "",
    deepseek_base_url: str = "",
) -> tuple[str, str]:
    """Async file parser wrapper that falls back to DeepSeek `/v1/tools/ocr` on empty/scanned PDFs (Diana #1557)."""
    if parse_wechat_file_xml is not _ORIG_PARSE_WECHAT_FILE_XML:
        filename, extracted = await asyncio.to_thread(parse_wechat_file_xml, raw_text)
        pdf_raw_bytes: bytes | None = None
    else:
        filename, extracted, pdf_raw_bytes = await asyncio.to_thread(_decode_and_parse_file_b64, raw_text)

    if not (filename or "").lower().endswith(".pdf"):
        return filename, extracted

    body_only = re.sub(r"\[第\d+页\]", "", extracted or "")
    body_non_ws = len(re.sub(r"\s+", "", body_only))
    if body_non_ws >= 20:
        return filename, extracted

    if not ((deepseek_api_key or "").strip() and is_deepseek_api_host(deepseek_base_url)):
        return filename, extracted

    if pdf_raw_bytes is None:
        _, _, pdf_raw_bytes = await asyncio.to_thread(_extract_file_metadata_and_bytes, raw_text)
    if not pdf_raw_bytes:
        return filename, extracted

    ocr_text = await ocr_via_deepseek(
        pdf_raw_bytes,
        filename,
        deepseek_api_key=deepseek_api_key,
        deepseek_base_url=deepseek_base_url or "https://api.deepseek.com",
    )
    if ocr_text.strip():
        return filename, ocr_text.strip()[:12000]
    return filename, extracted


# ==============================================================================
# 7. 多曲库点歌（网易云 / QQ音乐自动切换 + 卡片 & MP3 音频直发）
# ==============================================================================

def extract_cookie_field_value(raw_cookie: str, field_name: str) -> str:
    """Extracts a single cookie field value (such as NetEase `MUSIC_U` or Bilibili `SESSDATA`)
    whether the user pasted a bare token value, `MUSIC_U=xxx`, or a full browser `Cookie` string (Diana #1484).
    """
    raw = (raw_cookie or "").strip()
    target = (field_name or "").strip()
    if not raw or not target:
        return ""
    raw = re.sub(r"^cookie\s*:\s*", "", raw, flags=re.I).strip()
    if ";" in raw or "=" in raw:
        target_low = target.lower()
        for seg in raw.split(";"):
            item = seg.strip()
            if "=" not in item:
                continue
            k, v = item.split("=", 1)
            if k.strip().lower() == target_low:
                return v.strip().strip('"').strip("'")
        m = re.search(rf"(?:^|[;\s]){re.escape(target)}\s*=\s*([^;\s]+)", raw, re.I)
        if m:
            return m.group(1).strip().strip('"').strip("'")
        return ""
    return raw.strip('"').strip("'")


def normalize_qq_music_cookie(raw_cookie: str) -> tuple[str, str]:
    """Normalizes a QQ Music cookie string (Diana #1405):
    - Extracts numeric UIN from `uin`, `qqmusic_uin`, or `musicid` (defaulting to '0').
    - Maps QQMusicApi's `musicid` / `musickey` to official `uin` / `qqmusic_key` / `qm_keyst`
      when missing, so official `u.y.qq.com/cgi-bin/musicu.fcg` recognizes the login state.
    Returns (normalized_cookie_header, uin_str).
    """
    raw = (raw_cookie or "").strip()
    if not raw:
        return "", "0"
    kv: dict[str, str] = {}
    parts: list[str] = []
    for seg in raw.split(";"):
        item = seg.strip()
        if not item:
            continue
        parts.append(item)
        if "=" in item:
            k, v = item.split("=", 1)
            kv[k.strip()] = v.strip()

    raw_uin = kv.get("uin") or kv.get("qqmusic_uin") or kv.get("musicid") or ""
    uin_digits = re.sub(r"\D+", "", raw_uin.lstrip("oO"))
    uin = uin_digits.lstrip("0") or "0"

    music_key = kv.get("qqmusic_key") or kv.get("qm_keyst") or kv.get("musickey") or ""
    if uin != "0" and "uin" not in kv:
        parts.append(f"uin={uin}")
    if music_key:
        if "qqmusic_key" not in kv:
            parts.append(f"qqmusic_key={music_key}")
        if "qm_keyst" not in kv:
            parts.append(f"qm_keyst={music_key}")

    return "; ".join(parts), uin


async def search_and_fetch_music(
    query: str,
    qq_cookie: str = "",
    netease_music_u: str = "",
) -> dict[str, Any] | None:
    """
    Searches NetEase Cloud Music first, and automatically falls back to QQ Music / backup API
    if NetEase has no results or VIP audio lock.
    Returns dict: {title, artist, album, cover_url, page_url, audio_bytes, source}
    """
    q = (query or "").strip()
    if not q:
        return None

    fallback_candidate: dict[str, Any] | None = None

    # 1. Try NetEase Cloud Music (SSRF-validated + size-capped)
    try:
        ne_headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://music.163.com/",
        }
        clean_music_u = extract_cookie_field_value(
            netease_music_u or os.getenv("NETEASE_MUSIC_U", ""),
            "MUSIC_U",
        )
        if clean_music_u:
            ne_headers["Cookie"] = f"os=pc; appver=2.9.7; MUSIC_U={clean_music_u}"
        async with _borrow_client("public", timeout=12.0) as client:
            r = await _get_public_url(
                client,
                "https://music.163.com/api/cloudsearch/pc",
                max_bytes=2 * 1024 * 1024,
                method="POST",
                data={"s": q, "type": 1, "offset": 0, "limit": 5},
                headers=ne_headers,
                timeout=12.0,
            )
            songs = ((r.json() or {}).get("result") or {}).get("songs") or [] if r.status_code == 200 else []
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
                    ra = await _get_public_url(
                        client,
                        outer_mp3,
                        max_bytes=12 * 1024 * 1024,
                        headers=ne_headers,
                        timeout=10.0,
                    )
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

    # 2. Automatic switch to QQ Music search API (with musicid/musickey -> uin/qqmusic_key support)
    try:
        norm_cookie, qq_uin = normalize_qq_music_cookie(qq_cookie)
        qq_headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://y.qq.com/",
        }
        if norm_cookie:
            qq_headers["Cookie"] = norm_cookie
        async with _borrow_client("public", timeout=10.0) as client:
            qq_payload = {
                "comm": {"ct": "19", "cv": "1859", "uin": qq_uin},
                "req": {
                    "method": "DoSearchForQQMusicDesktop",
                    "module": "music.search.SearchCgiService",
                    "param": {"grp": 1, "num_per_page": 5, "page_num": 1, "query": q, "search_type": 0},
                },
            }
            rq = await _get_public_url(
                client,
                "https://u.y.qq.com/cgi-bin/musicu.fcg",
                max_bytes=2 * 1024 * 1024,
                method="POST",
                json=qq_payload,
                headers=qq_headers,
                timeout=10.0,
            )
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
    if not _WCF_BREAKER.allow_request():
        return False, False
    card_sent = False
    audio_sent = False
    title = song.get("title") or "未知歌曲"
    artist = song.get("artist") or "未知歌手"
    source = song.get("source") or "音乐"
    page_url = song.get("page_url") or ""
    cover_url = song.get("cover_url") or ""
    audio_bytes: bytes | None = song.get("audio_bytes")

    wcf_to = _wcf_timeout(20.0)
    async with _borrow_client("wcf", timeout=wcf_to) as client:
        if page_url and _WCF_BREAKER.allow_request():
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
                    timeout=wcf_to,
                )
                _WCF_BREAKER.record_success()
                if rc.status_code == 200 and rc.json().get("status") == 0:
                    card_sent = True
            except Exception as e:
                _WCF_BREAKER.record_failure(e)
                logger.debug("Send rich text card failed: %s", e)

        if audio_bytes and _WCF_BREAKER.allow_request():
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
                    timeout=wcf_to,
                )
                _WCF_BREAKER.record_success()
                if rf.status_code == 200 and rf.json().get("status") == 0:
                    audio_sent = True
            except Exception as e:
                _WCF_BREAKER.record_failure(e)
                logger.debug("Send MP3 file failed: %s", e)

            if not audio_sent and _WCF_BREAKER.allow_request():
                try:
                    rv = await client.post(
                        f"{WCF_GATEWAY_URL}/api/send_voice",
                        json={
                            "receiver": receiver,
                            "base64": b64_mp3,
                            "filename": safe_fn,
                        },
                        timeout=wcf_to,
                    )
                    _WCF_BREAKER.record_success()
                    if rv.status_code == 200 and rv.json().get("status") == 0:
                        audio_sent = True
                except Exception as e:
                    _WCF_BREAKER.record_failure(e)
                    logger.debug("Send voice fallback failed: %s", e)

    return card_sent, audio_sent
send_wcf_music_and_audio = send_music_to_wechat


def extract_forwarded_chat_record(
    raw_text: str,
    recent_bot_texts: Any = None,
) -> tuple[str, str, bool]:
    """
    Parses WeChat merged forwarded chat history (<type>19</type> / <recorditem>).
    Returns (title, des_summary, is_about_bot).
    Automatically detects if the forwarded chat record contains Dafeiyu's own messages
    (via recent_bot_texts or trusted dynamic bot account name) without mutating global bot names.
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
VOICE_REQUEST_RE = re.compile(
    r"(?:发(?:条|个|段)?语音|用语音(?:说|回|发|讲)|语音(?:说|回|骂|哄|唱)|说(?:句|两句)话听听|听听你(?:的)?声音|想听你(?:说话|声音)|来(?:条|个|段)语音)",
    re.I,
)


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
MAX_DEEPSEEK_ASR_BYTES = 8 * 1024 * 1024


def wrap_pcm24k_to_wav(pcm_bytes: bytes, sample_rate: int = 24000) -> bytes:
    """Wraps 16-bit mono PCM audio bytes with a 44-byte RIFF WAVE header (Diana #1555)."""
    raw = pcm_bytes or b""
    sr = max(8000, int(sample_rate or 24000))
    data_size = len(raw)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sr,
        sr * 2,
        2,
        16,
        b"data",
        data_size,
    )
    return header + raw


async def synthesize_tts_via_deepseek(
    text: str,
    deepseek_api_key: str,
    deepseek_base_url: str = "https://api.deepseek.com",
    voice_id: str = "stella",
) -> bytes | None:
    """Synthesizes speech via DeepSeek `/v1/tools/tts` (stella/tide, no `model` field), returning WAV bytes (Diana #1555)."""
    key = (deepseek_api_key or "").strip()
    cleaned_text = _clean_text_for_tts(text)
    if not key or not cleaned_text:
        return None
    vid = (voice_id or "").strip() or "stella"
    endpoint = deepseek_tool_endpoint(deepseek_base_url, "tts")
    try:
        async with _borrow_client("api", timeout=25.0) as client:
            r = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Accept": "audio/*, application/octet-stream",
                },
                json={
                    "text": cleaned_text,
                    "voice_id": vid,
                    "format": "wav",
                },
                timeout=25.0,
            )
            if r.status_code != 200 or not r.content:
                if "content exists risk" in (r.text if r.status_code != 200 else "").lower():
                    logger.debug("DeepSeek TTS rejected by content policy")
                return None
            ct = (r.headers.get("content-type") or "").lower()
            lead = r.content.lstrip()
            if (
                "application/json" in ct
                or "text/html" in ct
                or (lead.startswith(b"{") and b'"error"' in lead[:512])
            ):
                return None
            if r.content.startswith(b"RIFF"):
                return r.content
            return wrap_pcm24k_to_wav(r.content, 24000)
    except Exception as e:
        logger.debug("DeepSeek TTS synthesis failed: %s", e)
        return None


def _resolve_ffmpeg_executable() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _convert_audio_bytes_to_wav16k(raw_audio: bytes) -> bytes | None:
    """Converts arbitrary input audio bytes into 16kHz mono 16-bit PCM WAV (<= 120s) via ffmpeg."""
    ffmpeg_bin = _resolve_ffmpeg_executable()
    if not ffmpeg_bin or not raw_audio:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="whale_asr_") as tmpdir:
            in_path = os.path.join(tmpdir, "input.audio")
            out_path = os.path.join(tmpdir, "voice.wav")
            with open(in_path, "wb") as f_in:
                f_in.write(raw_audio)
            proc = subprocess.run(
                [
                    ffmpeg_bin,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    in_path,
                    "-t",
                    "120",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-acodec",
                    "pcm_s16le",
                    "-f",
                    "wav",
                    out_path,
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if proc.returncode != 0 or not os.path.exists(out_path):
                return None
            with open(out_path, "rb") as f_out:
                wav_bytes = f_out.read()
            return wav_bytes if wav_bytes else None
    except Exception as e:
        logger.debug("ffmpeg WAV conversion for DeepSeek ASR failed: %s", e)
        return None


async def transcribe_audio_via_deepseek(
    audio_b64: str,
    deepseek_api_key: str,
    deepseek_base_url: str = "https://api.deepseek.com",
) -> str | None:
    """Transcribes audio (<= 8 MiB, <= 120s) via DeepSeek `/v1/tools/asr` (Diana #1555)."""
    key = (deepseek_api_key or "").strip()
    b64_clean = (audio_b64 or "").strip()
    if not key or not b64_clean:
        return None
    if "," in b64_clean and b64_clean.startswith("data:"):
        b64_clean = b64_clean.split(",", 1)[1].strip()
    if len(b64_clean) > 12 * 1024 * 1024:
        return None
    try:
        raw_audio = base64.b64decode(b64_clean)
    except Exception:
        return None
    if not raw_audio or len(raw_audio) > MAX_DEEPSEEK_ASR_BYTES:
        return None

    if raw_audio.startswith(b"RIFF"):
        upload_bytes = raw_audio
        upload_name = "voice.wav"
        upload_mime = "audio/wav"
    elif raw_audio.startswith(b"OggS"):
        upload_bytes = raw_audio
        upload_name = "voice.ogg"
        upload_mime = "audio/ogg"
    else:
        converted = await asyncio.to_thread(_convert_audio_bytes_to_wav16k, raw_audio)
        if not converted or len(converted) > MAX_DEEPSEEK_ASR_BYTES:
            return None
        upload_bytes = converted
        upload_name = "voice.wav"
        upload_mime = "audio/wav"

    endpoint = deepseek_tool_endpoint(deepseek_base_url, "asr")
    try:
        async with _borrow_client("api", timeout=45.0) as client:
            r = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                },
                files={"file": (upload_name, upload_bytes, upload_mime)},
                timeout=45.0,
            )
            if r.status_code != 200:
                return None
            parsed = r.json()
            if not isinstance(parsed, dict):
                return None
            text = str(parsed.get("text") or "").strip()
            return text or None
    except Exception as e:
        logger.debug("DeepSeek ASR transcription failed: %s", e)
        return None


async def synthesize_tts_mp3_b64(
    text: str,
    sf_api_key: str = "",
    *,
    deepseek_api_key: str = "",
    deepseek_base_url: str = "",
    voice_id: str = "stella",
) -> str | None:
    """
    Synthesizes Dafeiyu's spoken reply into audio base64:
    1. SiliconFlow CosyVoice2 first when `sf_api_key` (or env) is configured;
    2. DeepSeek `/v1/tools/tts` when `deepseek_api_key` is provided and `is_deepseek_api_host(deepseek_base_url)`;
    3. Microsoft Edge Neural TTS (`zh-CN-XiaoyiNeural`) fallback.
    """
    spoken = _clean_text_for_tts(text)
    if not spoken or len(spoken) < 2 or len(spoken) > 140:
        return None

    # 1. Primary when configured: SiliconFlow CosyVoice2 TTS API
    resolved_sf_key = _resolve_sf_api_key(sf_api_key)
    if resolved_sf_key:
        try:
            async with _borrow_client("api", timeout=20.0) as client:
                r = await client.post(
                    "https://api.siliconflow.cn/v1/audio/speech",
                    headers={"Authorization": f"Bearer {resolved_sf_key}", "Content-Type": "application/json"},
                    json={
                        "model": "FunAudioLLM/CosyVoice2-0.5B",
                        "input": spoken,
                        "voice": "FunAudioLLM/CosyVoice2-0.5B:claire",
                        "response_format": "mp3",
                    },
                    timeout=20.0,
                )
                ct = (r.headers.get("content-type") or "").lower()
                if (
                    r.status_code == 200
                    and len(r.content) >= 4
                    and "application/json" not in ct
                    and not r.content.lstrip().startswith(b"{")
                ):
                    return base64.b64encode(r.content).decode("ascii")
        except Exception as e:
            logger.debug("SiliconFlow TTS failed, falling back: %s", e)

    # 2. DeepSeek /v1/tools/tts when DeepSeek key + DeepSeek API host are configured
    if (deepseek_api_key or "").strip() and is_deepseek_api_host(deepseek_base_url):
        wav_bytes = await synthesize_tts_via_deepseek(
            spoken,
            deepseek_api_key=deepseek_api_key,
            deepseek_base_url=deepseek_base_url or "https://api.deepseek.com",
            voice_id=voice_id,
        )
        if wav_bytes:
            return base64.b64encode(wav_bytes).decode("ascii")

    # 3. Fallback: Microsoft Edge Neural TTS (zh-CN-XiaoyiNeural - lively young female voice)
    try:
        import edge_tts

        async def _collect_edge_tts() -> bytes:
            comm = edge_tts.Communicate(spoken, voice="zh-CN-XiaoyiNeural", rate="+5%", pitch="+3Hz")
            chunks: list[bytes] = []
            async for chunk in comm.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    chunks.append(chunk["data"])
            return b"".join(chunks)

        audio_bytes = await asyncio.wait_for(_collect_edge_tts(), timeout=12.0)
        if len(audio_bytes) > 512:
            return base64.b64encode(audio_bytes).decode("ascii")
    except Exception as e:
        logger.debug("edge-tts synthesis fallback failed: %s", e)

    return None


async def synthesize_and_send_wcf_voice(
    receiver: str,
    text: str,
    sf_api_key: str = "",
    *,
    deepseek_api_key: str = "",
    deepseek_base_url: str = "",
    voice_id: str = "stella",
) -> bool:
    """Synthesizes TTS audio for `text` and sends it as a native WeChat 4.1 SILK v3 voice bubble."""
    audio_b64 = await synthesize_tts_mp3_b64(
        text,
        sf_api_key=sf_api_key,
        deepseek_api_key=deepseek_api_key,
        deepseek_base_url=deepseek_base_url,
        voice_id=voice_id,
    )
    if not audio_b64:
        return False
    is_wav = False
    try:
        head_raw = base64.b64decode(audio_b64[:8] + "====")
        is_wav = head_raw.startswith(b"RIFF")
    except Exception:
        is_wav = False
    fname = "dafeiyu_voice.wav" if is_wav else "dafeiyu_voice.mp3"
    return await send_wcf_voice(receiver, audio_b64, filename=fname)


# ==============================================================================
# from whale_listener.py
# ==============================================================================
SILICONFLOW_ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
SILICONFLOW_ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"
SENSEVOICE_TAGS: dict[str, str] = {
    "HAPPY": "开心/轻快",
    "SAD": "难过/低落",
    "ANGRY": "生气/激动",
    "NEUTRAL": "",
    "FEARFUL": "紧张/害怕",
    "DISGUSTED": "嫌弃",
    "SURPRISED": "惊讶",
    "LAUGHTER": "带着笑声",
    "GIGGLE": "偷笑",
    "SNICKER": "憋笑",
    "CHUCKLE": "轻笑",
    "CRY": "带着哭腔",
    "SIGH": "叹气/疲惫",
    "COUGH": "咳嗽了一声",
    "THROAT_CLEARING": "清了清嗓子",
    "YAWN": "打了个哈欠/犯困",
    "SNEEZE": "打了个喷嚏",
}
IMAGE_FOLLOWUP_KEYWORDS = (
    "图", "看", "照片", "截图", "上面", "这张", "那个", "啥",
    "里面", "左边", "右边", "写的啥", "写的什么", "报错", "内容",
    "锐评", "评价", "点评", "分析", "怎么看", "什么意思", "啥意思",
)


class MediaMixin:
    """``WhaleListener`` methods for media parsers (mixed into the listener class)."""

    @staticmethod
    def _sticker_bytes_to_jpeg_b64(raw_bytes: bytes) -> str | None:
        if not raw_bytes or Image is None:
            return None
        try:
            img = Image.open(io.BytesIO(raw_bytes))
            n_frames = getattr(img, "n_frames", 1)
            if n_frames > 1:
                indices = sorted(set([0, n_frames // 2, n_frames - 1]))
                frames = []
                for idx in indices:
                    img.seek(idx)
                    f_rgb = img.convert("RGB")
                    f_rgb.thumbnail((220, 220))
                    frames.append(f_rgb)
                total_w = sum(f.width for f in frames)
                max_h = max(f.height for f in frames)
                strip = Image.new("RGB", (total_w, max_h), (255, 255, 255))
                x_off = 0
                for f in frames:
                    strip.paste(f, (x_off, 0))
                    x_off += f.width
                out_img = strip
            else:
                out_img = img.convert("RGB")
                out_img.thumbnail((360, 360))
            buf = io.BytesIO()
            out_img.save(buf, format="JPEG", quality=82)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as e:
            logger.debug("Sticker decode failed: %s", e)
            return None

    async def _fetch_sticker_strip_b64(self, xml_text: str) -> str | None:
        m = re.search(r'cdnurl="([^"]+)"', xml_text or "")
        if not m:
            return None
        cdn_url = html.unescape(m.group(1)).strip()
        if not cdn_url.startswith("http"):
            return None
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await df._get_public_url(client, cdn_url)
                if r.status_code == 200 and r.content:
                    return self._sticker_bytes_to_jpeg_b64(r.content)
        except Exception as e:
            logger.debug("Fetch sticker cdnurl failed: %s", e)
        return None

    async def _extract_image_b64_from_chain(self, message_chain: Any, raw_text: str = "") -> str | None:
        if raw_text and "<img_b64>" in raw_text:
            m_ib64 = re.search(r"<img_b64>(.*?)</img_b64>", raw_text, re.S)
            if m_ib64:
                b64_val = m_ib64.group(1).strip()
                if "," in b64_val and b64_val.startswith("data:"):
                    b64_val = b64_val.split(",", 1)[1]
                try:
                    raw_bytes = base64.b64decode(b64_val)
                    return self._sticker_bytes_to_jpeg_b64(raw_bytes) or b64_val
                except Exception:
                    pass
        if not message_chain:
            return None
        try:
            for comp in message_chain:
                if isinstance(comp, platform_message.Image):
                    b64_val = str(getattr(comp, "base64", "") or "").strip()
                    if b64_val:
                        if "," in b64_val and b64_val.startswith("data:"):
                            b64_val = b64_val.split(",", 1)[1]
                        raw_bytes = base64.b64decode(b64_val)
                        return self._sticker_bytes_to_jpeg_b64(raw_bytes) or b64_val
                    url_val = str(getattr(comp, "url", "") or "").strip()
                    if url_val.startswith("http"):
                        async with httpx.AsyncClient(timeout=12.0) as client:
                            r = await df._get_public_url(client, url_val)
                            if r.status_code == 200 and r.content:
                                return self._sticker_bytes_to_jpeg_b64(r.content)
        except Exception as e:
            logger.debug("Extract image from chain skipped: %s", e)
        return None

    async def _transcribe_incoming_voice(self, xml_text: str) -> tuple[str, str, float]:
        dur_sec = 3.0
        m_len = re.search(r'voicelength="(\d+)"', xml_text or "")
        if m_len:
            try:
                dur_sec = round(max(1.0, int(m_len.group(1)) / 1000.0), 1)
            except Exception:
                dur_sec = 3.0

        m_b64 = re.search(r'audio_b64="([^"]+)"', xml_text or "")
        if not m_b64:
            return "", "", dur_sec
        audio_b64 = m_b64.group(1).strip()
        if not audio_b64:
            return "", "", dur_sec

        raw_trans = ""
        mood_tags: list[str] = []
        clean_trans = ""

        sf_key = self._get_config_str("siliconflow_api_key", DEFAULT_SF_API_KEY)
        if sf_key:
            try:
                mp3_bytes = base64.b64decode(audio_b64)
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.post(
                        SILICONFLOW_ASR_URL,
                        headers={"Authorization": f"Bearer {sf_key}"},
                        files={"file": ("voice.mp3", mp3_bytes, "audio/mpeg")},
                        data={"model": SILICONFLOW_ASR_MODEL},
                    )
                    if resp.status_code != 200:
                        logger.warning("SiliconFlow ASR failed: %s %s", resp.status_code, resp.text)
                    else:
                        raw_trans = str((resp.json().get("text") or "")).strip()
            except Exception as e:
                logger.warning("Transcribe voice exception: %s", e)

            if raw_trans:
                for tag_m in re.finditer(r"<\|([A-Z_]+)\|>", raw_trans):
                    t_code = tag_m.group(1).strip()
                    mapped = SENSEVOICE_TAGS.get(t_code, "")
                    if mapped and mapped not in mood_tags:
                        mood_tags.append(mapped)
                clean_trans = re.sub(r"<\|[^|>]+\|>", "", raw_trans).strip()

        if not clean_trans:
            ds_transcribe = getattr(df, "transcribe_audio_via_deepseek", None)
            api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
            if callable(ds_transcribe) and api_key:
                base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
                try:
                    try:
                        ds_res = await ds_transcribe(audio_b64, api_key, base_url)
                    except TypeError:
                        ds_res = await ds_transcribe(
                            audio_b64, deepseek_api_key=api_key, deepseek_base_url=base_url
                        )
                except Exception as e:
                    logger.warning("DeepSeek audio transcription fallback failed: %s", e)
                    ds_res = ""
                if isinstance(ds_res, tuple):
                    ds_text = str(ds_res[0] or "").strip() if len(ds_res) >= 1 else ""
                    ds_tone = str(ds_res[1] or "").strip() if len(ds_res) >= 2 else ""
                    if ds_tone and ds_tone not in mood_tags:
                        mood_tags.append(ds_tone)
                else:
                    ds_text = str(ds_res or "").strip()
                if ds_text:
                    for tag_m in re.finditer(r"<\|([A-Z_]+)\|>", ds_text):
                        mapped = SENSEVOICE_TAGS.get(tag_m.group(1).strip(), "")
                        if mapped and mapped not in mood_tags:
                            mood_tags.append(mapped)
                    clean_trans = re.sub(r"<\|[^|>]+\|>", "", ds_text).strip()

        return clean_trans, "、".join(mood_tags), dur_sec

    async def _call_search_web_parallel(
        self,
        queries: list[str],
        *,
        api_key: str,
        base_url: str,
    ) -> str:
        search_key = self._get_config_str("search_api_key", os.getenv("TAVILY_API_KEY", ""))
        try:
            return await df.search_web_parallel(
                queries,
                search_key,
                deepseek_api_key=api_key,
                deepseek_base_url=base_url,
            )
        except TypeError:
            try:
                return await df.search_web_parallel(queries, search_key)
            except TypeError:
                return await df.search_web_parallel(queries)

    async def _call_read_webpage_content(
        self,
        url: str,
        *,
        api_key: str,
        base_url: str,
    ) -> str:
        try:
            return await df.read_webpage_content(
                url,
                deepseek_api_key=api_key,
                deepseek_base_url=base_url,
            )
        except TypeError:
            return await df.read_webpage_content(url)

    async def _call_parse_wechat_file_xml(
        self,
        raw_text: str,
        *,
        api_key: str,
        base_url: str,
    ) -> tuple[str, str]:
        parse_async = getattr(df, "parse_wechat_file_xml_async", None)
        sync_mocked = hasattr(df.parse_wechat_file_xml, "assert_called") and not hasattr(
            parse_async, "assert_called"
        )
        if callable(parse_async) and not sync_mocked:
            try:
                return await parse_async(
                    raw_text,
                    deepseek_api_key=api_key,
                    deepseek_base_url=base_url,
                )
            except TypeError:
                return await parse_async(raw_text)
        return await asyncio.to_thread(df.parse_wechat_file_xml, raw_text)

    @staticmethod
    def _extract_plain_text(message_chain: Any) -> str:
        if not message_chain:
            return ""
        parts: list[str] = []
        has_image = False
        try:
            for comp in message_chain:
                if isinstance(comp, platform_message.Plain):
                    t = str(getattr(comp, "text", "") or "")
                    if t.strip():
                        parts.append(t.strip())
                elif isinstance(comp, platform_message.Image):
                    has_image = True
        except Exception:
            pass
        if not parts:
            if has_image:
                return "[图片]"
            raw = str(message_chain or "").strip()
            return raw
        return "\n".join(parts).strip()

    @staticmethod
    def _is_incoming_sticker(text: str, raw_source_obj: Any = None) -> bool:
        if isinstance(raw_source_obj, dict):
            wx_t = raw_source_obj.get("wx_type") or raw_source_obj.get("type") or raw_source_obj.get("msg_type")
            if wx_t == 47 or str(wx_t) == "47":
                return True
        s = (text or "").strip()
        return s == "[动画表情]" or "<emoji" in s or s.startswith("<msg><emoji")

    @staticmethod
    def _compress_sticker_description(desc: str) -> str:
        """Compresses animated sticker / meme visual description to a single-line <=120-char summary (Diana #1544)."""
        return re.sub(r"\s+", " ", (desc or "")).strip()[:120]

    @classmethod
    def _format_visual_history_entry(cls, base_text: str, desc: str, *, is_sticker: bool) -> str:
        clean_desc = (desc or "").strip()
        if not clean_desc:
            return base_text
        if is_sticker:
            short = cls._compress_sticker_description(clean_desc)
            return f"[发来了一张微信表情包：{short}]" if short else base_text
        return f"{base_text}\n[图片视觉内容：{clean_desc}]" if base_text else f"[图片视觉内容：{clean_desc}]"

    @staticmethod
    def _lookup_cached_vision_desc(image_b64: str | None) -> str:
        b64_clean = (image_b64 or "").strip()
        if not b64_clean:
            return ""
        if "," in b64_clean and b64_clean.startswith("data:"):
            b64_clean = b64_clean.split(",", 1)[1].strip()
        cache_map = getattr(df, "_VISION_DESC_CACHE", None)
        if not isinstance(cache_map, dict):
            return ""
        import hashlib as _hl

        cache_key = _hl.sha256(b64_clean.encode("utf-8", errors="ignore")).hexdigest()
        hit = cache_map.get(cache_key)
        if isinstance(hit, tuple) and len(hit) >= 2 and (time.time() - float(hit[0])) < 600.0:
            return str(hit[1] or "").strip()
        return ""

    @staticmethod
    def _is_incoming_voice(text: str) -> bool:
        s = (text or "").strip()
        return s in ("[语音]", "[语音消息]") or "<voicemsg " in s or s.startswith("<msg><voicemsg")

    def _store_user_doc(self, doc_key: Any, name: str, text: str, *, ts: Optional[float] = None) -> dict[str, Any]:
        """Caches an uploaded file/link/long-text snippet (up to 3 recent docs per key) with LRU eviction."""
        now = float(ts if ts is not None else time.time())
        clean_name = str(name or "文档").strip() or "文档"
        clean_text = str(text or "")
        existing = self._user_docs.get(doc_key)
        recent_docs: list[dict[str, Any]] = []
        if isinstance(existing, dict):
            prev_list = existing.get("recent_docs")
            if isinstance(prev_list, list):
                for item in prev_list:
                    if not isinstance(item, dict):
                        continue
                    item_ts = float(item.get("ts", existing.get("ts", 0)) or 0.0)
                    if now - item_ts >= 1800:
                        continue
                    if str(item.get("name", "")) == clean_name:
                        continue
                    recent_docs.append(dict(item))
            elif existing.get("text"):
                prev_ts = float(existing.get("ts", 0) or 0.0)
                if now - prev_ts < 1800 and str(existing.get("name", "")) != clean_name:
                    recent_docs.append({
                        "name": str(existing.get("name", "文档")),
                        "text": str(existing.get("text", "")),
                        "ts": prev_ts,
                    })
        recent_docs.append({"name": clean_name, "text": clean_text, "ts": now})
        recent_docs = recent_docs[-3:]
        entry = {
            "name": clean_name,
            "text": clean_text,
            "ts": now,
            "recent_docs": recent_docs,
        }
        self._lru_set(self._user_docs, doc_key, entry)
        return entry

    def _select_relevant_doc_snippet(self, doc_key: Any, query_text: str) -> Optional[dict[str, Any]]:
        """Returns the non-expired cached doc best matching query_text (falling back to most recent)."""
        entry = self._user_docs.get(doc_key)
        if not isinstance(entry, dict):
            return None
        now = time.time()
        top_ts = float(entry.get("ts", 0) or 0.0)
        if now - top_ts >= 1800:
            self._user_docs.pop(doc_key, None)
            return None
        self._lru_touch(self._user_docs, doc_key)
        raw_docs = entry.get("recent_docs")
        candidates: list[dict[str, Any]] = []
        if isinstance(raw_docs, list) and raw_docs:
            for d in raw_docs:
                if not isinstance(d, dict):
                    continue
                d_ts = float(d.get("ts", top_ts) or 0.0)
                if now - d_ts < 1800:
                    candidates.append(d)
        elif entry.get("text"):
            candidates.append(entry)
        if not candidates:
            self._user_docs.pop(doc_key, None)
            return None
        if len(candidates) == 1 or not str(query_text or "").strip():
            return candidates[-1]
        best_doc = candidates[-1]
        best_score = -1.0
        for cand in candidates:
            blob = f"{cand.get('name', '')} {str(cand.get('text', ''))[:400]}"
            score = float(df.compute_memory_query_overlap(query_text, blob))
            if score >= best_score:
                best_score = score
                best_doc = cand
        return best_doc if best_score > 0 else candidates[-1]
