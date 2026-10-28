"""第二轮审查回归：仅使用内存状态和模拟网络。"""

import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

try:
    import test_review_regressions as harness
except ImportError:
    from tests import test_review_regressions as harness
from components.event_listeners import diana_features as df
from components.event_listeners import whale_listener as wl

REAL_CLIENT = httpx.AsyncClient


class IsolationReviewFixes(harness.ListenerHarness):
    def private_secret(self):
        user = self.listener._get_user_state("wxid_test")
        user.update(hasPrivateChat=True, facts=["PRIVATE-FACT-SECRET"],
                    archive={"一": ["PRIVATE-ARCHIVE-SECRET"]},
                    lastReason="PRIVATE-MOOD-SECRET")

    def image_b64(self):
        buf = io.BytesIO()
        Image.new("RGB", (2, 2)).save(buf, format="JPEG")
        return base64.b64encode(buf.getvalue()).decode()

    async def test_group_prompt_never_reads_private_facts_or_archive(self):
        self.private_secret()
        await self.ask("大肥鱼 今天吃啥", group=True, room="room-A")
        self.assertNotIn("PRIVATE-FACT-SECRET", self.prompt())
        self.assertNotIn("PRIVATE-ARCHIVE-SECRET", self.prompt())
        self.assertNotIn("PRIVATE-MOOD-SECRET", self.prompt())

    async def test_group_speak_up_uses_group_context_and_mode(self):
        self.private_secret()
        await self.ask("大肥鱼 设置 模式 甜宠", group=True, room="room-A")
        await self.ask("大肥鱼 记住 本群爱吃火锅", group=True, room="room-A")
        await self.ask("大肥鱼 搭话", group=True, room="room-A")
        self.assertNotIn("PRIVATE-FACT-SECRET", self.prompt())
        self.assertNotIn("PRIVATE-ARCHIVE-SECRET", self.prompt())
        self.assertNotIn("PRIVATE-MOOD-SECRET", self.prompt())
        self.assertIn("本群爱吃火锅", self.prompt())
        self.assertIn("甜宠", self.prompt())

    async def test_document_summary_history_isolated_between_bots(self):
        self.model_reply = "BOT-A-DOCUMENT-SUMMARY"
        await self.upload("file body")
        self.listener._resolve_bot_uuid.return_value = "bot-B"
        await self.ask("总结刚才的文件")
        self.assertNotIn("BOT-A-DOCUMENT-SUMMARY", self.prompt())
        self.listener._resolve_bot_uuid.return_value = "bot-A"
        await self.ask("继续总结")
        self.assertIn("BOT-A-DOCUMENT-SUMMARY", self.prompt())

    async def test_private_recent_image_isolated_between_bots(self):
        await self.ask("[图片]", image=self.image_b64())
        self.listener._resolve_bot_uuid.return_value = "bot-B"
        await self.ask("看看上面这张图")
        self.assertIsInstance(self.payload()["messages"][-1]["content"], str)

    async def test_group_recent_image_isolated_between_bots(self):
        await self.ask("大肥鱼 看图", group=True, room="room-A", image=self.image_b64())
        self.listener._resolve_bot_uuid.return_value = "bot-B"
        await self.ask("大肥鱼 看看上面这张图", group=True, room="room-A")
        self.assertIsInstance(self.payload()["messages"][-1]["content"], str)

    async def test_group_buffer_isolated_between_bots(self):
        await self.ask("大肥鱼 BOT-A-GROUP-TOPIC", group=True, room="room-A")
        self.listener._resolve_bot_uuid.return_value = "bot-B"
        await self.ask("大肥鱼 聊点新的", group=True, room="room-A")
        self.assertNotIn("BOT-A-GROUP-TOPIC", self.prompt())

    async def test_group_continuation_isolated_between_bots(self):
        await self.ask("大肥鱼 你好", group=True, room="room-A")
        judge = self._patch(df, "evaluate_group_active_continuation", AsyncMock(return_value=(False, "", "", 0)))
        self._patch(df, "evaluate_group_interject", AsyncMock(return_value=(False, "", "", 0)))
        self.listener._resolve_bot_uuid.return_value = "bot-B"
        await self.ask("继续聊", group=True, room="room-A")
        judge.assert_not_awaited()

    async def test_empty_moment_wall_is_read_only(self):
        self.listener._state.update(moments=[], auto_moments_enabled=False)
        ctx = await self.ask("大肥鱼 朋友圈", group=True, room="room-A")
        self.publisher.assert_not_awaited()
        self.assertEqual(self.listener._state["moments"], [])
        self.assertIn("空", str(ctx.event.reply_message_chain))

    async def test_group_reset_clears_buffer_but_keeps_archive(self):
        await self.ask("大肥鱼 记住 本群长期火锅梗", group=True, room="room-A")
        await self.ask("大肥鱼 OLD-GROUP-TOPIC", group=True, room="room-A")
        await self.ask("大肥鱼 重置", group=True, room="room-A")
        await self.ask("大肥鱼 聊新的", group=True, room="room-A")
        self.assertNotIn("OLD-GROUP-TOPIC", self.prompt())
        self.assertIn("本群长期火锅梗", self.prompt())

    async def test_reset_serializes_with_inflight_generation(self):
        await self.ask("OLD-PRIVATE-TOPIC")
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_balance(*args):
            entered.set()
            await release.wait()
            return "10"

        self.listener._get_balance_str = blocked_balance
        pending = asyncio.create_task(self.ask("正在生成"))
        await entered.wait()
        reset = asyncio.create_task(self.ask("大肥鱼 重置"))
        try:
            await asyncio.sleep(0)
            self.assertFalse(reset.done(), "重置应等待同一会话的生成完成")
        finally:
            release.set()
            await asyncio.gather(pending, reset)
        self.listener._get_balance_str = AsyncMock(return_value="10")
        await self.ask("开始新话题")
        self.assertNotIn("OLD-PRIVATE-TOPIC", self.prompt())

    async def test_session_lock_is_not_evicted_while_woken_waiter_is_pending(self):
        lock = self.listener._get_session_lock("shared-session")
        await lock.acquire()
        waiter = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0)
        self.listener._session_locks.update({f"idle-{i}": asyncio.Lock() for i in range(256)})
        lock.release()
        replacement = self.listener._get_session_lock("shared-session")
        try:
            self.assertIs(replacement, lock)
        finally:
            await waiter
            lock.release()

    async def test_finished_session_releases_idle_lock_registry_entry(self):
        await self.ask("你好")
        self.assertEqual(self.listener._session_locks, {})

    async def test_forget_does_not_allow_pending_judge_to_recreate_memory(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def respond(req):
            payload = json.loads(req.content)
            if payload["messages"][0]["content"] == wl.MOOD_JUDGE_SYSTEM:
                entered.set()
                await release.wait()
                text = json.dumps({"memory_fact": "OLD-MEMORY-SECRET"})
            else:
                text = "测试回复"
            return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

        self.listener._async_post_turn_judge = wl.WhaleListener._async_post_turn_judge.__get__(self.listener)
        with patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(
                transport=httpx.MockTransport(respond), **kwargs)):
            await self.ask("忘掉前的对话")
            await asyncio.wait_for(entered.wait(), 2)
            await self.ask("大肥鱼 失忆")
            release.set()
            for _ in range(5):
                await asyncio.sleep(0)
        self.assertNotIn("wxid_test", self.listener._state["users"])

    async def test_forget_rejects_stale_memory_result_from_another_session(self):
        for group in (False, True):
            with self.subTest(group=group):
                entered, release = asyncio.Event(), asyncio.Event()

                async def respond(req):
                    entered.set()
                    await release.wait()
                    text = json.dumps({"memory_fact": "OLD-SECRET", "group_fact": "OLD-SECRET"})
                    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

                kwargs = dict(api_key="test-key", base_url="https://model.invalid", model="test",
                              user_text="失忆前的对话", reply_text="失忆前的回复")
                if group:
                    judge = wl.WhaleListener._async_post_group_turn_judge
                    kwargs["group_id"] = "room-A"
                    state_key, target = "groups", "room-A"
                else:
                    judge = wl.WhaleListener._async_post_turn_judge
                    kwargs["user_id"] = "wxid_test"
                    state_key, target = "users", "wxid_test"
                with patch.object(httpx, "AsyncClient", side_effect=lambda **kw: REAL_CLIENT(
                        transport=httpx.MockTransport(respond), **kw)):
                    task = asyncio.create_task(judge(self.listener, **kwargs))
                    await asyncio.wait_for(entered.wait(), 2)
                    # 该任务来自另一个会话，不在当前会话的取消集合中。
                    await self.ask("大肥鱼 失忆", group=group, room="room-A")
                    release.set()
                    await task
                self.assertNotIn(target, self.listener._state[state_key])

    async def test_non_owner_private_interject_logs_are_denied(self):
        self.listener._state["interject_logs"] = [{
            "bot_uuid": "bot-A", "group_id": "room-A", "trigger": "GROUP-SECRET",
        }]
        ctx = await self.ask("大肥鱼 搭话记录")
        self.assertNotIn("GROUP-SECRET", str(ctx.event.reply_message_chain))

    async def test_forget_invalidates_judge_that_has_not_started_in_other_bot(self):
        for group in (False, True):
            with self.subTest(group=group):
                self.listener._resolve_bot_uuid.return_value = "bot-A"
                entered, release = asyncio.Event(), asyncio.Event()
                method = "_async_post_group_turn_judge" if group else "_async_post_turn_judge"
                real_judge = getattr(wl.WhaleListener, method)

                async def deferred_judge(**kwargs):
                    entered.set()
                    await release.wait()
                    await real_judge(self.listener, **kwargs)

                setattr(self.listener, method, AsyncMock(side_effect=deferred_judge))
                self.model_reply = "测试回复"
                await self.ask("大肥鱼 旧话题", group=group, room="room-A")
                await asyncio.wait_for(entered.wait(), 2)
                old_tasks = set().union(*self.listener._session_tasks.values())
                self.listener._resolve_bot_uuid.return_value = "bot-B"
                await self.ask("大肥鱼 失忆", group=group, room="room-A")
                self.model_reply = json.dumps({"memory_fact": "OLD-SECRET", "group_fact": "OLD-SECRET"})
                release.set()
                await asyncio.gather(*old_tasks)
                self.assertNotIn("room-A" if group else "wxid_test",
                                 self.listener._state["groups" if group else "users"])

    async def test_group_interject_logs_are_scoped_to_bot_and_full_group_id(self):
        self.listener._state["interject_logs"] = [
            {"bot_uuid": "bot-A", "group_id": "room-A", "trigger": "CURRENT-LOG"},
            {"bot_uuid": "bot-A", "group_id": "room-B", "trigger": "OTHER-GROUP-SECRET"},
            {"bot_uuid": "bot-B", "group_id": "room-A", "trigger": "OTHER-BOT-SECRET"},
            {"group": "room-A", "trigger": "LEGACY-SECRET"},
        ]
        ctx = await self.ask("大肥鱼 搭话记录", group=True, room="room-A")
        reply = str(ctx.event.reply_message_chain)
        self.assertIn("CURRENT-LOG", reply)
        for secret in ("OTHER-GROUP-SECRET", "OTHER-BOT-SECRET", "LEGACY-SECRET"):
            self.assertNotIn(secret, reply)

    async def test_owner_private_logs_only_include_current_bot(self):
        get_config = self.listener._get_config_str
        self.listener._get_config_str = lambda key, default: "wxid_test" if key == "owner_ids" else get_config(key, default)
        self.listener._state["interject_logs"] = [
            {"bot_uuid": "bot-A", "group_id": "room-A", "trigger": "OWNER-VISIBLE"},
            {"bot_uuid": "bot-B", "group_id": "room-A", "trigger": "OTHER-BOT-SECRET"},
        ]
        ctx = await self.ask("大肥鱼 搭话记录")
        reply = str(ctx.event.reply_message_chain)
        self.assertIn("OWNER-VISIBLE", reply)
        self.assertNotIn("OTHER-BOT-SECRET", reply)

    async def test_followup_text_and_image_use_incoming_bot_uuid(self):
        send = AsyncMock()
        self.listener.plugin = SimpleNamespace(send_message=send)
        self.listener._split_reply_bubbles = lambda *args, **kwargs: ["第一条", "第二条"]
        followup = self._patch(self.listener, "_send_followup_bubbles", AsyncMock())
        await self.ask("你好")
        self.assertEqual(followup.await_args.kwargs.get("bot_uuid"), "bot-A")
        self.assertFalse(send.called)

    async def test_followup_fallback_sends_text_and_image_to_same_bot(self):
        send = AsyncMock()
        self.listener.plugin = SimpleNamespace(send_message=send)
        self.listener._state["meme_enabled"] = True
        self.listener._memes_b64 = {"干饭": self.image_b64()}
        self.model_reply = "第一条||第二条 [表情: 干饭]"
        self.listener._split_reply_bubbles = lambda *args, **kwargs: ["第一条", "第二条"]
        real_sleep = asyncio.sleep

        async def immediate_sleep(*args):
            await real_sleep(0)

        with patch.object(wl.asyncio, "sleep", immediate_sleep), \
                patch.object(df, "send_wcf_text_or_image", AsyncMock(return_value=False)):
            await self.ask("发个表情包")
            tasks = set(self.listener._session_tasks.get("bot-A:person:wxid_test:wxid_test", set()))
            await asyncio.gather(*tasks)
        self.assertEqual(send.await_count, 2)
        for call in send.await_args_list:
            self.assertEqual(call.kwargs["bot_uuid"], "bot-A")
            self.assertEqual(call.kwargs["target_type"], "person")
            self.assertEqual(call.kwargs["target_id"], "wxid_test")
        self.assertIn("第二条", str(send.await_args_list[0].kwargs["message_chain"]))
        self.assertIn("Image", str(send.await_args_list[1].kwargs["message_chain"]))

    async def test_group_manual_speak_up_followup_keeps_group_target(self):
        self.listener._split_reply_bubbles = lambda *args, **kwargs: ["第一条", "第二条"]
        followup = self._patch(self.listener, "_send_followup_bubbles", AsyncMock())
        await self.ask("大肥鱼 搭话", group=True, room="room-A")
        self.assertEqual(followup.await_args.kwargs.get("bot_uuid"), "bot-A")
        self.assertTrue(followup.await_args.kwargs.get("is_group"))

    async def test_reset_cancels_pending_followup_delivery(self):
        self.listener._split_reply_bubbles = lambda *args, **kwargs: ["第一条", "第二条"]
        entered, release = asyncio.Event(), asyncio.Event()
        real_sleep = asyncio.sleep

        async def blocked_delay(delay):
            if delay:
                entered.set()
                await release.wait()
            else:
                await real_sleep(0)

        send = self._patch(df, "send_wcf_text_or_image", AsyncMock(return_value=True))
        with patch.object(wl.asyncio, "sleep", blocked_delay):
            await self.ask("你好")
            await asyncio.wait_for(entered.wait(), 2)
            await self.ask("大肥鱼 重置")
            release.set()
            for _ in range(3):
                await real_sleep(0)
        send.assert_not_awaited()

    async def test_every_tool_call_receives_result_with_execution_limit(self):
        calls = [{"id": f"call-{i}", "type": "function", "function": {
            "name": "web_search", "arguments": json.dumps({"queries": ["query"]}),
        }} for i in range(4)]
        payloads = []

        def respond(req):
            payloads.append(json.loads(req.content))
            message = {"role": "assistant", "content": None, "tool_calls": calls} if len(payloads) == 1 else {"content": "检索完成"}
            return httpx.Response(200, json={"choices": [{"message": message}]})

        search = self._patch(df, "search_web_parallel", AsyncMock(return_value="搜索结果"))
        with patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(
                transport=httpx.MockTransport(respond), **kwargs)):
            ctx = await self.ask("帮我查些资料")
        tools = [m for m in payloads[1]["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tools], [c["id"] for c in calls])
        self.assertEqual(search.await_count, 3)
        self.assertIsNotNone(ctx.event.reply_message_chain)
