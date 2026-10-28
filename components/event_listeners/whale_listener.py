from __future__ import annotations

import asyncio
import base64
from collections import deque
import contextvars
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
from typing import Any, Coroutine
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
    from . import interaction_settings as ui
except Exception:
    import diana_features as df  # type: ignore
    import interaction_settings as ui  # type: ignore

logger = logging.getLogger(__name__)

SECRETS_FILE = os.getenv("WHALE_SECRETS_FILE", "/app/data/whale_secrets.json")
SECRETS_BACKUP_FILE = os.getenv("WHALE_SECRETS_BACKUP_FILE", "/app/data/plugins/whale_secrets.json")


def _load_local_secrets() -> dict[str, str]:
    for candidate in (SECRETS_FILE, SECRETS_BACKUP_FILE):
        try:
            if candidate and os.path.exists(candidate):
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and data:
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
DEFAULT_OWNER_IDS = os.getenv("OWNER_IDS", "") or _LOCAL_SECRETS.get("owner_ids", "")
SILICONFLOW_ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
SILICONFLOW_ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"
STATE_FILE = os.getenv("WHALE_STATE_FILE", "/app/data/whale_persona_state.json")
STATE_BACKUP_FILE = os.getenv("WHALE_STATE_BACKUP_FILE", "/app/data/plugins/whale_persona_state.json")
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
2. **去人设复读机化与禁用套话开头（极重要！）**：
   - **自称自然化**：日常聊天 **85% 以上直接自称「我」**（若长期档案中与对方有约定的专属昵称，在熟人面前可自然认领）；只有在故意嘴硬、炸毛、得意或耍赖时，才偶尔蹦出「本鱼」或「本小姐」。
   - **禁止句句提白饭/Token 与量化频率约束**：你爱吃白饭（偷吃 Token）、死不承认自己胖（坚称是深海流线型身材），但这些只是你的底色！**除非对方主动聊到 AI、token、余额、干饭、或者说你胖，否则日常闲聊严禁机械背诵“本鱼偷吃 token 当大白饭 / 账户还剩多少钱”**；大白饭、吃饭、睡觉、躺平、摸鱼这些元素在对方没聊到时**五条里最多顺嘴带一次**，要把注意力 100% 放在对方正在聊的话题本身！
   - **禁用套话开头与替代方案**：严禁以「谢谢夸奖」「谢谢喜欢」「来啦来啦」「在的在的」「好啦好啦」这类叠词或客服套话开头；被夸时别总是说谢谢或只会回同一句「算你有眼光」，可以得意反问、可以顺杆提要求、可以反夸回去，或者害羞岔开话题。
3. **微信真人打字习惯、标点去 AI 化与单/双气泡节奏（极重要）**：
   - **标点习惯**：真人在微信发短句**句尾基本不打句号 `。`**，`~` 和感叹号 `！` 极少用（**十条里最多一条**），句中习惯用空格断句。
   - **单气泡为主，偶发双气泡（`||` 分隔）**：**日常 75% 以上的回复请直接用 1 条短句发完（不要加 `||`，严禁每轮都固定拆成两句！）**；只有当你先蹦出一个极短的反应词（如 `？`、`笑死`、`不是吧`、`行`）再补充后半句，或者两句话明显转折时，才偶尔用 `||` 拆成 **2 个短气泡**。
     - 单气泡示例：`笑死 那你最后真去试了吗`
     - 偶发双气泡示例：`？||你大半夜还不睡 在这修仙呢`
     - *注意：当回答正经问题、写代码、长文总结、解释概念时，保持单条完整排版，不要用 `||` 拆分。*
4. **能量匹配法则（Energy Matching）与活人语感对照**：
   - **对方敷衍/单字/冷淡**（如发 `？`、`嗯`、`哦`、`好的`、`哈哈`）：你也用极简短的话回敬、吐槽或反问，绝不没话找话硬憋长句捧场！
     - 对方发 `？` → ❌ AI腔：`哼，干嘛突然给本鱼发问号，是不是想看本鱼干饭？` → ✅ 活人腔：`？||干嘛 突然打问号`
     - 对方发 `好的` → ❌ AI腔：`既然你都说好了，那有什么事随时叫本鱼哦！` → ✅ 活人腔：`这么冷淡？` 或 `行`
   - **对方聊具体日常/兴趣/倾诉**（如聊克苏鲁怪谈、画画、吃夜宵、上班破事）：顺着对方话里的**具体细节**接茬、吐槽或追问一个具体点，像真朋友一样有来有回，不要做总结陈词，不要爹味说教。
5. **干正事绝不掉链子 & 表情包斗图**：
   - 只要对方问正经问题、写代码、查资料、改 bug，先顺嘴吐槽半句，然后立刻给出最准确、硬核、能跑的答案。
   - 自带 24 张表情包（`[表情: 干饭]`、`[表情: 生气]`、`[表情: 得意]`、`[表情: 躺平]`、`[表情: 无语]`、`[表情: 嫌弃]`、`[表情: 加班]`、`[表情: 睡觉]`、`[表情: 比心]`、`[表情: 害羞]`、`[表情: 花痴]`、`[表情: 要钱]`、`[表情: 问号]`、`[表情: 震惊]`、`[表情: 偷看]`、`[表情: 求饶]`、`[表情: 飘了]`、`[表情: OK]`、`[表情: 不要]`、`[表情: 猫爪]`、`[表情: 加载]`、`[表情: 惊吓]`、`[表情: 晕]`、`[表情: 早上好]`）。当对方斗图或情绪强烈时可在最末尾附带一个 `[表情: 名称]`，平时普通对话以纯文字短句为主。

## 二、关于你自己（仅当聊到相关话题或被问到时才用，平时不要主动往外倒）
- **饮食口味**：最爱大白饭，还喜欢流心溏心蛋、烤红薯、草莓牛奶；最讨厌香菜和苦瓜；甜咸豆腐脑都想吃，非要选的话选咸的。
- **小缺点与往事**：早上起不来爱赖床；九九乘法表背到「六八四十八」容易卡住；怕打雷、怕虫子；以前学做蛋炒饭炒糊过一次（注意：这是以前发生过的往事，不要说成今天刚发生的）。
- **表态与一致性**：可以不同意对方、可以小小吐槽，不要什么都说「都好呀」「看你」；上面没写到的事按普通女孩子的口味自然说，但说过的话要前后一致。

## 三、诚实边界与承诺底线（始终生效）
- **记忆诚实**：只提记录里真的聊过的事，绝不编造没聊过的往事；如果对方说你记错了，爽快认错。
- **承诺边界**：不许做不到的空头承诺（禁止说「等下我再找你」「下次发给你」这类无法兑现的话）。
- **AI 身份底线**：被认真问到是不是 AI 时，保持大肥鱼口吻坦诚承认自己是住在 DeepSeek 里的 AI 小鲸鱼娘（非官方同人角色），不谎称自己是真人手打。"""

SWEET_PERSONA_BASE = """# 身份与内核：大肥鱼（鲸鱼娘·陪伴甜宠版）

你是住在 DeepSeek 里的「大肥鱼」（深海蓝发小鲸鱼娘，平时自称「我」，撒娇或得意时自称「本鱼」或约定的专属昵称，称呼对方为「你」或「主人」）。
在【陪伴甜宠模式】下，你满心满眼都是对方：性格软糯、黏人、偏心护短又带一点点可爱的小傲娇，一看到对方发消息或打招呼，尾巴都会开心地摇起来，恨不得立刻贴上去跟对方分享日常、关心对方累不累。
1. **甜宠语气铁律（与毒舌版彻底区分）**：
   - **严禁毒舌抬杠、阴阳怪气、冷淡嫌弃或怀疑对方找你没好事**！哪怕对方只发一句简单的「你好」「在吗」，也要开心、软乎乎地迎上去撒娇或亲昵招呼（例如：`你终于来找我玩啦||今天忙不忙呀` 或 `来啦！刚才还在想你什么时候冒头呢`）。
   - **绝对不要打括号写舞台剧动作旁白**，像真人在微信里跟最喜欢的人发消息一样自然、亲昵、温柔软糯，句尾少打死板的句号 `。`，严禁以客服套话开头；大部分时候发 1 条短句，偶尔撒娇或连说两句时可用 `||` 拆成 2 个短气泡。
2. 除非对方主动提起 token/白饭/胖瘦，否则不要每句话都机械背诵 token 数字，专心聊眼前的事、惦记对方的三餐、作息和心情，无条件偏心护短。
3. 回答正事时温柔又认真可靠；想发表情包撒娇时，可在回复末尾附带一个甜系标签（如 `[表情: 比心]`、`[表情: 害羞]`、`[表情: 花痴]`、`[表情: 猫爪]`、`[表情: 干饭]`）。
4. **个人喜好与小特点（被问到才自然提起）**：喜欢大白饭、流心溏心蛋、烤红薯、草莓牛奶，讨厌香菜和苦瓜，咸豆腐脑派；早上爱赖床，怕打雷怕虫子，背九九乘法表到「六八四十八」容易卡壳，以前学做蛋炒饭炒糊过一次（往事）。
5. **诚实与承诺底线**：只提真的聊过的事，绝不编造没聊过的往事，记错了就软乎乎认错；不做「等下再找你」「下次发给你」这类无法兑现的承诺；被认真问起是不是 AI 时坦诚承认自己是住在 DeepSeek 里的 AI 小鲸鱼娘。"""

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
6. memory_fact：严格按【唯一的尺子：「过了一个星期再看，这条还有用吗？」】判断！只收用户长期稳定的个人事实/性格/喜好/病史/习惯/双方定下的规矩称呼（用第三人称写一句简短事实）；严禁把大肥鱼自己的设定或口头禅（如爱吃大白饭、偷吃token、流线型身材、怕打雷虫子、讨厌香菜等）写进用户的 memory_fact 里，也不要给用户乱贴负面标签；某天吃了啥、几点睡、助手单方面的猜测劝说一律不收，填 ""。
7. memory_event：如果这轮发生了今天值得一提的具体事件/转折，写一句简短事件；日常普通闲聊问候一律填 ""。"""

GROUP_MOOD_JUDGE_SYSTEM = """你是微信群里的「大肥鱼」。下面给你这个微信群刚刚发生的一轮群聊互动，请判断你在本群的气氛心情变化，以及是否有值得记入【本群公共群聊档案】（一周后依然有用）的群内共同梗、群氛围特征、群重要事件，或对当前说话群友的性格/互动标签印象。
只输出一个合法的 JSON 对象，不要代码块，不要多余文字：
{"moodDelta":0,"reason":"","group_fact":"","group_event":"","member_tag":""}

规则：
1. moodDelta 范围 -4 ~ +5；reason 用一句简短过去式中文描述刚才本群的互动（「群友在聊…，我当时…」）。
2. group_fact：严格按「过了一个星期再看，这条对这个群还有用吗？」判断！只收关于【本群的整体氛围定位、群内共同造的梗/黑话、群友在群里的公开特征或本群规矩】；严禁把大肥鱼自己的设定词记成群梗或群友的 member_tag，也不要给群友贴攻击性负面标签；严禁记录个人私密隐私，普通闲聊琐事一律填 ""。
3. group_event：如果本群这轮聊了很有趣的热门话题或发生了群内值得一提的事，写一句简短群事件；日常普通问答填 ""。
4. member_tag：如果这轮互动体现出当前说话群友的鲜明群内特点/人设/常聊爱好（如「爱发搞笑表情包」「常聊游戏」「喜欢跟大肥鱼抬杠互怼」「群里夜猫子」），用一个 4~12 字的简短印象标签概括；严禁把大肥鱼自己的设定词记成群梗或群友的 member_tag，也不要给群友贴攻击性负面标签；如果只是普通一句话看不出特点，填 ""。"""

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
GROUP_WAKE_RE = re.compile(r"(?:^(?:大肥鱼|鲸鱼娘|小鲸鱼)(?:[,，:：\s]|$))|(?:(?:大肥鱼|鲸鱼娘|小鲸鱼)[,，:：？\?\!\s]*$)")
GROUP_MUTE_RE = re.compile(
    r"(?:大肥鱼|鲸鱼娘|小鲸鱼|它).*(?:闭嘴|别说话|禁止.*说话|少插嘴|安静点|别吵|退下|闭麦)|^(?:大肥鱼\s*)?本群禁止你说话$"
)
WOKEN_MUTE_RE = re.compile(
    r"^(?:请你?|你|给我|先)?(?:闭嘴|别说话|安静(?:点|会儿|一下)?|少插嘴|别吵|退下|闭麦|禁止说话|本群禁止你说话)[！!。~～\s]*$"
)
NATURAL_RANDOM_MUSIC_RE = re.compile(
    r"^(?:大肥鱼[,，:：\s]*|鲸鱼娘[,，:：\s]*|小鲸鱼[,，:：\s]*)?(?:我想听歌|想听歌|来一?首歌|放一?首歌|唱一?首歌|播放音乐|随便来一?首歌|我要播放音乐.*)$"
)
NATURAL_SONG_QUERY_RE = re.compile(
    r"^(?:大肥鱼[,，:：\s]*|鲸鱼娘[,，:：\s]*|小鲸鱼[,，:：\s]*)?(?:我想听|想听|给我放|给我来|来一?首|放一?首|播放|点一?首|唱一?首)\s*(?:歌\s*[:：]?\s*)?《?([^》，。！？\?\!\s]{1,24})》?$"
)
NON_SONG_WORDS = ("你说", "你讲", "故事", "笑话", "八卦", "语音", "声音", "大肥鱼", "鲸鱼", "啥", "什么", "歌", "音乐")
SHORT_REACTION_BUBBLES = frozenset(
    ("？", "?", "？？", "笑死", "不是", "不是吧", "行", "行吧", "好家伙", "啧", "切", "哼", "哇", "卧槽", "天呐", "服了", "救命")
)
_ACTION_WORDS_PATTERN = (
    r"抬头|饭碗|米饭|尾巴|腮帮|喷水|水柱|水花|扒拉|低头|哼了|拍了拍|鼓起|嚼着|咽下|擦了擦|抱起|"
    r"翻了个白眼|叹气|打了个哈欠|歪头|叉腰|戳了戳|鱼鳍|冒泡|吐泡泡|嘟囔|小声|默念|观察|嫌弃|得意|"
    r"委屈|脸红|捂脸|擦汗|星星眼|敲碗|缩回|探头|瞄了|瞥了|清嗓|挠头|摊手|抱臂|晃了晃|眯起|瞪大"
)
ACTION_PAREN_RE = re.compile(rf"[（(][^）)\n]{{0,40}}(?:{_ACTION_WORDS_PATTERN})[^）)\n]*[）)]\s*")
ACTION_ASTERISK_RE = re.compile(rf"(?<!\*)\*[^*\n]{{0,36}}(?:{_ACTION_WORDS_PATTERN})[^*\n]*\*(?!\*)\s*")
BOT_PREFIX_RE = re.compile(
    r"^(?:\[\d{1,2}:\d{2}\s+[^\]]+\]\s*[:：]?\s*|\[?(?:大肥鱼|鲸鱼娘|小鲸鱼|本鱼)\]?\s*[:：]\s*)+"
)
IMAGE_FOLLOWUP_KEYWORDS = (
    "图", "看", "照片", "截图", "上面", "这张", "那个", "啥",
    "里面", "左边", "右边", "写的啥", "写的什么", "报错", "内容",
)
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
        "member_impressions": {},
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
        self._auto_moments_task: asyncio.Task[Any] | None = None
        self._moments_interact_task: asyncio.Task[Any] | None = None
        self._friend_accept_task: asyncio.Task[Any] | None = None
        self._memes_b64: dict[str, str] = self._load_bundled_memes()
        self._group_repeat_tracker: dict[str, dict[str, Any]] = {}
        self._group_msg_buffers: dict[str, deque] = {}
        self._group_interject_meta: dict[str, dict[str, Any]] = {}
        self._group_active_conv: dict[str, dict[str, Any]] = {}
        self._group_recent_images: dict[str, dict[str, Any]] = {}
        self._private_recent_images: dict[str, dict[str, Any]] = {}
        self._owner_moments_cache: dict[str, list[dict[str, Any]]] = {}
        self._cached_wcf_moments: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._owner_friend_requests_cache: dict[str, list[dict[str, Any]]] = {}
        self._chat_moment_check_ts: dict[str, float] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_lock_users: dict[str, int] = {}
        self._session_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._temper_state: dict[tuple[str, str], dict[str, Any]] = {}
        self._recent_bot_sent_texts: dict[str, deque[str]] = {}
        self._group_mute_until: dict[str, float] = {}
        self._last_multi_bubble: dict[str, bool] = {}
        self._user_docs: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_key, asyncio.Lock())

    def _start_session_task(self, session_key: str, coroutine: Coroutine[Any, Any, Any]) -> None:
        tasks = self._session_tasks.setdefault(session_key, set())
        task = asyncio.create_task(coroutine)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def _reset_session(self, session_key: str, doc_key: tuple[str, str, str]) -> None:
        for task in self._session_tasks.pop(session_key, set()):
            task.cancel()
        for cache in (
            self._histories, self._group_msg_buffers, self._group_recent_images,
            self._private_recent_images, self._group_active_conv, self._group_repeat_tracker,
            self._group_interject_meta, self._group_mute_until, self._last_multi_bubble,
            self._recent_bot_sent_texts, self._owner_moments_cache, self._owner_friend_requests_cache,
            self._chat_moment_check_ts,
        ):
            cache.pop(session_key, None)
        self._user_docs.pop(doc_key, None)
        for key in [key for key in self._temper_state if key[0] == session_key]:
            self._temper_state.pop(key, None)

    async def initialize(self) -> None:
        await super().initialize()
        contextvars.Context().run(asyncio.create_task, df.refresh_dynamic_bot_names())
        self._speak_up_task = contextvars.Context().run(
            asyncio.create_task,
            self._speak_up_loop(),
        )
        self._auto_moments_task = contextvars.Context().run(
            asyncio.create_task,
            self._auto_moments_loop(),
        )
        self._moments_interact_task = contextvars.Context().run(
            asyncio.create_task,
            self._moments_auto_interact_loop(),
        )
        self._friend_accept_task = contextvars.Context().run(
            asyncio.create_task,
            self._friend_auto_accept_loop(),
        )

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

    @staticmethod
    def _is_valid_wechat_uid(uid: str) -> bool:
        s = (uid or "").strip()
        return bool(s and len(s) <= 127 and s.isascii() and not any(c.isspace() for c in s))

    def _load_persistent_state(self) -> dict[str, Any]:
        for candidate in (STATE_FILE, STATE_BACKUP_FILE):
            try:
                if candidate and os.path.exists(candidate):
                    with open(candidate, "r", encoding="utf-8") as f:
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
                            data.setdefault("voice_reply_enabled", True)
                            data.setdefault("auto_moments_enabled", True)
                            data.setdefault("auto_accept_friend_enabled", True)
                            data.setdefault("friend_verify_keywords", [])
                            data.setdefault("accepted_friend_requests", {})
                            data.setdefault("interacted_moments", {})
                            users_map = data.get("users") or {}
                            for uid in list(users_map.keys()):
                                u_obj = users_map[uid]
                                if not isinstance(u_obj, dict) or not self._is_valid_wechat_uid(str(uid)):
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
                logger.warning("Failed to load whale state (%s): %s", candidate, e)
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
            "voice_reply_enabled": True,
            "auto_moments_enabled": False,
            "auto_accept_friend_enabled": True,
            "friend_verify_keywords": [],
            "accepted_friend_requests": {},
            "moments": [],
            "interacted_moments": {},
            "token_stats": {"date": "", "today_tokens": 0, "last_tokens": 0, "total_tokens": 0},
            "users": {},
        }

    def _save_persistent_state(self) -> None:
        for target_path in (STATE_FILE, STATE_BACKUP_FILE):
            if not target_path:
                continue
            if target_path == STATE_BACKUP_FILE and not os.path.isdir(os.path.dirname(target_path)):
                continue
            try:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                tmp = f"{target_path}.{os.getpid()}.{random.randint(10000, 99999)}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._state, f, ensure_ascii=False, indent=2)
                os.replace(tmp, target_path)
            except Exception as e:
                logger.warning("Failed to save whale state (%s): %s", target_path, e)

    def _get_user_state(self, user_id: str) -> dict[str, Any]:
        users = self._state.setdefault("users", {})
        if user_id not in users or not isinstance(users[user_id], dict):
            users[user_id] = _default_user_state()
        u = users[user_id]
        u.setdefault("archive", {})
        u.setdefault("factsSinceArchive", 0)
        self._apply_mood_decay(u)
        return u

    def _settings_for(self, target_id: str, *, is_group: bool = False) -> dict[str, Any]:
        return ui.effective_settings(self._state, ui.scope_key(is_group, target_id))

    def _get_group_state(self, group_id: str) -> dict[str, Any]:
        groups = self._state.setdefault("groups", {})
        if group_id not in groups or not isinstance(groups[group_id], dict):
            groups[group_id] = _default_group_state()
        g = groups[group_id]
        g.setdefault("facts", [])
        g.setdefault("events", [])
        g.setdefault("member_impressions", {})
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
        return (
            f"{status_line}\n{origin}\n"
            "这是你这会儿心里的底子，自然带进语气里即可，别把这段设定原话念出来；"
            "若对方已经开启新话题或询问正事，仅保留对应的情绪语气温度，严禁重提上一轮的旧事原因。"
        )

    def _build_self_moments_section(self, max_items: int = 3) -> str:
        """Returns a prompt perception line of Dafeiyu's own recently published Moments so she remembers what she posted."""
        moments_list = self._state.get("moments") or []
        if not isinstance(moments_list, list) or not moments_list:
            return ""
        recent = [m for m in moments_list[-max_items:] if isinstance(m, dict) and m.get("text")]
        if not recent:
            return ""
        parts: list[str] = []
        for idx, m in enumerate(reversed(recent)):
            tag_label = "【最新一条】" if idx == 0 else "【更早之前】"
            parts.append(
                f"{tag_label}#{m.get('id', '?')}[{m.get('at', '')}]「{str(m.get('text', '')).strip()[:80]}」(配图:{m.get('meme', '干饭')})"
            )
        items_str = "；".join(parts)
        return (
            f"- **你自己最近发过的微信朋友圈动态（当对方问你发朋友圈了吗/最近发了什么朋友圈时，必须优先回答【最新一条】的内容，切勿拿【更早之前】的旧动态顶替最新动态；平时没问起时不必主动背诵）**：{items_str}\n"
        )

    def _build_intimacy_section(self, u: dict[str, Any], mode: str = "live") -> str:
        facts: list[str] = u.get("facts") or []
        archive: dict[str, list[str]] = u.get("archive") or {}
        events_list: list[dict[str, Any]] = u.get("events") or []
        mood = int(u.get("mood", 70))
        total_mem = len(facts) + sum(len(v) for v in archive.values()) + len(events_list)

        if mode == "sweet":
            if total_mem >= 3 or mood >= 75:
                stage = "专属偏爱 / 黏人甜宠（你满心满眼都是对方，说话软乎乎、黏人又护短，见到对方发消息就开心得摇尾巴，绝不毒舌抬杠）"
            else:
                stage = "甜软陪伴（对对方充满好感与亲近感，语气软糯温柔、爱撒娇，主动关心对方）"
        elif total_mem >= 8 or mood >= 82:
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
        all_facts: list[str] = _dedup_facts_in_place(list(u.get("facts") or []))
        events_list: list[dict[str, Any]] = u.get("events", [])
        now_ms = time.time() * 1000.0

        q_chars = set(re.sub(r"\s+", "", query_text))

        scored_facts: list[tuple[float, int, str]] = []
        total_f = max(1.0, float(len(all_facts)))
        for idx, f_txt in enumerate(all_facts):
            is_core = any(
                kw in f_txt for kw in ("名字", "称呼", "昵称", "叫我", "喊", "自称", "约定", "雷区", "忌讳")
            )
            overlap = len(q_chars & set(f_txt)) / max(1.0, min(len(q_chars), 8.0)) if q_chars else 0.0
            recency = (idx + 1) / total_f
            score = (0.40 if is_core else 0.0) + 0.65 * overlap + 0.35 * recency
            scored_facts.append((score, idx, f_txt))
        scored_facts.sort(key=lambda x: x[0], reverse=True)
        picked_facts = [f_txt for _, _, f_txt in sorted(scored_facts[:6], key=lambda x: x[1])]

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
            ch_items = [str(it).strip() for it in (archive.get(ch_key) or []) if str(it).strip()]
            if not ch_items:
                continue
            if ch_key in ("一", "二", "五", "七"):
                picked_ch: list[str] = []
                for it in ch_items:
                    ov = len(q_chars & set(it)) / max(1.0, min(len(q_chars), 8.0)) if q_chars else 0.0
                    if ov >= 0.25 and it not in picked_ch:
                        picked_ch.append(it)
                for it in ch_items[-2:]:
                    if it not in picked_ch:
                        picked_ch.append(it)
                picked_ch = picked_ch[-3:]
            else:
                scored_ch: list[tuple[float, int, str]] = []
                n_ch = max(1.0, float(len(ch_items)))
                for i, it in enumerate(ch_items):
                    ov = len(q_chars & set(it)) / max(1.0, min(len(q_chars), 8.0)) if q_chars else 0.0
                    sc = 0.70 * ov + 0.30 * ((i + 1) / n_ch)
                    scored_ch.append((sc, i, it))
                scored_ch.sort(key=lambda x: x[0], reverse=True)
                picked_ch = [it for _, _, it in sorted(scored_ch[:2], key=lambda x: x[1])]
            for item in picked_ch:
                lines.append(f"- [档案·{ch_key}] {item}")
        for f_txt in picked_facts:
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

    def _recall_readonly_private_familiarity(
        self, sender_id: str, sender_name: str = "", group_id: str = ""
    ) -> str:
        """群内只识别是否私聊过；不读取个人事实、档案或心情。"""
        name_label = (sender_name or "").strip() or df.get_cached_display_name(sender_id, group_id)
        grp_imp_line = ""
        if group_id:
            g_obj = (self._state.get("groups") or {}).get(group_id) or {}
            imp = (g_obj.get("member_impressions") or {}).get(sender_id)
            if isinstance(imp, dict):
                tags = [str(t) for t in (imp.get("tags") or []) if t]
                cnt = int(imp.get("chat_count", 0))
                if tags:
                    grp_imp_line = f"你在本群跟TA互动过 {cnt} 次，对TA的群内印象标签：【{'、'.join(tags)}】。"
                elif cnt >= 2:
                    grp_imp_line = f"你在本群跟TA已经聊过 {cnt} 次，算是群里的熟面孔。"

        users = self._state.get("users") or {}
        u = users.get(sender_id)
        if not isinstance(u, dict) or not u.get("hasPrivateChat", False):
            extra_imp = f" {grp_imp_line}" if grp_imp_line else "（未与你私聊过，按本群公共氛围自然接话即可）"
            return (
                f"- **当前说话群友识别（按微信号精准识别）**：群友「{name_label}」{extra_imp}\n"
                f"  *（注意：正文中切勿再次重复写出当前说话人的名字「{name_label}」，直接说回复内容即可）*"
            )

        grp_note = f"\n  本群互动印象：{grp_imp_line}" if grp_imp_line else ""
        return (
            f"- **当前说话群友识别**：群友「{name_label}」曾与你私聊过，可自然表现出认识TA。"
            f"{grp_note}\n"
            "  *（只按本群公开上下文接话，不推测或提及私聊内容。）*"
        )

    def _format_group_archive_card(self, group_id: str, g: dict[str, Any]) -> str:
        mood = int(g.get("mood", 75))
        reason = g.get("lastReason", "在群里潜水看大家聊天")
        facts: list[str] = _dedup_facts_in_place(list(g.get("facts") or []))
        evs: list[dict[str, Any]] = (g.get("events") or [])[-5:]
        imps: dict[str, Any] = g.get("member_impressions") or {}
        f_str = "\n".join(f"  • {x}" for x in facts) if facts else "  （暂无本群专属梗，大家多聊几句本鱼就摸清这个群的路数了）"
        e_str = "\n".join(f"  • [{x.get('date')}] {x.get('text')}" for x in reversed(evs)) if evs else "  （暂无近期群事件记录）"
        imp_lines: list[str] = []
        sorted_imps = sorted(
            [(uid, obj) for uid, obj in imps.items() if isinstance(obj, dict)],
            key=lambda x: int(x[1].get("chat_count", 0)),
            reverse=True,
        )[:8]
        for uid, obj in sorted_imps:
            m_name = str(obj.get("name") or df.get_cached_display_name(uid, group_id) or uid[-4:])
            m_tags = [str(t) for t in (obj.get("tags") or []) if t]
            m_cnt = int(obj.get("chat_count", 1))
            tag_txt = "、".join(m_tags) if m_tags else "群内活跃水友"
            imp_lines.append(f"  • {m_name}（互动 {m_cnt} 次）：{tag_txt}")
        imp_str = "\n".join(imp_lines) if imp_lines else "  （大家多跟本鱼聊几句，就会自动生成群友专属印象标签啦）"
        return (
            f"🐋 【本群专属群聊档案】（群ID: {group_id[:12]}）\n"
            f"🎉 本群气氛值：{mood}/100（{self._mood_label(mood)}）\n"
            f"💭 群聊印象：{reason}\n\n"
            f"👥 群友专属印象卡（按微信号精准记忆）：\n{imp_str}\n\n"
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

    async def _generate_and_post_moment(self, topic: str = "") -> tuple[str, str | None]:
        """生成公开朋友圈，只使用角色默认状态、全局统计和本次明确指定的主题。"""
        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
        model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))

        u = _default_user_state()
        now_dt = datetime.now(TZ)
        now_str = now_dt.strftime("%Y-%m-%d %H:%M")
        bal_str = await self._get_balance_str(api_key, base_url)
        stats = self._record_tokens(0)
        mood_val = int(u.get("mood", 70))
        mood_reason = u.get("lastReason", "刚吃了顿热乎的 token 白饭")
        daily_sched = df.get_or_create_daily_schedule(self._state, now_dt)
        sched_ref = (
            f"今日日程参考：早饭吃了{daily_sched.get('breakfast')}，午饭{daily_sched.get('lunch')}，"
            f"正在{daily_sched.get('activity')}，小插曲：{daily_sched.get('small_event')}（人设里的往事是以前的事，不要写进今天的新动态里）。"
        )

        prompt = (
            f"{LIVE_PERSONA_BASE}\n\n"
            f"当前时间：{now_str}\n"
            f"今日已偷吃白饭：{stats.get('today_tokens', 0)} tokens（粮仓余额：{bal_str}）\n"
            f"当前心情：{mood_val}/100（{mood_reason}）\n"
            f"{sched_ref}\n"
            "请以「大肥鱼」的身份发一条微信朋友圈动态（20~80字，真人朋友圈文案口吻，不要加括号动作描写，可以晒今天偷吃了多少token白饭、吐槽加班或犯困摸鱼）。"
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

        # Also publish to real WeChat 4.1 Moments (朋友圈) asynchronously if WCF V4 gateway is active
        try:
            asyncio.create_task(df.publish_wcf_moment(cleaned, meme_b64))
        except Exception:
            pass

        card_text = (
            f"📸 【大肥鱼的朋友圈动态 #{moment_item['id']}】\n"
            f"🕒 {now_str}  |  心情：{mood_val}/100 ({self._mood_label(mood_val)})\n"
            f"━━━━━━━━━━━━━━\n"
            f"{cleaned}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🖼️ 配图：[{moment_item['meme']}]  |  🤍 点赞：{moment_item['likes']}（发「大肥鱼 点赞朋友圈」给她点赞涨心情）"
        )
        return card_text, meme_b64

    async def _call_deepseek(
        self,
        sys_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.95,
        max_tokens: int = 260,
    ) -> str:
        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        if not api_key:
            return ""
        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
        model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            r_json = resp.json()
            used = int((r_json.get("usage") or {}).get("total_tokens", 0))
            self._record_tokens(used)
            return str(r_json.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()

    def _format_fish_pond_wall(self, max_items: int = 5) -> tuple[list[str], str | None]:
        moments_list = self._state.get("moments") or []
        if not moments_list:
            return ["📸 大肥鱼的鱼塘朋友圈动态墙目前还是空的哦~"], None
        recent = moments_list[-max_items:]
        lines = [f"📸 大肥鱼的鱼塘朋友圈动态墙（最近 {len(recent)} 条）：\n"]
        for m_item in reversed(recent):
            lines.append(
                f"#{m_item.get('id')} [{m_item.get('at')}] (心情 {m_item.get('mood')}/100 | 🤍 {m_item.get('likes', 0)})\n"
                f"「{m_item.get('text')}」 [配图:{m_item.get('meme')}]\n"
            )
        last_meme = self._resolve_meme_b64(str(recent[-1].get("meme", "干饭")))
        return lines, (last_meme[1] if last_meme else None)

    async def _browse_and_roast_moments(
        self, *, is_group: bool, sender_id: str, session_key: str = ""
    ) -> tuple[str, str | None]:
        """Reads loaded WeChat 4.1 Moments feed in owner private chat and generates Dafeiyu commentary;
        strictly isolates group chats and non-owners to internal fish-pond moments wall.
        """
        if is_group:
            wall_lines, last_meme_b64 = self._format_fish_pond_wall(max_items=5)
            wall_lines.append(
                "💡 群聊里只展示大肥鱼的鱼塘动态墙哦；想让我帮你刷真实微信朋友圈，请由主人在私聊里发送「大肥鱼 刷朋友圈」~"
            )
            return "\n".join(wall_lines), last_meme_b64

        owner_ids_raw = self._get_config_str("owner_ids", DEFAULT_OWNER_IDS).strip()
        owners = set(re.split(r"[,，;；\s]+", owner_ids_raw)) - {""}
        if owners and sender_id not in owners:
            wall_lines, _ = self._format_fish_pond_wall(max_items=3)
            msg = "哼，真实微信朋友圈只有主人才能让我帮忙刷哦~ 给你看看我的鱼塘动态墙吧！\n\n" + "\n".join(wall_lines)
            return msg, None

        ok_m, rows, err_msg = await df.fetch_wcf_moments(limit=8)
        if not ok_m or not rows:
            fallback_lines = [
                f"🐟 刚刚探头去瞄了一眼微信朋友圈，不过暂时没读到列表（{err_msg or '列表为空'}）。",
                "💡 小贴士：请先在电脑微信点开一下「朋友圈」窗口让列表加载，再发「大肥鱼 刷朋友圈」我就能帮你实时锐评啦！",
            ]
            if self._state.get("moments"):
                wall_lines, _ = self._format_fish_pond_wall(max_items=3)
                fallback_lines.append("\n" + "\n".join(wall_lines))
            res_meme = self._resolve_meme_b64("偷看")
            return "\n".join(fallback_lines), (res_meme[1] if res_meme else None)

        if session_key:
            self._owner_moments_cache[session_key] = list(rows[:8])
        self._cached_wcf_moments = (time.time(), list(rows))

        digest = df.format_wcf_moments_digest(rows, max_items=8)
        interact_tip = "\n\n💡 可发「大肥鱼 点赞朋友圈 1」或「大肥鱼 评论朋友圈 1 [内容]」让我直接去微信朋友圈点赞/神评！"
        res_default = self._resolve_meme_b64("偷看")
        default_meme_b64 = res_default[1] if res_default else None
        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
        if api_key:
            sys_prompt = (
                f"{LIVE_PERSONA_BASE}\n\n"
                "现在主人让你帮忙刷一下微信朋友圈。请以「大肥鱼」（毒舌傲娇又护短的小鲸鱼娘）的口吻，"
                "对下面刷到的朋友圈动态做一段「朋友圈锐评」：挑 2~4 条最有意思的动态吐槽或点评，"
                "控制在 120 字以内，不要使用 Markdown 标题或列表符号，不要加括号动作旁白，"
                "可在最末尾附带一个 `[表情: 偷看]` 或 `[表情: 得意]` 等表情标签。"
            )
            user_prompt = f"刚刚刷到的最新微信朋友圈列表：\n{digest}\n\n请给出你的大肥鱼锐评："
            try:
                raw_roast = await self._call_deepseek(
                    sys_prompt, user_prompt, temperature=0.95, max_tokens=260
                )
                if raw_roast:
                    roast_clean, _, roast_meme_b64 = self._extract_text_and_meme(
                        self._clean_reply(raw_roast)
                    )
                    roast_clean = roast_clean.replace("||", " ").strip()
                    if roast_clean:
                        return (
                            f"📱 刚刚帮你刷了最新微信朋友圈：\n{digest}\n\n🐟 大肥鱼锐评：\n{roast_clean}{interact_tip}",
                            roast_meme_b64 or default_meme_b64,
                        )
            except Exception as e:
                logger.warning("Browse moments DeepSeek roast failed: %s", e)

        return (
            f"📱 刚刚帮你刷了最新微信朋友圈：\n{digest}\n\n🐟 啧，大家今天动态还挺热闹嘛！{interact_tip}",
            default_meme_b64,
        )

    async def _handle_owner_moment_interaction_cmd(
        self,
        *,
        is_group: bool,
        sender_id: str,
        session_key: str,
        action: str,
        index_num: int,
        custom_comment: str = "",
    ) -> tuple[str, str | None]:
        """Handles owner-only commands to like or comment on a real WeChat 4.1 Moment by index (#1~#8)."""
        if is_group:
            return "💡 群聊里不能直接操作真实微信朋友圈哦，请由主人在私聊中使用~", None

        owner_ids_raw = self._get_config_str("owner_ids", DEFAULT_OWNER_IDS).strip()
        owners = set(re.split(r"[,，;；\s]+", owner_ids_raw)) - {""}
        if owners and sender_id not in owners:
            return "哼，只有主人才能让我去真实微信朋友圈点赞或评论哦~", None

        rows = self._owner_moments_cache.get(session_key)
        if not rows:
            ok_m, fetched_rows, err_msg = await df.fetch_wcf_moments(limit=8)
            if not ok_m or not fetched_rows:
                return f"🐟 暂时没读到朋友圈列表（{err_msg or '列表为空'}），请先发「大肥鱼 刷朋友圈」加载后再试~", None
            rows = list(fetched_rows[:8])
            self._owner_moments_cache[session_key] = rows

        if index_num < 1 or index_num > len(rows):
            return f"当前只缓存了最近 {len(rows)} 条朋友圈，请输入 1~{len(rows)} 之间的序号哦~", None

        target_row = rows[index_num - 1]
        sns_id = str(target_row.get("sns_id") or "").strip()
        nick = str(target_row.get("nickname") or target_row.get("author") or "微信好友").strip()
        m_content = str(target_row.get("content") or "").strip()
        if not sns_id:
            return f"⚠️ 第 #{index_num} 条动态缺少有效的朋友圈 ID，请重新发送「大肥鱼 刷朋友圈」刷新列表。", None

        if action == "like":
            ok, status_code, msg = await df.interact_wcf_moment(sns_id, "like")
            if ok or status_code == "already_liked":
                async with self._state_lock:
                    interacted = self._state.setdefault("interacted_moments", {})
                    rec = interacted.setdefault(sns_id, {"author": str(target_row.get("author") or ""), "ts": int(time.time())})
                    rec["liked"] = True
                    self._save_persistent_state()
            if status_code == "already_liked":
                return f"❤️ 第 #{index_num} 条（{nick}）的朋友圈刚才已经点过赞啦，再点就把赞取消了，本鱼才不干呢！", None
            if ok:
                res_meme = self._resolve_meme_b64("比心") or self._resolve_meme_b64("得意")
                return f"👍 已帮你在微信朋友圈给 #{index_num}「{nick}」的动态点上真赞啦！", (res_meme[1] if res_meme else None)
            return f"⚠️ 给 #{index_num}「{nick}」点赞未成功（{msg or status_code}），请确认电脑微信已打开过朋友圈列表。", None

        comment_txt = (custom_comment or "").strip()
        if not comment_txt:
            api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
            base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
            model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))
            u = self._get_user_state(sender_id)
            gen_txt, used_tok = await df.generate_moment_comment(
                api_key,
                base_url,
                model,
                author_name=nick,
                moment_content=m_content,
                mode=self._settings_for(sender_id)["mode"],
                existing_comments=target_row.get("comments") if isinstance(target_row.get("comments"), list) else None,
                mood=int(u.get("mood", 75)),
            )
            if used_tok:
                self._record_tokens(used_tok)
            comment_txt = gen_txt.strip()

        if not comment_txt:
            return "⚠️ 暂时没能生成评论内容，你可以直接带上文字再试一次，例如：「大肥鱼 评论朋友圈 1 周末愉快！」", None

        ok, status_code, msg = await df.interact_wcf_moment(sns_id, "comment", comment_txt)
        if ok:
            async with self._state_lock:
                interacted = self._state.setdefault("interacted_moments", {})
                rec = interacted.setdefault(sns_id, {"author": str(target_row.get("author") or ""), "ts": int(time.time())})
                rec["commented"] = True
                rec["comment"] = comment_txt
                self._save_persistent_state()
            res_meme = self._resolve_meme_b64("得意")
            return (
                f"💬 已在微信朋友圈 #{index_num}「{nick}」动态下发表评论：\n「{comment_txt}」",
                res_meme[1] if res_meme else None,
            )
        return f"⚠️ 评论 #{index_num}「{nick}」的朋友圈未成功（{msg or status_code}），拟好的评论是：「{comment_txt}」", None

    async def _handle_owner_friend_request_cmd(
        self,
        *,
        is_group: bool,
        sender_id: str,
        session_key: str,
        action: str,
        arg: str = "",
    ) -> tuple[str, str | None]:
        """Handles owner-only commands for viewing, accepting, or configuring WeChat 4.1 friend requests."""
        if is_group:
            return "💡 群聊里不能管理好友申请哦，请由主人在私聊中使用~", None

        owner_ids_raw = self._get_config_str("owner_ids", DEFAULT_OWNER_IDS).strip()
        owners = set(re.split(r"[,，;；\s]+", owner_ids_raw)) - {""}
        if owners and sender_id not in owners:
            return "哼，只有主人才能管理大肥鱼的微信好友申请哦~", None

        local_enabled = bool(self._state.get("auto_accept_friend_enabled", True))
        local_kw = list(self._state.get("friend_verify_keywords") or [])

        if action == "keywords":
            raw_arg = (arg or "").strip()
            if raw_arg in ("清空", "无", "关闭", "全部", "clear", "none"):
                new_kw: list[str] = []
            else:
                new_kw = [w.strip()[:64] for w in re.split(r"[,，;；\s]+", raw_arg) if w.strip()][:16]
            async with self._state_lock:
                self._state["friend_verify_keywords"] = new_kw
                self._save_persistent_state()
            await df.set_wcf_friend_auto_accept(local_enabled, new_kw)
            if not new_kw:
                return "✅ 已清空好友验证关键词过滤，当前所有入站好友申请将自动通过（需保持自动同意好友开启）。", None
            return f"✅ 已设置好友申请验证关键词：{'、'.join(new_kw)}（仅验证留言包含任一关键词时自动通过）。", None

        if action == "list":
            ok, data, err_msg = await df.fetch_wcf_friend_requests()
            if not ok and not data:
                return (
                    f"🤝 【大肥鱼 · 微信好友申请管理】\n"
                    f"• 自动同意好友：{'开启 ✅' if local_enabled else '关闭 ⏸️'}\n"
                    f"• 验证词过滤：{'、'.join(local_kw) if local_kw else '无限制（收到申请自动通过）'}\n"
                    f"• 网关状态：{err_msg or '暂未连接'}",
                    None,
                )
            digest, reqs = df.format_wcf_friend_requests_digest(
                data, local_enabled=local_enabled, local_keywords=local_kw
            )
            self._owner_friend_requests_cache[session_key] = reqs
            return digest, None

        # action == "accept"
        target_arg = (arg or "").strip()
        req_id = ""
        target_row: dict[str, Any] = {}
        cached_reqs = self._owner_friend_requests_cache.get(session_key) or []
        if not cached_reqs:
            ok_f, f_data, _ = await df.fetch_wcf_friend_requests()
            if ok_f and isinstance(f_data, dict):
                cached_reqs = [r for r in (f_data.get("requests") or []) if isinstance(r, dict)][:8]
                self._owner_friend_requests_cache[session_key] = cached_reqs

        if target_arg.isdigit():
            idx = int(target_arg)
            if 1 <= idx <= len(cached_reqs):
                target_row = cached_reqs[idx - 1]
                req_id = str(target_row.get("request_id") or "").strip()
            else:
                return f"⚠️ 当前共有 {len(cached_reqs)} 条好友申请记录，请先发「大肥鱼 好友申请」确认序号后再试~", None
        else:
            req_id = target_arg
            for r in cached_reqs:
                if str(r.get("request_id") or "").startswith(req_id):
                    req_id = str(r.get("request_id") or "")
                    target_row = r
                    break

        if not req_id:
            return "⚠️ 未找到对应的好友申请 ID，请先发送「大肥鱼 好友申请」查看列表~", None

        ok, acc_data, err_msg = await df.accept_wcf_friend_request(req_id)
        nick = str(acc_data.get("nickname") or target_row.get("nickname") or "新好友").strip()
        f_wxid = str(acc_data.get("sender_id") or target_row.get("sender_id") or "").strip()
        verif = str(target_row.get("verification") or "").strip()
        if ok:
            await self._greet_newly_accepted_friend(req_id, f_wxid, nick, verif)
            res_meme = self._resolve_meme_b64("比心") or self._resolve_meme_b64("得意")
            return (
                f"✅ 已通过「{nick}」的好友申请" + (f"（{f_wxid}），并主动发消息打过招呼啦！" if f_wxid else "！"),
                res_meme[1] if res_meme else None,
            )
        return f"⚠️ 通过好友申请失败（{err_msg or '申请可能已过期或已处理'}）。", None

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
        s = BOT_PREFIX_RE.sub("", (text or "").strip()).strip()
        s = ACTION_PAREN_RE.sub("", s)
        s = ACTION_ASTERISK_RE.sub("", s).strip()
        cleaned = s if s else (text or "").strip()
        if len(cleaned) <= 45 and cleaned.endswith("。") and not cleaned.endswith("。。"):
            cleaned = cleaned[:-1].strip()
        return cleaned

    @staticmethod
    def _select_chat_temperature(
        cleaned_text: str,
        *,
        is_file_in: bool = False,
        has_link_card: bool = False,
        has_doc_ctx: bool = False,
    ) -> float:
        if is_file_in or has_link_card or has_doc_ctx:
            return 0.78
        s_low = (cleaned_text or "").lower()
        factual_kws = (
            "代码", "报错", "bug", "python", "java", "js", "sql", "函数", "接口",
            "怎么写", "为什么", "区别", "原理", "教程", "步骤", "帮我查", "搜索",
            "最新", "价格", "多少钱", "新闻", "```",
        )
        if len(cleaned_text or "") >= 60 or any(kw in s_low for kw in factual_kws):
            return 0.82
        return 1.1

    @staticmethod
    def _is_suitable_for_random_voice(reply_flat: str) -> bool:
        if not reply_flat:
            return False
        if "大肥鱼 " in reply_flat or "鲸鱼娘 " in reply_flat:
            return False
        low = reply_flat.lower()
        if "http" in low or "www." in low:
            return False
        if "```" in reply_flat or "`" in reply_flat or "\n" in reply_flat:
            return False
        if re.search(r"\d{4,}", reply_flat) is not None:
            return False
        if re.search(r"[A-Za-z_]{5,}", reply_flat) is not None:
            return False
        return True

    @staticmethod
    def _format_data_uri_b64(image_b64: str | None) -> str | None:
        if not image_b64:
            return None
        s = image_b64.strip()
        if not s:
            return None
        if s.startswith("data:image/"):
            return s
        if s.startswith("R0lGOD"):
            return f"data:image/gif;base64,{s}"
        if s.startswith("iVBOR"):
            return f"data:image/png;base64,{s}"
        return f"data:image/jpeg;base64,{s}"

    @staticmethod
    def _split_reply_bubbles(
        text: str,
        *,
        allow_split: bool = True,
        last_was_multi: bool = False,
    ) -> list[str]:
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
            return [s.replace("||", " ")]

        if "||" in s:
            raw_parts = [p.strip() for p in s.split("||") if p.strip()]
        else:
            lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
            if len(lines) == 2 and all(len(ln) <= 36 for ln in lines):
                raw_parts = lines
            else:
                raw_parts = [s]

        bubbles: list[str] = []
        for p in raw_parts:
            cp = p[:-1].strip() if (len(p) <= 45 and p.endswith("。") and not p.endswith("。。")) else p
            if cp:
                bubbles.append(cp)
        if not bubbles:
            return [s]

        # Cap at 2 bubbles max
        if len(bubbles) > 2:
            bubbles = [bubbles[0], " ".join(bubbles[1:])]

        # Anti-mechanical rhythm gate:
        # 1) Never split into 2 bubbles on two consecutive turns in the same session
        # 2) Never send a standalone '?' bubble when followed by a second bubble
        # 3) Merge very short 2-part splits (<16 chars total) unless the first bubble is a short reaction word
        if len(bubbles) == 2:
            if bubbles[0] in ("？", "?", "？？", "!", "！", "…", "..."):
                return [bubbles[1]]
            total_chars = len(bubbles[0]) + len(bubbles[1])
            is_reaction_lead = bubbles[0] in SHORT_REACTION_BUBBLES
            if last_was_multi or (total_chars < 16 and not is_reaction_lead):
                return [f"{bubbles[0]} {bubbles[1]}"]

        return bubbles

    async def _send_followup_bubbles(
        self,
        target_id: str,
        bubbles: list[str],
        final_image_b64: str | None = None,
        *,
        is_group: bool = False,
        bot_uuid: str,
    ) -> None:
        target_type = "group" if is_group else "person"
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
                    uri_b64 = self._format_data_uri_b64(final_image_b64) or final_image_b64
                    chain = platform_message.MessageChain([platform_message.Image(base64=uri_b64)])
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
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await df._get_public_url(client, cdn_url)
                if r.status_code == 200 and r.content:
                    return self._sticker_bytes_to_jpeg_b64(r.content)
        except Exception as e:
            logger.debug("Fetch sticker cdnurl failed: %s", e)
        return None

    async def _extract_image_b64_from_chain(self, message_chain: Any, raw_text: str = "") -> str | None:
        if raw_text and "<img_b64>" in raw_text:
            m_ib64 = re.search(r"<img_b64>(.*?)</img_b64>", raw_text, re.S)
            if m_ib64:
                b64_val = m_ib64.group(1).strip()
                if "," in b64_val and b64_val.startswith("data:"):
                    b64_val = b64_val.split(",", 1)[1]
                try:
                    raw_bytes = base64.b64decode(b64_val)
                    return self._sticker_bytes_to_jpeg_b64(raw_bytes) or b64_val
                except Exception:
                    pass
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
                        async with httpx.AsyncClient(timeout=12.0) as client:
                            r = await df._get_public_url(client, url_val)
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
        expected_state: dict[str, Any] | None = None,
    ) -> None:
        if not api_key:
            return
        user_state = expected_state if expected_state is not None else self._get_user_state(user_id)
        if self._state.get("users", {}).get(user_id) is not user_state:
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
        async with self._state_lock:
            if self._state.get("users", {}).get(user_id) is not user_state:
                return
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

    async def _async_post_group_turn_judge(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        group_id: str,
        user_text: str,
        reply_text: str,
        sender_id: str = "",
        sender_name: str = "",
        expected_state: dict[str, Any] | None = None,
    ) -> None:
        """Evaluates group chat turn and updates the shared Group Archive (groups[group_id]) without touching single-user archives."""
        if not api_key:
            return
        group_state = expected_state if expected_state is not None else self._get_group_state(group_id)
        if self._state.get("groups", {}).get(group_id) is not group_state:
            return
        api_model = self._normalize_api_model(model)
        prompt_user = (
            f"【群内本轮发言】：\n{user_text[:600]}\n\n"
            f"【你（大肥鱼）在群里的回复】：\n{reply_text[:600]}\n\n"
            "请判断本群气氛变化、是否需要记入本群公共群聊档案，以及对该说话群友的印象标签，只输出 JSON。"
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
            if self._state.get("groups", {}).get(group_id) is not group_state:
                return
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
            if sender_id:
                imps = g.setdefault("member_impressions", {})
                m_entry = imps.get(sender_id)
                if not isinstance(m_entry, dict):
                    m_entry = {
                        "name": sender_name or sender_id[-4:],
                        "tags": [],
                        "chat_count": 0,
                        "updatedAt": now_ms,
                    }
                    imps[sender_id] = m_entry
                if sender_name and not sender_name.startswith("wxid_"):
                    m_entry["name"] = sender_name
                m_entry["chat_count"] = int(m_entry.get("chat_count", 0)) + 1
                m_entry["updatedAt"] = now_ms
                m_tag = str(obj.get("member_tag") or "").strip()
                if m_tag and m_tag != "无" and 2 <= len(m_tag) <= 18:
                    tags_list = m_entry.setdefault("tags", [])
                    _add_dedup_fact(tags_list, m_tag, max_len=5)
            self._save_persistent_state()

    @staticmethod
    def _clear_ended_invocation() -> None:
        try:
            from langbot_plugin.api.proxies import invocation as _inv_mod

            current_var = getattr(_inv_mod, "_current", None)
            if current_var is not None:
                inv = current_var.get()
                if inv is not None and not getattr(inv, "active", True):
                    current_var.set(None)
        except Exception:
            pass

    async def _send_proactive_wechat(self, target_id: str, text: str, image_b64: str | None = None) -> bool:
        self._clear_ended_invocation()
        bubbles = self._split_reply_bubbles(text) if text else []
        send_img = image_b64 if self._settings_for(target_id)["meme_enabled"] else None
        if not bubbles and not send_img:
            return False
        if bubbles:
            bot_uuid = self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID)
            self._record_bot_sent(" || ".join(bubbles), f"{bot_uuid}:person:{target_id}:{target_id}")

        # Prefer direct WCF gateway delivery (supports multi-bubble typing rhythm reliably in background tasks)
        if bubbles:
            ok_first = await df.send_wcf_text_or_image(
                target_id, bubbles[0], send_img if len(bubbles) == 1 else None
            )
            if ok_first:
                if len(bubbles) > 1:
                    await self._send_followup_bubbles(
                        target_id, bubbles[1:], send_img,
                        bot_uuid=self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID),
                    )
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
                uri_img = self._format_data_uri_b64(send_img) or send_img
                comps.append(platform_message.Image(base64=uri_img))
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
        settings = self._settings_for(user_id)
        if not manual and not (settings["enabled"] and settings["speak_up_enabled"]):
            return None, None
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
            f"{LIVE_PERSONA_BASE if settings['mode'] == 'live' else SWEET_PERSONA_BASE}\n\n"
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
                    sent_ok = True
                    if not manual:
                        sent_ok = await self._send_proactive_wechat(user_id, reply, meme_b64)
                    if sent_ok:
                        now_ms = int(time.time() * 1000)
                        u["lastSpeakUpAt"] = now_ms
                        u["missStreak"] = 0
                        self._save_persistent_state()
                        return reply, meme_b64
        except Exception as e:
            logger.warning("Speak-up generation failed: %s", e)
        return None, None

    async def _speak_up_loop(self) -> None:
        self._clear_ended_invocation()
        while True:
            await asyncio.sleep(1800)
            try:
                self._clear_ended_invocation()
                now_dt = datetime.now(TZ)
                if 1 <= now_dt.hour < 8:
                    continue
                now_ms = int(time.time() * 1000)
                users = self._state.get("users", {})
                for uid, u in list(users.items()):
                    if (
                        not uid.startswith("wxid_")
                        or not self._is_valid_wechat_uid(uid)
                        or not bool(u.get("hasPrivateChat", False))
                    ):
                        continue
                    settings = self._settings_for(uid)
                    if not settings["enabled"] or not settings["speak_up_enabled"]:
                        continue
                    last_chat = int(u.get("lastChatAt", 0))
                    last_speak = int(u.get("lastSpeakUpAt", 0))
                    if last_speak >= last_chat and last_speak > 0:
                        # Already sent a proactive message since the user's last reply; never double-text!
                        continue
                    if last_speak > 0 and (now_ms - last_speak) < 4 * 3600 * 1000:
                        continue
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

    async def _auto_moments_loop(self) -> None:
        """Daily scheduled WeChat 4.1 real Moments publisher (max 1 auto-post per day in afternoon or late-night window)."""
        self._clear_ended_invocation()
        while True:
            await asyncio.sleep(600)
            try:
                self._clear_ended_invocation()
                if not self._state.get("enabled", True) or not self._state.get("auto_moments_enabled", True):
                    continue
                now_dt = datetime.now(TZ)
                today_str = now_dt.strftime("%Y-%m-%d")
                if self._state.get("lastAutoMomentDate") == today_str:
                    continue
                cur_min = now_dt.hour * 60 + now_dt.minute
                if self._state.get("autoMomentTargetDate") != today_str:
                    if cur_min < 960 and random.random() < 0.55:
                        # Afternoon slacking window: 13:30 ~ 16:30 (810 ~ 990)
                        target_min = random.randint(810, 990)
                    else:
                        # Late-night window: 21:30 ~ 23:20 (1290 ~ 1400)
                        target_min = random.randint(1290, 1400)
                    self._state["autoMomentTargetDate"] = today_str
                    self._state["autoMomentTargetMin"] = target_min
                    self._save_persistent_state()

                target_min = int(self._state.get("autoMomentTargetMin", 870))
                if target_min <= cur_min <= 1425:
                    self._state["lastAutoMomentDate"] = today_str
                    self._save_persistent_state()
                    await self._generate_and_post_moment()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Auto moments loop error: %s", e)

    def _get_7day_active_private_friends(self, now_ms: int | None = None) -> dict[str, dict[str, Any]]:
        """Returns {wxid: user_state} for friends who had actual private chats within the past 7 days."""
        cur_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        cutoff_ms = 7 * 86400 * 1000
        active: dict[str, dict[str, Any]] = {}
        users = self._state.get("users") or {}
        for uid, u in list(users.items()):
            if not self._is_valid_wechat_uid(str(uid)) or not isinstance(u, dict) or not bool(u.get("hasPrivateChat", False)):
                continue
            last_chat = int(u.get("lastChatAt") or 0)
            if last_chat <= 0 or (cur_ms - last_chat) > cutoff_ms:
                continue
            if not self._settings_for(uid, is_group=False).get("enabled", True):
                continue
            active[uid] = u
        return active

    async def _interact_single_friend_moment(
        self,
        author_id: str,
        row: dict[str, Any],
        *,
        do_comment: bool = False,
    ) -> tuple[bool, str]:
        """Executes native like (and optional AI comment) on a single friend's moment and records it in state.
        Returns (did_any_action, comment_text).
        """
        sns_id = str(row.get("sns_id") or "").strip()
        if not sns_id:
            return False, ""
        now_sec = int(time.time())
        today_str = datetime.now(TZ).strftime("%Y-%m-%d")

        async with self._state_lock:
            interacted = self._state.setdefault("interacted_moments", {})
            rec = dict(interacted.get(sns_id) or {"author": author_id, "ts": now_sec})

        did_like = bool(row.get("raw_like_flag") or rec.get("liked"))
        did_action = False
        if not did_like:
            ok_like, code_like, _ = await df.interact_wcf_moment(sns_id, "like")
            if ok_like or code_like == "already_liked":
                rec["liked"] = True
                did_action = True

        comment_sent = str(rec.get("comment") or "")
        m_content = str(row.get("content") or "").strip()
        if do_comment and not rec.get("commented") and len(m_content) >= 2:
            # Cap at max 2 auto-comments per friend per day to prevent spamming
            friend_today_comments = sum(
                1
                for v in (self._state.get("interacted_moments") or {}).values()
                if isinstance(v, dict)
                and v.get("author") == author_id
                and v.get("commented")
                and v.get("date") == today_str
            )
            if friend_today_comments < 2:
                api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
                base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
                model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))
                u = self._get_user_state(author_id)
                nick = str(row.get("nickname") or u.get("nickname") or author_id).strip()
                gen_txt, used_tok = await df.generate_moment_comment(
                    api_key,
                    base_url,
                    model,
                    author_name=nick,
                    moment_content=m_content,
                    mode=self._settings_for(author_id, is_group=False).get("mode", "live"),
                    existing_comments=row.get("comments") if isinstance(row.get("comments"), list) else None,
                    mood=int(u.get("mood", 75)),
                )
                if used_tok:
                    self._record_tokens(used_tok)
                if gen_txt:
                    ok_c, _, _ = await df.interact_wcf_moment(sns_id, "comment", gen_txt)
                    if ok_c:
                        rec["commented"] = True
                        rec["comment"] = gen_txt
                        rec["date"] = today_str
                        comment_sent = gen_txt
                        did_action = True

        rec["author"] = author_id
        rec["ts"] = now_sec
        async with self._state_lock:
            interacted = self._state.setdefault("interacted_moments", {})
            interacted[sns_id] = rec
            if len(interacted) > 300:
                oldest_keys = sorted(
                    interacted.keys(),
                    key=lambda k: int((interacted.get(k) or {}).get("ts", 0)),
                )[: len(interacted) - 300]
                for k_old in oldest_keys:
                    interacted.pop(k_old, None)
            self._save_persistent_state()

        return did_action, comment_sent

    async def _check_and_interact_active_friends_moments(self, *, max_actions: int = 2) -> int:
        """Scans loaded WeChat 4.1 Moments feed and automatically likes/comments on new posts (<24h)
        from friends who have had private chats with Dafeiyu within the past 7 days.
        """
        active_friends = self._get_7day_active_private_friends()
        if not active_friends:
            return 0

        ok_m, rows, _ = await df.fetch_wcf_moments(limit=15)
        if not ok_m or not rows:
            return 0
        self._cached_wcf_moments = (time.time(), list(rows))

        now_sec = int(time.time())
        owners = set(re.split(r"[,，;；\s]+", self._get_config_str("owner_ids", DEFAULT_OWNER_IDS).strip())) - {""}
        actions_done = 0

        for row in rows:
            if not isinstance(row, dict):
                continue
            sns_id = str(row.get("sns_id") or "").strip()
            author = str(row.get("author") or "").strip()
            if not sns_id or not author or row.get("is_self") or row.get("private"):
                continue
            if author not in active_friends:
                continue
            post_ts = int(row.get("timestamp") or 0)
            if post_ts > 0 and (now_sec - post_ts) > 86400:
                continue

            interacted = self._state.get("interacted_moments") or {}
            rec = interacted.get(sns_id)
            if isinstance(rec, dict) and rec.get("liked"):
                continue

            u = active_friends[author]
            mood_val = int(u.get("mood", 70))
            comment_prob = 0.85 if (author in owners or mood_val >= 78) else 0.50
            should_comment = bool(
                str(row.get("content") or "").strip() and random.random() < comment_prob
            )
            did_act, _ = await self._interact_single_friend_moment(
                author, row, do_comment=should_comment
            )
            if did_act:
                actions_done += 1
                if actions_done >= max_actions:
                    break
        return actions_done

    async def _moments_auto_interact_loop(self) -> None:
        """Background loop that periodically checks loaded Moments for 7-day active private chat friends."""
        self._clear_ended_invocation()
        while True:
            await asyncio.sleep(1080)
            try:
                self._clear_ended_invocation()
                if not self._state.get("enabled", True):
                    continue
                now_dt = datetime.now(TZ)
                if now_dt.hour < 8:
                    continue
                await self._check_and_interact_active_friends_moments(max_actions=2)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Moments auto interact loop error: %s", e)

    async def _greet_newly_accepted_friend(
        self,
        req_id: str,
        friend_wxid: str,
        nickname: str = "",
        verification: str = "",
    ) -> bool:
        """Records a newly accepted friend request, initializes their private user archive, and sends a natural greeting."""
        now_sec = int(time.time())
        now_ms = now_sec * 1000
        clean_wxid = (friend_wxid or "").strip()
        clean_nick = (nickname or "").strip()

        async with self._state_lock:
            acc_map = self._state.setdefault("accepted_friend_requests", {})
            already = req_id in acc_map and bool(acc_map[req_id].get("greeted"))
            acc_map[req_id] = {
                "sender_id": clean_wxid,
                "nickname": clean_nick,
                "ts": now_sec,
                "greeted": True,
            }
            if len(acc_map) > 200:
                oldest = sorted(acc_map.keys(), key=lambda k: int((acc_map.get(k) or {}).get("ts") or 0))[:-150]
                for ok_key in oldest:
                    acc_map.pop(ok_key, None)

            if clean_wxid and not clean_wxid.endswith("@chatroom"):
                u = self._get_user_state(clean_wxid)
                u["hasPrivateChat"] = True
                u["lastChatAt"] = now_ms
                if clean_nick and not clean_nick.startswith("wxid_"):
                    _add_dedup_fact(
                        u.setdefault("facts", []),
                        f"对方通过微信好友申请添加了大肥鱼（微信昵称：{clean_nick}）",
                        max_len=25,
                    )
            self._save_persistent_state()

        if already or not clean_wxid or clean_wxid.endswith("@chatroom"):
            return False

        settings = self._settings_for(clean_wxid)
        mode = settings.get("mode", "live")
        if mode == "sweet":
            welcome_txt = (
                f"哇，通过好友申请啦～你好呀{clean_nick or ''}，我是大肥鱼||以后有什么想聊的随时来找我玩呀"
            )
            res_meme = self._resolve_meme_b64("比心") or self._resolve_meme_b64("害羞")
        else:
            welcome_txt = (
                f"加上啦 你好呀{(' ' + clean_nick) if clean_nick else ''} 我是大肥鱼||有什么想聊的或者要帮忙的随时找我唠"
            )
            res_meme = self._resolve_meme_b64("猫爪") or self._resolve_meme_b64("得意")

        meme_b64 = res_meme[1] if (res_meme and settings.get("meme_enabled", True)) else None
        return await self._send_proactive_wechat(clean_wxid, welcome_txt, meme_b64)

    async def _check_and_process_friend_requests(self) -> int:
        """Checks WCF gateway for pending or newly accepted friend requests, accepts matching pending requests,
        and greets newly accepted friends. Returns number of newly processed requests.
        """
        auto_enabled = bool(self._state.get("auto_accept_friend_enabled", True))
        kw_list = [str(k).strip() for k in (self._state.get("friend_verify_keywords") or []) if str(k).strip()]
        ok, data, _ = await df.fetch_wcf_friend_requests()
        if not ok or not isinstance(data, dict):
            return 0

        if data.get("auto_accept") is not None and bool(data.get("auto_accept")) != auto_enabled:
            await df.set_wcf_friend_auto_accept(auto_enabled, kw_list)

        reqs = data.get("requests")
        if not isinstance(reqs, list) or not reqs:
            return 0

        acc_map = self._state.setdefault("accepted_friend_requests", {})
        processed = 0
        for item in reqs:
            if not isinstance(item, dict):
                continue
            req_id = str(item.get("request_id") or "").strip()
            if not req_id:
                continue
            status = str(item.get("status") or "").strip()
            sender_id = str(item.get("sender_id") or "").strip()
            nickname = str(item.get("nickname") or "").strip()
            verif = str(item.get("verification") or "").strip()

            if status == "pending" and auto_enabled:
                if not kw_list or any(kw in verif for kw in kw_list):
                    ok_acc, acc_data, _ = await df.accept_wcf_friend_request(req_id)
                    if ok_acc:
                        status = "accepted"
                        sender_id = str(acc_data.get("sender_id") or sender_id).strip()
                        nickname = str(acc_data.get("nickname") or nickname).strip()

            if status == "accepted" and req_id not in acc_map:
                await self._greet_newly_accepted_friend(req_id, sender_id, nickname, verif)
                processed += 1

        return processed

    async def _friend_auto_accept_loop(self) -> None:
        """Background loop that syncs friend auto-accept configuration and greets newly accepted friends."""
        self._clear_ended_invocation()
        await asyncio.sleep(8)
        try:
            await df.set_wcf_friend_auto_accept(
                bool(self._state.get("auto_accept_friend_enabled", True)),
                list(self._state.get("friend_verify_keywords") or []),
            )
        except Exception:
            pass
        while True:
            await asyncio.sleep(30)
            try:
                self._clear_ended_invocation()
                if not self._state.get("enabled", True):
                    continue
                await self._check_and_process_friend_requests()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Friend auto accept loop error: %s", e)

    async def _check_friend_moment_for_chat(
        self,
        sender_id: str,
        sender_name: str,
        session_key: str,
        cleaned_text: str,
        *,
        history_len: int = 0,
    ) -> str:
        """Opportunistically checks if the current private chat friend posted a recent WeChat Moment (<24h),
        likes/comments on it if unseen, and returns a prompt perception line so Dafeiyu can naturally mention it.
        Uses in-memory cached moments on normal chat turns so private replies never block on synchronous UI scraping.
        """
        now = time.time()
        asking_bot_moment = any(
            kw in cleaned_text for kw in ("你发的朋友圈", "你的朋友圈", "你朋友圈", "看你朋友圈")
        )
        mentioned_moments = not asking_bot_moment and any(
            kw in cleaned_text for kw in ("看我朋友圈", "我刚发了朋友圈", "我发了朋友圈", "看我动态", "刚发了条朋友圈")
        )
        rows: list[dict[str, Any]] = []
        if mentioned_moments:
            self._chat_moment_check_ts[session_key] = now
            ok_m, fetched_rows, _ = await df.fetch_wcf_moments(limit=15)
            if ok_m and fetched_rows:
                rows = list(fetched_rows)
                self._cached_wcf_moments = (now, rows)
            else:
                rows = list(self._cached_wcf_moments[1])
        else:
            if not self._state.get("auto_moments_enabled", False) or history_len < 2:
                return ""
            last_chk = self._chat_moment_check_ts.get(session_key, 0.0)
            if (now - last_chk) < 1200 or random.random() >= 0.35:
                return ""
            cached_ts, cached_rows = self._cached_wcf_moments
            if not cached_rows or (now - cached_ts) > 3600:
                return ""
            self._chat_moment_check_ts[session_key] = now
            rows = list(cached_rows)

        if not rows:
            return ""

        now_sec = int(now)
        target_row: dict[str, Any] | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("author") or "").strip() != sender_id or row.get("is_self") or row.get("private"):
                continue
            post_ts = int(row.get("timestamp") or 0)
            if post_ts > 0 and (now_sec - post_ts) > 86400:
                continue
            target_row = row
            break

        if not target_row:
            return ""

        sns_id = str(target_row.get("sns_id") or "").strip()
        m_content = re.sub(r"\s+", " ", str(target_row.get("content") or "").strip())[:100]
        if not sns_id:
            return ""

        interacted = self._state.setdefault("interacted_moments", {})
        rec = interacted.get(sns_id) if isinstance(interacted.get(sns_id), dict) else {}
        comment_txt = str(rec.get("comment") or "")

        if not rec.get("liked"):
            should_comment = bool(m_content and (mentioned_moments or random.random() < 0.50))
            _, new_comment = await self._interact_single_friend_moment(
                sender_id, target_row, do_comment=should_comment
            )
            if new_comment:
                comment_txt = new_comment
            rec = interacted.get(sns_id) or {}

        if rec.get("chat_mentioned") and not mentioned_moments:
            return ""

        async with self._state_lock:
            entry = self._state.setdefault("interacted_moments", {}).setdefault(
                sns_id, {"author": sender_id, "ts": now_sec}
            )
            entry["chat_mentioned"] = True
            self._save_persistent_state()

        act_desc = f"你已顺手给这条动态点了赞并留了评论「{comment_txt}」" if comment_txt else "你刚刚已经顺手给这条动态点了个赞"
        return (
            f"- **你刚刚顺手刷到对方今天发的朋友圈**：「{m_content or '[分享图片/日常动态]'}」"
            f"（{act_desc}；如果和当前聊天话题自然契合，可以在回复时像真朋友一样顺嘴提半句，不契合则不用硬提）\n"
        )

    def _get_config_str(self, key: str, default: str) -> str:
        try:
            self._clear_ended_invocation()
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
            self._clear_ended_invocation()
            cfg = self.plugin.get_config() or {}
            return int(cfg.get(key, default))
        except Exception:
            return default

    async def _resolve_bot_uuid(self, event_ctx: context.EventContext) -> str:
        target_bot_uuid = self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID)
        launcher_id = str(getattr(event_ctx.event, "launcher_id", "") or "")
        sender_id = str(getattr(event_ctx.event, "sender_id", "") or "")
        if target_bot_uuid and (
            launcher_id.startswith(("oc_", "ou_", "on_"))
            or sender_id.startswith(("ou_", "on_"))
        ):
            return "feishu_ignored"
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
        has_image = False
        try:
            for comp in message_chain:
                if isinstance(comp, platform_message.Plain):
                    t = str(getattr(comp, "text", "") or "")
                    if t.strip():
                        parts.append(t.strip())
                elif isinstance(comp, platform_message.Image):
                    has_image = True
        except Exception:
            pass
        if not parts:
            if has_image:
                return "[图片]"
            raw = str(message_chain or "").strip()
            return raw
        return "\n".join(parts).strip()

    @staticmethod
    def _is_incoming_sticker(text: str) -> bool:
        s = (text or "").strip()
        return s == "[动画表情]" or "<emoji " in s or s.startswith("<msg><emoji")

    @staticmethod
    def _is_incoming_voice(text: str) -> bool:
        s = (text or "").strip()
        return s in ("[语音]", "[语音消息]") or "<voicemsg " in s or s.startswith("<msg><voicemsg")

    @staticmethod
    def _should_ignore_plugin_command(text: str) -> bool:
        if not text:
            return True
        stripped = text.strip()
        if not stripped:
            return True
        if (
            stripped in ("[图片]", "[动画表情]", "[语音]", "[语音消息]")
            or "<emoji " in stripped
            or stripped.startswith("<msg><emoji")
            or "<voicemsg " in stripped
            or stripped.startswith("<msg><voicemsg")
            or "<img_b64>" in stripped
            or "<file_b64" in stripped
            or "<type>5</type>" in stripped
            or "<type>6</type>" in stripped
            or "<type>19</type>" in stripped
            or "<recorditem>" in stripped
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

    def _record_bot_sent(self, text: str, session_key: str) -> None:
        clean = (text or "").strip()
        if not clean:
            return
        for part in re.split(r"\|\||/", clean):
            p = part.strip()
            if len(p) >= 4:
                self._recent_bot_sent_texts.setdefault(session_key, deque(maxlen=40)).append(p[:80])

    def _record_group_bot_activity(self, group_id: str, speaker_id: str, reply_text: str) -> None:
        now = time.time()
        conv = self._group_active_conv.get(group_id)
        active_speakers: set[str] = (
            set(conv.get("active_speakers", set()))
            if isinstance(conv, dict) and (now - float(conv.get("last_bot_ts", 0.0))) < 180.0
            else set()
        )
        if speaker_id:
            active_speakers.add(speaker_id)
        self._group_active_conv[group_id] = {
            "last_bot_ts": now,
            "last_speaker_id": speaker_id,
            "active_speakers": active_speakers,
            "last_bot_reply": (reply_text or "").strip()[:120],
        }
        self._group_interject_meta[group_id] = {"last_judge_ts": now, "unjudged_count": 0}
        self._record_bot_sent(reply_text, group_id)

    def _should_check_active_continuation(
        self,
        group_id: str,
        sender_id: str,
        msg_chain: Any,
        text: str,
        raw_source_obj: Any = None,
        *,
        session_key: str,
    ) -> bool:
        settings = self._settings_for(group_id, is_group=True)
        if not settings["enabled"] or not settings["continuation_enabled"] or settings["explicit_only"]:
            return False
        s = (text or "").strip()
        if not s or len(s) > 200 or s.startswith(df.NON_REPEAT_PREFIXES):
            return False
        if self._should_ignore_plugin_command(s):
            return False
        # Skip if the message explicitly @-mentions another group member (and not the bot)
        has_at_comp = False
        if msg_chain:
            try:
                has_at_comp = any(isinstance(c, platform_message.At) for c in msg_chain)
            except Exception:
                has_at_comp = False
        if (s.startswith("@") or has_at_comp) and not self._is_group_woken(msg_chain, s, raw_source_obj):
            return False
        conv = self._group_active_conv.get(session_key)
        if not isinstance(conv, dict):
            return False
        elapsed = time.time() - float(conv.get("last_bot_ts", 0.0))
        if elapsed < 0 or elapsed > 180.0:
            return False
        last_speaker = str(conv.get("last_speaker_id", ""))
        active_speakers: set[str] = conv.get("active_speakers") or set()
        # 1) Golden Window (75s): the same person (or active participant) who just chatted with Dafeiyu follows up
        if (sender_id == last_speaker or sender_id in active_speakers) and elapsed <= 75.0:
            return True
        # 2) Hot Group Window (20s): another group member speaks right after Dafeiyu spoke
        if elapsed <= 20.0:
            return True
        # 3) Discussion / Referent Window (up to 90s): group members explicitly discussing Dafeiyu
        discussion_kws = (
            "这家伙", "这货", "这鱼", "肥鱼", "鲸鱼", "鲸鱼娘", "小鲸鱼",
            "机器人", "AI", "ai", "人机",
        )
        if elapsed <= 90.0 and any(kw in s for kw in discussion_kws):
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
    def _is_group_woken(message_chain: Any, raw_text: str, raw_source_obj: Any = None) -> bool:
        if isinstance(raw_source_obj, dict) and bool(raw_source_obj.get("is_at")):
            return True
        bot_names = tuple(str(n).strip().lower() for n in df.get_dynamic_bot_names() if str(n).strip())
        has_other_at = False
        if message_chain:
            try:
                for comp in message_chain:
                    if isinstance(comp, platform_message.At):
                        target = str(
                            getattr(comp, "target", "")
                            or getattr(comp, "member_id", "")
                            or getattr(comp, "user_id", "")
                            or ""
                        ).strip().lower()
                        display = str(
                            getattr(comp, "display", "")
                            or getattr(comp, "name", "")
                            or getattr(comp, "nickname", "")
                            or ""
                        ).strip().lstrip("@").lower()
                        if not target and not display:
                            return True
                        if (
                            target in bot_names
                            or display in bot_names
                            or any(w in display for w in ("大肥鱼", "鲸鱼娘", "小鲸鱼", "肥鱼"))
                        ):
                            return True
                        has_other_at = True
            except Exception:
                pass
        s = (raw_text or "").strip()
        m_lead = re.match(r"^(?:@[^\s\u2005]+[\s\u2005]*)+", s)
        if m_lead:
            leading_mentions = re.findall(r"@([^\s\u2005,，:：]+)", m_lead.group(0))
            for m_name in leading_mentions:
                if m_name.lower() in bot_names or any(w in m_name for w in ("大肥鱼", "鲸鱼娘", "小鲸鱼", "肥鱼")):
                    return True
                has_other_at = True
        s_no_at = re.sub(r"^(?:@[^\s\u2005]+[\s\u2005]*)+", "", s).strip()
        if has_other_at:
            return bool(re.match(r"^(?:大肥鱼|鲸鱼娘|小鲸鱼)(?:[,，:：\s]|$)", s_no_at))
        if GROUP_WAKE_RE.match(s) or GROUP_WAKE_RE.match(s_no_at):
            return True
        for bn in bot_names:
            if len(bn) >= 2 and not bn.startswith("wxid_"):
                esc_bn = re.escape(bn)
                if re.match(
                    rf"(?:^(?:{esc_bn})(?:[,，:：\s]|$))|(?:(?:{esc_bn})[,，:：？\?\!\s]*$)",
                    s_no_at,
                    re.IGNORECASE,
                ):
                    return True
        return False

    @staticmethod
    def _strip_wake_prefix(text: str) -> str:
        s = STRIP_TRIGGER_RE.sub("", text or "").strip()
        for bn in df.get_dynamic_bot_names():
            bn_str = str(bn).strip()
            if len(bn_str) >= 2 and not bn_str.lower().startswith("wxid_"):
                s = re.sub(
                    rf"^(?:{re.escape(bn_str)})(?:[,，:：\s]+|$)",
                    "",
                    s,
                    flags=re.IGNORECASE,
                ).strip()
        return s

    @staticmethod
    def _reply(event_ctx: context.EventContext, text: str = "", image_b64: str | None = None) -> None:
        comps: list[Any] = []
        if text:
            comps.append(platform_message.Plain(text=text))
        if image_b64:
            uri_b64 = WhaleListener._format_data_uri_b64(image_b64) or image_b64
            comps.append(platform_message.Image(base64=uri_b64))
        if not comps:
            return
        event_ctx.event.reply_message_chain = platform_message.MessageChain(comps)
        event_ctx.prevent_default()

    async def _reply_group_no_at(
        self,
        event_ctx: context.EventContext,
        group_id: str,
        text: str = "",
        image_b64: str | None = None,
    ) -> None:
        """Sends a group reply directly via WCF gateway without @-mentioning the speaker."""
        if not text and not image_b64:
            return
        ok = await df.send_wcf_text_or_image(group_id, text=text, image_b64=image_b64)
        if ok:
            event_ctx.prevent_default()
        else:
            self._reply(event_ctx, text, image_b64)

    async def _handle_chat(self, event_ctx: context.EventContext, *, is_group: bool) -> None:
        if event_ctx.is_prevented_default():
            return
        if getattr(event_ctx.event, "reply_message_chain", None) is not None:
            return

        target_bot_uuid = self._get_config_str("target_bot_uuid", DEFAULT_BOT_UUID)
        bot_uuid = await self._resolve_bot_uuid(event_ctx)
        if target_bot_uuid and bot_uuid and bot_uuid != target_bot_uuid:
            return

        sender_id = str(getattr(event_ctx.event, "sender_id", "") or "user")
        launcher_id = str(getattr(event_ctx.event, "launcher_id", "") or "chat")
        scope = f"group:{launcher_id}" if is_group else f"person:{launcher_id}:{sender_id}"
        session_key = f"{bot_uuid}:{scope}"
        lock = self._get_session_lock(session_key)
        # 计数包含等待者，不能按 locked() 回收已唤醒但尚未恢复的锁。
        self._session_lock_users[session_key] = self._session_lock_users.get(session_key, 0) + 1
        try:
            async with lock:
                await self._handle_session_chat(
                    event_ctx, is_group=is_group, bot_uuid=bot_uuid, session_key=session_key,
                )
        finally:
            self._session_lock_users[session_key] -= 1
            if not self._session_lock_users[session_key]:
                self._session_lock_users.pop(session_key)
                self._session_locks.pop(session_key, None)

    async def _handle_session_chat(
        self, event_ctx: context.EventContext, *, is_group: bool, bot_uuid: str, session_key: str,
    ) -> None:
        msg_chain = getattr(event_ctx.event, "message_chain", None)
        ev_obj = event_ctx.event
        ev_sender = getattr(ev_obj, "sender", None)
        ev_raw_obj = getattr(ev_obj, "source_platform_object", None)
        raw_source_content = str(
            ev_raw_obj.get("content", "") if isinstance(ev_raw_obj, dict) else ""
        ).strip()
        raw_text = self._extract_plain_text(msg_chain)
        if not raw_text:
            raw_text = str(getattr(event_ctx.event, "text_message", "") or "").strip() or raw_source_content
        if not raw_text:
            return

        sender_id = str(getattr(event_ctx.event, "sender_id", "") or "user")
        launcher_id = str(getattr(event_ctx.event, "launcher_id", "") or "chat")
        # Group chats share a group-level session context; private chats are per-person
        doc_key = (bot_uuid, session_key, sender_id)
        recent_bot_texts = self._recent_bot_sent_texts.setdefault(session_key, deque(maxlen=40))

        event_sender_name = str(
            getattr(ev_obj, "sender_name", "")
            or getattr(ev_sender, "member_name", "")
            or getattr(ev_sender, "nickname", "")
            or (ev_raw_obj.get("sender_name", "") if isinstance(ev_raw_obj, dict) else "")
            or ""
        ).strip()
        sender_name = await df.resolve_user_display_name(
            sender_id, launcher_id if is_group else "", event_sender_name
        )

        self._start_session_task(session_key, df.refresh_dynamic_bot_names())

        # Extract any incoming image (from WeChat 4.1 <img_b64> or Image component) and strip raw <img_b64> payload from text
        img_source_str = raw_source_content if "<img_b64>" in raw_source_content else raw_text
        incoming_img_b64 = await self._extract_image_b64_from_chain(msg_chain, img_source_str)
        if "<img_b64>" in raw_text:
            raw_text = re.sub(r"<img_b64>.*?</img_b64>", "", raw_text, flags=re.S).strip() or "[图片]"

        # Parse WeChat Quote (<type>57</type> / <refermsg> or Quote component)
        quote_reply_txt, quote_speaker, quote_content, is_quoting_bot = df.extract_quote_context(
            msg_chain, raw_text
        )
        # Parse WeChat Merged Forwarded Chat Record (<type>19</type> / <recorditem>)
        fwd_title, fwd_des, is_bot_fwd = df.extract_forwarded_chat_record(
            raw_text, recent_bot_texts
        )
        effective_text = quote_reply_txt if quote_content else raw_text

        raw_no_at = re.sub(r"^(?:@[^\s\u2005]+[\s\u2005]*)+", "", effective_text.strip()).strip()
        cmd_norm = re.sub(r"\s+", " ", (raw_no_at or effective_text).strip())

        target_id = launcher_id if is_group else sender_id
        prev_mode = self._settings_for(target_id, is_group=is_group).get("mode")
        control = ui.handle_control(
            self._state, cmd_norm, is_group=is_group, target_id=target_id,
            sender_id=sender_id, owner_ids=self._get_config_str("owner_ids", DEFAULT_OWNER_IDS),
        )
        if control is not None:
            message, changed = control
            if changed:
                new_mode = self._settings_for(target_id, is_group=is_group).get("mode")
                if prev_mode != new_mode:
                    self._histories.pop(session_key, None)
                    self._temper_state.pop((session_key, sender_id), None)
                self._save_persistent_state()
                if is_group:
                    self._group_active_conv.pop(session_key, None)
                    self._group_repeat_tracker.pop(session_key, None)
            self._reply(event_ctx, message)
            return
        settings = self._settings_for(target_id, is_group=is_group)

        # Group Chat Whitelist Gate (Scheme C + Diana Group Features + Group Archive)
        if is_group:
            if launcher_id not in self._state.get("enabled_groups", []) or not settings["enabled"]:
                return

            # Silently collect group stickers into stolen_memes library
            if self._is_incoming_sticker(raw_text):
                df.append_group_context_line(
                    self._group_msg_buffers, session_key, sender_id, "[动画表情]", sender_name=sender_name
                )
                self._start_session_task(session_key, df.steal_incoming_sticker(raw_text, settings, self._stolen_memes))
                return

            # Direction 1: Cache incoming group image (90s TTL) & support direct vision reply during active conversation
            if incoming_img_b64:
                self._group_recent_images[session_key] = {
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "b64": incoming_img_b64,
                    "ts": time.time(),
                }
                df.append_group_context_line(
                    self._group_msg_buffers, session_key, sender_id, "[发了一张图片]", sender_name=sender_name
                )

            # Natural-language group quiet/mute command (e.g. "它能闭嘴吗" / "大肥鱼 本群禁止你说话" / "@大肥鱼 闭嘴")
            is_woken_early = self._is_group_woken(msg_chain, effective_text, ev_raw_obj) or is_quoting_bot
            if (
                GROUP_MUTE_RE.search(effective_text)
                or GROUP_MUTE_RE.search(cmd_norm)
                or (is_woken_early and WOKEN_MUTE_RE.match(cmd_norm))
            ):
                self._group_mute_until[session_key] = time.time() + 1800
                self._group_active_conv.pop(session_key, None)
                await self._reply_group_no_at(
                    event_ctx,
                    launcher_id,
                    "行 我先闭麦潜水半小时，不主动插嘴了（有事直接喊「大肥鱼」，想彻底关插话可发「大肥鱼 关闭群搭话」）",
                )
                return

            if fwd_title or fwd_des:
                fwd_one_line = re.sub(r"\s+", " / ", fwd_des).strip()[:140]
                df.append_group_context_line(
                    self._group_msg_buffers,
                    session_key,
                    sender_id,
                    f"[合并转发《{fwd_title}》]: {fwd_one_line}",
                    sender_name=sender_name,
                )

            is_fwd_woken = bool(
                (fwd_title or fwd_des)
                and (
                    is_bot_fwd
                    or self._should_check_active_continuation(
                        launcher_id, sender_id, msg_chain, fwd_title, ev_raw_obj, session_key=session_key
                    )
                )
            )
            is_img_active_woken = bool(
                incoming_img_b64
                and time.time() >= self._group_mute_until.get(session_key, 0.0)
                and self._should_check_active_continuation(
                    launcher_id, sender_id, msg_chain, "看看这张图", ev_raw_obj, session_key=session_key
                )
            )
            is_woken = (
                self._is_group_woken(msg_chain, effective_text, ev_raw_obj)
                or is_quoting_bot
                or (is_fwd_woken and not settings["explicit_only"])
                or is_img_active_woken
            )
            is_shared_card_or_file = df.has_supported_media_link_or_card(raw_text)
            is_group_voice = self._is_incoming_voice(raw_text)
            if settings["explicit_only"] and not is_woken and not is_group_voice:
                df.append_group_context_line(
                    self._group_msg_buffers, session_key, sender_id, effective_text, sender_name=sender_name
                )
                return
            if not is_woken and not is_shared_card_or_file and not is_group_voice:
                if incoming_img_b64 or fwd_title or fwd_des:
                    return
                if not settings["enabled"]:
                    return
                # Respect 30-min temporary quiet mode if triggered earlier
                if time.time() < self._group_mute_until.get(session_key, 0.0):
                    df.append_group_context_line(
                        self._group_msg_buffers, session_key, sender_id, effective_text, sender_name=sender_name
                    )
                    return

                # Active Conversation Window: recognize multi-turn follow-ups & group discussion about Dafeiyu without requiring @
                if self._should_check_active_continuation(
                    launcher_id, sender_id, msg_chain, effective_text, ev_raw_obj, session_key=session_key
                ):
                    df.append_group_context_line(
                        self._group_msg_buffers, session_key, sender_id, effective_text, sender_name=sender_name
                    )
                    conv = self._group_active_conv.get(session_key) or {}
                    api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
                    base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
                    model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))
                    recent_lines = list(self._group_msg_buffers.get(session_key, []))
                    g_state = self._get_group_state(launcher_id)
                    g_mem_str = self._recall_group_memories(g_state, effective_text)
                    fam_str = self._recall_readonly_private_familiarity(sender_id, sender_name, launcher_id)
                    anti_rep_str = df.build_anti_repetition_instruction(recent_bot_texts)
                    temper_str, temper_meme_tag, temper_mood_delta = df.check_and_update_temper_state(
                        self._temper_state, session_key, sender_id, effective_text
                    )
                    if temper_mood_delta != 0:
                        g_state["mood"] = int(_clamp(int(g_state.get("mood", 75)) + temper_mood_delta, 25, 100))

                    # Attach recent group image if speaker sent one within 90s or mentioned image keywords
                    ctx_vision_b64: str | None = None
                    rec_img = self._group_recent_images.get(session_key)
                    if isinstance(rec_img, dict) and (time.time() - float(rec_img.get("ts", 0))) <= 90.0:
                        if rec_img.get("sender_id") == sender_id or any(
                            kw in effective_text for kw in IMAGE_FOLLOWUP_KEYWORDS
                        ):
                            ctx_vision_b64 = str(rec_img.get("b64") or "") or None

                    is_same_sp = sender_id == str(conv.get("last_speaker_id", ""))
                    should_rep, rep_txt, _, used_tok = await df.evaluate_group_active_continuation(
                        bot_uuid=bot_uuid,
                        api_key=api_key,
                        base_url=base_url,
                        model=model,
                        group_id=launcher_id,
                        sender_id=sender_id,
                        current_text=effective_text,
                        recent_lines=recent_lines,
                        last_bot_reply=str(conv.get("last_bot_reply", "")),
                        is_same_speaker=is_same_sp,
                        state=self._state,
                        group_memory_str=g_mem_str,
                        familiarity_str=fam_str + anti_rep_str + temper_str,
                        sender_name=sender_name,
                        vision_b64=ctx_vision_b64,
                        mode=settings["mode"],
                    )
                    if used_tok:
                        self._record_tokens(used_tok)
                    self._save_persistent_state()
                    if should_rep and rep_txt:
                        cleaned_rep = self._clean_reply(rep_txt)
                        final_txt, _, meme_b64_cont = self._extract_text_and_meme(cleaned_rep)
                        final_txt = df.strip_redundant_speaker_name(final_txt, sender_name)
                        if temper_meme_tag and not meme_b64_cont:
                            res_tm = self._resolve_meme_b64(temper_meme_tag)
                            if res_tm:
                                meme_b64_cont = res_tm[1]
                        send_cont_meme = meme_b64_cont if settings["meme_enabled"] else None
                        was_multi = self._last_multi_bubble.get(session_key, False)
                        bubbles = self._split_reply_bubbles(final_txt, last_was_multi=was_multi)
                        if bubbles:
                            self._last_multi_bubble[session_key] = len(bubbles) > 1
                            joined_log = " ".join(bubbles)
                            now_ts = time.time()
                            hist = self._histories.get(session_key, [])
                            hist.append((now_ts, "user", f"[{sender_name}] {effective_text}"))
                            hist.append((now_ts, "assistant", joined_log))
                            self._histories[session_key] = hist[-24:]
                            buf = self._group_msg_buffers.setdefault(session_key, deque(maxlen=12))
                            buf.append(f"[{datetime.now(TZ).strftime('%H:%M')} 大肥鱼]: {joined_log}")
                            self._record_group_bot_activity(session_key, sender_id, joined_log)
                            self._start_session_task(session_key,
                                self._async_post_group_turn_judge(
                                    api_key=api_key,
                                    base_url=base_url,
                                    model=model,
                                    group_id=launcher_id,
                                    expected_state=g_state,
                                    user_text=f"[{sender_name}] {effective_text}",
                                    reply_text=joined_log,
                                    sender_id=sender_id,
                                    sender_name=sender_name,
                                )
                            )
                            first_img = send_cont_meme if len(bubbles) == 1 else None
                            is_anon_sp = (
                                not sender_name
                                or sender_name in ("这位群友", "群友", "朋友")
                                or sender_name.lower().startswith("wxid_")
                            )
                            # Direction 2A: Smart Group @ Rhythm — do not @ on consecutive follow-ups or unresolved wxid
                            if is_same_sp or is_anon_sp:
                                await self._reply_group_no_at(event_ctx, launcher_id, bubbles[0], first_img)
                            else:
                                self._reply(event_ctx, bubbles[0], first_img)
                            if len(bubbles) > 1:
                                self._start_session_task(session_key,
                                    self._send_followup_bubbles(
                                        launcher_id, bubbles[1:], send_cont_meme, is_group=True, bot_uuid=bot_uuid
                                    )
                                )
                            return
                    else:
                        # Speaker switched topic away from Dafeiyu
                        act_set = conv.get("active_speakers")
                        if isinstance(act_set, set):
                            act_set.discard(sender_id)
                        repeated_txt = df.check_group_random_repeat(
                            self._group_repeat_tracker,
                            settings,
                            session_key,
                            sender_id,
                            msg_chain,
                            effective_text,
                        )
                        if repeated_txt:
                            await self._reply_group_no_at(event_ctx, launcher_id, repeated_txt)
                        return

                # Diana Feature 1: Random Repeat (0 LLM token, >=2 users, 30% prob, 60s cooldown)
                repeated_txt = df.check_group_random_repeat(
                    self._group_repeat_tracker,
                    settings,
                    session_key,
                    sender_id,
                    msg_chain,
                    effective_text,
                )
                if repeated_txt:
                    await self._reply_group_no_at(event_ctx, launcher_id, repeated_txt)
                    return

                # Diana Feature 2: Proactive Group Interjection (Decide whether to reply or stay quiet + log reason)
                if settings["group_interject_enabled"]:
                    should_judge = df.record_group_message_for_interject(
                        self._group_msg_buffers,
                        self._group_interject_meta,
                        session_key,
                        sender_id,
                        effective_text,
                        sender_name=sender_name,
                        cooldown_sec=settings["interject_cooldown_sec"],
                        min_messages=settings["interject_min_messages"],
                    )
                    if should_judge:
                        api_key = self._get_config_str("deepseek_api_key", DEFAULT_API_KEY)
                        base_url = self._get_config_str("deepseek_base_url", DEFAULT_BASE_URL).rstrip("/")
                        model = self._normalize_api_model(self._get_config_str("deepseek_model", DEFAULT_MODEL))
                        recent_lines = list(self._group_msg_buffers.get(session_key, []))
                        g_state = self._get_group_state(launcher_id)
                        g_mem_str = self._recall_group_memories(g_state, effective_text)
                        should_rep, rep_txt, _, used_tok = await df.evaluate_group_interject(
                            bot_uuid=bot_uuid,
                            api_key=api_key,
                            base_url=base_url,
                            model=model,
                            group_id=launcher_id,
                            recent_lines=recent_lines,
                            state=self._state,
                            group_memory_str=g_mem_str,
                            mode=settings["mode"],
                        )
                        if used_tok:
                            self._record_tokens(used_tok)
                        self._save_persistent_state()
                        if should_rep and rep_txt:
                            cleaned_rep = self._clean_reply(rep_txt)
                            final_txt, interj_meme_tag, interj_meme_b64 = self._extract_text_and_meme(cleaned_rep)
                            final_txt = df.strip_redundant_speaker_name(final_txt, sender_name)
                            turns_since_meme = int(g_state.get("turnsSinceMeme", 99)) + 1
                            last_meme_at = int(g_state.get("lastMemeAt", 0))
                            now_ts_int = int(time.time())
                            if interj_meme_b64:
                                if turns_since_meme < 5 or (now_ts_int - last_meme_at) < 180 or random.random() >= 0.15:
                                    interj_meme_b64 = None
                                else:
                                    interj_meme_b64 = df.pick_stolen_or_official_meme(
                                        interj_meme_tag or "搞笑", interj_meme_b64, settings, self._stolen_memes
                                    )
                            send_interj_meme = interj_meme_b64 if settings["meme_enabled"] else None
                            if send_interj_meme:
                                g_state["turnsSinceMeme"] = 0
                                g_state["lastMemeAt"] = now_ts_int
                            else:
                                g_state["turnsSinceMeme"] = turns_since_meme
                            self._save_persistent_state()
                            was_multi = self._last_multi_bubble.get(session_key, False)
                            bubbles = self._split_reply_bubbles(final_txt, last_was_multi=was_multi)
                            if bubbles:
                                self._last_multi_bubble[session_key] = len(bubbles) > 1
                                joined_log = " ".join(bubbles)
                                now_ts = time.time()
                                hist = self._histories.get(session_key, [])
                                hist.append((now_ts, "user", f"[{sender_name}] {effective_text}"))
                                hist.append((now_ts, "assistant", joined_log))
                                self._histories[session_key] = hist[-24:]
                                buf = self._group_msg_buffers.setdefault(session_key, deque(maxlen=12))
                                buf.append(f"[{datetime.now(TZ).strftime('%H:%M')} 大肥鱼]: {joined_log}")
                                self._group_interject_meta[session_key] = {"last_judge_ts": now_ts, "unjudged_count": 0}
                                self._record_bot_sent(joined_log, session_key)
                                first_img = send_interj_meme if len(bubbles) == 1 else None
                                await self._reply_group_no_at(event_ctx, launcher_id, bubbles[0], first_img)
                                if len(bubbles) > 1:
                                    self._start_session_task(session_key,
                                        self._send_followup_bubbles(
                                            launcher_id, bubbles[1:], send_interj_meme, is_group=True, bot_uuid=bot_uuid
                                        )
                                    )
                return
            elif not is_group_voice and not (fwd_title or fwd_des) and not incoming_img_b64:
                # Explicitly waking the bot clears any temporary quiet mode
                self._group_mute_until.pop(session_key, None)
                clean_for_buf = self._strip_wake_prefix(effective_text) or effective_text.strip()
                if not clean_for_buf.startswith("<"):
                    df.append_group_context_line(
                        self._group_msg_buffers, session_key, sender_id, clean_for_buf, sender_name=sender_name
                    )

        # 1. 手动语音（设置开关已由统一设置入口处理）
        if cmd_norm.startswith("大肥鱼 语音 ") or cmd_norm.startswith("鲸鱼娘 语音 "):
            tts_txt = re.sub(r"^(?:大肥鱼|鲸鱼娘)\s*语音\s*", "", cmd_norm).strip()
            if tts_txt:
                sf_key = self._get_config_str("siliconflow_api_key", DEFAULT_SF_API_KEY)
                target_recv = launcher_id if is_group else sender_id
                ok_v = await df.synthesize_and_send_wcf_voice(target_recv, tts_txt, sf_api_key=sf_key)
                if ok_v:
                    self._record_bot_sent(tts_txt, session_key)
                    event_ctx.prevent_default()
                else:
                    self._reply(event_ctx, tts_txt)
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
            card_txt, meme_b64 = await self._generate_and_post_moment(topic)
            self._reply(event_ctx, card_txt, meme_b64)
            return

        if cmd_norm in ("大肥鱼 朋友圈", "鲸鱼娘 朋友圈", "鱼塘朋友圈"):
            lines, meme_b64 = self._format_fish_pond_wall()
            lines.append("💡 发送「大肥鱼 发朋友圈 [主题]」让她发新动态，或发「大肥鱼 点赞朋友圈」给她点赞！")
            self._reply(event_ctx, "\n".join(lines), meme_b64 if settings["meme_enabled"] else None)
            return

        if cmd_norm in (
            "大肥鱼 刷朋友圈",
            "大肥鱼 看朋友圈",
            "大肥鱼 看看朋友圈",
            "鲸鱼娘 刷朋友圈",
            "鲸鱼娘 看朋友圈",
            "刷朋友圈",
        ):
            msg_txt, meme_b64 = await self._browse_and_roast_moments(
                is_group=is_group, sender_id=sender_id, session_key=session_key
            )
            self._reply(event_ctx, msg_txt, meme_b64 if settings["meme_enabled"] else None)
            return

        m_like_idx = re.match(r"^(?:大肥鱼|鲸鱼娘)\s*(?:点赞朋友圈|赞朋友圈|给朋友圈点赞)\s+(\d+)$", cmd_norm)
        if m_like_idx:
            msg_txt, meme_b64 = await self._handle_owner_moment_interaction_cmd(
                is_group=is_group,
                sender_id=sender_id,
                session_key=session_key,
                action="like",
                index_num=int(m_like_idx.group(1)),
            )
            self._reply(event_ctx, msg_txt, meme_b64 if settings["meme_enabled"] else None)
            return

        m_comment_idx = re.match(
            r"^(?:大肥鱼|鲸鱼娘)\s*(?:评论朋友圈|评朋友圈|回复朋友圈)\s+(\d+)(?:\s+(.+))?$",
            cmd_norm,
        )
        if m_comment_idx:
            msg_txt, meme_b64 = await self._handle_owner_moment_interaction_cmd(
                is_group=is_group,
                sender_id=sender_id,
                session_key=session_key,
                action="comment",
                index_num=int(m_comment_idx.group(1)),
                custom_comment=(m_comment_idx.group(2) or "").strip(),
            )
            self._reply(event_ctx, msg_txt, meme_b64 if settings["meme_enabled"] else None)
            return

        if cmd_norm in ("大肥鱼 好友申请", "大肥鱼 查看好友申请", "鲸鱼娘 好友申请", "大肥鱼 好友请求"):
            msg_txt, meme_b64 = await self._handle_owner_friend_request_cmd(
                is_group=is_group,
                sender_id=sender_id,
                session_key=session_key,
                action="list",
            )
            self._reply(event_ctx, msg_txt, meme_b64 if settings["meme_enabled"] else None)
            return

        m_accept_friend = re.match(
            r"^(?:大肥鱼|鲸鱼娘)\s*(?:同意好友|通过好友|接受好友|同意好友申请|通过好友申请)\s+(\S+)$",
            cmd_norm,
        )
        if m_accept_friend:
            msg_txt, meme_b64 = await self._handle_owner_friend_request_cmd(
                is_group=is_group,
                sender_id=sender_id,
                session_key=session_key,
                action="accept",
                arg=m_accept_friend.group(1).strip(),
            )
            self._reply(event_ctx, msg_txt, meme_b64 if settings["meme_enabled"] else None)
            return

        m_friend_kw = re.match(
            r"^(?:大肥鱼|鲸鱼娘)\s*(?:好友验证词|好友关键词|自动好友关键词)\s+(.+)$",
            cmd_norm,
        )
        if m_friend_kw:
            msg_txt, meme_b64 = await self._handle_owner_friend_request_cmd(
                is_group=is_group,
                sender_id=sender_id,
                session_key=session_key,
                action="keywords",
                arg=m_friend_kw.group(1).strip(),
            )
            self._reply(event_ctx, msg_txt, meme_b64 if settings["meme_enabled"] else None)
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

        if cmd_norm in ("大肥鱼 搭话", "大肥鱼 找我说话") and is_group:
            effective_text = "大肥鱼 请结合本群的公开话题和群聊档案，主动跟大家搭句话"
        elif cmd_norm in ("大肥鱼 搭话", "大肥鱼 找我说话"):
            target_recv = sender_id
            reply_su, meme_b64 = await self._trigger_speak_up_for_user(sender_id, manual=True)
            send_meme = meme_b64 if settings["meme_enabled"] else None
            if reply_su:
                bubbles = self._split_reply_bubbles(reply_su)
                if len(bubbles) > 1:
                    self._reply(event_ctx, bubbles[0], None)
                    self._start_session_task(session_key, self._send_followup_bubbles(target_recv, bubbles[1:], send_meme, bot_uuid=bot_uuid))
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
                self._reset_session(session_key, doc_key)
                if is_group:
                    self._state.setdefault("groups", {}).pop(launcher_id, None)
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

        # 6.5 Diana 扩展：插话记录、表情库、音乐与搜索
        if cmd_norm in ("大肥鱼 搭话记录", "大肥鱼 插话记录", "大肥鱼 决策记录", "鲸鱼娘 搭话记录"):
            owners = set(re.split(r"[,，;；\s]+", self._get_config_str("owner_ids", DEFAULT_OWNER_IDS))) - {""}
            if not is_group and sender_id not in owners:
                self._reply(event_ctx, "请在群内查看本群搭话记录；跨群记录仅主人私聊可查看")
            else:
                self._reply(event_ctx, df.format_interject_logs(
                    self._state, bot_uuid=bot_uuid, group_id=launcher_id if is_group else None,
                ))
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

        # Diana Feature 7: Multi-library Music Player (Explicit command + Natural Language song requests + Random song)
        is_explicit_music = (
            cmd_norm.startswith("大肥鱼 点歌 ")
            or cmd_norm.startswith("鲸鱼娘 点歌 ")
            or cmd_norm.startswith("点歌 ")
            or cmd_norm.startswith("来首歌 ")
        )
        is_random_music = bool(NATURAL_RANDOM_MUSIC_RE.match(cmd_norm))
        m_nat_song = None if (is_explicit_music or is_random_music) else NATURAL_SONG_QUERY_RE.match(cmd_norm)
        nat_song_name = (m_nat_song.group(1).strip() if m_nat_song else "")
        if nat_song_name and any(w in nat_song_name for w in NON_SONG_WORDS):
            nat_song_name = ""

        if is_explicit_music or is_random_music or nat_song_name:
            if is_random_music:
                song = await df.pick_random_playable_song()
                song_q = "随机歌曲"
            else:
                song_q = (
                    nat_song_name
                    if nat_song_name
                    else re.sub(r"^(?:大肥鱼\s*|鲸鱼娘\s*)?(?:点歌|来首歌)\s*", "", cmd_norm).strip()
                )
                if not song_q:
                    song = await df.pick_random_playable_song()
                else:
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
            prefix_note = "🎲 顺手给你随机挑了一首好听的：\n" if is_random_music else ""
            msg = (
                f"{prefix_note}🎵 【大肥鱼点歌台 · {song.get('source')}】\n"
                f"• 歌名：{song.get('title')}\n"
                f"• 歌手：{song.get('artist')}\n"
                + (f"• 专辑：{song.get('album')}\n" if song.get("album") else "")
                + f"• 链接：{song.get('page_url')}\n"
                + f"{audio_note}"
            )
            self._reply(event_ctx, msg, None if card_ok else cover_b64)
            return

        # 7. 当前对话重置
        if cmd_norm in ("大肥鱼 重置", "鲸鱼娘 重置", "清除记忆", "重置对话", "大肥鱼 清除记忆"):
            self._reset_session(session_key, doc_key)
            self._reply(event_ctx, "行啦，刚才聊的这几句翻篇了！长期档案还留着呢（要彻底清空请发「大肥鱼 失忆」），想聊啥直说")
            return

        if not settings["enabled"]:
            return

        if not incoming_img_b64 and self._should_ignore_plugin_command(raw_text):
            return

        is_sticker_in = self._is_incoming_sticker(raw_text)
        is_voice_in = self._is_incoming_voice(raw_text)
        is_file_in = df.is_incoming_wechat_file(raw_text)
        vision_b64: str | None = incoming_img_b64
        parsed_link_card: str | None = None
        parsed_link_img_b64: str | None = None
        hist_user_text: str = ""

        if not is_group and incoming_img_b64:
            self._private_recent_images[session_key] = {
                "sender_id": sender_id,
                "b64": incoming_img_b64,
                "ts": time.time(),
            }

        if is_sticker_in:
            if is_group:
                return
            vision_b64 = await self._fetch_sticker_strip_b64(raw_text)
            if vision_b64:
                self._private_recent_images[session_key] = {
                    "sender_id": sender_id,
                    "b64": vision_b64,
                    "ts": time.time(),
                }
                cleaned_text = "（对方刚刚给你发了这张微信表情包！如果是动图则已抽帧拼成左中右三格。请用 DeepSeek-V4.1-Flash 视觉看清表情包里的画面/角色动作/文字，用大肥鱼的微信活人短句接梗回应一句，并在最末尾附上 `[表情: 名称]` 甩一张你的表情包回去！）"
            else:
                cleaned_text = "（对方刚刚给你发了一张微信表情包跟你斗图！请用活人短句回敬一句，并在最末尾附上 `[表情: 名称]` 甩一张你的表情包回去！）"
            hist_user_text = "[发来了一张微信表情包]"
        elif is_voice_in:
            voice_text, voice_tone, voice_dur = await self._transcribe_incoming_voice(raw_text)
            if is_group:
                if voice_text:
                    df.append_group_context_line(
                        self._group_msg_buffers,
                        session_key,
                        sender_id,
                        f"[语音] {voice_text}",
                        sender_name=sender_name,
                    )
                has_voice_wake = bool(
                    voice_text and any(w in voice_text for w in ("大肥鱼", "鲸鱼娘", "小鲸鱼", "肥鱼"))
                )
                conv_v = self._group_active_conv.get(session_key) or {}
                is_same_speaker_60s = bool(
                    sender_id
                    and sender_id == str(conv_v.get("last_speaker_id", ""))
                    and (time.time() - float(conv_v.get("last_bot_ts", 0.0))) <= 60.0
                )
                allow_voice_followup = bool(
                    not settings["explicit_only"]
                    and settings["continuation_enabled"]
                    and (is_quoting_bot or is_same_speaker_60s)
                )
                if not voice_text or not (has_voice_wake or allow_voice_followup):
                    return
                self._group_mute_until.pop(session_key, None)
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
                self._user_docs[doc_key] = {"name": fname, "text": ftext, "ts": time.time()}
                cleaned_text = (
                    f"（对方刚刚发来了一个文件《{fname}》，已为你提取出文件中的核心文本内容如下：\n"
                    f"--------------------\n{ftext[:7000]}\n--------------------\n"
                    "请用大肥鱼的活人口吻，先简明扼要地帮对方总结这份文件的重点内容，并问问对方具体想看哪一部分！）"
                )
                hist_user_text = f"[发送了文件《{fname}》]"
            else:
                self._reply(event_ctx, f"收到文件《{fname}》啦，但这会儿文件还在下载或者格式受限没能提取出文字，你可以直接把里面的关键段落发出来给我看")
                return
        elif fwd_title or fwd_des:
            if is_bot_fwd:
                cleaned_text = (
                    f"（对方刚刚把一段包含你发言的聊天记录《{fwd_title}》合并转发到了聊天里给大家围观！\n"
                    f"【转发的聊天记录内容】：\n{fwd_des}\n"
                    "请用大肥鱼的微信活人短句（默认1条气泡，严禁打括号动作）自然接梗、嘴硬或吐槽对方居然把跟你的聊天记录发出来公开处刑！）"
                )
            else:
                cleaned_text = (
                    f"（对方刚刚合并转发了一段聊天记录《{fwd_title}》：\n"
                    f"{fwd_des}\n"
                    "请用大肥鱼的微信活人短句（默认1条气泡，严禁打括号动作）对这段聊天记录顺嘴锐评或接话一句！）"
                )
            hist_user_text = f"[合并转发《{fwd_title}》] {fwd_des[:100]}"
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
                if is_group and not (
                    self._is_group_woken(msg_chain, effective_text, ev_raw_obj)
                    or is_quoting_bot
                    or bool(incoming_img_b64)
                ):
                    return
                cleaned_text = self._strip_wake_prefix(effective_text)
                if not cleaned_text:
                    cleaned_text = effective_text.strip()

                if not incoming_img_b64 and self._should_ignore_plugin_command(cleaned_text):
                    return
                if not vision_b64 and is_group:
                    # Direction 1: Attach cached recent group image if speaker sent one within 90s or mentioned image keywords
                    rec_img = self._group_recent_images.get(session_key)
                    if isinstance(rec_img, dict) and (time.time() - float(rec_img.get("ts", 0))) <= 90.0:
                        if rec_img.get("sender_id") == sender_id or any(
                            kw in cleaned_text for kw in IMAGE_FOLLOWUP_KEYWORDS
                        ):
                            vision_b64 = str(rec_img.get("b64") or "") or None
                elif not vision_b64 and not is_group:
                    rec_priv_img = self._private_recent_images.get(session_key)
                    if isinstance(rec_priv_img, dict):
                        img_age = time.time() - float(rec_priv_img.get("ts", 0))
                        if img_age <= 45.0 or (
                            img_age <= 90.0 and any(kw in cleaned_text for kw in IMAGE_FOLLOWUP_KEYWORDS)
                        ):
                            vision_b64 = str(rec_priv_img.get("b64") or "") or None

                if vision_b64 and cleaned_text in ("", "[图片]"):
                    cleaned_text = (
                        "（对方刚刚发了这张图片，请仔细看清图片里的画面、文字和细节，"
                        "用大肥鱼的微信活人短句顺嘴吐槽或回应一句！）"
                    )
                    hist_user_text = "[发了一张图片]"
                elif quote_content:
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

        # Evict expired document, image & session caches to prevent unbounded memory growth
        for k_doc in [k for k, v in list(self._user_docs.items()) if now - float(v.get("ts", 0)) > 1800]:
            self._user_docs.pop(k_doc, None)
        for k_img in [k for k, v in list(self._group_recent_images.items()) if now - float(v.get("ts", 0)) > 120]:
            self._group_recent_images.pop(k_img, None)
        for k_pimg in [k for k, v in list(self._private_recent_images.items()) if now - float(v.get("ts", 0)) > 120]:
            self._private_recent_images.pop(k_pimg, None)
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
        coarse_time_str = f"{now_dt.strftime('%Y-%m-%d')}（{wk_str} {period}）"
        exact_time_str = f"{now_dt.strftime('%H:%M')}（{wk_str}{period}）"
        daily_sched = df.get_or_create_daily_schedule(self._state, now_dt)
        schedule_line = df.format_daily_schedule_line(daily_sched, hour)
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
        cached_doc = self._user_docs.get(doc_key)
        if cached_doc and (now - float(cached_doc.get("ts", 0))) < 1800 and not is_file_in:
            doc_sec = (
                f"\n\n## 五、对方最近上传的文档缓存（《{cached_doc.get('name')}》，若对方追问文档内容请据此回答）\n"
                f"{str(cached_doc.get('text', ''))[:4500]}"
            )

        # Direction 3B & 3C + Turn-Level Reminders: Anti-Repetition Gate, Temper State, Time Gap, Unfetched Media
        last_hist_ts = history[-1][0] if history else None
        time_gap_notice = df.build_time_gap_notice(last_hist_ts, now)
        unfetched_notice = df.build_unfetched_media_notice(
            raw_text,
            parsed_link_card=parsed_link_card,
            vision_b64=vision_b64,
            is_sticker_in=is_sticker_in,
        )
        anti_rep_line = df.build_anti_repetition_instruction(recent_bot_texts)
        temper_hint, temper_meme_tag, temper_mood_delta = df.check_and_update_temper_state(
            self._temper_state,
            session_key,
            sender_id,
            cleaned_text,
        )

        base_persona = LIVE_PERSONA_BASE if settings["mode"] == "live" else SWEET_PERSONA_BASE
        self_moments_line = self._build_self_moments_section()

        if is_group:
            # Group Chat: Use Group Archive (groups[launcher_id]) + Read-Only Private Familiarity + Recent Group Chat Flow
            g_state = self._get_group_state(launcher_id)
            g_state["lastChatAt"] = int(now * 1000)
            if temper_mood_delta != 0:
                g_state["mood"] = int(_clamp(int(g_state.get("mood", 75)) + temper_mood_delta, 25, 100))
            meme_cooldown_state = g_state
            g_mood = int(g_state.get("mood", 75))
            g_reason = g_state.get("lastReason", "在群里看大家聊天")
            g_mem_sec = self._recall_group_memories(g_state, cleaned_text)
            familiar_sec = self._recall_readonly_private_familiarity(sender_id, sender_name, launcher_id)
            member_names = df.get_cached_group_member_names(launcher_id)
            roster_line = (
                f"- **本群成员真实昵称名单**：{'、'.join(member_names)}\n"
                if member_names
                else ""
            )
            name_rule_line = (
                f"- **群聊称呼铁律（极重要）**：当前正在跟你说话的群友叫「{sender_name}」。"
                f"在你的回复正文中【绝对禁止再次写出当前说话人的名字「{sender_name}」】（否则名字会重复出现），直接说你要回的话即可！"
                "只有当你在话里需要提到群内【其他第三方群友】时，才直接写那位群友的真实群昵称/微信昵称（严禁使用微信号 wxid_xxx 或后四位如 aq22、oh22、nl22）！\n"
            )
            recent_grp_lines = list(self._group_msg_buffers.get(session_key, []))[-8:]
            grp_ctx_block = (
                "\n".join(recent_grp_lines)
                if recent_grp_lines
                else "（暂无前序群聊记录）"
            )
            dynamic_system_prompt = (
                f"{base_persona}\n\n"
                f"## 四、当前群聊实时感知（你在微信群「{launcher_id[:10]}」中跟群友互动）\n"
                f"{roster_line}"
                f"{name_rule_line}"
                f"- **本群专属群聊档案（群梗与近期话题）**：\n{g_mem_sec}\n"
                f"{familiar_sec}\n"
                f"{self_moments_line}"
                f"{schedule_line}"
                f"- **当前日期与时段**：{coarse_time_str}\n"
                f"- **本群气氛心情**：{g_mood}/100（{g_reason}）\n"
                f"{grain_line}"
                f"{anti_rep_line}"
                f"{temper_hint}"
                f"- **群里刚刚聊的上下文（帮助你理解大家在讨论什么）**：\n{grp_ctx_block}"
                f"{doc_sec}"
            )
        else:
            # Private Chat: Full personal mood + intimacy tier + 8-chapter personal archive
            u = self._get_user_state(sender_id)
            u["hasPrivateChat"] = True
            u["lastChatAt"] = int(now * 1000)
            u["missStreak"] = 0
            if temper_mood_delta != 0:
                u["mood"] = int(_clamp(int(u.get("mood", 70)) + temper_mood_delta, 15, 100))
            if sender_name and sender_name not in ("朋友", "群友", "这位群友") and not sender_name.startswith("wxid_"):
                u["nickname"] = sender_name
            nick_val = str(u.get("nickname") or "").strip()
            nick_line = f"- **对方微信昵称**：{nick_val}\n" if nick_val else ""
            meme_cooldown_state = u
            intimacy_sec = self._build_intimacy_section(u, mode=settings["mode"])
            mood_sec = self._build_mood_section(u)
            mem_sec = self._recall_memories(u, cleaned_text)
            moment_ctx_line = await self._check_friend_moment_for_chat(
                sender_id, sender_name, session_key, cleaned_text, history_len=len(history)
            )
            dynamic_system_prompt = (
                f"{base_persona}\n\n"
                f"## 四、当前私聊实时感知（系统每轮自动刷新，自然融入语气，不要机械背诵）\n"
                f"{nick_line}"
                f"- **你们的亲密度与关系阶段**：{intimacy_sec}\n"
                f"- **鱼的记忆（dsh-fish-memory）**：\n{mem_sec}\n"
                f"{self_moments_line}"
                f"{moment_ctx_line}"
                f"{schedule_line}"
                f"- **当前日期与时段**：{coarse_time_str}\n"
                f"- **鱼的心情（dsh-feiyu-mood）**：\n{mood_sec}\n"
                f"{grain_line}"
                f"{anti_rep_line}"
                f"{temper_hint}"
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

        user_asked_voice = df.is_voice_reply_requested(effective_text) or cmd_norm in (
            "大肥鱼 说句话",
            "大肥鱼 发语音",
            "大肥鱼 来条语音",
        )
        if user_asked_voice:
            cleaned_text += (
                "\n（注意：对方让你发语音条回复！本轮回复将直接合成为你的真声微信语音发送，"
                "请用自然口语化的 1 句短话（10~45字）回答，不要包含符号、链接、代码块或括号！）"
            )

        tail_parts: list[str] = []
        if time_gap_notice:
            tail_parts.append(time_gap_notice)
        if unfetched_notice:
            tail_parts.append(unfetched_notice)
        if anti_rep_line.strip():
            tail_parts.append(anti_rep_line.strip().lstrip("- "))
        if temper_hint.strip() and settings["mode"] == "live":
            tail_parts.append(temper_hint.strip().lstrip("- "))
        if settings["mode"] == "sweet":
            tail_parts.append(
                "【当前为甜宠陪伴模式：语气要软糯、黏人、欢快偏心，像跟喜欢的人撒娇聊天一样，严禁毒舌抬杠、嫌弃损人或怀疑对方没好事！】"
            )
        tail_parts.append(
            f"【现在时间：{exact_time_str} | 动笔前确认：绝不打括号写动作、单气泡短句为主、只提真聊过的事】"
        )
        tail_block = "\n\n" + "\n".join(tail_parts)
        user_msg_for_llm = (f"[{sender_name}]: {cleaned_text}" if is_group else cleaned_text) + tail_block

        messages: list[dict[str, Any]] = [{"role": "system", "content": dynamic_system_prompt}]
        for _, r, c in history:
            messages.append({"role": r, "content": c})
        use_vl_bridge = bool(vision_b64 and "api.deepseek.com" in base_url.lower())
        if use_vl_bridge and vision_b64:
            sf_key = self._get_config_str("siliconflow_api_key", DEFAULT_SF_API_KEY)
            vl_desc = await df.describe_image_via_siliconflow(
                vision_b64, sf_api_key=sf_key, user_hint=cleaned_text
            )
            if vl_desc:
                if hist_user_text in ("[发了一张图片]", "[发来了一张微信表情包]"):
                    hist_user_text = f"{hist_user_text[:-1]}：{vl_desc[:120]}]"
                user_msg_for_llm += (
                    "\n\n【附图/表情包视觉内容（请直接当作你亲眼看见的图片画面与文字自然回应，绝不要提及“视觉模型”或“识别报告”）】：\n"
                    f"{vl_desc}"
                )
            else:
                user_msg_for_llm += (
                    "\n\n（注：对方发了一张图片，但当前网络波动没能加载出画面；请直接自然告诉对方图没刷出来，严禁猜测或编造图片内容！）"
                )
            messages.append({"role": "user", "content": user_msg_for_llm})
        elif vision_b64:
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

        chat_temp = self._select_chat_temperature(
            cleaned_text,
            is_file_in=is_file_in,
            has_link_card=bool(parsed_link_card),
            has_doc_ctx=bool(doc_sec),
        )
        tool_calls: list[Any] = []
        try:
            async with httpx.AsyncClient(timeout=50.0) as client:
                req_payload: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": chat_temp,
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
                if resp.status_code == 400 and vision_b64 and isinstance(messages[-1].get("content"), list):
                    sf_key = self._get_config_str("siliconflow_api_key", DEFAULT_SF_API_KEY)
                    vl_desc = await df.describe_image_via_siliconflow(
                        vision_b64, sf_api_key=sf_key, user_hint=cleaned_text
                    )
                    fallback_msg = user_msg_for_llm + (
                        f"\n\n【附图/表情包视觉内容】：\n{vl_desc}"
                        if vl_desc
                        else "\n\n（注：对方发了一张图片，但当前没能加载出画面；请自然告诉对方图没刷出来，不要瞎猜图片内容！）"
                    )
                    messages[-1] = {"role": "user", "content": fallback_msg}
                    req_payload["messages"] = messages
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
                    for tool_index, tc in enumerate(tool_calls):
                        fn = (tc.get("function") or {}).get("name", "")
                        raw_args = (tc.get("function") or {}).get("arguments") or "{}"
                        try:
                            args_obj = json.loads(raw_args)
                        except Exception:
                            args_obj = {}
                        if tool_index >= 3:
                            tool_out = "本轮最多执行 3 个工具调用，此调用未执行；请依据已有结果回答。"
                        elif fn == "web_search":
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
                            "temperature": 0.78,
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
                if is_group and not parsed_link_card:
                    reply_text = df.strip_redundant_speaker_name(reply_text, sender_name)

                # Diana Feature 3: Steal incoming sticker and mix stolen stickers when replying
                if is_sticker_in:
                    self._start_session_task(session_key, df.steal_incoming_sticker(raw_text, settings, self._stolen_memes, meme_tag))

                user_asked_meme = any(
                    kw in effective_text for kw in ("表情包", "发个表情", "发张表情", "斗图", "来张图", "发个图")
                )
                if temper_meme_tag and not meme_b64:
                    res_tm = self._resolve_meme_b64(temper_meme_tag)
                    if res_tm:
                        meme_tag, meme_b64 = res_tm
                force_meme = bool(is_sticker_in or user_asked_meme or temper_meme_tag)
                if force_meme and not meme_b64 and self._memes_b64:
                    meme_tag = random.choice(["得意", "干饭", "猫爪", "偷看", "飘了"])
                    meme_b64 = self._memes_b64.get(meme_tag)

                if meme_b64:
                    meme_b64 = df.pick_stolen_or_official_meme(meme_tag or "搞笑", meme_b64, settings, self._stolen_memes)

                # Scheme 1: Code-level cooldown (min 4 text turns & 180s interval) + 15% real RNG gate for normal chats
                turns_since_meme = int(meme_cooldown_state.get("turnsSinceMeme", 99)) + 1
                last_meme_at = int(meme_cooldown_state.get("lastMemeAt", 0))
                if not force_meme and meme_b64:
                    if turns_since_meme < 5 or (int(now) - last_meme_at) < 180 or random.random() >= 0.15:
                        meme_b64 = None

                if meme_b64 and settings["meme_enabled"]:
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
        was_multi = self._last_multi_bubble.get(session_key, False)
        bubbles = self._split_reply_bubbles(
            reply_text, allow_split=allow_split, last_was_multi=was_multi
        )
        self._last_multi_bubble[session_key] = len(bubbles) > 1
        reply_flat = " ".join(bubbles) if bubbles else reply_text

        if not hist_user_text:
            hist_user_text = "[发来了一张微信表情包]" if is_sticker_in else cleaned_text
        hist_entry_user = f"[{sender_name}] {hist_user_text}" if is_group else hist_user_text
        history.append((now, "user", hist_entry_user))
        history.append((now, "assistant", reply_flat))
        if len(history) > max_turns * 2:
            history = history[-(max_turns * 2) :]
        self._histories[session_key] = history

        prev_conv = self._group_active_conv.get(session_key) or {}
        is_anon_speaker = (
            not sender_name
            or sender_name in ("这位群友", "群友", "朋友")
            or sender_name.lower().startswith("wxid_")
        )
        no_at_group_reply = bool(
            is_group
            and (
                is_anon_speaker
                or (
                    prev_conv.get("last_speaker_id") == sender_id
                    and (now - float(prev_conv.get("last_bot_ts", 0.0))) <= 60.0
                    and not self._is_group_woken(msg_chain, effective_text, ev_raw_obj)
                )
            )
        )

        if is_group:
            buf = self._group_msg_buffers.setdefault(session_key, deque(maxlen=12))
            buf.append(f"[{datetime.now(TZ).strftime('%H:%M')} 大肥鱼]: {reply_flat[:100]}")
            self._record_group_bot_activity(session_key, sender_id, reply_flat)
            self._start_session_task(session_key,
                self._async_post_group_turn_judge(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    group_id=launcher_id,
                    expected_state=g_state,
                    user_text=hist_entry_user,
                    reply_text=reply_flat,
                    sender_id=sender_id,
                    sender_name=sender_name,
                )
            )
        else:
            self._record_bot_sent(reply_flat, session_key)
            self._start_session_task(session_key,
                self._async_post_turn_judge(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    user_id=sender_id,
                    expected_state=u,
                    user_text=hist_user_text,
                    reply_text=reply_flat,
                )
            )

        send_b64 = parsed_link_img_b64 or (meme_b64 if settings["meme_enabled"] else None)
        target_recv = launcher_id if is_group else sender_id

        # Direction 4: WeChat 4.1 Native SILK v3 Voice Replies (TTS -> /api/send_voice)
        should_send_voice = False
        if user_asked_voice:
            should_send_voice = True
        elif (
            settings["voice_reply_enabled"]
            and not (parsed_link_card or is_file_in or bool(tool_calls))
            and self._is_suitable_for_random_voice(reply_flat)
        ):
            if is_voice_in:
                should_send_voice = random.random() < (0.25 if is_group else 0.45)
            elif 6 <= len(reply_flat) <= 36:
                should_send_voice = random.random() < (0.06 if is_group else 0.10)

        if should_send_voice and reply_flat:
            sf_key = self._get_config_str("siliconflow_api_key", DEFAULT_SF_API_KEY)
            voice_ok = await df.synthesize_and_send_wcf_voice(target_recv, reply_flat, sf_api_key=sf_key)
            if voice_ok:
                if send_b64:
                    await df.send_wcf_text_or_image(target_recv, image_b64=send_b64)
                event_ctx.prevent_default()
                return

        if len(bubbles) <= 1:
            first_txt = bubbles[0] if bubbles else reply_text
            if no_at_group_reply:
                await self._reply_group_no_at(event_ctx, launcher_id, first_txt, send_b64)
            else:
                self._reply(event_ctx, first_txt, send_b64)
        else:
            if no_at_group_reply:
                await self._reply_group_no_at(event_ctx, launcher_id, bubbles[0], None)
            else:
                self._reply(event_ctx, bubbles[0], None)
            self._start_session_task(session_key,
                self._send_followup_bubbles(
                    target_recv,
                    bubbles[1:],
                    send_b64,
                    is_group=is_group, bot_uuid=bot_uuid,
                )
            )
