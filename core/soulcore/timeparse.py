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
