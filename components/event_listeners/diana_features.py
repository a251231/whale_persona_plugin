"""Compatibility facade for the Diana feature suite (v0.5.2 A-12).

The implementation lives in ``components/whale_core/`` (``http_gateway``, ``media_parsers``,
``prompts``, ``mood_memory``, ``outbound``). This module re-exports every historical name so
``from components.event_listeners import diana_features as df`` keeps working, and it is a
live facade: ``setattr``/``patch.object(df, name, ...)`` is propagated to every whale_core module
that holds the same object, so patched helpers are seen by the extracted code as before.
"""
# ruff: noqa: F401
# Module imports are part of the historical surface (tests patch e.g. df.socket, df.random, df.httpx).
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import html
import io
import itertools
from itertools import islice
import ipaddress
import json
import logging
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import weakref
import zipfile
from typing import Any
from zoneinfo import ZoneInfo

import httpx

try:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 20_000_000
except Exception:
    Image = None  # type: ignore

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None  # type: ignore
import sys
from pathlib import Path

try:
    from .. import whale_core as _core
except ImportError:  # loaded as a top-level module (no ``components`` parent package)
    _components_dir = str(Path(__file__).resolve().parents[1])
    if _components_dir not in sys.path:
        sys.path.insert(0, _components_dir)
    import whale_core as _core  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# Historical public surface, grouped by the whale_core module that now owns each name.
_EXPORTS: dict[str, tuple[str, ...]] = {
    "http_gateway": (
        "TZ",
        "WCF_GATEWAY_URL",
        "_ORIG_HTTPX_ASYNC_CLIENT",
        "USER_AGENT",
        "DOUYIN_MOBILE_USER_AGENT",
        "_CLIENT_POOLS",
        "_SECRETS_FILE_CACHE",
        "_SECRETS_FILE_TTL_SEC",
        "_is_httpx_mocked",
        "_wcf_timeout",
        "WcfCircuitBreaker",
        "_WCF_BREAKER",
        "reset_wcf_circuit_breaker",
        "_read_secrets_json_cached",
        "_lru_ttl_get",
        "_lru_ttl_set",
        "_sanitize_public_error",
        "_SafePublicNetworkBackend",
        "_create_pooled_client",
        "_get_shared_client",
        "_borrow_client",
        "close_shared_clients",
        "is_deepseek_api_host",
        "deepseek_tool_endpoint",
        "_NAT64_WELL_KNOWN",
        "_NAT64_LOCAL_USE",
        "_IPV4_COMPAT_NET",
        "_is_public_ip",
        "_resolve_validated_public_ips",
        "_is_safe_public_url",
        "_ORIG_IS_SAFE_PUBLIC_URL",
        "_get_public_url",
        "_DYNAMIC_BOT_CACHE",
        "_get_bot_wxids_and_names",
        "BOT_WXIDS_AND_NAMES",
        "refresh_dynamic_bot_names",
        "get_dynamic_bot_names",
        "_GROUP_MEMBERS_CACHE",
        "_USER_NAME_CACHE",
        "GROUP_MEMBERS_CACHE_MAX",
        "GROUP_MEMBERS_CACHE_TTL_SEC",
        "USER_NAME_CACHE_MAX",
        "USER_NAME_CACHE_TTL_SEC",
        "PLACEHOLDER_DISPLAY_NAMES",
        "is_placeholder_display_name",
        "_is_usable_display_name",
        "strip_redundant_speaker_name",
        "get_cached_display_name",
        "get_cached_group_member_names",
        "resolve_user_display_name",
        "send_wcf_text_or_image",
        "send_wcf_voice",
        "publish_wcf_moment",
        "fetch_wcf_moments",
        "interact_wcf_moment",
        "fetch_wcf_friend_requests",
        "accept_wcf_friend_request",
        "set_wcf_friend_auto_accept",
    ),
    "mood_memory": (
        "SERIOUS_MOMENT_KEYWORDS",
        "is_serious_or_sad_moment",
        "MEMORY_STOP_CHARS",
        "MEMORY_STOP_BIGRAMS",
        "strip_memory_query_noise",
        "compute_memory_query_overlap",
        "DAILY_WAKE_POOL",
        "DAILY_BREAKFAST_POOL",
        "DAILY_LUNCH_POOL",
        "DAILY_DINNER_POOL",
        "DAILY_ACTIVITY_POOL",
        "DAILY_SMALL_EVENT_POOL",
        "get_or_create_daily_schedule",
        "format_daily_schedule_line",
        "DIRECT_BOT_TAUNT_KEYWORDS",
        "GENERIC_TAUNT_KEYWORDS",
        "TAUNT_KEYWORDS",
        "COMPLAINT_SUBJECT_KEYWORDS",
        "_BOT_TAUNT_TARGETS",
        "_has_directed_taunt",
        "COAX_KEYWORDS",
        "check_and_update_temper_state",
    ),
    "media_parsers": (
        "SILICONFLOW_CHAT_URL",
        "SILICONFLOW_VL_MODELS",
        "_VISION_DESC_CACHE",
        "VISION_DESC_CACHE_MAX",
        "VISION_DESC_CACHE_TTL_SEC",
        "_resolve_sf_api_key",
        "describe_image_via_siliconflow",
        "ALL_MEME_CATEGORIES",
        "STOLEN_MEMES_FILE",
        "STOLEN_MEMES_BACKUP_FILE",
        "_STOLEN_MEMES_LOCK",
        "_STOLEN_GIF_MAX_BYTES",
        "load_stolen_memes",
        "save_stolen_memes",
        "_encode_stolen_sticker_sync",
        "steal_incoming_sticker",
        "pick_stolen_or_official_meme",
        "truncate_sticker_history_gist",
        "DEEPSEEK_SEARCH_TOOLS",
        "_parse_bing_search_html_sync",
        "_parse_sogou_search_html_sync",
        "_search_single_query",
        "search_via_deepseek",
        "search_web_parallel",
        "_DEEPSEEK_FETCH_CACHE",
        "DEEPSEEK_FETCH_CACHE_MAX",
        "DEEPSEEK_FETCH_CACHE_TTL_SEC",
        "fetch_webpage_via_deepseek",
        "_extract_webpage_text_sync",
        "read_webpage_content",
        "URL_REGEX",
        "SUPPORTED_PARSE_DOMAINS",
        "BILIBILI_LIVE_PATH_RE",
        "DOUYIN_LIVE_WEB_PATH_RE",
        "DOUYIN_LIVE_REFLOW_PATH_RE",
        "DOUYIN_SITE_LIVE_PATH_RE",
        "COVER_DOWNLOAD_TIMEOUT_SEC",
        "_host_matches_domain",
        "_supported_parse_domain",
        "bilibili_live_room_id",
        "douyin_live_target",
        "has_supported_media_link_or_card",
        "_encode_image_to_jpeg_b64_sync",
        "_download_image_as_jpeg_b64",
        "_download_cover_preserving_metadata",
        "parse_bilibili_live_url",
        "parse_douyin_live_url",
        "_parse_bilibili_url",
        "_parse_twitter_url",
        "XIAOHONGSHU_STATE_RE",
        "_is_js_ident_char",
        "xiaohongshu_state_json",
        "parse_xiaohongshu_state",
        "xiaohongshu_video_candidate_urls",
        "extract_xiaohongshu_note_from_state",
        "_parse_social_html_sync",
        "_parse_youtube_or_social_url",
        "_WeChatArticleHTMLParser",
        "_extract_clean_html_blocks",
        "parse_wechat_mp_html",
        "_parse_wechat_mp_url",
        "extract_and_parse_link",
        "MAX_PARSEABLE_FILE_BYTES",
        "MAX_PARSEABLE_FILE_XML_CHARS",
        "_PLAIN_TEXT_DECODE_CAP_BYTES",
        "BINARY_UNSUPPORTED_FILE_EXTENSIONS",
        "_is_likely_binary_bytes",
        "_validate_zip_safety",
        "compact_long_user_text",
        "is_incoming_wechat_file",
        "_extract_file_metadata_and_bytes",
        "_decode_text_bounded",
        "_decode_and_parse_file_b64",
        "parse_wechat_file_xml",
        "_ORIG_PARSE_WECHAT_FILE_XML",
        "DEEPSEEK_OCR_BOX_BLOCK_RE",
        "DEEPSEEK_OCR_TOKEN_RE",
        "DEEPSEEK_OCR_COORDS_RE",
        "DEEPSEEK_OCR_BLANK_RE",
        "MAX_DEEPSEEK_OCR_BYTES",
        "clean_deepseek_ocr_text",
        "ocr_via_deepseek",
        "parse_wechat_file_xml_async",
        "extract_cookie_field_value",
        "normalize_qq_music_cookie",
        "search_and_fetch_music",
        "RANDOM_SONG_POOL",
        "pick_random_playable_song",
        "send_music_to_wechat",
        "send_wcf_music_and_audio",
        "extract_forwarded_chat_record",
        "_clean_referred_content",
        "extract_quote_context",
        "VOICE_REQUEST_RE",
        "is_voice_reply_requested",
        "_clean_text_for_tts",
        "MAX_DEEPSEEK_ASR_BYTES",
        "wrap_pcm24k_to_wav",
        "synthesize_tts_via_deepseek",
        "_resolve_ffmpeg_executable",
        "_convert_audio_bytes_to_wav16k",
        "transcribe_audio_via_deepseek",
        "synthesize_tts_mp3_b64",
        "synthesize_and_send_wcf_voice",
    ),
    "prompts": (
        "NON_REPEAT_PREFIXES",
        "is_repeatable_plain_text",
        "check_group_random_repeat",
        "GROUP_INTEREST_KEYWORDS",
        "GROUP_INTERJECT_SYSTEM",
        "GROUP_CONTINUATION_SYSTEM",
        "TaggedGroupLine",
        "format_relative_age",
        "format_group_tagged_transcript",
        "append_group_context_line",
        "record_group_message_for_interject",
        "evaluate_group_interject",
        "evaluate_group_active_continuation",
        "format_interject_logs",
        "format_wcf_moments_digest",
        "generate_moment_comment",
        "generate_moment_reply_comment",
        "format_wcf_friend_requests_digest",
        "OVERUSED_CLICHE_WORDS",
        "MOMENT_THEME_POOL",
        "MOMENT_PUBLIC_MOOD_POOL",
        "MOMENT_BANNED_PHRASES",
        "format_moment_schedule_context",
        "sanitize_moment_text",
        "SINGLE_CHAR_OPENERS",
        "extract_opening_prefixes",
        "build_anti_repetition_instruction",
        "build_unfetched_media_notice",
        "build_time_gap_notice",
    ),
    "outbound": (
        "MD_BULLET_LINE_RE",
        "INLINE_REPLY_ORDINAL_RE",
        "plain_markdown_bullets",
    ),
}

_core.install_facade(sys.modules[__name__], "diana_features", _EXPORTS)
