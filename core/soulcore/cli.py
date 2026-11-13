"""命令行入口：python -m soulcore <命令>"""
from __future__ import annotations
import argparse, json, logging, os, sys, time

from .config import load_config
from .client import resolve_home
from .events import Event
from .emotion import EVENTS, EVENT_CN


def _engine(args, clock=time.time):
    from .engine import Engine
    cfg, base = load_config(_config_path(args))
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


def _lock_path(args) -> str:
    """锁文件放在配置文件旁边（soulcore home），和 data_dir 无关。"""
    cfg_path = _config_path(args)
    return os.path.join(os.path.dirname(cfg_path), "soulcore.lock")


def _probe(info: dict | None, timeout: float = 1.5) -> bool:
    """锁文件里记的实例是不是真的活着（pid 在 + /v1/health 通）。"""
    if not info or not info.get("port"):
        return False
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/v1/health",
                                     headers={"Authorization": f"Bearer {info.get('token', '')}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _config_path(args) -> str:
    """命令行给的配置；没给/不存在时回落到 resolve_home() 里的 config.json。

    resolve_home() 会顺便建目录、写默认配置和人设模板，所以第一次用 Claude Code /
    MCP 时不至于因为"没有配置文件"直接报错。
    """
    p = os.path.abspath(args.config)
    if os.path.exists(p):
        return p
    return os.path.join(resolve_home(), "config.json")


def cmd_serve(args):
    from .lockfile import InstanceLock
    from .server import serve
    from . import __version__
    lock_path = _lock_path(args)
    info = InstanceLock.read(lock_path)
    if _probe(info):
        print(f"已经有 soulcore 在运行：pid={info['pid']} port={info['port']}（{info.get('version', '?')}），"
              f"DSH 会直接接入它，不需要再起一个。")
        return
    # 先抢锁再创建 Engine：Engine 初始化会做数据库迁移、人物档案合并，这些写入只能由唯一实例来做
    lock = InstanceLock(lock_path)
    if not lock.acquire():
        info = InstanceLock.read(lock_path) or {}
        print("SOULCORE_ALREADY_RUNNING " + json.dumps(info, ensure_ascii=False), flush=True)
        return
    try:
        eng, cfg = _engine(args)
    except BaseException:
        lock.release()
        raise
    if args.port is not None:
        cfg["server"]["port"] = args.port
    if serve(eng, cfg, lock_path=lock_path, version=__version__, lock=lock,
             idle_exit_min=args.idle_exit_min) is None:
        eng.close()


def cmd_install_service(args):
    from .service import install
    ok, msg = install(_config_path(args))
    print(("✓ " if ok else "✗ ") + msg)
    if not ok:
        sys.exit(1)


def cmd_uninstall_service(args):
    from .service import uninstall
    ok, msg = uninstall()
    print(("✓ " if ok else "✗ ") + msg)
    if not ok:
        sys.exit(1)


def cmd_service_start(args):
    from .service import start
    ok, msg = start()
    print(("✓ " if ok else "✗ ") + (msg or ""))
    if not ok:
        sys.exit(1)


def cmd_service_status(args):
    from .service import status
    ok, msg = status()
    print(msg if msg else ("运行中" if ok else "未安装"))
    sys.exit(0 if ok else 1)


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


def cmd_cc_hook(args):
    """Claude Code hook：从 stdin 读 JSON，失败即放行（永远 exit 0）"""
    from .cc_hook import main as hook_main
    sys.exit(hook_main(args.event))


def cmd_mcp(args):
    """MCP server：stdio 上的 JSON-RPC 2.0，stdout 只放协议消息"""
    from .mcp_server import main as mcp_main
    sys.exit(mcp_main())


def cmd_cc_install(args):
    """把 hooks 合并进 Claude Code 的 settings.json（默认只打印）"""
    from .cc_install import install
    code, text = install(scope=args.scope, apply=args.apply, uninstall=args.uninstall,
                         cwd=os.getcwd())
    print(text)
    sys.exit(code)


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
    s.add_argument("--idle-exit-min", type=float, default=0.0,
                   help="空闲多少分钟后自动退出（0=不退出；Claude Code 拉起的后端用 240）")
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("review", help="审核 B站待发回复").set_defaults(fn=cmd_review)
    b = sub.add_parser("bili", help="单独运行 B站轮询")
    b.add_argument("--once", action="store_true"); b.add_argument("--resume", action="store_true")
    b.set_defaults(fn=cmd_bili)
    sub.add_parser("state", help="查看当前状态").set_defaults(fn=cmd_state)
    h = sub.add_parser("cc-hook", help="Claude Code hook 适配器（从 stdin 读事件 JSON，失败即放行）")
    h.add_argument("event", choices=["session-start", "user-prompt", "stop", "pre-compact", "session-end"])
    h.set_defaults(fn=cmd_cc_hook)
    sub.add_parser("mcp", help="MCP server（stdio，给 Claude Code / Cursor / Claude Desktop 用）") \
        .set_defaults(fn=cmd_mcp)
    ci = sub.add_parser("cc-install", help="把 hooks 合并进 Claude Code 的 settings.json（默认只打印）")
    ci.add_argument("--scope", choices=["user", "project"], default="user")
    ci.add_argument("--apply", action="store_true", help="真的写文件（先备份）")
    ci.add_argument("--uninstall", action="store_true", help="移除 soulcore 的 hooks")
    ci.set_defaults(fn=cmd_cc_install)
    sub.add_parser("demo", help="离线演示：模拟一天的 QQ/B站/项目消息").set_defaults(fn=cmd_demo)
    sub.add_parser("install-service", help="注册常驻计划任务（登录时自动启动，Windows）").set_defaults(fn=cmd_install_service)
    sub.add_parser("uninstall-service", help="删除常驻计划任务").set_defaults(fn=cmd_uninstall_service)
    sub.add_parser("service-start", help="立刻运行常驻计划任务（Windows）").set_defaults(fn=cmd_service_start)
    sub.add_parser("service-status", help="查看常驻计划任务状态").set_defaults(fn=cmd_service_status)
    args = p.parse_args(argv)
    # hook / MCP 的 stdout 是协议通道，日志只留错误级别（真正的记录写到各自日志文件）
    level = logging.DEBUG if args.verbose else (
        logging.ERROR if args.cmd in ("cc-hook", "mcp") else logging.INFO)
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    args.fn(args)
