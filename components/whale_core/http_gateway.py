"""Shared HTTP plumbing: pooled ``httpx.AsyncClient`` borrowing, WCF circuit breaker,
SSRF-safe public URL fetching, bot/user name caches and WCF messaging, moments and friend REST wrappers.

Extracted from ``components/event_listeners/whale_listener.py`` / ``diana_features.py``
(v0.5.2 A-12). Those modules remain the public compatibility facades: tests and callers keep
patching ``whale_listener`` / ``diana_features`` attributes, which the facades propagate here.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import time
import urllib.parse
import weakref
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any
from zoneinfo import ZoneInfo

import httpx


logger = logging.getLogger(__name__)

__all__ = [
    "BOT_WXIDS_AND_NAMES",
    "DOUYIN_MOBILE_USER_AGENT",
    "GROUP_MEMBERS_CACHE_MAX",
    "GROUP_MEMBERS_CACHE_TTL_SEC",
    "PLACEHOLDER_DISPLAY_NAMES",
    "TZ",
    "USER_AGENT",
    "USER_NAME_CACHE_MAX",
    "USER_NAME_CACHE_TTL_SEC",
    "WCF_GATEWAY_URL",
    "WcfCircuitBreaker",
    "accept_wcf_friend_request",
    "close_shared_clients",
    "deepseek_tool_endpoint",
    "fetch_wcf_friend_requests",
    "fetch_wcf_moments",
    "get_cached_display_name",
    "get_cached_group_member_names",
    "get_dynamic_bot_names",
    "interact_wcf_moment",
    "is_deepseek_api_host",
    "is_placeholder_display_name",
    "publish_wcf_moment",
    "refresh_dynamic_bot_names",
    "reset_wcf_circuit_breaker",
    "resolve_user_display_name",
    "send_wcf_text_or_image",
    "send_wcf_voice",
    "set_wcf_friend_auto_accept",
    "strip_redundant_speaker_name",
]


# ==============================================================================
# from diana_features.py
# ==============================================================================
TZ = ZoneInfo("Asia/Shanghai")
WCF_GATEWAY_URL = os.getenv("WCF_GATEWAY_URL", "http://172.22.0.1:18080")
_ORIG_HTTPX_ASYNC_CLIENT = httpx.AsyncClient
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DOUYIN_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
)

# ==============================================================================
# 0. 共享连接池、WCF 断路器、安全/缓存与严肃内容熔断基础设施
# ==============================================================================

_CLIENT_POOLS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, httpx.AsyncClient]] = (
    weakref.WeakKeyDictionary()
)
_SECRETS_FILE_CACHE: dict[str, tuple[float, float, dict[str, Any]]] = {}
_SECRETS_FILE_TTL_SEC = 60.0


def _is_httpx_mocked() -> bool:
    return httpx.AsyncClient is not _ORIG_HTTPX_ASYNC_CLIENT


def _wcf_timeout(read_sec: float) -> httpx.Timeout:
    return httpx.Timeout(read=read_sec, connect=0.8, write=5.0, pool=2.0)


class WcfCircuitBreaker:
    """Circuit breaker for the local WCF HTTP gateway (fast-fails when unreachable)."""

    def __init__(self, failure_threshold: int = 2, cooldown_sec: float = 45.0) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_sec = max(1.0, float(cooldown_sec))
        self._failures = 0
        self._open_until = 0.0

    def allow_request(self) -> bool:
        if _is_httpx_mocked():
            return True
        return time.monotonic() >= self._open_until

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0

    def record_failure(self, exc: BaseException | None = None) -> None:
        if _is_httpx_mocked():
            return
        if exc is not None and not isinstance(
            exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException)
        ):
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._open_until = time.monotonic() + self.cooldown_sec

    def reset(self) -> None:
        self._failures = 0
        self._open_until = 0.0
_WCF_BREAKER = WcfCircuitBreaker(failure_threshold=2, cooldown_sec=45.0)


def reset_wcf_circuit_breaker() -> None:
    """Resets the module-level WCF circuit breaker state (for tests or recovery)."""
    _WCF_BREAKER.reset()


def _read_secrets_json_cached(path: str) -> dict[str, Any]:
    """Reads a secrets JSON file with st_mtime + 60s TTL caching (including negative misses)."""
    p = (path or "").strip()
    if not p:
        return {}
    now = time.time()
    cached = _SECRETS_FILE_CACHE.get(p)
    if cached and (now - cached[0]) < _SECRETS_FILE_TTL_SEC:
        return cached[2]
    try:
        st = os.stat(p)
        mtime = float(st.st_mtime)
    except OSError:
        _SECRETS_FILE_CACHE[p] = (now, -1.0, {})
        return {}
    if cached and cached[1] == mtime:
        _SECRETS_FILE_CACHE[p] = (now, mtime, cached[2])
        return cached[2]
    try:
        with open(p, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        data = parsed if isinstance(parsed, dict) else {}
    except Exception:
        data = {}
    _SECRETS_FILE_CACHE[p] = (now, mtime, data)
    return data


def _lru_ttl_get(
    cache: OrderedDict[Any, tuple[float, Any]],
    key: Any,
    ttl_sec: float,
    now_ts: float | None = None,
) -> Any:
    item = cache.get(key)
    if not item:
        return None
    cur = float(now_ts if now_ts is not None else time.time())
    if ttl_sec > 0 and (cur - float(item[0])) >= ttl_sec:
        cache.pop(key, None)
        return None
    cache.move_to_end(key)
    return item[1]


def _lru_ttl_set(
    cache: OrderedDict[Any, tuple[float, Any]],
    key: Any,
    payload: Any,
    max_size: int,
    now_ts: float | None = None,
) -> None:
    cur = float(now_ts if now_ts is not None else time.time())
    cache[key] = (cur, payload)
    cache.move_to_end(key)
    while len(cache) > max_size:
        cache.popitem(last=False)


def _sanitize_public_error(exc: BaseException) -> str:
    """Strips internal IP/proxy/credential details from errors before returning text to the model."""
    if isinstance(exc, ValueError):
        msg = str(exc).strip()
        if msg.startswith(("无效或受限", "公网响应体积", "链接")):
            return msg
        return "请求参数或安全校验未通过"
    if isinstance(exc, httpx.TooManyRedirects):
        return "网页重定向次数超限"
    if isinstance(exc, httpx.TimeoutException):
        return "网页访问超时"
    if isinstance(exc, httpx.ConnectError):
        return "无法连接到目标网页"
    if isinstance(exc, httpx.HTTPError):
        return f"网页网络请求失败 ({type(exc).__name__})"
    return f"读取异常 ({type(exc).__name__})"


class _SafePublicNetworkBackend:
    """Network backend wrapper that validates DNS and pins TCP connections to checked public IPs."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        if _is_safe_public_url is not _ORIG_IS_SAFE_PUBLIC_URL:
            return await self._inner.connect_tcp(
                host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
            )
        if port not in (80, 443):
            raise ValueError("无效或受限的 URL（仅允许访问公网 HTTP/HTTPS 端口）")
        ips = await asyncio.to_thread(_resolve_validated_public_ips, host)
        if not ips:
            raise ValueError("无效或受限的 URL（仅允许访问公网 HTTP/HTTPS 页面）")
        last_exc: BaseException | None = None
        for ip_addr in ips:
            try:
                return await self._inner.connect_tcp(
                    ip_addr, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise ValueError("无效或受限的 URL（无法建立公网连接）")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> Any:
        raise ValueError("不允许通过公网客户端连接本地套接字")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _create_pooled_client(kind: str, timeout: Any = None) -> httpx.AsyncClient:
    limits = (
        httpx.Limits(max_connections=32, max_keepalive_connections=16)
        if kind == "wcf"
        else httpx.Limits(max_connections=64, max_keepalive_connections=20)
    )
    if kind == "wcf":
        return _ORIG_HTTPX_ASYNC_CLIENT(
            timeout=timeout or _wcf_timeout(15.0),
            trust_env=False,
            limits=limits,
        )
    if kind == "public":
        transport = httpx.AsyncHTTPTransport(limits=limits)
        try:
            import httpcore

            pool = getattr(transport, "_pool", None)
            if isinstance(pool, httpcore.AsyncConnectionPool):
                pool._network_backend = _SafePublicNetworkBackend(pool._network_backend)
        except Exception:
            pass
        return _ORIG_HTTPX_ASYNC_CLIENT(
            timeout=timeout or httpx.Timeout(12.0, connect=5.0),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
            transport=transport,
        )
    return _ORIG_HTTPX_ASYNC_CLIENT(
        timeout=timeout or httpx.Timeout(30.0, connect=5.0),
        limits=limits,
    )


def _get_shared_client(kind: str, timeout: Any = None) -> httpx.AsyncClient:
    """Returns an event-loop-scoped pooled httpx.AsyncClient, or a fresh mocked client in tests."""
    if _is_httpx_mocked():
        kw: dict[str, Any] = {}
        if timeout is not None:
            kw["timeout"] = timeout
        elif kind == "wcf":
            kw["timeout"] = _wcf_timeout(15.0)
        if kind == "wcf":
            kw["trust_env"] = False
        return httpx.AsyncClient(**kw)

    loop = asyncio.get_running_loop()
    pool = _CLIENT_POOLS.setdefault(loop, {})
    client = pool.get(kind)
    if client is None or client.is_closed:
        client = _create_pooled_client(kind, timeout=timeout)
        pool[kind] = client
    return client


@asynccontextmanager
async def _borrow_client(kind: str, timeout: Any = None, **mock_kwargs: Any):
    """Borrows a shared pooled client (unclosed on exit) or creates/closes a fresh mocked client."""
    if _is_httpx_mocked():
        kw: dict[str, Any] = dict(mock_kwargs)
        if timeout is not None:
            kw["timeout"] = timeout
        elif kind == "wcf":
            kw["timeout"] = _wcf_timeout(15.0)
        if kind == "wcf" and "trust_env" not in kw:
            kw["trust_env"] = False
        async with httpx.AsyncClient(**kw) as client:
            yield client
        return
    yield _get_shared_client(kind, timeout=timeout)


async def close_shared_clients() -> None:
    """Closes all pooled httpx.AsyncClient instances for the current running event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    pool = _CLIENT_POOLS.pop(loop, None)
    if not pool:
        return
    for client in list(pool.values()):
        try:
            if not client.is_closed:
                await client.aclose()
        except Exception:
            pass


# ==============================================================================
# 4. 联网搜索 + 网页深度阅读（Diana Parallel Web Search & Reader + DeepSeek Tools）
# ==============================================================================


def is_deepseek_api_host(base_url: str) -> bool:
    """Returns True when `base_url` is empty (defaults to DeepSeek official) or points to `deepseek.com` / `*.deepseek.com` (Diana #1555)."""
    raw = (base_url or "").strip()
    if not raw:
        return True
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urllib.parse.urlparse(raw).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return False
    return bool(host and (host == "deepseek.com" or host.endswith(".deepseek.com")))


def deepseek_tool_endpoint(base_url: str, tool: str) -> str:
    """Builds the DeepSeek `/v1/tools/<tool>` endpoint URL from `base_url` (Diana #1555 / #1557)."""
    t = (tool or "").strip().strip("/")
    base = (base_url or "").strip() or "https://api.deepseek.com"
    if "://" not in base:
        base = "https://" + base
    base = base.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    path = parsed.path.rstrip("/")
    if path.endswith(f"/tools/{t}"):
        return base
    if path.endswith("/v1"):
        return f"{base}/tools/{t}"
    return f"{base}/v1/tools/{t}"
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.IPv6Network("64:ff9b:1::/48")
_IPV4_COMPAT_NET = ipaddress.IPv6Network("::/96")


def _is_public_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Checks whether an IPv4/IPv6 address (and any embedded/mapped IPv4 transition address) is public."""
    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
        or not ip_obj.is_global
    ):
        return False
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if ip_obj in _NAT64_WELL_KNOWN or ip_obj in _NAT64_LOCAL_USE or ip_obj in _IPV4_COMPAT_NET:
            return False
        if ip_obj.ipv4_mapped is not None and not _is_public_ip(ip_obj.ipv4_mapped):
            return False
        if ip_obj.sixtofour is not None and not _is_public_ip(ip_obj.sixtofour):
            return False
        if ip_obj.teredo is not None:
            server_ip, client_ip = ip_obj.teredo
            if not _is_public_ip(server_ip) or not _is_public_ip(client_ip):
                return False
    return True


def _resolve_validated_public_ips(host: str) -> list[str]:
    """Resolves `host` and returns validated public IPs (fail-closed if any address is unsafe)."""
    h = (host or "").strip().lower().rstrip(".").strip("[]")
    if (
        not h
        or h in ("localhost", "0.0.0.0")
        or h.endswith(".local")
        or h.endswith(".internal")
        or h.endswith(".localhost")
    ):
        return []
    try:
        addresses = socket.getaddrinfo(h, None)
    except Exception:
        return []
    if not addresses:
        return []
    valid_ips: list[str] = []
    for info in addresses:
        try:
            ip_str = str(info[4][0]).split("%", 1)[0]
            ip_obj = ipaddress.ip_address(ip_str)
        except Exception:
            return []
        if not _is_public_ip(ip_obj):
            return []
        if ip_str not in valid_ips:
            valid_ips.append(ip_str)
    return valid_ips


def _is_safe_public_url(url: str) -> bool:
    """Blocks SSRF attempts against loopback, private RFC1918, NAT64, transition IPv6, or non-standard ports."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if parsed.username or parsed.password:
            return False
        if parsed.port not in (None, 80, 443):
            return False
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        return bool(_resolve_validated_public_ips(host))
    except Exception:
        return False
_ORIG_IS_SAFE_PUBLIC_URL = _is_safe_public_url


async def _get_public_url(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int = 2 * 1024 * 1024,
    *,
    method: str = "GET",
    **kwargs: Any,
) -> httpx.Response:
    """逐跳检查公网地址，禁止自动跟进内网/无限重定向，并流式限制响应体积。"""
    request = client.build_request(method, url, **kwargs)
    for _ in range(6):
        if not await asyncio.to_thread(_is_safe_public_url, str(request.url)):
            raise ValueError("无效或受限的 URL（仅允许访问公网 HTTP/HTTPS 页面）")
        response = await client.send(request, stream=True, follow_redirects=False)
        if response.next_request is not None:
            next_req = response.next_request
            await response.aclose()
            request = next_req
            continue
        try:
            cl_hdr = response.headers.get("content-length")
            if cl_hdr:
                try:
                    if int(cl_hdr) > max_bytes:
                        raise ValueError("公网响应体积超出安全上限")
                except ValueError as ve:
                    if "超出安全上限" in str(ve):
                        raise
            buf = bytearray()
            async for chunk in response.aiter_bytes():
                if len(buf) + len(chunk) > max_bytes:
                    raise ValueError("公网响应体积超出安全上限")
                buf.extend(chunk)
            response._content = bytes(buf)
            return response
        finally:
            await response.aclose()
    raise httpx.TooManyRedirects("公网请求重定向次数超限", request=request)

# ==============================================================================
# 8. 微信引用回复解析（Quote / <refermsg>）、合并转发记录解析（<type>19</type>）与多气泡拟真跟发
# ==============================================================================

_DYNAMIC_BOT_CACHE: dict[str, Any] = {"ts": 0.0, "retry_at": 0.0, "names": set()}


def _get_bot_wxids_and_names() -> tuple[str, ...]:
    env_wxid = os.getenv("BOT_WXID", "").strip().lower()
    if not env_wxid:
        sec_file = os.getenv("WHALE_SECRETS_FILE", "/app/data/whale_secrets.json")
        d = _read_secrets_json_cached(sec_file)
        env_wxid = str(d.get("bot_wxid", "") or "").strip().lower()
    return tuple(x for x in (env_wxid, "大肥鱼", "鲸鱼娘", "小鲸鱼", "肥鱼") if x)
BOT_WXIDS_AND_NAMES = _get_bot_wxids_and_names()


async def refresh_dynamic_bot_names() -> None:
    """Dynamically fetches the current WeChat account wxid and nickname from WCF /api/status (cached 10 min)."""
    now = time.time()
    if now - float(_DYNAMIC_BOT_CACHE.get("ts", 0.0)) < 600.0:
        return
    if now < float(_DYNAMIC_BOT_CACHE.get("retry_at", 0.0)):
        return
    if not _WCF_BREAKER.allow_request():
        _DYNAMIC_BOT_CACHE["retry_at"] = now + 30.0
        return
    wcf_to = _wcf_timeout(2.5)
    try:
        async with _borrow_client("wcf", timeout=wcf_to) as client:
            r = await client.get(f"{WCF_GATEWAY_URL}/api/status", timeout=wcf_to)
            _WCF_BREAKER.record_success()
            if r.status_code == 200:
                j = r.json()
                if isinstance(j, dict):
                    _DYNAMIC_BOT_CACHE["ts"] = now
                    _DYNAMIC_BOT_CACHE["retry_at"] = 0.0
                    wxid = str(j.get("self_wxid") or "").strip().lower()
                    uinfo = j.get("user_info") or {}
                    uname = str(uinfo.get("name") or "").strip().lower()
                    names_set: set[str] = _DYNAMIC_BOT_CACHE.setdefault("names", set())
                    if wxid:
                        names_set.add(wxid)
                    if uname:
                        names_set.add(uname)
                    return
            _DYNAMIC_BOT_CACHE["retry_at"] = now + 30.0
    except Exception as e:
        _WCF_BREAKER.record_failure(e)
        _DYNAMIC_BOT_CACHE["retry_at"] = now + 30.0


def get_dynamic_bot_names() -> tuple[str, ...]:
    dyn = set(BOT_WXIDS_AND_NAMES)
    dyn.update(_DYNAMIC_BOT_CACHE.get("names") or set())
    return tuple(x for x in dyn if x)
_GROUP_MEMBERS_CACHE: OrderedDict[str, tuple[float, dict[str, str]]] = OrderedDict()
_USER_NAME_CACHE: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()
GROUP_MEMBERS_CACHE_MAX = 256
GROUP_MEMBERS_CACHE_TTL_SEC = 300.0
USER_NAME_CACHE_MAX = 1024
USER_NAME_CACHE_TTL_SEC = 300.0
PLACEHOLDER_DISPLAY_NAMES = frozenset({"群友", "朋友", "这位群友", "微信好友", "对方", "未知"})


def is_placeholder_display_name(name: str | None, wxid: str = "") -> bool:
    """Returns True if `name` is empty, a fallback placeholder, raw wxid, or 4-char wxid suffix."""
    s = (name or "").strip()
    uid = (wxid or "").strip()
    if not s:
        return True
    if s in PLACEHOLDER_DISPLAY_NAMES or s.startswith("这位群友("):
        return True
    if s.lower().startswith("wxid_"):
        return True
    if uid and (s == uid or (len(uid) >= 8 and uid.lower().startswith("wxid_") and s == uid[-4:])):
        return True
    return False


def _is_usable_display_name(name: str, wxid: str) -> bool:
    return not is_placeholder_display_name(name, wxid)


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
    """Returns cached group alias / WeChat nickname for wxid (enforcing TTL), or a clean non-wxid fallback."""
    uid = (wxid or "").strip()
    rid = (roomid or "").strip()
    if not uid:
        return "群友" if rid else "朋友"

    now_ts = time.time()
    c1 = _lru_ttl_get(_USER_NAME_CACHE, (rid, uid), USER_NAME_CACHE_TTL_SEC, now_ts)
    if c1 and _is_usable_display_name(c1, uid):
        return c1

    if rid:
        m_map = _lru_ttl_get(_GROUP_MEMBERS_CACHE, rid, GROUP_MEMBERS_CACHE_TTL_SEC, now_ts)
        if isinstance(m_map, dict):
            m_val = str(m_map.get(uid) or "").strip()
            if _is_usable_display_name(m_val, uid):
                return m_val

    c2 = _lru_ttl_get(_USER_NAME_CACHE, ("", uid), USER_NAME_CACHE_TTL_SEC, now_ts)
    if c2 and _is_usable_display_name(c2, uid):
        return c2

    if not uid.lower().startswith("wxid_"):
        return uid
    return "这位群友" if rid else "朋友"


def get_cached_group_member_names(roomid: str) -> list[str]:
    """Returns the cached list of real display names in a group chat."""
    rid = (roomid or "").strip()
    if not rid:
        return []
    m_map = _lru_ttl_get(_GROUP_MEMBERS_CACHE, rid, GROUP_MEMBERS_CACHE_TTL_SEC)
    if not isinstance(m_map, dict):
        return []
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
    Caches results in bounded LRU+TTL caches for 300 seconds.
    """
    uid = (wxid or "").strip()
    rid = (roomid or "").strip()
    if not uid:
        return "群友" if rid else "朋友"

    now = time.time()
    ev_name = (event_sender_name or "").strip()
    if _is_usable_display_name(ev_name, uid):
        _lru_ttl_set(_USER_NAME_CACHE, (rid, uid), ev_name, USER_NAME_CACHE_MAX, now)

    wcf_to = _wcf_timeout(2.5)
    # Refresh group member roster if in a group chat and cache is missing, expired (300s),
    # or missing this specific speaker after 45s (for newly resolved non-friend LRU nicknames)
    if rid and "@chatroom" in rid and _WCF_BREAKER.allow_request():
        rm_cached = _GROUP_MEMBERS_CACHE.get(rid)
        has_speaker_in_rm = bool(
            rm_cached and isinstance(rm_cached[1], dict) and _is_usable_display_name(rm_cached[1].get(uid, ""), uid)
        )
        ttl = 300.0 if has_speaker_in_rm else 45.0
        if not rm_cached or (now - rm_cached[0]) > ttl:
            try:
                async with _borrow_client("wcf", timeout=wcf_to) as client:
                    q_room = urllib.parse.quote(rid)
                    r = await client.get(f"{WCF_GATEWAY_URL}/api/chatroom_members?roomid={q_room}", timeout=wcf_to)
                    _WCF_BREAKER.record_success()
                    if r.status_code == 200:
                        j = r.json() or {}
                        members = j.get("members") or {}
                        if isinstance(members, dict) and members:
                            if rm_cached and isinstance(rm_cached[1], dict):
                                for old_uid in list(rm_cached[1].keys()):
                                    if old_uid not in members:
                                        _USER_NAME_CACHE.pop((rid, old_uid), None)
                            clean_map: dict[str, str] = {}
                            for mk, mv in members.items():
                                mk_str = str(mk)
                                m_str = str(mv or "").strip()
                                if _is_usable_display_name(m_str, mk_str):
                                    clean_map[mk_str] = m_str
                                    _lru_ttl_set(
                                        _USER_NAME_CACHE, (rid, mk_str), m_str, USER_NAME_CACHE_MAX, now
                                    )
                            _lru_ttl_set(
                                _GROUP_MEMBERS_CACHE, rid, clean_map, GROUP_MEMBERS_CACHE_MAX, now
                            )
            except Exception as e:
                _WCF_BREAKER.record_failure(e)
                logger.debug("Fetch chatroom_members skipped: %s", e)

    cached_now = _lru_ttl_get(_USER_NAME_CACHE, (rid, uid), USER_NAME_CACHE_TTL_SEC, now)
    if cached_now and _is_usable_display_name(cached_now, uid):
        return cached_now

    # Query single user display name from WCF /api/user_name
    if _WCF_BREAKER.allow_request():
        try:
            async with _borrow_client("wcf", timeout=wcf_to) as client:
                q_uid = urllib.parse.quote(uid)
                q_rid = urllib.parse.quote(rid)
                r = await client.get(
                    f"{WCF_GATEWAY_URL}/api/user_name?wxid={q_uid}&roomid={q_rid}",
                    timeout=wcf_to,
                )
                _WCF_BREAKER.record_success()
                if r.status_code == 200:
                    j = r.json() or {}
                    resolved = str(j.get("name") or "").strip()
                    if _is_usable_display_name(resolved, uid):
                        _lru_ttl_set(_USER_NAME_CACHE, (rid, uid), resolved, USER_NAME_CACHE_MAX, now)
                        _lru_ttl_set(_USER_NAME_CACHE, ("", uid), resolved, USER_NAME_CACHE_MAX, now)
                        if rid and rid in _GROUP_MEMBERS_CACHE:
                            _GROUP_MEMBERS_CACHE[rid][1][uid] = resolved
                            _GROUP_MEMBERS_CACHE.move_to_end(rid)
                        return resolved
        except Exception as e:
            _WCF_BREAKER.record_failure(e)
            logger.debug("Fetch user_name skipped: %s", e)

    return get_cached_display_name(uid, rid)


async def send_wcf_text_or_image(
    receiver: str,
    text: str = "",
    image_b64: str | None = None,
) -> bool:
    """Sends a follow-up text bubble or image directly via WCF HTTP Gateway."""
    if not _WCF_BREAKER.allow_request():
        return False
    ok = False
    wcf_to = _wcf_timeout(15.0)
    try:
        async with _borrow_client("wcf", timeout=wcf_to) as client:
            if text:
                r = await client.post(
                    f"{WCF_GATEWAY_URL}/api/send",
                    json={"receiver": receiver, "msg": text},
                    timeout=wcf_to,
                )
                _WCF_BREAKER.record_success()
                if r.status_code == 200 and r.json().get("status") == 0:
                    ok = True
            if image_b64 and _WCF_BREAKER.allow_request():
                ri = await client.post(
                    f"{WCF_GATEWAY_URL}/api/send_image",
                    json={"receiver": receiver, "base64": image_b64},
                    timeout=wcf_to,
                )
                _WCF_BREAKER.record_success()
                if ri.status_code == 200 and ri.json().get("status") == 0:
                    ok = True
    except Exception as e:
        _WCF_BREAKER.record_failure(e)
        logger.debug("WCF send follow-up bubble skipped: %s", e)
    return ok


async def send_wcf_voice(
    receiver: str,
    audio_b64: str,
    filename: str = "voice.mp3",
) -> bool:
    """Sends a native WeChat voice bubble (Type 34 SILK v3) via WCF V4 Gateway."""
    if not _WCF_BREAKER.allow_request():
        return False
    wcf_to = _wcf_timeout(35.0)
    try:
        async with _borrow_client("wcf", timeout=wcf_to) as client:
            r = await client.post(
                f"{WCF_GATEWAY_URL}/api/send_voice",
                json={
                    "receiver": receiver,
                    "base64": audio_b64,
                    "filename": filename,
                },
                timeout=wcf_to,
            )
            _WCF_BREAKER.record_success()
            if r.status_code == 200 and r.json().get("status") == 0:
                return True
    except Exception as e:
        _WCF_BREAKER.record_failure(e)
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
    if not _WCF_BREAKER.allow_request():
        return False
    wcf_to = _wcf_timeout(60.0)
    try:
        async with _borrow_client("wcf", timeout=wcf_to) as client:
            r = await client.post(
                f"{WCF_GATEWAY_URL}/api/send_moment",
                json={
                    "content": content,
                    "images": images,
                    "visibility": visibility or "public",
                },
                timeout=wcf_to,
            )
            _WCF_BREAKER.record_success()
            if r.status_code == 200 and r.json().get("status") == 0:
                logger.info("Successfully published WeChat V4 Moment: %s", content[:40])
                return True
    except Exception as e:
        _WCF_BREAKER.record_failure(e)
        logger.debug("WCF publish_moment skipped (V3 or unavailable): %s", e)
    return False


async def fetch_wcf_moments(limit: int = 10) -> tuple[bool, list[dict[str, Any]], str]:
    """Fetches loaded WeChat 4.1 Moments feed via WCF V4 Gateway (GET /api/moments).
    Returns (ok, rows, message).
    """
    if not _WCF_BREAKER.allow_request():
        return False, [], "微信网关暂未连接或不支持读取朋友圈"
    clamped = max(1, min(int(limit or 10), 30))
    wcf_to = _wcf_timeout(20.0)
    try:
        async with _borrow_client("wcf", timeout=wcf_to) as client:
            r = await client.get(
                f"{WCF_GATEWAY_URL}/api/moments",
                params={"limit": clamped},
                timeout=wcf_to,
            )
            _WCF_BREAKER.record_success()
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
        _WCF_BREAKER.record_failure(e)
        logger.debug("WCF fetch_moments skipped or unavailable: %s", e)
        return False, [], "微信网关暂未连接或不支持读取朋友圈"


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

    if not _WCF_BREAKER.allow_request():
        return False, "error", "微信网关暂未连接或不支持朋友圈互动"

    wcf_to = _wcf_timeout(35.0)
    try:
        async with _borrow_client("wcf", timeout=wcf_to) as client:
            r = await client.post(
                f"{WCF_GATEWAY_URL}/api/interact_moment",
                json={"sns_id": sns_str, "action": act, "content": content_str},
                timeout=wcf_to,
            )
            _WCF_BREAKER.record_success()
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
        _WCF_BREAKER.record_failure(e)
        logger.debug("WCF interact_moment skipped or unavailable: %s", e)
        return False, "error", "微信网关暂未连接或不支持朋友圈互动"


async def fetch_wcf_friend_requests() -> tuple[bool, dict[str, Any], str]:
    """Queries pending and recently processed WeChat 4.1 friend requests via WCF V4 Gateway (GET /api/friend_requests).
    Returns (ok, data_dict, error_message).
    """
    if not _WCF_BREAKER.allow_request():
        return False, {}, "微信网关暂未连接或不支持好友申请查询"
    wcf_to = _wcf_timeout(15.0)
    try:
        async with _borrow_client("wcf", timeout=wcf_to) as client:
            r = await client.get(f"{WCF_GATEWAY_URL}/api/friend_requests", timeout=wcf_to)
            _WCF_BREAKER.record_success()
            try:
                raw_json = r.json()
                data = raw_json if isinstance(raw_json, dict) else {}
            except Exception:
                data = {}
            if r.status_code == 200 and data.get("status") == 0:
                return True, data, ""
            return False, data, str(data.get("message") or f"HTTP {r.status_code}")
    except Exception as e:
        _WCF_BREAKER.record_failure(e)
        logger.debug("WCF fetch_wcf_friend_requests skipped or unavailable: %s", e)
        return False, {}, "微信网关暂未连接或不支持好友申请查询"


async def accept_wcf_friend_request(request_id: str) -> tuple[bool, dict[str, Any], str]:
    """Accepts a pending WeChat 4.1 friend request by request_id (fr_...) via POST /api/accept_friend.
    Returns (ok, data_dict, error_message).
    """
    req_str = str(request_id or "").strip()
    if not re.fullmatch(r"^fr_[0-9a-f]{32}$", req_str):
        return False, {}, "无效的好友申请 ID（格式应为 fr_ 加 32 位十六进制字符）"
    if not _WCF_BREAKER.allow_request():
        return False, {}, "微信网关暂未连接或通过好友申请失败"
    wcf_to = _wcf_timeout(20.0)
    try:
        async with _borrow_client("wcf", timeout=wcf_to) as client:
            r = await client.post(
                f"{WCF_GATEWAY_URL}/api/accept_friend",
                json={"request_id": req_str},
                timeout=wcf_to,
            )
            _WCF_BREAKER.record_success()
            try:
                raw_json = r.json()
                data = raw_json if isinstance(raw_json, dict) else {}
            except Exception:
                data = {}
            ok = r.status_code == 200 and data.get("status") == 0 and bool(data.get("accepted"))
            return ok, data, str(data.get("message") or "")
    except Exception as e:
        _WCF_BREAKER.record_failure(e)
        logger.debug("WCF accept_wcf_friend_request failed: %s", e)
        return False, {}, "微信网关暂未连接或通过好友申请失败"


async def set_wcf_friend_auto_accept(
    enabled: bool,
    keywords: list[str] | None = None,
) -> bool:
    """Syncs the automatic friend request acceptance setting and optional verification keywords to WCF Gateway."""
    if not _WCF_BREAKER.allow_request():
        return False
    payload: dict[str, Any] = {"enabled": bool(enabled)}
    if keywords is not None:
        payload["keywords"] = [str(k).strip()[:128] for k in keywords if str(k).strip()][:32]
    wcf_to = _wcf_timeout(10.0)
    try:
        async with _borrow_client("wcf", timeout=wcf_to) as client:
            r = await client.post(f"{WCF_GATEWAY_URL}/api/friend_auto_accept", json=payload, timeout=wcf_to)
            _WCF_BREAKER.record_success()
            return r.status_code == 200
    except Exception as e:
        _WCF_BREAKER.record_failure(e)
        logger.debug("WCF set_wcf_friend_auto_accept skipped: %s", e)
        return False
