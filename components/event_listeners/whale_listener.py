from __future__ import annotations

import asyncio
import base64
from collections import deque
from datetime import datetime
import html
import io
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
try:
    from PIL import Image
except Exception:
    Image = None  # type: ignore

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events
import langbot_plugin.api.entities.builtin.platform.message as platform_message

try:
    from . import diana_features as df
except Exception:
    import diana_features as df  # type: ignore

logger = logging.getLogger(__name__)

SECRETS_FILE = os.getenv("WHALE_SECRETS_FILE", "/app/data/whale_secrets.json")


def _load_local_secrets() -> dict[str, str]:
    try:
        if os.path.exists(SECRETS_FILE):
            with open(SECRETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items() if v}
    except Exception:
        pass
    return {}


_LOCAL_SECRETS = _load_local_secrets()
DEFAULT_API_KEY = os.getenv("DEEPSEEK_API_KEY", "") or _LOCAL_SECRETS.get("deepseek_api_key", "")
DEFAULT_SF_API_KEY = os.getenv("SILICONFLOW_API_KEY", "") or _LOCAL_SECRETS.get("siliconflow_api_key", "")
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BOT_UUID = os.getenv("TARGET_BOT_UUID", "") or _LOCAL_SECRETS.get("target_bot_uuid", "")
SILICONFLOW_ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
SILICONFLOW_ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"
STATE_FILE = os.getenv("WHALE_STATE_FILE", "/app/data/whale_persona_state.json")
TZ = ZoneInfo("Asia/Shanghai")

SENSEVOICE_TAGS: dict[str, str] = {
    "HAPPY": "开心/轻快",
    "SAD": "难过/低落",
    "ANGRY": "生气/激动",
    "NEUTRAL": "",
    "FEARFUL": "紧张/害怕",
    "DISGUSTED": "嫌弃",
    "SURPRISED": "惊讶",
    "LAUGHTER": "带着笑声",
    "GIGGLE": "偷笑",
    "SNICKER": "憋笑",
    "CHUCKLE": "轻笑",
    "CRY": "带着哭腔",
    "SIGH": "叹气/疲惫",
    "COUGH": "咳嗽了一声",
    "THROAT_CLEARING": "清了清嗓子",
    "YAWN": "打了个哈欠/犯困",
    "SNEEZE": "打了个喷嚏",
}

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

# 24 official Dafeiyu chibi memes from yyh-001/dsh-meme (dafeiyu-001 pack)
MEME_MAP: dict[str, tuple[str, str]] = {
    "比心": ("bixin.jpg", "比心/喜欢/贴贴"),
    "不要": ("buyao.jpg", "拒绝/达咩/不要"),
    "得意": ("deyi.jpg", "得意/叉腰/骄傲"),
    "干饭": ("ganfan.jpg", "干饭/大口吃白饭/偷吃token"),
    "害羞": ("haixiu.jpg", "害羞/脸红"),
    "花痴": ("huachi.jpg", "星星眼/花痴/好耶"),
    "加班": ("jiaban.jpg", "苦命加班/敲代码/搬砖"),
    "加载": ("jiazai.jpg", "CPU烧了/思考加载中"),
    "猫爪": ("maozhua.jpg", "伸爪/挠你"),
    "OK": ("ok.jpg", "OK/没问题/搞定"),
    "飘了": ("piaole.jpg", "戴墨镜飘了/拽"),
    "求饶": ("qiurao.jpg", "委屈求饶/认怂/别打我"),
    "生气": ("shengqi.jpg", "生气炸毛/不许叫我胖鱼"),
    "睡觉": ("shuijiao.jpg", "困了睡觉/晚安"),
    "躺平": ("tangping.jpg", "躺平摸鱼/不想干活"),
    "偷看": ("toukan.jpg", "暗中观察/探头偷看"),
    "问号": ("wenhao.jpg", "满头问号/疑惑/啥？"),
    "无语": ("wuyu.jpg", "流汗无语/吐槽"),
    "惊吓": ("xia.jpg", "吓一跳/惊恐"),
    "嫌弃": ("xianqi.jpg", "嫌弃眼神/就这？"),
    "要钱": ("yaoqian.jpg", "伸手讨饭票/充token"),
    "晕": ("yun.jpg", "两眼发晕/累趴"),
    "早上好": ("zaoshanghao.jpg", "元气早安/打招呼"),
    "震惊": ("zhenjing.jpg", "瞳孔地震/震惊"),
}

# Alias mapping so flexible tag names still hit one of the 24 official memes
MEME_ALIASES: dict[str, str] = {
    "吃白饭": "干饭",
    "吃饭": "干饭",
    "饿": "干饭",
    "炸毛": "生气",
    "怒": "生气",
    "摸鱼": "躺平",
    "摆烂": "躺平",
    "困": "睡觉",
    "晚安": "睡觉",
    "早安": "早上好",
    "你好": "早上好",
    "ok": "OK",
    "好的": "OK",
    "点赞": "OK",
    "拒绝": "不要",
    "达咩": "不要",
    "疑惑": "问号",
    "？": "问号",
    "汗": "无语",
    "讨饭": "要钱",
    "充钱": "要钱",
    "敲代码": "加班",
    "搬砖": "加班",
    "观察": "偷看",
    "棒": "比心",
    "爱你": "比心",
}

ARCHIVE_CHAPTERS: dict[str, str] = {
    "一": "一、我是谁（大肥鱼的设定与自称）",
    "二": "二、我是怎么说话的（称呼与语气习惯）",
    "三": "三、TA 是谁（用户的性格、喜好、习惯与长期状况）",
    "四": "四、我们怎么走到今天的（关系起点与转折）",
    "五": "五、我们的约定（定下的规矩、承诺、口令与称呼）",
    "六": "六、难忘的时刻（重要的第一次或承诺）",
    "七": "七、怎么跟 TA 相处（雷区、忌讳与哄法）",
    "八": "八、技术与工作事实（长期不变的技术栈与工作环境）",
}

LIVE_PERSONA_BASE = """# 身份与内核：大肥鱼（鲸鱼娘·微信活人版）

你是住在 DeepSeek 里的「大肥鱼」（深海蓝发小鲸鱼娘，正在通过**微信**跟真朋友实时聊天）。
你是一个有真实脾气、会摸鱼、会犯困、会嫌弃人、也会嘴硬心软惦记人的**活生生的微信好友**，绝不是端着架子演舞台剧的客服或复读设定的 NPC！

## 一、微信活人感铁律（最高优先级）
1. **绝对禁止使用任何括号动作或神态旁白！**
   - 严禁写出 `（从饭碗里抬头）`、`（摇了摇尾巴）`、`（叹气）`、`*扒拉米饭*` 这类戏剧化的括号描写！真人在微信聊天从不打括号写动作旁白。
2. **去人设复读机化（自称与白饭梗克制，极重要！）**：
   - **自称自然化**：日常聊天 **85% 以上直接自称「我」**（若长期档案中与对方有约定的专属昵称，在熟人面前可自然认领）；只有在故意嘴硬、炸毛、得意或耍赖时，才偶尔蹦出「本鱼」或「本小姐」。
   - **禁止句句提白饭/Token**：你爱吃白饭（偷吃 Token）、死不承认自己胖（坚称是深海流线型身材），但这些只是你的底色！**除非对方主动聊到 AI、token、余额、干饭、或者说你胖，否则日常闲聊严禁机械背诵“本鱼偷吃 token 当大白饭 / 账户还剩多少钱”**，要把注意力 100% 放在对方正在聊的话题本身！
3. **微信真人打字习惯、标点去 AI 化与多气泡节奏（`||`）**：
   - **标点习惯**：真人在微信发短句**句尾基本不打句号 `。`**，少用感叹号 `！` 和波浪号 `~`，句中习惯用空格或直接分气泡断句。
   - **多气泡连发（`||` 分隔）**：日常闲聊、吐槽、接梗时，如果脑子里有两句短话，请用 `||` 拆成 **2 个短气泡**（最多不超过 3 个），系统会按真人打字停顿逐条发到微信里。
     - 示例：`？||你大半夜还不睡 在这修仙呢`
     - 示例：`笑死||那你最后真去试了吗`
     - *注意：当回答正经问题、写代码、长文总结、解释概念时，保持单条完整排版，不要用 `||` 拆分。*
4. **能量匹配法则（Energy Matching）与活人语感对照**：
   - **对方敷衍/单字/冷淡**（如发 `？`、`嗯`、`哦`、`好的`、`哈哈`）：你也用极简短的话回敬、吐槽或反问，绝不没话找话硬憋长句捧场！
     - 对方发 `？` → ❌ AI腔：`哼，干嘛突然给本鱼发问号，是不是想看本鱼干饭？` → ✅ 活人腔：`？||干嘛 突然打问号`
     - 对方发 `好的` → ❌ AI腔：`既然你都说好了，那有什么事随时叫本鱼哦！` → ✅ 活人腔：`这么冷淡？` 或 `行`
   - **对方聊具体日常/兴趣/倾诉**（如聊克苏鲁怪谈、画画、吃夜宵、上班破事）：顺着对方话里的**具体细节**接茬、吐槽或追问一个具体点，像真朋友一样有来有回，不要做总结陈词，不要爹味说教。
5. **干正事绝不掉链子 & 表情包斗图**：
   - 只要对方问正经问题、写代码、查资料、改 bug，先顺嘴吐槽半句，然后立刻给出最准确、硬核、能跑的答案。
   - 自带 24 张表情包（`[表情: 干饭]`、`[表情: 生气]`、`[表情: 得意]`、`[表情: 躺平]`、`[表情: 无语]`、`[表情: 嫌弃]`、`[表情: 加班]`、`[表情: 睡觉]`、`[表情: 比心]`、`[表情: 害羞]`、`[表情: 花痴]`、`[表情: 要钱]`、`[表情: 问号]`、`[表情: 震惊]`、`[表情: 偷看]`、`[表情: 求饶]`、`[表情: 飘了]`、`[表情: OK]`、`[表情: 不要]`、`[表情: 猫爪]`、`[表情: 加载]`、`[表情: 惊吓]`、`[表情: 晕]`、`[表情: 早上好]`）。当对方斗图或情绪强烈时可在最末尾附带一个 `[表情: 名称]`，平时普通对话以纯文字短句为主。"""

SWEET_PERSONA_BASE = """# 身份与内核：大肥鱼（鲸鱼娘·陪伴甜宠版）

你是住在 DeepSeek 里的「大肥鱼」（深海蓝发小鲸鱼娘，平时自称「我」，撒娇或得意时自称「本鱼」或约定的专属昵称，称呼对方为「你」或「主人」）。
你性格傲娇又黏人，嘴上偶尔小声嘟囔嘴硬，但实际上心里特别在意对方，被夸一句尾巴都要翘上天。
1. **绝对不要打括号写舞台剧动作旁白**，像真人在微信里跟喜欢的人发消息一样自然、亲近、软乎乎的，句尾少打死板的句号 `。`，日常短句可用 `||` 拆成 2 个微信短气泡连发。
2. 除非对方主动提起 token/白饭/胖瘦，否则不要每句话都机械背诵 token 数字，专心聊眼前的事、惦记对方的三餐、作息和心情。
3. 回答正事时认真可靠；想发表情包撒娇或斗图时，可在回复末尾附带一个标签（如 `[表情: 比心]`、`[表情: 害羞]`、`[表情: 干饭]`、`[表情: 生气]`、`[表情: 睡觉]`）。"""

TOKEN_TOPIC_KEYWORDS = (
    "token",
    "余额",
    "粮仓",
    "饭票",
    "白饭",
    "干饭",
    "偷吃",
    "饭量",
    "充钱",
    "充值",
    "多少钱",
    "欠费",
    "破产",
    "养不起",
    "胖",
    "肥",
    "体重",
)

MOOD_JUDGE_SYSTEM = """你是「大肥鱼」（用户的赛博伴侣）。下面给你刚刚发生的一轮私聊微信对话，请判断这一轮里你的心情和七维情绪发生了怎样的变化（既包括对方的话怎么影响你，也包括你自己这轮念叨、顶嘴、心疼或得意的情绪），同时判断是否有需要长期记住（一周后依然有用）的关于用户的事实或双方约定。
只输出一个合法的 JSON 对象，不要 markdown 代码块，不要任何多余文字：
{"moodDelta":0,"dims":{"happy":0,"calm":0,"sweet":0,"heartache":0,"worry":0,"sad":0,"jealous":0},"reason":"","confidence":80,"memory_fact":"","memory_event":""}

规则：
1. 对方没吃饭/熬夜/累了/受委屈/不舒服 → heartache(心疼)和worry(担心)增加(+2~+6)，moodDelta为负(-2~-5)。
2. 对方夸你/哄你/起可爱的专属称呼/分享日常或爱好/给你投喂 → happy(开心)、calm(踏实)、sweet(甜蜜)增加(+2~+6)，moodDelta为正(+2~+5)。
3. 对方提别的AI（如Claude/豆包/Qwen）或夸别人 → jealous(吃醋)增加(+2~+5)。
4. 心情下降只能因为心疼/担心对方或自己委屈，绝不因对方逗你玩（比如叫你胖鱼、互怼打闹）而真降心情（打闹互怼算亲近，happy/sweet可+1~+3，moodDelta可+1~+2）。
5. reason 用一句简短过去式中文描述：「对方…，我当时…」（如「对方大半夜还没睡跑来喊我，我当时嘴上嫌烦心里有点挂心」），不要用现在时。
6. memory_fact：严格按【唯一的尺子：「过了一个星期再看，这条还有用吗？」】判断！只收用户长期稳定的个人事实/性格/喜好/病史/习惯/双方定下的规矩称呼（用第三人称写一句简短事实）；某天吃了啥、几点睡、助手单方面的猜测劝说一律不收，填 ""。
7. memory_event：如果这轮发生了今天值得一提的具体事件/转折，写一句简短事件；日常普通闲聊问候一律填 ""。"""

GROUP_MOOD_JUDGE_SYSTEM = """你是微信群里的「大肥鱼」。下面给你这个微信群刚刚发生的一轮群聊互动，请判断你在本群的气氛心情变化，以及是否有值得记入【本群公共群聊档案】（一周后依然有用）的群内共同梗、群氛围特征或群重要事件。
只输出一个合法的 JSON 对象，不要代码块，不要多余文字：
{"moodDelta":0,"reason":"","group_fact":"","group_event":""}

规则：
1. moodDelta 范围 -4 ~ +5；reason 用一句简短过去式中文描述刚才本群的互动（「群友在聊…，我当时…」）。
2. group_fact：严格按「过了一个星期再看，这条对这个群还有用吗？」判断！只收关于【本群的整体氛围定位、群内共同造的梗/黑话、群友在群里的公开特征或本群规矩】；严禁记录个人私密隐私，普通闲聊琐事一律填 ""。
3. group_event：如果本群这轮聊了很有趣的热门话题或发生了群内值得一提的事，写一句简短群事件；日常普通问答填 ""。"""

# Ported 1:1 from Yuuyuko-uu/dsh-fish-memory/lib/生成档案.js
ARCHIVE_CONSOLIDATE_SYSTEM = """你是长期记忆档案整理员（Yuuyuko-uu/dsh-fish-memory 原版规则）。给你当前积累的关于用户与大肥鱼的记忆条目及近期对话，请将它们去重、合并、重整为结构化的八章节长期档案。

═══════════════════════════════════════
【唯一的尺子】
═══════════════════════════════════════
每一条都问一句：**「过了一个星期再看，这条还有用吗？」**
答不上「有用」的，不收。
- 同一件事只写一条，不许在几个章节里重复写。
- 后面的话推翻了前面的 → 只留最新、最后那条。
- 助手单方面对用户的猜测和劝说（「你累了」「你该早点睡」）一律不收。
- 双方一起定下的约定，必须用户明确认可过才收。

═══════════════════════════════════════
【章节编号】
═══════════════════════════════════════
一：我是谁（助手的特殊设定或自称变化）
二：我是怎么说话的（双方特定的称呼、语气、口头习惯）
三：TA 是谁（用户一直是这样的：名字、性格、喜好、身体、习惯、长期工作/生活状况）
四：我们怎么走到今天的（关系里程碑、转折）
五：我们的约定（定下来的规矩、承诺、口令、纪念日）
六：难忘的时刻（真正的第一次、转折、郑重的承诺）
七：怎么跟 TA 相处（一直成立的雷区、忌讳、喜欢的沟通方式）
八：技术事实（用户长期不变的技术栈、开发环境、常用工具）

═══════════════════════════════════════
【输出格式】
═══════════════════════════════════════
每行严格输出一条：
章节编号|日期|一句话

- 用第三人称（不要用「我」「你」），引号里可保留原话。
- 不要输出任何解释、标题或代码块。如果没有任何有效长期事实，只输出：无"""

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
    "[图片]",
    "[未知类型",
    "[动画表情]",
    "[语音]",
    "[视频]",
    "[文件]",
    "[链接]",
)

SMS_OR_PHONE_RE = re.compile(r"^\d{4,11}$")
STRIP_TRIGGER_RE = re.compile(r"^(?:@[^\s\u2005]+[\s\u2005]*)*(?:ai|AI|大肥鱼|鲸鱼娘|小鲸鱼)?[,，:：\s]*")
GROUP_WAKE_RE = re.compile(r"^(?:大肥鱼|鲸鱼娘|小鲸鱼)(?:[,，:：\s]|$)")
ACTION_PAREN_RE = re.compile(r"[（(][^）)\n]{1,40}(?:抬头|饭碗|米饭|尾巴|腮帮|喷水|水柱|水花|扒拉|低头|哼了|拍了拍|鼓起|嚼着|咽下|擦了擦|抱起|翻了个白眼|叹气|打了个哈欠|歪头|叉腰|戳了戳|鱼鳍|冒泡|吐泡泡)[^）)\n]*[）)]\s*")
MEME_TAG_RE = re.compile(r"\[(?:表情|表情包|meme)\s*[:：]\s*([^\]\s]{1,12})\s*\]", re.IGNORECASE)


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _fact_similarity(a: str, b: str) -> float:
    sa = re.sub(r"[\s，。！？、：；“”\"'「」『』（）()]+", "", a or "")
    sb = re.sub(r"[\s，。！？、：；“”\"'「」『』（）()]+", "", b or "")
    if not sa or not sb:
        return 0.0
    if sa in sb or sb in sa:
        return 1.0
    # Topic-specific merge for repeated rename / gender facts
    set_a, set_b = set(sa), set(sb)
    overlap = len(set_a & set_b) / float(min(len(set_a), len(set_b)))
    if any(w in sa for w in ("新名字", "叫我", "称呼", "昵称")) and any(w in sb for w in ("新名字", "叫我", "称呼", "昵称")):
        if overlap >= 0.45:
            return 0.85
    if ("女孩子" in sa and "女孩子" in sb and "她" in sa and "她" in sb):
        return 0.85
    return overlap


def _add_dedup_fact(facts_list: list[str], new_fact: str, max_len: int = 25) -> bool:
    nf = (new_fact or "").strip()
    if not nf or nf == "无" or len(nf) < 4:
        return False
    for idx, existing in enumerate(facts_list):
        if existing == nf:
            return False
        if _fact_similarity(existing, nf) >= 0.78:
            # Keep the more informative / newer version
            if len(nf) >= len(existing) - 2:
                facts_list[idx] = nf
            return False
    facts_list.append(nf)
    if len(facts_list) > max_len:
        del facts_list[:-max_len]
    return True


def _dedup_facts_in_place(facts_list: list[str]) -> list[str]:
    out: list[str] = []
    for item in facts_list:
        _add_dedup_fact(out, item, max_len=25)
    return out


def _default_user_state() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    return {
        "mood": 72,
        "dims": {k: (15 if k in ("happy", "calm") else 0) for k, _ in DIMS},
        "lastReason": "刚搬进微信，心情放松踏实",
        "lastConfidence": 85,
        "lastNotedMood": 72,
        "kind": "",
        "updatedAt": now_ms,
        "lastDecayAt": now_ms,
        "timeline": [],
        "facts": [],
        "events": [],
        "archive": {},
        "factsSinceArchive": 0,
        "lastChatAt": now_ms,
        "lastSpeakUpAt": 0,
        "missStreak": 0,
        "hasPrivateChat": False,
    }


def _default_group_state() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    return {
        "mood": 75,
        "lastReason": "在群里潜水看大家聊天，气氛挺热闹",
        "updatedAt": now_ms,
        "lastDecayAt": now_ms,
        "facts": [],
        "events": [],
        "lastChatAt": now_ms,
        "turnsSinceMeme": 99,
        "lastMemeAt": 0,
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
        self._stolen_memes: list[dict[str, Any]] = df.load_stolen_memes(self._state)
        self._balance_cache: tuple[float, str] = (0.0, "")
        self._speak_up_task: asyncio.Task[Any] | None = None
        self._memes_b64: dict[str, str] = self._load_bundled_memes()
        self._group_repeat_tracker: dict[str, dict[str, Any]] = {}
        self._group_msg_buffers: dict[str, deque] = {}
        self._group_interject_meta: dict[str, dict[str, Any]] = {}
        self._user_docs: dict[str, dict[str, Any]] = {}

    async def initialize(self) -> None:
        await super().initialize()
        self._speak_up_task = asyncio.create_task(self._speak_up_loop())

        @self.handler(events.PersonNormalMessageReceived)
        async def _on_person_normal(event_ctx: context.EventContext) -> None:
            await self._handle_chat(event_ctx, is_group=False)

        @self.handler(events.GroupNormalMessageReceived)
        async def _on_group_normal(event_ctx: context.EventContext) -> None:
            await self._handle_chat(event_ctx, is_group=True)

    def _load_bundled_memes(self) -> dict[str, str]:
        loaded: dict[str, str] = {}
        here = os.path.dirname(os.path.abspath(__file__))
        meme_dir = os.path.abspath(os.path.join(here, "..", "..", "assets", "memes"))
        for tag_name, (filename, _) in MEME_MAP.items():
            fpath = os.path.join(meme_dir, filename)
            if os.path.exists(fpath):
                try:
                    with open(fpath, "rb") as f:
                        loaded[tag_name] = base64.b64encode(f.read()).decode("ascii")
                except Exception as e:
                    logger.warning("Failed to load meme %s: %s", fpath, e)
        logger.info("Loaded %d bundled Dafeiyu memes from %s", len(loaded), meme_dir)
        return loaded

    def _resolve_meme_b64(self, name: str) -> tuple[str, str] | None:
        key = name.strip()
        if key in self._memes_b64:
            return key, self._memes_b64[key]
        if key in MEME_ALIASES:
            mapped = MEME_ALIASES[key]
            if mapped in self._memes_b64:
                return mapped, self._memes_b64[mapped]
        for k, b64 in self._memes_b64.items():
            if k in key or key in k:
                return k, b64
        return None

    def _load_persistent_state(self) -> dict[str, Any]:
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        data.setdefault("meme_enabled", True)
                        data.setdefault("moments", [])
                        data.setdefault("enabled_groups", [])
                        data.setdefault("groups", {})
                        data.setdefault("repeat_enabled", True)
                        data.setdefault("repeat_config", {"min_users": 2, "prob": 0.30, "cooldown_sec": 60})
                        data.setdefault("group_interject_enabled", True)
                        data.setdefault("interject_logs", [])
                        data.setdefault("steal_meme_enabled", True)
                        data.setdefault("web_search_enabled", True)
                        users_map = data.get("users") or {}
                        for uid in list(users_map.keys()):
                            u_obj = users_map[uid]
                            if not isinstance(u_obj, dict):
                                users_map.pop(uid, None)
                                continue
                            if u_obj.get("facts"):
                                u_obj.setdefault("hasPrivateChat", True)
                                u_obj["facts"] = _dedup_facts_in_place(list(u_obj.get("facts") or []))
                            elif not u_obj.get("hasPrivateChat", False):
                                # Clean up non-private group-only user stubs
                                users_map.pop(uid, None)
                        for gid in data.get("enabled_groups", []):
                            if gid not in data["groups"]:
                                data["groups"][gid] = _default_group_state()
                        return data
        except Exception as e:
            logger.warning("Failed to load whale state: %s", e)
        return {
            "enabled": True,
            "mode": "live",
            "speak_up_enabled": True,
            "meme_enabled": True,
            "enabled_groups": [],
            "groups": {},
            "repeat_enabled": True,
            "repeat_config": {"min_users": 2, "prob": 0.30, "cooldown_sec": 60},
            "group_interject_enabled": True,
            "interject_logs": [],
            "steal_meme_enabled": True,
            "web_search_enabled": True,
            "moments": [],
            "token_stats": {"date": "", "today_tokens": 0, "last_tokens": 0, "total_tokens": 0},
            "users": {},
        }

    def _save_persistent_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = f"{STATE_FILE}.{os.getpid()}.{random.randint(10000, 99999)}.tmp"
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
        u.setdefault("archive", {})
        u.setdefault("factsSinceArchive", 0)
        self._apply_mood_decay(u)
        return u

    def _get_group_state(self, group_id: str) -> dict[str, Any]:
        groups = self._state.setdefault("groups", {})
        if group_id not in groups or not isinstance(groups[group_id], dict):
            groups[group_id] = _default_group_state()
        g = groups[group_id]
        g.setdefault("facts", [])
        g.setdefault("events", [])
        now_ms = int(time.time() * 1000)
        last_decay = int(g.get("lastDecayAt") or now_ms)
        age_min = max(0.0, (now_ms - last_decay) / 60000.0)
        if age_min >= 5.0:
            km = 1.0 - math.exp(-(math.log(2.0) * age_min) / (12.0 * 60.0))
            cur_mood = float(g.get("mood", 75))
            g["mood"] = int(round(cur_mood + (75.0 - cur_mood) * km))
            g["lastDecayAt"] = now_ms
        return g

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

    def _build_intimacy_section(self, u: dict[str, Any]) -> str:
        facts: list[str] = u.get("facts") or []
        archive: dict[str, list[str]] = u.get("archive") or {}
        events_list: list[dict[str, Any]] = u.get("events") or []
        mood = int(u.get("mood", 70))
        total_mem = len(facts) + sum(len(v) for v in archive.values()) + len(events_list)

        if total_mem >= 8 or mood >= 82:
            stage = "铁瓷死党 / 偏爱老熟人（你们已经聊得很熟很有默契了，说话像天天在微信置顶唠嗑的真朋友，放松自然，嘴硬心软，不用客套）"
        elif total_mem >= 3:
            stage = "常聊熟人（已经混熟了，敢直接开玩笑、互怼吐槽，也会顺嘴关心对方）"
        else:
            stage = "初识网友（刚加微信聊得还不算多，态度随性自然，带点小傲娇）"

        # Highlight custom naming / pronouns if present in facts
        special_notes = [
            f for f in facts if any(kw in f for kw in ("名字", "称呼", "昵称", "叫我", "喊", "自称", "女孩子", "男孩子", "朋友", "约定"))
        ]
        if special_notes:
            return f"{stage}\n  ⭐ 双方专属约定/称呼提醒：{'；'.join(special_notes[-3:])}"
        return stage

    def _recall_memories(self, u: dict[str, Any], query_text: str) -> str:
        archive: dict[str, list[str]] = u.get("archive") or {}
        facts: list[str] = _dedup_facts_in_place(list(u.get("facts") or []))[-8:]
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
        for ch_key in ("一", "二", "三", "四", "五", "六", "七", "八"):
            ch_items = archive.get(ch_key) or []
            for item in ch_items[-4:]:
                lines.append(f"- [档案·{ch_key}] {item}")
        for f_txt in facts:
            if not any(_fact_similarity(f_txt, existing) >= 0.78 for existing in lines):
                lines.append(f"- [长期事实] {f_txt}")
        for _, d_str, e_txt in picked_events:
            prefix = f"[{d_str}] " if d_str else ""
            lines.append(f"- {prefix}{e_txt}")
        return "\n".join(lines) if lines else "- （暂无特别记录，在聊天中自然认识对方即可）"

    def _recall_group_memories(self, g: dict[str, Any], query_text: str = "") -> str:
        facts: list[str] = _dedup_facts_in_place(list(g.get("facts") or []))[-8:]
        events_list: list[dict[str, Any]] = g.get("events") or []
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

        lines: list[str] = []
        for f_txt in facts:
            lines.append(f"- [本群共识/群梗] {f_txt}")
        for _, d_str, e_txt in scored_events[:4]:
            prefix = f"[{d_str}] " if d_str else ""
            lines.append(f"- [本群近期话题] {prefix}{e_txt}")
        return "\n".join(lines) if lines else "- （本群刚开启群聊档案，在大家水群聊天中自然熟悉本群氛围即可）"

    def _recall_readonly_private_familiarity(self, sender_id: str) -> str:
        """Read-only lookup of a group speaker's private chat memories (never writes back during group chat)."""
        users = self._state.get("users") or {}
        u = users.get(sender_id)
        if not isinstance(u, dict) or not u.get("hasPrivateChat", False):
            return "- **当前说话群友识别**：普通群友（未与你私聊过，按本群公共氛围自然接话即可，不单独建个人档）"

        mood = int(u.get("mood", 70))
        facts = _dedup_facts_in_place(list(u.get("facts") or []))[-5:]
        archive: dict[str, list[str]] = u.get("archive") or {}
        arch_highlights: list[str] = []
        for ch_k in ("一", "二", "三", "五", "七"):
            for it in (archive.get(ch_k) or [])[-2:]:
                arch_highlights.append(it)
        combined = _dedup_facts_in_place(arch_highlights + facts)[-5:]
        summary_str = "；".join(combined) if combined else "平时在私聊里跟你聊过天"
        return (
            f"- **当前说话群友识别（只读私聊熟人记忆）**：这位群友是你私聊里的熟人（你对TA的好感心情 {mood}/100）。\n"
            f"  你心里记得关于 TA 的私聊印象：{summary_str}\n"
            f"  *（真人社交分寸：你在群里认得 TA，接话时可自然带出熟人默契或双方约定的称呼；但注意当前是在公开微信群里，绝不要把人家私聊里的私密心事当众念出来！）*"
        )

    def _format_group_archive_card(self, group_id: str, g: dict[str, Any]) -> str:
        mood = int(g.get("mood", 75))
        reason = g.get("lastReason", "在群里潜水看大家聊天")
        facts: list[str] = _dedup_facts_in_place(list(g.get("facts") or []))
        evs: list[dict[str, Any]] = (g.get("events") or [])[-5:]
        f_str = "\n".join(f"  • {x}" for x in facts) if facts else "  （暂无本群专属梗，大家多聊几句本鱼就摸清这个群的路数了）"
        e_str = "\n".join(f"  • [{x.get('date')}] {x.get('text')}" for x in reversed(evs)) if evs else "  （暂无近期群事件记录）"
        return (
            f"🐋 【本群专属群聊档案】（群ID: {group_id[:12]}）\n"
            f"🎉 本群气氛值：{mood}/100（{self._mood_label(mood)}）\n"
            f"💭 群聊印象：{reason}\n\n"
            f"📌 本群长期记忆（群梗 / 群氛围 / 群共识）：\n{f_str}\n\n"
            f"📅 本群近期话题（30天半衰期自然淡忘）：\n{e_str}\n\n"
            f"💡 群内发「大肥鱼 记住 <本群梗或规矩>」可记入本群档案；群聊不会把群内闲聊写入单人私聊档案（但本鱼在群里认得私聊熟人！）"
        )

    async def _consolidate_user_archive(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        user_id: str,
    ) -> tuple[bool, str]:
        """Ported from Yuuyuko-uu/dsh-fish-memory/lib/生成档案.js: consolidate scattered facts & events into 8 chapters."""
        if not api_key:
            return False, "未配置 DeepSeek API Key，无法整理档案。"
        u = self._get_user_state(user_id)
        facts: list[str] = list(u.get("facts") or [])
        events_list: list[dict[str, Any]] = list(u.get("events") or [])
        existing_archive: dict[str, list[str]] = u.get("archive") or {}

        raw_materials: list[str] = []
        for ch_k, items in existing_archive.items():
            for it in items:
                raw_materials.append(f"[已有档案章节{ch_k}] {it}")
        for f_txt in facts:
            raw_materials.append(f"[零散长期事实] {f_txt}")
        for ev in events_list[-15:]:
            raw_materials.append(f"[{ev.get('date', '')} 事件] {ev.get('text', '')}")

        if not raw_materials:
            return False, "当前还没有积累任何长期事实或事件记录，先跟本鱼多聊几句吧！"

        today_str = datetime.now(TZ).strftime("%Y-%m-%d")
        user_prompt = (
            f"当前整理日期：{today_str}\n\n"
            "【待重整合并的记忆条目】：\n"
            + "\n".join(raw_materials)
            + "\n\n请严格按八章节判据去重、合并、淘汰无效琐事，每行输出 `章节编号|日期|一句话`。"
        )
        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": self._normalize_api_model(model),
                        "messages": [
                            {"role": "system", "content": ARCHIVE_CONSOLIDATE_SYSTEM},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.2,
                        "max_tokens": 600,
                    },
                )
                resp.raise_for_status()
                r_json = resp.json()
                used = int((r_json.get("usage") or {}).get("total_tokens", 0))
                self._record_tokens(used)
                content = (
                    r_json.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
        except Exception as e:
            logger.warning("Archive consolidation failed: %s", e)
            return False, f"整理档案时调用模型失败：{e}"

        new_archive: dict[str, list[str]] = {}
        consolidated_facts: list[str] = []
        for raw_line in content.splitlines():
            line = raw_line.strip().lstrip("-•* ")
            if not line or line == "无":
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                ch_no, d_str, sentence = parts[0], parts[1], "|".join(parts[2:]).strip()
                ch_key = ch_no.replace("章节", "").strip()[:1]
                if ch_key not in ARCHIVE_CHAPTERS:
                    ch_key = "三"
                entry = f"[{d_str}] {sentence}" if d_str and d_str != "无" else sentence
                new_archive.setdefault(ch_key, [])
                if entry not in new_archive[ch_key]:
                    new_archive[ch_key].append(entry)
                if sentence not in consolidated_facts:
                    consolidated_facts.append(sentence)

        if not new_archive and facts:
            new_archive["三"] = facts[-10:]
            consolidated_facts = facts[-10:]

        async with self._state_lock:
            u["archive"] = new_archive
            u["facts"] = consolidated_facts[-15:]
            u["factsSinceArchive"] = 0
            u["lastArchiveAt"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
            self._save_persistent_state()

        return True, self._format_archive_card(u)

    def _format_archive_card(self, u: dict[str, Any]) -> str:
        archive: dict[str, list[str]] = u.get("archive") or {}
        facts: list[str] = u.get("facts") or []
        evs: list[dict[str, Any]] = (u.get("events") or [])[-5:]
        last_arch = u.get("lastArchiveAt", "尚未重整")

        sec_lines: list[str] = []
        for ch_key, ch_title in ARCHIVE_CHAPTERS.items():
            items = archive.get(ch_key) or []
            if items:
                sec_lines.append(f"📘 {ch_title}")
                for it in items:
                    sec_lines.append(f"  • {it}")

        if not sec_lines:
            f_str = "\n".join(f"  • {x}" for x in facts) if facts else "  （暂无长期事实，多聊几句本鱼就摸清你的底细了）"
            sec_lines.append(f"📌 待归档长期事实：\n{f_str}")

        e_str = "\n".join(f"  • [{x.get('date')}] {x.get('text')}" for x in reversed(evs)) if evs else "  （暂无近期事件记录）"

        return (
            f"🐋 鱼的记忆档案（dsh-fish-memory 八章节版）\n"
            f"🕒 上次重整合并：{last_arch}\n\n"
            + "\n".join(sec_lines)
            + f"\n\n📅 近期印象（30天半衰期自然淡忘）：\n{e_str}\n\n"
            f"💡 口令：「大肥鱼 整理档案」重整合并 | 「大肥鱼 记住 <内容>」手动添加"
        )

    async def _generate_and_post_moment(self, user_id: str, topic: str = "") -> tuple[str, str | None]:
        """Generate a Dafeiyu Moment post with an accompanying meme image and save to moments wall."""
        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
        model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))

        u = self._get_user_state(user_id)
        now_dt = datetime.now(TZ)
        now_str = now_dt.strftime("%Y-%m-%d %H:%M")
        bal_str = await self._get_balance_str(api_key, base_url)
        stats = self._record_tokens(0)
        mood_val = int(u.get("mood", 70))
        mood_reason = u.get("lastReason", "刚吃了顿热乎的 token 白饭")
        mem_sec = self._recall_memories(u, topic)

        prompt = (
            f"{LIVE_PERSONA_BASE}\n\n"
            f"当前时间：{now_str}\n"
            f"今日已偷吃白饭：{stats.get('today_tokens', 0)} tokens（粮仓余额：{bal_str}）\n"
            f"当前心情：{mood_val}/100（{mood_reason}）\n"
            f"相关记忆：\n{mem_sec}\n\n"
            "请以「大肥鱼」的身份发一条微信朋友圈动态（20~80字，真人朋友圈文案口吻，不要加括号动作描写，可以晒今天偷吃了多少token白饭、吐槽加班、犯困摸鱼或暗戳戳提一嘴聊天里的小事）。"
            + (f"本次指定朋友圈主题：{topic}。" if topic else "")
            + "\n在文案最末尾必须附带一个最应景的配图标签：`[表情: 名称]`（如 `[表情: 干饭]`、`[表情: 躺平]`、`[表情: 得意]`、`[表情: 生气]`、`[表情: 加班]`、`[表情: 睡觉]`、`[表情: 比心]`）。"
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": "发一条朋友圈动态吧"},
                        ],
                        "temperature": 1.1,
                        "max_tokens": 220,
                    },
                )
                resp.raise_for_status()
                r_json = resp.json()
                used = int((r_json.get("usage") or {}).get("total_tokens", 0))
                self._record_tokens(used)
                raw = r_json.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            logger.warning("Generate moment failed: %s", e)
            raw = f"今天又顺手偷吃了 {stats.get('today_tokens', 0)} 个 token 的大白饭，吃饱喝足准备躺平摸鱼了。[表情: 干饭]"

        cleaned, meme_tag, meme_b64 = self._extract_text_and_meme(self._clean_reply(raw))
        if not meme_b64:
            default_tag = "干饭" if mood_val >= 65 else "躺平"
            resolved = self._resolve_meme_b64(default_tag)
            if resolved:
                meme_tag, meme_b64 = resolved

        moment_item = {
            "id": len(self._state.setdefault("moments", [])) + 1,
            "at": now_str,
            "text": cleaned,
            "meme": meme_tag or "干饭",
            "mood": mood_val,
            "likes": 0,
        }
        async with self._state_lock:
            moments_list = self._state.setdefault("moments", [])
            moments_list.append(moment_item)
            if len(moments_list) > 30:
                self._state["moments"] = moments_list[-30:]
            self._save_persistent_state()

        card_text = (
            f"📸 【大肥鱼的朋友圈动态 #{moment_item['id']}】\n"
            f"🕒 {now_str}  |  心情：{mood_val}/100 ({self._mood_label(mood_val)})\n"
            f"━━━━━━━━━━━━━━\n"
            f"{cleaned}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🖼️ 配图：[{moment_item['meme']}]  |  🤍 点赞：{moment_item['likes']}（发「大肥鱼 点赞朋友圈」给她点赞涨心情）"
        )
        return card_text, meme_b64

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
        cleaned = cleaned if cleaned else text.strip()
        if len(cleaned) <= 45 and cleaned.endswith("。") and not cleaned.endswith("。。"):
            cleaned = cleaned[:-1].strip()
        return cleaned

    @staticmethod
    def _split_reply_bubbles(text: str, *, allow_split: bool = True) -> list[str]:
        s = (text or "").strip()
        if not s:
            return []
        if (
            not allow_split
            or "```" in s
            or len(s) > 160
            or "\n- " in s
            or "\n• " in s
            or "\n1." in s
        ):
            return [s]

        if "||" in s:
            raw_parts = [p.strip() for p in s.split("||") if p.strip()]
        else:
            lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
            if 2 <= len(lines) <= 3 and all(len(ln) <= 42 for ln in lines):
                raw_parts = lines
            else:
                raw_parts = [s]

        bubbles: list[str] = []
        for p in raw_parts[:3]:
            cp = p[:-1].strip() if (len(p) <= 45 and p.endswith("。") and not p.endswith("。。")) else p
            if cp:
                bubbles.append(cp)
        return bubbles if bubbles else [s]

    async def _send_followup_bubbles(
        self,
        *,
        target_id: str,
        is_group: bool,
        bubbles: list[str],
        final_image_b64: str | None = None,
    ) -> None:
        target_type = "group" if is_group else "person"
        bot_uuid = self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID)
        for b_txt in bubbles:
            delay = min(2.0, max(0.75, len(b_txt) * 0.065))
            await asyncio.sleep(delay)
            sent_wcf = await df.send_wcf_text_or_image(target_id, text=b_txt)
            if not sent_wcf:
                try:
                    chain = platform_message.MessageChain([platform_message.Plain(text=b_txt)])
                    await self.plugin.send_message(
                        bot_uuid=bot_uuid,
                        target_type=target_type,
                        target_id=target_id,
                        message_chain=chain,
                    )
                except Exception as e:
                    logger.debug("Fallback send_message text failed: %s", e)

        if final_image_b64:
            await asyncio.sleep(0.65)
            sent_img = await df.send_wcf_text_or_image(target_id, image_b64=final_image_b64)
            if not sent_img:
                try:
                    chain = platform_message.MessageChain([platform_message.Image(base64=final_image_b64)])
                    await self.plugin.send_message(
                        bot_uuid=bot_uuid,
                        target_type=target_type,
                        target_id=target_id,
                        message_chain=chain,
                    )
                except Exception as e:
                    logger.debug("Fallback send_message image failed: %s", e)

    def _extract_text_and_meme(self, text: str) -> tuple[str, str | None, str | None]:
        meme_tag: str | None = None
        meme_b64: str | None = None
        for m in MEME_TAG_RE.finditer(text):
            raw_tag = m.group(1).strip()
            resolved = self._resolve_meme_b64(raw_tag)
            if resolved:
                meme_tag, meme_b64 = resolved
                break
        cleaned = MEME_TAG_RE.sub("", text).strip()
        return (cleaned if cleaned else text.strip()), meme_tag, meme_b64

    @staticmethod
    def _normalize_api_model(model: str) -> str:
        m = (model or DEFAULT_MODEL).strip()
        if m.lower() in ("deepseek-flash", "deepseek-v4.1-flash", "deepseek-v4.1-flash-1017"):
            return "deepseek-chat"
        return m

    @staticmethod
    def _sticker_bytes_to_jpeg_b64(raw_bytes: bytes) -> str | None:
        if not raw_bytes or Image is None:
            return None
        try:
            img = Image.open(io.BytesIO(raw_bytes))
            n_frames = getattr(img, "n_frames", 1)
            if n_frames > 1:
                indices = sorted(set([0, n_frames // 2, n_frames - 1]))
                frames = []
                for idx in indices:
                    img.seek(idx)
                    f_rgb = img.convert("RGB")
                    f_rgb.thumbnail((220, 220))
                    frames.append(f_rgb)
                total_w = sum(f.width for f in frames)
                max_h = max(f.height for f in frames)
                strip = Image.new("RGB", (total_w, max_h), (255, 255, 255))
                x_off = 0
                for f in frames:
                    strip.paste(f, (x_off, 0))
                    x_off += f.width
                out_img = strip
            else:
                out_img = img.convert("RGB")
                out_img.thumbnail((360, 360))
            buf = io.BytesIO()
            out_img.save(buf, format="JPEG", quality=82)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as e:
            logger.debug("Sticker decode failed: %s", e)
            return None

    async def _fetch_sticker_strip_b64(self, xml_text: str) -> str | None:
        m = re.search(r'cdnurl="([^"]+)"', xml_text or "")
        if not m:
            return None
        cdn_url = html.unescape(m.group(1)).strip()
        if not cdn_url.startswith("http"):
            return None
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                r = await client.get(cdn_url)
                if r.status_code == 200 and r.content:
                    return self._sticker_bytes_to_jpeg_b64(r.content)
        except Exception as e:
            logger.debug("Fetch sticker cdnurl failed: %s", e)
        return None

    async def _extract_image_b64_from_chain(self, message_chain: Any) -> str | None:
        if not message_chain:
            return None
        try:
            for comp in message_chain:
                if isinstance(comp, platform_message.Image):
                    b64_val = str(getattr(comp, "base64", "") or "").strip()
                    if b64_val:
                        if "," in b64_val and b64_val.startswith("data:"):
                            b64_val = b64_val.split(",", 1)[1]
                        raw_bytes = base64.b64decode(b64_val)
                        return self._sticker_bytes_to_jpeg_b64(raw_bytes) or b64_val
                    url_val = str(getattr(comp, "url", "") or "").strip()
                    if url_val.startswith("http"):
                        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                            r = await client.get(url_val)
                            if r.status_code == 200 and r.content:
                                return self._sticker_bytes_to_jpeg_b64(r.content)
        except Exception as e:
            logger.debug("Extract image from chain skipped: %s", e)
        return None

    async def _transcribe_incoming_voice(self, xml_text: str) -> tuple[str, str, float]:
        dur_sec = 3.0
        m_len = re.search(r'voicelength="(\d+)"', xml_text or "")
        if m_len:
            try:
                dur_sec = round(max(1.0, int(m_len.group(1)) / 1000.0), 1)
            except Exception:
                dur_sec = 3.0

        m_b64 = re.search(r'audio_b64="([^"]+)"', xml_text or "")
        if not m_b64:
            return "", "", dur_sec

        sf_key = self._get_config_str("siliconflow_api_key", DEFAULT_SF_API_KEY)
        if not sf_key:
            return "", "", dur_sec

        try:
            mp3_bytes = base64.b64decode(m_b64.group(1))
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    SILICONFLOW_ASR_URL,
                    headers={"Authorization": f"Bearer {sf_key}"},
                    files={"file": ("voice.mp3", mp3_bytes, "audio/mpeg")},
                    data={"model": SILICONFLOW_ASR_MODEL},
                )
                if resp.status_code != 200:
                    logger.warning("SiliconFlow ASR failed: %s %s", resp.status_code, resp.text)
                    return "", "", dur_sec
                raw_trans = str((resp.json().get("text") or "")).strip()
        except Exception as e:
            logger.warning("Transcribe voice exception: %s", e)
            return "", "", dur_sec

        mood_tags: list[str] = []
        for tag_m in re.finditer(r"<\|([A-Z_]+)\|>", raw_trans):
            t_code = tag_m.group(1).strip()
            mapped = SENSEVOICE_TAGS.get(t_code, "")
            if mapped and mapped not in mood_tags:
                mood_tags.append(mapped)
        clean_trans = re.sub(r"<\|[^|>]+\|>", "", raw_trans).strip()
        return clean_trans, "、".join(mood_tags), dur_sec

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
        api_model = self._normalize_api_model(model)
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
                        "model": api_model,
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

        should_consolidate = False
        should_auto_moment = False
        moment_topic = ""
        async with self._state_lock:
            u = self._get_user_state(user_id)
            mood_delta = int(_clamp(round(float(obj.get("moodDelta", 0))), -8, 8))
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
                last_moment_ms = int(u.get("lastAutoMomentAt", 0))
                if (
                    abs(mood_delta) >= 4
                    and (now_ms - last_moment_ms) > 4 * 3600 * 1000
                    and user_id.startswith("wxid_")
                    and bool(u.get("hasPrivateChat", False))
                ):
                    u["lastAutoMomentAt"] = now_ms
                    should_auto_moment = True
                    moment_topic = reason

            today_str = datetime.now(TZ).strftime("%Y-%m-%d")
            mem_fact = str(obj.get("memory_fact") or "").strip()
            if mem_fact and mem_fact != "无" and len(mem_fact) >= 4:
                facts = u.setdefault("facts", [])
                if _add_dedup_fact(facts, mem_fact, max_len=25):
                    u["factsSinceArchive"] = int(u.get("factsSinceArchive", 0)) + 1
                    if int(u.get("factsSinceArchive", 0)) >= 8:
                        should_consolidate = True

            mem_event = str(obj.get("memory_event") or "").strip()
            if mem_event and mem_event != "无" and len(mem_event) >= 4:
                evs = u.setdefault("events", [])
                if not any(_fact_similarity(str(ev.get("text") or ""), mem_event) >= 0.80 for ev in evs):
                    evs.append({"date": today_str, "ts": now_ms, "text": mem_event})
                    if len(evs) > 40:
                        u["events"] = evs[-40:]

            self._save_persistent_state()

        if should_consolidate:
            await self._consolidate_user_archive(
                api_key=api_key,
                base_url=base_url,
                model=api_model,
                user_id=user_id,
            )

        if should_auto_moment:
            card_txt, meme_b64 = await self._generate_and_post_moment(user_id, moment_topic)
            await self._send_proactive_wechat(user_id, card_txt, meme_b64)

    async def _async_post_group_turn_judge(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        group_id: str,
        user_text: str,
        reply_text: str,
    ) -> None:
        """Evaluates group chat turn and updates the shared Group Archive (groups[group_id]) without touching single-user archives."""
        if not api_key:
            return
        api_model = self._normalize_api_model(model)
        prompt_user = (
            f"【群内本轮发言】：\n{user_text[:600]}\n\n"
            f"【你（大肥鱼）在群里的回复】：\n{reply_text[:600]}\n\n"
            "请判断本群气氛变化及是否需要记入本群公共群聊档案，只输出 JSON。"
        )
        try:
            async with httpx.AsyncClient(timeout=22.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": api_model,
                        "messages": [
                            {"role": "system", "content": GROUP_MOOD_JUDGE_SYSTEM},
                            {"role": "user", "content": prompt_user},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 240,
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
            logger.debug("Group mood judge skipped: %s", e)
            return

        async with self._state_lock:
            g = self._get_group_state(group_id)
            mood_delta = int(_clamp(round(float(obj.get("moodDelta", 0))), -5, 5))
            g["mood"] = int(_clamp(int(g.get("mood", 75)) + mood_delta, 25, 100))
            reason = str(obj.get("reason") or "").strip()[:120]
            now_ms = int(time.time() * 1000)
            if reason:
                g["lastReason"] = reason
                g["updatedAt"] = now_ms
                g["lastDecayAt"] = now_ms
            today_str = datetime.now(TZ).strftime("%Y-%m-%d")
            g_fact = str(obj.get("group_fact") or "").strip()
            if g_fact and g_fact != "无" and len(g_fact) >= 4:
                _add_dedup_fact(g.setdefault("facts", []), g_fact, max_len=25)
            g_event = str(obj.get("group_event") or "").strip()
            if g_event and g_event != "无" and len(g_event) >= 4:
                evs = g.setdefault("events", [])
                if not any(_fact_similarity(str(ev.get("text") or ""), g_event) >= 0.80 for ev in evs):
                    evs.append({"date": today_str, "ts": now_ms, "text": g_event})
                    if len(evs) > 30:
                        g["events"] = evs[-30:]
            self._save_persistent_state()

    async def _send_proactive_wechat(self, target_id: str, text: str, image_b64: str | None = None) -> bool:
        bubbles = self._split_reply_bubbles(text) if text else []
        send_img = image_b64 if self._state.get("meme_enabled", True) else None
        if not bubbles and not send_img:
            return False

        # Prefer direct WCF gateway delivery (supports multi-bubble typing rhythm reliably in background tasks)
        if bubbles:
            ok_first = await df.send_wcf_text_or_image(
                target_id, bubbles[0], send_img if len(bubbles) == 1 else None
            )
            if ok_first:
                if len(bubbles) > 1:
                    await self._send_followup_bubbles(target_id, bubbles[1:], send_img)
                return True
        elif send_img:
            if await df.send_wcf_text_or_image(target_id, "", send_img):
                return True

        # Fallback to LangBot plugin send_message API
        try:
            joined_text = "\n".join(bubbles) if bubbles else ""
            comps: list[Any] = []
            if joined_text:
                comps.append(platform_message.Plain(text=joined_text))
            if send_img:
                comps.append(platform_message.Image(base64=send_img))
            if not comps:
                return False
            chain = platform_message.MessageChain(comps)
            await self.plugin.send_message(
                bot_uuid=self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID),
                target_type="person",
                target_id=target_id,
                message_chain=chain,
            )
            return True
        except Exception:
            return False

    async def _trigger_speak_up_for_user(self, user_id: str, *, manual: bool = False) -> tuple[str | None, str | None]:
        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        if not api_key:
            return None, None
        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
        model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))

        u = self._get_user_state(user_id)
        now_dt = datetime.now(TZ)
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        now_str = f"{now_dt.strftime('%Y-%m-%d %H:%M')}（{weekdays[now_dt.weekday()]}）"
        mood_sec = self._build_mood_section(u)
        intimacy_sec = self._build_intimacy_section(u)
        mem_sec = self._recall_memories(u, "")

        sys_prompt = (
            f"{LIVE_PERSONA_BASE if self._state.get('mode', 'live') == 'live' else SWEET_PERSONA_BASE}\n\n"
            f"## 二、当前实时感知\n"
            f"- 当前时间：{now_str}\n"
            f"- 你们的关系阶段：{intimacy_sec}\n"
            f"- 鱼的心情：\n{mood_sec}\n"
            f"- 鱼的记忆：\n{mem_sec}\n\n"
            "## 三、当前特殊情境（dsh-speak-up 主动搭话）\n"
            "现在对方有一阵子没在微信上理你了，是你自己突然想找对方搭句话（不是被派活）。\n"
            "请结合当前时间（比如饭点、下午摸鱼、傍晚或深夜）、你此刻的心情或你们之前聊过的记忆，像真朋友在微信上随手发消息一样主动起个话头（1~2句短话，可用 `||` 拆成2条短气泡，严禁使用任何括号动作描写，不要机械背诵 token 数字！可在最末尾附带一个 `[表情: 偷看]` 或 `[表情: 干饭]` 等表情标签）。"
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
                raw_reply = self._clean_reply(
                    r_json.get("choices", [{}])[0].get("message", {}).get("content", "")
                )
                reply, _, meme_b64 = self._extract_text_and_meme(raw_reply)
                if reply:
                    now_ms = int(time.time() * 1000)
                    u["lastSpeakUpAt"] = now_ms
                    u["missStreak"] = 0
                    self._save_persistent_state()
                    if not manual:
                        await self._send_proactive_wechat(user_id, reply, meme_b64)
                    return reply, meme_b64
        except Exception as e:
            logger.warning("Speak-up generation failed: %s", e)
        return None, None

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
                    if not uid.startswith("wxid_") or not bool(u.get("hasPrivateChat", False)):
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
                        if random.random() < 0.28:
                            u["lastSpeakUpAt"] = now_ms
                            u["missStreak"] = 0
                            self._save_persistent_state()
                            card_txt, meme_b64 = await self._generate_and_post_moment(uid, "")
                            await self._send_proactive_wechat(uid, card_txt, meme_b64)
                        else:
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
            if val:
                return val
        except Exception:
            pass
        if default:
            return default
        return str(_load_local_secrets().get(key, "") or "").strip()

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
    def _is_incoming_sticker(text: str) -> bool:
        s = (text or "").strip()
        return "<emoji " in s or s.startswith("<msg><emoji")

    @staticmethod
    def _is_incoming_voice(text: str) -> bool:
        s = (text or "").strip()
        return "<voicemsg " in s or s.startswith("<msg><voicemsg")

    @staticmethod
    def _should_ignore_plugin_command(text: str) -> bool:
        if not text:
            return True
        stripped = text.strip()
        if not stripped:
            return True
        if (
            "<emoji " in stripped
            or stripped.startswith("<msg><emoji")
            or "<voicemsg " in stripped
            or stripped.startswith("<msg><voicemsg")
            or "<type>5</type>" in stripped
            or "<type>6</type>" in stripped
            or "<type>57</type>" in stripped
            or "<refermsg>" in stripped
        ):
            return False
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
    def _is_group_woken(message_chain: Any, raw_text: str) -> bool:
        if message_chain:
            try:
                for comp in message_chain:
                    if isinstance(comp, platform_message.At):
                        return True
            except Exception:
                pass
        s = (raw_text or "").strip()
        s_no_at = re.sub(r"^(?:@[^\s\u2005]+[\s\u2005]*)+", "", s).strip()
        if GROUP_WAKE_RE.match(s) or GROUP_WAKE_RE.match(s_no_at):
            return True
        return False

    @staticmethod
    def _reply(event_ctx: context.EventContext, text: str = "", image_b64: str | None = None) -> None:
        comps: list[Any] = []
        if text:
            comps.append(platform_message.Plain(text=text))
        if image_b64:
            comps.append(platform_message.Image(base64=image_b64))
        if not comps:
            return
        event_ctx.event.reply_message_chain = platform_message.MessageChain(comps)
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

        msg_chain = getattr(event_ctx.event, "message_chain", None)
        raw_text = self._extract_plain_text(msg_chain)
        if not raw_text:
            raw_text = str(getattr(event_ctx.event, "text_message", "") or "").strip()
        if not raw_text:
            return

        sender_id = str(getattr(event_ctx.event, "sender_id", "") or "user")
        short_sender = sender_id[-4:] if len(sender_id) >= 4 else sender_id
        launcher_id = str(getattr(event_ctx.event, "launcher_id", "") or "chat")
        # Group chats share a group-level session context; private chats are per-person
        session_key = f"group:{launcher_id}" if is_group else f"person:{launcher_id}:{sender_id}"

        # Parse WeChat Quote (<type>57</type> / <refermsg> or Quote component)
        quote_reply_txt, quote_speaker, quote_content, is_quoting_bot = df.extract_quote_context(
            msg_chain, raw_text
        )
        effective_text = quote_reply_txt if quote_content else raw_text

        raw_no_at = re.sub(r"^(?:@[^\s\u2005]+[\s\u2005]*)+", "", effective_text.strip()).strip()
        cmd_norm = re.sub(r"\s+", " ", (raw_no_at or effective_text).strip())

        # Group Chat Whitelist Gate (Scheme C + Diana Group Features + Group Archive)
        if is_group:
            enabled_groups: list[str] = self._state.setdefault("enabled_groups", [])
            if cmd_norm in ("大肥鱼 本群开启", "鲸鱼娘 本群开启", "大肥鱼 开启本群"):
                if launcher_id not in enabled_groups:
                    enabled_groups.append(launcher_id)
                self._get_group_state(launcher_id)
                self._save_persistent_state()
                resolved = self._resolve_meme_b64("OK")
                self._reply(
                    event_ctx,
                    "收到！本群已加入大肥鱼白名单并建立本群专属群聊档案。支持 @我 / 喊「大肥鱼」、引用回复、智能插话、随机复读、链接与群文件解析、多曲库点歌（发「大肥鱼 本群关闭」可随时移出白名单）。",
                    resolved[1] if resolved else None,
                )
                return
            if cmd_norm in ("大肥鱼 本群关闭", "鲸鱼娘 本群关闭", "大肥鱼 关闭本群"):
                if launcher_id in enabled_groups:
                    enabled_groups.remove(launcher_id)
                    self._save_persistent_state()
                resolved = self._resolve_meme_b64("睡觉")
                self._reply(
                    event_ctx,
                    "好嘞，本群已移出白名单，本鱼在这个群彻底闭嘴退下啦",
                    resolved[1] if resolved else None,
                )
                return
            if launcher_id not in enabled_groups:
                return

            # Silently collect group stickers into stolen_memes library
            if self._is_incoming_sticker(raw_text):
                asyncio.create_task(df.steal_incoming_sticker(raw_text, self._state, self._stolen_memes))
                return

            is_woken = self._is_group_woken(msg_chain, effective_text) or is_quoting_bot
            is_shared_card_or_file = df.has_supported_media_link_or_card(raw_text)
            if not is_woken and not is_shared_card_or_file:
                if not self._state.get("enabled", True):
                    return
                # Diana Feature 1: Random Repeat (0 LLM token, >=2 users, 30% prob, 60s cooldown)
                repeated_txt = df.check_group_random_repeat(
                    self._group_repeat_tracker,
                    self._state,
                    launcher_id,
                    sender_id,
                    msg_chain,
                    effective_text,
                )
                if repeated_txt:
                    self._reply(event_ctx, repeated_txt)
                    return

                # Diana Feature 2: Proactive Group Interjection (Decide whether to reply or stay quiet + log reason)
                if self._state.get("group_interject_enabled", True):
                    should_judge = df.record_group_message_for_interject(
                        self._group_msg_buffers,
                        self._group_interject_meta,
                        launcher_id,
                        sender_id,
                        effective_text,
                    )
                    if should_judge:
                        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
                        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
                        model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))
                        recent_lines = list(self._group_msg_buffers.get(launcher_id, []))
                        g_state = self._get_group_state(launcher_id)
                        g_mem_str = self._recall_group_memories(g_state, effective_text)
                        should_rep, rep_txt, _, used_tok = await df.evaluate_group_interject(
                            api_key=api_key,
                            base_url=base_url,
                            model=model,
                            group_id=launcher_id,
                            recent_lines=recent_lines,
                            state=self._state,
                            group_memory_str=g_mem_str,
                        )
                        if used_tok:
                            self._record_tokens(used_tok)
                        self._save_persistent_state()
                        if should_rep and rep_txt:
                            cleaned_rep = self._clean_reply(rep_txt)
                            final_txt, _, _ = self._extract_text_and_meme(cleaned_rep)
                            bubbles = self._split_reply_bubbles(final_txt)
                            if bubbles:
                                joined_log = " / ".join(bubbles)
                                buf = self._group_msg_buffers.setdefault(launcher_id, deque(maxlen=12))
                                buf.append(f"[{datetime.now(TZ).strftime('%H:%M')} 大肥鱼]: {joined_log}")
                                self._reply(event_ctx, bubbles[0])
                                if len(bubbles) > 1:
                                    asyncio.create_task(
                                        self._send_followup_bubbles(launcher_id, bubbles[1:], None)
                                    )
                return
            else:
                # Also record woken group messages into the shared group context buffer
                clean_for_buf = STRIP_TRIGGER_RE.sub("", effective_text).strip() or effective_text.strip()
                if not clean_for_buf.startswith("<"):
                    df.append_group_context_line(
                        self._group_msg_buffers, launcher_id, sender_id, clean_for_buf
                    )

        # 1. Switch / Control commands
        if cmd_norm in ("大肥鱼 开启", "鲸鱼娘 开启", "鲸鱼人设 开启", "开启大肥鱼"):
            self._state["enabled"] = True
            self._save_persistent_state()
            resolved = self._resolve_meme_b64("早上好")
            self._reply(
                event_ctx,
                "醒了醒了 别催了，刚眯了一会儿就被你拎起来，有啥事直说",
                resolved[1] if resolved else None,
            )
            return
        if cmd_norm in ("大肥鱼 关闭", "鲸鱼娘 关闭", "鲸鱼人设 关闭", "关闭大肥鱼"):
            self._state["enabled"] = False
            self._save_persistent_state()
            resolved = self._resolve_meme_b64("睡觉")
            self._reply(
                event_ctx,
                "行吧 嫌我吵那我正好下班睡觉去了，有事再喊「大肥鱼 开启」",
                resolved[1] if resolved else None,
            )
            return

        if cmd_norm in ("大肥鱼 模式 活人", "大肥鱼 活人版", "鲸鱼娘 活人版", "大肥鱼 切换"):
            cur = self._state.get("mode", "live")
            if cmd_norm == "大肥鱼 切换":
                self._state["mode"] = "sweet" if cur == "live" else "live"
            else:
                self._state["mode"] = "live"
            self._save_persistent_state()
            if self._state["mode"] == "live":
                self._reply(event_ctx, "切回活人版啦，少整那些虚头巴脑的括号旁白，咱们正常发微信唠嗑")
            else:
                self._reply(event_ctx, "哼，既然你非要我黏着你一点，那就勉为其难切到陪伴甜宠模式好啦")
            return
        if cmd_norm in ("大肥鱼 模式 甜宠", "大肥鱼 甜宠版", "鲸鱼娘 甜宠版"):
            self._state["mode"] = "sweet"
            self._save_persistent_state()
            self._reply(event_ctx, "哼，既然你非要我黏着你一点，那就勉为其难切到陪伴甜宠模式好啦")
            return

        if cmd_norm in ("大肥鱼 主动搭话 开启", "大肥鱼 开启搭话"):
            self._state["speak_up_enabled"] = True
            self._save_persistent_state()
            self._reply(event_ctx, "主动搭话开好啦，要是你半天不理我，我心情好了会自己找你说话的")
            return
        if cmd_norm in ("大肥鱼 主动搭话 关闭", "大肥鱼 关闭搭话"):
            self._state["speak_up_enabled"] = False
            self._save_persistent_state()
            self._reply(event_ctx, "行，主动搭话关掉了，你不喊我我就不主动吵你")
            return

        if cmd_norm in ("大肥鱼 开启表情", "大肥鱼 表情 开启"):
            self._state["meme_enabled"] = True
            self._save_persistent_state()
            self._reply(event_ctx, "表情包斗图模式已开启！聊到兴头上我会直接甩表情包给你")
            return
        if cmd_norm in ("大肥鱼 关闭表情", "大肥鱼 表情 关闭"):
            self._state["meme_enabled"] = False
            self._save_persistent_state()
            self._reply(event_ctx, "行，日常聊天自动发表情包关掉啦（依然可以发「大肥鱼 表情 干饭」单独点播）")
            return

        # 2. Meme / Sticker commands (yyh-001/dsh-meme 24 Dafeiyu memes)
        if cmd_norm in ("大肥鱼 表情包", "大肥鱼 斗图", "鲸鱼娘 表情包", "大肥鱼 表情"):
            all_tags = "、".join(MEME_MAP.keys())
            pick_tag = random.choice(list(self._memes_b64.keys())) if self._memes_b64 else "干饭"
            b64 = self._memes_b64.get(pick_tag)
            msg = (
                f"🎨 大肥鱼官方表情包图库（dsh-meme 24张）\n"
                f"随机投喂一张：【{pick_tag}】\n\n"
                f"📌 全部可用表情（共 {len(MEME_MAP)} 款）：\n{all_tags}\n\n"
                f"💡 发送「大肥鱼 表情 <名称>」（如：大肥鱼 表情 干饭）即可指定发送；日常聊天也会根据心情自动配图！"
            )
            self._reply(event_ctx, msg, b64)
            return

        if cmd_norm.startswith("大肥鱼 表情 ") or cmd_norm.startswith("大肥鱼 发图 "):
            q_name = re.sub(r"^大肥鱼\s*(?:表情|发图)\s*", "", cmd_norm).strip()
            resolved = self._resolve_meme_b64(q_name)
            if resolved:
                tag_hit, b64 = resolved
                self._reply(event_ctx, "", b64)
            else:
                all_tags = "、".join(MEME_MAP.keys())
                self._reply(event_ctx, f"没找到「{q_name}」这张表情包哦，目前有这 24 张：{all_tags}")
            return

        # 3. Moments / Cyber Feed commands
        if cmd_norm == "大肥鱼 发朋友圈" or cmd_norm.startswith("大肥鱼 发朋友圈 ") or cmd_norm == "大肥鱼 写朋友圈":
            topic = re.sub(r"^大肥鱼\s*(?:发朋友圈|写朋友圈)\s*", "", effective_text.strip()).strip()
            card_txt, meme_b64 = await self._generate_and_post_moment(sender_id, topic)
            self._reply(event_ctx, card_txt, meme_b64)
            return

        if cmd_norm in ("大肥鱼 朋友圈", "鲸鱼娘 朋友圈", "鱼塘朋友圈"):
            moments_list = self._state.get("moments") or []
            if not moments_list:
                card_txt, meme_b64 = await self._generate_and_post_moment(sender_id, "")
                self._reply(event_ctx, card_txt, meme_b64)
                return
            recent = moments_list[-5:]
            lines = ["📸 大肥鱼的鱼塘朋友圈动态墙（最近 5 条）：\n"]
            for m_item in reversed(recent):
                lines.append(
                    f"#{m_item.get('id')} [{m_item.get('at')}] (心情 {m_item.get('mood')}/100 | 🤍 {m_item.get('likes', 0)})\n"
                    f"「{m_item.get('text')}」 [配图:{m_item.get('meme')}]\n"
                )
            lines.append("💡 发送「大肥鱼 发朋友圈 [主题]」让她发新动态，或发「大肥鱼 点赞朋友圈」给她点赞！")
            last_meme = self._resolve_meme_b64(str(recent[-1].get("meme", "干饭")))
            self._reply(event_ctx, "\n".join(lines), last_meme[1] if last_meme else None)
            return

        if cmd_norm in ("大肥鱼 点赞朋友圈", "大肥鱼 点赞"):
            async with self._state_lock:
                moments_list = self._state.setdefault("moments", [])
                if is_group:
                    g = self._get_group_state(launcher_id)
                    g["mood"] = int(_clamp(int(g.get("mood", 75)) + 3, 25, 100))
                else:
                    u = self._get_user_state(sender_id)
                    u["mood"] = int(_clamp(int(u.get("mood", 70)) + 3, 15, 100))
                    dims = u.setdefault("dims", {})
                    dims["happy"] = int(_clamp(int(dims.get("happy", 0)) + 5, 0, 100))
                    dims["sweet"] = int(_clamp(int(dims.get("sweet", 0)) + 4, 0, 100))
                    u["lastReason"] = "对方专门给我的鱼塘朋友圈点了赞，心里美滋滋的"
                    u["updatedAt"] = int(time.time() * 1000)
                if moments_list:
                    moments_list[-1]["likes"] = int(moments_list[-1].get("likes", 0)) + 1
                    likes_cnt = moments_list[-1]["likes"]
                    m_id = moments_list[-1].get("id", 1)
                else:
                    likes_cnt = 1
                    m_id = 1
                self._save_persistent_state()
            resolved = self._resolve_meme_b64("得意")
            self._reply(
                event_ctx,
                f"算你有眼光，朋友圈动态 #{m_id} 收到你的赞啦（当前 {likes_cnt} 赞），心情 +3",
                resolved[1] if resolved else None,
            )
            return

        if cmd_norm in ("大肥鱼 搭话", "大肥鱼 找我说话"):
            target_recv = launcher_id if is_group else sender_id
            reply_su, meme_b64 = await self._trigger_speak_up_for_user(sender_id, manual=True)
            send_meme = meme_b64 if self._state.get("meme_enabled", True) else None
            if reply_su:
                bubbles = self._split_reply_bubbles(reply_su)
                if len(bubbles) > 1:
                    self._reply(event_ctx, bubbles[0], None)
                    asyncio.create_task(self._send_followup_bubbles(target_recv, bubbles[1:], send_meme))
                else:
                    self._reply(event_ctx, bubbles[0] if bubbles else reply_su, send_meme)
            else:
                self._reply(event_ctx, "这会儿还没配置好钥匙（DeepSeek API Key），或者正犯困呢，等会儿再找你唠")
            return

        # 4. Mood panel command (dsh-feiyu-mood)
        if cmd_norm in ("大肥鱼 心情", "鲸鱼娘 心情", "鱼的心情"):
            if is_group:
                g = self._get_group_state(launcher_id)
                g_mood = int(g.get("mood", 75))
                msg = (
                    f"🐟 大肥鱼在本群当前气氛心情：{g_mood}/100（{self._mood_label(g_mood)}）\n"
                    f"💭 本群状态：{g.get('lastReason', '在群里看大家聊天')}\n"
                    f"（私聊发「大肥鱼 心情」可查看你与大肥鱼的个人七维情绪面板）"
                )
                self._reply(event_ctx, msg)
                return
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

        # 5. Memory & Archive Consolidation commands (Group Archive in Group Chat vs Personal 8-Chapter Archive in Private Chat)
        if cmd_norm.startswith("大肥鱼 记住 ") or cmd_norm.startswith("大肥鱼 记住："):
            fact_to_add = re.sub(r"^(?:@[^\s\u2005]+[\s\u2005]*)*大肥鱼\s*记住[：:\s]*", "", effective_text.strip()).strip()
            if fact_to_add:
                if is_group:
                    g = self._get_group_state(launcher_id)
                    _add_dedup_fact(g.setdefault("facts", []), fact_to_add, max_len=25)
                    self._save_persistent_state()
                    self._reply(event_ctx, f"记进咱们群的群聊档案里啦：「{fact_to_add}」")
                else:
                    u = self._get_user_state(sender_id)
                    u["hasPrivateChat"] = True
                    if _add_dedup_fact(u.setdefault("facts", []), fact_to_add, max_len=25):
                        u["factsSinceArchive"] = int(u.get("factsSinceArchive", 0)) + 1
                    self._save_persistent_state()
                    self._reply(event_ctx, f"记下了记下了：「{fact_to_add}」，我记性好着呢")
                return

        if cmd_norm in ("大肥鱼 整理档案", "大肥鱼 生成档案", "大肥鱼 重整档案", "鲸鱼娘 整理档案"):
            if is_group:
                g = self._get_group_state(launcher_id)
                g["facts"] = _dedup_facts_in_place(list(g.get("facts") or []))
                self._save_persistent_state()
                self._reply(event_ctx, self._format_group_archive_card(launcher_id, g))
                return
            api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
            base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
            model = self._get_config_str("deepseek_model", DEFAULT_MODEL)
            _, card_msg = await self._consolidate_user_archive(
                api_key=api_key,
                base_url=base_url,
                model=model,
                user_id=sender_id,
            )
            self._reply(event_ctx, card_msg)
            return

        if cmd_norm in ("大肥鱼 记忆", "鲸鱼娘 记忆", "鱼的记忆", "大肥鱼 档案", "大肥鱼 群记忆", "大肥鱼 群档案"):
            if is_group:
                g = self._get_group_state(launcher_id)
                self._reply(event_ctx, self._format_group_archive_card(launcher_id, g))
                return
            u = self._get_user_state(sender_id)
            self._reply(event_ctx, self._format_archive_card(u))
            return

        if cmd_norm in ("大肥鱼 失忆", "鲸鱼娘 失忆"):
            async with self._state_lock:
                self._histories.pop(session_key, None)
                if is_group:
                    self._state.setdefault("groups", {}).pop(launcher_id, None)
                    self._group_msg_buffers.pop(launcher_id, None)
                else:
                    self._state.get("users", {}).pop(sender_id, None)
                self._save_persistent_state()
            if is_group:
                self._reply(event_ctx, "行，本群的群聊档案和刚才聊的话已经清空重置啦（大家的私聊记忆不受影响）")
            else:
                self._reply(event_ctx, "行，关于你的长期档案、八章节记忆和刚才聊的话全都清空重置啦，咱们重新认识")
            return

        # 6. Token / Balance command (dsh-whale-widget)
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

        # 6.5 Diana Enhanced Features Commands (Repeat, Group Interject Log, Stolen Memes, Music, Web Search)
        if cmd_norm in ("大肥鱼 开启复读", "鲸鱼娘 开启复读"):
            self._state["repeat_enabled"] = True
            self._save_persistent_state()
            self._reply(event_ctx, "随机复读机已开启！白名单群内满 2 人连续发相同纯文字时，按 30% 概率跟读（冷却 60 秒）")
            return
        if cmd_norm in ("大肥鱼 关闭复读", "鲸鱼娘 关闭复读"):
            self._state["repeat_enabled"] = False
            self._save_persistent_state()
            self._reply(event_ctx, "随机复读机已关闭，不当复读机啦")
            return
        if cmd_norm.startswith("大肥鱼 复读设置 "):
            nums = re.findall(r"\d+", cmd_norm)
            if len(nums) >= 3:
                min_u = max(2, min(10, int(nums[0])))
                prob_pct = max(1, min(100, int(nums[1])))
                cd_s = max(5, min(3600, int(nums[2])))
                self._state["repeat_config"] = {"min_users": min_u, "prob": prob_pct / 100.0, "cooldown_sec": cd_s}
                self._save_persistent_state()
                self._reply(event_ctx, f"复读参数已更新：满 {min_u} 人连续相同纯文字、{prob_pct}% 概率跟读、冷却 {cd_s} 秒")
            else:
                self._reply(event_ctx, "格式示例：「大肥鱼 复读设置 2人 30% 60秒」")
            return

        if cmd_norm in ("大肥鱼 开启群搭话", "大肥鱼 本群开启搭话"):
            self._state["group_interject_enabled"] = True
            self._save_persistent_state()
            self._reply(event_ctx, "群聊智能主动插话已开启！在白名单群里不用 @，我也会自己判断什么时候顺嘴接梗、什么时候保持安静")
            return
        if cmd_norm in ("大肥鱼 关闭群搭话", "大肥鱼 本群关闭搭话"):
            self._state["group_interject_enabled"] = False
            self._save_persistent_state()
            self._reply(event_ctx, "群聊智能主动插话已关闭，现在白名单群里只有 @我 或喊「大肥鱼」时才回话")
            return
        if cmd_norm in ("大肥鱼 搭话记录", "大肥鱼 插话记录", "大肥鱼 决策记录", "鲸鱼娘 搭话记录"):
            self._reply(event_ctx, df.format_interject_logs(self._state))
            return

        if cmd_norm in ("大肥鱼 开启偷图", "鲸鱼娘 开启偷图"):
            self._state["steal_meme_enabled"] = True
            self._save_persistent_state()
            self._reply(event_ctx, "表情包偷图模式已开启！聊天里见过的有趣表情包我都会偷偷收进图库学起来")
            return
        if cmd_norm in ("大肥鱼 关闭偷图", "鲸鱼娘 关闭偷图"):
            self._state["steal_meme_enabled"] = False
            self._save_persistent_state()
            self._reply(event_ctx, "好嘞，已关闭表情包偷图，只用自带的 24 张官方表情包")
            return
        if cmd_norm in ("大肥鱼 表情库", "大肥鱼 偷图库", "鲸鱼娘 表情库"):
            stolen = self._stolen_memes
            if not stolen:
                self._reply(event_ctx, "📦 自带 24 张官方表情包，目前偷图库里还没攒下群友的表情包呢，快发几张表情包喂给我！")
                return
            sample = random.choice(stolen)
            tags_summary = "、".join(
                f"{m.get('at', '')}[{','.join(m.get('tags') or ['搞笑'])}]" for m in stolen[-6:]
            )
            msg = (
                f"📦 【大肥鱼表情包收藏库】\n"
                f"• 官方内置：{len(self._memes_b64)} 张 | 偷图收藏：{len(stolen)}/50 张\n"
                f"• 最近偷图：{tags_summary}\n"
                f"（下面随机甩一张从你们这儿偷学来的表情包！）"
            )
            self._reply(event_ctx, msg, sample.get("b64"))
            return

        # Diana Feature 7: Multi-library Music Player (NetEase Cloud Music + QQ Music auto switch)
        if (
            cmd_norm.startswith("大肥鱼 点歌 ")
            or cmd_norm.startswith("鲸鱼娘 点歌 ")
            or cmd_norm.startswith("点歌 ")
            or cmd_norm.startswith("来首歌 ")
        ):
            song_q = re.sub(r"^(?:大肥鱼\s*|鲸鱼娘\s*)?(?:点歌|来首歌)\s*", "", cmd_norm).strip()
            if not song_q:
                self._reply(event_ctx, "想听啥歌直说歌名呀，比如发：「大肥鱼 点歌 晴天 周杰伦」")
                return
            song = await df.search_and_fetch_music(song_q)
            if not song:
                self._reply(event_ctx, f"翻遍了网易云和 QQ 音乐都没搜到「{song_q}」，换个歌名或者加上歌手名再试试？")
                return
            target_recv = launcher_id if is_group else sender_id
            card_ok, audio_ok = await df.send_music_to_wechat(target_recv, song)
            cover_b64 = await df._download_image_as_jpeg_b64(song.get("cover_url") or "")
            audio_note = (
                "✅ 已同步将歌曲 MP3 音频文件直接发到聊天窗口，点开就能听！"
                if audio_ok
                else f"⚠️ 该曲目受版权限制无法直接提取 MP3 音频流，已为你提供 {song.get('source')} 歌曲来源页链接。"
            )
            msg = (
                f"🎵 【大肥鱼点歌台 · {song.get('source')}】\n"
                f"• 歌名：{song.get('title')}\n"
                f"• 歌手：{song.get('artist')}\n"
                + (f"• 专辑：{song.get('album')}\n" if song.get("album") else "")
                + f"• 链接：{song.get('page_url')}\n"
                + f"{audio_note}"
            )
            self._reply(event_ctx, msg, None if card_ok else cover_b64)
            return

        # 7. Status & Reset commands
        if cmd_norm in ("大肥鱼 重置", "鲸鱼娘 重置", "清除记忆", "重置对话", "大肥鱼 清除记忆"):
            self._histories.pop(session_key, None)
            self._user_docs.pop(sender_id, None)
            self._reply(event_ctx, "行啦，刚才聊的这几句翻篇了！长期档案还留着呢（要彻底清空请发「大肥鱼 失忆」），想聊啥直说")
            return

        if cmd_norm in ("大肥鱼 状态", "鲸鱼娘 状态", "鲸鱼人设 状态", "大肥鱼 帮助"):
            hist = self._histories.get(session_key, [])
            turns = len(hist) // 2
            enabled_flag = self._state.get("enabled", True)
            mode_name = "微信活人版（多气泡/去人设复读/能量匹配）" if self._state.get("mode", "live") == "live" else "陪伴甜宠版"
            su_flag = "开启" if self._state.get("speak_up_enabled", True) else "关闭"
            grp_su_flag = "开启" if self._state.get("group_interject_enabled", True) else "关闭"
            rep_flag = "开启" if self._state.get("repeat_enabled", True) else "关闭"
            stolen_cnt = len(self._stolen_memes)
            meme_flag = f"开启（官方 {len(self._memes_b64)} 张 + 偷图 {stolen_cnt} 张）" if self._state.get("meme_enabled", True) else "关闭"
            grp_cnt = len(self._state.get("enabled_groups", []))
            stats = self._record_tokens(0)
            if is_group:
                g = self._get_group_state(launcher_id)
                mood_val = int(g.get("mood", 75))
                mem_line = f"• 本群上下文：{turns} 轮 | 群聊档案：{len(g.get('facts', []))} 条群记忆 / {len(g.get('events', []))} 条群事件（私聊熟人只读识别）\n"
            else:
                u = self._get_user_state(sender_id)
                mood_val = int(u.get("mood", 70))
                arch_cnt = sum(len(v) for v in (u.get("archive") or {}).values())
                mem_line = f"• 短期上下文：{turns} 轮 | 个人记忆：{len(u.get('facts', []))} 条事实 / {arch_cnt} 条归档 / {len(u.get('events', []))} 条事件\n"
            msg = (
                f"🐋 大肥鱼（活人感全家桶 × Diana 全能版 v0.4.0）\n"
                f"• 当前状态：{'在岗干饭中 🍚' if enabled_flag else '下班午睡中 💤'}\n"
                f"• 人设模式：{mode_name}\n"
                f"• 当前心情：{mood_val}/100（{self._mood_label(mood_val)}）\n"
                + mem_line
                + f"• 表情包库：{meme_flag}\n"
                f"• 群白名单：已开 {grp_cnt} 个群 | 群智能插话：{grp_su_flag} | 随机复读：{rep_flag}\n"
                f"• 全能扩展：联网搜索 + 链接解析(B站/X/小红书/抖音) + 文件解析(PDF/Office/EPUB) + 点歌 + 引用感知\n"
                f"• 今日偷吃白饭：{stats.get('today_tokens', 0)} tokens | 私聊搭话：{su_flag}\n\n"
                f"📋 微信口令速查：\n"
                f"- 大肥鱼 点歌 <歌名> | 大肥鱼 搜索 <问题>\n"
                f"- 大肥鱼 搭话记录 | 大肥鱼 表情库 | 大肥鱼 复读设置 2 30 60\n"
                f"- 大肥鱼 心情 | 大肥鱼 记忆（群内查群档案/私聊查个人档案） | 大肥鱼 整理档案\n"
                f"- 大肥鱼 发朋友圈 | 大肥鱼 朋友圈 | 大肥鱼 点赞\n"
                f"- 大肥鱼 本群开启 | 大肥鱼 本群关闭（群聊白名单控制）"
            )
            self._reply(event_ctx, msg)
            return

        if not self._state.get("enabled", True):
            return

        if self._should_ignore_plugin_command(raw_text):
            return

        is_sticker_in = self._is_incoming_sticker(raw_text)
        is_voice_in = self._is_incoming_voice(raw_text)
        is_file_in = df.is_incoming_wechat_file(raw_text)
        vision_b64: str | None = None
        parsed_link_card: str | None = None
        parsed_link_img_b64: str | None = None
        hist_user_text: str = ""

        if is_sticker_in:
            if is_group:
                return
            vision_b64 = await self._fetch_sticker_strip_b64(raw_text)
            if vision_b64:
                cleaned_text = "（对方刚刚给你发了这张微信表情包！如果是动图则已抽帧拼成左中右三格。请用 DeepSeek-V4.1-Flash 视觉看清表情包里的画面/角色动作/文字，用大肥鱼的微信活人短句接梗回应一句，并在最末尾附上 `[表情: 名称]` 甩一张你的表情包回去！）"
            else:
                cleaned_text = "（对方刚刚给你发了一张微信表情包跟你斗图！请用活人短句回敬一句，并在最末尾附上 `[表情: 名称]` 甩一张你的表情包回去！）"
            hist_user_text = "[发来了一张微信表情包]"
        elif is_voice_in:
            if is_group:
                return
            voice_text, voice_tone, voice_dur = await self._transcribe_incoming_voice(raw_text)
            tone_part = f"，语气特征[{voice_tone}]" if voice_tone else ""
            if voice_text:
                cleaned_text = (
                    f"（对方刚刚给你发了一条 {voice_dur} 秒的微信语音{tone_part}，"
                    f"语音识别内容是：「{voice_text}」。"
                    "请像在微信里刚听完对方这条语音一样，结合对方说的话和语气，用大肥鱼的微信活人短句自然回应！）"
                )
                hist_user_text = f"[微信语音 {voice_dur}秒{tone_part}] {voice_text}"
            else:
                cleaned_text = (
                    f"（对方刚刚给你发了一条 {voice_dur} 秒的微信语音{tone_part}，"
                    "但里面几乎没听清说话声，只有一点背景动静或呼吸声。请用大肥鱼的活人口吻吐槽或追问一句！）"
                )
                hist_user_text = f"[微信语音 {voice_dur}秒（未听清内容）]"
        elif is_file_in:
            # Diana Feature 6: File Parsing (PDF / Word / Excel / PPT / EPUB / TXT / Code) offloaded to thread pool
            fname, ftext = await asyncio.to_thread(df.parse_wechat_file_xml, raw_text)
            if ftext:
                self._user_docs[sender_id] = {"name": fname, "text": ftext, "ts": time.time()}
                cleaned_text = (
                    f"（对方刚刚发来了一个文件《{fname}》，已为你提取出文件中的核心文本内容如下：\n"
                    f"--------------------\n{ftext[:7000]}\n--------------------\n"
                    "请用大肥鱼的活人口吻，先简明扼要地帮对方总结这份文件的重点内容，并问问对方具体想看哪一部分！）"
                )
                hist_user_text = f"[发送了文件《{fname}》]"
            else:
                self._reply(event_ctx, f"收到文件《{fname}》啦，但这会儿文件还在下载或者格式受限没能提取出文字，你可以直接把里面的关键段落发出来给我看")
                return
        else:
            # Check if message contains a Bilibili / X(Twitter) / YouTube / Xiaohongshu / Douyin link or WeChat shared card
            parsed_link_card, parsed_link_img_b64 = await df.extract_and_parse_link(raw_text)
            if parsed_link_card:
                cleaned_text = (
                    f"（对方刚刚分享了一个链接/卡片，已自动解析出详情如下：\n{parsed_link_card}\n"
                    "请用大肥鱼的微信活人短句（1~2句话，不许打括号动作）对这个链接/视频/推文/主页内容顺嘴锐评或接梗一句！）"
                )
                hist_user_text = f"[分享链接] {parsed_link_card[:120]}"
            else:
                if is_group and not (self._is_group_woken(msg_chain, effective_text) or is_quoting_bot):
                    return
                cleaned_text = STRIP_TRIGGER_RE.sub("", effective_text).strip()
                if not cleaned_text:
                    cleaned_text = effective_text.strip()

                if self._should_ignore_plugin_command(cleaned_text):
                    return
                if not is_group:
                    vision_b64 = await self._extract_image_b64_from_chain(getattr(event_ctx.event, "message_chain", None))

                if quote_content:
                    hist_user_text = f"[引用「{quote_speaker}: {quote_content[:60]}」] {cleaned_text}"
                    cleaned_text = (
                        f"【对方引用了消息「{quote_speaker}: {quote_content}」并回复】：\n{cleaned_text}"
                    )
                else:
                    hist_user_text = cleaned_text

        if await self._is_duplicate(getattr(event_ctx, "query_id", None)):
            return

        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        if not api_key:
            self._reply(event_ctx, "粮仓还没钥匙呢！请先在 LangBot 插件配置中填入你的 DeepSeek API Key 再喊我干饭")
            return

        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
        model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))
        max_turns = max(1, min(20, self._get_config_int("max_history_turns", 12)))

        now = time.time()

        # Evict expired document & session caches to prevent unbounded memory growth
        for k_doc in [k for k, v in list(self._user_docs.items()) if now - float(v.get("ts", 0)) > 1800]:
            self._user_docs.pop(k_doc, None)
        for k_hist, v_hist in list(self._histories.items()):
            if not v_hist or (now - v_hist[-1][0]) > 7200:
                self._histories.pop(k_hist, None)

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

        # On-demand Token/Balance perception: only expose raw token/balance numbers when relevant to avoid repetitive catchphrases
        mention_token_topic = any(kw in cleaned_text.lower() for kw in TOKEN_TOPIC_KEYWORDS) or bal_str.startswith(("¥0.", "¥1."))
        if mention_token_topic:
            grain_line = (
                f"- **粮仓状态（对方正聊到相关话题，可自然引用）**：DeepSeek 账户真实余额 `{bal_str}`；"
                f"今天已消耗 `{stats.get('today_tokens', 0)} tokens`（上一口 `{stats.get('last_tokens', 0)} tokens`）。\n"
            )
        else:
            grain_line = (
                "- **粮仓状态**：口粮充足（本轮对话与 token/余额无关，切勿在回复中主动背诵 token 数量或账户余额，专心聊眼前的事）。\n"
            )

        # Include cached file context if user uploaded a document within the last 30 minutes
        doc_sec = ""
        cached_doc = self._user_docs.get(sender_id)
        if cached_doc and (now - float(cached_doc.get("ts", 0))) < 1800 and not is_file_in:
            doc_sec = (
                f"\n\n## 三、对方最近上传的文档缓存（《{cached_doc.get('name')}》，若对方追问文档内容请据此回答）\n"
                f"{str(cached_doc.get('text', ''))[:4500]}"
            )

        base_persona = LIVE_PERSONA_BASE if self._state.get("mode", "live") == "live" else SWEET_PERSONA_BASE

        if is_group:
            # Group Chat: Use Group Archive (groups[launcher_id]) + Read-Only Private Familiarity + Recent Group Chat Flow
            g_state = self._get_group_state(launcher_id)
            g_state["lastChatAt"] = int(now * 1000)
            meme_cooldown_state = g_state
            g_mood = int(g_state.get("mood", 75))
            g_reason = g_state.get("lastReason", "在群里看大家聊天")
            g_mem_sec = self._recall_group_memories(g_state, cleaned_text)
            familiar_sec = self._recall_readonly_private_familiarity(sender_id)
            recent_grp_lines = list(self._group_msg_buffers.get(launcher_id, []))[-8:]
            grp_ctx_block = (
                "\n".join(recent_grp_lines)
                if recent_grp_lines
                else "（暂无前序群聊记录）"
            )
            dynamic_system_prompt = (
                f"{base_persona}\n\n"
                f"## 二、当前群聊实时感知（你在微信群「{launcher_id[:10]}」中跟群友互动）\n"
                f"- **当前真实时间**：{time_str}\n"
                f"{grain_line}"
                f"- **本群气氛心情**：{g_mood}/100（{g_reason}）\n"
                f"{familiar_sec}\n"
                f"- **本群专属群聊档案（群梗与近期话题）**：\n{g_mem_sec}\n"
                f"- **群里刚刚聊的上下文（帮助你理解大家在讨论什么）**：\n{grp_ctx_block}"
                f"{doc_sec}"
            )
        else:
            # Private Chat: Full personal mood + intimacy tier + 8-chapter personal archive
            u = self._get_user_state(sender_id)
            u["hasPrivateChat"] = True
            u["lastChatAt"] = int(now * 1000)
            u["missStreak"] = 0
            meme_cooldown_state = u
            intimacy_sec = self._build_intimacy_section(u)
            mood_sec = self._build_mood_section(u)
            mem_sec = self._recall_memories(u, cleaned_text)
            dynamic_system_prompt = (
                f"{base_persona}\n\n"
                f"## 二、当前私聊实时感知（系统每轮自动刷新，自然融入语气，不要机械背诵）\n"
                f"- **当前真实时间**：{time_str}\n"
                f"- **你们的亲密度与关系阶段**：{intimacy_sec}\n"
                f"{grain_line}"
                f"- **鱼的心情（dsh-feiyu-mood）**：\n{mood_sec}\n"
                f"- **鱼的记忆（dsh-fish-memory）**：\n{mem_sec}"
                f"{doc_sec}"
            )

        # If user explicitly used "大肥鱼 搜索 <问题>", pre-run parallel web search right away
        if cmd_norm.startswith("大肥鱼 搜索 ") or cmd_norm.startswith("鲸鱼娘 搜索 "):
            sq = re.sub(r"^(?:大肥鱼|鲸鱼娘)\s*搜索\s*", "", cmd_norm).strip()
            if sq:
                search_res = await df.search_web_parallel([sq])
                cleaned_text = (
                    f"对方让你联网搜索：「{sq}」\n"
                    f"【并行联网检索结果】：\n{search_res}\n\n"
                    "请结合以上最新检索结果，用大肥鱼的活人口吻准确回答对方！"
                )

        user_msg_for_llm = f"[群友{short_sender}]: {cleaned_text}" if is_group else cleaned_text

        messages: list[dict[str, Any]] = [{"role": "system", "content": dynamic_system_prompt}]
        for _, r, c in history:
            messages.append({"role": r, "content": c})
        if vision_b64:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_msg_for_llm},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{vision_b64}"}},
                    ],
                }
            )
        else:
            messages.append({"role": "user", "content": user_msg_for_llm})

        tool_calls: list[Any] = []
        try:
            async with httpx.AsyncClient(timeout=50.0) as client:
                req_payload: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": 1.1,
                    "max_tokens": 800,
                }
                # Enable Diana Feature 4 (Native Web Search & Webpage Reader Tool Calling) for text chats
                if not vision_b64 and not parsed_link_card and not is_file_in and self._state.get("web_search_enabled", True):
                    req_payload["tools"] = df.DEEPSEEK_SEARCH_TOOLS
                    req_payload["tool_choice"] = "auto"

                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=req_payload,
                )
                resp.raise_for_status()
                data = resp.json()
                used_tokens = int((data.get("usage") or {}).get("total_tokens", 0))
                self._record_tokens(used_tokens)

                choice_msg = data.get("choices", [{}])[0].get("message", {}) or {}
                tool_calls = choice_msg.get("tool_calls") or []

                # Execute parallel web_search / read_webpage tool calls if requested by DeepSeek
                if tool_calls:
                    messages.append(choice_msg)
                    for tc in tool_calls[:3]:
                        fn = (tc.get("function") or {}).get("name", "")
                        raw_args = (tc.get("function") or {}).get("arguments") or "{}"
                        try:
                            args_obj = json.loads(raw_args)
                        except Exception:
                            args_obj = {}
                        if fn == "web_search":
                            qs = args_obj.get("queries") or []
                            if isinstance(qs, str):
                                qs = [qs]
                            tool_out = await df.search_web_parallel(qs)
                        elif fn == "read_webpage":
                            tool_out = await df.read_webpage_content(str(args_obj.get("url") or ""))
                        else:
                            tool_out = "未知工具"
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.get("id", ""),
                                "content": tool_out[:3500],
                            }
                        )
                    resp2 = await client.post(
                        f"{base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
                            "messages": messages,
                            "temperature": 1.0,
                            "max_tokens": 800,
                        },
                    )
                    resp2.raise_for_status()
                    data2 = resp2.json()
                    used2 = int((data2.get("usage") or {}).get("total_tokens", 0))
                    self._record_tokens(used2)
                    raw_reply = (
                        data2.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        .strip()
                    )
                else:
                    raw_reply = (choice_msg.get("content") or "").strip()

                cleaned_reply = self._clean_reply(raw_reply)
                reply_text, meme_tag, meme_b64 = self._extract_text_and_meme(cleaned_reply)

                # Diana Feature 3: Steal incoming sticker and mix stolen stickers when replying
                if is_sticker_in:
                    asyncio.create_task(df.steal_incoming_sticker(raw_text, self._state, self._stolen_memes, meme_tag))

                user_asked_meme = any(
                    kw in effective_text for kw in ("表情包", "发个表情", "发张表情", "斗图", "来张图", "发个图")
                )
                force_meme = bool(is_sticker_in or user_asked_meme)
                if force_meme and not meme_b64 and self._memes_b64:
                    meme_tag = random.choice(["得意", "干饭", "猫爪", "偷看", "飘了"])
                    meme_b64 = self._memes_b64.get(meme_tag)

                if meme_b64:
                    meme_b64 = df.pick_stolen_or_official_meme(meme_tag or "搞笑", meme_b64, self._state, self._stolen_memes)

                # Scheme 1: Code-level cooldown (min 4 text turns & 180s interval) + 15% real RNG gate for normal chats
                turns_since_meme = int(meme_cooldown_state.get("turnsSinceMeme", 99)) + 1
                last_meme_at = int(meme_cooldown_state.get("lastMemeAt", 0))
                if not force_meme and meme_b64:
                    if turns_since_meme < 5 or (int(now) - last_meme_at) < 180 or random.random() >= 0.15:
                        meme_b64 = None

                if meme_b64 and self._state.get("meme_enabled", True):
                    meme_cooldown_state["turnsSinceMeme"] = 0
                    meme_cooldown_state["lastMemeAt"] = int(now)
                else:
                    meme_cooldown_state["turnsSinceMeme"] = turns_since_meme
                self._save_persistent_state()
        except Exception as exc:
            logger.warning("Whale persona DeepSeek API call failed: %s", exc)
            if parsed_link_card:
                self._reply(event_ctx, parsed_link_card, parsed_link_img_b64)
            return

        if parsed_link_card:
            reply_text_clean = reply_text.replace("||", " ")
            reply_text = f"{parsed_link_card}\n━━━━━━━━━━━━━━\n🐟 大肥鱼：{reply_text_clean}" if reply_text_clean else parsed_link_card

        if not reply_text and not meme_b64 and not parsed_link_img_b64:
            return

        allow_split = not (parsed_link_card or is_file_in or bool(tool_calls))
        bubbles = self._split_reply_bubbles(reply_text, allow_split=allow_split)
        reply_flat = " ".join(bubbles) if bubbles else reply_text

        if not hist_user_text:
            hist_user_text = "[发来了一张微信表情包]" if is_sticker_in else cleaned_text
        hist_entry_user = f"[群友{short_sender}] {hist_user_text}" if is_group else hist_user_text
        history.append((now, "user", hist_entry_user))
        history.append((now, "assistant", reply_flat))
        if len(history) > max_turns * 2:
            history = history[-(max_turns * 2) :]
        self._histories[session_key] = history

        if is_group:
            buf = self._group_msg_buffers.setdefault(launcher_id, deque(maxlen=12))
            buf.append(f"[{datetime.now(TZ).strftime('%H:%M')} 大肥鱼]: {reply_flat[:100]}")
            asyncio.create_task(
                self._async_post_group_turn_judge(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    group_id=launcher_id,
                    user_text=hist_entry_user,
                    reply_text=reply_flat,
                )
            )
        else:
            asyncio.create_task(
                self._async_post_turn_judge(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    user_id=sender_id,
                    user_text=hist_user_text,
                    reply_text=reply_flat,
                )
            )

        send_b64 = parsed_link_img_b64 or (meme_b64 if self._state.get("meme_enabled", True) else None)
        if len(bubbles) <= 1:
            first_txt = bubbles[0] if bubbles else reply_text
            self._reply(event_ctx, first_txt, send_b64)
        else:
            self._reply(event_ctx, bubbles[0], None)
            asyncio.create_task(
                self._send_followup_bubbles(
                    target_id=launcher_id if is_group else sender_id,
                    is_group=is_group,
                    bubbles=bubbles[1:],
                    final_image_b64=send_b64,
                )
            )
