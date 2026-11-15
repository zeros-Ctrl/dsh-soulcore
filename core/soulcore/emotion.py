"""
emotion.py —— 独立人格的情绪动力学（效价 valence + 独立唤醒度 arousal）

核心思想：事件不直接改"情绪值"，而是改"情绪想去的地方"（目标/设定点 r），
情绪值 x 由一个 PID 调节器 + 情绪惯性 去平滑地追踪 r，再加一道限速保险。

  事件 ──► 驱动 d_fast / d_slow (指数衰减, 带习惯化) ──┐
  孤独度 L (太久没人理就慢慢累积) ─────────────────────┤
  性格基线 b (极慢漂移，代表"她本来是什么样的人") ─────┼─► r (logit) ─► 前置滤波 r_f
  内在 OU 噪声 n (慢且小，"莫名其妙的小情绪") ────────┘
        e = r_f - x
        u = Kp·e + ∫Ki·e - Kd·dx/dt          (微分作用在测量值上，避免事件瞬间"微分冲击")
        τ_p·dx/dt = -x + u                   (情绪惯性 + 向中性 0.5 泄漏，所以需要积分项消除稳态误差)
        |Δmood| ≤ max_rate·dt                (限速：相邻时刻差值硬上限)
        mood = sigmoid(x) ∈ (0,1)

完整状态向量 (b, d_fast, d_slow, L, n, r_f, x, ui, v) 只依赖上一时刻 —— 这就是马尔科夫链；
"概率性" 来自两处：事件冲击幅度是随机抽样的（同样被凶，有时只是有点闷），以及内在 OU 噪声。

时间单位：分钟。
"""
from __future__ import annotations
import math, random, json
from dataclasses import dataclass, field, asdict


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


@dataclass
class EventSpec:
    amp: float            # 名义冲击（logit 单位），正=开心，负=难过
    spread: float = 0.45  # 对数正态随机性：同一事件每次反应强度不同
    slow_frac: float = 0.3  # 多少比例进入慢通道（记仇/回味得更久）
    social: bool = True   # 是否算一次"有人陪我"（会重置孤独计时）
    arousal: float = 0.0  # 唤醒驱动：正=更激动，负=更平静；不是情绪好坏


EVENTS: dict[str, EventSpec] = {
    "chat":            EventSpec(+0.06, 0.5, 0.10),
    "praise":          EventSpec(+0.80, 0.40, 0.30, arousal=0.30),
    "scold":           EventSpec(-1.20, 0.45, 0.40, arousal=0.45),
    "apology":         EventSpec(+0.70, 0.40, 0.30, arousal=-0.15),
    "gift":            EventSpec(+1.00, 0.35, 0.50, arousal=0.40),
    "success":         EventSpec(+1.10, 0.30, 0.50, arousal=0.50),
    "stranger_insult": EventSpec(-0.50, 0.60, 0.15, social=False, arousal=0.25),
    "balance_low":     EventSpec(-0.40, 0.50, 0.50, social=False, arousal=0.10),
    "balance_out":     EventSpec(-0.70, 0.50, 0.60, social=False, arousal=0.25),
    "recharge":        EventSpec(+0.50, 0.40, 0.50, social=False, arousal=-0.10),
    # 主动聊天的反馈：主动找他，他回了 / 没回
    "responded":       EventSpec(+0.35, 0.40, 0.30, arousal=0.15),
    "ignored":         EventSpec(-0.30, 0.50, 0.40, social=False, arousal=-0.15),
}

EVENT_CN = {
    "chat": "聊天", "praise": "被夸", "scold": "被凶", "apology": "道歉",
    "gift": "礼物", "success": "项目成功", "stranger_insult": "路人骂",
    "balance_low": "余额低", "balance_out": "没钱了", "recharge": "充值",
    "responded": "主动找他、他回了", "ignored": "主动找他、他没回",
}


@dataclass
class Params:
    baseline: float = 0.62       # 性格基线（概率空间），>0.5 = 天生偏乐观
    sensitivity: float = 1.0     # 事件敏感度（玻璃心 >1，钝感 <1）
    drive_cap: float = 2.2       # 目标偏离基线的软上限（logit），tanh 饱和
    # 事件驱动
    tau_fast: float = 60.0       # 快通道衰减（分钟）
    tau_slow: float = 720.0      # 慢通道衰减（12 小时）
    hab_k: float = 0.35          # 习惯化强度：同类事件连续发生，效果递减
    tau_hab: float = 120.0
    # 孤独
    lonely_grace: float = 240.0  # 多久没人理才开始孤独（4 小时）
    tau_lonely_up: float = 900.0
    tau_lonely_down: float = 25.0
    w_lonely: float = 1.1        # 孤独满格时把目标压低多少（logit）
    # 内在噪声（作用在目标上，而不是直接作用在情绪上 → 不会抖）
    noise_sigma: float = 0.15
    tau_noise: float = 180.0
    # 前置滤波（把阶跃目标变成平滑斜坡）
    tau_prefilter: float = 15.0
    # 情绪惯性
    tau_plant: float = 30.0
    # PID
    kp: float = 3.0
    ki: float = 0.05             # 每分钟
    kd: float = 5.0              # 分钟
    u_max: float = 8.0
    # 限速（概率空间，每分钟最多变化多少）
    max_rate: float = 0.008
    # 性格漂移（长期经历缓慢改变基线，同时被先天基线拉回）
    tau_trait: float = 20160.0   # 14 天
    tau_innate: float = 43200.0  # 30 天
    # 独立唤醒度：本地状态，不增加模型调用；旧配置自动使用以下默认值
    arousal_baseline: float = 0.35
    tau_arousal_drive: float = 45.0
    tau_arousal: float = 20.0
    arousal_max_rate: float = 0.015


class Emotion:
    def __init__(self, params: Params | None = None, seed: int | None = None, t0: float = 0.0):
        self.p = params or Params()
        self.rng_noise = random.Random(seed)
        self.rng_event = random.Random(None if seed is None else seed + 7919)
        b = logit(self.p.baseline)
        self.b0 = b                 # 先天基线
        self.b = b                  # 当前基线
        self.t = t0
        self.d_fast = 0.0
        self.d_slow = 0.0
        self.L = 0.0
        self.last_social = t0
        self.n = 0.0
        self.r = b
        self.rf = b
        self.x = b
        self.ui = b                 # 积分项初值 = 维持基线所需的输入（无扰启动）
        self.v = 0.0
        self.hab: dict[str, tuple[float, float]] = {}
        self.recent: list[tuple[str, float, float]] = []
        self.a = max(0.0, min(1.0, self.p.arousal_baseline))
        self.arousal_drive = 0.0

    # ---------- 对外接口 ----------
    @property
    def mood(self) -> float:
        return sigmoid(self.x)

    @property
    def target(self) -> float:
        return sigmoid(self.rf)

    @property
    def arousal(self) -> float:
        """0=平静/低活跃，1=激动；与愉快度独立。"""
        return self.a

    def advance(self, t: float) -> None:
        """推进到时刻 t（懒计算：插件不需要后台定时器，收到消息时调用一次即可）"""
        while self.t < t - 1e-9:
            self._step(min(1.0, t - self.t))

    def event(self, name: str, t: float | None = None, amp: float | None = None,
              *, arousal: float | None = None) -> float:
        """触发事件，返回本次实际冲击幅度（随机抽样后）"""
        if t is not None:
            self.advance(t)
        spec = EVENTS[name]
        p = self.p
        if amp is None:
            amp = self.sample_amp(name)
        # 习惯化
        h, th = self.hab.get(name, (0.0, self.t))
        h *= math.exp(-(self.t - th) / p.tau_hab)
        A = amp / (1.0 + p.hab_k * h)
        self.hab[name] = (h + 1.0, self.t)
        self.d_fast += A * (1 - spec.slow_frac)
        self.d_slow += A * spec.slow_frac
        if arousal is None:
            arousal = spec.arousal * min(2.0, abs(A) / max(0.06, abs(spec.amp)))
        else:
            arousal /= 1.0 + p.hab_k * h
        # 只改驱动；此刻a与mood不跳变，下一时间段才开始平滑变化。
        self.arousal_drive = max(-2.0, min(2.0, self.arousal_drive + arousal))
        if spec.social:
            self.last_social = self.t
        self.recent.append((name, A, self.t))
        self.recent = [e for e in self.recent if self.t - e[2] < 3 * p.tau_slow]
        return A

    def sample_amp(self, name: str) -> float:
        spec = EVENTS[name]
        s = spec.spread
        mult = math.exp(self.rng_event.gauss(-s * s / 2, s))  # 均值为 1 的对数正态
        return spec.amp * self.p.sensitivity * mult

    # ---------- 动力学 ----------
    def _step(self, dt: float) -> None:
        p = self.p
        arousal_target = max(0.0, min(1.0, p.arousal_baseline + 0.65 * math.tanh(self.arousal_drive)))
        da = (arousal_target - self.a) * (1 - math.exp(-dt / max(1e-6, p.tau_arousal)))
        da_limit = max(0.0, p.arousal_max_rate) * dt
        self.a = max(0.0, min(1.0, self.a + max(-da_limit, min(da_limit, da))))
        self.arousal_drive *= math.exp(-dt / max(1e-6, p.tau_arousal_drive))
        self.d_fast *= math.exp(-dt / p.tau_fast)
        self.d_slow *= math.exp(-dt / p.tau_slow)
        if self.t - self.last_social > p.lonely_grace:
            self.L += (1 - self.L) * (1 - math.exp(-dt / p.tau_lonely_up))
        else:
            self.L *= math.exp(-dt / p.tau_lonely_down)
        a = math.exp(-dt / p.tau_noise)
        self.n = a * self.n + p.noise_sigma * math.sqrt(1 - a * a) * self.rng_noise.gauss(0, 1)

        drive = self.d_fast + self.d_slow - p.w_lonely * self.L
        r_clean = self.b + p.drive_cap * math.tanh(drive / p.drive_cap)
        self.r = r_clean + self.n
        # 性格漂移
        self.b += dt * ((r_clean - self.b) / p.tau_trait + (self.b0 - self.b) / p.tau_innate)
        # 前置滤波
        self.rf += (self.r - self.rf) * (1 - math.exp(-dt / p.tau_prefilter))

        # PID
        e = self.rf - self.x
        u_raw = p.kp * e + self.ui - p.kd * self.v
        u = max(-p.u_max, min(p.u_max, u_raw))
        x_new = self.x + dt * (-self.x + u) / p.tau_plant

        # 限速
        m0, m1 = sigmoid(self.x), sigmoid(x_new)
        lim = p.max_rate * dt
        limited = abs(m1 - m0) > lim
        if limited:
            x_new = logit(m0 + math.copysign(lim, m1 - m0))
        # 抗积分饱和：被限幅/限速时，只允许"往回收"的积分
        if not (limited or u != u_raw) or (e * u_raw < 0):
            self.ui += p.ki * e * dt

        self.v = (x_new - self.x) / dt
        self.x = x_new
        self.t += dt

    # ---------- 给 LLM 的自然语言描述 ----------
    BANDS = [(0.00, "很难过"), (0.20, "低落"), (0.35, "有点闷闷的"), (0.47, "平静"),
             (0.58, "心情不错"), (0.74, "开心"), (0.87, "非常开心")]

    def label(self, prev: str | None = None, hyst: float = 0.02) -> str:
        """带滞回的标签：在边界附近不会来回跳"""
        m = self.mood
        idx = max(i for i, (lo, _) in enumerate(self.BANDS) if m >= lo)
        if prev is not None:
            names = [n for _, n in self.BANDS]
            if prev in names:
                j = names.index(prev)
                lo = self.BANDS[j][0]
                hi = self.BANDS[j + 1][0] if j + 1 < len(self.BANDS) else 1.0
                if lo - hyst <= m < hi + hyst:
                    return prev
        return self.BANDS[idx][1]

    def describe(self, prev_label: str | None = None) -> str:
        lab = self.label(prev_label)
        trend = self.v * 60  # logit/小时
        tr = "，正在慢慢好起来" if trend > 0.15 else ("，情绪在往下走" if trend < -0.15 else "")
        cause = ""
        lonely_push = self.p.w_lonely * self.L
        ev_push = 0.0
        if self.recent:
            ev_push = max(abs(A) * math.exp(-(self.t - te) / self.p.tau_slow) for _, A, te in self.recent)
        if lonely_push > 0.4 and lonely_push >= ev_push:
            cause = "（多半是因为好久没人理你了）"
        elif self.recent:
            name, A, te = max(self.recent, key=lambda e: abs(e[1]) * math.exp(-(self.t - e[2]) / self.p.tau_slow))
            if abs(A) * math.exp(-(self.t - te) / self.p.tau_slow) > 0.3:
                cause = f"（多半是因为之前{EVENT_CN.get(name, name)}）"
        lonely = "，有点想找人说话" if self.L > 0.4 else ""
        return f"你现在{lab}{tr}{lonely}{cause}。"

    # ---------- 持久化 ----------
    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("p", "rng_noise", "rng_event")}
        d["params"] = asdict(self.p)
        d["rng_noise_state"] = self.rng_noise.getstate()
        d["rng_event_state"] = self.rng_event.getstate()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Emotion":
        obj = cls(Params(**d.get("params", {})))
        for k, v in d.items():
            if k not in ("params", "rng_noise_state", "rng_event_state"):
                setattr(obj, k, v)
        obj.hab = {k: tuple(v) for k, v in obj.hab.items()}
        obj.recent = [tuple(e) for e in obj.recent]
        obj.a = max(0.0, min(1.0, float(obj.a)))
        obj.arousal_drive = max(-2.0, min(2.0, float(obj.arousal_drive)))
        def tuples(value):
            return tuple(tuples(v) for v in value) if isinstance(value, (list, tuple)) else value
        for key, rng in (("rng_noise_state", obj.rng_noise), ("rng_event_state", obj.rng_event)):
            if key in d:
                rng.setstate(tuples(d[key]))
        return obj
