"""会话设置、交互档位和兼容口令的唯一入口。"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class Switch:
    key: str
    label: str
    aliases: tuple[str, ...]
    scope: str = "conversation"


SWITCHES = (
    Switch("enabled", "回应", ("", "回应")),
    Switch("speak_up_enabled", "主动搭话", ("主动搭话", "搭话"), "person"),
    Switch("group_interject_enabled", "主动插话", ("群搭话", "主动插话"), "group"),
    Switch("continuation_enabled", "连续接话", ("连续接话",), "group"),
    Switch("repeat_enabled", "复读", ("复读",), "group"),
    Switch("meme_enabled", "表情", ("表情",)),
    Switch("voice_reply_enabled", "语音", ("语音",)),
    Switch("steal_meme_enabled", "偷图", ("偷图",)),
    Switch("auto_moments_enabled", "自动朋友圈", ("自动朋友圈",), "global"),
)
DEFAULTS: dict[str, Any] = {
    **{item.key: True for item in SWITCHES if item.scope != "global"},
    "mode": "live", "explicit_only": False,
    "repeat_config": {"min_users": 2, "prob": 0.30, "cooldown_sec": 60},
    "interject_cooldown_sec": 240, "interject_min_messages": 8,
}
PROFILES = {
    "安静": {
        "speak_up_enabled": False, "group_interject_enabled": False,
        "continuation_enabled": False, "repeat_enabled": False, "explicit_only": True,
    },
    "均衡": {
        "speak_up_enabled": True, "group_interject_enabled": True,
        "continuation_enabled": True, "repeat_enabled": False, "explicit_only": False,
        "interject_cooldown_sec": 240, "interject_min_messages": 8,
    },
    "热闹": {
        "speak_up_enabled": True, "group_interject_enabled": True,
        "continuation_enabled": True, "repeat_enabled": True, "explicit_only": False,
        "interject_cooldown_sec": 120, "interject_min_messages": 4,
    },
}

# action, key, value, scope；菜单和旧别名使用同一份设置定义。
ALIASES: dict[str, tuple[str, str, Any, str]] = {
    "": ("help", "", None, "conversation"),
    "帮助": ("help", "", None, "conversation"),
    "状态": ("view", "", None, "conversation"),
    "高级": ("advanced", "", None, "conversation"),
    "本群开启": ("set", "enabled", True, "group"),
    "本群关闭": ("set", "enabled", False, "group"),
    "开启本群": ("set", "enabled", True, "group"),
    "关闭本群": ("set", "enabled", False, "group"),
    "本群开启搭话": ("set", "group_interject_enabled", True, "group"),
    "本群关闭搭话": ("set", "group_interject_enabled", False, "group"),
    "切换": ("set", "mode", "toggle", "conversation"),
    "切换模式": ("set", "mode", "toggle", "conversation"),
    "切换人格": ("set", "mode", "toggle", "conversation"),
}
for item in SWITCHES:
    for alias in item.aliases:
        for word, value in (("开启", True), ("关闭", False), ("开", True), ("关", False)):
            ALIASES[f"{word}{alias}"] = ("set", item.key, value, item.scope)
            if alias:
                ALIASES[f"{alias} {word}"] = ("set", item.key, value, item.scope)
for label, value in (("活人", "live"), ("毒舌", "live"), ("甜宠", "sweet")):
    for alias in (f"模式 {label}", f"人格 {label}", f"{label}版", f"{label}模式"):
        ALIASES[alias] = ("set", "mode", value, "conversation")


def scope_key(is_group: bool, target_id: str) -> str:
    return f"{'group' if is_group else 'person'}:{target_id}"


def effective_settings(state: dict[str, Any], scope: str) -> dict[str, Any]:
    """旧全局配置只作为默认值；读取不会创建档案或改变原状态。"""
    settings = {key: state.get(key, default) for key, default in DEFAULTS.items()}
    overrides = (state.get("conversation_settings") or {}).get(scope) or {}
    settings.update(overrides)
    settings["profile"] = overrides.get("profile", "自定义" if overrides else "沿用原设置")
    return settings


def parse_control(text: str) -> tuple[str, str, Any, str] | None:
    if text in ("开启大肥鱼", "关闭大肥鱼"):
        return "set", "enabled", text.startswith("开启"), "conversation"
    match = re.fullmatch(r"(?:大肥鱼|鲸鱼娘|小鲸鱼|鲸鱼人设)(?:\s+(.*))?", text)
    if not match:
        return None
    body = (match.group(1) or "").strip()
    is_settings = body == "设置" or body.startswith("设置 ")
    if is_settings:
        body = body[2:].strip()
        if not body:
            return "view", "", None, "conversation"
        if body in PROFILES:
            return "profile", "", body, "conversation"
    if body in ALIASES:
        return ALIASES[body]
    repeat = re.fullmatch(r"(?:复读设置|复读参数)\s+(.+)", body)
    if repeat:
        nums = re.findall(r"\d+", repeat.group(1))
        if len(nums) == 3:
            values = {
                "min_users": max(2, min(10, int(nums[0]))),
                "prob": max(1, min(100, int(nums[1]))) / 100.0,
                "cooldown_sec": max(5, min(3600, int(nums[2]))),
            }
            return "set", "repeat_config", values, "group"
    if is_settings or repeat:
        return "invalid", "", None, "conversation"
    return None


def help_text(is_group: bool, enabled: bool) -> str:
    lines = [
        "🐋 大肥鱼 · 常用入口",
        "大肥鱼 帮助：查看用法",
        "大肥鱼 设置：查看状态、选择安静 / 均衡 / 热闹",
        "大肥鱼 记忆：查看当前聊天的记忆",
        "大肥鱼 重置：清除当前对话，保留长期记忆",
        "平时直接说：来首歌 晴天 / 用语音回我 / 帮我查一下……",
        "也可以直接发图片、文件或链接；更多功能在「设置 高级」",
    ]
    if is_group and not enabled:
        lines.append("本群尚未启用：大肥鱼 设置 开启")
    return "\n".join(lines)


def settings_text(state: dict[str, Any], scope: str, *, advanced: bool = False) -> str:
    cfg = effective_settings(state, scope)
    is_group = scope.startswith("group:")
    active = cfg["enabled"] and (not is_group or scope[6:] in state.get("enabled_groups", []))
    def flag(value: bool) -> str:
        return "开" if value else "关"
    lines = [f"⚙️ {'本群' if is_group else '当前私聊'}设置", f"回应：{flag(active)} | 档位：{cfg['profile']}"]
    if is_group:
        lines.append(f"主动插话：{flag(cfg['group_interject_enabled'])} | 连续接话：{flag(cfg['continuation_enabled'])} | 复读：{flag(cfg['repeat_enabled'])}")
    else:
        lines.append(f"主动搭话：{flag(cfg['speak_up_enabled'])}")
    if not advanced:
        lines.extend(["切换：大肥鱼 设置 安静 / 均衡 / 热闹", "更多：大肥鱼 设置 高级"])
        if not active:
            lines.append("启用：大肥鱼 设置 开启")
        return "\n".join(lines)
    lines.append(f"人格：{'活人' if cfg['mode'] == 'live' else '甜宠'}")
    for item in SWITCHES:
        if item.scope == "global":
            lines.append(f"{item.label}：{flag(state.get(item.key, False))}（全局，仅主人私聊修改）")
        elif item.key != "enabled" and item.scope in ("conversation", "group" if is_group else "person"):
            lines.append(f"{item.label}：{flag(cfg[item.key])}")
    lines.extend([
        "调整：大肥鱼 设置 <项目> 开启/关闭；大肥鱼 设置 模式 活人/甜宠",
        "查询：大肥鱼 心情 / 粮仓 / 搭话记录 / 表情库",
        "记忆维护：大肥鱼 记住 <内容> / 整理档案 / 失忆",
        "点播：大肥鱼 语音 <内容> / 表情 <名称> / 点歌 <歌名>",
        "动态：大肥鱼 发朋友圈 [主题] / 朋友圈 / 刷朋友圈 / 点赞",
    ])
    if is_group:
        repeat = cfg["repeat_config"]
        lines.append(f"复读参数：{repeat['min_users']}人 {round(repeat['prob'] * 100)}% {repeat['cooldown_sec']}秒；可用「大肥鱼 复读设置 2人 30% 60秒」调整")
    return "\n".join(lines)


def handle_control(
    state: dict[str, Any], text: str, *, is_group: bool, target_id: str,
    sender_id: str, owner_ids: str = "",
) -> tuple[str, bool] | None:
    """返回（回复、是否修改状态）；普通聊天返回 None，不调用模型。"""
    command = parse_control(text)
    if command is None:
        return None
    action, key, value, required_scope = command
    scope = scope_key(is_group, target_id)
    if action == "help":
        enabled = not is_group or target_id in state.get("enabled_groups", [])
        return help_text(is_group, enabled), False
    if action in ("view", "advanced"):
        return settings_text(state, scope, advanced=action == "advanced"), False
    if action == "invalid":
        return "设置可选：安静 / 均衡 / 热闹 / 高级；项目开关示例：大肥鱼 设置 语音 关闭", False
    if required_scope in ("group", "person") and (required_scope == "group") != is_group:
        return f"这项设置只支持{'群聊' if required_scope == 'group' else '私聊'}，请在对应聊天中修改", False
    if required_scope == "global":
        if is_group:
            return "自动朋友圈是全局设置，请由主人在私聊中修改", False
        owners = set(re.split(r"[,，;；\s]+", owner_ids.strip())) - {""}
        if sender_id not in owners:
            return "仅主人可修改全局自动朋友圈，请先在插件配置 owner_ids 中填写主人账号 ID", False
        state[key] = value
        return f"已更新全局设置：自动朋友圈{'开启' if value else '关闭'}", True
    overrides = state.setdefault("conversation_settings", {}).setdefault(scope, {})
    if action == "profile":
        overrides.update(PROFILES[value])
        overrides["profile"] = value
    else:
        if key == "mode" and value == "toggle":
            value = "sweet" if effective_settings(state, scope)["mode"] == "live" else "live"
        overrides[key] = value
        if value is True and key in ("group_interject_enabled", "continuation_enabled", "repeat_enabled"):
            overrides["explicit_only"] = False
        if key in {k for profile in PROFILES.values() for k in profile}:
            overrides.pop("profile", None)
        if key == "enabled" and is_group:
            groups = state.setdefault("enabled_groups", [])
            if value and target_id not in groups:
                groups.append(target_id)
            elif not value and target_id in groups:
                groups.remove(target_id)
    return "已更新" + settings_text(state, scope), True
