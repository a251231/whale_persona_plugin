"""Diana IO / 安全回归：断路器、zip 炸弹、SSRF、缓存 TTL、识图兜底、TTS 超时、严肃话题熔断与炸毛状态机。

所有网络均通过 httpx.MockTransport / patch 模拟，不访问真实网络。
"""

import asyncio
import base64
import io
import os
import socket
import sys
import time
import types
import unittest
import zipfile
from contextlib import contextmanager
from unittest.mock import patch

import httpx

from components.event_listeners import diana_features as df

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def mock_client(handler):
    return patch.object(
        httpx,
        "AsyncClient",
        side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )


def fake_getaddrinfo(mapping=None, default="1.1.1.1"):
    mapping = mapping or {}

    def _fake(host, port=None, *args, **kwargs):
        try:
            import ipaddress

            ipaddress.ip_address(str(host).strip("[]"))
            ip = str(host).strip("[]")
        except ValueError:
            ip = mapping.get(host, default)
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return patch("socket.getaddrinfo", side_effect=_fake)


def file_message(name, raw: bytes) -> str:
    b64 = base64.b64encode(raw).decode("ascii")
    return f"<file_name>{name}</file_name><file_b64>{b64}</file_b64>"


def build_bomb_zip(entries=3, entry_mb=18) -> bytes:
    """Highly compressible zip whose declared uncompressed size exceeds 50 MiB."""
    out = io.BytesIO()
    chunk = b"\x00" * (1024 * 1024)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for i in range(entries):
            with zf.open(f"word/part{i}.xml", "w", force_zip64=True) as fh:
                for _ in range(entry_mb):
                    fh.write(chunk)
    return out.getvalue()


# ----------------------------------------------------------------------------
# A-06 forwarded chat record
# ----------------------------------------------------------------------------
class ForwardedChatRecordTests(unittest.TestCase):
    def test_bot_text_match_does_not_pollute_dynamic_bot_names(self):
        before = tuple(sorted(df.get_dynamic_bot_names()))
        bot_text = "今天天气真不错啊大家好"
        xml = (
            "<msg><appmsg><title>群聊的聊天记录</title>"
            f"<des>张三: {bot_text}\n李四: 嗯嗯</des><type>19</type>"
            "<recorditem><![CDATA[<recordinfo/>]]></recorditem></appmsg></msg>"
        )
        title, des, about_bot = df.extract_forwarded_chat_record(xml, [bot_text])
        self.assertTrue(about_bot)
        self.assertIn("张三", des)
        after = df.get_dynamic_bot_names()
        self.assertEqual(before, tuple(sorted(after)))
        self.assertNotIn("张三", after)
        self.assertNotIn("李四", after)

    def test_unrelated_record_is_not_about_bot(self):
        xml = (
            "<msg><appmsg><title>聊天记录</title><des>张三: 午饭吃什么</des>"
            "<type>19</type></appmsg></msg>"
        )
        _, _, about_bot = df.extract_forwarded_chat_record(xml, ["完全无关的机器人回复内容"])
        self.assertFalse(about_bot)
        self.assertNotIn("张三", df.get_dynamic_bot_names())


# ----------------------------------------------------------------------------
# A-04 circuit breaker / dynamic bot names retry
# ----------------------------------------------------------------------------
class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        df.reset_wcf_circuit_breaker()

    def tearDown(self):
        df.reset_wcf_circuit_breaker()

    def test_opens_after_two_connect_errors_then_cooldown(self):
        br = df.WcfCircuitBreaker(failure_threshold=2, cooldown_sec=45.0)
        self.assertTrue(br.allow_request())
        br.record_failure(httpx.ConnectError("down"))
        self.assertTrue(br.allow_request())
        br.record_failure(httpx.ConnectError("down"))
        self.assertFalse(br.allow_request())
        base = time.monotonic()
        with patch.object(time, "monotonic", return_value=base + 10.0):
            self.assertFalse(br.allow_request())
        with patch.object(time, "monotonic", return_value=base + 46.0):
            self.assertTrue(br.allow_request())

    def test_non_connect_errors_do_not_trip(self):
        br = df.WcfCircuitBreaker(failure_threshold=2, cooldown_sec=45.0)
        for _ in range(5):
            br.record_failure(ValueError("bad json"))
        self.assertTrue(br.allow_request())

    def test_success_resets_failure_count(self):
        br = df.WcfCircuitBreaker(failure_threshold=2, cooldown_sec=45.0)
        br.record_failure(httpx.ConnectTimeout("t"))
        br.record_success()
        br.record_failure(httpx.ConnectTimeout("t"))
        self.assertTrue(br.allow_request())

    def test_module_breaker_open_and_reset(self):
        df._WCF_BREAKER.record_failure(httpx.ConnectError("x"))
        df._WCF_BREAKER.record_failure(httpx.ConnectError("x"))
        self.assertFalse(df._WCF_BREAKER.allow_request())
        df.reset_wcf_circuit_breaker()
        self.assertTrue(df._WCF_BREAKER.allow_request())

    def test_breaker_bypassed_when_httpx_mocked(self):
        br = df.WcfCircuitBreaker(failure_threshold=1, cooldown_sec=45.0)
        br.record_failure(httpx.ConnectError("x"))
        self.assertFalse(br.allow_request())
        with mock_client(lambda r: httpx.Response(200)):
            self.assertTrue(br.allow_request())


class DynamicBotNamesRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        df.reset_wcf_circuit_breaker()
        self._saved = dict(df._DYNAMIC_BOT_CACHE)
        self._saved_names = set(df._DYNAMIC_BOT_CACHE.get("names") or set())
        df._DYNAMIC_BOT_CACHE.update({"ts": 0.0, "retry_at": 0.0, "names": set()})

    def tearDown(self):
        df._DYNAMIC_BOT_CACHE.clear()
        df._DYNAMIC_BOT_CACHE.update(self._saved)
        df._DYNAMIC_BOT_CACHE["names"] = self._saved_names
        df.reset_wcf_circuit_breaker()

    async def test_gateway_failure_retries_after_about_30s(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            raise httpx.ConnectError("gateway down", request=request)

        t0 = time.time()
        with mock_client(handler):
            await df.refresh_dynamic_bot_names()
            first_calls = len(calls)
            retry_at = float(df._DYNAMIC_BOT_CACHE["retry_at"])
            await df.refresh_dynamic_bot_names()  # inside the retry window: no new request
        self.assertGreaterEqual(first_calls, 1)
        self.assertEqual(len(calls), first_calls)
        self.assertGreaterEqual(retry_at - t0, 25.0)
        self.assertLessEqual(retry_at - t0, 35.0)
        self.assertEqual(float(df._DYNAMIC_BOT_CACHE["ts"]), 0.0)

    async def test_bad_status_also_retries_in_30s_not_600s(self):
        t0 = time.time()
        with mock_client(lambda r: httpx.Response(500)):
            await df.refresh_dynamic_bot_names()
        retry_at = float(df._DYNAMIC_BOT_CACHE["retry_at"])
        self.assertLessEqual(retry_at - t0, 35.0)
        self.assertEqual(float(df._DYNAMIC_BOT_CACHE["ts"]), 0.0)

    async def test_success_caches_names_for_long_window(self):
        payload = {"self_wxid": "wxid_BotSelf", "user_info": {"name": "TestBotNick"}}
        with mock_client(lambda r: httpx.Response(200, json=payload)):
            await df.refresh_dynamic_bot_names()
        names = df.get_dynamic_bot_names()
        self.assertIn("wxid_botself", names)
        self.assertIn("testbotnick", names)
        self.assertGreater(float(df._DYNAMIC_BOT_CACHE["ts"]), 0.0)


# ----------------------------------------------------------------------------
# A-05 zip safety / file parsing caps
# ----------------------------------------------------------------------------
class ZipSafetyTests(unittest.TestCase):
    def test_validate_zip_safety_rejects_declared_oversize_total(self):
        raw = build_bomb_zip()
        self.assertLess(len(raw), 5 * 1024 * 1024)  # compressible: tiny on the wire
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            self.assertFalse(df._validate_zip_safety(zf))

    def test_validate_zip_safety_rejects_single_oversize_entry(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            with zf.open("big.xml", "w", force_zip64=True) as fh:
                for _ in range(21):
                    fh.write(b"\x00" * (1024 * 1024))
        with zipfile.ZipFile(io.BytesIO(out.getvalue())) as zf:
            self.assertFalse(df._validate_zip_safety(zf))

    def test_validate_zip_safety_rejects_too_many_entries(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            for i in range(2005):
                zf.writestr(f"e{i}.txt", b"")
        with zipfile.ZipFile(io.BytesIO(out.getvalue())) as zf:
            self.assertFalse(df._validate_zip_safety(zf))

    def test_validate_zip_safety_accepts_small_archive(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("a.txt", "hello")
        with zipfile.ZipFile(io.BytesIO(out.getvalue())) as zf:
            self.assertTrue(df._validate_zip_safety(zf))

    def test_crafted_docx_and_xlsx_rejected_safely(self):
        raw = build_bomb_zip()
        for name in ("炸弹.docx", "炸弹.xlsx"):
            with self.subTest(name=name):
                fname, text = df.parse_wechat_file_xml(file_message(name, raw))
                self.assertEqual(fname, name)
                self.assertIn("安全上限", text)

    def test_plain_text_capped(self):
        raw = ("文本行 abc\n" * 40000).encode("utf-8")  # far beyond the 96 KiB window
        self.assertGreater(len(raw), 98304)
        fname, text = df.parse_wechat_file_xml(file_message("big.txt", raw))
        self.assertEqual(fname, "big.txt")
        self.assertTrue(text)
        self.assertLessEqual(len(text), 12000)

    def test_decode_text_bounded_handles_cut_multibyte_char(self):
        raw = b"a" + "中".encode("utf-8") * 60000
        out = df._decode_text_bounded(raw)
        self.assertTrue(out)
        self.assertLessEqual(len(out), 12000)
        self.assertNotIn("\ufffd", out)

    def test_cap_constants(self):
        self.assertEqual(df._PLAIN_TEXT_DECODE_CAP_BYTES, 96 * 1024)


# ----------------------------------------------------------------------------
# A-07 SSRF / public fetch
# ----------------------------------------------------------------------------
class SafePublicUrlTests(unittest.TestCase):
    def test_blocked_literals(self):
        urls = [
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:172.22.0.1]/",
            "http://[64:ff9b::7f00:1]/",
            "http://[64:ff9b:1::1]/",
            "http://1.1.1.1:8080/",
            "http://user@1.1.1.1/",
            "http://user:pw@1.1.1.1/",
            "http://127.0.0.1/",
            "http://localhost/",
            "ftp://1.1.1.1/",
            "file:///etc/passwd",
        ]
        for u in urls:
            with self.subTest(url=u):
                self.assertFalse(df._is_safe_public_url(u))

    def test_public_literal_allowed(self):
        self.assertTrue(df._is_safe_public_url("http://1.1.1.1/"))
        self.assertTrue(df._is_safe_public_url("https://1.1.1.1:443/x"))

    def test_hostname_resolving_to_mixed_private_is_blocked(self):
        def _mixed(host, port=None, *a, **k):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
            ]

        with patch("socket.getaddrinfo", side_effect=_mixed):
            self.assertFalse(df._is_safe_public_url("http://evil.example/"))

    def test_hostname_resolving_to_mapped_loopback_is_blocked(self):
        with fake_getaddrinfo({"rebind.example": "::ffff:127.0.0.1"}):
            self.assertFalse(df._is_safe_public_url("https://rebind.example/"))


class GetPublicUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_content_length_over_limit_rejected(self):
        with fake_getaddrinfo(), mock_client(lambda r: httpx.Response(200, content=b"x" * 3000)):
            async with httpx.AsyncClient() as client:
                with self.assertRaises(ValueError):
                    await df._get_public_url(client, "http://pub.example/a", max_bytes=1000)

    async def test_streaming_without_content_length_truncated(self):
        async def gen():
            for _ in range(10):
                yield b"y" * 500

        def handler(request):
            return httpx.Response(200, content=gen())

        with fake_getaddrinfo(), mock_client(handler):
            async with httpx.AsyncClient() as client:
                with self.assertRaises(ValueError):
                    await df._get_public_url(client, "http://pub.example/a", max_bytes=1000)

    async def test_small_body_returned(self):
        with fake_getaddrinfo(), mock_client(lambda r: httpx.Response(200, content=b"hello")):
            async with httpx.AsyncClient() as client:
                r = await df._get_public_url(client, "http://pub.example/a", max_bytes=1000)
        self.assertEqual(r.content, b"hello")

    async def test_redirect_to_loopback_blocked(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if request.url.host == "pub.example":
                return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})
            return httpx.Response(200, content=b"secret")

        with fake_getaddrinfo({"pub.example": "1.1.1.1"}), mock_client(handler):
            async with httpx.AsyncClient() as client:
                with self.assertRaises(ValueError):
                    await df._get_public_url(client, "http://pub.example/a")
        self.assertEqual(len(calls), 1)

    async def test_redirect_to_private_hostname_blocked(self):
        calls = []

        def handler(request):
            calls.append(request.url.host)
            if request.url.host == "pub.example":
                return httpx.Response(301, headers={"Location": "http://inner.example/x"})
            return httpx.Response(200, content=b"secret")

        with fake_getaddrinfo({"pub.example": "1.1.1.1", "inner.example": "192.168.1.9"}), mock_client(handler):
            async with httpx.AsyncClient() as client:
                with self.assertRaises(ValueError):
                    await df._get_public_url(client, "http://pub.example/a")
        self.assertEqual(calls, ["pub.example"])

    async def test_redirect_loop_raises_too_many_redirects(self):
        def handler(request):
            return httpx.Response(302, headers={"Location": "http://pub.example/loop"})

        with fake_getaddrinfo(), mock_client(handler):
            async with httpx.AsyncClient() as client:
                with self.assertRaises(httpx.TooManyRedirects):
                    await df._get_public_url(client, "http://pub.example/loop")

    async def test_public_redirect_is_followed(self):
        def handler(request):
            if request.url.path == "/a":
                return httpx.Response(302, headers={"Location": "http://pub.example/b"})
            return httpx.Response(200, content=b"final")

        with fake_getaddrinfo(), mock_client(handler):
            async with httpx.AsyncClient() as client:
                r = await df._get_public_url(client, "http://pub.example/a")
        self.assertEqual(r.content, b"final")


class ReadWebpageTests(unittest.IsolatedAsyncioTestCase):
    async def test_never_contacts_jina(self):
        hosts = []

        def handler(request):
            hosts.append(request.url.host)
            return httpx.Response(500)

        with fake_getaddrinfo(), mock_client(handler):
            out = await df.read_webpage_content("http://pub.example/page")
            out2 = await df.read_webpage_content(
                "http://pub.example/page2", deepseek_api_key="", deepseek_base_url=""
            )
        self.assertIsInstance(out, str)
        self.assertIsInstance(out2, str)
        self.assertTrue(hosts)
        self.assertFalse(any("jina" in h for h in hosts))

    async def test_blocks_private_url_without_request(self):
        hosts = []

        def handler(request):
            hosts.append(request.url.host)
            return httpx.Response(200, text="x")

        with mock_client(handler):
            out = await df.read_webpage_content("http://127.0.0.1/secret")
        self.assertEqual(hosts, [])
        self.assertIn("无效或受限", out)


class ExtractLinkDepthTests(unittest.IsolatedAsyncioTestCase):
    CARD = (
        "<msg><appmsg><title><![CDATA[分享标题]]></title><des><![CDATA[摘要]]></des>"
        "<type>5</type><url><![CDATA[https://mp.weixin.qq.com/s/abc]]></url>"
        "<sourcedisplayname>某号</sourcedisplayname></appmsg></msg>"
    )

    async def test_depth_beyond_limit_returns_none(self):
        with mock_client(lambda r: self.fail("no request expected")):
            self.assertEqual(await df.extract_and_parse_link("https://b23.tv/abc", _depth=2), (None, None))

    async def test_card_at_depth_one_does_not_recurse_into_url(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, text="<html></html>")

        with fake_getaddrinfo(), mock_client(handler):
            text, _ = await df.extract_and_parse_link(self.CARD, _depth=1)
        self.assertEqual(calls, [])
        self.assertIn("分享标题", text or "")


# ----------------------------------------------------------------------------
# A-08 / A-09 caches, pixel bomb, stolen stickers
# ----------------------------------------------------------------------------
class GroupMemberCacheTests(unittest.IsolatedAsyncioTestCase):
    ROOM = "99999@chatroom"

    def setUp(self):
        df.reset_wcf_circuit_breaker()
        df._GROUP_MEMBERS_CACHE.clear()
        df._USER_NAME_CACHE.clear()

    def tearDown(self):
        df._GROUP_MEMBERS_CACHE.clear()
        df._USER_NAME_CACHE.clear()

    async def test_refresh_replaces_departed_members(self):
        stale_ts = time.time() - 10000
        df._GROUP_MEMBERS_CACHE[self.ROOM] = (stale_ts, {"wxid_old0001": "已退群的人", "wxid_new0001": "旧昵称"})
        df._USER_NAME_CACHE[(self.ROOM, "wxid_old0001")] = (time.time(), "已退群的人")

        def handler(request):
            if request.url.path.endswith("/api/chatroom_members"):
                return httpx.Response(200, json={"members": {"wxid_new0001": "新昵称", "wxid_new0002": "另一位"}})
            return httpx.Response(404)

        with mock_client(handler):
            name = await df.resolve_user_display_name("wxid_new0001", self.ROOM)
        self.assertEqual(name, "新昵称")
        members = df._GROUP_MEMBERS_CACHE[self.ROOM][1]
        self.assertNotIn("wxid_old0001", members)
        self.assertEqual(members["wxid_new0001"], "新昵称")
        self.assertNotIn((self.ROOM, "wxid_old0001"), df._USER_NAME_CACHE)
        self.assertNotIn("已退群的人", df.get_cached_group_member_names(self.ROOM))

    def test_get_cached_display_name_honors_ttl(self):
        uid = "wxid_ttl00001"
        df._USER_NAME_CACHE[("", uid)] = (time.time(), "小明")
        self.assertEqual(df.get_cached_display_name(uid), "小明")
        df._USER_NAME_CACHE[("", uid)] = (time.time() - df.USER_NAME_CACHE_TTL_SEC - 5, "小明")
        self.assertEqual(df.get_cached_display_name(uid), "朋友")

    def test_group_roster_ttl_expired_falls_back_to_placeholder(self):
        uid = "wxid_ttl00002"
        df._GROUP_MEMBERS_CACHE[self.ROOM] = (time.time(), {uid: "群友甲"})
        self.assertEqual(df.get_cached_display_name(uid, self.ROOM), "群友甲")
        df._GROUP_MEMBERS_CACHE[self.ROOM] = (time.time() - df.GROUP_MEMBERS_CACHE_TTL_SEC - 5, {uid: "群友甲"})
        self.assertEqual(df.get_cached_display_name(uid, self.ROOM), "这位群友")
        self.assertEqual(df.get_cached_group_member_names(self.ROOM), [])


def _png_bytes(size) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("L", size, 0).save(buf, format="PNG", optimize=False, compress_level=9)
    return buf.getvalue()


def _big_noisy_gif(min_bytes=620 * 1024) -> bytes:
    from PIL import Image

    frames = [Image.frombytes("L", (300, 300), os.urandom(300 * 300)) for _ in range(8)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], loop=0)
    data = buf.getvalue()
    if len(data) <= min_bytes:
        raise unittest.SkipTest("could not build large GIF")  # pragma: no cover
    return data


class StickerSafetyTests(unittest.IsolatedAsyncioTestCase):
    XML = '<emoji cdnurl="http://pub.example/s.bin" md5="abcdef0123456789" />'

    def test_pixel_bomb_rejected_cleanly(self):
        raw = _png_bytes((5001, 5001))  # 25.01 MP > 20 MP limit
        self.assertLess(len(raw), 1024 * 1024)
        with self.assertRaises(ValueError):
            df._encode_stolen_sticker_sync(raw)

    def test_small_gif_kept_large_gif_not_stored_raw(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("P", (8, 8), 1).save(buf, format="GIF")
        small = buf.getvalue()
        b64, is_gif = df._encode_stolen_sticker_sync(small)
        self.assertTrue(is_gif)
        self.assertEqual(base64.b64decode(b64), small)

        big = _big_noisy_gif()
        self.assertGreater(len(big), df._STOLEN_GIF_MAX_BYTES)
        b64, is_gif = df._encode_stolen_sticker_sync(big)
        self.assertFalse(is_gif)
        self.assertLess(len(base64.b64decode(b64)), len(big))

    async def test_steal_rejects_pixel_bomb(self):
        raw = _png_bytes((5001, 5001))
        stolen: list = []
        with fake_getaddrinfo(), mock_client(lambda r: httpx.Response(200, content=raw)), patch.object(
            df, "save_stolen_memes"
        ) as saver:
            ok = await df.steal_incoming_sticker(self.XML, {}, stolen)
        self.assertFalse(ok)
        self.assertEqual(stolen, [])
        saver.assert_not_called()

    async def test_steal_large_gif_never_saved_as_gif(self):
        big = _big_noisy_gif()
        stolen: list = []
        with fake_getaddrinfo(), mock_client(lambda r: httpx.Response(200, content=big)), patch.object(
            df, "save_stolen_memes"
        ):
            await df.steal_incoming_sticker(self.XML, {}, stolen)
        for item in stolen:
            self.assertFalse(item.get("is_gif"))
            self.assertLess(len(base64.b64decode(item["b64"])), len(big))

    async def test_steal_rejects_loopback_cdn(self):
        stolen: list = []
        xml = '<emoji cdnurl="http://127.0.0.1/s.png" md5="ffff" />'
        with mock_client(lambda r: self.fail("must not fetch")), patch.object(df, "save_stolen_memes"):
            ok = await df.steal_incoming_sticker(xml, {}, stolen)
        self.assertFalse(ok)
        self.assertEqual(stolen, [])


# ----------------------------------------------------------------------------
# A-10 SiliconFlow / continuation / TTS
# ----------------------------------------------------------------------------
_vision_counter = [0]


def _unique_b64() -> str:
    _vision_counter[0] += 1
    return base64.b64encode(f"img-{os.getpid()}-{time.time_ns()}-{_vision_counter[0]}".encode()).decode()


class SiliconFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_stops_on_401_and_402(self):
        for status in (401, 402, 403):
            with self.subTest(status=status):
                calls = []

                def handler(request, status=status):
                    calls.append(1)
                    return httpx.Response(status, json={"error": "x"})

                with mock_client(handler):
                    out = await df.describe_image_via_siliconflow(_unique_b64(), sf_api_key="sk-test")
                self.assertEqual(out, "")
                self.assertEqual(len(calls), 1)

    async def test_falls_through_to_next_model_on_500(self):
        models = []

        def handler(request):
            import json

            body = json.loads(request.content)
            models.append(body["model"])
            if len(models) == 1:
                return httpx.Response(500)
            return httpx.Response(200, json={"choices": [{"message": {"content": "一只猫"}}]})

        with mock_client(handler):
            out = await df.describe_image_via_siliconflow(_unique_b64(), sf_api_key="sk-test")
        self.assertEqual(out, "一只猫")
        self.assertEqual(len(models), 2)
        self.assertNotEqual(models[0], models[1])

    async def test_result_cached_by_hash(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json={"choices": [{"message": {"content": "缓存图"}}]})

        b64 = _unique_b64()
        with mock_client(handler):
            a = await df.describe_image_via_siliconflow(b64, sf_api_key="sk-test")
            b = await df.describe_image_via_siliconflow(b64, sf_api_key="sk-test")
        self.assertEqual((a, b), ("缓存图", "缓存图"))
        self.assertEqual(len(calls), 1)


class ContinuationVisionBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_deepseek_endpoint_bridges_via_siliconflow_vl(self):
        import json

        chat_bodies = []

        def handler(request):
            body = json.loads(request.content)
            if "siliconflow" in request.url.host:
                return httpx.Response(200, json={"choices": [{"message": {"content": "一张猫咪表情包"}}]})
            chat_bodies.append(body)
            content = json.dumps(
                {"should_reply": True, "reply": "好可爱", "reason": "继续聊", "is_serious": False},
                ensure_ascii=False,
            )
            return httpx.Response(
                200, json={"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 7}}
            )

        state: dict = {}
        with mock_client(handler):
            should, reply, _, used = await df.evaluate_group_active_continuation(
                api_key="k",
                base_url="https://llm.example.com/v1",
                model="some-text-model",
                group_id="1234567890@chatroom",
                sender_id="wxid_cont0001",
                current_text="你看这个",
                recent_lines=[],
                last_bot_reply="嗯嗯",
                is_same_speaker=True,
                state=state,
                sender_name="阿猫",
                vision_b64=_unique_b64(),
                sf_api_key="sk-test",
            )
        self.assertTrue(should)
        self.assertEqual(reply, "好可爱")
        self.assertEqual(used, 7)
        self.assertEqual(len(chat_bodies), 1)
        user_content = chat_bodies[0]["messages"][1]["content"]
        self.assertIsInstance(user_content, str)
        self.assertIn("一张猫咪表情包", user_content)
        self.assertNotIn("image_url", json.dumps(chat_bodies[0]))

    async def test_serious_text_stays_silent_without_request(self):
        state: dict = {}
        with mock_client(lambda r: self.fail("must not call model")):
            should, reply, reason, used = await df.evaluate_group_active_continuation(
                api_key="k",
                base_url="https://llm.example.com/v1",
                model="m",
                group_id="1234567890@chatroom",
                sender_id="wxid_cont0002",
                current_text="我奶奶昨天去世了",
                recent_lines=[],
                last_bot_reply="嗯",
                is_same_speaker=True,
                state=state,
                sender_name="阿猫",
            )
        self.assertFalse(should)
        self.assertEqual(reply, "")
        self.assertEqual(used, 0)
        self.assertTrue(reason)


class TtsTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_edge_tts_timeout_returns_none(self):
        class _Comm:
            def __init__(self, *a, **k):
                pass

            async def stream(self):
                await asyncio.sleep(3600)
                yield {}  # pragma: no cover

        fake_mod = types.ModuleType("edge_tts")
        fake_mod.Communicate = _Comm

        async def fake_wait_for(coro, timeout=None):
            coro.close()
            raise asyncio.TimeoutError()

        env = {
            "SILICONFLOW_API_KEY": "",
            "WHALE_SECRETS_FILE": os.path.join(os.path.dirname(__file__), "_no_such_secrets.json"),
            "WHALE_SECRETS_BACKUP_FILE": os.path.join(os.path.dirname(__file__), "_no_such_secrets2.json"),
        }
        with patch.dict(os.environ, env), patch.dict(sys.modules, {"edge_tts": fake_mod}), patch.object(
            asyncio, "wait_for", fake_wait_for
        ), mock_client(lambda r: self.fail("no network expected")):
            out = await df.synthesize_tts_mp3_b64("你好呀今天天气不错")
        self.assertIsNone(out)

    async def test_too_short_or_too_long_text_skipped(self):
        with mock_client(lambda r: self.fail("no network expected")):
            self.assertIsNone(await df.synthesize_tts_mp3_b64("好"))
            self.assertIsNone(await df.synthesize_tts_mp3_b64("好" * 200))


# ----------------------------------------------------------------------------
# U-05 / U-11 serious moments & temper
# ----------------------------------------------------------------------------
class SeriousMomentTests(unittest.IsolatedAsyncioTestCase):
    def test_is_serious_or_sad_moment(self):
        for t in ("我妈住院了", "爷爷去世了", "ICU 抢救中", "icu外面等着"):
            with self.subTest(text=t):
                self.assertTrue(df.is_serious_or_sad_moment(t))
        for t in ("今天吃火锅", "", "   ", None):
            with self.subTest(text=t):
                self.assertFalse(df.is_serious_or_sad_moment(t))

    async def test_comment_empty_for_serious_without_request(self):
        def handler(request):
            self.fail("model must not be called for serious moments")

        with mock_client(handler):
            for content in ("我妈住院了，心情很差", "沉痛悼念，爷爷去世"):
                out = await df.generate_moment_comment(
                    "k", "https://llm.example.com/v1", "m", author_name="阿猫", moment_content=content
                )
                self.assertEqual(out, ("", 0))

    async def test_comment_empty_when_existing_comment_is_serious(self):
        with mock_client(lambda r: self.fail("model must not be called")):
            out = await df.generate_moment_comment(
                "k",
                "https://llm.example.com/v1",
                "m",
                author_name="阿猫",
                moment_content="晒一下今天的天空",
                existing_comments=[{"nickname": "路人", "content": "节哀"}],
            )
        self.assertEqual(out, ("", 0))

    async def test_comment_empty_when_model_says_skip(self):
        for raw in ("SKIP", "`SKIP`", " skip "):
            with self.subTest(raw=raw):
                with mock_client(
                    lambda r, raw=raw: httpx.Response(
                        200, json={"choices": [{"message": {"content": raw}}], "usage": {"total_tokens": 5}}
                    )
                ):
                    text, used = await df.generate_moment_comment(
                        "k", "https://llm.example.com/v1", "m", author_name="阿猫", moment_content="晒娃"
                    )
                self.assertEqual(text, "")
                self.assertEqual(used, 5)

    async def test_comment_normal_text_returned(self):
        with mock_client(
            lambda r: httpx.Response(
                200, json={"choices": [{"message": {"content": "“这顿看着就好吃。”"}}], "usage": {"total_tokens": 9}}
            )
        ):
            text, used = await df.generate_moment_comment(
                "k", "https://llm.example.com/v1", "m", author_name="阿猫", moment_content="今天吃火锅"
            )
        self.assertEqual(text, "这顿看着就好吃")
        self.assertEqual(used, 9)


class TemperStateTests(unittest.TestCase):
    def test_generic_self_or_third_party_insults_ignored(self):
        for text in ("甲方真蠢", "我真是个笨蛋"):
            with self.subTest(text=text):
                tmap: dict = {}
                hint, tag, delta = df.check_and_update_temper_state(tmap, "g1", "u1", text)
                self.assertEqual((hint, tag, delta), ("", None, 0))
                self.assertEqual(tmap[("g1", "u1")]["taunt_times"], [])
                self.assertEqual(tmap[("g1", "u1")]["sulking_until"], 0.0)

    def test_directed_taunt_is_recorded_and_triggers_sulk_on_second(self):
        tmap: dict = {}
        df.check_and_update_temper_state(tmap, "g1", "u1", "你真蠢")
        self.assertEqual(len(tmap[("g1", "u1")]["taunt_times"]), 1)
        hint, tag, delta = df.check_and_update_temper_state(tmap, "g1", "u1", "你好蠢啊")
        self.assertTrue(hint)
        self.assertEqual(delta, -4)
        self.assertGreater(tmap[("g1", "u1")]["sulking_until"], time.time())

    def test_hotpot_without_sulking_does_not_forgive(self):
        tmap: dict = {}
        hint, tag, delta = df.check_and_update_temper_state(tmap, "g1", "u1", "周末一起去吃火锅吧")
        self.assertEqual((hint, tag, delta), ("", None, 0))
        self.assertNotIn("原谅", hint)

    def test_coax_while_sulking_forgives(self):
        tmap = {("g1", "u1"): {"taunt_times": [time.time()], "sulking_until": time.time() + 100.0}}
        hint, tag, delta = df.check_and_update_temper_state(tmap, "g1", "u1", "请你吃火锅")
        self.assertIn("原谅", hint)
        self.assertGreater(delta, 0)
        self.assertEqual(tmap[("g1", "u1")]["sulking_until"], 0.0)


if __name__ == "__main__":
    unittest.main()
