"""统一设置入口、兼容口令与会话消费者的集成测试。"""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_review_regressions as harness
from components.event_listeners import diana_features as df
from components.event_listeners import whale_listener as wl


class CommandSettingsRegressions(harness.ListenerHarness):
    def reply(self, ctx):
        chain = ctx.event.reply_message_chain or []
        return "\n".join(getattr(comp, "text", "") for comp in chain)

    async def test_help_is_short_and_separate_from_status(self):
        result = self.reply(await self.ask("大肥鱼 帮助"))
        for word in ("帮助", "设置", "记忆", "重置"):
            self.assertIn(word, result)
        self.assertNotIn("搭话记录", result)
        self.assertNotIn("今日偷吃", result)
        self.assertLessEqual(len(result.splitlines()), 10)
        self.assertFalse(self.requests)

    async def test_help_and_settings_work_in_disabled_group(self):
        self.listener._state["enabled_groups"] = []
        for command in ("大肥鱼 帮助", "大肥鱼 设置", "鲸鱼娘 状态"):
            with self.subTest(command=command):
                result = self.reply(await self.ask(command, group=True, room="room-A"))
                self.assertTrue(result)
                self.assertIn("本群", result)
                self.assertFalse(self.requests)
        self.assertEqual(self.listener._state["enabled_groups"], [])

    async def test_profile_is_applied_to_current_group_only(self):
        result = self.reply(await self.ask("大肥鱼 设置 安静", group=True, room="room-A"))
        self.assertIn("本群", result)
        self.assertIn("安静", result)
        other = self.reply(await self.ask("大肥鱼 设置", group=True, room="room-B"))
        self.assertNotIn("档位：安静", other)
        self.assertNotEqual(self.listener._state.get("group_interject_enabled"), False)

    async def test_private_profile_does_not_change_another_user(self):
        result = self.reply(await self.ask("大肥鱼 设置 安静"))
        self.assertIn("当前私聊", result)
        self.assertIn("档位：安静", result)
        other = self.reply(await self.ask("大肥鱼 设置", sender="wxid_other", room="wxid_other"))
        self.assertNotIn("档位：安静", other)

    async def test_setting_alias_changes_current_group_not_global(self):
        self.listener._state["group_interject_enabled"] = True
        result = self.reply(await self.ask("大肥鱼 关闭群搭话", group=True, room="room-A"))
        self.assertIn("本群", result)
        self.assertTrue(self.listener._state["group_interject_enabled"])
        other = self.reply(await self.ask("大肥鱼 设置", group=True, room="room-B"))
        self.assertIn("主动插话：开", other)

    async def test_profile_does_not_change_voice_mode_or_moments(self):
        self.listener._state.update({"voice_reply_enabled": True, "mode": "sweet", "auto_moments_enabled": False})
        await self.ask("大肥鱼 设置 安静", group=True, room="room-A")
        result = self.reply(await self.ask("大肥鱼 设置 高级", group=True, room="room-A"))
        self.assertIn("语音：开", result)
        self.assertIn("甜宠", result)
        self.assertFalse(self.listener._state["auto_moments_enabled"])

    async def test_quiet_profile_blocks_unprompted_continuation(self):
        await self.ask("大肥鱼 设置 安静", group=True, room="room-A")
        self.listener._group_active_conv["room-A"] = {"last_speaker_id": "wxid_test", "last_bot_ts": time.time()}
        judge = self._patch(df, "evaluate_group_active_continuation", AsyncMock(return_value=(False, "", "", 0)))
        await self.ask("接着刚才的话题", group=True, room="room-A")
        judge.assert_not_awaited()
        self.assertFalse(self.requests)

    async def test_quiet_profile_blocks_automatic_shared_link_reply(self):
        await self.ask("大肥鱼 设置 安静", group=True, room="room-A")
        parser = self._patch(df, "extract_and_parse_link", AsyncMock(return_value=("分享链接", None)))
        await self.ask("https://www.bilibili.com/video/BV1234567890", group=True, room="room-A")
        parser.assert_not_awaited()
        self.assertFalse(self.requests)

    async def test_quiet_profile_still_answers_explicit_wake(self):
        await self.ask("大肥鱼 设置 安静", group=True, room="room-A")
        await self.ask("大肥鱼 今天怎么样", group=True, room="room-A")
        self.assertTrue(self.requests)

    async def test_balanced_profile_does_not_repeat_group_messages(self):
        await self.ask("大肥鱼 设置 均衡", group=True, room="room-A")
        self._patch(df, "evaluate_group_interject", AsyncMock(return_value=(False, "", "", 0)))
        direct = self._patch(self.listener, "_reply_group_no_at", AsyncMock())
        with patch.object(df.random, "random", return_value=0.0):
            await self.ask("大家好", group=True, room="room-A", sender="wxid_A")
            await self.ask("大家好", group=True, room="room-A", sender="wxid_B")
        direct.assert_not_awaited()

    async def test_lively_profile_can_repeat_group_messages(self):
        await self.ask("大肥鱼 设置 热闹", group=True, room="room-A")
        self._patch(df, "evaluate_group_interject", AsyncMock(return_value=(False, "", "", 0)))
        direct = self._patch(self.listener, "_reply_group_no_at", AsyncMock())
        with patch.object(df.random, "random", return_value=0.0):
            await self.ask("大家好", group=True, room="room-A", sender="wxid_A")
            await self.ask("大家好", group=True, room="room-A", sender="wxid_B")
        direct.assert_awaited_once()

    async def test_enable_and_disable_are_conversation_scoped(self):
        await self.ask("大肥鱼 设置 关闭", group=True, room="room-A")
        self.assertNotIn("room-A", self.listener._state["enabled_groups"])
        self.assertIn("room-B", self.listener._state["enabled_groups"])
        await self.ask("鲸鱼娘 本群开启", group=True, room="room-A")
        self.assertIn("room-A", self.listener._state["enabled_groups"])
        await self.ask("大肥鱼 关闭")
        self.assertTrue(self.listener._state["enabled"])
        await self.ask("普通私聊")
        self.assertFalse(self.requests)
        await self.ask("普通私聊", sender="wxid_other", room="wxid_other")
        self.assertTrue(self.requests)

    async def test_invalid_setting_does_not_call_model(self):
        before = json.dumps(self.listener._state, sort_keys=True)
        result = self.reply(await self.ask("大肥鱼 设置 不存在的档位"))
        self.assertIn("安静", result)
        self.assertFalse(self.requests)
        self.assertEqual(json.dumps(self.listener._state, sort_keys=True), before)

    async def test_non_owner_cannot_enable_global_auto_moments(self):
        self.listener._state["auto_moments_enabled"] = False
        result = self.reply(await self.ask("大肥鱼 开启自动朋友圈"))
        self.assertFalse(self.listener._state["auto_moments_enabled"])
        self.assertIn("主人", result)

    async def test_owner_can_enable_auto_moments_in_private(self):
        prior = self.listener._get_config_str
        self.listener._get_config_str = lambda key, default: "wxid_test" if key == "owner_ids" else prior(key, default)
        self.listener._state["auto_moments_enabled"] = False
        result = self.reply(await self.ask("大肥鱼 设置 自动朋友圈 开启"))
        self.assertTrue(self.listener._state["auto_moments_enabled"])
        self.assertIn("全局", result)

    async def test_group_cannot_change_global_moments(self):
        prior = self.listener._get_config_str
        self.listener._get_config_str = lambda key, default: "wxid_test" if key == "owner_ids" else prior(key, default)
        self.listener._state["auto_moments_enabled"] = False
        result = self.reply(await self.ask("大肥鱼 开启自动朋友圈", group=True, room="room-A"))
        self.assertFalse(self.listener._state["auto_moments_enabled"])
        self.assertIn("私聊", result)

    async def test_new_install_does_not_auto_publish_moments(self):
        with patch.object(wl.os.path, "exists", return_value=False):
            state = wl.WhaleListener._load_persistent_state(self.listener)
        self.assertFalse(state["auto_moments_enabled"])

    async def test_lively_profile_judges_after_four_plain_messages(self):
        judge = self._patch(df, "evaluate_group_interject", AsyncMock(return_value=(False, "", "", 0)))
        await self.ask("大肥鱼 设置 热闹", group=True, room="room-A")
        for index in range(4):
            await self.ask(f"甲乙丙丁{index}", group=True, room="room-A")
        judge.assert_awaited_once()

    async def test_balanced_profile_waits_for_eight_plain_messages(self):
        judge = self._patch(df, "evaluate_group_interject", AsyncMock(return_value=(False, "", "", 0)))
        await self.ask("大肥鱼 设置 均衡", group=True, room="room-A")
        for index in range(4):
            await self.ask(f"甲乙丙丁{index}", group=True, room="room-A")
        judge.assert_not_awaited()

    async def test_group_profiles_use_separate_interjection_cooldowns(self):
        judge = self._patch(df, "evaluate_group_interject", AsyncMock(return_value=(False, "", "", 0)))
        for profile, room, calls in (("均衡", "room-A", 0), ("热闹", "room-B", 1)):
            with self.subTest(profile=profile):
                await self.ask(f"大肥鱼 设置 {profile}", group=True, room=room)
                self.listener._group_interject_meta[room] = {
                    "last_judge_ts": time.time() - 130, "unjudged_count": 7,
                }
                judge.reset_mock()
                await self.ask("甲乙丙丁", group=True, room=room)
                self.assertEqual(judge.await_count, calls)

    async def test_enabling_group_interject_leaves_quiet_explicit_only_gate(self):
        judge = self._patch(df, "evaluate_group_interject", AsyncMock(return_value=(False, "", "", 0)))
        await self.ask("大肥鱼 设置 安静", group=True, room="room-A")
        await self.ask("大肥鱼 设置 群搭话 开启", group=True, room="room-A")
        for index in range(8):
            await self.ask(f"甲乙丙丁{index}", group=True, room="room-A")
        judge.assert_awaited_once()

    async def test_private_voice_switch_only_changes_current_user(self):
        self.listener._state["voice_reply_enabled"] = True
        self.model_reply = "今天过得还不错呀"
        voice = self._patch(df, "synthesize_and_send_wcf_voice", AsyncMock(return_value=False))
        await self.ask("大肥鱼 设置 语音 关闭")
        with patch.object(wl.random, "random", return_value=0.0):
            await self.ask("今天怎么样")
            voice.assert_not_awaited()
            await self.ask("今天怎么样", sender="wxid_other", room="wxid_other")
        voice.assert_awaited_once()
        self.assertTrue(self.listener._state["voice_reply_enabled"])

    async def test_manual_voice_command_is_still_available(self):
        voice = self._patch(df, "synthesize_and_send_wcf_voice", AsyncMock(return_value=True))
        await self.ask("鲸鱼娘 语音 你好呀")
        voice.assert_awaited_once()
        self.assertEqual(voice.await_args.args, ("wxid_test", "你好呀"))
        self.assertFalse(self.requests)

    async def test_background_speak_up_respects_private_profile(self):
        await self.ask("大肥鱼 设置 安静")
        last_chat = int(time.time() * 1000) - 3600000
        self.listener._state["users"] = {
            "wxid_test": {"hasPrivateChat": True, "lastChatAt": last_chat},
            "wxid_other": {"hasPrivateChat": True, "lastChatAt": last_chat},
        }
        self.listener._trigger_speak_up_for_user = AsyncMock()
        await harness.ListenerRegressions.one_loop_iteration(self, self.listener._speak_up_loop)
        self.listener._trigger_speak_up_for_user.assert_awaited_once_with("wxid_other", manual=False)

    async def test_group_mode_alias_does_not_change_private_persona(self):
        await self.ask("鲸鱼娘 甜宠版", group=True, room="room-A")
        group = self.reply(await self.ask("大肥鱼 设置 高级", group=True, room="room-A"))
        private = self.reply(await self.ask("大肥鱼 设置 高级"))
        self.assertIn("人格：甜宠", group)
        self.assertIn("人格：活人", private)

    async def test_legacy_repeat_parameters_are_group_scoped(self):
        result = self.reply(await self.ask("大肥鱼 复读设置 3人 15% 90秒", group=True, room="room-A"))
        self.assertIn("本群", result)
        current = self.reply(await self.ask("大肥鱼 设置 高级", group=True, room="room-A"))
        other = self.reply(await self.ask("大肥鱼 设置 高级", group=True, room="room-B"))
        self.assertIn("3人 15% 90秒", current)
        self.assertIn("2人 30% 60秒", other)

    async def test_settings_survive_save_and_reload_with_legacy_defaults(self):
        self.listener._state["auto_moments_enabled"] = True
        self.listener._state["voice_reply_enabled"] = True
        await self.ask("大肥鱼 设置 安静", group=True, room="room-A")
        with tempfile.TemporaryDirectory() as directory, patch.object(wl, "STATE_FILE", str(Path(directory) / "state.json")):
            wl.WhaleListener._save_persistent_state(self.listener)
            loaded = wl.WhaleListener._load_persistent_state(self.listener)
        self.listener._state = loaded
        current = self.reply(await self.ask("大肥鱼 设置 高级", group=True, room="room-A"))
        self.assertIn("档位：安静", current)
        self.assertIn("语音：开", current)
        self.assertTrue(loaded["auto_moments_enabled"])
        self.assertTrue(loaded["voice_reply_enabled"])

    async def test_persona_aliases_and_toggle_mode_commands(self):
        session_key = "person:wxid_test:wxid_test"
        self.listener._histories[session_key] = [(time.time(), "assistant", "毒舌旧回复")]
        res1 = self.reply(await self.ask("大肥鱼 设置 人格 甜宠"))
        self.assertIn("人格：甜宠", res1)
        self.assertIn("档位：沿用原设置", res1)
        self.assertNotIn(session_key, self.listener._histories)
        res2 = self.reply(await self.ask("大肥鱼 设置 人格 毒舌"))
        self.assertIn("人格：活人", res2)
        res3 = self.reply(await self.ask("大肥鱼 切换模式"))
        self.assertIn("人格：甜宠", res3)
        res4 = self.reply(await self.ask("大肥鱼 切换人格"))
        self.assertIn("人格：活人", res4)


if __name__ == "__main__":
    unittest.main()
