"""把"明天下午三点答辩""周五面试""12月3号考试"这类说法解析成"适合跟进的时刻"。

主动聊天里的"跟进"念头靠它：事情发生之后找个合适的时候问一句"怎么样了"。

规则：
  - 有明确钟点（下午三点、晚上8点、15:30）：事件时刻 + 2 小时再问
  - 只有日期（明天、周五、12月3号）：当天 18:00 问
  - 只说"今天/今晚"且没钟点：今天 21:00 问
  - 解析不出将来的日期：返回 None（不是跟进类）

只依赖标准库，按本地时区计算。
"""
from __future__ import annotations

import re
import time

CN_NUM = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
          "十": 10, "日": 7, "天": 7}

# 会让人想"之后问一句"的事
EVENT_WORDS = ("考试", "答辩", "面试", "比赛", "汇报", "组会", "开会", "出差", "体检", "手术", "上线", "发布",
               "截止", "deadline", "ddl", "考研", "复试", "初试", "笔试", "演讲", "约会", "相亲", "看病",
               "复查", "搬家", "入职", "报到", "毕业", "答疑", "评审", "交论文", "交报告", "演出", "考核",
               "期末", "期中", "四六级", "考驾照", "科目")


def _cn_int(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + CN_NUM.get(s[1:], 0)
    if "十" in s:
        a, _, b = s.partition("十")
        return CN_NUM.get(a, 0) * 10 + (CN_NUM.get(b, 0) if b else 0)
    if len(s) == 1 and s in CN_NUM:
        return CN_NUM[s]
    return None


_NUM = r"[0-9零〇一二两三四五六七八九十]+"
_DAY_REL = re.compile(r"(大后天|后天|明天|明早|明晚|今天|今晚|今早|今儿)")
_WEEK = re.compile(r"(下下|下个?|这个?|本)?(?:周|星期|礼拜)([一二三四五六日天1-7])")
_MD = re.compile(rf"(?:({_NUM})月)?({_NUM})[号日]")
_CLOCK = re.compile(rf"(凌晨|早上|上午|中午|下午|傍晚|晚上|今晚|明晚)?\s*({_NUM})\s*(?:点|:|：)\s*(半|({_NUM})分?)?")


def _midnight(ts: float) -> float:
    t = time.localtime(ts)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _date_of(text: str, now: float) -> tuple[float | None, str]:
    """返回 (那一天 0 点的时间戳, 命中方式)"""
    today = _midnight(now)
    m = _DAY_REL.search(text)
    if m:
        w = m.group(1)
        off = {"大后天": 3, "后天": 2, "明天": 1, "明早": 1, "明晚": 1}.get(w, 0)
        return _shift_days(today, off), ("today" if off == 0 else "rel")
    m = _WEEK.search(text)
    if m:
        pre, d = m.group(1) or "", m.group(2)
        target = (int(d) if d.isdigit() else CN_NUM[d]) - 1          # 0=周一
        cur = time.localtime(now).tm_wday
        if pre.startswith("下下"):
            off = (7 - cur) + 7 + target
        elif pre.startswith("下"):
            off = (7 - cur) + target
        else:
            off = target - cur
            if off < 0 and not pre:                                   # "周一"已经过了 → 下周一
                off += 7
        if off < 0:
            return None, ""
        return _shift_days(today, off), "week"
    m = _MD.search(text)
    if m:
        mon = _cn_int(m.group(1)) if m.group(1) else None
        day = _cn_int(m.group(2))
        t = time.localtime(now)
        if not day or not (1 <= day <= 31):
            return None, ""
        y, mo = t.tm_year, mon or t.tm_mon
        if mon is None and day < t.tm_mday:                           # "3号"已过 → 下个月
            mo += 1
            if mo > 12:
                y, mo = y + 1, 1
        elif mon is not None and (mo, day) < (t.tm_mon, t.tm_mday):   # "1月3日"已过 → 明年
            y += 1
        try:
            ts = time.mktime((y, mo, day, 0, 0, 0, 0, 0, -1))
        except (OverflowError, ValueError):
            return None, ""
        if time.localtime(ts).tm_mday != day:                         # 2月30号之类
            return None, ""
        return ts, "md"
    return None, ""


def _shift_days(midnight: float, days: int) -> float:
    t = time.localtime(midnight)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + days, 0, 0, 0, 0, 0, -1))


def _clock(text: str) -> tuple[int, int] | None:
    for m in _CLOCK.finditer(text):
        h = _cn_int(m.group(2))
        if h is None or h > 24:
            continue
        mi = 30 if m.group(3) == "半" else (_cn_int(m.group(4)) if m.group(4) else 0) or 0
        part = m.group(1) or ""
        if part in ("下午", "傍晚", "晚上", "今晚", "明晚") and h < 12:
            h += 12
        elif part == "中午" and h < 6:
            h += 12
        return h % 24, min(59, mi)
    return None


def parse_followup(text: str, now: float) -> float | None:
    """解析出"适合跟进"的时刻；不是将来的事件就返回 None。"""
    text = text or ""
    if not any(w in text.lower() for w in EVENT_WORDS):
        return None
    if re.search(r"(昨天|前天|上周|上个?星期|刚才|已经|考完|结束了|过了)", text):
        return None                                                    # 已经发生的事，不是"之后再问"
    day, how = _date_of(text, now)
    if day is None:
        return None
    clk = _clock(text)
    if clk:
        h, mi = clk
        if any(p in text for p in ("今晚", "明晚")) and h < 12:
            h += 12
        due = day + h * 3600 + mi * 60 + 2 * 3600
    elif how == "today":
        due = day + 21 * 3600
    else:
        due = day + 18 * 3600
    if due <= now:
        return None
    return due


# ---------------- v0.8：从问句里解析"过去的"时间窗口 ----------------
# "问历史"：这些词一出现，说明对方问的是"以前那件事"，失效条目也该被召回（并标成已过时）
HISTORY_Q = re.compile(r"以前|之前|原来|曾经|过去|当初|一开始|最早|改之前|换之前|旧的|上一个|变过|变了")


def wants_history(text: str) -> bool:
    return bool(HISTORY_Q.search(text or ""))


_WD = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
       "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}
_DAYS_AGO = re.compile(rf"({_NUM})\s*天\s*(?:之前|以前|前)")
_LAST_WEEK_DAY = re.compile(r"(?:上|上个|上一个)(?:周|星期|礼拜)\s*([一二三四五六日天1-7])")
_A_MONTH_DAY = re.compile(rf"(?:({_NUM})\s*月)?\s*({_NUM})\s*[号日]")


def _monday(ts: float) -> float:
    """那一天所在周的周一 0 点"""
    return _shift_days(_midnight(ts), -time.localtime(ts).tm_wday)


def _month_start(ts: float, delta: int = 0) -> float:
    t = time.localtime(ts)
    y, mo = t.tm_year, t.tm_mon + delta
    y, mo = y + (mo - 1) // 12, (mo - 1) % 12 + 1
    return time.mktime((y, mo, 1, 0, 0, 0, 0, 0, -1))


def query_window(text: str, now: float) -> tuple[float, float, str] | None:
    """解析问句里的"过去的"时间窗口 → (起, 止, 去掉时间词后的问句)。

    只处理今天和过去；"明天/下周/下个月"这类将来的说法返回 None（那不是回忆，是计划）。
    """
    text = text or ""
    today = _midnight(now)

    def ret(start: float, end: float, m) -> tuple[float, float, str]:
        rest = (text[:m.start()] + " " + text[m.end():]).strip()
        return start, end, rest

    m = re.search(r"大前天", text)
    if m:
        d = _shift_days(today, -3)
        return ret(d, _shift_days(d, 1), m)
    m = re.search(r"前天", text)
    if m:
        d = _shift_days(today, -2)
        return ret(d, _shift_days(d, 1), m)
    m = re.search(r"昨天|昨晚|昨儿", text)
    if m:
        d = _shift_days(today, -1)
        return ret(d, today, m)
    m = re.search(r"今天|今早|今儿|今天早上", text)
    if m:
        return ret(today, now, m)
    m = _DAYS_AGO.search(text)
    if m:
        n = _cn_int(m.group(1))
        if n is not None and 0 < n <= 3650:
            d = _shift_days(today, -n)
            return ret(d, _shift_days(d, 1), m)
    m = _LAST_WEEK_DAY.search(text)
    if m:
        d = _shift_days(_monday(now), -7 + _WD[m.group(1)])
        return ret(d, _shift_days(d, 1), m)
    m = re.search(r"上个?(?:周|星期|礼拜)", text)
    if m:
        mon = _monday(now)
        return ret(_shift_days(mon, -7), mon, m)
    m = re.search(r"这个?(?:周|星期|礼拜)|本(?:周|星期|礼拜)", text)
    if m:
        return ret(_monday(now), now, m)
    m = re.search(r"上个?月", text)
    if m:
        return ret(_month_start(now, -1), _month_start(now), m)
    m = re.search(r"这个?月|本月", text)
    if m:
        return ret(_month_start(now), now, m)
    m = re.search(r"前几天|前些天", text)
    if m:
        return ret(now - 7 * 86400, _shift_days(today, -1), m)
    m = re.search(r"最近|这几天|这两天|这些天", text)
    if m:
        return ret(now - 7 * 86400, now, m)
    m = _A_MONTH_DAY.search(text)
    if m:
        mon = _cn_int(m.group(1)) if m.group(1) else None
        day = _cn_int(m.group(2))
        t = time.localtime(now)
        if day and 1 <= day <= 31:
            y, mo = t.tm_year, mon or t.tm_mon
            if mon is None:
                if day > t.tm_mday:                      # "20号"今天还没到 → 上个月那次
                    y, mo = (y, mo - 1) if mo > 1 else (y - 1, 12)
            elif (mo, day) > (t.tm_mon, t.tm_mday):      # "12月3号"今年还没到 → 去年那次
                y -= 1
            try:
                d = time.mktime((y, mo, day, 0, 0, 0, 0, 0, -1))
            except (OverflowError, ValueError):
                return None
            if time.localtime(d).tm_mday == day:
                return ret(d, _shift_days(d, 1), m)
    return None


def parse_due_string(s: str) -> float | None:
    """LLM 给的绝对时间 "YYYY-MM-DD HH:MM"（或只有日期）→ 时间戳。"""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            ts = time.mktime(time.strptime(s, fmt))
            return ts + 18 * 3600 if fmt == "%Y-%m-%d" else ts
        except ValueError:
            continue
    return None
