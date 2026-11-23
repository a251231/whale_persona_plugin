"""Regression tests for Nov 22 upstream sync (v0.5.4):
- Yuuyuko-uu/dsh-fish-memory v1.6.5..v1.6.6: default-off short-query (<6 char) prior-turn borrowing
  and expanded common filler bigram/window filtering so common-char combos never recall unrelated memory
- SuInk/Diana #1602, #1634, #1638: tail pre-output check in `<turn-context>` (keeping Tier-1
  system_prompt byte-stable for Prefix-Cache) + group-only ~20-char short-reaction guard
- SuInk/Diana #1639, #1645: protected-span bubble splitting (inline code, URLs, paired CJK/ASCII
  brackets & quotes, English apostrophes like `character's`), explicit newline boundary, and
  comedic empty-parens (`（）`) split keeping `（）` at the end of bubble 1
- SuInk/Diana #1617, #1641: `edge-tts>=7.3.1` pin (Sec-MS-GEC v2), QQ-Music-only proxy env var
  (`QQ_MUSIC_PROXY` / `MUSIC_PROXY`), credential-redacting `_sanitize_music_error`, and QQ `code=2001` hint
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from components.event_listeners import diana_features as df  # noqa: E402
from components.event_listeners import whale_listener as wl  # noqa: E402
from components.whale_core import media_parsers as mp  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _MockBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self._patches = [
            patch.object(wl, "STATE_FILE", str(tmp / "state.json")),
            patch.object(wl, "STATE_BACKUP_FILE", str(tmp / "state.bak.json")),
            patch.object(wl, "SECRETS_FILE", str(tmp / "secrets.json")),
            patch.object(wl, "SECRETS_BACKUP_FILE", str(tmp / "secrets.bak.json")),
            patch.object(df, "STOLEN_MEMES_FILE", str(tmp / "stolen.json")),
            patch.object(df, "STOLEN_MEMES_BACKUP_FILE", str(tmp / "stolen.bak.json")),
        ]
        for p in self._patches:
            p.start()
        self.listener = wl.WhaleListener()
        self.listener._state["enabled"] = True
        self.listener._state["enabled_groups"] = ["room-A"]
        self.listener._state["group_chat_enabled"] = True
        cfg = {"deepseek_api_key": "k-test", "deepseek_model": "deepseek-flash", "owner_ids": "wxid_test"}
        self.listener.plugin = type(
            "_P",
            (),
            {
                "config": cfg,
                "get_config": lambda _self: cfg,
                "send_message": AsyncMock(return_value=None),
            },
        )()

    def tearDown(self):
        try:
            if getattr(self.listener, "_save_debounce_handle", None):
                self.listener._save_debounce_handle.cancel()
        except Exception:
            pass
        for p in reversed(self._patches):
            p.stop()
        self._tmpdir.cleanup()

    @staticmethod
    def _mk_ctx(text: str, *, sender_id: str = "wxid_u1", launcher_id: str = "wxid_u1"):
        replies: list[str] = []

        async def _reply_mc(mc, *args, **kwargs):
            replies.append(str(mc))

        ctx = SimpleNamespace(
            event=SimpleNamespace(
                message_chain=MessageChain([Plain(text=text)]),
                sender_id=sender_id,
                launcher_id=launcher_id,
                source_platform_object={},
                reply_message_chain=None,
            ),
            reply=_reply_mc,
            query_id=None,
            is_prevented_default=lambda: False,
            prevent_default=lambda: None,
        )
        return ctx, replies


class MemoryShortQueryAndStopBigramTests(_MockBase):
    """dsh-fish-memory v1.6.5..v1.6.6: stop-word bigrams + default-off short-query borrowing."""

    def test_common_filler_bigrams_and_windows_score_zero(self):
        noise_pairs = [
            ("没去呢", "上周没去公园散步"),
            ("还没好吗", "作业还没写完呢"),
            ("其实确实有点", "对方说其实确实喜欢安静"),
            ("好像之前提过", "好像之前路过那条街"),
            ("随便这样吧", "平时随便这样吃一口"),
            ("应该可能行", "明天应该可能下雨"),
        ]
        for query, target in noise_pairs:
            with self.subTest(query=query, target=target):
                self.assertEqual(df.compute_memory_query_overlap(query, target), 0.0)
                self.assertEqual(df.compute_memory_query_metrics(query, target), (0.0, 0, 0))

        # Concrete nouns and proper-noun clues must continue matching strongly
        real_pairs = [
            ("钥匙忘带了", "出门总忘带家门钥匙"),
            ("拖鞋放哪了", "买了一双毛绒拖鞋"),
            ("小糯今天乖吗", "对方养的小猫叫小糯"),
            ("瑞幸生椰好喝", "爱喝瑞幸生椰拿铁"),
        ]
        for query, target in real_pairs:
            with self.subTest(query=query, target=target):
                self.assertGreater(df.compute_memory_query_overlap(query, target), 0.0)

    async def test_short_query_borrowing_default_off_and_opt_in_in_private_chat(self):
        self.listener._resolve_bot_uuid = AsyncMock(return_value="bot-A")
        self.listener._save_persistent_state = lambda: None
        self.listener._async_post_turn_judge = AsyncMock(return_value=None)
        self.listener._async_post_group_turn_judge = AsyncMock(return_value=None)
        self.listener._maybe_check_user_moment = AsyncMock(return_value="")

        u = self.listener._get_user_state("wxid_u1")
        now_ms = time.time() * 1000.0
        # Put 4 newer filler events ahead of the older hotpot event so the hotpot event ONLY surfaces
        # when query overlap actually hits "九宫格火锅" (isolating short-query borrowing from recency slice).
        u["events"] = [
            {"date": "2026-11-01", "ts": now_ms - 20 * 86400000, "text": "在重庆吃了九宫格火锅"},
            {"date": "2026-11-18", "ts": now_ms - 4 * 86400000, "text": "周二整理了书架相册"},
            {"date": "2026-11-19", "ts": now_ms - 3 * 86400000, "text": "周三去公园散步吹风"},
            {"date": "2026-11-20", "ts": now_ms - 2 * 86400000, "text": "周四给窗台绿植浇水"},
            {"date": "2026-11-21", "ts": now_ms - 1 * 86400000, "text": "周五修了台灯开关线"},
        ]

        # Seed prior user turn in session history mentioning 九宫格火锅
        sk = "bot-A:person:wxid_u1:wxid_u1"
        self.listener._histories[sk] = [
            (time.time() - 30.0, "user", "昨晚吃了九宫格火锅"),
            (time.time() - 25.0, "assistant", "辣不辣呀"),
        ]

        captured_payloads: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/chat/completions"):
                captured_payloads.append(json.loads(request.content.decode("utf-8")))
                return httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "行吧知道了"}}],
                        "usage": {"total_tokens": 18},
                    },
                )
            return httpx.Response(200, json={"is_available": True, "balance_infos": [{"total_balance": "50.00"}]})

        with (
            patch.object(
                httpx,
                "AsyncClient",
                side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw),
            ),
            patch.object(df, "resolve_user_display_name", AsyncMock(return_value="小明")),
            patch.object(df, "refresh_dynamic_bot_names", AsyncMock(return_value=None)),
            patch.object(df, "send_wcf_text_or_image", AsyncMock(return_value=True)),
        ):
            # 1) Default (memory_borrow_short_context unset/False): short "<6" msg must NOT borrow prior turn
            ctx1, _ = self._mk_ctx("确实啊")
            await self.listener._handle_chat(ctx1, is_group=False)
            self.assertEqual(len(captured_payloads), 1)
            user_msg_1 = captured_payloads[0]["messages"][-1]["content"]
            self.assertNotIn("九宫格火锅", user_msg_1)

            # 2) Opt-in (memory_borrow_short_context = True): short msg borrows prior turn and recalls event
            self.listener._state["memory_borrow_short_context"] = True
            self.listener._histories[sk] = [
                (time.time() - 20.0, "user", "昨晚吃了九宫格火锅"),
                (time.time() - 15.0, "assistant", "辣不辣呀"),
            ]
            ctx2, _ = self._mk_ctx("真香啊")
            await self.listener._handle_chat(ctx2, is_group=False)
            self.assertEqual(len(captured_payloads), 2)
            user_msg_2 = captured_payloads[1]["messages"][-1]["content"]
            self.assertIn("九宫格火锅", user_msg_2)


class TailPrecheckAndGroupGuardTests(_MockBase):
    """SuInk/Diana #1602, #1634, #1638: tail pre-output check & group short-reaction guard."""

    def test_build_tail_precheck_line_private_vs_group(self):
        priv = df.build_tail_precheck_line("2026-11-22 14:30", is_group=False)
        self.assertIn("2026-11-22 14:30", priv)
        self.assertIn("别照抄对方原话开头", priv)
        self.assertIn("没实际调工具查过别说查过", priv)
        self.assertNotIn("群聊闲聊只回1句", priv)

        grp = df.build_tail_precheck_line("2026-11-22 14:30", is_group=True)
        self.assertIn("2026-11-22 14:30", grp)
        self.assertIn("别照抄对方原话开头", grp)
        self.assertIn("没实际调工具查过别说查过", grp)
        self.assertIn("群聊闲聊只回1句", grp)
        self.assertIn("约20字内", grp)
        self.assertIn("问技术/查资料不受限", grp)

    async def test_tier1_system_prompt_stays_stable_while_turn_context_carries_tail_check(self):
        self.listener._resolve_bot_uuid = AsyncMock(return_value="bot-A")
        self.listener._save_persistent_state = lambda: None
        self.listener._async_post_turn_judge = AsyncMock(return_value=None)
        self.listener._async_post_group_turn_judge = AsyncMock(return_value=None)
        self.listener._maybe_check_user_moment = AsyncMock(return_value="")

        captured_payloads: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/chat/completions"):
                captured_payloads.append(json.loads(request.content.decode("utf-8")))
                return httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "哈哈不错呀"}}],
                        "usage": {"total_tokens": 20},
                    },
                )
            return httpx.Response(200, json={"is_available": True, "balance_infos": [{"total_balance": "50.00"}]})

        with (
            patch.object(
                httpx,
                "AsyncClient",
                side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw),
            ),
            patch.object(df, "resolve_user_display_name", AsyncMock(return_value="阿鱼友")),
            patch.object(df, "refresh_dynamic_bot_names", AsyncMock(return_value=None)),
            patch.object(df, "send_wcf_text_or_image", AsyncMock(return_value=True)),
        ):
            # Two consecutive group turns: Tier-1 system prompt must stay byte-identical (Prefix-Cache safe)
            ctx_g1, _ = self._mk_ctx("大肥鱼 今天天气真不错", sender_id="wxid_g1", launcher_id="room-A")
            await self.listener._handle_chat(ctx_g1, is_group=True)
            ctx_g2, _ = self._mk_ctx("大肥鱼 我准备点奶茶了", sender_id="wxid_g1", launcher_id="room-A")
            await self.listener._handle_chat(ctx_g2, is_group=True)

            self.assertEqual(len(captured_payloads), 2)
            sys1 = captured_payloads[0]["messages"][0]["content"]
            sys2 = captured_payloads[1]["messages"][0]["content"]
            self.assertEqual(sys1, sys2)
            self.assertNotIn("动笔前确认", sys1)
            self.assertNotIn("约20字内", sys1)

            tail1 = captured_payloads[0]["messages"][-1]["content"]
            self.assertIn("<turn-context>", tail1)
            self.assertIn("群聊闲聊只回1句", tail1)
            self.assertIn("没实际调工具查过别说查过", tail1)


class ProtectedSpanBubbleSplitTests(unittest.TestCase):
    """SuInk/Diana #1639, #1645: protected spans, apostrophes, newlines, comedic `（）`."""

    def test_protects_inline_code_books_brackets_quotes_and_urls(self):
        # Inline code `a || b` must NOT be split, while outer `||` splits cleanly
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("用法是 `a || b` 很常见"),
            ["用法是 `a || b` 很常见"],
        )
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("用法是 `a || b` || 你先照这个试试看"),
            ["用法是 `a || b`", "你先照这个试试看"],
        )
        # Book titles, CJK brackets, quotes, and URLs containing `||` stay intact
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("我最近在看《书名||副标题》这本小说"),
            ["我最近在看《书名||副标题》这本小说"],
        )
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("先看【注意||重点】再决定吧"),
            ["先看【注意||重点】再决定吧"],
        )
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("她说「别乱||切分」你就别动"),
            ["她说「别乱||切分」你就别动"],
        )
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("点这个链接 https://a.com/x||y 自己看"),
            ["点这个链接 https://a.com/x||y 自己看"],
        )
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("https://a.com/x||y || 自己点进去看吧"),
            ["https://a.com/x||y", "自己点进去看吧"],
        )
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("'keep || together' || 第二句也挺长的吧"),
            ["'keep || together'", "第二句也挺长的吧"],
        )
        # Disabling split must still preserve `||` inside inline code
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("写法 `a || b` 别改", allow_split=False),
            ["写法 `a || b` 别改"],
        )

    def test_ignores_english_apostrophes(self):
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("character's story || 这段剧情太离谱了"),
            ["character's story", "这段剧情太离谱了"],
        )
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("don't worry || it's totally fine"),
            ["don't worry", "it's totally fine"],
        )

    def test_explicit_newline_and_protected_newline(self):
        # Unprotected 2-line explicit newline splits cleanly
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("今天加班真是累死我了\n一会去吃顿火锅吧"),
            ["今天加班真是累死我了", "一会去吃顿火锅吧"],
        )
        # Newline inside protected CJK quotes must NOT split
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("「第一行引文内容挺长的\n第二行引文也不要拆」"),
            ["「第一行引文内容挺长的\n第二行引文也不要拆」"],
        )

    def test_comedic_empty_parens_split_keeps_parens_at_bubble1_end(self):
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("我才没有偷吃你的宵夜（）就是帮你试了下毒"),
            ["我才没有偷吃你的宵夜（）", "就是帮你试了下毒"],
        )
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("确实很厉害（）虽然我也行"),
            ["确实很厉害（）", "虽然我也行"],
        )
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("我才没偷吃（），顺手试毒而已"),
            ["我才没偷吃（）", "顺手试毒而已"],
        )
        # Consecutive multi-bubble gate still merges when last_was_multi=True
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("确实很厉害（）虽然我也行", last_was_multi=True),
            ["确实很厉害（） 虽然我也行"],
        )
        # ASCII function call `main()` and `（）` inside quotes must NOT split
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("先调用 main() 然后再看日志输出"),
            ["先调用 main() 然后再看日志输出"],
        )
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("「确实很厉害（）真的假的」"),
            ["「确实很厉害（）真的假的」"],
        )

    def test_fenced_code_block_preserves_pipe_operator(self):
        snippet = "```js\nconst x = a || b;\n```"
        self.assertEqual(wl.WhaleListener._split_reply_bubbles(snippet), [snippet])


class MusicProxyRedactionAndEdgeTtsTests(unittest.IsolatedAsyncioTestCase):
    """SuInk/Diana #1617, #1641: edge-tts pin, QQ-only proxy, error redaction, code=2001 hint."""

    def test_requirements_pins_edge_tts_7_3_1(self):
        req_text = (ROOT_DIR / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("edge-tts>=7.3.1,<8.0.0", req_text)

    def test_resolve_qq_music_proxy_validation(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QQ_MUSIC_PROXY", None)
            os.environ.pop("MUSIC_PROXY", None)
            self.assertIsNone(df._resolve_qq_music_proxy())
            self.assertEqual(
                df._resolve_qq_music_proxy("http://127.0.0.1:7890"),
                "http://127.0.0.1:7890",
            )
            self.assertEqual(
                df._resolve_qq_music_proxy("socks5h://user:pass@127.0.0.1:1080"),
                "socks5h://user:pass@127.0.0.1:1080",
            )
            # Invalid schemes / missing host / whitespace / invalid port must return None safely
            for bad in ("ftp://127.0.0.1:21", "not-a-url", "http://", "http://127.0.0.1:99999", "http://a b:8080"):
                with self.subTest(bad=bad):
                    self.assertIsNone(df._resolve_qq_music_proxy(bad))

        # Env precedence: QQ_MUSIC_PROXY > MUSIC_PROXY
        with patch.dict(
            os.environ,
            {"QQ_MUSIC_PROXY": "http://10.0.0.1:8080", "MUSIC_PROXY": "http://10.0.0.2:8080"},
            clear=False,
        ):
            self.assertEqual(df._resolve_qq_music_proxy(), "http://10.0.0.1:8080")
        with patch.dict(os.environ, {"QQ_MUSIC_PROXY": "", "MUSIC_PROXY": "socks5://127.0.0.1:1080"}, clear=False):
            self.assertEqual(df._resolve_qq_music_proxy(), "socks5://127.0.0.1:1080")

    def test_sanitize_music_error_redacts_secrets_and_urls(self):
        raw_exc = RuntimeError(
            "GET https://u.y.qq.com/cgi-bin/musicu.fcg?vkey=VKEY_SECRET&uin=12345678 "
            "via http://alice:s3cr3t@proxy.internal:8080 failed "
            "(qqmusic_key=QM_TOKEN_99 MUSIC_U=NE_SECRET_88)"
        )
        sanitized = df._sanitize_music_error(raw_exc)
        self.assertTrue(sanitized.startswith("RuntimeError:"))
        for secret in ("VKEY_SECRET", "12345678", "alice", "s3cr3t", "QM_TOKEN_99", "NE_SECRET_88", "u.y.qq.com"):
            self.assertNotIn(secret, sanitized)

    async def test_search_and_fetch_music_scopes_proxy_to_qq_only_and_warns_on_2001(self):
        client_calls: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if host == "music.163.com":
                return httpx.Response(200, json={"result": {"songs": []}})
            if host == "u.y.qq.com":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "req": {
                            "code": 2001,
                            "data": {
                                "body": {
                                    "song": {
                                        "list": [
                                            {
                                                "mid": "0039MnYb0qxYhV",
                                                "name": "晴天",
                                                "singer": [{"name": "周杰伦"}],
                                                "album": {"mid": "000MkMni19ClKG", "name": "叶惠美"},
                                            }
                                        ]
                                    }
                                }
                            },
                        },
                    },
                )
            return httpx.Response(404)

        def fake_async_client(**kw):
            client_calls.append(dict(kw))
            kw.pop("proxy", None)
            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw)

        with (
            patch.dict(os.environ, {"QQ_MUSIC_PROXY": "http://127.0.0.1:7890"}, clear=False),
            patch.object(httpx, "AsyncClient", side_effect=fake_async_client),
            patch.object(df, "_is_safe_public_url", return_value=True),
            self.assertLogs(mp.logger, level="WARNING") as logs,
        ):
            song = await df.search_and_fetch_music("晴天", qq_cookie="uin=99999; qqmusic_key=SUPER_SECRET_COOKIE")

        self.assertIsNotNone(song)
        self.assertEqual(song["source"], "QQ音乐")
        self.assertEqual(song["title"], "晴天")

        # 2 client instantiations: NetEase direct (no proxy), QQ Music with proxy
        self.assertEqual(len(client_calls), 2)
        self.assertNotIn("proxy", client_calls[0])
        self.assertEqual(client_calls[1].get("proxy"), "http://127.0.0.1:7890")
        self.assertFalse(client_calls[1].get("follow_redirects"))

        # Warning emitted for code=2001 without leaking raw cookie value
        joined_logs = "\n".join(logs.output)
        self.assertIn("code=2001", joined_logs)
        self.assertNotIn("SUPER_SECRET_COOKIE", joined_logs)

    async def test_invalid_qq_proxy_does_not_break_netease_or_direct_qq(self):
        client_calls: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "music.163.com" and request.url.path == "/api/cloudsearch/pc":
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "songs": [
                                {
                                    "id": 186016,
                                    "name": "晴天",
                                    "ar": [{"name": "周杰伦"}],
                                    "al": {"name": "叶惠美", "picUrl": "https://p1.music.126.net/a.jpg"},
                                }
                            ]
                        }
                    },
                )
            if request.url.host == "music.163.com":
                return httpx.Response(200, content=b"M" * 50_000, headers={"content-type": "audio/mpeg"})
            return httpx.Response(404)

        def fake_async_client(**kw):
            client_calls.append(dict(kw))
            kw.pop("proxy", None)
            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw)

        with (
            patch.dict(os.environ, {"QQ_MUSIC_PROXY": "ftp://broken-proxy"}, clear=False),
            patch.object(httpx, "AsyncClient", side_effect=fake_async_client),
            patch.object(df, "_is_safe_public_url", return_value=True),
        ):
            song = await df.search_and_fetch_music("晴天")

        self.assertIsNotNone(song)
        self.assertEqual(song["source"], "网易云音乐")
        self.assertEqual(len(client_calls), 1)
        self.assertNotIn("proxy", client_calls[0])


if __name__ == "__main__":
    unittest.main()
