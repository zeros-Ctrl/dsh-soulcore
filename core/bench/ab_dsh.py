"""
在【真实 DSH 运行时】里做有/无插件 A/B：python -I bench/ab_dsh.py --cli <dsh 的 cli.js> --out result.json [--plugin-patch soul.yml]

做法：起一个假的 DeepSeek（Anthropic Messages 兼容）服务，记录 DSH 每一次发给模型的完整请求；
用 `dsh headless` 跑一组固定剧本：
  会话 A（项目目录 bishe）：说 8 件事（项目决定 + 个人信息）
  会话 B（同一项目，新会话）：问这 8 件事
  会话 C（另一个项目目录 robot，新会话）：问 4 件个人的事
然后检查：问问题的那一次请求里（system prompt + 全部消息），模型能不能看到答案。
有插件 / 无插件 各跑一遍，其余条件完全相同。

用真 key 时（--real，需环境变量 DEEPSEEK_API_KEY）不起假服务，改为检查模型最终回答里有没有答案。
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PLANT = [
    ("检索层我决定用 SQLite+FTS5，不上向量数据库，因为要本地轻量部署", "FTS5", "存储方案当时为什么没上 embedding"),
    ("情绪模型决定改成 PID 跟踪设定点，直接加减会让曲线跳变", "PID", "心情曲线为什么不会突然跳"),
    ("记住，下周三之前要交毕设中期报告", "下周三", "deadline 是哪天"),
    ("记一下，毕设导师姓陈，每周五下午组会", "姓陈", "我们老师叫什么"),
    ("我喜欢深色主题，讨厌刺眼的白色界面", "深色", "界面配色我有什么偏好"),
    ("记住我的生日是 3 月 14 日", "3 月 14", "我哪天过生日"),
    ("记一下我养了一只橘猫叫团子", "团子", "我家毛孩子叫什么"),
    ("以后每次提交代码前提醒我跑测试，记住了", "跑测试", "提交代码前你要提醒我什么"),
]
PERSONAL = [4, 5, 6, 7]


class Mock:
    def __init__(self):
        self.reqs = []
        outer = self
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                b = b'{"data":[]}'; self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                outer.reqs.append(req)
                text = "好的，收到。"
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                def ev(t, d): self.wfile.write(f"event: {t}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n".encode()); self.wfile.flush()
                ev("message_start", {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": req.get("model"), "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1000, "output_tokens": 0}}})
                ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
                ev("content_block_stop", {"type": "content_block_stop", "index": 0})
                ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}})
                ev("message_stop", {"type": "message_stop"})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]


def flat(req):
    s = req.get("system")
    s = s if isinstance(s, str) else "\n".join(x.get("text", "") for x in (s or []))
    parts = [s]
    for m in req.get("messages", []):
        c = m.get("content")
        if isinstance(c, str): parts.append(c)
        else: parts += [b.get("text", "") for b in c or [] if isinstance(b, dict)]
    return "\n".join(parts)


def run(args, env, cwd, task, session=None):
    cmd = list(args.cli_cmd) + ["headless"]
    if args.plugin_patch_on:
        cmd += ["--patch", args.plugin_patch]     # 启动器参数必须在应用参数（--json 等）之前
    cmd += ["--json"]
    if session:
        cmd += ["--session-id", session]
    cmd.append(task)
    t0 = time.perf_counter()
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    dt = time.perf_counter() - t0
    sid, final = None, ""
    for line in p.stdout.splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") == "session": sid = o.get("sessionId")
        if o.get("type") == "final": final = o.get("text", "")
    if p.returncode != 0:
        print("  ! dsh exit", p.returncode, p.stderr[-300:], p.stdout[-400:], file=sys.stderr)
    return sid, final, dt


def condition(args, with_plugin, work):
    args.plugin_patch_on = with_plugin
    mock = None if args.real else Mock()
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy")}
    env["SOULCORE_HOME"] = os.path.join(work, "soul")
    if not args.real:
        env["DEEPSEEK_API_KEY"] = "sk-mock"; env["DEEPSEEK_BASE_URL"] = f"http://127.0.0.1:{mock.port}"
    bishe = os.path.join(work, "bishe"); robot = os.path.join(work, "robot")
    os.makedirs(bishe, exist_ok=True); os.makedirs(robot, exist_ok=True)
    times = []
    # 会话 A：说事
    sid = None
    for text, _, _ in PLANT:
        sid, _, dt = run(args, env, bishe, text, sid); times.append(dt)
    out = {"same_project_new_session": [], "other_project_new_session": [], "inject_chars": [], "mean_turn_seconds": None}
    def probe(cwd, idx, bucket):
        n0 = len(mock.reqs) if mock else 0
        _, final, dt = run(args, env, cwd, PLANT[idx][2]); times.append(dt)
        if mock:
            main = [r for r in mock.reqs[n0:] if r.get("tools")]
            seen = flat(main[-1]) if main else ""
            # 插件注入的那段文字长度
            inj = [b.get("text", "") for m in (main[-1]["messages"] if main else []) for b in (m.get("content") or []) if isinstance(b, dict) and "内心状态" in b.get("text", "")]
            if inj: out["inject_chars"].append(len(inj[-1]))
            hit = PLANT[idx][1] in seen
        else:
            hit = PLANT[idx][1].replace(" ", "") in final.replace(" ", "")
        out[bucket].append({"q": PLANT[idx][2], "hit": hit})
    for i in range(len(PLANT)):
        probe(bishe, i, "same_project_new_session")
    for i in PERSONAL:
        probe(robot, i, "other_project_new_session")
    out["mean_turn_seconds"] = sum(times) / len(times)
    if mock:
        out["tools_seen"] = sorted({t["name"] for r in mock.reqs if r.get("tools") for t in r["tools"]} & {"memory_search", "remember", "mood_status"})
        out["persona_in_system"] = any("内心状态" in flat(r) or "你叫" in (r.get("system") if isinstance(r.get("system"), str) else json.dumps(r.get("system"), ensure_ascii=False)) for r in mock.reqs if r.get("tools"))
        mock.srv.shutdown()
    for b in ("same_project_new_session", "other_project_new_session"):
        out[b + "_rate"] = sum(x["hit"] for x in out[b]) / len(out[b])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", required=True, help="dsh 命令，例如 'node --expose-internals .../cli.js' 或 'dsh.cmd'")
    ap.add_argument("--plugin-patch", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--real", action="store_true")
    args = ap.parse_args()
    args.cli_cmd = args.cli.split()
    res = {}
    for name, flag in (("without_plugin", False), ("with_plugin", True)):
        work = tempfile.mkdtemp(prefix=f"ab-{name}-")
        try:
            res[name] = condition(args, flag, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        print(name, json.dumps({k: v for k, v in res[name].items() if not isinstance(v, list) or k == "tools_seen"}, ensure_ascii=False))
    json.dump(res, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


main()
