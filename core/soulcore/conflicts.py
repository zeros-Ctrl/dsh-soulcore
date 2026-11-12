"""规则版冲突检测：没有 API key（规则提炼）时也能处理"搬家了 / 换导师了"。

LLM 提炼漏标 replaces 时也用它兜底。原则：**宁可漏判，不可错判**——错判会把仍然有效的
记忆标成"已过时"藏起来，比漏掉一次更新更糟。

三条判定：
  A. 同槽位不同值（住址、年级、导师、主控板型号…），并且句子里有变更词，或者槽位本身只能有一个值；
  B. 喜好反转（"喜欢喝咖啡" → "戒咖啡了"）；
  C. 通用兜底：有变更词、两句话足够像、旧句里有个英文/数字值在新句里不见了。
"""
from __future__ import annotations

import re

from .textutil import jaccard

SPEAKER_PREFIX = re.compile(r"^[^：:\s]{1,12}说[：:]")          # 规则提炼产出的 "主人说：..."
FILLER_PREFIX = re.compile(r"^(记住|记一下|记得|别忘了|跟你说|告诉你)[，,：:\s]*")

_CHANGE_WORDS = (r"改成|改为|改用|换成|换了|换到|搬到|搬去|搬家|迁到|迁移到|升到|升级到|变成|不再|"
                 r"不用.{0,8}了|已经不|现在(是|改|用|只|住|在)|戒了|戒掉|退了|分手|辞职|离职|跳槽|毕业了")
CHANGE = re.compile(_CHANGE_WORDS)

_V = r"[一-鿿A-Za-z0-9\- ]{1,16}"
SLOTS = [
    ("住址", re.compile(r"(?:住在|住到|搬到|搬去|搬家到)\s*(?P<v>[一-鿿A-Za-z0-9]{2,10})")),
    ("年级", re.compile(r"(?P<v>大[一二三四]|研[一二三]|博[一二三四五]|高[一二三]|初[一二三])")),
    ("专业", re.compile(r"专业是\s*(?P<v>[一-鿿A-Za-z]{2,10})")),
    ("导师", re.compile(r"(?:导师|老师)(?:姓|是|叫|换成了?|改成了?)\s*(?P<v>[一-鿿A-Za-z]{1,6})")),
    ("工作", re.compile(r"(?:在|去|入职|跳槽到)(?P<v>[一-鿿A-Za-z0-9]{2,12})(?:工作|上班|实习)")),
    ("部署", re.compile(r"(?P<s>[一-鿿A-Za-z0-9]{1,10})(?:部署在|部署到|跑在|放在|迁到|迁移到|托管在|换到)\s*(?P<v>[一-鿿A-Za-z0-9]{2,12})")),
    ("型号", re.compile(r"(?P<s>[一-鿿A-Za-z0-9]{1,8}?)(?:是|用的是|换成了?|改成了?|改成)\s*(?P<v>[A-Za-z][A-Za-z0-9\- ]{1,15}|[一-鿿]{1,4}[A-Za-z0-9\-]{1,10})")),
    ("用的", re.compile(r"(?P<s>[一-鿿A-Za-z0-9]{2,8})(?:用的是|用|改成用|改用|换成)\s*(?P<v>[A-Za-z][A-Za-z0-9\-]{1,20})")),
    ("频率时间", re.compile(r"(?P<v>每[天周月日][^，。]{0,10}|周[一二三四五六日天][^，。]{0,8})")),
    ("喜好", re.compile(r"(?:喜欢|爱|只)(?:喝|吃|玩|看|听|用)?\s*(?P<v>[一-鿿A-Za-z]{1,6})")),
]

# 这些槽位本身只能有一个当前值：不需要变更词也算"变了"
_SINGLE_VALUE = {"住址", "年级", "导师", "工作", "专业", "部署", "频率时间"}
# 个人属性：只看"我"的属性，句子里说到别人（弟弟/室友/他…）就不要往主人身上套
_PERSONAL = {"住址", "年级", "工作", "专业", "喜好"}
# 句子里"人"的指代：值前面最近的那个决定了这句话在说谁
_PERSON_REF = re.compile(r"我|你|他|她|弟弟|妹妹|哥哥|姐姐|爸|妈|儿子|女儿|表[哥姐弟妹]|堂[哥姐弟妹]|"
                         r"男朋友|女朋友|对象|老婆|老公|同学|室友|同事|朋友|爷爷|奶奶|姥姥|姥爷")
_NEG_BEFORE = re.compile(r"不|别|没")

_LIKE = re.compile(r"(?:喜欢|爱)(?:喝|吃|玩|看|听|用)?\s*(?P<v>[一-鿿A-Za-z]{1,6})")
_NEGATED = re.compile(r"戒了|戒掉|不(?:再|喜欢)?(?:喝|吃|玩|看|听|用)?|讨厌|受不了")
_ASCII_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-\.]*")


def core_text(s: str) -> str:
    """去掉说话人前缀和"记住，"这类前缀，再去掉首尾空白"""
    s = (s or "").strip()
    s = SPEAKER_PREFIX.sub("", s)
    s = FILLER_PREFIX.sub("", s)
    return s.strip()


def _norm(s: str | None) -> str:
    return (s or "").strip().lower().replace(" ", "")


def _unpack(c) -> tuple[int, str, str]:
    """candidates 允许 (id, content) 或 (id, content, kind)"""
    if len(c) >= 3:
        return int(c[0]), str(c[1]), str(c[2])
    return int(c[0]), str(c[1]), "fact"


def _subj_match(a: str, b: str) -> bool:
    """主语是否同一个东西：完全相同、包含关系（"博客"/"博客已经"）、或 jaccard ≥ 0.5"""
    if not a or not b:
        return True                      # 槽位没有主语（住址/年级这种），视为同一主语
    if a == b or a in b or b in a:
        return True
    return jaccard(a, b) >= 0.5


def _slots(text: str) -> list[tuple[str, str, str, int]]:
    """返回 (槽位名, 主语, 值, 值在句中的位置)。

    位置用来判断"这句话说的是不是我"：主语出现在值前面，且不是"我"，就不是我自己的属性。
    """
    out: list[tuple[str, str, str, int]] = []
    for name, pat in SLOTS:
        for m in pat.finditer(text):
            gd = m.groupdict()
            v = _norm(gd.get("v")).strip("的了")
            if not v:
                continue
            # "我最喜欢的电影是…"会被"喜好"槽位抓成"的电影是星际"这种短语：不是值，丢掉
            if name == "喜好" and ("的" in v or "是" in v or len(v) > 4):
                continue
            try:
                vpos = m.start("v")
            except IndexError:
                vpos = m.start()
            # "我不再喜欢熬夜"里的"熬夜"是被否定的对象，不是新的喜好
            if name == "喜好" and _NEG_BEFORE.search(text[max(0, vpos - 4):vpos]):
                continue
            out.append((name, _norm(gd.get("s")), v, vpos))
    return out


def _value_changed(old_v: str, new_v: str) -> bool:
    """值真的变了：不能只是"北京" → "北京海淀"这种补充细节"""
    if old_v == new_v:
        return False
    if old_v in new_v or new_v in old_v:
        return False
    return True


def _subject_before(text: str, pos: int) -> str:
    """值前面最近的一个"人"：我（自己）或别人（弟弟/室友/他…）。没有就返回空。"""
    head = text[:pos]
    found = None
    for m in _PERSON_REF.finditer(head):
        found = m.group(0)
    return found or ""


def _rule_a(new: str, old: str) -> bool:
    new_slots = _slots(new)
    if not new_slots:
        return False
    for name, s1, v1, _p1 in _slots(old):
        for name2, s2, v2, p2 in new_slots:
            if name != name2 or not _value_changed(v1, v2):
                continue
            if not _subj_match(s1, s2):
                continue
            if name in _PERSONAL:
                # 个人属性只看"我"：值前面最近的人不是"我"，就是在说别人
                who = _subject_before(new, p2)
                if who and who != "我":
                    continue
            if CHANGE.search(new) or name in _SINGLE_VALUE:
                return True
    return False


def _rule_b(new: str, old: str) -> bool:
    m = _LIKE.search(old)
    if not m:
        return False
    x = m.group("v").strip("的了")
    if not x or len(x) > 4 or "的" in x or "是" in x:
        return False
    if x not in new:
        return False
    # 否定的必须**就是**旧的那个对象：X 要出现在否定词后面 6 个字以内
    for neg in _NEGATED.finditer(new):
        if x in new[neg.start():neg.start() + 6 + len(x)]:
            return True
    return False


def _rule_c(new: str, old: str, kind: str) -> bool:
    if kind not in ("fact", "decision"):
        return False
    if not CHANGE.search(new):
        return False
    if jaccard(new, old) < 0.35:
        return False
    for tok in _ASCII_TOKEN.findall(old):
        if tok.lower() not in new:
            return True              # 旧句里的英文/数字值在新句里没了 → 值变了
    return False


def find_superseded(new_text: str, candidates) -> list[int]:
    """candidates = [(id, content[, kind])]，来自同一路径（或同一人物档案）、同一类组的有效条目。

    返回应被 new_text 取代的旧条目 id（最多 2 条，按相似度从高到低）。
    """
    new_core = core_text(new_text)
    if not new_core:
        return []
    hits: list[tuple[float, int]] = []
    for c in candidates or ():
        try:
            cid, content, kind = _unpack(c)
        except (TypeError, ValueError):
            continue
        old_core = core_text(content)
        if not old_core:
            continue
        j = jaccard(new_core, old_core)
        if j >= 0.9:                  # 同一件事的另一种说法，不算取代
            continue
        if _rule_a(new_core, old_core) or _rule_b(new_core, old_core) or _rule_c(new_core, old_core, kind):
            hits.append((j, cid))
    hits.sort(key=lambda x: -x[0])
    return [cid for _, cid in hits[:2]]
