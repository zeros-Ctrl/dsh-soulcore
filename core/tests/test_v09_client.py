"""v0.9 K1：数据目录解析 + 后端自启动（soulcore.client.Backend）。

用临时的 SOULCORE_HOME + --mock 起真实后端，不碰用户的常驻进程和真实数据。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from soulcore.client import Backend, BackendUnavailable, pid_alive, resolve_home

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ResolveHomeTest(Base):
    def test_env_home_creates_config_and_persona(self):
        home = os.path.join(self.tmp, "soul")
        with patch.dict(os.environ, {"SOULCORE_HOME": home}):
            got = resolve_home()
        self.assertEqual(got, os.path.abspath(home))
        self.assertTrue(os.path.isfile(os.path.join(got, "config.json")))
        self.assertTrue(os.path.isfile(os.path.join(got, "persona.md")))
        with open(os.path.join(got, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["llm"]["api_key"], "", "默认配置里 api_key 要留空")

    def test_uses_dsh_home_when_present(self):
        fake_home = os.path.join(self.tmp, "user")
        os.makedirs(os.path.join(fake_home, ".dsh"), exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k != "SOULCORE_HOME"}
        with patch.dict(os.environ, env, clear=True), patch("soulcore.client.user_home", return_value=fake_home):
            self.assertEqual(resolve_home(), os.path.join(fake_home, ".dsh", "soulcore"))

    def test_falls_back_to_dot_soulcore(self):
        fake_home = os.path.join(self.tmp, "user2")
        os.makedirs(fake_home, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k != "SOULCORE_HOME"}
        with patch.dict(os.environ, env, clear=True), patch("soulcore.client.user_home", return_value=fake_home):
            self.assertEqual(resolve_home(), os.path.join(fake_home, ".soulcore"))

    def test_keeps_existing_config(self):
        home = os.path.join(self.tmp, "soul3")
        os.makedirs(home)
        cfg_path = os.path.join(home, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"llm": {"api_key": "sk-keep"}}, f)
        with patch.dict(os.environ, {"SOULCORE_HOME": home}):
            resolve_home()
        with open(cfg_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["llm"]["api_key"], "sk-keep")


class BackendTest(Base):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.backends = []

    def tearDown(self):
        for b in self.backends:
            try:
                b.shutdown()
            except Exception:
                pass
        super().tearDown()

    def backend(self, **kw):
        b = Backend(home=self.home, mock=True, **kw)
        self.backends.append(b)
        return b

    def wait_dead(self, pid, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            if not pid_alive(pid):
                return True
            time.sleep(0.1)
        return False

    def test_ensure_starts_backend_and_shutdown_works(self):
        b = self.backend()
        found = b.ensure(start=True, wait_s=20)
        self.assertIsNotNone(found, "ensure() 应该把后端拉起来")
        base, token = found
        self.assertRegex(base, r"^http://127\.0\.0\.1:\d+$")
        self.assertTrue(token)
        self.assertEqual(b.health()["ok"], True)

        with open(os.path.join(self.home, "soulcore.lock"), encoding="utf-8") as f:
            info = json.load(f)
        self.assertTrue(pid_alive(info["pid"]))
        self.assertTrue(b.shutdown())
        self.assertTrue(self.wait_dead(info["pid"]), "调 /v1/shutdown 之后进程要真的退出")
        self.assertFalse(os.path.exists(os.path.join(self.home, "soulcore.lock")))

    def test_concurrent_ensure_spawns_one_backend(self):
        b = self.backend()
        results = []
        errors = []

        def run():
            try:
                results.append(b.ensure(start=True, wait_s=20))
            except Exception as ex:      # noqa: BLE001
                errors.append(ex)

        threads = [threading.Thread(target=run) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(40)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 5)
        self.assertTrue(all(results), "5 个并发 ensure 都应该拿到后端")
        self.assertEqual(len({r[0] for r in results}), 1, "应该都指向同一个后端")
        log_path = os.path.join(self.home, "logs", "serve.log")
        with open(log_path, encoding="utf-8", errors="replace") as f:
            log = f.read()
        self.assertEqual(log.count("soulcore 服务已启动"), 1, "并发启动只能拉起一个后端进程")

    def test_ensure_without_start_is_quiet(self):
        b = self.backend()
        self.assertIsNone(b.ensure(start=False))
        self.assertFalse(os.path.exists(os.path.join(self.home, "logs", "serve.log")))

    def test_request_without_backend_raises(self):
        b = self.backend()
        with self.assertRaises(BackendUnavailable):
            b.request("GET", "/v1/health", timeout=0.5)

    def test_401_rediscovers_and_retries(self):
        b = self.backend()
        self.assertIsNotNone(b.ensure(start=True, wait_s=20))
        b._token = "stale-token"                     # 模拟常驻进程重启后 token 变了
        self.assertEqual(b.request("GET", "/v1/health", timeout=3)["ok"], True)

    def test_discover_rejects_dead_pid(self):
        b = self.backend()
        with open(os.path.join(self.home, "soulcore.lock"), "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "port": 1, "token": "x"}, f)
        self.assertIsNone(b.discover())


if __name__ == "__main__":
    unittest.main()
