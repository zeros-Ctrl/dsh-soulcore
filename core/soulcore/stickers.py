"""QQ 表情与表情包：把回复里的标记变成 OneBot v11 消息段。

模型输出里可以写：
  [表情:微笑]   → face 段（QQ 内置小表情，不需要图片）
  [图:无语]     → image 段（本地表情包，按标签取图，走 base64 发送）

为什么用标记：模型看不到图，只能在文字层面决定"此刻该发个无语表情"，
所以标签要人写清楚（比如"狗头翻白眼"），它才能挑对。

FACE_TABLE 的 id 以 QQ 官方表情列表为准（只收 type=1 的系统表情），
旧版按表情面板 0 起顺序编号的那张表有 20 多个 ID 是错位的，别再用。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re

log = logging.getLogger("soulcore.sticker")

# QQ 系统表情 id（来源：QQ 机器人官方文档「表情对象 · Emoji 列表」type=1）
FACE_TABLE: dict[str, int] = {
    "得意": 4, "流泪": 5, "睡": 8, "大哭": 9, "尴尬": 10, "调皮": 12,
    "呲牙": 13, "微笑": 14, "酷": 16, "可爱": 21, "傲慢": 23, "饥饿": 24,
    "困": 25, "惊恐": 26, "流汗": 27, "憨笑": 28, "悠闲": 29, "奋斗": 30,
    "疑问": 32, "嘘": 33, "晕": 34, "敲打": 38, "再见": 39,
    "发抖": 41, "爱情": 42, "跳跳": 43, "拥抱": 49, "蛋糕": 53, "咖啡": 60,
    "玫瑰": 63, "爱心": 66, "太阳": 74, "月亮": 75, "赞": 76, "握手": 78,
    "胜利": 79, "飞吻": 85, "西瓜": 89, "冷汗": 96, "擦汗": 97, "抠鼻": 98,
    "鼓掌": 99, "糗大了": 100, "坏笑": 101, "左哼哼": 102, "右哼哼": 103,
    "哈欠": 104, "委屈": 106, "左亲亲": 109, "可怜": 111, "示爱": 116,
    "抱拳": 118, "拳头": 120, "爱你": 122, "NO": 123, "OK": 124, "转圈": 125,
    "挥手": 129, "喝彩": 144, "棒棒糖": 147, "茶": 171, "泪奔": 173,
    "无奈": 174, "卖萌": 175, "小纠结": 176, "doge": 179, "惊喜": 180,
    "骚扰": 181, "笑哭": 182, "我最美": 183, "点赞": 201, "托脸": 203,
    "托腮": 212, "啵啵": 214, "蹭一蹭": 219, "抱抱": 222, "拍手": 227,
    "佛系": 232, "喷脸": 240, "甩头": 243, "加油抱抱": 246, "脑阔疼": 262,
    "捂脸": 264, "辣眼睛": 265, "哦哟": 266, "头秃": 267, "问号脸": 268,
    "暗中观察": 269, "emm": 270, "吃瓜": 271, "呵呵哒": 272, "我酸了": 273,
    "汪汪": 277, "汗": 278, "无眼笑": 281, "敬礼": 282, "面无表情": 284,
    "摸鱼": 285, "哦": 287, "睁眼": 289, "敲开心": 290, "摸锦鲤": 293,
    "期待": 294, "拜谢": 297, "元宝": 298, "牛啊": 299, "右亲亲": 305,
    "牛气冲天": 306, "喵喵": 307, "仔细分析": 314, "加油": 315, "崇拜": 318,
    "比心": 319, "庆祝": 320, "拒绝": 322, "吃糖": 324, "生气": 326,
}

# 常用别名（"强"按语义走赞，别再发成胜利）
FACE_ALIAS = {"强": 76, "棒": 76, "笑": 182, "哈哈": 182, "点赞": 76, "good": 76}

# 提示词里优先露脸的几个：都是高频、语义清楚、id 已核对的
FACE_COMMON = ["微笑", "呲牙", "憨笑", "笑哭", "doge", "捂脸", "吃瓜", "尴尬",
               "擦汗", "疑问", "问号脸", "委屈", "大哭", "可怜", "无奈", "赞", "比心"]

_TAG_RE = re.compile(r"\[(表情|face|图|表情包|贴图|sticker)\s*[:：]\s*([^\[\]]{1,40})\]")
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 4 * 1024 * 1024


class StickerLib:
    """本地表情包目录。

    manifest.json 格式：
        {"stickers": [{"tag": "无语", "file": "wuyu.gif", "desc": "狗头翻白眼，表示无语"}]}
    没有 manifest 时，用目录里的图片文件名当标签。
    """

    def __init__(self, directory: str | None, enabled: bool = True):
        self.dir = (directory or "").strip()
        self.enabled = bool(enabled)
        self.items: dict[str, dict] = {}
        if self.enabled and self.dir:
            self.reload()

    def reload(self) -> None:
        self.items = {}
        if not os.path.isdir(self.dir):
            log.info("表情包目录不存在，[图:] 标记会失效：%s", self.dir)
            return
        manifest = os.path.join(self.dir, "manifest.json")
        if os.path.isfile(manifest):
            try:
                with open(manifest, encoding="utf-8") as f:
                    raw = json.load(f)
                rows = raw if isinstance(raw, list) else raw.get("stickers", [])
                for it in rows:
                    tag = str(it.get("tag") or "").strip()
                    fn = str(it.get("file") or "").strip()
                    if tag and fn:
                        self.items[tag] = {"file": os.path.join(self.dir, fn),
                                           "desc": str(it.get("desc") or tag)}
            except Exception as ex:
                log.warning("表情包清单读取失败：%s", ex)
        else:
            for fn in sorted(os.listdir(self.dir)):
                stem, ext = os.path.splitext(fn)
                if ext.lower() in _IMAGE_EXTS:
                    self.items[stem] = {"file": os.path.join(self.dir, fn), "desc": stem}

    def get(self, tag: str) -> dict | None:
        if not self.enabled or not tag:
            return None
        it = self.items.get(tag)
        if it and os.path.isfile(it["file"]):
            return it
        for key, val in self.items.items():
            if tag in key or key in tag:
                if os.path.isfile(val["file"]):
                    return val
        return None

    def image_segment(self, tag: str) -> dict | None:
        it = self.get(tag)
        if not it:
            return None
        try:
            if os.path.getsize(it["file"]) > MAX_IMAGE_BYTES:
                log.warning("表情包超过大小上限，跳过：%s", it["file"])
                return None
            with open(it["file"], "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
        except OSError as ex:
            log.warning("表情包读取失败 %s: %s", it["file"], ex)
            return None
        return {"type": "image", "data": {"file": "base64://" + b64}}

    def describe(self) -> list[str]:
        out = []
        for tag, it in self.items.items():
            desc = (it.get("desc") or "").strip()
            out.append(tag if not desc or desc == tag else f"{tag}（{desc}）")
        return out


def find_face(name: str) -> int | None:
    if name in FACE_TABLE:
        return FACE_TABLE[name]
    if name in FACE_ALIAS:
        return FACE_ALIAS[name]
    for key in FACE_ALIAS:
        if name and (name in key or key in name):
            return FACE_ALIAS[key]
    for key, fid in FACE_TABLE.items():
        if name and (name in key or key in name):
            return fid
    return None


def strip_tags(text: str) -> str:
    """把认不出的表情标记从文字里删掉，别把 [图:无语] 原样发出去。"""
    return _TAG_RE.sub("", text or "")


def parse_reply(text: str, lib: StickerLib | None = None, max_images: int = 1) -> list[dict]:
    """把回复文本切成 OneBot 消息段；认不出的标记直接丢掉，不会漏成原文。"""
    segs: list[dict] = []
    pos = 0
    images = 0
    for m in _TAG_RE.finditer(text or ""):
        if m.start() > pos:
            chunk = text[pos:m.start()]
            if chunk.strip():
                segs.append({"type": "text", "data": {"text": chunk}})
        kind, val = m.group(1), m.group(2).strip().strip("【】[]")
        seg = None
        if kind in ("表情", "face"):
            fid = find_face(val)
            if fid is not None:
                seg = {"type": "face", "data": {"id": str(fid)}}
        elif images < max_images and lib is not None:
            seg = lib.image_segment(val)
            if seg:
                images += 1
        if seg:
            segs.append(seg)
        else:
            log.debug("表情标记丢弃：[%s:%s]", kind, val)
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        segs.append({"type": "text", "data": {"text": tail}})
    return segs


def prompt_hint(lib: StickerLib | None, max_items: int = 20) -> str:
    """给 system prompt 的一段说明。没有图库时只提内置小表情，不教它写 [图:]。"""
    has_pics = bool(lib is not None and lib.enabled and lib.items)
    lines = ["## 发表情",
             "你可以偶尔在回复里发表情配合语气，写法是标记：",
             "- [表情:名字] 发一个 QQ 小表情，比如 [表情:笑哭]"]
    if has_pics:
        lines.append("- [图:标签] 发一张表情包图片")
    lines.append("可用小表情：" + "、".join(f for f in FACE_COMMON if f in FACE_TABLE) + "。")
    if has_pics:
        lines.append("可用表情包：" + "；".join(lib.describe()[:max_items]) + "。")
        lines.append("表情会单独成气泡，放在句尾；别每条都发，一条回复最多一个表情包，小表情也克制点。")
    else:
        lines.append("表情会单独成气泡，放在句尾；别每条都发，克制点。")
    return "\n".join(lines)
