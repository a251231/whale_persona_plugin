"""Regression tests for Nov 27 upstream sync (v0.5.5):
- SuInk/Diana #1674, #1677: unified Chinese-unit engagement stats formatter (`万`/`亿`) with
  malformed-field tolerance, placed strictly on the last line of Bilibili / X / Xiaohongshu /
  Douyin / YouTube parsed cards
- SuInk/Diana #1658: trailing laughter tone-mark bubble splitting (`ww`/`hhh`/`233+`) alongside
  comedic empty parens (`（）`/`()`), skipping English words (`aww`/`Ohh`), URLs (`www.`),
  numbers (`1233`/`2333.5`), and protected spans
- Yuuyuko-uu/dsh-fish-memory v1.6.7: never normalize user name into `你` on memory write
  (preventing subject inversion against the injected `你` model header)
- SuInk/Diana #1659, #1654: memory-error tool-first correction rule + fenced code blocks allowed
  in WeChat plaintext replies
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from components.event_listeners import diana_features as df  # noqa: E402
from components.event_listeners import whale_listener as wl  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class LinkResolverStatsTests(unittest.IsolatedAsyncioTestCase):
    """SuInk/Diana #1674, #1677: lenient Chinese-unit engagement stats placed at the bottom."""

    def test_format_resolver_count_chinese_units_and_lenient_inputs(self):
        cases = [
            (0, ""),
            (-5, ""),
            (9999, "9999"),
            (10000, "1万"),
            (123456, "12.3万"),
            (99999999, "9999.9万"),
            (100000000, "1亿"),
            (12345678901, "123.4亿"),
            ("1.2万", "1.2万"),
            ("100万+", "100万+"),
            ("3456", "3456"),
            ("12,345", "1.2万"),
            ("0", ""),
            ("-12", ""),
            (None, ""),
            (True, ""),
            ([1, 2], ""),
            ({"count": 3}, ""),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(df.format_resolver_count(raw), want)

        line = df.format_resolver_stats_line(
            [
                ("播放", 123456),
                ("点赞", "1.2万"),
                ("投币", [1, 2]),
                ("收藏", 0),
                ("评论", 78),
            ]
        )
        self.assertEqual(line, "播放 12.3万 · 点赞 1.2万 · 评论 78")

    async def test_bilibili_video_places_formatted_stats_on_last_line(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "api.bilibili.com/x/web-interface/view" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "bvid": "BV1Ab4y1z7Kp",
                            "title": "深海鲸鱼实测",
                            "owner": {"name": "大肥鱼UP"},
                            "stat": {
                                "view": 123456,
                                "like": 3456,
                                "coin": 890,
                                "favorite": 11200,
                                "danmaku": 50,
                            },
                            "desc": "这是视频简介内容",
                            "pic": "",
                        },
                    },
                )
            return httpx.Response(404)

        with patch.object(df, "_is_safe_public_url", return_value=True), patch.object(
            df.httpx,
            "AsyncClient",
            side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw),
        ):
            card, _ = await df._parse_bilibili_url("https://www.bilibili.com/video/BV1Ab4y1z7Kp")

        self.assertIsNotNone(card)
        lines = card.splitlines()
        self.assertEqual(lines[0], "📺 【B站视频解析】深海鲸鱼实测")
        self.assertEqual(lines[1], "👤 UP主：大肥鱼UP")
        self.assertIn("📝 简介：这是视频简介内容", lines)
        self.assertEqual(lines[-1], "📊 播放 12.3万 · 点赞 3456 · 投币 890 · 收藏 1.1万 · 弹幕 50")

    async def test_twitter_tweet_and_profile_place_formatted_stats_on_last_line(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/status/" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "tweet": {
                            "author": {"name": "Alice", "screen_name": "alice_dev", "avatar_url": ""},
                            "text": "这是一条测试推文正文",
                            "likes": 12500,
                            "retweets": 3456,
                            "replies": 89,
                            "bookmarks": 420,
                            "views": 1234567,
                            "media": {"all": []},
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "user": {
                        "name": "Alice",
                        "screen_name": "alice_dev",
                        "description": "开源作者",
                        "followers": 250000,
                        "following": 120,
                        "tweets": 4321,
                        "avatar_url": "",
                    }
                },
            )

        with patch.object(df, "_is_safe_public_url", return_value=True), patch.object(
            df.httpx,
            "AsyncClient",
            side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw),
        ):
            tweet_card, _ = await df._parse_twitter_url("https://x.com/alice_dev/status/1234567890")
            prof_card, _ = await df._parse_twitter_url("https://x.com/alice_dev")

        self.assertIsNotNone(tweet_card)
        t_lines = tweet_card.splitlines()
        self.assertIn("这是一条测试推文正文", tweet_card)
        self.assertEqual(t_lines[-1], "📊 点赞 1.2万 · 转推 3456 · 评论 89 · 收藏 420 · 浏览 123.4万")

        self.assertIsNotNone(prof_card)
        p_lines = prof_card.splitlines()
        self.assertEqual(p_lines[-1], "📊 粉丝 25万 · 关注 120 · 推文 4321")

    def test_xiaohongshu_and_douyin_social_html_append_bottom_stats_and_tolerate_malformed(self):
        state_ok = {
            "note": {
                "noteDetailMap": {
                    "abc123": {
                        "note": {
                            "title": "探店笔记",
                            "desc": "味道不错推荐打卡",
                            "user": {"nickname": "测试作者"},
                            "interactInfo": {
                                "likedCount": "1.2万",
                                "collectedCount": "3456",
                                "commentCount": 78,
                                "shareCount": "0",
                            },
                            "imageList": [],
                        }
                    }
                }
            }
        }
        html_ok = f"<script>window.__INITIAL_STATE__={json.dumps(state_ok, ensure_ascii=False)};</script>"
        card_ok, _ = df._parse_social_html_sync(html_ok, "https://www.xiaohongshu.com/explore/abc123", "xiaohongshu.com")
        self.assertIsNotNone(card_ok)
        lines_ok = card_ok.splitlines()
        self.assertEqual(lines_ok[-1], "📊 点赞 1.2万 · 收藏 3456 · 评论 78")

        # Malformed interactInfo fields must never crash or drop title/desc
        state_bad = {
            "note": {
                "noteDetailMap": {
                    "abc123": {
                        "note": {
                            "title": "容错笔记",
                            "desc": "正文完整保留",
                            "user": {"nickname": "作者B"},
                            "interactInfo": {"likedCount": [1, 2], "collectedCount": {"x": 1}},
                            "imageList": [],
                        }
                    }
                }
            }
        }
        html_bad = f"<script>window.__INITIAL_STATE__={json.dumps(state_bad, ensure_ascii=False)};</script>"
        card_bad, _ = df._parse_social_html_sync(
            html_bad, "https://www.xiaohongshu.com/explore/abc123", "xiaohongshu.com"
        )
        self.assertIsNotNone(card_bad)
        self.assertIn("容错笔记", card_bad)
        self.assertIn("正文完整保留", card_bad)
        self.assertNotIn("📊", card_bad)

        # Douyin HTML embedded counters placed at the bottom
        dy_html = (
            '<html><head><meta property="og:title" content="抖音热梗视频">'
            '<meta property="og:description" content="一段搞笑合集"></head>'
            '<body><script>{"play_count": 560000, "digg_count": 25400, "comment_count": 320}</script></body></html>'
        )
        dy_card, _ = df._parse_social_html_sync(dy_html, "https://www.douyin.com/video/7123456", "douyin.com")
        self.assertIsNotNone(dy_card)
        self.assertEqual(dy_card.splitlines()[-1], "📊 播放 56万 · 点赞 2.5万 · 评论 320")

    async def test_youtube_appends_optional_bottom_stats_when_present(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "title": "Demo Video",
                    "author_name": "DevChannel",
                    "thumbnail_url": "",
                    "view_count": 1500000,
                    "like_count": 48000,
                },
            )

        with patch.object(df, "_is_safe_public_url", return_value=True), patch.object(
            df.httpx,
            "AsyncClient",
            side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw),
        ):
            yt_card, _ = await df._parse_youtube_or_social_url("https://youtu.be/dQw4w9WgXcQ")

        self.assertIsNotNone(yt_card)
        self.assertEqual(yt_card.splitlines()[-1], "📊 播放 150万 · 点赞 4.8万")


class LaughterToneMarkBubbleSplitTests(unittest.TestCase):
    """SuInk/Diana #1658: trailing laughter (`ww`/`hhh`/`233+`) split keeping laughter in bubble 1."""

    def test_splits_after_ww_hhh_233_keeping_laughter_in_first_bubble(self):
        cases = [
            (
                "你说的那个朋友是不是你自己 ww 还免费洗涤服务，你这算盘珠子崩我脸上了",
                ["你说的那个朋友是不是你自己 ww", "还免费洗涤服务，你这算盘珠子崩我脸上了"],
            ),
            (
                "是摩纳哥不是摩洛哥 ww 一字之差",
                ["是摩纳哥不是摩洛哥 ww", "一字之差"],
            ),
            (
                "自己留 issue 求助的类型ww 窝也是",
                ["自己留 issue 求助的类型ww", "窝也是"],
            ),
            (
                "笑死hhh你也太菜了",
                ["笑死hhh", "你也太菜了"],
            ),
            (
                "2333 这也太离谱了吧",
                ["2333", "这也太离谱了吧"],
            ),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(wl.WhaleListener._split_reply_bubbles(raw), want)

    def test_skips_english_words_urls_numbers_protected_spans_trailing_laughter_and_consecutive_multi(self):
        no_split_cases = [
            "aww 好可爱呀真不错",
            "Ohh 原来是这样啊",
            "去 www.example.com 看看吧",
            "一共花了1233元挺贵的",
            "大概2333.5块左右吧",
            "结尾的笑不用拆 ww",
            "「笑死hhh你也太菜了」",
            "代码里写 `ww 123` 别拆",
            "链接 https://www.example.com/ww 别动",
        ]
        for raw in no_split_cases:
            with self.subTest(raw=raw):
                self.assertEqual(wl.WhaleListener._split_reply_bubbles(raw), [raw])

        # Consecutive multi-bubble rhythm gate still merges when last_was_multi=True
        self.assertEqual(
            wl.WhaleListener._split_reply_bubbles("笑死hhh你也太菜了", last_was_multi=True),
            ["笑死hhh 你也太菜了"],
        )


class MemoryPronounAndPromptGuardTests(unittest.IsolatedAsyncioTestCase):
    """dsh-fish-memory v1.6.7 + SuInk/Diana #1659/#1654."""

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

    def tearDown(self):
        try:
            if getattr(self.listener, "_save_debounce_handle", None):
                self.listener._save_debounce_handle.cancel()
        except Exception:
            pass
        for p in reversed(self._patches):
            p.stop()
        self._tmpdir.cleanup()

    def test_normalize_memory_owner_pronoun_never_writes_ni_and_preserves_quotes_and_naming(self):
        # Replaces unquoted user nickname with third-person `对方`, NEVER `你`
        self.assertEqual(
            df.normalize_memory_owner_pronoun("由 gunsteel 自己维护", "gunsteel"),
            "由对方自己维护",
        )
        # Even if caller explicitly passes `你`, coerce to `对方` so injected memory never inverts subject
        self.assertEqual(
            df.normalize_memory_owner_pronoun("由 gunsteel 自己维护", "gunsteel", pronoun="你"),
            "由对方自己维护",
        )
        # Naming facts & quoted literal names stay intact
        self.assertEqual(
            df.normalize_memory_owner_pronoun("对方希望被叫 gunsteel", "gunsteel"),
            "对方希望被叫 gunsteel",
        )
        self.assertEqual(
            df.normalize_memory_owner_pronoun("备注写着「gunsteel」项目", "gunsteel"),
            "备注写着「gunsteel」项目",
        )
        # ASCII word boundary prevents corrupting substrings
        self.assertEqual(
            df.normalize_memory_owner_pronoun("gunsteel2 负责测试", "gunsteel"),
            "gunsteel2 负责测试",
        )

    async def test_post_turn_judge_normalizes_nickname_to_third_person_on_write(self):
        u = self.listener._get_user_state("wxid_pronoun")
        u["nickname"] = "gunsteel"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "moodDelta": 1,
                                        "subject": "owner",
                                        "dims": {"happy": 1},
                                        "reason": "对方分享了维护流程",
                                        "confidence": 90,
                                        "memory_fact": "该脚本长期由 gunsteel 自己维护",
                                        "memory_event": "gunsteel 今天完成了版本升级",
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ],
                    "usage": {"total_tokens": 15},
                },
            )

        with patch.object(
            httpx,
            "AsyncClient",
            side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw),
        ):
            await self.listener._async_post_turn_judge(
                api_key="k-test",
                base_url="https://api.deepseek.com",
                model="deepseek-flash",
                user_id="wxid_pronoun",
                user_text="这个脚本一直我自己维护的",
                reply_text="挺厉害呀",
                expected_state=u,
            )

        self.assertIn("该脚本长期由对方自己维护", u["facts"])
        self.assertTrue(any("对方今天完成了版本升级" in str(ev.get("text") or "") for ev in u["events"]))
        for f_txt in u["facts"]:
            self.assertNotIn("由你自己维护", f_txt)

    def test_prompts_include_memory_correction_pronoun_and_code_fence_rules(self):
        # dsh-fish-memory v1.6.7 + gender-neutral guard preserved
        self.assertIn("严禁把用户写成「你」", wl.MOOD_JUDGE_SYSTEM)
        self.assertNotIn("她", wl.MOOD_JUDGE_SYSTEM)
        self.assertIn("严禁用「你」指代用户", wl.ARCHIVE_CONSOLIDATE_SYSTEM)

        # Diana #1659 (memory error -> tool-first) & #1654 (code fences allowed)
        for persona_text in (wl.LIVE_PERSONA_BASE, wl.SWEET_PERSONA_BASE):
            self.assertIn("我资料里写着", persona_text)
            self.assertIn("围栏代码块", persona_text)


if __name__ == "__main__":
    unittest.main()
