"""简单限流：每个 key 最小间隔 + 滑动窗口总量"""
from __future__ import annotations
import threading, time
from collections import deque


class RateLimiter:
    def __init__(self, min_interval: float, max_per_window: int, window: float, clock=time.monotonic):
        self.min_interval, self.max_n, self.window = min_interval, max_per_window, window
        self.clock = clock
        self.last: dict[str, float] = {}
        self.hist: deque[float] = deque()
        self.lock = threading.Lock()

    def wait_time(self, key: str = "") -> float:
        now = self.clock()
        with self.lock:
            while self.hist and now - self.hist[0] > self.window:
                self.hist.popleft()
            w = 0.0
            if key in self.last:
                w = max(w, self.min_interval - (now - self.last[key]))
            if len(self.hist) >= self.max_n:
                w = max(w, self.window - (now - self.hist[0]))
            return max(0.0, w)

    def mark(self, key: str = "") -> None:
        now = self.clock()
        with self.lock:
            self.last[key] = now
            self.hist.append(now)

    def acquire(self, key: str = "", max_wait: float = 120.0) -> bool:
        """阻塞等待直到允许发送；等太久返回 False（直接丢弃，避免积压后集中刷屏）"""
        w = self.wait_time(key)
        if w > max_wait:
            return False
        if w > 0:
            time.sleep(w)
        self.mark(key)
        return True
