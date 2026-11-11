"""回复质量与群聊/私聊上下文优化的单元测试套件。"""

import asyncio
import base64
from datetime import datetime
import io
import json
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
        hist = self.listener._histories.get("bot-A:group:room-A", [])
        self.assertTrue(any(role == "assistant" and "我也想吃夜宵" in content for _, role, content in hist))

    async def test_group_voice_followup_in_balanced_vs_quiet_profile(self):
        voice_xml = '<msg><voicemsg audio_b64="UklGRg==" voicelength="2400" length="4200" /></msg>'

        await self.ask("大肥鱼 设置 均衡", group=True, room="room-A")
        self.listener._record_group_bot_activity("bot-A:group:room-A", "wxid_test", "刚才聊了一句")
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
        self.listener._record_group_bot_activity("bot-A:group:room-A", "wxid_test", "刚才聊了一句")
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
        session_key = "bot-A:person:wxid_test:wxid_test"
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

    # ------------------------------------------------------------------
    # 8. @dafeifish Persona & Architecture Optimizations
    # ------------------------------------------------------------------
    def test_daily_schedule_persistence_and_Progressive_formatting(self):
        state = {}
        day1 = datetime(2026, 10, 27, 9, 30)
        sched1 = df.get_or_create_daily_schedule(state, day1)
        expected_keys = ("date", "wake", "breakfast", "lunch", "dinner", "activity", "small_event")
        for k in expected_keys:
            self.assertIn(k, sched1)
            self.assertTrue(sched1[k])
        self.assertEqual(state.get("daily_schedule"), sched1)
        self.assertEqual(sched1["date"], "2026-10-27")

        sched1_again = df.get_or_create_daily_schedule(state, datetime(2026, 10, 27, 18, 0))
        self.assertEqual(sched1_again, sched1)

        day2 = datetime(2026, 10, 28, 10, 0)
        sched2 = df.get_or_create_daily_schedule(state, day2)
        self.assertEqual(sched2["date"], "2026-10-28")
        self.assertNotEqual(sched2["lunch"], sched1["lunch"])
        self.assertNotEqual(sched2["activity"], sched1["activity"])

        line_morning = df.format_daily_schedule_line(sched1, hour=9)
        self.assertIn("早饭", line_morning)
        self.assertNotIn("晚饭", line_morning)

        line_evening = df.format_daily_schedule_line(sched1, hour=19)
        self.assertIn("早饭", line_evening)
        self.assertIn("午饭", line_evening)
        self.assertIn("晚饭", line_evening)
        self.assertIn("今日小插曲", line_evening)

    def test_anti_repetition_extracts_opening_prefixes(self):
        prefixes = df.extract_opening_prefixes(["啧，你又熬夜", "笑死||那你最后去了吗", "行吧 算你厉害"])
        self.assertIn("啧", prefixes)
        self.assertIn("笑死", prefixes)
        self.assertIn("行吧", prefixes)

        inst = df.build_anti_repetition_instruction(["啧，你又熬夜", "笑死，真有你的"])
        self.assertIn("开头", inst)
        self.assertIn("啧", inst)
        self.assertIn("笑死", inst)

    def test_unfetched_media_and_time_gap_notices(self):
        link_notice = df.build_unfetched_media_notice("看看这个 https://example.com/post/123", parsed_link_card=None)
        self.assertTrue(link_notice)
        self.assertTrue(any(kw in link_notice for kw in ("链接", "网页", "猜测", "编造", "没打", "打不开")))
        self.assertEqual(
            df.build_unfetched_media_notice("看看这个 https://example.com/post/123", parsed_link_card="已解析"),
            "",
        )

        img_notice = df.build_unfetched_media_notice("[图片]", vision_b64=None)
        self.assertTrue(img_notice)
        self.assertTrue(any(kw in img_notice for kw in ("图", "加载", "看清", "瞎猜", "猜测", "编造")))
        self.assertEqual(df.build_unfetched_media_notice("[图片]", vision_b64="abc"), "")

        self.assertEqual(df.build_time_gap_notice(1000.0, 1500.0), "")
        gap_notice = df.build_time_gap_notice(1000.0, 5000.0)
        self.assertTrue(gap_notice)

    def test_persona_and_mood_judge_contain_dafeifish_rules(self):
        for kw in ("溏心蛋", "草莓牛奶", "香菜", "六八四十八", "只提记录里真的聊过的事"):
            self.assertIn(kw, wl.LIVE_PERSONA_BASE)
        self.assertIn("严禁把大肥鱼自己的设定", wl.MOOD_JUDGE_SYSTEM)
        self.assertIn("严禁把大肥鱼自己的设定", wl.GROUP_MOOD_JUDGE_SYSTEM)

    # ------------------------------------------------------------------
    # 9. Native WeChat 4.1 Moments Likes, Comments & 7-Day Friend Auto-Interact
    # ------------------------------------------------------------------
    async def test_moments_digest_includes_likes_comments_and_interact_api(self):
        rows = [
            {
                "sns_id": "13899001122334455",
                "author": "wxid_friend1",
                "nickname": "阿强",
                "is_self": False,
                "time": "2026-10-27T18:20:00",
                "content": "终于吃上铜锅涮肉了！",
                "media_count": 2,
                "content_type": 1,
                "raw_like_flag": True,
                "likes": [{"username": "wxid_a", "nickname": "小明"}, {"username": "wxid_b", "nickname": "小红"}],
                "likes_total": 2,
                "comments": [{"username": "wxid_c", "nickname": "老王", "content": "看着太香了吧！"}],
                "comments_total": 1,
            }
        ]
        digest = df.format_wcf_moments_digest(rows, max_items=8)
        self.assertIn("❤️已赞", digest)
        self.assertIn("👍2人(小明、小红)", digest)
        self.assertIn("💬1条", digest)
        self.assertIn("└ 💬 老王：看着太香了吧！", digest)

        original_client = _REAL_ASYNC_CLIENT
        captured_payloads = []

        def respond_interact(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.path, "/api/interact_moment")
            body = json.loads(req.content)
            captured_payloads.append(body)
            return httpx.Response(
                200,
                json={"status": 0, "code": "completed", "result": {"status": "completed"}, "message": "ok"},
            )

        with patch.object(
            httpx,
            "AsyncClient",
            side_effect=lambda **kw: original_client(transport=httpx.MockTransport(respond_interact), **kw),
        ):
            ok_like, code_like, _ = await df.interact_wcf_moment("13899001122334455", "like")
            ok_comm, code_comm, _ = await df.interact_wcf_moment("13899001122334455", "comment", "给我也留一口！")

        self.assertTrue(ok_like)
        self.assertEqual(code_like, "completed")
        self.assertTrue(ok_comm)
        self.assertEqual(code_comm, "completed")
        self.assertEqual(captured_payloads[0], {"sns_id": "13899001122334455", "action": "like", "content": ""})
        self.assertEqual(
            captured_payloads[1],
            {"sns_id": "13899001122334455", "action": "comment", "content": "给我也留一口！"},
        )

    async def test_owner_moment_like_and_comment_commands_and_permission_isolation(self):
        prior_cfg = self.listener._get_config_str
        self.listener._get_config_str = lambda key, default: (
            "wxid_owner" if key == "owner_ids" else prior_cfg(key, default)
        )
        sample_rows = [
            {
                "sns_id": "14800001111222233",
                "author": "wxid_friend1",
                "nickname": "小美",
                "is_self": False,
                "timestamp": int(time.time()) - 300,
                "time": "2026-10-27T19:00:00",
                "content": "周末去海边吹吹风",
                "media_count": 1,
                "content_type": 1,
            }
        ]
        self._patch(df, "fetch_wcf_moments", AsyncMock(return_value=(True, sample_rows, "")))
        mock_interact = self._patch(
            df, "interact_wcf_moment", AsyncMock(return_value=(True, "completed", "ok"))
        )
        self.model_reply = "海风看着好舒服，替我也多吹两口"

        # Non-owner cannot trigger native moment like
        ctx_non = await self.ask("大肥鱼 点赞朋友圈 1", group=False, room="wxid_other", sender="wxid_other")
        mock_interact.assert_not_awaited()
        non_txt = "\n".join(getattr(c, "text", "") for c in (ctx_non.event.reply_message_chain or []))
        self.assertIn("只有主人", non_txt)

        # Owner likes #1
        ctx_like = await self.ask("大肥鱼 点赞朋友圈 1", group=False, room="wxid_owner", sender="wxid_owner")
        mock_interact.assert_awaited_once_with("14800001111222233", "like")
        like_txt = "\n".join(getattr(c, "text", "") for c in (ctx_like.event.reply_message_chain or []))
        self.assertIn("小美", like_txt)
        self.assertIn("真赞", like_txt)

        # Owner comments on #1 with AI auto-generated comment
        mock_interact.reset_mock()
        ctx_comm = await self.ask("大肥鱼 评论朋友圈 1", group=False, room="wxid_owner", sender="wxid_owner")
        mock_interact.assert_awaited_once_with(
            "14800001111222233", "comment", "海风看着好舒服，替我也多吹两口"
        )
        comm_txt = "\n".join(getattr(c, "text", "") for c in (ctx_comm.event.reply_message_chain or []))
        self.assertIn("已", comm_txt)
        self.assertIn("海风看着好舒服", comm_txt)

    async def test_auto_interact_only_targets_7day_active_private_friends_and_chat_mentions_it(self):
        now_sec = int(time.time())
        now_ms = now_sec * 1000
        self.listener._state["users"] = {
            "wxid_active": {
                "hasPrivateChat": True,
                "lastChatAt": now_ms - 2 * 86400 * 1000,  # 2 days ago -> active!
                "mood": 82,
                "nickname": "铁瓷小张",
            },
            "wxid_stale": {
                "hasPrivateChat": True,
                "lastChatAt": now_ms - 10 * 86400 * 1000,  # 10 days ago -> expired!
                "mood": 80,
                "nickname": "老李",
            },
        }
        feed_rows = [
            {
                "sns_id": "15900000000000001",
                "author": "wxid_stranger",
                "nickname": "路人甲",
                "is_self": False,
                "private": False,
                "timestamp": now_sec - 600,
                "content": "路人甲的朋友圈",
            },
            {
                "sns_id": "15900000000000002",
                "author": "wxid_stale",
                "nickname": "老李",
                "is_self": False,
                "private": False,
                "timestamp": now_sec - 600,
                "content": "老李的朋友圈",
            },
            {
                "sns_id": "15900000000000003",
                "author": "wxid_active",
                "nickname": "铁瓷小张",
                "is_self": False,
                "private": False,
                "timestamp": now_sec - 600,
                "content": "今晚自己烤了蛋挞，大成功！",
            },
        ]
        self._patch(df, "fetch_wcf_moments", AsyncMock(return_value=(True, feed_rows, "")))
        mock_interact = self._patch(
            df, "interact_wcf_moment", AsyncMock(return_value=(True, "completed", "ok"))
        )
        self.model_reply = "这蛋挞烤得焦糖色绝了，快给我留两个"

        with patch.object(wl.random, "random", return_value=0.1):
            actions = await self.listener._check_and_interact_active_friends_moments(max_actions=2)

        self.assertEqual(actions, 1)
        # Must only interact with wxid_active's post (15900000000000003): 1 like + 1 comment
        called_sns_ids = {call.args[0] for call in mock_interact.await_args_list}
        self.assertEqual(called_sns_ids, {"15900000000000003"})
        self.assertEqual(len(mock_interact.await_args_list), 2)

        # Second scan must skip already-interacted moment (idempotency)
        mock_interact.reset_mock()
        actions_again = await self.listener._check_and_interact_active_friends_moments(max_actions=2)
        self.assertEqual(actions_again, 0)
        mock_interact.assert_not_awaited()

        # When wxid_active chats in private asking about their moment, prompt includes the moment perception
        await self.ask("你看到我刚发了条朋友圈吗", group=False, room="wxid_active", sender="wxid_active")
        sys_prompt = self.prompt()
        self.assertIn("今晚自己烤了蛋挞，大成功！", sys_prompt)
        self.assertIn("这蛋挞烤得焦糖色绝了", sys_prompt)

    async def test_friend_auto_accept_and_owner_friend_request_commands(self):
        prior = self.listener._get_config_str
        self.listener._get_config_str = lambda key, default: "wxid_owner" if key == "owner_ids" else prior(key, default)

        req_id_1 = "fr_" + "a" * 32
        req_id_2 = "fr_" + "b" * 32
        mock_reqs_data = {
            "status": 0,
            "auto_accept": True,
            "keywords": [],
            "requests": [
                {
                    "request_id": req_id_1,
                    "sender_id": "wxid_new_friend_1",
                    "nickname": "深海小星",
                    "verification": "你好大肥鱼，想加你好友",
                    "scene": 3,
                    "opcode": 2,
                    "status": "pending",
                },
                {
                    "request_id": req_id_2,
                    "sender_id": "wxid_new_friend_2",
                    "nickname": "路人乙",
                    "verification": "推销广告",
                    "scene": 3,
                    "opcode": 2,
                    "status": "pending",
                },
            ],
        }
        self._patch(df, "fetch_wcf_friend_requests", AsyncMock(return_value=(True, mock_reqs_data, "")))
        mock_accept = self._patch(
            df,
            "accept_wcf_friend_request",
            AsyncMock(
                return_value=(
                    True,
                    {
                        "status": 0,
                        "accepted": True,
                        "request_id": req_id_1,
                        "sender_id": "wxid_new_friend_1",
                        "nickname": "深海小星",
                    },
                    "ok",
                )
            ),
        )
        mock_set_cfg = self._patch(df, "set_wcf_friend_auto_accept", AsyncMock(return_value=True))
        mock_proactive = self._patch(self.listener, "_send_proactive_wechat", AsyncMock(return_value=True))

        # 1. Owner sets verification keyword "大肥鱼"
        ctx_kw = await self.ask("大肥鱼 好友验证词 大肥鱼", group=False, room="wxid_owner", sender="wxid_owner")
        kw_txt = "\n".join(getattr(c, "text", "") for c in (ctx_kw.event.reply_message_chain or []))
        self.assertIn("已设置好友申请验证关键词：大肥鱼", kw_txt)
        mock_set_cfg.assert_awaited_once_with(True, ["大肥鱼"])

        # 2. Background check only auto-accepts req_id_1 (which contains "大肥鱼"), greets wxid_new_friend_1, and records fact
        processed = await self.listener._check_and_process_friend_requests()
        self.assertEqual(processed, 1)
        mock_accept.assert_awaited_once_with(req_id_1)
        mock_proactive.assert_awaited_once()
        self.assertEqual(mock_proactive.await_args.args[0], "wxid_new_friend_1")
        self.assertIn("大肥鱼", mock_proactive.await_args.args[1])
        u_new = self.listener._state["users"].get("wxid_new_friend_1") or {}
        self.assertTrue(u_new.get("hasPrivateChat"))
        self.assertTrue(any("深海小星" in f for f in (u_new.get("facts") or [])))

        # 3. Owner views friend requests and manually accepts #2
        ctx_list = await self.ask("大肥鱼 好友申请", group=False, room="wxid_owner", sender="wxid_owner")
        list_txt = "\n".join(getattr(c, "text", "") for c in (ctx_list.event.reply_message_chain or []))
        self.assertIn("深海小星", list_txt)
        self.assertIn("路人乙", list_txt)

        mock_accept.reset_mock()
        mock_accept.return_value = (
            True,
            {
                "status": 0,
                "accepted": True,
                "request_id": req_id_2,
                "sender_id": "wxid_new_friend_2",
                "nickname": "路人乙",
            },
            "ok",
        )
        ctx_acc = await self.ask("大肥鱼 同意好友 2", group=False, room="wxid_owner", sender="wxid_owner")
        acc_txt = "\n".join(getattr(c, "text", "") for c in (ctx_acc.event.reply_message_chain or []))
        mock_accept.assert_awaited_once_with(req_id_2)
        self.assertIn("已通过「路人乙」的好友申请", acc_txt)

    # ------------------------------------------------------------------
    # 10. Live Log Diagnostics Optimizations (Vision Bridge, Self Moments, Speak-Up Anti-Spam, Group Mute)
    # ------------------------------------------------------------------
    async def test_image_placeholder_not_ignored_and_vl_bridge_for_deepseek_api(self):
        # 1) Plain "[图片]" placeholder without base64 must not be dropped as a plugin command
        ctx_placeholder = await self.ask("[图片]")
        self.assertTrue(self.requests)
        self.assertIsNotNone(ctx_placeholder.event.reply_message_chain)
        self.assertIn("图没刷出来", self.prompt())

        # 2) _normalize_api_model preserves deepseek-flash (DeepSeek-V4.1-Flash-1017) and sends native image_url on api.deepseek.com
        self.assertEqual(self.listener._normalize_api_model("deepseek-flash"), "deepseek-flash")
        self.assertEqual(self.listener._normalize_api_model("DeepSeek-V4.1-Flash-1017"), "deepseek-flash")
        prior_cfg = self.listener._get_config_str
        self.listener._get_config_str = lambda key, default: (
            "https://api.deepseek.com"
            if key == "deepseek_base_url"
            else ("deepseek-flash" if key == "deepseek_model" else prior_cfg(key, default))
        )
        buf = io.BytesIO()
        PILImage.new("RGB", (2, 2)).save(buf, format="JPEG")
        img_b64 = base64.b64encode(buf.getvalue()).decode()

        ctx_vl = await self.ask("看看这张图", image=img_b64)
        self.assertIsNotNone(ctx_vl.event.reply_message_chain)
        self.assertEqual(self.payload()["model"], "deepseek-flash")
        user_content = self.payload()["messages"][-1]["content"]
        self.assertIsInstance(user_content, list)
        self.assertEqual(user_content[1]["type"], "image_url")

    async def test_self_moments_memory_injected_and_nonblocking_friend_moment_check(self):
        self.listener._state["moments"] = [
            {
                "id": 13,
                "at": "2026-10-27 23:25",
                "text": "今晚有草莓牛奶配歌，本鱼决定原地躺平",
                "meme": "得意",
                "mood": 78,
                "likes": 2,
            }
        ]
        self.listener._state["auto_moments_enabled"] = True
        mock_fetch = self._patch(df, "fetch_wcf_moments", AsyncMock(return_value=(True, [], "")))

        await self.ask("你刚发的朋友圈是什么意思呀")
        mock_fetch.assert_not_awaited()
        sys_prompt = self.prompt()
        self.assertIn("今晚有草莓牛奶配歌，本鱼决定原地躺平", sys_prompt)

    async def test_speak_up_loop_never_double_texts_unanswered_user_and_cleans_invalid_uid(self):
        self.assertTrue(self.listener._is_valid_wechat_uid("wxid_kndjpm85zk22"))
        self.assertFalse(self.listener._is_valid_wechat_uid("wxid_kndjpm85zk每天至少八杯水"))

        now_ms = int(time.time() * 1000)
        self.listener._state["users"] = {
            "wxid_kndjpm85zk每天至少八杯水": {
                "hasPrivateChat": True,
                "lastChatAt": now_ms - 7200 * 1000,
                "lastSpeakUpAt": 0,
            },
            "wxid_unanswered": {
                "hasPrivateChat": True,
                "lastChatAt": now_ms - 10800 * 1000,
                "lastSpeakUpAt": now_ms - 5400 * 1000,  # Already spoke up after lastChatAt!
            },
        }
        self.listener._trigger_speak_up_for_user = AsyncMock()
        fixed = SimpleNamespace(now=lambda _: datetime(2026, 10, 27, 22, 0, tzinfo=wl.TZ))
        with patch.object(wl.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()])), \
                patch.object(wl, "datetime", fixed), \
                patch.object(wl.random, "uniform", return_value=0.0):
            with self.assertRaises(asyncio.CancelledError):
                await self.listener._speak_up_loop()
        self.listener._trigger_speak_up_for_user.assert_not_awaited()

    async def test_group_woken_mute_and_interject_does_not_open_continuation_window(self):
        self._patch(df, "get_dynamic_bot_names", return_value=("大肥鱼", "鲸鱼娘", "小鲸鱼", "free"))
        self._patch(df, "send_wcf_text_or_image", AsyncMock(return_value=True))

        # 1) Unprompted interjection must NOT open active continuation window
        self._patch(
            df,
            "evaluate_group_interject",
            AsyncMock(return_value=(True, "我也想吃火锅", "聊到火锅", 10)),
        )
        await self.ask("今晚吃火锅吗", group=True, room="room-A", sender="wxid_member1")
        self.assertNotIn("bot-A:group:room-A", self.listener._group_active_conv)

        # 2) "@Free 闭嘴" must trigger 30-min group mute even after @Free is stripped
        await self.ask("@Free 闭嘴", group=True, room="room-A", sender="wxid_member1")
        self.assertGreater(self.listener._group_mute_until.get("bot-A:group:room-A", 0.0), time.time())

    def test_strip_wake_prefix_removes_trailing_and_inline_bot_mentions(self):
        self._patch(df, "get_dynamic_bot_names", return_value=("大肥鱼", "鲸鱼娘", "小鲸鱼", "free"))
        self.assertEqual(
            self.listener._strip_wake_prefix("锐评一下 @Free"),
            "锐评一下",
        )
        self.assertEqual(
            self.listener._strip_wake_prefix("@Free 锐评一下这张图"),
            "锐评一下这张图",
        )
        self.assertIn("锐评", wl.IMAGE_FOLLOWUP_KEYWORDS)

    def test_xiaohongshu_state_json_and_ef4_video_stream(self):
        body = (
            "<script>window.__INITIAL_STATE__={"
            '"global":{"x":undefined},'
            '"AiNoteDetailStore":{"noteDetailMap":new Map([])},'
            '"tags":new Set(["a"]),'
            '"empty":new Map(),'
            '"note":{"noteDetailMap":{"abc123":{"note":{'
            '"title":"undefined 也能写 new Map(1)",'
            '"desc":"引号\\"和)不算"'
            "}}}}}</script>"
        )
        state = df.parse_xiaohongshu_state(body)
        self.assertIsNotNone(state)
        note = df.extract_xiaohongshu_note_from_state(state, "abc123")
        self.assertIsNotNone(note)
        self.assertEqual(note["title"], "undefined 也能写 new Map(1)")
        self.assertEqual(note["desc"], '引号"和)不算')
        self.assertEqual(state["tags"], ["a"])

        vid_note = {
            "video": {
                "media": {
                    "stream": {
                        "EF4": [
                            {
                                "masterUrl": "https://sns-video.example/a.mp4",
                                "backupUrls": ["https://sns-video.example/b.mp4"],
                            }
                        ],
                        "EF5": [],
                    }
                }
            }
        }
        self.assertEqual(
            df.xiaohongshu_video_candidate_urls(vid_note),
            ["https://sns-video.example/a.mp4", "https://sns-video.example/b.mp4"],
        )

    def test_normalize_qq_music_cookie_maps_musicid_and_musickey(self):
        norm, uin = df.normalize_qq_music_cookie("musicid=o010001; musickey=Q_H_L_12345; refresh_key=rk-1")
        self.assertEqual(uin, "10001")
        self.assertIn("uin=10001", norm)
        self.assertIn("qqmusic_key=Q_H_L_12345", norm)
        self.assertIn("qm_keyst=Q_H_L_12345", norm)

    def test_plain_markdown_bullets_numbers_items_without_double_numbering_ordinals(self):
        raw = "要点：\n- 甲\n  补充说明\n  - 子项\n- 乙\n\n另一组：\n* 丙\n- 第 2 步启动"
        want = "要点：\n1. 甲\n  补充说明\n  - 子项\n2. 乙\n\n另一组：\n1. 丙\n第 2 步启动"
        self.assertEqual(df.plain_markdown_bullets(raw), want)
        self.assertEqual(self.listener._clean_reply(raw), want)

    async def test_mood_saturation_and_chronological_judge_order(self):
        # 1) Normal range (60): +6 -> 66
        self.assertEqual(self.listener._apply_saturated_mood_delta(60, 6), (66, 6))
        # 2) Upper saturation (>=85): positive halved (88 + 3*0.5 -> 90), negative full (88 - 3 -> 85)
        self.assertEqual(self.listener._apply_saturated_mood_delta(88, 3), (90, 2))
        self.assertEqual(self.listener._apply_saturated_mood_delta(88, -3), (85, -3))
        # 3) Lower saturation (<=40): negative halved (35 - 3*0.5 -> 34), positive full (35 + 3 -> 38)
        self.assertEqual(self.listener._apply_saturated_mood_delta(35, -3), (34, -1))
        self.assertEqual(self.listener._apply_saturated_mood_delta(35, 3), (38, 3))

        # 4) Verify _async_post_turn_judge sends user_text BEFORE reply_text
        u_state = self.listener._get_user_state("wxid_judge_order")
        u_state["mood"] = 88
        u_state["dims"] = {"happy": 0}
        self.model_reply = json.dumps(
            {
                "moodDelta": 3,
                "dims": {"happy": 6},
                "reason": "对方夸我聪明，我心里美滋滋",
                "confidence": 85,
                "memory_fact": "",
                "memory_event": "",
            }
        )
        await wl.WhaleListener._async_post_turn_judge(
            self.listener,
            api_key="sk-test",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            user_id="wxid_judge_order",
            user_text="USER-FIRST-MESSAGE",
            reply_text="BOT-SECOND-REPLY",
            expected_state=u_state,
        )
        judge_prompt = self.prompt()
        self.assertLess(judge_prompt.index("USER-FIRST-MESSAGE"), judge_prompt.index("BOT-SECOND-REPLY"))
        self.assertEqual(u_state["mood"], 90)
        self.assertEqual(u_state["dims"]["happy"], 6)
        self.assertEqual(u_state["timeline"][-1]["delta"], 2)

    async def test_moments_generation_dedup_sanitization_and_7day_schedule(self):
        # 1) 7-day rolling schedule deduplication
        state_sched = {}
        seen_lunches = set()
        for day in range(1, 8):
            dt = datetime(2026, 11, day, 12, 0, tzinfo=wl.TZ)
            sc = df.get_or_create_daily_schedule(state_sched, dt)
            seen_lunches.add(sc["lunch"])
        self.assertEqual(len(seen_lunches), 7)

        # 2) Recent Moments deduplication, no default token bill, `||` & cliche sanitization, meme dedup
        self.listener._state["moments"] = [
            {"id": 1, "at": "2026-11-03 20:00", "text": "旧动态一：草莓牛奶真好喝", "meme": "躺平", "theme": "美食与嘴馋时刻"},
            {"id": 2, "at": "2026-11-04 12:00", "text": "旧动态二：困得直点头", "meme": "干饭", "theme": "今日份元气小确幸"},
        ]
        # Model tries to output `||`, `谁懂啊`, and a repeated meme `[表情: 干饭]`
        self.model_reply = "刚拆开一包跳跳糖 || 噼里啪啦在嘴里炸开 谁懂啊 [表情: 干饭]"
        card_txt, _ = await self.listener._generate_and_post_moment()
        gen_prompt = self.prompt()
        self.assertIn("旧动态一：草莓牛奶真好喝", gen_prompt)
        self.assertIn("严禁在朋友圈里报账", gen_prompt)
        self.assertNotIn("tokens（粮仓余额", gen_prompt)

        latest_m = self.listener._state["moments"][-1]
        self.assertNotIn("||", latest_m["text"])
        self.assertNotIn("谁懂啊", latest_m["text"])
        self.assertIn("跳跳糖", latest_m["text"])
        # Because `干饭` and `躺平` were in the last 2 moments, meme tag must be rotated away
        self.assertNotIn(latest_m["meme"], ("干饭", "躺平"))
        self.assertEqual(self.listener._state["self_moments_history"][-1]["text"], latest_m["text"])
        self.assertIn(latest_m["text"], card_txt)

    async def test_auto_reply_to_comments_on_self_and_friend_moments(self):
        now_sec = int(time.time())
        self.listener._state["moments"] = [{"id": 1, "text": "今天天气超好"}]
        self.listener._state["interacted_moments"] = {
            "26000000000000002": {
                "author": "wxid_friend_b",
                "liked": True,
                "commented": True,
                "comment": "这火锅看着太香了吧",
                "ts": now_sec - 600,
            }
        }
        feed_rows = [
            {
                "sns_id": "26000000000000001",
                "author": "wxid_bot_self",
                "nickname": "大肥鱼",
                "is_self": True,
                "private": False,
                "timestamp": now_sec - 1200,
                "content": "耳机里随机到一首超好听的歌",
                "comments": [
                    {
                        "username": "wxid_friend_a",
                        "nickname": "阿泽",
                        "content": "求歌名！我最近也在找歌听",
                        "timestamp": now_sec - 300,
                        "comment_id": "9001",
                    }
                ],
            },
            {
                "sns_id": "26000000000000002",
                "author": "wxid_friend_b",
                "nickname": "小美",
                "is_self": False,
                "private": False,
                "timestamp": now_sec - 900,
                "content": "今晚吃牛油火锅！",
                "comments": [
                    {
                        "username": "wxid_bot_self",
                        "nickname": "大肥鱼",
                        "content": "这火锅看着太香了吧",
                        "timestamp": now_sec - 600,
                        "comment_id": "9002",
                    },
                    {
                        "username": "wxid_friend_b",
                        "nickname": "小美",
                        "content": "哈哈下次带你一起吃呀肥鱼",
                        "timestamp": now_sec - 200,
                        "comment_id": "9003",
                    },
                ],
            },
        ]
        self._patch(df, "fetch_wcf_moments", AsyncMock(return_value=(True, feed_rows, "")))
        mock_interact = self._patch(
            df, "interact_wcf_moment", AsyncMock(return_value=(True, "completed", "ok"))
        )
        self.model_reply = "必须安排上 我连筷子都准备好了"

        actions = await self.listener._check_and_interact_active_friends_moments(max_actions=3)
        self.assertEqual(actions, 2)
        self.assertEqual(len(mock_interact.await_args_list), 2)
        first_call = mock_interact.await_args_list[0].args
        second_call = mock_interact.await_args_list[1].args
        self.assertEqual(first_call[0], "26000000000000001")
        self.assertEqual(first_call[1], "comment")
        self.assertTrue(first_call[2].startswith("回复 阿泽："))
        self.assertEqual(second_call[0], "26000000000000002")
        self.assertEqual(second_call[1], "comment")
        self.assertTrue(second_call[2].startswith("回复 小美："))

        # Second scan must not duplicate replies
        mock_interact.reset_mock()
        actions_again = await self.listener._check_and_interact_active_friends_moments(max_actions=3)
        self.assertEqual(actions_again, 0)
        mock_interact.assert_not_awaited()

    async def test_wechat_mp_article_parse_and_doc_cache(self):
        sample_html = """
        <html>
          <head>
            <meta property="og:title" content="DeepSeek V4.1 深度测评：多模态与长文本全面突破" />
            <meta property="og:description" content="本文实测最新 Flash 模型延迟与准确度。" />
          </head>
          <body>
            <a id="js_name">AI前沿观察</a>
            <div id="js_content">
              <p>第一段：今天实测了 DeepSeek-V4.1-Flash 的多模态识图能力。</p>
              <p style="display: none">这段是隐藏的反爬干扰文字，绝不能提取出来</p>
              <script>console.log("noise");</script>
              <p>第二段：结合 V7 桥接后，最高可接收 256KB 的超长文本消息。</p>
            </div>
          </body>
        </html>
        """
        parsed = df.parse_wechat_mp_html(sample_html, "https://mp.weixin.qq.com/s?__biz=MzA1&mid=100&idx=1&sn=abcd")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["title"], "DeepSeek V4.1 深度测评：多模态与长文本全面突破")
        self.assertEqual(parsed["author"], "AI前沿观察")
        self.assertIn("第一段：今天实测了", parsed["content"])
        self.assertIn("第二段：结合 V7 桥接后", parsed["content"])
        self.assertNotIn("隐藏的反爬干扰文字", parsed["content"])
        self.assertNotIn("console.log", parsed["content"])

        async def _fake_get_public(client, url, **kwargs):
            return httpx.Response(200, text=sample_html, request=httpx.Request("GET", url))

        self._patch(df, "_get_public_url", AsyncMock(side_effect=_fake_get_public))
        xml_card = (
            "<msg><appmsg><type>5</type>"
            "<title>DeepSeek V4.1 深度测评</title>"
            "<des>简短摘要</des>"
            "<url>https://mp.weixin.qq.com/s?__biz=MzA1&amp;mid=100&amp;idx=1&amp;sn=abcd</url>"
            "</appmsg></msg>"
        )
        await self.ask(xml_card, group=False, room="u_mp", sender="u_mp")
        doc_Entries = [v for k, v in self.listener._user_docs.items() if k[2] == "u_mp"]
        self.assertEqual(len(doc_Entries), 1)
        self.assertIn("第二段：结合 V7 桥接后", doc_Entries[0]["text"])
        all_hists = [h for k, h in self.listener._histories.items() if "u_mp" in k]
        self.assertTrue(all_hists)
        self.assertTrue(any("[分享链接]" in c and "第二段" not in c for _, r, c in all_hists[0] if r == "user"))

    async def test_v7_long_text_compaction_and_token_protection(self):
        from collections import deque

        long_msg = "【开头关键信息】" + ("这是一段超长的技术日志与代码分析内容。" * 320) + "【结尾核心结论】"
        self.assertGreater(len(long_msg), 5500)

        prompt_txt, hist_txt, doc_txt = df.compact_long_user_text(long_msg)
        self.assertIsNotNone(doc_txt)
        self.assertIn("【开头关键信息】", prompt_txt)
        self.assertIn("【结尾核心结论】", prompt_txt)
        self.assertIn("完整内容已存入当前会话文档缓存", prompt_txt)
        self.assertLess(len(hist_txt), 320)
        self.assertIn("已存入文档缓存", hist_txt)

        await self.ask(long_msg, group=False, room="u_long", sender="u_long")
        doc_Entries = [v for k, v in self.listener._user_docs.items() if k[2] == "u_long"]
        self.assertEqual(len(doc_Entries), 1)
        self.assertEqual(len(doc_Entries[0]["text"]), len(long_msg))
        all_hists = [h for k, h in self.listener._histories.items() if "u_long" in k]
        self.assertTrue(all_hists)
        user_hist_entries = [c for _, r, c in all_hists[0] if r == "user"]
        self.assertEqual(len(user_hist_entries), 1)
        self.assertLess(len(user_hist_entries[0]), 320)

        # Group context buffer should summarize long V7 text instead of dropping it
        grp_buf: dict[str, deque] = {}
        line = df.append_group_context_line(grp_buf, "g1@chatroom", "u1", long_msg, sender_name="阿泽")
        self.assertIn("...(长文共", line)
        self.assertEqual(len(grp_buf["g1@chatroom"]), 1)

    def test_binary_and_oversized_file_filtering(self):
        # 1. Known binary archive extension (.zip) must be rejected immediately
        zip_b64 = base64.b64encode(b"PK\x03\x04\x14\x00\x00\x00\x08\x00binary_payload").decode("ascii")
        fname, ftext = df.parse_wechat_file_xml(
            f'<msg><appmsg file_b64="{zip_b64}"><type>6</type><title>archive.zip</title></appmsg></msg>'
        )
        self.assertEqual(fname, "archive.zip")
        self.assertEqual(ftext, "")

        # 2. Unknown extension with binary null bytes must NOT fall through to latin1 garbage
        bin_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\xfd").decode("ascii")
        fname2, ftext2 = df.parse_wechat_file_xml(
            f'<msg><appmsg file_b64="{bin_b64}"><type>6</type><title>unknown.dat</title></appmsg></msg>'
        )
        self.assertEqual(fname2, "unknown.dat")
        self.assertEqual(ftext2, "")

        # 3. Oversized file (> 35 MiB declared size) must be skipped safely
        txt_b64 = base64.b64encode("正常文本内容".encode("utf-8")).decode("ascii")
        fname3, ftext3 = df.parse_wechat_file_xml(
            f'<msg><appmsg file_b64="{txt_b64}" file_size="{40 * 1024 * 1024}"><type>6</type><title>huge.log</title></appmsg></msg>'
        )
        self.assertEqual(fname3, "huge.log")
        self.assertEqual(ftext3, "")

        # 4. Valid UTF-8 text/log file within limit extracts cleanly
        fname4, ftext4 = df.parse_wechat_file_xml(
            f'<msg><appmsg file_b64="{txt_b64}" file_size="18"><type>6</type><title>notes.txt</title></appmsg></msg>'
        )
        self.assertEqual(fname4, "notes.txt")
        self.assertEqual(ftext4, "正常文本内容")

    def test_non_friend_group_member_nickname_auto_promotion(self):
        uid = "wxid_nonfriend_9988"
        gid = "888888@chatroom"
        self.assertTrue(df.is_placeholder_display_name("这位群友", uid))
        self.assertTrue(df.is_placeholder_display_name("这位群友(9988)", uid))
        self.assertTrue(df.is_placeholder_display_name("9988", uid))
        self.assertTrue(df.is_placeholder_display_name(uid, uid))
        self.assertFalse(df.is_placeholder_display_name("林深见鹿", uid))

        g_state = self.listener._get_group_state(gid)
        g_state["member_impressions"] = {
            uid: {"name": "这位群友", "tags": ["潜水党"], "chat_count": 3, "updatedAt": 1000}
        }
        promoted = self.listener._sync_group_member_real_name(gid, uid, "林深见鹿")
        self.assertTrue(promoted)
        self.assertEqual(g_state["member_impressions"][uid]["name"], "林深见鹿")

        # Placeholder must never overwrite an existing real nickname
        not_promoted = self.listener._sync_group_member_real_name(gid, uid, "这位群友")
        self.assertFalse(not_promoted)
        self.assertEqual(g_state["member_impressions"][uid]["name"], "林深见鹿")

    def test_group_tagged_transcript_self_current_age_and_spoof_protection(self):
        from collections import deque

        bufs: dict[str, deque] = {}
        gid = "room-tag-test"
        base_ts = 1762500000.0

        # 1. Genuine bot message (is_self=True) -> gets self="true"
        df.append_group_context_line(
            bufs, gid, "__self__", "今晚吃烤鱼", sender_name="大肥鱼", is_self=True, now_ts=base_ts - 120
        )
        # 2. Group member spoofing nickname "大肥鱼" or "本人" (is_self=False) -> MUST NOT get self="true"
        df.append_group_context_line(
            bufs, gid, "wxid_spoof_1", "我也叫大肥鱼哈哈", sender_name="大肥鱼", is_self=False, now_ts=base_ts - 18
        )
        df.append_group_context_line(
            bufs, gid, "wxid_spoof_2", "今天谁请客", sender_name="本人", is_self=False, now_ts=base_ts - 2
        )

        transcript = df.format_group_tagged_transcript(list(bufs[gid]), now_ts=base_ts)
        lines = transcript.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn('age="2分钟前"', lines[0])
        self.assertIn('from="大肥鱼"', lines[0])
        self.assertIn('self="true"', lines[0])
        self.assertNotIn('current="true"', lines[0])

        self.assertIn('age="18秒前"', lines[1])
        self.assertIn('from="大肥鱼"', lines[1])
        self.assertNotIn('self="true"', lines[1])
        self.assertNotIn('current="true"', lines[1])

        self.assertIn('age="刚刚"', lines[2])
        self.assertIn('from="本人"', lines[2])
        self.assertNotIn('self="true"', lines[2])
        self.assertIn('current="true"', lines[2])

        # 3. Pure image / sticker anti-unprompted-commentary rules in group judge system prompts
        self.assertIn("纯图片或纯表情包", df.GROUP_INTERJECT_SYSTEM)
        self.assertIn('self="true"', df.GROUP_INTERJECT_SYSTEM)
        self.assertIn("纯表情包或纯梗图", df.GROUP_CONTINUATION_SYSTEM)
        self.assertIn('self="true"', df.GROUP_CONTINUATION_SYSTEM)

    async def test_group_system_prompt_prefix_cache_stability_across_speakers(self):
        self.listener._state["users"]["wxid_alice"] = {
            "hasPrivateChat": True,
            "nickname": "阿狸",
        }
        name_map = {"wxid_alice": "阿狸", "wxid_bob": "大熊"}
        self._patch(
            df,
            "resolve_user_display_name",
            AsyncMock(side_effect=lambda uid, *args, **kwargs: name_map.get(uid, "群友")),
        )

        await self.ask("大肥鱼 今晚吃啥", group=True, room="room-A", sender="wxid_alice")
        payload_alice = self.payload()
        sys_alice = payload_alice["messages"][0]["content"]
        user_alice = payload_alice["messages"][-1]["content"]

        await self.ask("大肥鱼 我也想问", group=True, room="room-A", sender="wxid_bob")
        payload_bob = self.payload()
        sys_bob = payload_bob["messages"][0]["content"]
        user_bob = payload_bob["messages"][-1]["content"]

        # System prompt (messages[0]) must remain 100% identical across different speakers in the same group
        self.assertEqual(sys_alice, sys_bob)
        # Per-speaker rules and familiarity must be in the tail of the user message (messages[-1])
        self.assertIn("阿狸", user_alice)
        self.assertIn("曾与你私聊过", user_alice)
        self.assertIn("大熊", user_bob)
        self.assertIn("未与你私聊过", user_bob)

    def test_cross_group_card_recognition_by_wxid_and_owner_recognition(self):
        self.listener._state["users"]["wxid_friend_x"] = {
            "hasPrivateChat": True,
            "nickname": "星河",
            "facts": ["PRIVATE-SECRET-DO-NOT-LEAK"],
        }
        fam_txt = self.listener._recall_readonly_private_familiarity(
            "wxid_friend_x", sender_name="研发部-老张", group_id="room-A"
        )
        self.assertIn("星河", fam_txt)
        self.assertIn("研发部-老张", fam_txt)
        self.assertIn("同一位熟人", fam_txt)
        self.assertNotIn("PRIVATE-SECRET-DO-NOT-LEAK", fam_txt)

        prior_cfg = self.listener._get_config_str
        self.listener._get_config_str = lambda key, default: (
            "wxid_boss" if key == "owner_ids" else prior_cfg(key, default)
        )
        boss_txt = self.listener._recall_readonly_private_familiarity(
            "wxid_boss", sender_name="低调群友", group_id="room-A"
        )
        self.assertIn("主人", boss_txt)
        self.assertIn("低调群友", boss_txt)

        # When another group member speaks, Dafeiyu should still know who her owner is by public nickname
        self.listener._state["users"]["wxid_boss"] = {
            "hasPrivateChat": True,
            "nickname": "阿鲸舰长",
            "facts": ["BOSS-PRIVATE-SECRET"],
        }
        other_txt = self.listener._recall_readonly_private_familiarity(
            "wxid_other_member", sender_name="路人甲", group_id="room-A"
        )
        self.assertIn("群内主人识别", other_txt)
        self.assertIn("阿鲸舰长", other_txt)
        self.assertNotIn("wxid_boss", other_txt)
        self.assertNotIn("BOSS-PRIVATE-SECRET", other_txt)

    def test_memory_recall_2gram_no_single_char_noise_and_quote_stripping(self):
        # 1. Single-character stop words or isolated 1-char overlaps in multi-char queries must score 0.0
        self.assertEqual(
            df.compute_memory_query_overlap("我今天在公司加班了", "对方昨天在家里睡了一整天"),
            0.0,
        )
        self.assertEqual(
            df.compute_memory_query_overlap("今天去打篮球了", "对方上周打破了一个玻璃杯"),
            0.0,
        )
        # 2. Multi-char 2-gram or English token hits must score > 0.0
        self.assertGreater(
            df.compute_memory_query_overlap("今晚又想吃螺蛳粉了", "对方超爱吃加辣螺蛳粉"),
            0.0,
        )
        # 3. Quote wrapper speaker/text must be stripped so it doesn't falsely trigger recall
        quoted_query = "【对方引用了消息「火锅: 我昨晚去吃海底捞火鍋了」并回复】：\n今天天气真不错呀"
        self.assertEqual(
            df.compute_memory_query_overlap(quoted_query, "对方最喜欢吃海底捞番茄锅底"),
            0.0,
        )
        # 4. _recall_memories keeps core name/address facts and ranks 2-gram event hits above noise
        now_ms = time.time() * 1000.0
        u_state = {
            "facts": [
                "对方喜欢周末去钓鱼",
                "对方习惯熬夜写Rust",
                "对方不喜欢喝冰美式",
                "对方养了一只布偶猫",
                "对方经常逛数码论坛",
                "对方喜欢听后摇音乐",
                "对方希望大肥鱼叫他阿哲",
            ],
            "events": [
                {"date": "2026-11-01", "ts": now_ms - 8 * 86400000, "text": "对方今天去医院复查了慢性胃炎"},
                {"date": "2026-11-08", "ts": now_ms - 3 * 86400000, "text": "对方在商场买了一件黑色冲锋衣"},
                {"date": "2026-11-09", "ts": now_ms - 2 * 86400000, "text": "对方跟朋友去看了电影"},
                {"date": "2026-11-10", "ts": now_ms - 1 * 86400000, "text": "对方修好了家里的路由器"},
                {"date": "2026-11-11", "ts": now_ms, "text": "对方中午吃了牛肉拉面"},
            ],
            "archive": {},
        }
        recalled = self.listener._recall_memories(u_state, "我胃炎好像又有点犯了")
        self.assertIn("叫他阿哲", recalled)
        self.assertIn("复查了慢性胃炎", recalled)

    async def test_group_interject_preempted_when_direct_wake_arrives_concurrently(self):
        gid = "room-preempt"
        self.listener._state["enabled_groups"] = [gid]
        self.listener._state["group_interject_enabled"] = True
        session_key = f"bot-A:group:{gid}"
        self.listener._group_interject_meta[session_key] = {
            "last_judge_ts": 0.0,
            "unjudged_count": 10,
        }

        async def _slow_interject(**kwargs):
            # Simulate a direct @ wake message arriving in the same group while interject LLM call is in flight
            s_key = f"{kwargs['bot_uuid']}:group:{kwargs['group_id']}"
            self.listener._group_direct_wake_seq[s_key] = (
                self.listener._group_direct_wake_seq.get(s_key, 0) + 1
            )
            kwargs["state"].setdefault("interject_logs", []).append(
                {
                    "at": "11-11 10:00:00",
                    "bot_uuid": kwargs["bot_uuid"],
                    "group_id": kwargs["group_id"],
                    "sender": "群友A",
                    "text": "今晚吃啥好呢",
                    "interjected": True,
                    "reply": "吃烤鱼呗",
                    "reason": "聊到美食",
                }
            )
            return True, "吃烤鱼呗", "聊到美食", 42

        self._patch(df, "evaluate_group_interject", AsyncMock(side_effect=_slow_interject))
        mock_no_at = self._patch(self.listener, "_reply_group_no_at", AsyncMock())

        ctx = await self.ask("今晚吃啥好呢", group=True, room=gid, sender="wxid_user_a")
        # Because a direct wake arrived during evaluation, the interject reply must be aborted!
        self.assertIsNone(ctx.event.reply_message_chain)
        mock_no_at.assert_not_awaited()
        last_log = self.listener._state["interject_logs"][-1]
        self.assertFalse(last_log["interjected"])
        self.assertIn("放弃重复插话", last_log["reason"])

    def test_extract_cookie_field_value_handles_bare_prefixed_and_full_cookie(self):
        self.assertEqual(
            df.extract_cookie_field_value("0099aabbccdd", "MUSIC_U"),
            "0099aabbccdd",
        )
        self.assertEqual(
            df.extract_cookie_field_value("MUSIC_U=0099aabbccdd", "MUSIC_U"),
            "0099aabbccdd",
        )
        self.assertEqual(
            df.extract_cookie_field_value(
                "Cookie: os=pc; appver=2.9.7; MUSIC_U=0099aabbccdd; __csrf=12345",
                "MUSIC_U",
            ),
            "0099aabbccdd",
        )
        self.assertEqual(
            df.extract_cookie_field_value("os=pc; __csrf=12345", "MUSIC_U"),
            "",
        )
        self.assertEqual(
            df.extract_cookie_field_value("buvid3=xyz; SESSDATA=sess_val_888; bili_jct=111", "SESSDATA"),
            "sess_val_888",
        )


if __name__ == "__main__":
    unittest.main()
