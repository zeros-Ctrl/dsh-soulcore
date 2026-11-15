"""v0.9 K2：Claude Code hook 适配器（cc-hook）。

用 subprocess 跑真实的 `python -I core/run.py cc-hook <event>`，后端是临时 SOULCORE_HOME
下用 --mock 启动的真实后端；不碰用户的常驻进程和 ~/.dsh/soulcore 数据。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from soulcore.cc_hook import (last_assistant_text, make_event, project_name, transcript_reply)
from soulcore.client import Backend, pid_alive, resolve_home
from soulcore.config import load_config
from soulcore.lockfile import InstanceLock

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_PY = os.path.join(CORE, "run.py")
FIX = os.path.join(CORE, "tests", "fixtures")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Cleanups run in reverse registration order, including after failed setUp.
        # A subclass's backend must finish shutting down before its home is removed.
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class TranscriptTest(Base):
    def test_normal_conversation(self):
        text = transcript_reply(os.path.join(FIX, "cc_transcript_normal.jsonl"))
        self.assertIn("加了三个用例", text)
        self.assertIn("导航参数没动", text)
        self.assertNotIn("先看一下现有的测试结构", text, "最后一条用户输入之前的回复不算本轮")
        self.assertIn("\n", text)

    def test_tool_calls_are_skipped(self):
        text = transcript_reply(os.path.join(FIX, "cc_transcript_tools.jsonl"))
        self.assertTrue(text.endswith("测试全过了，用时 3.2 秒。"), text)
        self.assertNotIn("3 passed", text, "tool_result 里的内容不能当成她的话")

    def test_subagent_and_compact_summary_skipped(self):
        text = transcript_reply(os.path.join(FIX, "cc_transcript_subagent.jsonl"))
        self.assertIn("派一个子 agent", text)
        self.assertIn("过时的有三个：requests、numpy、flask", text)
        self.assertNotIn("devDependencies", text, "子 agent 的内部推理不能混进来")
        self.assertNotIn("compact summary", text)

    def test_bad_lines_skipped(self):
        path = os.path.join(self.tmp, "t.jsonl")
        with open(os.path.join(FIX, "cc_transcript_normal.jsonl"), encoding="utf-8") as f:
            good = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write("{坏行不是 JSON\n")
            f.write(good)
            f.write("\n{\"type\": \"assistant\"} 也坏了\n")
        self.assertIn("导航参数没动", transcript_reply(path))

    def test_missing_file_and_no_user(self):
        self.assertEqual(transcript_reply(os.path.join(self.tmp, "nope.jsonl")), "")
        self.assertEqual(transcript_reply(None), "")
        path = os.path.join(self.tmp, "only_assistant.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"你好"}]}}\n')
        self.assertEqual(transcript_reply(path), "")

    def test_truncated_to_4000(self):
        path = os.path.join(self.tmp, "long.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"type":"user","message":{"role":"user","content":"说点长的"}}\n')
            f.write(json.dumps({"type": "assistant", "message": {
                "role": "assistant", "content": [{"type": "text", "text": "长" * 5000}]}}) + "\n")
        self.assertLessEqual(len(transcript_reply(path)), 4000)

    def test_last_assistant_message_preferred(self):
        payload = {"last_assistant_message": "官方字段里的最终回复", "transcript_path": "不存在"}
        self.assertEqual(last_assistant_text(payload), "官方字段里的最终回复")


class ProjectNameTest(Base):
    def test_plain_dir_and_windows_path(self):
        self.assertEqual(project_name("/home/dev/myrobot"), "myrobot")
        self.assertEqual(project_name("C:\\Users\\dev\\myrobot"), "myrobot")
        self.assertEqual(project_name("C:\\Users\\dev\\myrobot\\"), "myrobot")
        self.assertEqual(project_name("C:\\"), "default")

    def test_git_root_wins(self):
        repo = os.path.join(self.tmp, "work", "shop-api")
        sub = os.path.join(repo, "src", "api")
        os.makedirs(sub)
        os.makedirs(os.path.join(repo, ".git"))
        self.assertEqual(project_name(sub), "shop-api")

    def test_git_file_counts(self):
        repo = os.path.join(self.tmp, "worktrees", "wt-one")
        os.makedirs(repo)
        with open(os.path.join(repo, ".git"), "w", encoding="utf-8") as f:
            f.write("gitdir: /somewhere\n")
        self.assertEqual(project_name(repo), "wt-one")

    def test_session_fallback(self):
        self.assertEqual(project_name(None, "abc123"), "session-abc123")
        self.assertEqual(project_name(None, "session-xyz"), "session-xyz")
        self.assertEqual(project_name(None, ""), "default")

    def test_event_shape(self):
        cfg = {"owner": {"name": "丁真"}}
        ev = make_event({"cwd": "/home/dev/demo", "session_id": "s1"}, cfg, "帮我看看")
        self.assertEqual(ev["platform"], "local")
        self.assertEqual(ev["scene"], "project")
        self.assertEqual(ev["channel_id"], "demo")
        self.assertEqual(ev["user_id"], "owner")
        self.assertEqual(ev["user_name"], "丁真")
        self.assertEqual(ev["text"], "帮我看看")
        self.assertEqual(ev["raw"]["client"], "claude-code")
        self.assertIsInstance(ev["ts"], float)


class CcHookTest(Base):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        with patch.dict(os.environ, {"SOULCORE_HOME": self.home}):
            resolve_home()
        self.backend = Backend(home=self.home, mock=True)
        self.addCleanup(self.stop_backend)
        found = self.backend.ensure(start=True, wait_s=20)
        log_path = os.path.join(self.home, "logs", "serve.log")
        startup_log = ""
        if found is None and os.path.isfile(log_path):
            with open(log_path, encoding="utf-8", errors="replace") as f:
                startup_log = f.read()[-4000:]
        self.assertIsNotNone(found, f"mock 后端没有启动：{log_path}\n{startup_log}")
        self.cwd = os.path.join(self.tmp, "myrobot")
        os.makedirs(self.cwd)

    def stop_backend(self):
        info = InstanceLock.read(self.backend.lock_path) or {}
        self.backend.shutdown()
        pid = info.get("pid")
        deadline = time.monotonic() + 15.0
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(pid_alive(pid), f"mock 后端没有完成退出：pid={pid}")

    def hook(self, event, payload, home=None, stdin=None, timeout=40):
        env = {**os.environ, "SOULCORE_HOME": home or self.home,
               "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        data = stdin if stdin is not None else json.dumps(payload, ensure_ascii=False)
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, "-I", RUN_PY, "--config", os.path.join(self.home, "config.json"),
                            "cc-hook", event], input=data, text=True, capture_output=True,
                           encoding="utf-8", timeout=timeout, env=env, cwd=CORE)
        return p, time.monotonic() - t0

    def set_config(self, **cc):
        path = os.path.join(self.home, "config.json")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("cc", {}).update(cc)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def dsh_event(self, text="", kind="project", channel="myrobot"):
        return {"platform": "local", "scene": "project", "channel_id": channel, "user_id": "owner",
                "user_name": "主人", "text": text, "mentions_self": True, "ts": 1.8e9}

    def remember(self, content, kind="decision", channel="myrobot"):
        return self.backend.request("POST", "/v1/memory/remember",
                                    {"event": self.dsh_event(content, channel=channel),
                                     "content": content, "kind": kind})

    def search(self, q, path="project/myrobot"):
        return self.backend.request("GET", f"/v1/memory/search?q={quote(q)}&path={quote(path)}&touch=0")

    def test_user_prompt_memory_style(self):
        self.remember("项目的检索层决定用 SQLite+FTS5，不上向量库")
        p, _ = self.hook("user-prompt", {"session_id": "s1", "cwd": self.cwd,
                                         "hook_event_name": "UserPromptSubmit",
                                         "prompt": "我们检索层最后用的是什么来着"})
        self.assertEqual(p.returncode, 0, p.stderr)
        body = json.loads(p.stdout)
        self.assertEqual(body["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        ctx = body["hookSpecificOutput"]["additionalContext"]
        self.assertIn("SQLite", ctx)
        self.assertNotIn("内心状态", ctx, "memory 风格不该带心情")
        self.assertNotIn("保持人设", ctx)

    def test_companion_style_has_mood(self):
        self.set_config(style="companion")
        p, _ = self.hook("user-prompt", {"session_id": "s2", "cwd": self.cwd,
                                        "hook_event_name": "UserPromptSubmit",
                                        "prompt": "今天继续写代码"})
        self.assertEqual(p.returncode, 0, p.stderr)
        ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("内心状态", ctx)

    def test_session_start_companion_and_compact(self):
        self.set_config(style="companion")
        self.remember("决定用 SQLite 做检索层")
        p, _ = self.hook("session-start", {"session_id": "s3", "cwd": self.cwd,
                                           "hook_event_name": "SessionStart", "source": "startup"})
        self.assertEqual(p.returncode, 0, p.stderr)
        ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("保持人设", ctx, "companion 模式要给稳定块")
        self.assertIn("[soulcore·本项目长期记忆]", ctx)
        self.assertIn("SQLite", ctx)

        p, _ = self.hook("session-start", {"session_id": "s3", "cwd": self.cwd,
                                           "hook_event_name": "SessionStart", "source": "compact"})
        ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[soulcore·压缩前的要点]", ctx)
        self.assertNotIn("[soulcore·本项目长期记忆]", ctx)

    def test_session_start_memory_style_has_no_stable(self):
        self.remember("决定用 SQLite 做检索层")
        p, _ = self.hook("session-start", {"session_id": "s4", "cwd": self.cwd,
                                           "hook_event_name": "SessionStart", "source": "startup"})
        ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("保持人设", ctx)
        self.assertIn("[soulcore·本项目长期记忆]", ctx)

    def test_stop_writes_reply(self):
        p, _ = self.hook("stop", {"session_id": "s5", "cwd": self.cwd,
                                  "hook_event_name": "Stop", "stop_hook_active": False,
                                  "transcript_path": os.path.join(FIX, "cc_transcript_normal.jsonl")})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "", "stop 不输出任何内容")
        items = self.search("三个用例 导航参数")
        self.assertTrue(any("导航参数没动" in x["content"] for x in items), items)

    def test_stop_hook_active_does_nothing(self):
        self.hook("stop", {"session_id": "s6", "cwd": self.cwd, "hook_event_name": "Stop",
                           "stop_hook_active": True,
                           "last_assistant_message": "不该被写进记忆的回复"})
        self.assertFalse(any("不该被写进记忆" in x["content"] for x in self.search("不该被写进记忆")))

    def test_stop_uses_last_assistant_message(self):
        self.hook("stop", {"session_id": "s7", "cwd": self.cwd, "hook_event_name": "Stop",
                           "last_assistant_message": "最终回复从官方字段来"})
        self.assertTrue(any("最终回复从官方字段来" in x["content"] for x in self.search("最终回复 官方字段")))

    def test_pre_compact_flushes(self):
        self.backend.request("POST", "/v1/hooks/context", {"event": self.dsh_event(
            "记住，部署用 Docker Compose 起三个服务", channel="myrobot")})
        p, _ = self.hook("pre-compact", {"session_id": "s8", "cwd": self.cwd,
                                         "hook_event_name": "PreCompact", "trigger": "auto"})
        self.assertEqual(p.returncode, 0, p.stderr)
        items = self.search("Docker Compose 部署")
        self.assertTrue(any(x["kind"] != "turn" and "Docker" in x["content"] for x in items), items)

    def test_backend_absent_is_fast_and_silent(self):
        absent = os.path.join(self.tmp, "no-backend")
        os.makedirs(absent)
        p, cost = self.hook("user-prompt", {"session_id": "s9", "cwd": self.cwd,
                                            "hook_event_name": "UserPromptSubmit",
                                            "prompt": "后端没起的时候应该安静"}, home=absent)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")
        self.assertLess(cost, 3.0, f"后端不存在时 hook 不能拖慢输入（{cost:.2f}s）")

    def test_invalid_json_fails_open_and_logs(self):
        p, cost = self.hook("user-prompt", None, stdin="{这不是 JSON")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")
        log_path = os.path.join(self.home, "logs", "cc-hook.log")
        self.assertTrue(os.path.exists(log_path))
        with open(log_path, encoding="utf-8") as f:
            self.assertIn("user-prompt", f.read())

    def test_log_rotation(self):
        log_dir = os.path.join(self.home, "logs")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "cc-hook.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x" * (1024 * 1024 + 10))
        p, _ = self.hook("user-prompt", None, stdin="bad json")
        self.assertEqual(p.returncode, 0)
        self.assertTrue(os.path.exists(path + ".1"), "超过 1MB 要轮转")

    def test_shared_project_memory_with_dsh(self):
        """DSH 风格写入、Claude Code 风格读出：同一个 project/<名> 分支。"""
        self.remember("博客以前部署在香橙派，后来迁到阿里云了", kind="fact")
        p, _ = self.hook("user-prompt", {"session_id": "s10", "cwd": self.cwd,
                                         "hook_event_name": "UserPromptSubmit",
                                         "prompt": "博客现在部署在哪"})
        ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("阿里云", ctx)
        # 反向：Claude Code 的 Stop 写进去的回复，DSH 的检索也能看到
        self.hook("stop", {"session_id": "s10", "cwd": self.cwd, "hook_event_name": "Stop",
                           "last_assistant_message": "确认过：博客在阿里云上跑着"})
        items = self.backend.request("GET", f"/v1/memory/search?q={quote('阿里云 博客')}&path=project/myrobot")
        self.assertTrue(any("阿里云上跑着" in x["content"] for x in items), items)


if __name__ == "__main__":
    unittest.main()
