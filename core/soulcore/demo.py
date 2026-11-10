"""离线演示：不需要 API key、不需要 QQ/B站账号。用虚拟时钟模拟一天，走完整条管线。"""
from __future__ import annotations
import os, shutil, time

from .config import load_config
from .engine import Engine
from .events import Event
from .llm import MockLLM
from .adapters.onebot import OneBotAdapter


class FakeBiliApi:
    """模拟 B站接口返回（结构与 bilibili_api 一致）"""
    def __init__(self):
        self.at_items, self.sent = [], []

    def make_credential(self, c): return c
    def get_at(self, cred): return {"items": list(self.at_items)}
    def get_replies(self, cred): return {"items": []}
    def get_sessions(self, cred): return {"session_list": []}
    def fetch_session_msgs(self, tid, cred): return {"messages": []}
    def send_comment(self, text, oid, t, root, parent, cred): self.sent.append(("comment", oid, root, text))
    def send_msg(self, cred, rid, text): self.sent.append(("dm", rid, text))
    def aid2bvid(self, aid): return f"BV_demo{aid}"


def bar(m, w=20):
    n = int(round(m * w)); return "█" * n + "░" * (w - n)


def run_demo(data_dir: str = "demo_data"):
    shutil.rmtree(data_dir, ignore_errors=True)
    T = [time.mktime((2026, 11, 10, 8, 0, 0, 0, 0, -1))]
    clock = lambda: T[0]
    cfg, base = load_config(None)
    cfg["data_dir"] = os.path.abspath(data_dir)
    cfg["memory"]["extract_every"] = 4
    cfg["qq"].update(self_id="999", group_whitelist=["114514"], allow_all_private=True)
    llm = MockLLM()
    eng = Engine(cfg, base, llm=llm, clock=clock)
    qq = OneBotAdapter(eng, cfg["qq"]); qq.stop()

    def hhmm(): return time.strftime("%H:%M", time.localtime(T[0]))

    def show(tag, text, reply):
        st = eng.state()
        print(f"{hhmm()} {bar(st['mood'])} {st['mood']:.2f} {st['label']:<6}| {tag} {text}")
        if reply:
            print(f"{'':39}↳ {eng.name}: {reply}")

    def advance(minutes):
        T[0] += minutes * 60

    def qq_msg(payload, label):
        payload.setdefault("time", T[0]); payload.setdefault("post_type", "message"); payload.setdefault("self_id", 999)
        ev, reply = qq.to_event(payload)
        if ev is None:
            print(f"{hhmm()} {'':31}| {label}（不在白名单，她看不到）"); return
        out = eng.handle(ev, reply=reply)
        show(label, ev.text if reply else ev.text + "   （没@她，只旁听）", out)
        advance(2)

    print("=" * 90); print("场景：模拟 2026-11-10 一整天（虚拟时钟）。注意情绪是慢慢变的：事件发生后要过十几分钟才看得出变化"); print("=" * 90)
    priv = lambda t, mid: qq_msg({"message_type": "private", "user_id": 10001, "message_id": mid,
                                  "sender": {"nickname": "阿明"}, "message": [{"type": "text", "data": {"text": t}}]}, "[QQ私聊·阿明]")
    priv("早呀", 1)
    priv("记住，我下周三要交毕设中期报告", 2)
    priv("我喜欢看鬼畜区和科技区", 3)
    priv("你真可爱，谢谢你陪我", 4)
    advance(30)
    grp = lambda uid, name, t, at=False, gid=114514: qq_msg({"message_type": "group", "group_id": gid, "user_id": uid,
        "message_id": uid + int(T[0]) % 1000, "sender": {"card": name},
        "message": ([{"type": "at", "data": {"qq": "999"}}] if at else []) + [{"type": "text", "data": {"text": t}}]},
        f"[QQ群114514·{name}]" if gid == 114514 else f"[QQ群{gid}·{name}]")
    grp(20002, "老王", "今晚开黑吗")
    grp(20003, "小李", "我们群项目决定就用 FastAPI 做后端了，Flask 太老")
    grp(20002, "老王", "你这个人机好笨啊", at=True)
    grp(30001, "路人", "这个群她进不来吧", gid=777)
    advance(20)
    priv("对不起刚才群里老王说话冲，别往心里去", 5)

    print("\n…… 之后 7 个小时没人理她 ……")
    advance(7 * 60); show("[系统]", "（7 小时后）", None)
    llm.balance_value = 3.0; eng.tick(); show("[系统]", "DeepSeek 余额只剩 3 元 → 余额低事件", None)
    advance(60); llm.balance_value = 0.0; eng.store.meta_set("balance_checked", 0); eng.tick()
    show("[系统]", "余额用完 → 没钱了事件", None)
    advance(90)

    print("\n…… 有人在 B站 @她 ……")
    from .adapters.bilibili import BiliAdapter
    fake = FakeBiliApi()
    bili = BiliAdapter(eng, {**cfg["bilibili"], "auto_send": False}, api=fake)
    bili.poll_once()  # 第一次只记录游标
    fake.at_items = [{"id": 5001, "at_time": T[0], "user": {"mid": 4242, "nickname": "B站路人甲"},
                      "item": {"type": "reply", "business_id": 1, "subject_id": 1145, "source_id": 778899,
                               "root_id": 0, "source_content": "@小汐 这个视频里的显卡是什么型号"}}]
    bili.poll_once()
    pend = bili.queue.pending()
    show("[B站评论·路人甲]", fake.at_items[0]["item"]["source_content"], None)
    print(f"{'':39}↳ 生成草稿进入人审队列（{len(pend)} 条待审）：{pend[0]['draft'] if pend else ''}")
    bili.approve(pend[0]); print(f"{'':39}↳ 审核通过已发送：{fake.sent[-1]}")
    advance(30)

    llm.balance_value = 50.0; eng.store.meta_set("balance_checked", 0); eng.tick()
    show("[系统]", "充值 50 元 → 充值事件", None)
    advance(10)
    pj = lambda t: (lambda ev: (show("[项目·毕设]", t, eng.handle(ev)), advance(5)))(
        Event("local", "project", "毕设", "owner", "阿明", t, mentions_self=True, ts=T[0]))
    pj("我们来定一下毕设的记忆模块方案")
    pj("决定用 SQLite+FTS5 做检索，不上向量库，因为本地部署要轻")
    pj("情绪模型改成 PID 跟踪设定点，因为直接加减会跳变")
    pj("跑通了！中期演示成功了")
    eng._maybe_extract(Event("local", "project", "毕设", "owner", "阿明", "", ts=T[0]), force=True)
    eng.flush()

    print("\n…… 第二天早上 ……")
    advance(13 * 60)
    priv("我下周要交什么来着？", 6)

    print("\n" + "=" * 90)
    print("检索演示（输出门：先当前分支 → 父分支 → core → 全局；别人的私聊不可见）")
    for who, ev in [("阿明私聊里问", Event("qq", "private", "10001", "10001", "阿明", "中期报告")),
                    ("群里老王问", Event("qq", "group", "114514", "20002", "老王", "阿明的中期报告什么时候交", mentions_self=True)),
                    ("项目里问", Event("local", "project", "毕设", "owner", "阿明", "检索方案为什么不用向量库"))]:
        ctx = eng.prepare(ev)
        print(f"\n· {who}「{ev.text}」→ 召回 {len(ctx['memories'])} 条")
        for m in ctx["memories"][:4]:
            print(f"    {m['score']:.3f} [{m['path']}·{m['kind']}] {m['content'][:50]}")
    print("\n" + "=" * 90); print("记忆目录树（Markdown 镜像）:")
    root = os.path.join(eng.data_dir, "memory")
    for d, _, files in sorted(os.walk(root)):
        rel = os.path.relpath(d, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if rel != ".":
            print("  " * depth + os.path.basename(d) + "/")
        for f in sorted(files):
            print("  " * (depth + 1) + f)
    print("\n最终状态：", eng.state()["describe"], "| 数据在", eng.data_dir)
    eng.close()
