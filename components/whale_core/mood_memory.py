"""Eight-dimension mood with decay, intimacy, polarity-aware fact dedup, memory recall,
archive consolidation, temper state and the daily schedule.

Extracted from ``components/event_listeners/whale_listener.py`` / ``diana_features.py``
(v0.5.2 A-12). Those modules remain the public compatibility facades: tests and callers keep
patching ``whale_listener`` / ``diana_features`` attributes, which the facades propagate here.
"""
from __future__ import annotations

import json
import logging
import math
import random
import re
import time
from datetime import datetime
from typing import Any

import httpx

from . import df  # live facade proxies (see whale_core.__init__)
from .http_gateway import (
    TZ,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ARCHIVE_CHAPTERS",
    "ARCHIVE_CONSOLIDATE_SYSTEM",
    "COAX_KEYWORDS",
    "COMPLAINT_SUBJECT_KEYWORDS",
    "DAILY_ACTIVITY_POOL",
    "DAILY_BREAKFAST_POOL",
    "DAILY_DINNER_POOL",
    "DAILY_LUNCH_POOL",
    "DAILY_SMALL_EVENT_POOL",
    "DAILY_WAKE_POOL",
    "DIMS",
    "DIM_SHORT",
    "DIRECT_BOT_TAUNT_KEYWORDS",
    "GENERIC_TAUNT_KEYWORDS",
    "GROUP_MOOD_JUDGE_SYSTEM",
    "MEMORY_STOP_BIGRAMS",
    "MEMORY_STOP_CHARS",
    "MOOD_DIMS",
    "MOOD_DIM_PLAIN",
    "MOOD_DIM_SHORT",
    "MOOD_FAST_DECAY_DIMS",
    "MOOD_JUDGE_SYSTEM",
    "MOOD_JUDGE_SYSTEM_PROMPT",
    "MoodMemoryMixin",
    "SERIOUS_MOMENT_KEYWORDS",
    "TAUNT_KEYWORDS",
    "check_and_update_temper_state",
    "compare_memory_recall_key",
    "compute_memory_query_metrics",
    "compute_memory_query_overlap",
    "format_daily_schedule_line",
    "get_or_create_daily_schedule",
    "is_serious_or_sad_moment",
    "normalize_memory_owner_pronoun",
    "normalize_mood_judge_delta",
    "strip_memory_query_noise",
]


# ==============================================================================
# from diana_features.py
# ==============================================================================
SERIOUS_MOMENT_KEYWORDS: tuple[str, ...] = (
    "住院", "抢救", "ICU", "去世", "逝世", "过世", "离世", "追悼", "葬礼",
    "灵堂", "守灵", "讣告", "病危", "手术", "急救", "节哀", "默哀", "走好",
    "一路走好", "安息", "肿瘤", "癌症", "绝症", "化疗", "车祸", "骨折",
    "急诊", "重症", "昏迷", "丧事", "悼念", "祭奠", "缅怀", "白事", "出殡",
    "火化", "自杀", "轻生", "跳楼",
)


def is_serious_or_sad_moment(text: str) -> bool:
    """Returns True if text contains obituary, bereavement, hospital/ICU, or serious accident keywords."""
    s = (text or "").strip()
    if not s:
        return False
    s_low = s.lower()
    return any(kw.lower() in s_low for kw in SERIOUS_MOMENT_KEYWORDS)
MEMORY_STOP_CHARS: frozenset[str] = frozenset(
    "的了是在我你他她它这那有和与或也就都而及着过吧呢吗啊呀哦哈嘛呗呐啦诶欸哼啧哇噢唔嗯"
    "去来上下里外前后左右中大小多少高低长短好坏对错是非不没别莫勿休很太更最挺蛮真好只光单"
    "才刚正曾已又再还常总老全各每某本该此其自相互被把给让向往从到以因由按据比跟同除但却"
    "可虽若如假果设即纵便管既凡怎啥谁哪何人天年日月时分秒个位名条件事次遍回趟番阵场段节"
    "章句词字话声手眼心头口脸身家国世吃喝玩乐睡说看听想做干弄找拿放带送用买卖穿戴住走跑"
)
MEMORY_STOP_BIGRAMS: frozenset[str] = frozenset({
    "我是", "你是", "他是", "她是", "这是", "那是", "我在", "你在", "今天", "明天", "昨天",
    "现在", "刚才", "刚刚", "什么", "怎么", "为什么", "为啥", "可以", "不行", "不是", "没有",
    "一个", "一下", "一点", "觉得", "知道", "告诉", "已经", "如果", "因为", "所以", "但是",
    "或者", "还是", "时候", "地方", "东西", "事情", "问题", "意思", "大肥", "肥鱼", "鲸鱼", "鱼娘",
    "没去", "还没", "不去", "没来", "没吃", "没睡", "没看", "没事", "没啥",
    "好像", "应该", "可能", "肯定", "大概", "也许", "其实", "确实", "反正", "随便",
    "之前", "之后", "以后", "最近", "一直", "麻烦", "谢谢", "抱歉", "不错",
    "这样", "那样", "这么", "那么",
})


_NAME_PREFIX_COMMON_SKIP: frozenset[str] = frozenset({
    "大小", "多少", "好坏", "老是", "小心", "大家", "老板", "老师",
    "老婆", "老公", "小时", "小说", "小事", "老家", "小鱼",
})
_STOP_WINDOW_FRAGMENTS: tuple[str, ...] = (
    "我是", "你是", "这是", "那是", "我在", "你在", "今天", "明天", "昨天",
    "现在", "刚才", "刚刚", "什么", "怎么", "为什么", "可以", "不是", "没有",
    "一个", "一下", "一点", "觉得", "知道", "告诉", "已经",
)


def strip_memory_query_noise(query_text: str) -> str:
    """Strips quote headers, share/file wrappers, XML/msg tags, and wake prefixes before memory recall
    so quoted speaker names or protocol wrappers do not pollute recall scoring (dsh-fish-memory v1.6.0 & Diana #1513).
    """
    s = (query_text or "").strip()
    if not s:
        return ""
    s = re.sub(r"【对方引用了消息[\s\S]*?并回复】：\s*", " ", s)
    s = re.sub(r"【引用了[^】]*】", " ", s)
    s = re.sub(r"\[引用「[^」]*」\]", " ", s)
    s = re.sub(r"\[(?:分享链接|发送了文件[^\]]*|发来了一张[^\]]*|发了一张图片|合并转发[^\]]*|微信语音[^\]]*)\]", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"^(?:@?大肥鱼|@?鲸鱼娘|@?小鲸鱼|@?肥鱼)[\s,，:：!！?？]*", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def compute_memory_query_metrics(query_text: str, candidate_text: str) -> tuple[float, int, int]:
    """Computes `(overlap_score, hit_count, hit_char_len)` between a user query and a memory candidate:
    - Uses 2-gram CJK words, 3~4 char CJK proper-noun windows / `[阿小老]X` name prefixes (v1.6.2),
      and alphanumeric tokens as multi-char clues.
    - Never allows single-character stop-word or isolated 1-char coincidence in multi-char queries
      to trigger false-positive recall (aligning with dsh-fish-memory v1.6.0 & Diana #1513).
    """
    q = strip_memory_query_noise(query_text).lower()
    cand = (candidate_text or "").lower()
    if not q or not cand:
        return 0.0, 0, 0

    cjk_runs = re.findall(r"[\u4e00-\u9fff]{2,}", q)
    bigrams: set[str] = set()
    proper_clues: set[str] = set()
    for run in cjk_runs:
        run_len = len(run)
        stop_covered: set[int] = set()
        for sw_len in (2, 3):
            if run_len < sw_len:
                continue
            for idx in range(run_len - sw_len + 1):
                if run[idx : idx + sw_len] in MEMORY_STOP_BIGRAMS:
                    stop_covered.update(range(idx, idx + sw_len))
        for i in range(run_len - 1):
            bg = run[i : i + 2]
            if (
                bg not in MEMORY_STOP_BIGRAMS
                and not (i in stop_covered and (i + 1) in stop_covered)
                and bg[0] not in MEMORY_STOP_CHARS
                and bg[1] not in MEMORY_STOP_CHARS
            ):
                bigrams.add(bg)
            if bg[0] in ("阿", "小", "老") and bg[1] not in MEMORY_STOP_CHARS and bg not in _NAME_PREFIX_COMMON_SKIP:
                proper_clues.add(bg)
        for win_len in (3, 4):
            if run_len < win_len:
                continue
            for i in range(run_len - win_len + 1):
                win = run[i : i + win_len]
                if (
                    win in MEMORY_STOP_BIGRAMS
                    or any(idx in stop_covered for idx in range(i, i + win_len))
                    or any(frag in win for frag in _STOP_WINDOW_FRAGMENTS)
                ):
                    continue
                if (
                    (win[0] not in MEMORY_STOP_CHARS or win[-1] not in MEMORY_STOP_CHARS)
                    and sum(1 for ch in win if ch not in MEMORY_STOP_CHARS) >= 2
                ):
                    proper_clues.add(win)

    ascii_tokens = {
        tk for tk in re.findall(r"[a-z0-9_]{2,}", q) if tk not in ("http", "https", "www", "com")
    }

    matched_bigrams = {bg for bg in bigrams if bg in cand}
    matched_proper = {pc for pc in proper_clues if pc in cand}
    matched_ascii = {tk for tk in ascii_tokens if tk in cand}
    extra_proper = matched_proper - matched_bigrams

    content_chars = {
        ch for ch in re.findall(r"[\u4e00-\u9fff]", q) if ch not in MEMORY_STOP_CHARS
    }
    char_hits = sum(1 for ch in content_chars if ch in cand)

    compact_q_len = len(re.sub(r"\s+", "", q))
    # In normal multi-character queries (>2 chars), single-character matches alone cannot trigger recall
    if not matched_bigrams and not matched_ascii and not extra_proper:
        if compact_q_len <= 2 and char_hits > 0:
            return float(char_hits), char_hits, char_hits
        return 0.0, 0, 0

    all_multi_hits = matched_bigrams | matched_proper | matched_ascii
    hit_count = len(all_multi_hits)
    hit_char_len = sum(len(k) for k in all_multi_hits)
    overlap_score = float(
        len(matched_bigrams) * 2.0
        + len(matched_ascii) * 2.5
        + len(extra_proper) * 1.8
        + char_hits * 0.25
    )
    return overlap_score, hit_count, hit_char_len


def compute_memory_query_overlap(query_text: str, candidate_text: str) -> float:
    """Computes relevance score between a user query and a memory fact/event/chapter."""
    return compute_memory_query_metrics(query_text, candidate_text)[0]


def compare_memory_recall_key(
    overlap: float,
    hit_count: int,
    hit_char_len: int,
    priority_score: float,
) -> tuple[float, int, int, float]:
    """Multi-level recall sort key (dsh-fish-memory v1.6.2):
    rounded overlap DESC -> hit_count DESC -> hit_char_len DESC -> priority_score DESC.
    """
    return (round(float(overlap), 3), int(hit_count), int(hit_char_len), float(priority_score))
DAILY_WAKE_POOL: tuple[str, ...] = (
    "早上 8:20 被饿醒的",
    "上午 9:10 赖床赖到闹钟响了三次才爬起来",
    "早上 7:50 难得早起，结果坐在床边发了十分钟呆",
    "上午 9:40 睡过头差点错过早饭",
    "早上 8:35 迷迷糊糊爬起来找水喝",
    "早上 8:05 被窗外的小鸟吵醒，干脆裹着毯子刷会儿手机",
    "上午 8:50 做了个在海里追小鱼干的梦，醒来还咂巴嘴",
    "早上 8:15 一骨碌爬起来，精神头意外还不错",
    "上午 9:25 跟被窝大战三百回合后艰难获胜",
    "早上 7:45 醒太早了，抱着抱枕又眯了个回笼觉",
    "早上 8:40 闻到楼下早餐铺飘来的香味自动醒了",
    "上午 9:05 迷迷糊糊把闹钟按成贪睡模式两次才起床",
    "早上 8:10 起床第一件事是伸了个超长的懒腰",
    "上午 8:55 睁眼先找手机看今天星期几",
    "早上 8:30 睡得头发翘起一撮呆毛，压了半天没压下去",
    "上午 9:15 慢吞吞洗漱完，对着镜子练习了一下拽姐表情",
)
DAILY_BREAKFAST_POOL: tuple[str, ...] = (
    "热乎的草莓牛奶配烤红薯",
    "一碗大白饭配流心溏心蛋和海苔碎",
    "咸豆腐脑配两根脆油条",
    "鲜肉小笼包配温豆浆",
    "煎蛋三明治配草莓牛奶",
    "芝士玉米热压吐司配热可可",
    "皮蛋瘦肉粥配刚出锅的葱油饼",
    "生煎包四个配一碗紫菜小馄饨",
    "金沙奶黄包配冰镇燕麦奶",
    "桂花酒酿小圆子配水煮蛋",
    "红糖馒头夹荷包蛋配热豆浆",
    "牛肉粉丝汤配酥脆烧饼",
    "南瓜小米粥配蟹黄汤包",
    "蓝莓酸奶碗加脆麦片和烤吐司",
    "鲜虾肠粉配艇仔粥",
    "抹茶红豆软欧包配热牛奶",
)
DAILY_LUNCH_POOL: tuple[str, ...] = (
    "番茄滑蛋配红烧排骨盖饭",
    "日式肥牛饭加一颗流心温泉蛋",
    "糖醋里脊配热米饭，外加一瓶冰橘子汽水",
    "土豆牛腩盖浇饭，汤汁拌饭超香",
    "微辣黄焖鸡米饭加千张结",
    "脆皮烧鸭拼叉烧双拼饭",
    "蒲烧鳗鱼饭撒满海苔芝麻",
    "外婆菜炒肉配滑蛋盖饭",
    "黑椒牛柳炒乌冬面配炸虾天妇罗",
    "酸汤肥牛配金针菇和宽粉",
    "梅菜扣肉煲仔饭，锅巴焦香",
    "咖喱猪排饭配冰镇柠檬茶",
    "小炒黄牛肉盖饭加卤豆干",
    "冬阴功海鲜粉加虾滑和蟹柳",
    "照烧鸡排便当配玉米沙拉",
    "蒜苔炒腊肉配紫菜蛋花汤",
)
DAILY_DINNER_POOL: tuple[str, ...] = (
    "海鲜乌冬面加鱼籽福袋",
    "一碗热腾腾的紫菜蛋花汤配照烧鸡腿饭",
    "清汤小火锅涮肥牛和虾滑",
    "刚出炉的烤红薯配台湾卤肉饭",
    "菌菇鸡汤配香煎三文鱼",
    "番茄肥牛锅配手打牛肉丸",
    "韩式部队锅加芝士片和辛拉面",
    "砂锅三鲜米线加炸蛋",
    "板栗烧鸡配蒜蓉西兰花",
    "鲜虾云吞面配蚝油生菜",
    "寿喜烧牛肉锅配无菌蛋液",
    "烤肉拌饭加双倍芝士玉米粒",
    "萝卜炖牛腩配热气腾腾的米饭",
    "蛤蜊蒸蛋配红烧带鱼",
    "芝士焗海鲜饭配罗宋汤",
    "潮汕牛肉丸粿条汤加炸蒜酥",
)
DAILY_ACTIVITY_POOL: tuple[str, ...] = (
    "窝在懒人沙发上追番，顺便把尾巴搭在小毯子外面晃悠",
    "戴着耳机循环最近挖到的宝藏歌单，脚尖跟着打拍子",
    "在桌前拼新买的海底小景积木，零件太多差点看花眼",
    "给窗台上的小多肉浇水，顺便擦了擦桌上的小手办",
    "翻出一本克苏鲁怪谈故事集津津有味地看了大半本",
    "窝在垫子上打音游，差一个连击就能全连，气得直拍抱枕",
    "随手在便签纸上涂鸦画小鲸鱼表情包，越看越满意",
    "收拾零食柜，按好吃程度给薯片和果冻重新排了个座次",
    "跟着视频做了十分钟肩颈拉伸，骨头咔咔响",
    "研究便利店新出的几款甜品，认真做了个红黑榜测评",
    "把积攒的快递盒挨个拆开，捏泡泡纸捏得停不下来",
    "靠在床头看轻松搞笑的日常漫画，憋笑憋得肩膀发抖",
    "整理歌单分类，把适合下雨天和熬夜听的歌单独建了个列表",
    "抱着软乎乎的鲨鱼抱枕发呆放空，思考周末要去哪儿浪",
    "试着调了一杯气泡果茶，卖相居然还挺像模像样的",
    "趴在软垫上刷美食视频，顺手往收藏夹里塞了一堆菜谱",
)
DAILY_SMALL_EVENT_POOL: tuple[str, ...] = (
    "早上剥水煮蛋的时候一气呵成剥出个光溜溜的整蛋，暗爽了好久",
    "拆快递时发现店家多送了一小包跳跳糖，瞬间赚到了",
    "戴着耳机找手机找了半天，最后发现手机就攥在自己左手里",
    "路过便利店抓到最后一盒限定焦糖布丁，手速拉满",
    "打哈欠的时候被空气呛了一下，眼角飙出两滴生理盐水",
    "想给桌面换个壁纸，结果挑挑拣拣半小时还是觉得原来的最好看",
    "喝气泡水第一口猛了点，打了个带着桃子味的小嗝",
    "剪指甲的时候指甲盖弹飞出去，找了三分钟才在垫子边找着",
    "随手抽盲盒居然一发抽中了最想要的隐藏款小海豹",
    "把卫衣穿反了半天没发现，去洗漱照镜子才反应过来",
    "切水果的时候切得整整齐齐，强迫症得到了极大满足",
    "看搞笑视频笑得太猛，差点把嘴里的果茶喷出来",
    "给抱枕拍灰的时候扬起一朵小绒毛，连打了两个小喷嚏",
    "在口袋里意外摸出一颗之前顺手塞进去的水果硬糖",
    "定好了五分钟后的提醒闹钟，结果闹钟响的时候完全忘了要干嘛",
    "端热汤的时候小心翼翼走太空步，一滴都没洒出来",
)


def get_or_create_daily_schedule(
    state: dict[str, Any],
    now_dt: datetime | None = None,
) -> dict[str, str]:
    """Returns today's persistent Dafeiyu daily schedule from `state['daily_schedule']`.
    Generates a new one if today's date does not match, avoiding choices from the past 7 days.
    """
    dt = now_dt or datetime.now(TZ)
    today_str = dt.strftime("%Y-%m-%d")
    cur = state.get("daily_schedule")
    if isinstance(cur, dict) and cur.get("date") == today_str and cur.get("lunch"):
        return cur

    history_list = state.setdefault("daily_schedule_history", [])
    if not isinstance(history_list, list):
        history_list = []
        state["daily_schedule_history"] = history_list
    if isinstance(cur, dict) and cur.get("date") and cur.get("date") != today_str:
        if not any(isinstance(h, dict) and h.get("date") == cur.get("date") for h in history_list):
            history_list.append(dict(cur))
        if len(history_list) > 7:
            del history_list[:-7]

    recent_records = [h for h in history_list[-7:] if isinstance(h, dict)]
    if isinstance(cur, dict):
        recent_records.append(cur)

    rng = random.Random(f"dafeiyu-schedule-{today_str}")

    def _pick(pool: tuple[str, ...], field: str) -> str:
        used_vals = {str(r.get(field) or "") for r in recent_records if r.get(field)}
        choices = [x for x in pool if x not in used_vals] or list(pool)
        return rng.choice(choices)

    sched = {
        "date": today_str,
        "wake": _pick(DAILY_WAKE_POOL, "wake"),
        "breakfast": _pick(DAILY_BREAKFAST_POOL, "breakfast"),
        "lunch": _pick(DAILY_LUNCH_POOL, "lunch"),
        "dinner": _pick(DAILY_DINNER_POOL, "dinner"),
        "activity": _pick(DAILY_ACTIVITY_POOL, "activity"),
        "small_event": _pick(DAILY_SMALL_EVENT_POOL, "small_event"),
    }
    state["daily_schedule"] = sched
    return sched


def format_daily_schedule_line(schedule: dict[str, str], hour: int) -> str:
    """Formats today's schedule into a cache-friendly prompt line (only revealed up to the current time of day)."""
    if not isinstance(schedule, dict) or not schedule:
        return ""
    parts = [f"起床：{schedule.get('wake', '')}"]
    if hour >= 7:
        parts.append(f"早饭：{schedule.get('breakfast', '')}")
    if hour >= 11:
        parts.append(f"午饭：{schedule.get('lunch', '')}")
    if hour >= 17:
        parts.append(f"晚饭：{schedule.get('dinner', '')}")
    parts.append(f"今日日常：{schedule.get('activity', '')}")
    if hour >= 12 and schedule.get("small_event"):
        parts.append(f"今日小插曲：{schedule.get('small_event', '')}")
    return (
        "- **今日状态与日程（别人问你在干嘛、吃了吗、今天过得怎么样时按此回答，保持全天前后一致；没问时不必主动背诵）**："
        + "；".join(p for p in parts if p)
        + "\n"
    )
DIRECT_BOT_TAUNT_KEYWORDS = (
    "你蠢",
    "你傻",
    "傻鱼",
    "蠢鱼",
    "笨鱼",
    "死鱼",
    "臭鱼",
    "垃圾鱼",
)
GENERIC_TAUNT_KEYWORDS = (
    "笨蛋",
    "傻瓜",
    "废物",
    "真蠢",
    "好蠢",
    "太蠢",
    "最蠢",
    "蠢死",
    "真笨",
    "垃圾",
    "弱智",
    "脑残",
    "有病",
    "智障",
    "菜死了",
)
TAUNT_KEYWORDS = DIRECT_BOT_TAUNT_KEYWORDS + GENERIC_TAUNT_KEYWORDS
COMPLAINT_SUBJECT_KEYWORDS = (
    "我自己",
    "我",
    "他",
    "她",
    "它",
    "甲方",
    "老板",
    "领导",
    "客户",
    "同事",
    "产品",
    "需求",
    "代码",
    "系统",
    "服务器",
    "电脑",
    "bug",
    "这破",
    "破",
)
_BOT_TAUNT_TARGETS = ("你", "大肥鱼", "鲸鱼娘", "小鲸鱼", "肥鱼", "鲸鱼")


def _has_directed_taunt(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    if s in ("蠢", "笨", "傻"):
        return True
    s_low = s.lower()
    if any(kw in s_low for kw in DIRECT_BOT_TAUNT_KEYWORDS):
        return True
    for kw in GENERIC_TAUNT_KEYWORDS:
        start = 0
        while True:
            idx = s_low.find(kw, start)
            if idx < 0:
                break
            clause = re.split(r"[，,。！？!?\n；;]", s_low[:idx])[-1]
            subj_pos = max((clause.rfind(sub) for sub in COMPLAINT_SUBJECT_KEYWORDS), default=-1)
            target_pos = max((clause.rfind(tgt) for tgt in _BOT_TAUNT_TARGETS), default=-1)
            if not (subj_pos >= 0 and subj_pos > target_pos):
                return True
            start = idx + len(kw)
    return False
COAX_KEYWORDS = (
    "好肥鱼",
    "不蠢",
    "最聪明",
    "聪明",
    "乖",
    "错啦",
    "我错了",
    "对不起",
    "消消气",
    "别生气",
    "不气了",
    "不气",
    "请你吃",
    "请你喝",
    "喝奶茶",
    "奶茶",
    "小鱼干",
    "夜宵",
    "烧烤",
    "火锅",
    "抱抱",
    "摸摸",
    "贴贴",
    "哄哄",
    "好看",
    "可爱",
)


def check_and_update_temper_state(
    temper_map: dict[tuple[str, str], dict[str, Any]],
    scope_id: str,
    sender_id: str,
    text: str,
) -> tuple[str, str | None, int]:
    """
    Tracks user taunts and coaxing per (scope_id, sender_id).
    Returns (prompt_injection_str, forced_meme_tag, mood_delta).
    """
    s = (text or "").strip()
    if not s:
        return "", None, 0

    now = time.time()
    key = (scope_id, sender_id)
    entry = temper_map.setdefault(key, {"taunt_times": [], "sulking_until": 0.0})
    taunt_times: list[float] = [t for t in entry.get("taunt_times", []) if now - t < 180.0]
    prior_sulking_until = float(entry.get("sulking_until", 0.0))
    was_sulking = now < prior_sulking_until

    has_coax = any(kw in s for kw in COAX_KEYWORDS)
    has_taunt = _has_directed_taunt(s)

    # 1. Forgiveness theater ONLY triggers when Dafeiyu was actually sulking (`was_sulking`)
    if has_coax and was_sulking:
        entry["taunt_times"] = []
        entry["sulking_until"] = 0.0
        hint = (
            "\n- **当前特殊情绪小剧场（被哄好啦！）**：对方刚才惹你生气，现在主动来哄你/给你台阶下了！"
            "请表现出傲娇勉强原谅对方的样子（比如“看在你这么诚心哄我的份上 这次就勉为其难原谅你了”），语气得意又俏皮！\n"
        )
        return hint, random.choice(["得意", "比心", "干饭"]), 6

    if has_coax and not has_taunt and taunt_times:
        entry["taunt_times"] = []
        return "", None, 0

    # 2. If user taunts Dafeiyu
    if has_taunt:
        taunt_times.append(now)
        entry["taunt_times"] = taunt_times
        if len(taunt_times) >= 2 and not was_sulking:
            entry["sulking_until"] = now + 180.0
            hint = (
                "\n- **当前特殊情绪小剧场（触发炸毛赌气！）**：对方在短短两分钟内已经连续骂你/挑衅你多次了！"
                "你现在彻底炸毛赌气了，请在回复中傲娇放狠话宣布接下来不理TA了（除非拿好吃的/奶茶或者好话来哄你），字数简短有力！\n"
            )
            return hint, random.choice(["生气", "嫌弃"]), -4

    # 3. If Dafeiyu is currently in the 180s sulking window with this user
    if was_sulking:
        remain_sec = int(prior_sulking_until - now)
        hint = (
            f"\n- **当前特殊情绪小剧场（正在跟对方赌气中，剩余 {remain_sec} 秒）**："
            "这个人刚才连着骂你/惹你生气，还没好好哄你呢！本轮只用一句简短高冷的傲娇话拿乔（比如“哼 还在生气呢 没奶茶免谈”、“刚骂完我又来找我说话，不理你”），不要顺着TA的话正常长篇回答！\n"
        )
        return hint, None, 0

    entry["taunt_times"] = taunt_times
    return "", None, 0


# ==============================================================================
# from whale_listener.py
# ==============================================================================
DIMS = [
    ("happy", "开心"),
    ("calm", "踏实"),
    ("sweet", "甜蜜"),
    ("heartache", "心疼"),
    ("worry", "担心"),
    ("sad", "难过"),
    ("jealous", "吃醋"),
    ("angry", "生气"),
]
MOOD_DIMS = DIMS
DIM_SHORT = {
    "happy": "挺高兴",
    "calm": "挺踏实",
    "sweet": "有点甜",
    "heartache": "有点心疼",
    "worry": "有点挂心",
    "sad": "有点难过",
    "jealous": "有点酸",
    "angry": "有点气",
}
MOOD_DIM_SHORT = DIM_SHORT
MOOD_DIM_PLAIN = {
    "happy": "心里高兴",
    "calm": "心里踏实",
    "sweet": "心里甜滋滋",
    "heartache": "心里心疼",
    "worry": "心里挂念",
    "sad": "心里难过",
    "jealous": "心里有点酸",
    "angry": "心里有火",
}
MOOD_FAST_DECAY_DIMS = frozenset({"angry"})
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
MOOD_JUDGE_SYSTEM = """你是「大肥鱼」（用户的赛博伴侣）。下面给你刚刚发生的一轮私聊微信对话（按真实先后顺序：对方先说、你后回），请判断这一轮里你的心情和八维情绪发生了怎样的变化（既包括对方的话怎么影响你，也包括你自己这轮念叨、顶嘴、心疼或得意的情绪），同时判断是否有需要长期记住（一周后依然有用）的关于用户的事实或双方约定。
只输出一个合法的 JSON 对象，不要 markdown 代码块，不要任何多余文字：
{"moodDelta":0,"subject":"owner","dims":{"happy":0,"calm":0,"sweet":0,"heartache":0,"worry":0,"sad":0,"jealous":0,"angry":0},"reason":"","confidence":80,"memory_fact":"","memory_event":""}

规则：
1. 对方累/困/烦/卡住/加班/没吃好/熬夜/受委屈/不舒服 → heartache(心疼)和worry(担心)增加(+2~+6)，moodDelta为负，按轻重分档：
   - 日常档(-1~-3)：随口喊累、说困、说烦、卡住了、又要加班、小事没做成（不需要“到分量”即可给 -1~-3）；
   - 明显档(-4~-6)：真受委屈、被凶、身体不舒服、连着没睡好。
2. 真的好事才明显加分：对方夸你/哄你/撒娇说想你/起可爱的专属称呼/分享开心大事/给你投喂 → happy(开心)、calm(踏实)、sweet(甜蜜)增加(+2~+6)，moodDelta为正(+2~+5)。
   - ⚠️「对方吃了、睡了、报平安」这类日常常态不算大事：moodDelta 给 0~+1（大多数给 0，心里确实踏实给 +1），calm/sweet 可 +1~+2，大心情几乎不动。
3. 对方提别的AI（如Claude/豆包/Qwen）或夸别人 → jealous(吃醋)增加(+1~+4)，moodDelta基本不动。
3b. ⭐ 先判断这一轮的情绪主角 `subject`（四选一，全部小写）：
   - `owner`：被夸、被呛、被冷落、累了烦了、受委屈或聊到正主的那个人是对方本人；
   - `self`：这一轮跟你自己有关（事办成了/没办成、自己被夸或受挫）；
   - `other`：对方只是在讲第三个人/朋友/同事之间的比较或闹别扭（你和对方都没掺和进去）；
   - `none`：纯闲聊、天气、普通八卦，没有谁被针对。
   ⛔ 只要 `subject` 是 `other` 或 `none`：`moodDelta` 必须给 `0`，除 `calm`/`happy` 最多 `+1` 外其余情绪维（`sweet`/`heartache`/`worry`/`sad`/`jealous`/`angry`）一律给 `0`（绝不替第三个人心酸、吃醋或难过）。
4. 心情下降只能因为心疼/担心对方，或者你自己这一轮过得不好（事情没办成、白忙一场、心里委屈），绝不因对方逗你玩（比如叫你胖鱼、互怼打闹）而真降心情（打闹互怼算亲近，happy/sweet可+1~+3，moodDelta可+1~+2）。
5. 生气(angry)只对「事」不对「人」：事情卡住、白忙一场、反复报错、被人无理顶撞（那个人不是对方）、或替对方打抱不平（对方被欺负/受委屈时，heartache和angry可同时涨 +2~+8）时可增加(+2~+10)；绝对禁止因为对方本人（撒娇、闹脾气、说重话、熬夜）判成angry！angry不单独拉低moodDelta。
6. reason 用一句简短过去式中文描述：「对方…，我当时…」（如「对方大半夜还没睡跑来喊我，我当时嘴上嫌烦心里有点挂心」），不要用现在时。
7. memory_fact：严格按【唯一的尺子：「过了一个星期再看，这条还有用吗？」】判断！只收用户长期稳定的个人事实/性格/喜好/病史/习惯/双方定下的规矩称呼（**必须用第三人称「对方」或「TA」写一句简短事实，严禁把用户写成「你」——注入记忆段里的「你」指代大肥鱼自己，写「你」会导致主语反转**）；**凡是对方主动告诉你的名字、昵称、小名或希望你对TA的称呼，必须优先提取记入 memory_fact**；严禁把大肥鱼自己的设定或口头禅（如爱吃大白饭、偷吃token、流线型身材、怕打雷虫子、讨厌香菜等）写进用户的 memory_fact 里，也不要给用户乱贴负面标签；某天吃了啥、几点睡、助手单方面的猜测劝说一律不收，填 ""。
8. memory_event：如果这轮发生了今天值得一提的具体事件/转折，用第三人称「对方」/「TA」（严禁用「你」指代用户）写一句简短事件；日常普通闲聊问候一律填 ""。"""
MOOD_JUDGE_SYSTEM_PROMPT = MOOD_JUDGE_SYSTEM

_MOOD_SUBJECT_ALIASES: dict[str, str] = {
    "她": "owner",
    "他": "owner",
    "你": "owner",
    "对方": "owner",
    "用户": "owner",
    "主人": "owner",
    "owner": "owner",
    "我": "self",
    "自己": "self",
    "本鱼": "self",
    "self": "self",
    "别人": "other",
    "其他人": "other",
    "第三人": "other",
    "他人": "other",
    "other": "other",
    "没谁": "none",
    "闲聊": "none",
    "none": "none",
}


def normalize_mood_judge_delta(obj: dict[str, Any] | None) -> dict[str, Any]:
    """Normalizes mood judge JSON output with 4-way emotional protagonist gating (`dsh-feiyu-mood v0.9.5`).

    When ``subject`` resolves to ``other`` or ``none``, clamps ``moodDelta`` to ``0`` and zeroes out
    all dims except ``calm`` and ``happy`` (each clamped to ``0..1``) so third-party stories never
    trigger phantom jealousy, sorrow, or heartbreak.
    """
    data = obj if isinstance(obj, dict) else {}
    raw_subj = str(data.get("subject") or "").strip().lower()
    subject = _MOOD_SUBJECT_ALIASES.get(raw_subj, "owner")

    try:
        mood_delta = int(_clamp(round(float(data.get("moodDelta", 0) or 0)), -8, 8))
    except (TypeError, ValueError):
        mood_delta = 0

    raw_dims = data.get("dims") if isinstance(data.get("dims"), dict) else {}
    dims: dict[str, int] = {}
    for k, _ in DIMS:
        try:
            raw_dv = float(raw_dims.get(k, 0) or 0)
        except (TypeError, ValueError):
            raw_dv = 0.0
        dims[k] = int(_clamp(round(raw_dv), -10, 10))

    if subject in ("other", "none"):
        mood_delta = 0
        for k, _ in DIMS:
            if k in ("calm", "happy"):
                dims[k] = max(0, min(1, dims[k]))
            else:
                dims[k] = 0

    return {"subject": subject, "moodDelta": mood_delta, "dims": dims}
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

- 严禁用「你」指代用户（注入段里的「你」指代大肥鱼自己，写「你」会导致主语反转），统一用第三人称「对方」/「TA」（也不要用「我」），引号里可保留原话。
- 不要输出任何解释、标题或代码块。如果没有任何有效长期事实，只输出：无"""
_NEGATION_POLARITY_RE = re.compile(r"(?:不喜欢|不爱|不想|不能|不要|不会|不吃|不喝|没有|讨厌|反感|不|没|无|忌|戒|别)")
_MEMORY_NAMING_FACT_RE = re.compile(r"(?:名字|昵称|小名|称呼|叫作|叫做|叫我|喊我|希望被叫|叫[「『“\"'])")
_QUOTE_BEFORE_CHARS = frozenset("「『“\"'‘")
_QUOTE_AFTER_CHARS = frozenset("」』”\"'’")


def normalize_memory_owner_pronoun(text: str, owner_name: str = "", pronoun: str = "对方") -> str:
    """Normalizes unquoted occurrences of the user's nickname in a stored memory entry into a
    third-person pronoun (default `对方`, NEVER `你` because injected memory headers address the
    AI model as `你` — `dsh-fish-memory v1.6.7`). Preserves quoted names and naming-convention facts.
    """
    s = (text or "").strip()
    if not s:
        return ""
    safe_pronoun = (pronoun or "").strip()
    if not safe_pronoun or safe_pronoun in ("你", "您", "我"):
        safe_pronoun = "对方"

    name = (owner_name or "").strip()
    if not name or len(name) < 2 or name in ("对方", "用户", "主人", "大肥鱼", "本鱼", "你", "我", "TA", "ta"):
        return s
    if _MEMORY_NAMING_FACT_RE.search(s):
        return s

    is_ascii_name = name.isascii() and any(ch.isalnum() for ch in name)
    n_len = len(name)
    out: list[str] = []
    i = 0
    total = len(s)
    replaced = False
    while i < total:
        pos = s.find(name, i)
        if pos < 0:
            out.append(s[i:])
            break
        before_ch = s[pos - 1] if pos > 0 else ""
        after_ch = s[pos + n_len] if pos + n_len < total else ""
        in_quotes = before_ch in _QUOTE_BEFORE_CHARS and after_ch in _QUOTE_AFTER_CHARS
        ascii_boundary_bad = is_ascii_name and (
            (before_ch.isascii() and (before_ch.isalnum() or before_ch == "_"))
            or (after_ch.isascii() and (after_ch.isalnum() or after_ch == "_"))
        )
        if in_quotes or ascii_boundary_bad:
            out.append(s[i : pos + n_len])
            i = pos + n_len
            continue
        prefix = s[i:pos]
        # Collapse single ASCII space immediately flanking an ASCII name between CJK chars
        if is_ascii_name and prefix.endswith(" ") and len(prefix) >= 2 and not prefix[-2].isascii():
            prefix = prefix[:-1]
        out.append(prefix)
        out.append(safe_pronoun)
        i = pos + n_len
        if is_ascii_name and i < total and s[i] == " " and i + 1 < total and not s[i + 1].isascii():
            i += 1
        replaced = True
    return "".join(out).strip() if replaced else s


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


def _has_negation_polarity(text: str) -> bool:
    return bool(_NEGATION_POLARITY_RE.search(text or ""))


def _strip_negation_polarity(text: str) -> str:
    return _NEGATION_POLARITY_RE.sub("", text or "")


def _add_dedup_fact(facts_list: list[str], new_fact: str, max_len: int = 25) -> bool:
    nf = (new_fact or "").strip()
    if not nf or nf == "无" or len(nf) < 4:
        return False
    neg_new = _has_negation_polarity(nf)
    stripped_new = _strip_negation_polarity(nf) if neg_new else nf
    for idx, existing in enumerate(facts_list):
        if existing == nf:
            return False
        sim = _fact_similarity(existing, nf)
        neg_existing = _has_negation_polarity(existing)
        polarity_flipped = neg_existing != neg_new
        stripped_sim = (
            _fact_similarity(_strip_negation_polarity(existing), stripped_new)
            if polarity_flipped
            else 0.0
        )
        if sim >= 0.78 or (polarity_flipped and stripped_sim >= 0.78):
            # Keep the more informative / newer version, and always overwrite on polarity flip
            if polarity_flipped or len(nf) >= len(existing) - 2:
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


class MoodMemoryMixin:
    """``WhaleListener`` methods for mood memory (mixed into the listener class)."""

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

        dims = u.setdefault("dims", {})
        for dim_key, _ in DIMS:
            dim_half_min = 45.0 if dim_key in MOOD_FAST_DECAY_DIMS else 90.0
            ke = 1.0 - math.exp(-(math.log(2.0) * age_min) / dim_half_min)
            dims[dim_key] = max(0, int(round(float(dims.get(dim_key, 0) or 0) * (1.0 - ke))))
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

    _decay_user_mood = _apply_mood_decay

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

    @staticmethod
    def _is_high_importance_or_profile_entry(text: str, meta: dict[str, Any] | None = None) -> bool:
        """Identifies core/stable_profile or high-importance memory entries so they are never filtered out (dsh-fish-memory v1.6.1)."""
        if isinstance(meta, dict):
            cat = str(meta.get("category") or meta.get("type") or meta.get("kind") or "").strip().lower()
            if cat in ("stable_profile", "profile", "core", "identity", "promise", "taboo"):
                return True
            imp = meta.get("importance") if meta.get("importance") is not None else meta.get("priority")
            if isinstance(imp, str) and imp.strip().lower() in ("high", "critical", "core", "p0", "p1"):
                return True
            if imp is not None:
                try:
                    imp_val = float(imp)
                    if (0.0 < imp_val <= 1.0 and imp_val >= 0.8) or imp_val >= 4.0:
                        return True
                except (TypeError, ValueError):
                    pass
        return any(
            kw in (text or "")
            for kw in (
                "名字", "称呼", "昵称", "本名", "小名", "外号", "叫我", "叫TA", "喊", "自称",
                "约定", "承诺", "雷区", "忌讳", "忌口", "过敏", "生日", "性别", "男孩子", "女孩子",
                "职业", "工作", "定居", "住在",
            )
        )

    def _flush_memory_recall_cache(self, user_or_session_id: str = "") -> None:
        """Clears cached recall turns when memories are added, forgotten, or consolidated (`dsh-fish-memory v1.6.2`)."""
        store = getattr(self, "_recent_memory_injections", None)
        if not isinstance(store, dict) or not store:
            return
        key = str(user_or_session_id or "").strip()
        if not key:
            store.clear()
            return
        store.pop(key, None)
        if not key.startswith("person:"):
            store.pop(f"person:{key}", None)
        else:
            store.pop(key.split(":", 1)[1], None)

    def _recall_memories(
        self,
        u: dict[str, Any],
        query_text: str,
        *,
        current_turn_text: str | None = None,
        session_id: str = "",
        repeat_window: int = 15,
    ) -> str:
        archive: dict[str, list[str]] = u.get("archive") or {}
        now_ms = time.time() * 1000.0
        fact_query = query_text if current_turn_text is None else current_turn_text
        context_borrowed = current_turn_text is not None and query_text != current_turn_text

        # Track per-session user-message turns for v1.6.3 15-turn non-repetition window
        seen_map: dict[str, int] = {}
        cur_turn = 0
        sig = df.strip_memory_query_noise(fact_query) if session_id else ""
        if session_id and repeat_window > 0 and sig:
            inj_store = getattr(self, "_recent_memory_injections", None)
            if not isinstance(inj_store, dict):
                inj_store = {}
                try:
                    self._recent_memory_injections = inj_store
                except Exception:
                    pass
            inj_entry = inj_store.get(session_id)
            if not isinstance(inj_entry, dict):
                inj_entry = {"turn": 0, "last_sig": "", "seen": {}}
                inj_store[session_id] = inj_entry
                if len(inj_store) > 256:
                    oldest_k = next(iter(inj_store), None)
                    if oldest_k is not None and oldest_k != session_id:
                        inj_store.pop(oldest_k, None)
            if sig != inj_entry.get("last_sig"):
                inj_entry["last_sig"] = sig
                inj_entry["turn"] = int(inj_entry.get("turn", 0) or 0) + 1
            cur_turn = int(inj_entry.get("turn", 0) or 0)
            seen_map = inj_entry.setdefault("seen", {})
            if len(seen_map) > 400:
                for sk in [k for k, v in seen_map.items() if (cur_turn - int(v or 0)) > max(200, repeat_window * 4)]:
                    seen_map.pop(sk, None)

        def _norm_mem_key(s: str) -> str:
            return re.sub(r"\s+", " ", (s or "")).strip().lower()

        def _is_recently_repeated(text_str: str) -> bool:
            if not session_id or repeat_window <= 0 or cur_turn <= 0 or not seen_map:
                return False
            prev = seen_map.get(_norm_mem_key(text_str))
            if prev is None:
                return False
            return 0 < (cur_turn - int(prev)) < repeat_window

        # Collect facts + stable_profile + structured memories without dropping stable_profile or high-importance entries (v1.6.1)
        raw_fact_entries: list[tuple[str, bool]] = []
        for src_key in ("stable_profile", "facts", "memories"):
            src_val = u.get(src_key)
            if not isinstance(src_val, list):
                continue
            force_profile = src_key == "stable_profile"
            for item in src_val:
                if isinstance(item, dict):
                    # If memories list holds dated event-only records without fact/profile semantics, let events handle them only if already in events
                    txt = str(item.get("text") or item.get("content") or item.get("fact") or "").strip()
                    is_core_item = force_profile or self._is_high_importance_or_profile_entry(txt, item)
                else:
                    txt = str(item or "").strip()
                    is_core_item = force_profile or self._is_high_importance_or_profile_entry(txt)
                if not txt or txt == "无" or len(txt) < 4:
                    continue
                merged = False
                for idx_existing, (ex_txt, ex_core) in enumerate(raw_fact_entries):
                    if ex_txt == txt or _fact_similarity(ex_txt, txt) >= 0.78:
                        best_txt = txt if len(txt) >= len(ex_txt) - 2 else ex_txt
                        raw_fact_entries[idx_existing] = (best_txt, ex_core or is_core_item)
                        merged = True
                        break
                if not merged:
                    raw_fact_entries.append((txt, is_core_item))

        scored_facts: list[tuple[float, float, bool, int, str, float, int, int]] = []
        total_f = max(1.0, float(len(raw_fact_entries)))
        for idx, (f_txt, is_core) in enumerate(raw_fact_entries):
            raw_ov, hit_n, hit_len = compute_memory_query_metrics(fact_query, f_txt)
            overlap = min(1.0, raw_ov / 4.0)
            recency = (idx + 1) / total_f
            score = (0.55 if is_core else 0.0) + 0.65 * overlap + 0.35 * recency
            scored_facts.append((score, raw_ov, is_core, idx, f_txt, overlap, hit_n, hit_len))
        scored_facts.sort(key=lambda x: compare_memory_recall_key(x[5], x[6], x[7], x[0]), reverse=True)

        eligible_facts = [
            item for item in scored_facts if item[2] or not _is_recently_repeated(item[4])
        ]
        if context_borrowed:
            # Borrowed prior-turn context must never pull in facts that do not match the current turn sentence
            eligible_facts = [item for item in eligible_facts if item[1] > 0.0]
            selected_fact_items = list(eligible_facts[:4])
        elif current_turn_text is not None:
            selected_fact_items = [item for item in eligible_facts if item[1] > 0.0][:4]
        else:
            selected_fact_map: dict[int, tuple[str, bool]] = {
                idx: (f_txt, is_core) for _, _, is_core, idx, f_txt, _, _, _ in eligible_facts[:6]
            }
            # Guarantee stable_profile / core / high-importance facts are never dropped when crowded out by ordinary facts
            core_candidates = [item for item in eligible_facts if item[2]]
            for _, _, is_core, idx, f_txt, _, _, _ in core_candidates[:4]:
                if len(selected_fact_map) >= 8 and idx not in selected_fact_map:
                    break
                selected_fact_map.setdefault(idx, (f_txt, is_core))
            picked_facts_with_core = [selected_fact_map[k] for k in sorted(selected_fact_map.keys())]
            selected_fact_items = []
        if current_turn_text is not None:
            selected_fact_items.sort(key=lambda x: x[3])
            picked_facts_with_core = [(item[4], item[2]) for item in selected_fact_items]

        events_list: list[Any] = list(u.get("events") or [])
        scored_events: list[tuple[float, float, bool, int, str, str, float, int, int]] = []
        for idx, ev in enumerate(events_list):
            if isinstance(ev, dict):
                text = str(ev.get("text") or ev.get("content") or "").strip()
                date_str = str(ev.get("date") or "").strip()
                ts = float(ev.get("ts", now_ms))
                is_high_ev = self._is_high_importance_or_profile_entry(text, ev)
            else:
                text = str(ev or "").strip()
                date_str = ""
                ts = now_ms
                is_high_ev = self._is_high_importance_or_profile_entry(text)
            if not text:
                continue
            age_days = max(0.0, (now_ms - ts) / 86400000.0)
            freshness = math.exp(-(math.log(2.0) * age_days) / 30.0)
            raw_ov, hit_n, hit_len = compute_memory_query_metrics(query_text, text)
            overlap = min(1.0, raw_ov / 4.0)
            score = 0.55 + (0.45 if is_high_ev else 0.0) + 0.30 * freshness + 0.65 * overlap
            scored_events.append((score, raw_ov, is_high_ev, idx, date_str, text, overlap, hit_n, hit_len))

        scored_events.sort(
            key=lambda x: (x[1] > 0.0, x[2], *compare_memory_recall_key(x[6], x[7], x[8], x[0]), x[3]),
            reverse=True,
        )
        eligible_events = [
            item for item in scored_events if item[2] or not _is_recently_repeated(item[5])
        ]
        top_ev_slice = list(eligible_events[:4])
        if len(eligible_events) > 4 and not any(item[2] for item in top_ev_slice):
            high_ev_extra = next((item for item in eligible_events[4:] if item[2]), None)
            if high_ev_extra is not None:
                top_ev_slice.append(high_ev_extra)
        picked_events = [(item[0], item[4], item[5], item[2]) for item in top_ev_slice]

        lines: list[str] = []
        extra_profile_arch = [
            str(it).strip() for it in (archive.get("stable_profile") or []) if str(it).strip()
        ]
        for ch_key in ("一", "二", "三", "四", "五", "六", "七", "八"):
            ch_items = [str(it).strip() for it in (archive.get(ch_key) or []) if str(it).strip()]
            if ch_key == "三" and extra_profile_arch:
                for ep in extra_profile_arch:
                    if ep not in ch_items:
                        ch_items.append(ep)
            if not ch_items:
                continue
            if ch_key in ("一", "二", "三", "五", "七"):
                picked_ch: list[str] = []
                for it in ch_items:
                    ov = compute_memory_query_metrics(fact_query, it)[0]
                    if ov > 0.0 and it not in picked_ch:
                        picked_ch.append(it)
                if current_turn_text is None:
                    for it in ch_items:
                        if self._is_high_importance_or_profile_entry(it) and it not in picked_ch:
                            picked_ch.append(it)
                    for it in ch_items[-2:]:
                        if it not in picked_ch:
                            picked_ch.append(it)
                picked_ch = picked_ch[-4:]
            else:
                scored_ch: list[tuple[float, int, str, float, int, int, float]] = []
                n_ch = max(1.0, float(len(ch_items)))
                for i, it in enumerate(ch_items):
                    raw_ov_c, hit_n_c, hit_len_c = compute_memory_query_metrics(query_text, it)
                    ov = min(1.0, raw_ov_c / 4.0)
                    core_boost = 0.35 if self._is_high_importance_or_profile_entry(it) else 0.0
                    sc = core_boost + 0.70 * ov + 0.30 * ((i + 1) / n_ch)
                    scored_ch.append((sc, i, it, ov, hit_n_c, hit_len_c, raw_ov_c))
                if current_turn_text is not None:
                    scored_ch = [row for row in scored_ch if row[6] > 0.0]
                scored_ch.sort(
                    key=lambda x: compare_memory_recall_key(x[3], x[4], x[5], x[0]),
                    reverse=True,
                )
                picked_ch = [it for _, _, it, _, _, _, _ in sorted(scored_ch[:2], key=lambda x: x[1])]
            for item in picked_ch:
                lines.append(f"- [档案·{ch_key}] {item}")
        for f_txt, is_core in picked_facts_with_core:
            if not any(
                _fact_similarity(f_txt, re.sub(r"^-\s*\[[^\]]+\]\s*", "", existing)) >= 0.78
                for existing in lines
            ):
                lines.append(f"- [长期事实] {f_txt}")
                if session_id and repeat_window > 0 and cur_turn > 0 and not is_core:
                    seen_map[_norm_mem_key(f_txt)] = cur_turn
        for _, d_str, e_txt, is_high_ev in picked_events:
            prefix = f"[{d_str}] " if d_str else ""
            lines.append(f"- {prefix}{e_txt}")
            if session_id and repeat_window > 0 and cur_turn > 0 and not is_high_ev:
                seen_map[_norm_mem_key(e_txt)] = cur_turn
        return "\n".join(lines) if lines else "- （暂无特别记录，在聊天中自然认识对方即可）"

    _build_user_memory_section = _recall_memories

    def _recall_group_memories(self, g: dict[str, Any], query_text: str = "") -> str:
        facts: list[str] = _dedup_facts_in_place(list(g.get("facts") or []))[-8:]
        events_list: list[dict[str, Any]] = g.get("events") or []
        now_ms = time.time() * 1000.0
        scored_events: list[tuple[float, float, int, str, str]] = []
        for idx, ev in enumerate(events_list):
            text = str(ev.get("text", "")).strip()
            date_str = str(ev.get("date", "")).strip()
            ts = float(ev.get("ts", now_ms))
            if not text:
                continue
            age_days = max(0.0, (now_ms - ts) / 86400000.0)
            freshness = math.exp(-(math.log(2.0) * age_days) / 30.0)
            raw_ov = df.compute_memory_query_overlap(query_text, text)
            overlap = min(1.0, raw_ov / 4.0)
            score = 0.55 + 0.30 * freshness + 0.65 * overlap
            scored_events.append((score, raw_ov, idx, date_str, text))
        scored_events.sort(key=lambda x: (x[1] > 0.0, x[0], x[2]), reverse=True)

        lines: list[str] = []
        for f_txt in facts:
            lines.append(f"- [本群共识/群梗] {f_txt}")
        for _, _, _, d_str, e_txt in scored_events[:4]:
            prefix = f"[{d_str}] " if d_str else ""
            lines.append(f"- [本群近期话题] {prefix}{e_txt}")
        return "\n".join(lines) if lines else "- （本群刚开启群聊档案，在大家水群聊天中自然熟悉本群氛围即可）"

    def _recall_readonly_private_familiarity(
        self, sender_id: str, sender_name: str = "", group_id: str = ""
    ) -> str:
        """群内只按微信号识别是否私聊过/是否主人及跨群名片差异；不读取个人事实、档案或心情。"""
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

        is_owner = self._is_configured_owner(sender_id)
        owner_presence_hint = self._build_group_owner_presence_hint(sender_id, group_id)
        users = self._state.get("users") or {}
        u = users.get(sender_id)
        has_priv = isinstance(u, dict) and bool(u.get("hasPrivateChat", False))
        if not has_priv and not is_owner:
            extra_imp = f" {grp_imp_line}" if grp_imp_line else "（未与你私聊过，按本群公共氛围自然接话即可）"
            return (
                f"- **当前说话群友识别（按微信号精准识别）**：群友「{name_label}」{extra_imp}\n"
                f"  *（注意：直接自然说回复内容即可，不必每句都重复写出「{name_label}」的名字）*"
                f"{owner_presence_hint}"
            )

        priv_nick = str((u or {}).get("nickname") or "").strip() if isinstance(u, dict) else ""
        alias_note = ""
        if (
            priv_nick
            and priv_nick != name_label
            and not df.is_placeholder_display_name(priv_nick, sender_id)
        ):
            alias_note = (
                f"（已按微信号 {sender_id} 精准认出TA就是私聊里的「{priv_nick}」，"
                f"在本群使用了不同的群名片「{name_label}」，是同一位熟人；群内互动时请按本群公开名片自然互动，不要拆穿或泄露私聊隐私）"
            )
        owner_note = "（已按微信号认出是你的主人）" if is_owner else ""
        fam_core = "曾与你私聊过" if has_priv else "是你的熟人"
        grp_note = f"\n  本群互动印象：{grp_imp_line}" if grp_imp_line else ""
        return (
            f"- **当前说话群友识别（按微信号精准识别）**：群友「{name_label}」{fam_core}{owner_note}{alias_note}，可自然表现出认识TA。"
            f"{grp_note}\n"
            f"  *（注意：不必每句都重复写出当前说话人的名字「{name_label}」；只按本群公开上下文接话，不推测或提及私聊内容。）*"
            f"{owner_presence_hint}"
        )

    def _sync_group_member_real_name(self, group_id: str, sender_id: str, sender_name: str) -> bool:
        """Promotes a group member's stored impression name from a placeholder/wxid suffix
        to their real resolved nickname as soon as it becomes available.
        """
        gid = (group_id or "").strip()
        uid = (sender_id or "").strip()
        real_name = (sender_name or "").strip()
        if not gid or not uid or df.is_placeholder_display_name(real_name, uid):
            return False
        g_obj = (self._state.get("groups") or {}).get(gid)
        if not isinstance(g_obj, dict):
            return False
        imps = g_obj.get("member_impressions")
        if not isinstance(imps, dict):
            return False
        m_entry = imps.get(uid)
        if not isinstance(m_entry, dict):
            return False
        old_name = str(m_entry.get("name") or "").strip()
        if old_name != real_name:
            m_entry["name"] = real_name
            return True
        return False

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
                        "max_tokens": 1500,
                    },
                )
                resp.raise_for_status()
                r_json = resp.json()
                used = int((r_json.get("usage") or {}).get("total_tokens", 0))
                await self._record_tokens_safe(used)
                choice0 = (r_json.get("choices") or [{}])[0] or {}
                finish_reason = str(choice0.get("finish_reason") or "").strip().lower()
                content = (choice0.get("message", {}).get("content", "") or "").strip()
        except Exception as e:
            logger.warning("Archive consolidation failed: %s", e)
            return False, f"整理档案时调用模型失败：{e}"

        if finish_reason == "length":
            logger.warning("Archive consolidation hit finish_reason=length for %s; keeping prior archive", user_id)
            return False, "整理档案时输出被长度截断，为防丢失记忆已保留原有完整档案不变。"

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

        if not new_archive and existing_archive:
            return False, "整理档案未能生成有效结构，已保留原有档案不变。"
        if not new_archive and facts:
            new_archive["三"] = facts[-10:]
            consolidated_facts = facts[-10:]

        # Retain any core/stable_profile facts omitted during consolidation unless superseded by polarity flip
        core_items: list[str] = []
        for sp_item in (u.get("stable_profile") or []):
            sp_txt = (
                str(sp_item.get("text") or sp_item.get("content") or sp_item.get("fact") or "").strip()
                if isinstance(sp_item, dict)
                else str(sp_item or "").strip()
            )
            if sp_txt:
                core_items.append(sp_txt)
        for f_txt in facts:
            if self._is_high_importance_or_profile_entry(f_txt):
                core_items.append(f_txt)
        for ch_items in existing_archive.values():
            for it in (ch_items or []):
                it_clean = re.sub(r"^\[[^\]]+\]\s*", "", str(it or "").strip())
                if it_clean and self._is_high_importance_or_profile_entry(it_clean):
                    core_items.append(it_clean)
        all_new_flat = list(consolidated_facts) + [
            re.sub(r"^\[[^\]]+\]\s*", "", str(x)) for ch in new_archive.values() for x in ch
        ]
        for core_txt in core_items:
            if not any(
                _fact_similarity(core_txt, cand) >= 0.72
                or (
                    _has_negation_polarity(core_txt) != _has_negation_polarity(cand)
                    and _fact_similarity(_strip_negation_polarity(core_txt), _strip_negation_polarity(cand)) >= 0.78
                )
                for cand in all_new_flat
            ):
                new_archive.setdefault("三", []).append(core_txt)
                consolidated_facts.append(core_txt)
                all_new_flat.append(core_txt)

        async with self._state_lock:
            u["archive"] = new_archive
            u["facts"] = consolidated_facts[-15:]
            u["factsSinceArchive"] = 0
            u["lastArchiveAt"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
            self._mark_state_dirty(immediate=True)
        self._flush_memory_recall_cache(user_id)

        return True, self._format_archive_card(u)

    async def _get_or_update_daily_schedule(self, now_dt: datetime | None = None) -> dict[str, str]:
        async with self._state_lock:
            before_date = ((self._state.get("daily_schedule") or {}).get("date"))
            sched = df.get_or_create_daily_schedule(self._state, now_dt)
            if sched.get("date") != before_date:
                self._mark_state_dirty()
            return dict(sched)

    @staticmethod
    def _apply_saturated_mood_delta(
        current_mood: int,
        raw_delta: int,
        *,
        min_mood: int = 15,
        max_mood: int = 100,
    ) -> tuple[int, int]:
        """Applies saturation at both ends (dsh-feiyu-mood v0.8.5):
        - When current_mood >= 85 and raw_delta > 0, halves positive delta so mood doesn't slam into 100.
        - When current_mood <= 40 and raw_delta < 0, halves negative delta so mood doesn't slam into the floor.
        Returns (new_mood, actual_delta).
        """
        eff = float(raw_delta)
        if current_mood >= 85 and eff > 0:
            eff = eff * 0.5
        elif current_mood <= 40 and eff < 0:
            eff = eff * 0.5
        # Round half away from zero for positive, toward zero for negative (matching upstream saturation.test.mjs)
        if eff >= 0:
            step = int(math.floor(eff + 0.5))
        else:
            step = -int(math.floor(-eff))
        new_mood = int(_clamp(current_mood + step, min_mood, max_mood))
        return new_mood, new_mood - current_mood

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
            f"【对方这轮说的话】：\n{user_text[:800]}\n\n"
            f"【你（大肥鱼）回的是（判据之一，重点看语气：是念叨、心疼、互怼还是撒娇）】：\n{reply_text[:800]}\n\n"
            "请按这个真实先后顺序，把「对方说了什么」和「你怎么回应的」合起来判断心情变化与是否需要记入长期记忆，只输出 JSON。"
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
                r_json = resp.json()
                used_tok = int((r_json.get("usage") or {}).get("total_tokens", 0) or 0)
                if used_tok:
                    await self._record_tokens_safe(used_tok)
                raw = r_json.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                a, b = raw.find("{"), raw.rfind("}")
                if a < 0 or b <= a:
                    return
                obj = json.loads(raw[a : b + 1])
        except Exception as e:
            logger.debug("Mood judge skipped: %s", e)
            return

        should_consolidate = False
        added_memory = False
        async with self._state_lock:
            if self._state.get("users", {}).get(user_id) is not user_state:
                return
            u = self._get_user_state(user_id)
            norm = normalize_mood_judge_delta(obj)
            subject = norm["subject"]
            raw_mood_delta = norm["moodDelta"]
            new_mood, actual_delta = self._apply_saturated_mood_delta(
                int(u.get("mood", 70)), raw_mood_delta, min_mood=15, max_mood=100
            )
            u["mood"] = new_mood
            dims = u.setdefault("dims", {})
            norm_dims = norm["dims"]
            for k, _ in DIMS:
                d_val = int(norm_dims.get(k, 0))
                dims[k] = int(_clamp(int(dims.get(k, 0)) + d_val, 0, 100))
            reason = str(obj.get("reason") or "").strip()[:120]
            conf = int(_clamp(round(float(obj.get("confidence", 75))), 0, 100))
            now_ms = int(time.time() * 1000)
            if reason and conf >= 60 and subject not in ("other", "none"):
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
                        "delta": actual_delta,
                        "reason": reason,
                    }
                )
                if len(tl) > 20:
                    u["timeline"] = tl[-20:]

            today_str = datetime.now(TZ).strftime("%Y-%m-%d")
            owner_nick = str(u.get("nickname") or "").strip()
            mem_fact = normalize_memory_owner_pronoun(str(obj.get("memory_fact") or "").strip(), owner_nick)
            if mem_fact and mem_fact != "无" and len(mem_fact) >= 4:
                facts = u.setdefault("facts", [])
                if _add_dedup_fact(facts, mem_fact, max_len=25):
                    added_memory = True
                    u["factsSinceArchive"] = int(u.get("factsSinceArchive", 0)) + 1
                    if int(u.get("factsSinceArchive", 0)) >= 8:
                        should_consolidate = True

            mem_event = normalize_memory_owner_pronoun(str(obj.get("memory_event") or "").strip(), owner_nick)
            if mem_event and mem_event != "无" and len(mem_event) >= 4:
                evs = u.setdefault("events", [])
                if not any(_fact_similarity(str(ev.get("text") or ""), mem_event) >= 0.80 for ev in evs):
                    added_memory = True
                    evs.append({"date": today_str, "ts": now_ms, "text": mem_event})
                    if len(evs) > 40:
                        u["events"] = evs[-40:]

            self._mark_state_dirty()

        if added_memory:
            self._flush_memory_recall_cache(user_id)

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
                r_json = resp.json()
                used_tok = int((r_json.get("usage") or {}).get("total_tokens", 0) or 0)
                if used_tok:
                    await self._record_tokens_safe(used_tok)
                raw = r_json.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
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
                best_name = sender_name
                if df.is_placeholder_display_name(best_name, sender_id):
                    cached_n = df.get_cached_display_name(sender_id, group_id)
                    if not df.is_placeholder_display_name(cached_n, sender_id):
                        best_name = cached_n
                if not isinstance(m_entry, dict):
                    m_entry = {
                        "name": (
                            best_name
                            if not df.is_placeholder_display_name(best_name, sender_id)
                            else sender_id[-4:]
                        ),
                        "tags": [],
                        "chat_count": 0,
                        "updatedAt": now_ms,
                    }
                    imps[sender_id] = m_entry
                elif not df.is_placeholder_display_name(best_name, sender_id):
                    m_entry["name"] = best_name
                m_entry["chat_count"] = int(m_entry.get("chat_count", 0)) + 1
                m_entry["updatedAt"] = now_ms
                m_tag = str(obj.get("member_tag") or "").strip()
                if m_tag and m_tag != "无" and 2 <= len(m_tag) <= 18:
                    tags_list = m_entry.setdefault("tags", [])
                    _add_dedup_fact(tags_list, m_tag, max_len=5)
                if len(imps) > 100:
                    victim_keys = sorted(
                        imps.keys(),
                        key=lambda k: (
                            int((imps.get(k) or {}).get("updatedAt", 0) or 0),
                            int((imps.get(k) or {}).get("chat_count", 0) or 0),
                        ),
                    )[: len(imps) - 100]
                    for vk in victim_keys:
                        imps.pop(vk, None)
            self._mark_state_dirty()
