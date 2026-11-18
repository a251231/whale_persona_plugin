"""Regression tests for Nov 18 upstream sync (v0.5.3):
- Yuuyuko-uu/dsh-feiyu-mood v0.9.4..v0.9.6: 4-way emotional protagonist `subject` gate + gender-neutral prompt
- Yuuyuko-uu/dsh-fish-memory v1.6.2..v1.6.3: proper-noun clues, multi-level recall tie-breaking,
  current-turn-only long-term fact gate during short-query context borrowing, and 15-turn non-repetition window
- SuInk/Diana #1584, #1585, #1589: Markdown-safe URL_REGEX, (BV/av, p) link dedup, multi-link resolving (max 3),
  FxTwitter repost + quote formatting, and pre-output honesty & unit-check rules
"""
from __future__ import annotations

import asyncio
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
        self.listener.plugin = type(
            "_P", (), {"config": {"deepseek_api_key": "k-test", "deepseek_model": "deepseek-flash", "owner_ids": "wxid_test"}}
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


class MoodSubjectGateTests(_MockBase):
    """dsh-feiyu-mood v0.9.4..v0.9.6: neutral subject classification & clamping."""

    def test_mood_judge_prompt_schema_and_neutral_wording(self):
        self.assertIn('"subject":"owner"', wl.MOOD_JUDGE_SYSTEM)
        self.assertEqual(wl.MOOD_JUDGE_SYSTEM, wl.MOOD_JUDGE_SYSTEM_PROMPT)
        self.assertIn("other", wl.MOOD_JUDGE_SYSTEM)
        self.assertIn("none", wl.MOOD_JUDGE_SYSTEM)
        # Ensure judge prompt never hardcodes "她" for the user
        self.assertNotIn("她", wl.MOOD_JUDGE_SYSTEM)

    def test_normalize_mood_judge_delta_clamps_other_and_none(self):
        for raw_subj in ("other", "别人", "其他人", "none", "闲聊"):
            with self.subTest(raw_subj=raw_subj):
                out = wl.normalize_mood_judge_delta(
                    {
                        "subject": raw_subj,
                        "moodDelta": -6,
                        "dims": {
                            "happy": 5,
                            "calm": -2,
                            "sweet": 4,
                            "heartache": 7,
                            "worry": 6,
                            "sad": 5,
                            "jealous": 8,
                            "angry": 9,
                        },
                    }
                )
                self.assertIn(out["subject"], ("other", "none"))
                self.assertEqual(out["moodDelta"], 0)
                self.assertEqual(out["dims"]["happy"], 1)
                self.assertEqual(out["dims"]["calm"], 0)
                for zero_dim in ("sweet", "heartache", "worry", "sad", "jealous", "angry"):
                    self.assertEqual(out["dims"][zero_dim], 0)

    def test_normalize_mood_judge_delta_preserves_owner_and_self_and_missing(self):
        for raw_subj in ("owner", "self", "对方", "自己", "", None, "unknown_val"):
            with self.subTest(raw_subj=raw_subj):
                out = wl.normalize_mood_judge_delta(
                    {
                        "subject": raw_subj,
                        "moodDelta": -5,
                        "dims": {"heartache": 6, "worry": 4, "angry": 3},
                    }
                )
                expected_subj = "self" if raw_subj in ("self", "自己") else "owner"
                self.assertEqual(out["subject"], expected_subj)
                self.assertEqual(out["moodDelta"], -5)
                self.assertEqual(out["dims"]["heartache"], 6)
                self.assertEqual(out["dims"]["worry"], 4)
                self.assertEqual(out["dims"]["angry"], 3)

    async def test_post_turn_judge_skips_mood_shift_and_reason_for_third_party_story(self):
        u = self.listener._get_user_state("wxid_subj")
        u["mood"] = 72
        u["lastReason"] = "ORIGINAL-REASON"
        u["dims"]["heartache"] = 2
        u["dims"]["jealous"] = 1
        before_happy = int(u["dims"].get("happy", 0))

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "moodDelta": -6,
                                        "subject": "other",
                                        "dims": {"happy": 3, "heartache": 7, "jealous": 8, "sad": 5},
                                        "reason": "同事A被比下去了",
                                        "confidence": 92,
                                        "memory_fact": "",
                                        "memory_event": "",
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ],
                    "usage": {"total_tokens": 12},
                },
            )

        with patch.object(
            httpx,
            "AsyncClient",
            side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw),
        ):
            await self.listener._async_post_turn_judge(
                api_key="k",
                base_url="https://api.deepseek.com",
                model="deepseek-flash",
                user_id="wxid_subj",
                user_text="我同事A今天被说不如同事B",
                reply_text="别人的事听听就算了",
                expected_state=u,
            )

        self.assertEqual(u["mood"], 72)
        self.assertEqual(u["dims"]["heartache"], 2)
        self.assertEqual(u["dims"]["jealous"], 1)
        self.assertEqual(u["dims"]["happy"], before_happy + 1)
        self.assertEqual(u["lastReason"], "ORIGINAL-REASON")
        self.assertEqual(u["timeline"], [])


class MemoryCluesAndDedupTests(_MockBase):
    """dsh-fish-memory v1.6.2..v1.6.3: proper nouns, tie-breaking, fact gate, and 15-turn dedup."""

    def test_proper_noun_and_name_prefix_clues_without_single_char_noise(self):
        # Name prefix clue (小糯) matches even when 小 is in MEMORY_STOP_CHARS
        score_name, hit_n, hit_len = df.compute_memory_query_metrics("小糯最近还好吗", "对方养的小猫叫小糯")
        self.assertGreater(score_name, 0.0)
        self.assertGreaterEqual(hit_n, 1)
        self.assertGreaterEqual(hit_len, 2)

        # 3-4 char window proper term clue
        score_term, hit_n2, hit_len2 = df.compute_memory_query_metrics("瑞幸生椰拿铁好喝吗", "对方喜欢买瑞幸生椰")
        self.assertGreater(score_term, 0.0)
        self.assertGreaterEqual(hit_n2, 2)
        self.assertGreaterEqual(hit_len2, 4)

        # Single-char coincidence in multi-char query must still return 0
        self.assertEqual(df.compute_memory_query_overlap("我昨天问小鱼去哪里", "对方去市场买了条小鱼"), 0.0)
        self.assertEqual(df.compute_memory_query_overlap("我今天在公司加班了", "对方昨天在家里睡了一整天"), 0.0)

    def test_recall_tie_breaking_prefers_more_and_longer_clue_hits(self):
        now_ms = time.time() * 1000.0
        # 5 matching events while slice cap is 4 -> E_single (newer, 1 clue) drops behind multi-clue events!
        u = {
            "facts": [],
            "events": [
                {"date": "2026-11-01", "ts": now_ms - 10 * 86400000, "text": "周末备份脚本代码库迁移测试"},
                {"date": "2026-11-02", "ts": now_ms - 9 * 86400000, "text": "周末备份脚本代码库归档"},
                {"date": "2026-11-03", "ts": now_ms - 8 * 86400000, "text": "周末备份脚本代码库检查"},
                {"date": "2026-11-04", "ts": now_ms - 7 * 86400000, "text": "周末备份脚本代码库校验"},
                {"date": "2026-11-17", "ts": now_ms, "text": "随手记了一次备份"},
            ],
            "archive": {},
        }
        recalled = self.listener._recall_memories(u, "周末备份脚本代码库怎么办")
        self.assertIn("迁移测试", recalled)
        self.assertIn("归档", recalled)
        self.assertIn("检查", recalled)
        self.assertIn("校验", recalled)
        self.assertNotIn("随手记了一次备份", recalled)

    def test_short_query_context_borrowing_does_not_drag_unrelated_facts(self):
        now_ms = time.time() * 1000.0
        u = {
            "stable_profile": [{"text": "对方对海底捞番茄锅底有执念", "category": "stable_profile"}],
            "facts": ["用户周末经常去吃火锅", "用户爱吃加辣螺蛳粉"],
            "events": [
                {"date": "2026-11-17", "ts": now_ms, "text": "昨天晚上约了朋友去吃火锅"},
            ],
            "archive": {"三": ["对方一到周末就想吃火锅"]},
        }
        recalled = self.listener._recall_memories(
            u,
            "昨晚去吃火锅了 好辣",
            current_turn_text="好辣",
            session_id="person:wxid_short",
        )
        self.assertIn("约了朋友去吃火锅", recalled)
        self.assertNotIn("周末经常去吃火锅", recalled)
        self.assertNotIn("对海底捞番茄锅底有执念", recalled)
        self.assertNotIn("一到周末就想吃火锅", recalled)

    def test_repeat_window_blocks_consecutive_re_injection_and_flushes_on_update(self):
        now_ms = time.time() * 1000.0
        u = {
            "facts": [],
            "events": [
                {"date": f"2026-11-0{i}", "ts": now_ms - (10 - i) * 86400000, "text": f"记录事件{i} 瑞幸拿铁咖啡打卡"}
                for i in range(1, 7)
            ],
            "archive": {},
        }
        sid = "person:wxid_repeat"
        first = self.listener._recall_memories(
            u, "我想喝瑞幸拿铁咖啡", current_turn_text="我想喝瑞幸拿铁咖啡", session_id=sid, repeat_window=15
        )
        # Same turn signature repeated (e.g. multiple calls per turn) must NOT advance turns or drop its own pick
        same_turn = self.listener._recall_memories(
            u, "我想喝瑞幸拿铁咖啡", current_turn_text="我想喝瑞幸拿铁咖啡", session_id=sid, repeat_window=15
        )
        self.assertEqual(first, same_turn)

        # Next distinct user message skips the 4 events already shown on Turn 1 and promotes events 1..2 into slots
        second = self.listener._recall_memories(
            u, "再来一杯瑞幸拿铁咖啡吧", current_turn_text="再来一杯瑞幸拿铁咖啡吧", session_id=sid, repeat_window=15
        )
        # Events 6,5,4,3 were newer so shown on Turn 1; Turn 2 should surface events 2 & 1
        self.assertIn("记录事件1", second)
        self.assertIn("记录事件2", second)
        self.assertNotIn("记录事件6", second)

        # Flushing cache allows events 6..3 back immediately
        self.listener._flush_memory_recall_cache("wxid_repeat")
        after_flush = self.listener._recall_memories(
            u, "第三次点瑞幸拿铁咖啡", current_turn_text="第三次点瑞幸拿铁咖啡", session_id=sid, repeat_window=15
        )
        self.assertIn("记录事件6", after_flush)

    async def test_remember_and_forget_commands_flush_recall_cache(self):
        self.listener._resolve_bot_uuid = AsyncMock(return_value="bot-A")
        self.listener._save_persistent_state = lambda: None
        u = self.listener._get_user_state("wxid_test")
        u["facts"] = ["用户喜欢蓝山咖啡"]
        # Prime Turn 1 injection for "蓝山咖啡"
        r1 = self.listener._recall_memories(
            u, "聊聊蓝山咖啡", current_turn_text="聊聊蓝山咖啡", session_id="person:wxid_test"
        )
        self.assertIn("用户喜欢蓝山咖啡", r1)
        self.assertIn("person:wxid_test", self.listener._recent_memory_injections)

        def _mk_ctx(text: str):
            async def _reply_mc(*args, **kwargs):
                return None

            return SimpleNamespace(
                event=SimpleNamespace(
                    message_chain=MessageChain([Plain(text=text)]),
                    sender_id="wxid_test",
                    launcher_id="wxid_test",
                    source_platform_object={},
                    reply_message_chain=None,
                ),
                reply=_reply_mc,
                query_id=None,
                is_prevented_default=lambda: False,
                prevent_default=lambda: None,
            )

        with patch.object(df, "resolve_user_display_name", AsyncMock(return_value="测试用户")):
            # Executing 记住 flushes _recent_memory_injections for wxid_test
            await self.listener._handle_chat(_mk_ctx("小鲸鱼 记住 用户还喜欢哥伦比亚咖啡"), is_group=False)
            self.assertNotIn("person:wxid_test", self.listener._recent_memory_injections)

            # Re-prime cache, then verify 忘掉 also flushes it immediately
            r2 = self.listener._recall_memories(
                u, "再聊聊蓝山咖啡", current_turn_text="再聊聊蓝山咖啡", session_id="person:wxid_test"
            )
            self.assertIn("用户喜欢蓝山咖啡", r2)
            self.assertIn("person:wxid_test", self.listener._recent_memory_injections)

            await self.listener._handle_chat(_mk_ctx("大肥鱼 忘掉 蓝山咖啡"), is_group=False)
            self.assertNotIn("person:wxid_test", self.listener._recent_memory_injections)


class DianaLinkAndOutputCheckTests(_MockBase):
    """SuInk/Diana #1584, #1585, #1589 regressions."""

    def test_url_regex_does_not_glue_markdown_links(self):
        md = "[点击看视频](https://www.bilibili.com/video/BV1xx411c7mD?t=12) 还有笔记"
        matches = [m.group(0) for m in df.URL_REGEX.finditer(md)]
        self.assertEqual(matches, ["https://www.bilibili.com/video/BV1xx411c7mD?t=12"])

        md2 = "[https://www.bilibili.com/video/BV1xx411c7mD](https://www.bilibili.com/video/BV1xx411c7mD)"
        matches2 = [m.group(0) for m in df.URL_REGEX.finditer(md2)]
        self.assertEqual(
            matches2,
            ["https://www.bilibili.com/video/BV1xx411c7mD", "https://www.bilibili.com/video/BV1xx411c7mD"],
        )

    def test_resolver_url_dedupe_key_normalizes_bilibili_timestamps_and_pages(self):
        k1 = df.resolver_url_dedupe_key("https://www.bilibili.com/video/BV1Ab4y1z7Kp/?t=15&vd_source=abc#reply")
        k2 = df.resolver_url_dedupe_key("https://b23.tv/other")
        k3 = df.resolver_url_dedupe_key("https://www.bilibili.com/video/BV1Ab4y1z7Kp?p=1&t=120")
        k4 = df.resolver_url_dedupe_key("https://www.bilibili.com/video/BV1Ab4y1z7Kp?p=2&t=30")
        self.assertEqual(k1, "bilibili:BV1Ab4y1z7Kp:p1")
        self.assertEqual(k1, k3)
        self.assertEqual(k4, "bilibili:BV1Ab4y1z7Kp:p2")
        self.assertEqual(k2, "https://b23.tv/other")

    async def test_bilibili_timestamp_links_deduped_and_multi_part_resolved(self):
        api_calls: list[str] = []

        def mock_handler(request: httpx.Request) -> httpx.Response:
            api_calls.append(str(request.url))
            if "api.bilibili.com/x/web-interface/view" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "bvid": "BV1Ab4y1z7Kp",
                            "title": "教程合集",
                            "owner": {"name": "大肥鱼课堂"},
                            "stat": {"view": 1000, "danmaku": 50, "like": 88},
                            "desc": "分P讲解",
                            "pic": "",
                            "pages": [
                                {"page": 1, "part": "开篇"},
                                {"page": 2, "part": "进阶实战"},
                            ],
                        },
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)
        msg = (
            "片段1 [link](https://www.bilibili.com/video/BV1Ab4y1z7Kp?t=10)\n"
            "片段2 https://www.bilibili.com/video/BV1Ab4y1z7Kp?t=45\n"
            "片段3 https://www.bilibili.com/video/BV1Ab4y1z7Kp?p=1&t=90\n"
            "第二P https://www.bilibili.com/video/BV1Ab4y1z7Kp?p=2&t=5\n"
        )
        with patch.object(df, "_is_safe_public_url", return_value=True), patch.object(
            df.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw)
        ):
            card, _ = await df.resolve_any_supported_link(msg)

        self.assertIsNotNone(card)
        # Only 2 Bilibili API requests (once for p=1 across 3 timestamp variants, once for p=2)
        self.assertEqual(len(api_calls), 2)
        self.assertIn("教程合集", card)
        self.assertIn("P2 · 进阶实战", card)

    async def test_twitter_reposted_by_and_quote_annotations(self):
        def mock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "tweet": {
                        "author": {"name": "OrigAuthor", "screen_name": "orig_dev", "avatar_url": ""},
                        "reposted_by": {"screen_name": "sub_account"},
                        "text": "这里是原推正文第一行内容",
                        "quote": {"author": {"screen_name": "quoted_dev"}, "text": "被引用的观点内容"},
                        "likes": 42,
                        "retweets": 9,
                        "views": 1200,
                        "media": {"all": []},
                    }
                },
            )

        transport = httpx.MockTransport(mock_handler)
        with patch.object(df, "_is_safe_public_url", return_value=True), patch.object(
            df.httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw)
        ):
            card, _ = await df.extract_and_parse_link("https://x.com/sub_account/status/1234567890")

        self.assertIsNotNone(card)
        self.assertIn("转推自 @orig_dev（由 @sub_account 转推）", card)
        self.assertIn("引用 @quoted_dev：被引用的观点内容", card)
        self.assertEqual(card.count("这里是原推正文第一行内容"), 1)

    def test_persona_prompts_include_diana_1589_honesty_and_unit_rules(self):
        for prompt_text in (wl.LIVE_PERSONA_BASE, wl.SWEET_PERSONA_BASE):
            self.assertIn("我刚查过", prompt_text)
            self.assertIn("单位", prompt_text)


if __name__ == "__main__":
    unittest.main()
