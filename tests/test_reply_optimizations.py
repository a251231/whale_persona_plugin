"""回复质量与群聊/私聊上下文优化的单元测试套件。"""

import asyncio
import base64
import io
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image as PILImage
from langbot_plugin.api.entities.builtin.platform.message import At, Image, MessageChain, Plain

_REAL_ASYNC_CLIENT = httpx.AsyncClient

try:
    import test_review_regressions as harness
except ImportError:
    from tests import test_review_regressions as harness
from components.event_listeners import diana_features as df
from components.event_listeners import whale_listener as wl


class ReplyOptimizationsTests(harness.ListenerHarness):
    async def ask(
        self,
        text="",
        *,
        group=False,
        room="wxid_test",
        sender="wxid_test",
        image=None,
        extra_comps=None,
        source_platform_object=None,
        clear_requests=True,
    ):
        comps = list(extra_comps or [])
        if text:
            comps.append(Plain(text=text))
        if image:
            comps.append(Image(base64=image))
        ctx = SimpleNamespace(
            event=SimpleNamespace(
                message_chain=MessageChain(comps),
                sender_id=sender,
                launcher_id=room,
                source_platform_object=source_platform_object if source_platform_object is not None else {},
                reply_message_chain=None,
            ),
            query_id=None,
            is_prevented_default=lambda: False,
            prevent_default=lambda: None,
        )
        if clear_requests:
            self.requests.clear()
        await self.listener._handle_chat(ctx, is_group=group)
        await asyncio.sleep(0)
        return ctx

    # ------------------------------------------------------------------
    # 1. Group `@` Wake Validation & Dynamic Bot Name Support
    # ------------------------------------------------------------------
    async def test_group_at_other_member_does_not_wake_bot(self):
        self._patch(df, "evaluate_group_interject", AsyncMock(return_value=(False, "", "", 0)))

        ctx_plain_at = await self.ask("@张三 你看大肥鱼", group=True, room="room-A")
        self.assertFalse(self.requests)
        self.assertIsNone(ctx_plain_at.event.reply_message_chain)

        ctx_comp_at = await self.ask(
            "你看大肥鱼",
            group=True,
            room="room-A",
            extra_comps=[At(target="wxid_other", display="张三")],
        )
        self.assertFalse(self.requests)
        self.assertIsNone(ctx_comp_at.event.reply_message_chain)

    async def test_group_dynamic_bot_name_and_platform_is_at_wake_and_strip_prefix(self):
        self._patch(df, "get_dynamic_bot_names", return_value=("大肥鱼", "鲸鱼娘", "小鲸鱼", "free"))

        ctx_dyn = await self.ask("@Free 今天吃啥", group=True, room="room-A")
        self.assertTrue(self.requests)
        self.assertIsNotNone(ctx_dyn.event.reply_message_chain)
        user_msg = self.payload()["messages"][-1]["content"]
        self.assertIn("今天吃啥", user_msg)
        self.assertNotIn("@Free", user_msg)
        self.assertNotIn("@free", user_msg.lower())

        ctx_flag = await self.ask(
            "@某个群昵称 今天吃啥",
            group=True,
            room="room-A",
            source_platform_object={"is_at": True},
        )
        self.assertTrue(self.requests)
        self.assertIsNotNone(ctx_flag.event.reply_message_chain)
        user_msg_flag = self.payload()["messages"][-1]["content"]
        self.assertIn("今天吃啥", user_msg_flag)
        self.assertNotIn("@某个群昵称", user_msg_flag)

    # ------------------------------------------------------------------
    # 2. Group Interject History/Meme/Mode & Group Voice Follow-up
    # ------------------------------------------------------------------
    async def test_group_interject_passes_sweet_mode_and_records_history(self):
        await self.ask("鲸鱼娘 甜宠版", group=True, room="room-A")
        judge = self._patch(
            df,
            "evaluate_group_interject",
            AsyncMock(return_value=(True, "我也想吃夜宵", "聊到夜宵", 10)),
        )
        await self.ask("好想吃夜宵啊", group=True, room="room-A")
        judge.assert_awaited_once()
        self.assertEqual(judge.await_args.kwargs.get("mode"), "sweet")
        hist = self.listener._histories.get("group:room-A", [])
        self.assertTrue(any(role == "assistant" and "我也想吃夜宵" in content for _, role, content in hist))

    async def test_group_voice_followup_in_balanced_vs_quiet_profile(self):
        voice_xml = '<msg><voicemsg audio_b64="UklGRg==" voicelength="2400" length="4200" /></msg>'

        await self.ask("大肥鱼 设置 均衡", group=True, room="room-A")
        self.listener._record_group_bot_activity("room-A", "wxid_test", "刚才聊了一句")
        with patch.object(
            self.listener,
            "_transcribe_incoming_voice",
            AsyncMock(return_value=("那我们走吧", "开心", 2.4)),
        ):
            ctx_balanced = await self.ask(voice_xml, group=True, room="room-A", sender="wxid_test")
        self.assertTrue(self.requests)
        self.assertIsNotNone(ctx_balanced.event.reply_message_chain)
        self.assertIn("那我们走吧", self.prompt())

        await self.ask("大肥鱼 设置 安静", group=True, room="room-A")
        self.listener._record_group_bot_activity("room-A", "wxid_test", "刚才聊了一句")
        with patch.object(
            self.listener,
            "_transcribe_incoming_voice",
            AsyncMock(return_value=("那我们走吧", "开心", 2.4)),
        ):
            ctx_quiet_unwoken = await self.ask(voice_xml, group=True, room="room-A", sender="wxid_test")
        self.assertFalse(self.requests)
        self.assertIsNone(ctx_quiet_unwoken.event.reply_message_chain)

        with patch.object(
            self.listener,
            "_transcribe_incoming_voice",
            AsyncMock(return_value=("大肥鱼 那我们走吧", "开心", 2.4)),
        ):
            ctx_quiet_woken = await self.ask(voice_xml, group=True, room="room-A", sender="wxid_test")
        self.assertTrue(self.requests)
        self.assertIsNotNone(ctx_quiet_woken.event.reply_message_chain)

    # ------------------------------------------------------------------
    # 3. Private Chat 90s TTL Recent Image Cache
    # ------------------------------------------------------------------
    async def test_private_recent_image_cache_attaches_within_90s_and_expires_after_91s(self):
        buf = io.BytesIO()
        PILImage.new("RGB", (2, 2)).save(buf, format="JPEG")
        img_b64 = base64.b64encode(buf.getvalue()).decode()

        await self.ask("[图片]", image=img_b64)
        self.assertTrue(self.listener._private_recent_images)

        await self.ask("看看上面这张图里写的啥")
        content_fresh = self.payload()["messages"][-1]["content"]
        self.assertIsInstance(content_fresh, list)
        self.assertTrue(any(item.get("type") == "image_url" for item in content_fresh))

        for entry in self.listener._private_recent_images.values():
            entry["ts"] = time.time() - 91

        await self.ask("看看上面这张图里写的啥")
        content_expired = self.payload()["messages"][-1]["content"]
        self.assertIsInstance(content_expired, str)

    # ------------------------------------------------------------------
    # 4. Per-`session_key` Async Session Lock (`_session_locks`)
    # ------------------------------------------------------------------
    async def test_concurrent_same_session_messages_preserve_both_turns_in_history(self):
        session_key = "person:wxid_test:wxid_test"
        lock_a = self.listener._get_session_lock(session_key)
        lock_b = self.listener._get_session_lock(session_key)
        self.assertIs(lock_a, lock_b)

        async def delayed_balance(*args, **kwargs):
            await asyncio.sleep(0.02)
            return "10"

        self.listener._get_balance_str = AsyncMock(side_effect=delayed_balance)

        await asyncio.gather(
            self.ask("第一句", clear_requests=False),
            self.ask("第二句", clear_requests=False),
        )
        hist = self.listener._histories.get(session_key, [])
        self.assertEqual(len(hist), 4)
        self.assertEqual([role for _, role, _ in hist], ["user", "assistant", "user", "assistant"])
        user_turns = [content for _, role, content in hist if role == "user"]
        self.assertCountEqual(user_turns, ["第一句", "第二句"])

    # ------------------------------------------------------------------
    # 5. Private Memory Recall Relevance Sorting & Mood Anti-Repetition
    # ------------------------------------------------------------------
    def test_recall_memories_retrieves_early_matching_fact_beyond_recent_eight(self):
        u = {
            "facts": ["用户对海鲜严重过敏"] + [f"用户平时收藏第{i}款笔记本" for i in range(1, 12)],
            "events": [],
            "archive": {},
        }
        recalled = self.listener._recall_memories(u, "今晚去吃海鲜大咖")
        self.assertIn("用户对海鲜严重过敏", recalled)

    def test_build_mood_section_includes_anti_repetition_on_topic_switch(self):
        u = {
            "mood": 78,
            "dims": {"happy": 18},
            "lastReason": "对方刚才夸我聪明，心里美滋滋",
            "updatedAt": int(time.time() * 1000),
        }
        mood_sec = self.listener._build_mood_section(u)
        self.assertIn("对方刚才夸我聪明", mood_sec)
        self.assertTrue(
            any(kw in mood_sec for kw in ("新话题", "换了话题", "旧原因", "旧事原因", "翻篇", "不要反复", "切勿反复")),
            f"Expected topic-switch anti-repetition instruction in mood section, got: {mood_sec}",
        )

    # ------------------------------------------------------------------
    # 6. Shared Reply Post-Processing (`_clean_reply`, `temperature`, Voice Filter)
    # ------------------------------------------------------------------
    def test_clean_reply_strips_prefixes_paren_and_asterisk_actions_preserving_bold(self):
        self.assertEqual(self.listener._clean_reply("[10:20 大肥鱼]: 今天不加班。"), "今天不加班")
        self.assertEqual(self.listener._clean_reply("大肥鱼：今天不加班。"), "今天不加班")
        self.assertEqual(self.listener._clean_reply("（小声嘟囔）才没有想你呢。"), "才没有想你呢")
        self.assertEqual(self.listener._clean_reply("（一脸嫌弃）这也太离谱了吧。"), "这也太离谱了吧")
        self.assertEqual(
            self.listener._clean_reply("*叹气* 行吧我帮你看 **重点**"),
            "行吧我帮你看 **重点**",
        )

    def test_select_chat_temperature_lowers_for_technical_and_keeps_casual_high(self):
        code_temp = self.listener._select_chat_temperature("请帮我排查这段 Python 代码报错")
        file_temp = self.listener._select_chat_temperature("总结这份文档", is_file_in=True)
        search_temp = self.listener._select_chat_temperature("帮我搜索最新价格")
        casual_temp = self.listener._select_chat_temperature("哈哈今天天气真不错呀")
        self.assertLess(code_temp, 0.9)
        self.assertLess(file_temp, 0.9)
        self.assertLess(search_temp, 0.9)
        self.assertEqual(casual_temp, 1.1)

    def test_is_suitable_for_random_voice_filters_commands_urls_digits_and_english_ids(self):
        self.assertFalse(self.listener._is_suitable_for_random_voice("你可以发 大肥鱼 设置 查看"))
        self.assertFalse(self.listener._is_suitable_for_random_voice("点这个链接 https://example.com 看看"))
        self.assertFalse(self.listener._is_suitable_for_random_voice("订单号是 123456 收好"))
        self.assertFalse(self.listener._is_suitable_for_random_voice("看看 python_version 对不对"))
        self.assertTrue(self.listener._is_suitable_for_random_voice("笑死 你怎么还没睡"))

    # ------------------------------------------------------------------
    # 7. WeChat 4.1 Moments Feed Reading (`fetch_wcf_moments`) & Privacy Isolation
    # ------------------------------------------------------------------
    async def test_fetch_wcf_moments_and_format_digest_with_mock_transport(self):
        original_client = _REAL_ASYNC_CLIENT
        sample_rows = [
            {
                "id": "1001",
                "author": "wxid_friend1",
                "nickname": "小王",
                "is_self": False,
                "time": "2026-10-27T14:30:00",
                "content": "终于下班去吃火锅啦！",
                "media_count": 3,
                "content_type": 1,
            },
            {
                "id": "1002",
                "author": "wxid_self",
                "nickname": "大肥鱼",
                "is_self": True,
                "time": "2026-10-27T12:15:00",
                "content": "",
                "media_count": 1,
                "content_type": 15,
            },
        ]

        def respond_ok(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.path, "/api/moments")
            self.assertEqual(req.url.params.get("limit"), "8")
            return httpx.Response(200, json={"status": 0, "count": 2, "rows": sample_rows})

        with patch.object(
            httpx,
            "AsyncClient",
            side_effect=lambda **kw: original_client(transport=httpx.MockTransport(respond_ok), **kw),
        ):
            ok, rows, err = await df.fetch_wcf_moments(limit=8)
        self.assertTrue(ok)
        self.assertEqual(len(rows), 2)
        self.assertEqual(err, "")

        digest = df.format_wcf_moments_digest(rows, max_items=8)
        self.assertIn("1. [14:30] 小王：终于下班去吃火锅啦！ [附图×3]", digest)
        self.assertIn("2. [12:15] 大肥鱼（我）： [视频×1]", digest)

        def respond_not_loaded(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                503,
                json={
                    "status": -1,
                    "error": "moments_not_loaded",
                    "message": "请先在电脑微信点击打开一次「朋友圈」窗口以加载缓存",
                },
            )

        with patch.object(
            httpx,
            "AsyncClient",
            side_effect=lambda **kw: original_client(transport=httpx.MockTransport(respond_not_loaded), **kw),
        ):
            ok_err, rows_err, msg_err = await df.fetch_wcf_moments(limit=8)
        self.assertFalse(ok_err)
        self.assertEqual(rows_err, [])
        self.assertIn("打开一次「朋友圈」窗口", msg_err)

    async def test_browse_and_roast_moments_privacy_isolation_and_owner_roast(self):
        mock_fetch = self._patch(
            df,
            "fetch_wcf_moments",
            AsyncMock(
                return_value=(
                    True,
                    [
                        {
                            "nickname": "阿强",
                            "is_self": False,
                            "time": "2026-10-27T13:20:00",
                            "content": "今天又在公司加班改需求",
                            "media_count": 1,
                            "content_type": 1,
                        }
                    ],
                    "",
                )
            ),
        )
        self.model_reply = "阿强怎么天天在加班，老板给他发几个菜啊"

        # 1) Group chat must NEVER call fetch_wcf_moments
        ctx_grp = await self.ask("大肥鱼 刷朋友圈", group=True, room="room-A", sender="wxid_owner")
        mock_fetch.assert_not_awaited()
        grp_reply = "\n".join(getattr(c, "text", "") for c in (ctx_grp.event.reply_message_chain or []))
        self.assertIn("鱼塘", grp_reply)
        self.assertNotIn("阿强", grp_reply)

        # 2) Non-owner in private chat when owner_ids is configured must NEVER call fetch_wcf_moments
        prior_cfg = self.listener._get_config_str
        self.listener._get_config_str = lambda key, default: (
            "wxid_owner" if key == "owner_ids" else prior_cfg(key, default)
        )
        ctx_stranger = await self.ask("大肥鱼 刷朋友圈", group=False, room="wxid_other", sender="wxid_other")
        mock_fetch.assert_not_awaited()
        stranger_reply = "\n".join(getattr(c, "text", "") for c in (ctx_stranger.event.reply_message_chain or []))
        self.assertIn("只有主人才能让我帮忙刷", stranger_reply)
        self.assertNotIn("阿强", stranger_reply)

        # 3) Owner in private chat calls fetch_wcf_moments and gets digest + Dafeiyu roast
        ctx_owner = await self.ask("大肥鱼 刷朋友圈", group=False, room="wxid_owner", sender="wxid_owner")
        mock_fetch.assert_awaited_once_with(limit=8)
        owner_reply = "\n".join(getattr(c, "text", "") for c in (ctx_owner.event.reply_message_chain or []))
        self.assertIn("刚刚帮你刷了最新微信朋友圈", owner_reply)
        self.assertIn("[13:20] 阿强：今天又在公司加班改需求 [附图×1]", owner_reply)
        self.assertIn("大肥鱼锐评", owner_reply)
        self.assertIn("阿强怎么天天在加班", owner_reply)


if __name__ == "__main__":
    unittest.main()
