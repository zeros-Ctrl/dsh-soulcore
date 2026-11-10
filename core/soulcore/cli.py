"""命令行入口：python -m soulcore <命令>"""
from __future__ import annotations
import argparse, json, logging, os, sys, time

from .config import load_config
from .events import Event
from .emotion import EVENTS, EVENT_CN


def _engine(args, clock=time.time):
    from .engine import Engine
    cfg, base = load_config(args.config)
    if getattr(args, "data", None):
        cfg["data_dir"] = os.path.abspath(args.data)
    return Engine(cfg, base, mock=args.mock, clock=clock), cfg


def bar(m: float, width: int = 20) -> str:
    n = int(round(m * width))
    return "█" * n + "░" * (width - n)


CHAT_HELP = """命令：
  /event <名称>   触发情绪事件：""" + "、".join(f"{k}({v})" for k, v in EVENT_CN.items()) + """
  /skip <小时>    时间快进（测试遗忘、孤独、情绪回落）
  /state          查看情绪和记忆统计
  /mem <关键词>   搜索记忆
  /prompt         查看下一轮会注入的 system prompt
  /flush          立即抽取记忆
  /tick           执行一次维护（冷却/合并摘要）
  /quit           退出"""


def cmd_chat(args):
    offset = [0.0]
    clock = lambda: time.time() + offset[0]
    eng, cfg = _engine(args, clock)
    if args.project:
        mk = lambda t: Event("local", "project", args.project, "owner", args.who, t, mentions_self=True, ts=clock())
    else:
        mk = lambda t: Event("local", "session", args.session, "owner", args.who, t, mentions_self=True, ts=clock())
    print(f"和 {eng.name} 聊天（{'离线模拟' if args.mock else cfg['llm']['model']}），记忆分支：{mk('').path}")
    print("输入 /help 查看命令\n")
    try:
        while True:
            st = eng.state()
            try:
                line = input(f"[{bar(st['mood'], 10)} {st['mood']:.2f} {st['label']}] 你> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line.startswith("/"):
                c, _, a = line.partition(" ")
                if c in ("/quit", "/exit"):
                    break
                elif c == "/help":
                    print(CHAT_HELP)
                elif c == "/event":
                    if a not in EVENTS:
                        print("可选：", ", ".join(EVENTS)); continue
                    amp = eng.system_event(a); print(f"  触发 {EVENT_CN[a]}，冲击 {amp:+.2f}（情绪会在接下来几十分钟慢慢变化）")
                elif c == "/skip":
                    h = float(a or 1); offset[0] += h * 3600; st = eng.state()
                    print(f"  ⏩ 过了 {h} 小时 → {st['describe']}  mood={st['mood']:.3f}")
                elif c == "/state":
                    print(json.dumps(eng.state(), ensure_ascii=False, indent=2))
                elif c == "/mem":
                    for it in eng.store.search(a, mk("").path, k=10, touch=False):
                        print(f"  {it.score:.3f}  [{it.path} · {it.kind}] {it.content[:80]}")
                elif c == "/prompt":
                    print(eng.prepare(mk("（预览）"))["system"])
                elif c == "/flush":
                    eng._maybe_extract(mk(""), force=True); print("  已抽取")
                elif c == "/tick":
                    print(" ", eng.tick())
                else:
                    print(CHAT_HELP)
                continue
            reply = eng.handle(mk(line))
            print(f"{eng.name}> {reply}\n")
    finally:
        eng.close()


def cmd_serve(args):
    from .server import serve
    eng, cfg = _engine(args)
    if args.port is not None:
        cfg["server"]["port"] = args.port
    serve(eng, cfg)


def cmd_review(args):
    from .adapters.bilibili import BiliAdapter
    eng, cfg = _engine(args)
    ad = BiliAdapter(eng, cfg["bilibili"])
    items = ad.queue.pending()
    if not items:
        print("没有待审核的回复。"); return
    for it in items:
        e = it["event"]
        print("─" * 60)
        print(f"[{e['scene']}] {e['user_name']}（{e['channel_id']}）：{e['text']}")
        print(f"她当时：{it.get('mood', '')}")
        print(f"草稿：{it['draft']}")
        a = input("[y]发送  [e]编辑后发送  [s]跳过  [q]退出 > ").strip().lower()
        if a == "q":
            break
        if a == "s":
            ad.queue.update(it["id"], status="skipped"); continue
        text = input("新内容> ").strip() if a == "e" else None
        if a in ("y", "e"):
            try:
                print("已发送" if ad.approve(it, text) else "未发送（限流）")
            except Exception as ex:
                print("发送失败：", ex); break
    eng.close()


def cmd_bili(args):
    from .adapters.bilibili import BiliAdapter
    eng, cfg = _engine(args)
    ad = BiliAdapter(eng, cfg["bilibili"])
    if args.resume:
        ad.resume(); print("已解除暂停"); return
    if args.once:
        evs = ad.poll_once(); print(f"拉到 {len(evs)} 条新消息；待审核 {len(ad.queue.pending())} 条")
    else:
        ad.run_forever()
    eng.close()


def cmd_state(args):
    eng, _ = _engine(args)
    print(json.dumps(eng.state(), ensure_ascii=False, indent=2))
    eng.close()


def cmd_demo(args):
    from .demo import run_demo
    run_demo(args.data or "demo_data")


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="soulcore", description="独立人格 + 分层记忆插件")
    p.add_argument("-c", "--config", default="config.json")
    p.add_argument("--mock", action="store_true", help="不调用 DeepSeek，用离线模拟（测试管线）")
    p.add_argument("--data", help="覆盖数据目录")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chat", help="本地命令行聊天（调试人格和记忆）")
    c.add_argument("--project", help="以项目模式聊天，记忆进 project/<名称>")
    c.add_argument("--session", default="default")
    c.add_argument("--who", default="主人")
    c.set_defaults(fn=cmd_chat)
    s = sub.add_parser("serve", help="启动 HTTP 钩子服务（含 QQ 上报入口、B站轮询）")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("review", help="审核 B站待发回复").set_defaults(fn=cmd_review)
    b = sub.add_parser("bili", help="单独运行 B站轮询")
    b.add_argument("--once", action="store_true"); b.add_argument("--resume", action="store_true")
    b.set_defaults(fn=cmd_bili)
    sub.add_parser("state", help="查看当前状态").set_defaults(fn=cmd_state)
    sub.add_parser("demo", help="离线演示：模拟一天的 QQ/B站/项目消息").set_defaults(fn=cmd_demo)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    args.fn(args)
