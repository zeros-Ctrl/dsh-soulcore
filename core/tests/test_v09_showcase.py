"""v0.9 K5：showcase 一键体验（离线、不乱码、30 秒内）。"""
import os
import subprocess
import sys
import time
import unittest

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ShowcaseTest(unittest.TestCase):
    def test_showcase_runs_offline(self):
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, "-m", "soulcore", "--mock", "showcase"],
                           cwd=CORE, capture_output=True, text=True, encoding="utf-8",
                           timeout=120, env=env)
        cost = time.monotonic() - t0
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertLess(cost, 30, f"showcase 要 30 秒内跑完（实际 {cost:.1f}s）")
        out = p.stdout
        self.assertIn("SQLite", out, "第一段要演示'新会话还记得'")
        self.assertIn("阿里云", out, "第二段要演示'知道哪些过时了'")
        self.assertIn("已过时", out)
        self.assertIn("心情", out, "第三段要演示心情变化")
        self.assertIn("夸她一次", out)
        # 不乱码：中文正常出现在 stdout 里（解码按 UTF-8 已经由 encoding 参数保证）
        self.assertNotIn("\ufffd", out)


if __name__ == "__main__":
    unittest.main()
