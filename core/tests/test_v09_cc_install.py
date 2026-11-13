"""v0.9 K4：Claude Code 插件打包 + cc-install 合并/幂等/卸载。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from soulcore.cc_install import EVENTS, install, merge_settings, remove_settings, settings_path

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(CORE)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.cwd = os.path.join(self.tmp, "proj")
        os.makedirs(self.home)
        os.makedirs(self.cwd)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, scope="user"):
        return settings_path(scope, cwd=self.cwd, home=self.home)


class InstallTest(Base):
    def settings(self, scope="user"):
        with open(self.path(scope), encoding="utf-8") as f:
            return json.load(f)

    def test_print_mode_writes_nothing(self):
        code, text = install(scope="user", home=self.home)
        self.assertEqual(code, 0)
        self.assertIn("SessionStart", text)
        self.assertIn("claude mcp add soulcore", text)
        self.assertFalse(os.path.exists(self.path()), "默认只打印，不能动用户文件")

    def test_apply_creates_all_events(self):
        code, _ = install(scope="user", apply=True, home=self.home)
        self.assertEqual(code, 0)
        data = self.settings()
        for event, *_ in EVENTS:
            self.assertIn(event, data["hooks"])
        self.assertEqual(len(data["hooks"]["SessionStart"]), 1)
        cmd = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn("cc-hook session-start", cmd)
        self.assertEqual(data["hooks"]["SessionStart"][0]["matcher"], "startup|resume|clear|compact|fork")
        self.assertEqual(data["hooks"]["PreCompact"][0]["hooks"][0]["timeout"], 60)

    def test_apply_is_idempotent(self):
        install(scope="user", apply=True, home=self.home)
        with open(self.path(), encoding="utf-8") as f:
            first = f.read()
        code, text = install(scope="user", apply=True, home=self.home)
        self.assertEqual(code, 0)
        self.assertIn("没有变化", text)
        with open(self.path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), first, "连续 apply 两次结果要一样")

    def test_apply_keeps_user_hooks(self):
        path = self.path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        user = {"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "echo bye"}]}],
        }, "model": "opus"}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(user, f)
        install(scope="user", apply=True, home=self.home)
        data = self.settings()
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"]["PreToolUse"], user["hooks"]["PreToolUse"])
        self.assertEqual(len(data["hooks"]["Stop"]), 2, "用户自己的 Stop hook 要留着")
        self.assertTrue(any("echo bye" in json.dumps(g) for g in data["hooks"]["Stop"]))

    def test_backup_created(self):
        path = self.path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"model": "opus"}, f)
        code, text = install(scope="user", apply=True, home=self.home)
        self.assertEqual(code, 0)
        self.assertIn("已备份", text)
        backups = [x for x in os.listdir(os.path.dirname(path)) if ".bak-" in x]
        self.assertEqual(len(backups), 1)

    def test_uninstall_only_removes_ours(self):
        path = self.path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo bye"}]}]}}, f)
        install(scope="user", apply=True, home=self.home)
        code, text = install(scope="user", uninstall=True, home=self.home)
        self.assertEqual(code, 0)
        data = self.settings()
        self.assertNotIn("SessionStart", data["hooks"])
        self.assertEqual(len(data["hooks"]["Stop"]), 1)
        self.assertIn("echo bye", json.dumps(data["hooks"]["Stop"]))
        self.assertNotIn("soulcore", json.dumps(data, ensure_ascii=False))

    def test_uninstall_without_file_is_clean(self):
        code, _ = install(scope="user", uninstall=True, home=self.home)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.path()))

    def test_invalid_json_refused(self):
        path = self.path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{这不是 JSON")
        code, text = install(scope="user", apply=True, home=self.home)
        self.assertEqual(code, 2)
        self.assertIn("不是合法 JSON", text)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{这不是 JSON", "坏文件不能被动过")

    def test_project_scope(self):
        code, _ = install(scope="project", apply=True, cwd=self.cwd, home=self.home)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.cwd, ".claude", "settings.json")))

    def test_merge_pure_functions(self):
        groups = {"X": {"hooks": [{"type": "command", "command": "soulcore cc-hook x"}]}}
        data = merge_settings({"hooks": {"X": [{"hooks": [{"type": "command", "command": "old soulcore cmd"}]},
                                             {"hooks": [{"type": "command", "command": "other"}]}]}}, groups)
        self.assertEqual(len(data["hooks"]["X"]), 2)
        self.assertFalse(any("old soulcore" in json.dumps(g) for g in data["hooks"]["X"]))
        self.assertEqual(remove_settings(data)["hooks"]["X"][0]["hooks"][0]["command"], "other")


class PluginPackTest(Base):
    def load(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return json.load(f)

    def test_plugin_manifest(self):
        m = self.load(".claude-plugin/plugin.json")
        self.assertEqual(m["name"], "soulcore")
        self.assertIn("version", m)
        self.assertIn("description", m)
        self.assertIsInstance(m["author"], dict)
        self.assertTrue(m["author"].get("name"))
        self.assertTrue(m["homepage"].startswith("https://"))
        with open(os.path.join(ROOT, "package.json"), encoding="utf-8") as f:
            self.assertEqual(m["version"], json.load(f)["version"], "插件版本要和 package.json 一致")

    def test_marketplace_manifest(self):
        m = self.load(".claude-plugin/marketplace.json")
        self.assertEqual(m["name"], "dsh-soulcore")
        self.assertTrue(m["owner"]["name"])
        entry = [p for p in m["plugins"] if p["name"] == "soulcore"][0]
        self.assertEqual(entry["source"], "./")
        self.assertIn("description", entry)

    def test_hooks_json(self):
        h = self.load("hooks/hooks.json")
        hooks = h["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "UserPromptSubmit", "Stop", "PreCompact", "SessionEnd"})
        wants = {"SessionStart": ("session-start", 10), "UserPromptSubmit": ("user-prompt", 5),
                 "Stop": ("stop", 5), "PreCompact": ("pre-compact", 60), "SessionEnd": ("session-end", 10)}
        for event, (sub, timeout) in wants.items():
            group = hooks[event][0]
            handler = group["hooks"][0]
            self.assertEqual(handler["type"], "command")
            self.assertIn("${CLAUDE_PLUGIN_ROOT}", handler["command"])
            self.assertIn(f"bin/soulcore-hook {sub}", handler["command"])
            self.assertEqual(handler["timeout"], timeout)

    def test_mcp_json(self):
        m = self.load(".mcp.json")
        server = m["mcpServers"]["soulcore"]
        self.assertIn("bin/soulcore-mcp", server["command"])
        self.assertTrue(server["args"] == [])

    def test_launchers(self):
        for name in ("soulcore-hook", "soulcore-mcp"):
            path = os.path.join(ROOT, "bin", name)
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertTrue(text.startswith("#!/bin/sh"), f"{name} 要是 POSIX sh")
            self.assertIn("SOULCORE_PYTHON", text)
            self.assertIn("dsh-runtimes", text, "要能回落到 DSH 自带的 Python")
            self.assertIn("core/run.py", text)
        with open(os.path.join(ROOT, "bin", "soulcore-hook"), encoding="utf-8") as f:
            self.assertIn("exit 0", f.read(), "hook 找不到 Python 时要 fail-open")

    def test_commands(self):
        for name in ("recall", "remember", "memories", "mood"):
            path = os.path.join(ROOT, "commands", name + ".md")
            self.assertTrue(os.path.isfile(path), name)
            with open(path, encoding="utf-8") as f:
                self.assertIn("---", f.read(20))

    def test_package_files_include_new_dirs(self):
        with open(os.path.join(ROOT, "package.json"), encoding="utf-8") as f:
            files = json.load(f)["files"]
        for rel in ("bin", "hooks", "commands", ".claude-plugin", "docs"):
            self.assertIn(rel, files, f"npm 包里要带上 {rel}")


if __name__ == "__main__":
    unittest.main()
