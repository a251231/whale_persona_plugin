"""v0.5.2 集群优化回归（U-01~U-11 / A-01~A-03）：全部使用本地替身，不访问真实模型、微信或公网。"""

import asyncio
import base64
import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from PIL import Image as PILImage
from langbot_plugin.api.entities.builtin.platform.message import Image, MessageChain, Plain

_REAL_ASYNC_CLIENT = httpx.AsyncClient

try:
    import test_review_regressions as harness
except ImportError:
    from tests import test_review_regressions as harness
from components.event_listeners import diana_features as df
from components.event_listeners import interaction_settings as ui
from components.event_listeners import whale_listener as wl


def _jpeg_b64() -> str:
    buf = io.BytesIO()
    PILImage.new("RGB", (2, 2)).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


class SwarmBase(harness.ListenerHarness):
    owner_ids = ""

    def setUp(self):
        super().setUp()
        prior = self.listener._get_config_str
        owners = self.owner_ids
        self.listener._get_config_str = lambda key, default: (
            owners if key == "owner_ids" else prior(key, default)
        )
        self.listener._state["meme_enabled"] = True

    async def ask(self, text, *, group=False, room="wxid_test", sender="wxid_test",
                  image=None, platform_obj=None, clear=True):
        comps = [Plain(text=text)] if text else []
        if image:
            comps.append(Image(base64=image))
        ctx = SimpleNamespace(
            event=SimpleNamespace(
                message_chain=MessageChain(comps), sender_id=sender, launcher_id=room,
                source_platform_object=platform_obj if platform_obj is not None else {},
                reply_message_chain=None,
            ),
            query_id=None, is_prevented_default=lambda: False, prevent_default=lambda: None,
        )
        if clear:
            self.requests.clear()
        await self.listener._handle_chat(ctx, is_group=group)
        await asyncio.sleep(0)
        return ctx

    @staticmethod
    def reply(ctx):
        chain = ctx.event.reply_message_chain or []
        return "\n".join(getattr(comp, "text", "") for comp in chain)

    def chat_requests(self):
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/chat/completions")]

    async def run_loop_once(self, loop, *, hour=22):
        fixed = SimpleNamespace(now=lambda _: datetime(2026, 10, 27, hour, 0, tzinfo=wl.TZ))
        with patch.object(wl.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()])), \
                patch.object(wl, "datetime", fixed), \
                patch.object(wl.random, "uniform", return_value=0.0), \
                patch.object(wl.random, "random", return_value=0.0):
            with self.assertRaises(asyncio.CancelledError):
                await loop()


class OwnerSecurityTests(SwarmBase):
    """U-01：owner_ids 留空默认拒绝全部主人权限指令。"""

    async def test_empty_owner_ids_denies_private_owner_commands_and_echoes_sender_id(self):
        fetch = self._patch(df, "fetch_wcf_moments", AsyncMock(return_value=(True, [], "")))
        interact = self._patch(df, "interact_wcf_moment", AsyncMock(return_value=(True, "", "")))
        friends = self._patch(df, "fetch_wcf_friend_requests", AsyncMock(return_value=(True, [], "")))
        self.listener._state["auto_moments_enabled"] = False
        commands = ("刷朋友圈", "点赞朋友圈 1", "发朋友圈 今天天气", "好友申请", "开启自动朋友圈")
        for cmd in commands:
            with self.subTest(cmd=cmd):
                result = self.reply(await self.ask(f"大肥鱼 {cmd}", sender="wxid_stranger", room="wxid_stranger"))
                self.assertIn("wxid_stranger", result)
        fetch.assert_not_awaited()
        interact.assert_not_awaited()
        friends.assert_not_awaited()
        self.publisher.assert_not_awaited()
        self.assertFalse(self.listener._state["auto_moments_enabled"])
        self.assertFalse(self.requests)

    async def test_group_publish_moment_never_calls_gateway(self):
        self.owner_ids = "wxid_test"
        prior = self.listener._get_config_str
        self.listener._get_config_str = lambda key, default: (
            "wxid_test" if key == "owner_ids" else prior(key, default)
        )
        result = self.reply(await self.ask("大肥鱼 发朋友圈 今天天气", group=True, room="room-A"))
        self.assertIn("私聊", result)
        self.publisher.assert_not_awaited()

    async def test_configured_non_owner_gets_no_id_echo_and_no_publish(self):
        prior = self.listener._get_config_str
        self.listener._get_config_str = lambda key, default: (
            "wxid_owner" if key == "owner_ids" else prior(key, default)
        )
        result = self.reply(await self.ask("大肥鱼 发朋友圈 今天天气", sender="wxid_other", room="wxid_other"))
        self.assertTrue(result)
        self.assertNotIn("wxid_other", result)
        self.publisher.assert_not_awaited()


class MomentsInteractGateTests(SwarmBase):
    """U-02：朋友圈互动默认关闭，仅开启互动或自动朋友圈时后台循环才会处理。"""

    def test_default_state_disables_moments_auto_interact(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(wl, "STATE_FILE", os.path.join(tmp, "a.json")), \
                    patch.object(wl, "STATE_BACKUP_FILE", os.path.join(tmp, "b.json")), \
                    patch.object(wl.WhaleListener, "_load_bundled_memes", return_value={}), \
                    patch.object(df, "load_stolen_memes", return_value=[]):
                fresh = wl.WhaleListener()
        self.assertIs(fresh._state["moments_auto_interact_enabled"], False)
        self.assertIs(fresh._state["auto_moments_enabled"], False)
        keys = {item.key: item for item in ui.SWITCHES}
        self.assertEqual(keys["moments_auto_interact_enabled"].scope, "global")

    async def test_loop_skips_when_both_switches_off(self):
        self.listener._state.update({"auto_moments_enabled": False, "moments_auto_interact_enabled": False})
        check = AsyncMock(return_value=0)
        self.listener._check_and_interact_active_friends_moments = check
        fetch = self._patch(df, "fetch_wcf_moments", AsyncMock(return_value=(True, [], "")))
        await self.run_loop_once(self.listener._moments_auto_interact_loop)
        check.assert_not_awaited()
        fetch.assert_not_awaited()

    async def test_loop_runs_when_interact_switch_on(self):
        self.listener._state.update({"auto_moments_enabled": False, "moments_auto_interact_enabled": True})
        check = AsyncMock(return_value=0)
        self.listener._check_and_interact_active_friends_moments = check
        await self.run_loop_once(self.listener._moments_auto_interact_loop)
        check.assert_awaited_once()

    async def test_loop_honors_quiet_morning_hours(self):
        self.listener._state.update({"moments_auto_interact_enabled": True})
        check = AsyncMock(return_value=0)
        self.listener._check_and_interact_active_friends_moments = check
        await self.run_loop_once(self.listener._moments_auto_interact_loop, hour=3)
        check.assert_not_awaited()


class PersistenceTests(SwarmBase):
    """A-01：_save_seq 递增、临时文件清理、择新恢复，以及主动搭话循环合并落盘。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.primary = os.path.join(self.tmp.name, "primary.json")
        self.backup = os.path.join(self.tmp.name, "backup.json")
        self._patch(wl, "STATE_FILE", self.primary)
        self._patch(wl, "STATE_BACKUP_FILE", self.backup)
        self.listener.__dict__.pop("_save_persistent_state", None)

    def _read(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_save_increments_seq_and_mirrors_both_files(self):
        self.listener._state["_save_seq"] = 4
        self.listener._save_persistent_state()
        self.listener._save_persistent_state()
        self.assertEqual(self._read(self.primary)["_save_seq"], 6)
        self.assertEqual(self._read(self.backup)["_save_seq"], 6)

    def test_save_failure_cleans_temp_files(self):
        with patch.object(wl.os, "replace", side_effect=OSError("disk full")):
            self.listener._save_persistent_state()
        leftovers = [n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_load_prefers_higher_seq_when_backup_is_newer(self):
        for path, seq, marker in ((self.primary, 3, "old"), (self.backup, 9, "new")):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"_save_seq": seq, "marker": marker, "users": {}}, fh)
        self.assertEqual(self.listener._load_persistent_state()["marker"], "new")

    def test_load_prefers_primary_when_seq_equal_or_higher(self):
        for path, seq, marker in ((self.primary, 9, "primary"), (self.backup, 3, "backup")):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"_save_seq": seq, "marker": marker, "users": {}}, fh)
        self.assertEqual(self.listener._load_persistent_state()["marker"], "primary")

    def test_load_survives_corrupt_primary(self):
        with open(self.primary, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        with open(self.backup, "w", encoding="utf-8") as fh:
            json.dump({"_save_seq": 1, "marker": "backup", "users": {}}, fh)
        self.assertEqual(self.listener._load_persistent_state()["marker"], "backup")

    async def test_speak_up_loop_marks_dirty_once_per_pass(self):
        now_ms = int(time.time() * 1000)
        users = self.listener._state["users"]
        for idx in range(3):
            users[f"wxid_miss{idx}"] = {
                "hasPrivateChat": True, "lastChatAt": now_ms - 3 * 3600 * 1000, "missStreak": 0,
            }
        dirty = MagicMock()
        self.listener._mark_state_dirty = dirty
        self.listener._trigger_speak_up_for_user = AsyncMock()
        with patch.object(wl.random, "uniform", return_value=100.0):
            fixed = SimpleNamespace(now=lambda _: datetime(2026, 10, 27, 22, 0, tzinfo=wl.TZ))
            with patch.object(wl.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()])), \
                    patch.object(wl, "datetime", fixed):
                with self.assertRaises(asyncio.CancelledError):
                    await self.listener._speak_up_loop()
        self.listener._trigger_speak_up_for_user.assert_not_awaited()
        for idx in range(3):
            self.assertEqual(users[f"wxid_miss{idx}"]["missStreak"], 1)
        self.assertEqual(dirty.call_count, 1)


class PrefixCacheTests(SwarmBase):
    """A-03：三层 Prefix Cache，静态层逐字节稳定，动态内容仅进入末条 <turn-context>。"""

    async def test_private_system_prompt_is_byte_stable_while_dynamic_state_changes(self):
        await self.ask("今天好无聊啊")
        first = self.payload()["messages"]
        u = self.listener._get_user_state("wxid_test")
        u["mood"] = 40
        u["dims"]["happy"] = 90
        u["lastReason"] = "MOODREASON-SENTINEL"
        u["facts"] = ["用户养了一只叫团团的橘猫"]
        await self.ask("团团最近怎么样")
        second = self.payload()["messages"]
        self.assertEqual(first[0]["content"].encode("utf-8"), second[0]["content"].encode("utf-8"))
        self.assertNotIn("<turn-context>", second[0]["content"])
        last = second[-1]["content"]
        self.assertIn("<turn-context>", last)
        self.assertIn("MOODREASON-SENTINEL", last)
        self.assertNotIn("MOODREASON-SENTINEL", second[0]["content"])
        self.assertIn("团团", last)
        self.assertNotIn("团团", second[0]["content"])

    async def test_grain_line_only_appears_for_token_topics(self):
        await self.ask("今天天气不错")
        self.assertNotIn("粮仓状态", self.payload()["messages"][-1]["content"])
        await self.ask("今天消耗了多少 token 呀，余额还剩多少")
        payload = self.payload()["messages"]
        self.assertIn("粮仓状态", payload[-1]["content"])
        self.assertNotIn("粮仓状态", payload[0]["content"])

    async def test_group_system_prompt_is_stable_across_speakers(self):
        await self.ask("大肥鱼 今天吃啥", group=True, room="room-A", sender="wxid_a")
        first = self.payload()["messages"][0]["content"]
        self.listener._get_group_state("room-A")["mood"] = 40
        await self.ask("大肥鱼 晚上吃啥", group=True, room="room-A", sender="wxid_b")
        payload = self.payload()["messages"]
        self.assertEqual(first, payload[0]["content"])
        self.assertIn("<turn-context>", payload[-1]["content"])


class ReplyGuardTests(SwarmBase):
    """U-03：回复清洗、慢工具过渡句先发、表情冷却。"""

    def test_sanitize_strips_verbatim_user_opening_echo(self):
        out = wl.WhaleListener._sanitize_reply_echo_and_catchphrases(
            "今天好累啊真的，那你早点休息吧", "今天好累啊真的"
        )
        self.assertEqual(out, "那你早点休息吧")

    def test_sanitize_keeps_reply_when_nothing_follows_echo(self):
        out = wl.WhaleListener._sanitize_reply_echo_and_catchphrases("今天好累啊真的", "今天好累啊真的")
        self.assertEqual(out, "今天好累啊真的")

    def test_sanitize_caps_repeated_catchphrases(self):
        out = wl.WhaleListener._sanitize_reply_echo_and_catchphrases("本鱼觉得本鱼最棒", "你好", ["本鱼才不管"])
        self.assertNotIn("本鱼", out)
        self.assertEqual(wl.WhaleListener._sanitize_reply_echo_and_catchphrases("哼，哼，哼 行吧", "你好"), "哼 行吧")
        once = wl.WhaleListener._sanitize_reply_echo_and_catchphrases("本鱼觉得本鱼最棒", "你好")
        self.assertEqual(once.count("本鱼"), 1)

    async def test_slow_tool_interim_text_is_pushed_before_second_model_call(self):
        events = []
        self._patch(df, "search_web_parallel", AsyncMock(return_value="今日上海晴，22度"))

        async def fake_send(*args, **kwargs):
            events.append(("push", kwargs.get("text")))
            return True

        self._patch(df, "send_wcf_text_or_image", AsyncMock(side_effect=fake_send))
        queued = [
            {"choices": [{"message": {
                "role": "assistant", "content": "我先帮你瞅一眼今天上海的天气哈",
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "web_search", "arguments": json.dumps({"queries": ["上海天气"]}, ensure_ascii=False)}}],
            }}], "usage": {"total_tokens": 5}},
            {"choices": [{"message": {"content": "查到啦，上海今天大晴天"}}], "usage": {"total_tokens": 5}},
        ]

        def respond(req):
            self.requests.append(req)
            events.append(("req", None))
            return httpx.Response(200, json=queued.pop(0) if queued else queued_last)

        queued_last = {"choices": [{"message": {"content": "好的"}}], "usage": {"total_tokens": 1}}
        self._patch(httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(respond), **kw))
        await self.ask("帮我查下今天上海天气")
        kinds = [e[0] for e in events]
        self.assertIn("push", kinds)
        self.assertEqual(events[kinds.index("push")][1], "我先帮你瞅一眼今天上海的天气哈")
        second_req = [i for i, k in enumerate(kinds) if k == "req"][1]
        self.assertLess(kinds.index("push"), second_req)

    async def test_meme_tool_is_omitted_during_cooldown(self):
        u = self.listener._get_user_state("wxid_test")
        u["turnsSinceMeme"] = 0
        u["lastMemeAt"] = int(time.time())
        await self.ask("今天天气不错")
        tools = self.payload().get("tools") or []
        names = [t["function"]["name"] for t in tools]
        self.assertNotIn("send_meme", names)
        self.assertIn("web_search", names)
        settings = self.listener._settings_for("wxid_test")
        self.assertNotIn(wl.SEND_MEME_TOOL, self.listener._build_chat_tools(settings, allow_meme=False))

    async def test_meme_tool_is_offered_when_cooldown_elapsed(self):
        u = self.listener._get_user_state("wxid_test")
        u["turnsSinceMeme"] = 99
        u["lastMemeAt"] = 0
        await self.ask("今天天气不错")
        names = [t["function"]["name"] for t in (self.payload().get("tools") or [])]
        self.assertIn("send_meme", names)


class SpeakUpTests(SwarmBase):
    """U-04：主动搭话写入历史、最多两次未回复、凌晨静默、用户回复后清零。"""

    async def test_manual_false_speak_up_appends_history(self):
        self.listener._send_proactive_wechat = AsyncMock(return_value=True)
        self.model_reply = "在干嘛呢，半天没动静"
        u = self.listener._get_user_state("wxid_su")
        u["hasPrivateChat"] = True
        u["lastChatAt"] = int((time.time() - 7200) * 1000)
        reply, _ = await self.listener._trigger_speak_up_for_user("wxid_su", manual=False)
        self.assertTrue(reply)
        key = self.listener._resolve_private_session_key("wxid_su", u)
        hist = self.listener._histories.get(key) or []
        self.assertTrue(any(role == "assistant" and "在干嘛呢" in text for _, role, text in hist))
        self.assertEqual(u["speakUpCountSinceReply"], 1)

    async def test_manual_true_speak_up_does_not_touch_history_or_counter(self):
        self.model_reply = "在干嘛呢，半天没动静"
        u = self.listener._get_user_state("wxid_su2")
        reply, _ = await self.listener._trigger_speak_up_for_user("wxid_su2", manual=True)
        self.assertTrue(reply)
        self.assertEqual(u["speakUpCountSinceReply"], 0)
        self.assertEqual(self.listener._histories, {})

    def _prepare(self, **fields):
        now_ms = int(time.time() * 1000)
        u = self.listener._state["users"].setdefault("wxid_su", {})
        u.update({"hasPrivateChat": True, "missStreak": 0})
        for key, hours in (("lastChatAt", fields.get("chat_h")), ("lastSpeakUpAt", fields.get("speak_h"))):
            u[key] = 0 if hours is None else now_ms - int(hours * 3600 * 1000)
        u["speakUpCountSinceReply"] = fields.get("count", 0)
        return u

    async def _loop(self, hour=22):
        self.listener._trigger_speak_up_for_user = AsyncMock(return_value=(None, None))
        await self.run_loop_once(self.listener._speak_up_loop, hour=hour)
        return self.listener._trigger_speak_up_for_user

    async def test_first_and_second_unanswered_speak_up_are_allowed(self):
        self._prepare(chat_h=3, speak_h=None, count=0)
        trig = await self._loop()
        trig.assert_awaited_once_with("wxid_su", manual=False)
        self._prepare(chat_h=8, speak_h=5, count=1)
        trig = await self._loop()
        trig.assert_awaited_once_with("wxid_su", manual=False)

    async def test_third_unanswered_speak_up_is_blocked_until_user_replies(self):
        self._prepare(chat_h=8, speak_h=5, count=2)
        trig = await self._loop()
        trig.assert_not_awaited()

    async def test_speak_up_respects_four_hour_spacing(self):
        self._prepare(chat_h=8, speak_h=1, count=1)
        trig = await self._loop()
        trig.assert_not_awaited()

    async def test_midnight_quiet_hours_block_speak_up(self):
        self._prepare(chat_h=3, speak_h=None, count=0)
        trig = await self._loop(hour=2)
        trig.assert_not_awaited()

    async def test_user_message_resets_counter(self):
        u = self.listener._get_user_state("wxid_test")
        u["speakUpCountSinceReply"] = 2
        await self.ask("我回来啦")
        self.assertEqual(self.listener._state["users"]["wxid_test"]["speakUpCountSinceReply"], 0)


class MomentCommentOwnershipTests(SwarmBase):
    """U-05：只回复大肥鱼自己发布过的 is_self 动态。"""

    def _row(self, content):
        return {
            "sns_id": "sns-1", "author": "wxid_owner", "is_self": True, "content": content,
            "comments": [{"username": "wxid_friend", "nickname": "好友", "content": "好看", "comment_id": "9"}],
        }

    async def test_unknown_is_self_moment_is_skipped(self):
        self.listener._state["self_moments_history"] = [{"text": "大肥鱼自己发的小鱼干"}]
        self.listener._state["moments"] = []
        gen = self._patch(df, "generate_moment_reply_comment", AsyncMock(return_value=("谢谢", 1)))
        act = self._patch(df, "interact_wcf_moment", AsyncMock(return_value=(True, "", "")))
        out = await self.listener._check_and_reply_moment_comments([self._row("主人手动发的别的动态")])
        self.assertEqual(out, [])
        gen.assert_not_awaited()
        act.assert_not_awaited()

    async def test_known_self_moment_is_replied(self):
        self.listener._state["self_moments_history"] = [{"text": "大肥鱼自己发的小鱼干"}]
        gen = self._patch(df, "generate_moment_reply_comment", AsyncMock(return_value=("谢谢", 1)))
        act = self._patch(df, "interact_wcf_moment", AsyncMock(return_value=(True, "", "")))
        out = await self.listener._check_and_reply_moment_comments([self._row("大肥鱼自己发的小鱼干")])
        self.assertEqual(len(out), 1)
        gen.assert_awaited_once()
        act.assert_awaited_once()

    async def test_empty_history_with_no_moments_never_replies(self):
        self.listener._state["self_moments_history"] = []
        self.listener._state["moments"] = []
        gen = self._patch(df, "generate_moment_reply_comment", AsyncMock(return_value=("谢谢", 1)))
        out = await self.listener._check_and_reply_moment_comments([self._row("任何动态")])
        self.assertEqual(out, [])
        gen.assert_not_awaited()


class PrefixCommandAndSettingsTests(SwarmBase):
    """U-06/U-07：统一前缀口令与设置档位反推。"""

    async def test_small_whale_prefix_works_for_memory_commands(self):
        u = self.listener._get_user_state("wxid_test")
        u["facts"] = ["用户爱喝咖啡"]
        for cmd in ("小鲸鱼 重置", "小鲸鱼 记住 我喜欢蓝色", "小鲸鱼 心情", "小鲸鱼 忘掉 咖啡", "小鲸鱼 失忆"):
            with self.subTest(cmd=cmd):
                result = self.reply(await self.ask(cmd))
                self.assertTrue(result)
                self.assertFalse(self.requests)
        self.assertEqual(self.listener._get_user_state("wxid_test")["facts"], [])

    async def test_remember_with_small_whale_prefix_stores_fact(self):
        await self.ask("小鲸鱼 记住 我喜欢蓝色的天空")
        self.assertIn("我喜欢蓝色的天空", self.listener._get_user_state("wxid_test")["facts"])

    def test_profile_is_reverse_matched_from_manual_overrides(self):
        for name, profile in ui.PROFILES.items():
            with self.subTest(profile=name):
                state = {"conversation_settings": {"group:g1": dict(profile)}}
                self.assertEqual(ui.effective_settings(state, "group:g1")["profile"], name)

    def test_profile_becomes_custom_when_overrides_differ(self):
        state = {"conversation_settings": {"group:g1": {**ui.PROFILES["热闹"], "repeat_enabled": False}}}
        self.assertEqual(ui.effective_settings(state, "group:g1")["profile"], "自定义")

    def test_untouched_scope_keeps_legacy_profile_label(self):
        self.assertEqual(ui.effective_settings({}, "group:g1")["profile"], "沿用原设置")

    def test_group_interject_shortcut_maps_to_group_switch(self):
        state = {}
        for text, expected in (("大肥鱼 开启搭话", True), ("大肥鱼 关闭搭话", False)):
            reply, changed = ui.handle_control(
                state, text, is_group=True, target_id="room-A", sender_id="wxid_x")
            self.assertTrue(changed)
            overrides = state["conversation_settings"]["group:room-A"]
            self.assertIs(overrides["group_interject_enabled"], expected)
            self.assertNotIn("speak_up_enabled", overrides)

    def test_private_speak_up_stays_person_scoped(self):
        state = {}
        ui.handle_control(state, "大肥鱼 关闭搭话", is_group=False, target_id="wxid_x", sender_id="wxid_x")
        overrides = state["conversation_settings"]["person:wxid_x"]
        self.assertIs(overrides["speak_up_enabled"], False)

    async def test_disabled_group_at_gets_rate_limited_hint(self):
        self.listener._state["enabled_groups"] = []
        hint = self._patch(self.listener, "_reply_group_no_at", AsyncMock())
        for _ in range(3):
            await self.ask("@大肥鱼 你好呀", group=True, room="room-X", platform_obj={"is_at": True})
        self.assertEqual(hint.await_count, 1)
        self.listener._disabled_group_hint_ts["room-X"] -= 1801
        await self.ask("@大肥鱼 你好呀", group=True, room="room-X", platform_obj={"is_at": True})
        self.assertEqual(hint.await_count, 2)


class GroupGateTests(SwarmBase):
    """U-08/U-09：未启用群 O(1) 退出、路人图片不触发评论、flash 保留工具、低心情不发随机语音。"""

    async def test_disabled_group_exits_before_name_lookup_and_buffering(self):
        self.listener._state["enabled_groups"] = []
        resolver = df.resolve_user_display_name
        await self.ask("今天吃啥", group=True, room="room-X")
        resolver.assert_not_awaited()
        self.assertEqual(self.listener._group_msg_buffers, {})
        self.assertEqual(self.listener._group_recent_images, {})
        self.assertFalse(self.requests)

    async def test_bystander_pure_image_does_not_trigger_continuation(self):
        key = "bot-A:group:room-A"
        self.listener._group_active_conv[key] = {
            "last_speaker_id": "wxid_A", "last_bot_ts": time.time() - 10,
            "last_bot_reply": "刚才的回复", "active_speakers": {"wxid_A"},
        }
        judge = self._patch(df, "evaluate_group_active_continuation", AsyncMock(return_value=(True, "好看", "", 1)))
        self._patch(df, "evaluate_group_interject", AsyncMock(return_value=(False, "", "", 0)))
        await self.ask("[图片]", group=True, room="room-A", sender="wxid_B", image=_jpeg_b64())
        judge.assert_not_awaited()
        self.assertFalse(self.requests)

    async def test_flash_keeps_web_search_when_image_attached(self):
        await self.ask("[图片]", image=_jpeg_b64())
        payload = self.payload()
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertIsInstance(payload["messages"][-1]["content"], list)
        names = [t["function"]["name"] for t in payload.get("tools", [])]
        self.assertIn("web_search", names)

    async def test_low_mood_suppresses_spontaneous_voice(self):
        self.listener._state["voice_reply_enabled"] = True
        voice = self._patch(self.listener, "_synthesize_and_send_voice", AsyncMock(return_value=True))
        self.model_reply = "好呀好呀我知道啦"
        u = self.listener._get_user_state("wxid_test")
        u["mood"] = 50
        with patch.object(wl.random, "random", return_value=0.0):
            await self.ask("今天天气不错")
        voice.assert_not_awaited()

    async def test_high_mood_allows_spontaneous_voice(self):
        self.listener._state["voice_reply_enabled"] = True
        voice = self._patch(self.listener, "_synthesize_and_send_voice", AsyncMock(return_value=True))
        self.model_reply = "好呀好呀我知道啦"
        u = self.listener._get_user_state("wxid_test")
        u["mood"] = 90
        with patch.object(wl.random, "random", return_value=0.0):
            await self.ask("今天天气不错")
        voice.assert_awaited_once()


class MemoryQualityTests(SwarmBase):
    """U-10/U-11：极性去重、档案整理保护、忘掉指令、陌生人亲密度。"""

    def test_polarity_flip_replaces_old_fact(self):
        facts = ["用户喜欢吃香菜"]
        wl._add_dedup_fact(facts, "用户不喜欢吃香菜")
        self.assertEqual(facts, ["用户不喜欢吃香菜"])
        wl._add_dedup_fact(facts, "用户喜欢吃香菜")
        self.assertEqual(facts, ["用户喜欢吃香菜"])

    def test_unrelated_fact_is_appended(self):
        facts = ["用户喜欢吃香菜"]
        self.assertTrue(wl._add_dedup_fact(facts, "用户住在杭州西湖区附近"))
        self.assertEqual(len(facts), 2)

    def _client_returning(self, content, finish_reason):
        def handler(req):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
                "usage": {"total_tokens": 1},
            })
        return patch.object(httpx, "AsyncClient", side_effect=lambda **kw: _REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(handler), **kw))

    async def test_consolidation_keeps_archive_when_output_truncated(self):
        u = self.listener._get_user_state("wxid_a")
        u["archive"] = {"一": ["[2026-01-01] 旧档案条目"]}
        u["facts"] = ["用户爱喝咖啡"]
        with self._client_returning("三|2026-02-02|被截断的半截", "length"):
            ok, msg = await self.listener._consolidate_user_archive(
                api_key="k", base_url="https://model.invalid", model="deepseek-flash", user_id="wxid_a")
        self.assertFalse(ok)
        self.assertTrue(msg)
        self.assertEqual(u["archive"], {"一": ["[2026-01-01] 旧档案条目"]})
        self.assertEqual(u["facts"], ["用户爱喝咖啡"])

    async def test_consolidation_applies_complete_output(self):
        u = self.listener._get_user_state("wxid_a")
        u["facts"] = ["用户爱喝咖啡"]
        with self._client_returning("三|2026-02-02|用户爱喝咖啡且不加糖", "stop"):
            ok, _ = await self.listener._consolidate_user_archive(
                api_key="k", base_url="https://model.invalid", model="deepseek-flash", user_id="wxid_a")
        self.assertTrue(ok)
        self.assertIn("三", u["archive"])

    async def test_forget_keyword_clears_private_memory_containers(self):
        u = self.listener._get_user_state("wxid_test")
        u["facts"] = ["用户爱喝咖啡", "用户住在杭州"]
        u["events"] = [{"date": "2026-01-01", "text": "一起喝咖啡聊天", "ts": 1.0}, {"date": "2026-01-02", "text": "看了电影"}]
        u["archive"] = {"一": ["[2026-01-01] 咖啡馆见面", "[2026-01-02] 去爬山"]}
        result = self.reply(await self.ask("大肥鱼 忘掉 咖啡"))
        self.assertEqual(u["facts"], ["用户住在杭州"])
        self.assertEqual([e["text"] for e in u["events"]], ["看了电影"])
        self.assertEqual(u["archive"]["一"], ["[2026-01-02] 去爬山"])
        self.assertIn("3", result)
        self.assertFalse(self.requests)

    async def test_forget_keyword_without_match_reports_nothing_removed(self):
        u = self.listener._get_user_state("wxid_test")
        u["facts"] = ["用户住在杭州"]
        result = self.reply(await self.ask("大肥鱼 忘掉 咖啡"))
        self.assertEqual(u["facts"], ["用户住在杭州"])
        self.assertIn("没找到", result)

    async def test_forget_keyword_clears_group_memory(self):
        g = self.listener._get_group_state("room-A")
        g["facts"] = ["群里都爱喝咖啡", "群里周末爬山"]
        g["events"] = [{"date": "2026-01-01", "text": "咖啡聚会"}]
        g["member_impressions"] = {"wxid_test": {"tags": ["咖啡党", "话多"]}}
        result = self.reply(await self.ask("大肥鱼 忘掉 咖啡", group=True, room="room-A"))
        self.assertEqual(g["facts"], ["群里周末爬山"])
        self.assertEqual(g["events"], [])
        self.assertEqual(g["member_impressions"]["wxid_test"]["tags"], ["话多"])
        self.assertIn("3", result)

    def test_zero_memory_stranger_stays_in_first_tier_even_with_high_mood(self):
        stranger = {"mood": 95, "facts": [], "events": [], "archive": {}, "stable_profile": []}
        self.assertIn("初识网友", self.listener._build_intimacy_section(stranger, mode="live"))
        sweet = self.listener._build_intimacy_section(stranger, mode="sweet")
        self.assertNotIn("专属偏爱", sweet)
        self.assertIn("甜软陪伴", sweet)

    async def test_mood_panel_does_not_rank_zero_memory_user_top_tier(self):
        u = self.listener._get_user_state("wxid_test")
        u["mood"] = 90
        result = self.reply(await self.ask("大肥鱼 心情"))
        self.assertIn("已攒 0 条记忆", result)
        self.assertNotIn("偏爱熟透", result)


if __name__ == "__main__":
    unittest.main()
