"""中文友好的轻量分词：英文/数字按词，中文按单字+二元组。不依赖 jieba，SQLite FTS5 直接可用。"""
from __future__ import annotations
import re

_TOKEN = re.compile(r"[A-Za-z0-9_\-\.]+|[一-鿿]+")
STOP = set("的了是我你他她它们在和就也都而及与着或一个这那吗呢吧啊哦嗯呀")


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for m in _TOKEN.finditer(text or ""):
        w = m.group(0)
        if w[0] < "一":
            if len(w) > 1:
                out.append(w.lower())
            continue
        chars = [c for c in w]
        out.extend(c for c in chars if c not in STOP)
        out.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return out


def index_text(text: str) -> str:
    return " ".join(tokens(text))


def fts_query(text: str, limit: int = 24) -> str:
    """把自然语言变成 FTS5 OR 查询；优先二元组（更有区分度）"""
    toks = list(dict.fromkeys(tokens(text)))
    toks.sort(key=lambda t: -len(t))
    toks = toks[:limit]
    return " OR ".join('"' + t.replace('"', "") + '"' for t in toks)


def jaccard(a: str, b: str) -> float:
    sa, sb = set(tokens(a)), set(tokens(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def token_weights(text: str) -> dict[str, float]:
    """二元组/英文词权重 2，单字权重 1"""
    out: dict[str, float] = {}
    for t in tokens(text):
        out[t] = 2.0 if len(t) > 1 else 1.0
    return out


def overlap(qw: dict[str, float], doc: str) -> float:
    """与语料规模无关的相关度：带权集合余弦，∈[0,1]"""
    dw = token_weights(doc)
    if not qw or not dw:
        return 0.0
    hit = sum(w for t, w in qw.items() if t in dw)
    return hit / ((sum(qw.values()) * sum(dw.values())) ** 0.5)
