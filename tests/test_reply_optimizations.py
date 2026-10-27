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


if __name__ == "__main__":
    unittest.main()
