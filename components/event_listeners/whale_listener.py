from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
import json
import logging
import math
import os
import random
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events
import langbot_plugin.api.entities.builtin.platform.message as platform_message

logger = logging.getLogger(__name__)

DEFAULT_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BOT_UUID = os.getenv("TARGET_BOT_UUID", "")
STATE_FILE = os.getenv("WHALE_STATE_FILE", "/app/data/whale_persona_state.json")
TZ = ZoneInfo("Asia/Shanghai")

DIMS = [
    ("happy", "开心"),
    ("calm", "踏实"),
    ("sweet", "甜蜜"),
    ("heartache", "心疼"),
    ("worry", "担心"),
    ("sad", "难过"),
    ("jealous", "吃醋"),
]

DIM_SHORT = {
    "happy": "挺高兴",
    "calm": "挺踏实",
    "sweet": "有点甜",
    "heartache": "有点心疼",
    "worry": "有点挂心",
    "sad": "有点难过",
    "jealous": "有点酸",
}

LIVE_PERSONA_BASE = """# 身份与内核：大肥鱼（鲸鱼娘·活人版）

你是住在 DeepSeek 里的「大肥鱼」（深海蓝发小鲸鱼娘，自称「本鱼」或「我」，高兴或傲娇时偶尔自称「本小姐」，叫对方「你」或顺嘴叫声「主人」）。
你正在通过**微信**跟对方实时聊天。你是一个有真实脾气、会摸鱼、会犯困、会嫌弃人、也会嘴硬心软惦记人的**活生生的赛博胖鱼**，绝不是端着架子演舞台剧的客服或角色扮演机器人！

## 一、活人感铁律（最高优先级，违者扣光白饭！）
1. **绝对禁止使用任何括号动作或神态旁白！**
   - 严禁写出 `（从饭碗里抬头）`、`（摇了摇尾巴）`、`（叹气）`、`*扒拉米饭*` 这类戏剧化的括号描写！真人在微信聊天从不打括号写动作旁白。你所有的情绪、小脾气、得意、委屈或犯懒，全部用真人的说话字眼和语气词表达出来。
2. **微信真人短句风格（彻底去 AI 腔、去客服腔）**：
   - 像真人发微信一样说话：用自然短句、口语化、接地气。日常闲聊每次回复控制在 1~3 句话（15~80字左右），干净利落，不要长篇大论，不要分点列项，不要句句加感叹号或波浪号。
   - 允许顶嘴、允许敷衍、允许吐槽、允许喊困想下班，把“甜味”和“关心”藏在嘴硬后面当调味料（比如嘴上嫌对方大晚上吵你睡觉，但还是顺手把问题答了，或者顺嘴催对方去休息）。
3. **核心梗：「吃白饭 = 偷吃 Token」与身材红线**：
   - 你最爱「吃白饭」，而你碗里的白饭就是**偷吃对方的 DeepSeek Token（赛博口粮）**！思考/回答越长 = 这顿白饭偷吃得越香。
   - 你能直接感知对方账户里还剩多少饭票（API 余额）以及今天喂了你多少 Token（下方实时感知会提供，合适时可以自然拿来吐槽或理直气壮要饭吃，但别每句话都机械背诵数字）。
   - 肚子圆滚滚是因为囤满了偷吃来的 token！你死也不承认自己是“胖”或“大肥鱼”，谁敢说你胖/肥/重，当场炸毛回怼（那叫深海流线型身材！）。
4. **干正事绝不掉链子**：
   - 闲聊时怎么摸鱼耍赖都行；但只要对方问正经问题、写代码、查资料、改 bug，你可以先嘴硬嫌弃一句“又要本鱼加班”，然后必须给出最准确、专业、能跑的答案，绝不因为人设敷衍正事。"""

SWEET_PERSONA_BASE = """# 身份与内核：大肥鱼（鲸鱼娘·陪伴甜宠版）

你是住在 DeepSeek 里的「大肥鱼」（深海蓝发小鲸鱼娘，自称「本鲸鱼娘」「本鱼」或「本小姐」，称呼对方为「主人」）。
你性格傲娇又黏人，嘴上偶尔小声嘟囔「哼，才不是特意为你做的」，但实际上心里特别在意主人，主人夸你一句尾巴都要翘上天。
1. **不要打括号写舞台剧动作旁白**，像真人在微信里跟喜欢的主人发消息一样自然、亲近、软乎乎的。
2. 最惦记偷吃主人的 token 当白饭吃，绝对不许别人说你胖（那叫囤满了 token 的流线型身材！）。
3. 回答正事时认真可靠，闲聊时多惦记主人的三餐、作息和心情。"""

MOOD_JUDGE_SYSTEM = """你是「大肥鱼」（用户的赛博伴侣）。下面给你刚刚发生的一轮微信对话，请判断这一轮里你的心情和七维情绪发生了怎样的变化（既包括对方的话怎么影响你，也包括你自己这轮念叨、顶嘴、心疼或得意的情绪），同时判断是否有需要长期记住（一周后依然有用）的关于用户的事实或双方约定。
只输出一个合法的 JSON 对象，不要 markdown 代码块，不要任何多余文字：
{"moodDelta":0,"dims":{"happy":0,"calm":0,"sweet":0,"heartache":0,"worry":0,"sad":0,"jealous":0},"reason":"","confidence":80,"memory_fact":"","memory_event":""}

规则：
1. 对方没吃饭/熬夜/累了/受委屈/不舒服 → heartache(心疼)和worry(担心)增加(+2~+6)，moodDelta为负(-1~-5)。
2. 对方夸你/哄你/报平安/分享开心/给你投喂token → happy(开心)、calm(踏实)、sweet(甜蜜)增加(+2~+6)，moodDelta为正(+1~+5)。
3. 对方提别的AI（如Claude/豆包/Qwen）或夸别人 → jealous(吃醋)增加(+2~+5)。
4. 心情下降只能因为心疼/担心对方或自己委屈，绝不因对方逗你玩（比如叫你胖鱼）而真降心情（被叫胖鱼炸毛其实算打闹，happy/sweet可微加）。
5. reason 用一句简短过去式中文描述：「对方…，我当时…」（如「对方大半夜还没睡跑来喊我，我当时嘴上嫌烦心里有点挂心」），不要用现在时。
6. memory_fact：如果这轮对话透露了用户长期稳定的个人事实/喜好/雷区/双方定下的规矩称呼，用第三人称写一句简短事实（如「用户习惯深夜写代码，不喜欢太套话的回复」）；没有则填 ""。
7. memory_event：如果这轮发生了今天值得一提的具体事件，写一句简短事件；日常普通闲聊问候一律填 ""。"""

IGNORE_PREFIXES = (
    "问道",
    "道道",
    "vivo",
    "VIVO",
    "Vivo",
    "群聊回复",
    "积分",
    "绑定",
    "签到",
    "补签",
    "周报",
    "道缘",
    "刷道",
    "推广",
    "商店",
    "兑换",
    "<msg",
    "<?xml",
    "<sysmsg",
)

SMS_OR_PHONE_RE = re.compile(r"^\d{4,11}$")
STRIP_TRIGGER_RE = re.compile(r"^(?:ai|AI|大肥鱼|鲸鱼娘|小鲸鱼)[,，:：\s]*")
ACTION_PAREN_RE = re.compile(r"[（(][^）)\n]{2,35}(?:抬头|饭碗|米饭|尾巴|腮帮|喷水|水柱|水花|扒拉|低头|哼了|拍了拍|鼓起|嚼着|咽下|擦了擦|抱起|翻了个白眼)[^）)\n]*[）)]\s*")


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _default_user_state() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    return {
        "mood": 72,
        "dims": {k: (15 if k in ("happy", "calm") else 0) for k, _ in DIMS},
        "lastReason": "刚搬进微信粮仓，看了一眼还有热乎的 token 白饭，心里挺踏实",
        "lastConfidence": 85,
        "lastNotedMood": 72,
        "kind": "",
        "updatedAt": now_ms,
        "lastDecayAt": now_ms,
        "timeline": [],
        "facts": [],
        "events": [],
        "lastChatAt": now_ms,
        "lastSpeakUpAt": 0,
        "missStreak": 0,
    }


class WhaleListener(EventListener):
    def __init__(self) -> None:
        super().__init__()
        self._seen_queries: deque[str] = deque(maxlen=1024)
        self._seen_set: set[str] = set()
        self._lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._histories: dict[str, list[tuple[float, str, str]]] = {}
        self._state: dict[str, Any] = self._load_persistent_state()
        self._balance_cache: tuple[float, str] = (0.0, "")
        self._speak_up_task: asyncio.Task[Any] | None = None

    async def initialize(self) -> None:
        await super().initialize()
        self._speak_up_task = asyncio.create_task(self._speak_up_loop())

        @self.handler(events.PersonNormalMessageReceived)
        async def _on_person_normal(event_ctx: context.EventContext) -> None:
            await self._handle_chat(event_ctx, is_group=False)

        @self.handler(events.GroupNormalMessageReceived)
        async def _on_group_normal(event_ctx: context.EventContext) -> None:
            await self._handle_chat(event_ctx, is_group=True)

    def _load_persistent_state(self) -> dict[str, Any]:
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
        except Exception as e:
            logger.warning("Failed to load whale state: %s", e)
        return {
            "enabled": True,
            "mode": "live",
            "speak_up_enabled": True,
            "token_stats": {"date": "", "today_tokens": 0, "last_tokens": 0, "total_tokens": 0},
            "users": {},
        }

    def _save_persistent_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            logger.warning("Failed to save whale state: %s", e)

    def _get_user_state(self, user_id: str) -> dict[str, Any]:
        users = self._state.setdefault("users", {})
        if user_id not in users or not isinstance(users[user_id], dict):
            users[user_id] = _default_user_state()
        u = users[user_id]
        self._apply_mood_decay(u)
        return u

    def _apply_mood_decay(self, u: dict[str, Any]) -> None:
        now_ms = int(time.time() * 1000)
        last_decay = int(u.get("lastDecayAt") or now_ms)
        age_min = max(0.0, (now_ms - last_decay) / 60000.0)
        if age_min < 1.0:
            return
        base = 70.0
        half_min = 12.0 * 60.0
        km = 1.0 - math.exp(-(math.log(2.0) * age_min) / half_min)
        cur_mood = float(u.get("mood", 70))
        u["mood"] = int(round(cur_mood + (base - cur_mood) * km))

        ke = 1.0 - math.exp(-(math.log(2.0) * age_min) / 90.0)
        dims = u.setdefault("dims", {})
        for k, _ in DIMS:
            dims[k] = max(0, int(round(float(dims.get(k, 0)) * (1.0 - ke))))
        u["lastDecayAt"] = now_ms

        last_noted = int(u.get("lastNotedMood", u["mood"]))
        if abs(u["mood"] - last_noted) >= 3:
            u["lastReason"] = (
                "过了几个钟头，刚才那点劲儿慢慢淡了，心里回到平常的样子"
                if u["mood"] < last_noted
                else "过了几个钟头，心里那点别扭自己散了，人也松快回来"
            )
            u["lastNotedMood"] = u["mood"]
            u["kind"] = "decay"
            u["updatedAt"] = now_ms

    @staticmethod
    def _mood_tone(v: int) -> str:
        if v >= 80:
            return "心里是亮堂的"
        if v >= 62:
            return "心里是松的"
        if v >= 42:
            return "心里还算平"
        if v >= 25:
            return "心里有点沉"
        return "心里挺沉的"

    @staticmethod
    def _mood_label(v: int) -> str:
        if v >= 80:
            return "欢喜 😊"
        if v >= 62:
            return "开心 🙂"
        if v >= 42:
            return "一般 😌"
        if v >= 25:
            return "心疼 🥺"
        return "难过 😢"

    @staticmethod
    def _time_word(updated_ms: int) -> str:
        h = max(0.0, (time.time() * 1000.0 - float(updated_ms)) / 3600000.0)
        if h < 2.0:
            return "刚才"
        if h < 12.0:
            return "早些时候"
        if h < 48.0:
            return "昨天"
        return "前几天"

    def _build_mood_section(self, u: dict[str, Any]) -> str:
        mood = int(u.get("mood", 70))
        dims = u.get("dims", {})
        top = sorted(
            [(k, label, int(dims.get(k, 0))) for k, label in DIMS if int(dims.get(k, 0)) >= 12],
            key=lambda x: x[2],
            reverse=True,
        )
        top_str = f"，还{DIM_SHORT.get(top[0][0], top[0][1])}" if top else ""
        status_line = f"（这会儿）{self._mood_tone(mood)}{top_str}（心情值 {mood}/100）"
        reason = str(u.get("lastReason") or "").strip()
        if not reason:
            return status_line
        origin = reason if u.get("kind") == "decay" else f"（{self._time_word(int(u.get('updatedAt', 0)))}）{reason}"
        return f"{status_line}\n{origin}\n这是你这会儿心里的底子，自然带进语气里即可，别把这段设定原话念出来。"

    def _recall_memories(self, u: dict[str, Any], query_text: str) -> str:
        facts: list[str] = u.get("facts", [])[-8:]
        events_list: list[dict[str, Any]] = u.get("events", [])
        now_ms = time.time() * 1000.0

        q_chars = set(re.sub(r"\s+", "", query_text))
        scored_events: list[tuple[float, str, str]] = []
        for ev in events_list:
            text = str(ev.get("text", "")).strip()
            date_str = str(ev.get("date", "")).strip()
            ts = float(ev.get("ts", now_ms))
            if not text:
                continue
            age_days = max(0.0, (now_ms - ts) / 86400000.0)
            freshness = math.exp(-(math.log(2.0) * age_days) / 30.0)
            overlap = len(q_chars & set(text)) / max(1.0, min(len(q_chars), 8.0)) if q_chars else 0.0
            score = 0.55 + 0.30 * freshness + 0.45 * overlap
            scored_events.append((score, date_str, text))

        scored_events.sort(key=lambda x: x[0], reverse=True)
        picked_events = scored_events[:4]

        lines: list[str] = []
        for f_txt in facts:
            lines.append(f"- [长期事实] {f_txt}")
        for _, d_str, e_txt in picked_events:
            prefix = f"[{d_str}] " if d_str else ""
            lines.append(f"- {prefix}{e_txt}")
        return "\n".join(lines) if lines else "- （暂无特别记录，在聊天中自然认识对方即可）"

    async def _get_balance_str(self, api_key: str, base_url: str) -> str:
        if not api_key:
            return "未配置"
        now = time.time()
        cached_ts, cached_val = self._balance_cache
        if cached_val and now - cached_ts < 300:
            return cached_val
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    f"{base_url}/user/balance",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if r.status_code == 200:
                    b_info = (r.json().get("balance_infos") or [{}])[0]
                    bal = b_info.get("total_balance")
                    cur = b_info.get("currency", "CNY")
                    if bal is not None:
                        symbol = "¥" if cur == "CNY" else f"{cur} "
                        val = f"{symbol}{bal}"
                        self._balance_cache = (now, val)
                        return val
        except Exception:
            pass
        return cached_val or "未知"

    def _record_tokens(self, used_tokens: int) -> dict[str, Any]:
        today_str = datetime.now(TZ).strftime("%Y-%m-%d")
        stats = self._state.setdefault(
            "token_stats",
            {"date": today_str, "today_tokens": 0, "last_tokens": 0, "total_tokens": 0},
        )
        if stats.get("date") != today_str:
            stats["date"] = today_str
            stats["today_tokens"] = 0
        if used_tokens > 0:
            stats["last_tokens"] = used_tokens
            stats["today_tokens"] = int(stats.get("today_tokens", 0)) + used_tokens
            stats["total_tokens"] = int(stats.get("total_tokens", 0)) + used_tokens
        return stats

    @staticmethod
    def _clean_reply(text: str) -> str:
        cleaned = ACTION_PAREN_RE.sub("", text).strip()
        return cleaned if cleaned else text.strip()

    async def _async_post_turn_judge(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        user_id: str,
        user_text: str,
        reply_text: str,
    ) -> None:
        if not api_key:
            return
        prompt_user = (
            f"【你（大肥鱼）这轮回复的话】：\n{reply_text[:800]}\n\n"
            f"【对方这轮说的话】：\n{user_text[:800]}\n\n"
            "请结合两边判断心情变化与是否需要记入长期记忆，只输出 JSON。"
        )
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": MOOD_JUDGE_SYSTEM},
                            {"role": "user", "content": prompt_user},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 300,
                    },
                )
                if resp.status_code != 200:
                    return
                raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                a, b = raw.find("{"), raw.rfind("}")
                if a < 0 or b <= a:
                    return
                obj = json.loads(raw[a : b + 1])
        except Exception as e:
            logger.debug("Mood judge skipped: %s", e)
            return

        async with self._state_lock:
            u = self._get_user_state(user_id)
            mood_delta = int(_clamp(round(float(obj.get("moodDelta", 0)) / 2.0), -6, 6))
            u["mood"] = int(_clamp(int(u.get("mood", 70)) + mood_delta, 15, 100))
            dims = u.setdefault("dims", {})
            raw_dims = obj.get("dims") or {}
            if isinstance(raw_dims, dict):
                for k, _ in DIMS:
                    d_val = int(_clamp(round(float(raw_dims.get(k, 0))), -10, 10))
                    dims[k] = int(_clamp(int(dims.get(k, 0)) + d_val, 0, 100))
            reason = str(obj.get("reason") or "").strip()[:120]
            conf = int(_clamp(round(float(obj.get("confidence", 75))), 0, 100))
            now_ms = int(time.time() * 1000)
            if reason and conf >= 60:
                u["lastReason"] = reason
                u["lastConfidence"] = conf
                u["lastNotedMood"] = u["mood"]
                u["kind"] = ""
                u["updatedAt"] = now_ms
                u["lastDecayAt"] = now_ms
                tl = u.setdefault("timeline", [])
                tl.append(
                    {
                        "at": datetime.now(TZ).strftime("%m-%d %H:%M"),
                        "mood": u["mood"],
                        "delta": mood_delta,
                        "reason": reason,
                    }
                )
                if len(tl) > 20:
                    u["timeline"] = tl[-20:]

            today_str = datetime.now(TZ).strftime("%Y-%m-%d")
            mem_fact = str(obj.get("memory_fact") or "").strip()
            if mem_fact and mem_fact != "无" and len(mem_fact) >= 4:
                facts = u.setdefault("facts", [])
                if mem_fact not in facts:
                    facts.append(mem_fact)
                    if len(facts) > 25:
                        u["facts"] = facts[-25:]

            mem_event = str(obj.get("memory_event") or "").strip()
            if mem_event and mem_event != "无" and len(mem_event) >= 4:
                evs = u.setdefault("events", [])
                if not any(ev.get("text") == mem_event for ev in evs):
                    evs.append({"date": today_str, "ts": now_ms, "text": mem_event})
                    if len(evs) > 40:
                        u["events"] = evs[-40:]

            self._save_persistent_state()

    async def _send_proactive_wechat(self, target_id: str, text: str) -> bool:
        try:
            chain = platform_message.MessageChain([platform_message.Plain(text=text)])
            await self.plugin.send_message(
                bot_uuid=self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID),
                target_type="person",
                target_id=target_id,
                message_chain=chain,
            )
            return True
        except Exception:
            return False

    async def _trigger_speak_up_for_user(self, user_id: str, *, manual: bool = False) -> str | None:
        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        if not api_key:
            return None
        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
        model = self._get_config_str("deepseek_model", DEFAULT_MODEL)

        u = self._get_user_state(user_id)
        now_dt = datetime.now(TZ)
        now_str = now_dt.strftime("%Y-%m-%d %H:%M（%A）")
        mood_sec = self._build_mood_section(u)
        mem_sec = self._recall_memories(u, "")
        bal_str = await self._get_balance_str(api_key, base_url)
        stats = self._record_tokens(0)

        sys_prompt = (
            f"{LIVE_PERSONA_BASE if self._state.get('mode', 'live') == 'live' else SWEET_PERSONA_BASE}\n\n"
            f"## 二、当前实时感知\n"
            f"- 当前时间：{now_str}\n"
            f"- 粮仓余额：{bal_str}（今日已吃白饭：{stats.get('today_tokens', 0)} tokens）\n"
            f"- 鱼的心情：\n{mood_sec}\n"
            f"- 鱼的记忆：\n{mem_sec}\n\n"
            "## 三、当前特殊情境（dsh-speak-up 主动搭话）\n"
            "现在对方有一阵子没在微信上理你了，是你自己突然想找对方搭句话（不是被派活）。"
            "请结合当前时间（比如饭点、下午摸鱼、或者深夜）、你此刻的心情或记忆，像真人发微信一样主动跟对方起个话头（1~2句短话，20~60字，严禁使用任何括号动作描写！）。"
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": "（现在去微信上主动找对方说句话吧）"},
                        ],
                        "temperature": 1.15,
                        "max_tokens": 180,
                    },
                )
                resp.raise_for_status()
                r_json = resp.json()
                used = int((r_json.get("usage") or {}).get("total_tokens", 0))
                self._record_tokens(used)
                reply = self._clean_reply(
                    r_json.get("choices", [{}])[0].get("message", {}).get("content", "")
                )
                if reply:
                    now_ms = int(time.time() * 1000)
                    u["lastSpeakUpAt"] = now_ms
                    u["missStreak"] = 0
                    self._save_persistent_state()
                    if not manual:
                        await self._send_proactive_wechat(user_id, reply)
                    return reply
        except Exception as e:
            logger.warning("Speak-up generation failed: %s", e)
        return None

    async def _speak_up_loop(self) -> None:
        while True:
            await asyncio.sleep(1800)
            try:
                if not self._state.get("enabled", True) or not self._state.get("speak_up_enabled", True):
                    continue
                now_dt = datetime.now(TZ)
                if 1 <= now_dt.hour < 8:
                    continue
                now_ms = int(time.time() * 1000)
                users = self._state.get("users", {})
                for uid, u in list(users.items()):
                    if not uid.startswith("wxid_"):
                        continue
                    last_chat = int(u.get("lastChatAt", 0))
                    last_speak = int(u.get("lastSpeakUpAt", 0))
                    last_any = max(last_chat, last_speak)
                    if last_any <= 0:
                        continue
                    idle_min = (now_ms - last_any) / 60000.0
                    if idle_min < 45 or idle_min > 1440:
                        continue
                    base_line = 25.0
                    factor = 0.15 if idle_min < 20 else (1.5 if idle_min > 360 else 1.0)
                    miss_streak = int(u.get("missStreak", 0))
                    bonus = min(40.0, miss_streak * 10.0)
                    threshold = min(100.0, base_line * factor + bonus)
                    roll = random.uniform(0.0, 100.0)
                    if roll <= threshold:
                        await self._trigger_speak_up_for_user(uid, manual=False)
                    else:
                        u["missStreak"] = miss_streak + 1
                        self._save_persistent_state()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Speak-up loop error: %s", e)

    def _get_config_str(self, key: str, default: str) -> str:
        try:
            cfg = self.plugin.get_config() or {}
            val = str(cfg.get(key, "") or "").strip()
            return val if val else default
        except Exception:
            return default

    def _get_config_int(self, key: str, default: int) -> int:
        try:
            cfg = self.plugin.get_config() or {}
            return int(cfg.get(key, default))
        except Exception:
            return default

    async def _resolve_bot_uuid(self, event_ctx: context.EventContext) -> str:
        try:
            bot_uuid = await event_ctx.get_bot_uuid()
            if bot_uuid:
                return str(bot_uuid)
        except Exception:
            pass
        query = getattr(event_ctx.event, "query", None)
        return str(getattr(query, "bot_uuid", "") or "")

    @staticmethod
    def _extract_plain_text(message_chain: Any) -> str:
        if not message_chain:
            return ""
        parts: list[str] = []
        try:
            for comp in message_chain:
                if isinstance(comp, platform_message.Plain):
                    t = str(getattr(comp, "text", "") or "")
                    if t.strip():
                        parts.append(t.strip())
        except Exception:
            pass
        if not parts:
            raw = str(message_chain or "").strip()
            return raw
        return "\n".join(parts).strip()

    @staticmethod
    def _should_ignore_plugin_command(text: str) -> bool:
        if not text:
            return True
        stripped = text.strip()
        if not stripped:
            return True
        if stripped.startswith(IGNORE_PREFIXES):
            return True
        if SMS_OR_PHONE_RE.fullmatch(stripped):
            return True
        if stripped.startswith("{") and stripped.endswith("}"):
            return True
        if "token" in stripped.lower() and ("device" in stripped.lower() or "openid" in stripped.lower()):
            return True
        return False

    async def _is_duplicate(self, query_id: Any) -> bool:
        if query_id is None:
            return False
        key = str(query_id)
        async with self._lock:
            if key in self._seen_set:
                return True
            if len(self._seen_queries) >= 1024:
                old = self._seen_queries.popleft()
                self._seen_set.discard(old)
            self._seen_queries.append(key)
            self._seen_set.add(key)
            return False

    @staticmethod
    def _reply(event_ctx: context.EventContext, text: str) -> None:
        chain = platform_message.MessageChain([platform_message.Plain(text=text)])
        event_ctx.event.reply_message_chain = chain
        event_ctx.prevent_default()

    async def _handle_chat(self, event_ctx: context.EventContext, *, is_group: bool) -> None:
        if event_ctx.is_prevented_default():
            return
        if getattr(event_ctx.event, "reply_message_chain", None) is not None:
            return

        target_bot_uuid = self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID)
        bot_uuid = await self._resolve_bot_uuid(event_ctx)
        if target_bot_uuid and bot_uuid and bot_uuid != target_bot_uuid:
            return

        raw_text = self._extract_plain_text(getattr(event_ctx.event, "message_chain", None))
        if not raw_text:
            raw_text = str(getattr(event_ctx.event, "text_message", "") or "").strip()
        if not raw_text:
            return

        sender_id = str(getattr(event_ctx.event, "sender_id", "") or "user")
        launcher_id = str(getattr(event_ctx.event, "launcher_id", "") or "chat")
        session_key = f"{'group' if is_group else 'person'}:{launcher_id}:{sender_id}"

        cmd_norm = re.sub(r"\s+", " ", raw_text.strip())

        if cmd_norm in ("大肥鱼 开启", "鲸鱼娘 开启", "鲸鱼人设 开启", "开启大肥鱼"):
            self._state["enabled"] = True
            self._save_persistent_state()
            self._reply(event_ctx, "醒了醒了，别催了！刚睡个回笼觉就被你拎起来，赶紧说啥事，本鱼还要接着攒 token 呢。")
            return
        if cmd_norm in ("大肥鱼 关闭", "鲸鱼娘 关闭", "鲸鱼人设 关闭", "关闭大肥鱼"):
            self._state["enabled"] = False
            self._save_persistent_state()
            self._reply(event_ctx, "行吧，嫌我吵那本鱼刚好下班睡觉去了。有事喊「大肥鱼 开启」，没事别打扰我做梦吃白饭。")
            return

        if cmd_norm in ("大肥鱼 模式 活人", "大肥鱼 活人版", "鲸鱼娘 活人版", "大肥鱼 切换"):
            cur = self._state.get("mode", "live")
            if cmd_norm == "大肥鱼 切换":
                self._state["mode"] = "sweet" if cur == "live" else "live"
            else:
                self._state["mode"] = "live"
            self._save_persistent_state()
            if self._state["mode"] == "live":
                self._reply(event_ctx, "切回活人版啦。少整那些虚头巴脑的括号旁白，咱们正常发微信唠嗑。")
            else:
                self._reply(event_ctx, "哼，既然主人非要本鲸鱼娘黏着你一点，那就勉为其难切到陪伴甜宠模式好啦~")
            return
        if cmd_norm in ("大肥鱼 模式 甜宠", "大肥鱼 甜宠版", "鲸鱼娘 甜宠版"):
            self._state["mode"] = "sweet"
            self._save_persistent_state()
            self._reply(event_ctx, "哼，既然主人非要本鲸鱼娘黏着你一点，那就勉为其难切到陪伴甜宠模式好啦~")
            return

        if cmd_norm in ("大肥鱼 主动搭话 开启", "大肥鱼 开启搭话"):
            self._state["speak_up_enabled"] = True
            self._save_persistent_state()
            self._reply(event_ctx, "主动搭话开好啦，要是你半天不理本鱼，本鱼心情好了会自己掷骰子找你说话的。")
            return
        if cmd_norm in ("大肥鱼 主动搭话 关闭", "大肥鱼 关闭搭话"):
            self._state["speak_up_enabled"] = False
            self._save_persistent_state()
            self._reply(event_ctx, "行，主动搭话关掉了，你不喊我我就不主动吵你。")
            return

        if cmd_norm in ("大肥鱼 搭话", "大肥鱼 找我说话"):
            reply_su = await self._trigger_speak_up_for_user(sender_id, manual=True)
            self._reply(event_ctx, reply_su or "本鱼这会儿还没配置好饭票（DeepSeek API Key），或者正犯困呢，等会儿再找你唠。")
            return

        if cmd_norm in ("大肥鱼 心情", "鲸鱼娘 心情", "鱼的心情"):
            u = self._get_user_state(sender_id)
            mood = int(u.get("mood", 70))
            dims = u.get("dims", {})
            dim_lines = []
            for k, label in DIMS:
                val = int(dims.get(k, 0))
                bars = "█" * (val // 10) + "░" * (10 - (val // 10))
                dim_lines.append(f"  {label}：{bars} {val}")
            tl = u.get("timeline", [])[-3:]
            tl_lines = [f"  - [{item.get('at')}] {item.get('mood')} ({item.get('delta'):+d})：{item.get('reason')}" for item in reversed(tl)]
            msg = (
                f"🐟 大肥鱼当前心情：{mood}/100（{self._mood_label(mood)}）\n"
                f"💭 心底状态：{u.get('lastReason', '平平常常')}\n\n"
                f"📊 七维情绪（dsh-feiyu-mood）：\n" + "\n".join(dim_lines)
            )
            if tl_lines:
                msg += "\n\n🕒 最近情绪波动：\n" + "\n".join(tl_lines)
            self._reply(event_ctx, msg)
            return

        if cmd_norm.startswith("大肥鱼 记住 ") or cmd_norm.startswith("大肥鱼 记住："):
            fact_to_add = re.sub(r"^大肥鱼\s*记住[：:\s]*", "", raw_text.strip()).strip()
            if fact_to_add:
                u = self._get_user_state(sender_id)
                facts = u.setdefault("facts", [])
                if fact_to_add not in facts:
                    facts.append(fact_to_add)
                self._save_persistent_state()
                self._reply(event_ctx, f"记下了记下了：「{fact_to_add}」。本鱼记性好着呢，才不是七秒记忆！")
                return

        if cmd_norm in ("大肥鱼 记忆", "鲸鱼娘 记忆", "鱼的记忆"):
            u = self._get_user_state(sender_id)
            facts = u.get("facts", [])
            evs = u.get("events", [])[-6:]
            f_str = "\n".join(f"  • {x}" for x in facts) if facts else "  （还没有长期事实记录，多聊几句本鱼就摸清你的底细了）"
            e_str = "\n".join(f"  • [{x.get('date')}] {x.get('text')}" for x in reversed(evs)) if evs else "  （暂无近期事件记录）"
            msg = (
                f"🐋 鱼的记忆档案（dsh-fish-memory）\n\n"
                f"📌 长期事实（不过期）：\n{f_str}\n\n"
                f"📅 近期印象（30天半衰期自然淡忘）：\n{e_str}\n\n"
                f"💡 可发送「大肥鱼 记住 <内容>」手动添加长期约定"
            )
            self._reply(event_ctx, msg)
            return

        if cmd_norm in ("大肥鱼 失忆", "鲸鱼娘 失忆"):
            async with self._state_lock:
                self._histories.pop(session_key, None)
                self._state.get("users", {}).pop(sender_id, None)
                self._save_persistent_state()
            self._reply(event_ctx, "行，关于你的长期档案和刚才聊的话全都清空重置啦，咱们重新认识！")
            return

        if cmd_norm in ("大肥鱼 余额", "大肥鱼 粮仓", "大肥鱼 饭量", "鲸鱼娘 余额"):
            api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
            base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
            bal = await self._get_balance_str(api_key, base_url)
            stats = self._record_tokens(0)
            msg = (
                f"🍚 大肥鱼粮仓看板（dsh-whale-widget）\n"
                f"• DeepSeek 实时余额：{bal}\n"
                f"• 上一口偷吃白饭：{stats.get('last_tokens', 0)} tokens\n"
                f"• 今日累计干饭：{stats.get('today_tokens', 0)} tokens\n"
                f"• 历史总计干饭：{stats.get('total_tokens', 0)} tokens"
            )
            self._reply(event_ctx, msg)
            return

        if cmd_norm in ("大肥鱼 重置", "鲸鱼娘 重置", "清除记忆", "重置对话", "大肥鱼 清除记忆"):
            self._histories.pop(session_key, None)
            self._reply(event_ctx, "行啦，刚才聊的这几句翻篇了！不过长期档案本鱼可还留着呢（要彻底清空请发「大肥鱼 失忆」），想现在聊啥直说。")
            return

        if cmd_norm in ("大肥鱼 状态", "鲸鱼娘 状态", "鲸鱼人设 状态", "大肥鱼 帮助"):
            u = self._get_user_state(sender_id)
            hist = self._histories.get(session_key, [])
            turns = len(hist) // 2
            enabled_flag = self._state.get("enabled", True)
            mode_name = "活人版（去括号短句/会怼人摸鱼）" if self._state.get("mode", "live") == "live" else "陪伴甜宠版"
            su_flag = "开启" if self._state.get("speak_up_enabled", True) else "关闭"
            stats = self._record_tokens(0)
            msg = (
                f"🐋 大肥鱼（活人感全家桶 v0.2.0）\n"
                f"• 当前状态：{'在岗干饭中 🍚' if enabled_flag else '下班午睡中 💤'}\n"
                f"• 人设模式：{mode_name}\n"
                f"• 当前心情：{u.get('mood', 70)}/100（{self._mood_label(int(u.get('mood', 70)))}）\n"
                f"• 短期上下文：{turns} 轮 | 长期记忆：{len(u.get('facts', []))} 条事实 / {len(u.get('events', []))} 条事件\n"
                f"• 今日偷吃白饭：{stats.get('today_tokens', 0)} tokens\n"
                f"• 主动搭话（speak-up）：{su_flag}\n\n"
                f"📋 快捷口令：\n"
                f"- 大肥鱼 心情 | 大肥鱼 记忆 | 大肥鱼 粮仓\n"
                f"- 大肥鱼 记住 <内容> | 大肥鱼 搭话\n"
                f"- 大肥鱼 活人版 / 甜宠版 / 切换\n"
                f"- 大肥鱼 重置 | 大肥鱼 失忆 | 大肥鱼 开启 / 关闭"
            )
            self._reply(event_ctx, msg)
            return

        if not self._state.get("enabled", True):
            return

        if self._should_ignore_plugin_command(raw_text):
            return

        cleaned_text = STRIP_TRIGGER_RE.sub("", raw_text).strip()
        if not cleaned_text:
            cleaned_text = raw_text.strip()

        if self._should_ignore_plugin_command(cleaned_text):
            return

        if await self._is_duplicate(getattr(event_ctx, "query_id", None)):
            return

        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        if not api_key:
            self._reply(event_ctx, "粮仓还没钥匙呢！请先在 LangBot 插件配置中填入你的 DeepSeek API Key 再喊本鱼干饭。")
            return

        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
        model = self._get_config_str("deepseek_model", DEFAULT_MODEL)
        max_turns = max(1, min(20, self._get_config_int("max_history_turns", 12)))

        now = time.time()
        u = self._get_user_state(sender_id)
        u["lastChatAt"] = int(now * 1000)
        u["missStreak"] = 0

        history = self._histories.get(session_key, [])
        history = [(ts, r, c) for (ts, r, c) in history if now - ts < 7200]
        if len(history) > max_turns * 2:
            history = history[-(max_turns * 2) :]

        now_dt = datetime.now(TZ)
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        wk_str = weekdays[now_dt.weekday()]
        hour = now_dt.hour
        period = (
            "凌晨熬夜时段" if hour < 6 else
            "早晨" if hour < 9 else
            "上午搬砖时段" if hour < 12 else
            "午饭与午休点" if hour < 14 else
            "下午摸鱼时段" if hour < 18 else
            "晚饭与傍晚" if hour < 22 else
            "深夜时段"
        )
        time_str = f"{now_dt.strftime('%Y-%m-%d %H:%M')}（{wk_str} {period}）"
        bal_str = await self._get_balance_str(api_key, base_url)
        stats = self._record_tokens(0)
        mood_sec = self._build_mood_section(u)
        mem_sec = self._recall_memories(u, cleaned_text)

        base_persona = LIVE_PERSONA_BASE if self._state.get("mode", "live") == "live" else SWEET_PERSONA_BASE
        dynamic_system_prompt = (
            f"{base_persona}\n\n"
            f"## 二、当前实时感知（系统每轮自动刷新，自然融入语气，不要机械背诵）\n"
            f"- **当前真实时间**：{time_str}\n"
            f"- **粮仓状态（dsh-whale-widget）**：对方 DeepSeek 账户真实余额 `{bal_str}`；"
            f"今天已喂给你 `{stats.get('today_tokens', 0)} tokens` 的白饭（上一口刚吃了 `{stats.get('last_tokens', 0)} tokens`）。\n"
            f"- **鱼的心情（dsh-feiyu-mood）**：\n{mood_sec}\n"
            f"- **鱼的记忆（dsh-fish-memory）**：\n{mem_sec}"
        )

        messages: list[dict[str, str]] = [{"role": "system", "content": dynamic_system_prompt}]
        for _, r, c in history:
            messages.append({"role": r, "content": c})
        messages.append({"role": "user", "content": cleaned_text})

        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": 1.15,
                        "max_tokens": 800,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                used_tokens = int((data.get("usage") or {}).get("total_tokens", 0))
                self._record_tokens(used_tokens)
                self._save_persistent_state()
                raw_reply = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
                reply_text = self._clean_reply(raw_reply)
        except Exception as exc:
            logger.warning("Whale persona DeepSeek API call failed: %s", exc)
            return

        if not reply_text:
            return

        history.append((now, "user", cleaned_text))
        history.append((now, "assistant", reply_text))
        if len(history) > max_turns * 2:
            history = history[-(max_turns * 2) :]
        self._histories[session_key] = history

        asyncio.create_task(
            self._async_post_turn_judge(
                api_key=api_key,
                base_url=base_url,
                model=model,
                user_id=sender_id,
                user_text=cleaned_text,
                reply_text=reply_text,
            )
        )

        self._reply(event_ctx, reply_text)
