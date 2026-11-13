"""Explicit conversational controls; ordinary conversation remains with the model."""
from __future__ import annotations
import re

def parse_intent(text: str) -> dict | None:
    t = (text or "").strip().rstrip("。.!！")
    lead = r"(?:请|麻烦)?(?:你)?"
    if re.fullmatch(lead + r"(?:以后)?(?:别|不要|不用)(?:再)?主动(?:给我发消息|发消息给我|找我聊天|找我|联系我|发消息)(?:了)?", t):
        return {"action": "mute"}
    if re.fullmatch(lead + r"(?:恢复主动聊天|可以主动找我(?:聊天)?|以后可以主动找我(?:聊天)?|允许主动聊天)", t):
        return {"action": "unmute"}
    m = re.fullmatch(lead + r"(?:以后)?(?:别|不要)(?:再)?(?:问|提)(?:我)?(?:关于)?(.+?)(?:的事|这件事|了)?", t)
    if m:
        return {"action": "mute_topic", "topic": m.group(1).strip()}
    m = re.fullmatch(lead + r"(?:记住|记一下|别忘了)[，,:：\s]*(.+)", t)
    if m:
        return {"action": "remember", "content": m.group(1).strip()}
    m = re.fullmatch(lead + r"(?:忘掉|忘记|删除|删掉|别记|不要记|别再记|不要再记)(.+)", t)
    if not m:
        m = re.fullmatch(lead + r"(?:把|将)(.+?)(?:忘掉|忘记|删掉|删除)(?:吧|了)?", t)
    if not m:
        m = re.fullmatch(r"(.+?)(?:别记|不要记|别再记)(?:了)?", t)
    if m:
        topic = re.sub(r"^(?:关于|之前说的|上次说的)", "", m.group(1)).strip(" ，,:：")
        topic = re.sub(r"(?:的记忆|这条记忆|这件事|这条信息)$", "", topic).strip()
        return {"action": "forget", "topic": topic or "这件事"}
    m = re.fullmatch(r"(?:我的)?(.{2,30}?)(?:已经)?(?:取消了|已取消|结束了|完成了|考完了|不用再问了)", t)
    if m:
        return {"action": "complete", "topic": m.group(1).strip()}
    return None

def topic_matches(topic: str, content: str) -> bool:
    topic = re.sub(r"^(?:我的|我|关于)", "", (topic or "").strip()).strip()
    if not topic:
        return False
    c = (content or "").lower()
    if topic.lower() in c:
        return True
    aliases = {
        "住址": r"住址|地址|住在|居住|搬到|搬家",
        "地址": r"住址|地址|住在|居住|搬到|搬家",
        "生日": r"生日|出生|诞辰",
        "病情": r"病情|疾病|诊断|糖尿病|住院|患病",
        "过敏": r"过敏",
    }
    if topic in aliases:
        return bool(re.search(aliases[topic], c))
    from .textutil import tokens
    wanted = {t for t in tokens(topic) if len(t) > 1}
    found = set(tokens(content or ""))
    return len(wanted) >= 2 and len(wanted & found) / len(wanted) >= 0.7
