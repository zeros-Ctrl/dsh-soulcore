"""python -m unittest discover -s tests -v   （全部离线，不需要 API key / QQ / B站）"""
import json, math, os, shutil, sys, tempfile, threading, time, unittest, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from soulcore.config import load_config
from soulcore.engine import Engine
from soulcore.events import Event
from soulcore.memory import MemoryStore
from soulcore.emotion import Emotion, Params
from soulcore.llm import MockLLM
from soulcore.adapters.onebot import OneBotAdapter, parse_segments
from soulcore.adapters.bilibili import BiliAdapter, BiliPaused
from soulcore.demo import FakeBiliApi


class Clock:
    def __init__(self, t=1.8e9): self.t = t
    def __call__(self): return self.t
    def skip(self, h): self.t += h * 3600


def make_engine(tmp, clock, **over):
    cfg, _ = load_config(None)
    cfg["data_dir"] = tmp
    cfg["memory"]["extract_every"] = 3
    for k, v in over.items():
        cfg[k].update(v) if isinstance(v, dict) else cfg.__setitem__(k, v)
    return Engine(cfg, tmp, llm=MockLLM(), clock=clock), cfg


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestRouting(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(Event("qq", "group", "114514", "1").path, "chat/qq/group/114514")
        self.assertEqual(Event("qq", "private", "10001", "10001").path, "chat/qq/private/10001")
        self.assertEqual(Event("bilibili", "dm", "42", "42").path, "chat/bilibili/dm/42")
        self.assertEqual(Event("bilibili", "comment", "BV1xx", "42").path, "chat/bilibili/comment/BV1xx")
        self.assertEqual(Event("local", "project", "毕设", "o").path, "project/毕设")
        self.assertEqual(Event("qq", "group", "1", "7").person_path, "core/people/qq/7")
        self.assertEqual(Event("local", "project", "a/b:c", "o").path, "project/a_b_c")


class TestMemory(Base):
    def test_gates(self):
        s = MemoryStore(self.tmp, clock=self.clock)
        self.assertIsNone(s.write("chat/x", "turn", "嗯", 0.01))                   # 输入门：太不重要，不存
        a = s.write("project/p", "decision", "决定用 SQLite 做检索，因为部署轻", 0.8)
        b = s.write("project/p", "decision", "决定用 SQLite 做检索，因为部署轻。", 0.9)  # 去重
        self.assertEqual(a, b)
        it = s.db.execute("SELECT * FROM items WHERE id=?", (a,)).fetchone()
        w0 = s.weight(it)
        self.clock.skip(24 * 365)                                                   # 遗忘门：一年后衰减到 1/e
        self.assertAlmostEqual(s.weight(it) / w0, math.exp(-1), places=3)
        t = s.write("chat/qq/group/1", "turn", "今天吃什么", 0.2)
        self.clock.skip(24 * 30)
        s.maintain()
        st = s.db.execute("SELECT status FROM items WHERE id=?", (t,)).fetchone()[0]
        self.assertEqual(st, "cold")                                                # 冷却而不是删除
        self.assertEqual(s.search("吃什么", "chat/qq/group/1"), [])
        self.assertEqual(len(s.search("吃什么", "chat/qq/group/1", include_cold=True)), 1)

    def test_retrieval_refresh(self):
        s = MemoryStore(self.tmp, clock=self.clock)
        i = s.write("chat/qq/private/1", "fact", "阿明的生日是3月14日", 0.9)
        self.clock.skip(24 * 50)
        r = s.search("生日", "chat/qq/private/1")
        self.assertEqual(r[0].id, i)
        row = s.db.execute("SELECT * FROM items WHERE id=?", (i,)).fetchone()
        self.assertEqual(row["access_count"], 1)
        self.assertAlmostEqual(row["last_access"], self.clock())                    # 复习：衰减计时被刷新

    def test_hierarchy_and_privacy(self):
        s = MemoryStore(self.tmp, clock=self.clock)
        s.write("chat/qq/private/1", "fact", "阿明的密码提示是小猫", 0.9)
        s.write("core/people/qq/1", "fact", "阿明喜欢小猫", 0.9)
        s.write("chat/qq/group/9", "turn", "群里有人说小猫好可爱", 0.5)
        s.write("chat/qq/group/9", "fact", "群里约定周五一起看小猫视频", 0.7)
        s.write("project/p", "decision", "项目里的小猫图标先不改", 0.8)
        # 别人在另一个群里问：看不到阿明的私聊/档案、看不到别的群的原话、看不到项目
        r = s.search("小猫", "chat/qq/group/8", allow_projects=False)
        self.assertEqual({x.content for x in r}, {"群里约定周五一起看小猫视频"})
        # 阿明自己私聊：能看到自己的私聊和自己的档案
        r = s.search("小猫", "chat/qq/private/1", extra_paths=["core/people/qq/1"], allow_projects=False)
        self.assertIn("阿明喜欢小猫", {x.content for x in r})
        self.assertIn("阿明的密码提示是小猫", {x.content for x in r})
        # 公开场景（B站评论区）：其他聊天分支一律不可见
        r = s.search("小猫", "chat/bilibili/comment/BV1", public=True, allow_projects=False)
        self.assertEqual(r, [])
        # 当前分支优先
        r = s.search("小猫", "chat/qq/group/9")
        self.assertEqual(r[0].path, "chat/qq/group/9")


class TestEmotion(Base):
    def test_rate_limit_under_barrage(self):
        e = Emotion(Params(), seed=1)
        prev = e.mood; mx = 0
        for t in range(1, 600):
            if t % 5 == 0:
                e.event("scold" if t < 300 else "gift", t=t, amp=-3 if t < 300 else 3)
            e.advance(t)
            mx = max(mx, abs(e.mood - prev)); prev = e.mood
        self.assertLessEqual(mx, Params().max_rate + 1e-9)
        self.assertTrue(0 < e.mood < 1)

    def test_persist_across_restart(self):
        eng, _ = make_engine(self.tmp, self.clock)
        eng.system_event("scold"); self.clock.skip(0.5)
        m1 = eng.state()["mood"]; eng.close()
        eng2, _ = make_engine(self.tmp, self.clock)
        self.assertAlmostEqual(eng2.state()["mood"], m1, places=6)
        self.clock.skip(3)                                      # 3 小时后（还没到孤独阈值）：情绪回升
        self.assertGreater(eng2.state()["mood"], m1)
        eng2.close()


class TestEngine(Base):
    def test_pipeline_and_extract(self):
        eng, _ = make_engine(self.tmp, self.clock)
        mk = lambda t: Event("qq", "private", "1", "1", "阿明", t, ts=self.clock())
        for t in ["你好", "记住我生日是3月14号", "我喜欢鬼畜区"]:
            self.assertTrue(eng.handle(mk(t))); self.clock.t += 60
        eng.flush()
        notes = eng.store.by_path("core/people/qq/1")
        self.assertTrue(any("3月14" in n.content for n in notes))
        self.assertFalse(any(eng.name in n.content.split("说：")[0] for n in notes))   # 不把她自己的话当事实
        ctx = eng.prepare(mk("我生日哪天"))
        self.assertIn("3月14", ctx["system"])
        self.assertIn("## 你的心情", ctx["system"])
        eng.close()

    def test_pre_compact(self):
        eng, _ = make_engine(self.tmp, self.clock)
        ev = Event("local", "project", "毕设", "owner", "阿明", "")
        n = eng.pre_compact(ev, [{"role": "user", "content": "我们决定不用向量库，因为部署要轻"},
                                 {"role": "assistant", "content": "好的"}])
        self.assertGreaterEqual(n, 1)
        self.assertTrue(eng.store.by_path("project/毕设", kinds=["decision"]))
        eng.close()

    def test_balance_events(self):
        eng, _ = make_engine(self.tmp, self.clock)
        eng.llm.balance_value = 50; self.assertIsNone(eng.check_balance())
        eng.llm.balance_value = 3; self.assertEqual(eng.check_balance(), "balance_low")
        eng.llm.balance_value = 0; self.assertEqual(eng.check_balance(), "balance_out")
        eng.llm.balance_value = 40; self.assertEqual(eng.check_balance(), "recharge")
        eng.close()


class FakeOneBot:
    """假的 OneBot HTTP API（协议端），记录收到的发送请求"""
    def __init__(self):
        self.calls = []
        outer = self
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.calls.append((self.path, json.loads(self.rfile.read(n)), self.headers.get("Authorization")))
                b = b'{"status":"ok","retcode":0,"data":{"message_id":1}}'
                self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
                self.wfile.write(b)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]


def post(url, obj, token=None):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
        return r.status, (json.loads(body) if body else None)


class TestOneBot(Base):
    def test_cq_parse(self):
        segs = parse_segments("[CQ:at,qq=999] 你好&#91;doge&#93;[CQ:image,file=a.jpg]")
        self.assertEqual([s["type"] for s in segs], ["at", "text", "image"])
        self.assertEqual(segs[1]["data"]["text"], " 你好[doge]")

    def test_http_roundtrip(self):
        from soulcore.server import serve
        fake = FakeOneBot()
        eng, cfg = make_engine(self.tmp, time.time,
                               qq={"enabled": True, "api_base": fake.base, "access_token": "tk", "self_id": "999",
                                   "group_whitelist": ["114514"], "typing_delay": False, "min_interval_s": 0},
                               server={"port": 0, "tick_every_min": 999})
        httpd, onebot, stop = serve(eng, cfg, block=False)
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        msg = lambda gid, at, text, mid: {"post_type": "message", "message_type": "group", "self_id": 999,
            "group_id": gid, "user_id": 20002, "message_id": mid, "sender": {"card": "老王"},
            "message": ([{"type": "at", "data": {"qq": "999"}}] if at else []) + [{"type": "text", "data": {"text": text}}]}
        self.assertEqual(post(base + "/onebot", msg(114514, False, "今晚开黑吗", 1))[0], 204)   # 旁听
        self.assertEqual(post(base + "/onebot", msg(777, True, "非白名单群", 2))[0], 204)        # 忽略
        self.assertEqual(post(base + "/onebot", msg(114514, True, "你好呀", 3))[0], 204)         # 回复
        onebot.q.join()
        self.assertEqual(len(fake.calls), 1)
        path, body, auth = fake.calls[0]
        self.assertEqual(path, "/send_group_msg"); self.assertEqual(auth, "Bearer tk")
        self.assertEqual(body["group_id"], 114514)
        self.assertEqual(body["message"][0], {"type": "reply", "data": {"id": "3"}})
        turns = eng.store.recent_turns("chat/qq/group/114514")
        self.assertEqual([t.role for t in turns], ["user", "user", "assistant"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "memory", "chat", "qq", "group", "777")))
        # 钩子接口
        ev = {"platform": "local", "scene": "project", "channel_id": "p", "user_id": "o", "text": "你好"}
        st, ctx = post(base + "/v1/hooks/before", {"event": ev})
        self.assertIn("## 你的心情", ctx["system"]); self.assertEqual(ctx["history"][-1]["content"], "你好")
        self.assertEqual(post(base + "/v1/hooks/after", {"event": ev, "reply": "嗨"})[1], {"ok": True})
        st, res = post(base + "/v1/event", {"name": "praise"}); self.assertIn("mood", res)
        stop.set(); onebot.stop(); httpd.shutdown(); httpd.server_close(); fake.srv.shutdown(); eng.close()


class RiskApi(FakeBiliApi):
    def send_comment(self, *a):
        e = Exception("风控"); e.code = -352; raise e


class TestBili(Base):
    def _item(self, i, text="@小汐 你好", uid=4242):
        return {"id": i, "at_time": self.clock(), "user": {"mid": uid, "nickname": "路人"},
                "item": {"type": "reply", "business_id": 1, "subject_id": 1145, "source_id": 9000 + i,
                         "root_id": 0, "source_content": text}}

    def test_flow(self):
        eng, cfg = make_engine(self.tmp, self.clock)
        api = FakeBiliApi(); api.at_items = [self._item(1)]
        ad = BiliAdapter(eng, cfg["bilibili"], api=api)
        self.assertEqual(ad.poll_once(), [])                    # 首次：只记游标，不回历史
        api.at_items = [self._item(1), self._item(2)]
        evs = ad.poll_once()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].path, "chat/bilibili/comment/BV_demo1145")
        self.assertTrue(evs[0].is_stranger)
        pend = ad.queue.pending(); self.assertEqual(len(pend), 1)
        self.assertEqual(api.sent, [])                          # 默认不自动发
        n_before = len(eng.store.recent_turns(evs[0].path))
        ad.approve(pend[0])
        self.assertEqual(api.sent[0][:3], ("comment", 1145, 9002))
        self.assertEqual(len(eng.store.recent_turns(evs[0].path)), n_before + 1)   # 发出后才写入记忆
        self.assertEqual(ad.poll_once(), [])                    # 不重复处理
        eng.close()

    def test_risk_pause(self):
        eng, cfg = make_engine(self.tmp, self.clock)
        api = RiskApi(); ad = BiliAdapter(eng, {**cfg["bilibili"], "auto_send": True}, api=api)
        ad.poll_once(); api.at_items = [self._item(5)]
        with self.assertRaises(BiliPaused):
            ad.poll_once()
        self.assertEqual(ad.paused["code"], -352)
        with self.assertRaises(BiliPaused):                     # 暂停后不再请求
            ad.poll_once()
        ad.resume(); self.assertIsNone(ad.paused)
        eng.close()


if __name__ == "__main__":
    unittest.main()
