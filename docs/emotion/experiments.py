import sys, math, random, json
sys.path.insert(0, '.')
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
from emotion import Emotion, Params, EVENTS, EVENT_CN, sigmoid, logit

for f in font_manager.findSystemFonts():
    if 'NotoSansCJK-Regular' in f:
        font_manager.fontManager.addfont(f)
plt.rcParams['font.family'] = 'Noto Sans CJK JP'
plt.rcParams['axes.unicode_minus'] = False
plt.rcParams['figure.dpi'] = 110
OUT = 'figs/'
import os; os.makedirs(OUT, exist_ok=True)

T = 1440  # 一天，分钟，t=0 视为早上 8:00
def hhmm(t): return f"{(8 + int(t)//60) % 24:02d}:{int(t)%60:02d}"

# ---------------- 一天的剧本 ----------------
def day_script():
    ev = []
    for t in range(0, 70, 6): ev.append((t, 'chat'))
    ev.append((40, 'praise'))
    ev.append((120, 'scold'))
    for t in (126, 133, 140): ev.append((t, 'chat'))
    ev.append((150, 'apology'))
    for t in (155, 162, 170): ev.append((t, 'chat'))
    # 180~600 没人理
    ev.append((560, 'stranger_insult'))
    ev.append((620, 'balance_low'))
    ev.append((690, 'balance_out'))
    ev.append((800, 'recharge'))
    for t in range(805, 900, 7): ev.append((t, 'chat'))
    ev.append((820, 'gift'))
    ev.append((880, 'success'))
    return sorted(ev)

def run_pid(script, params=None, seed=0, fixed_amp=None, record_internal=False):
    e = Emotion(params, seed=seed)
    ev = list(script); i = 0
    mood, tgt, lon = [], [], []
    for t in range(T + 1):
        while i < len(ev) and ev[i][0] <= t:
            name = ev[i][1]
            amp = None if fixed_amp is None else fixed_amp[i]
            e.event(name, t=ev[i][0], amp=amp); i += 1
        e.advance(t)
        mood.append(e.mood); tgt.append(e.target); lon.append(e.L)
    if record_internal:
        return np.array(mood), np.array(tgt), np.array(lon), e
    return np.array(mood)

# 对照组 A：事件直接加减情绪值（概率空间），线性回归基线
def run_naive(script, amps, base=0.62, gain=0.13, tau=120, seed=0):
    rng = random.Random(seed); m = base; out = []; i = 0
    for t in range(T + 1):
        while i < len(script) and script[i][0] <= t:
            m += gain * amps[i]; i += 1
        m += (base - m) / tau + rng.gauss(0, 0.004)
        m = min(max(m, 0.0), 1.0)
        out.append(m)
    return np.array(out)

# 对照组 B：logit 空间 OU 过程（上一轮我提的方案 A），冲击直接作用在状态上
def run_ou(script, amps, base=0.62, tau=120, sigma=0.06, seed=0):
    rng = random.Random(seed); b = logit(base); x = b; out = []; i = 0
    for t in range(T + 1):
        while i < len(script) and script[i][0] <= t:
            x += amps[i]; i += 1
        x += (b - x) / tau + sigma * rng.gauss(0, 1)
        out.append(sigmoid(x))
    return np.array(out)

def metrics(m):
    d = np.abs(np.diff(m))
    return dict(max_step=d.max(), p99_step=np.quantile(d, 0.99), tv=d.sum(), lo=m.min(), hi=m.max())

def draw_events(ax, script, ymin=0.0):
    seen = set()
    colors = {}
    for t, n in script:
        if n == 'chat':
            ax.plot(t, 0.03, '|', color='#9aa', ms=6); continue
        c = '#2a9d8f' if EVENTS[n].amp > 0 else '#e76f51'
        ax.axvline(t, color=c, alpha=.25, lw=1)
        ax.text(t, 0.97, EVENT_CN[n], rotation=90, va='top', ha='right', fontsize=8, color=c)

def fmt_time(ax):
    ticks = list(range(0, T + 1, 120))
    ax.set_xticks(ticks); ax.set_xticklabels([hhmm(t) for t in ticks])
    ax.set_xlim(0, T)

script = day_script()
rng = random.Random(42)
e_tmp = Emotion(seed=42)
amps = [e_tmp.sample_amp(n) for _, n in script]

# ====== 图1：三种模型对比 ======
naive = run_naive(script, amps)
ou = run_ou(script, amps)
pid, tgt, lon, _ = run_pid(script, seed=3, fixed_amp=amps, record_internal=True)

fig, axes = plt.subplots(3, 1, figsize=(12, 9.5), sharex=True)
for ax, (m, title, c) in zip(axes, [
        (naive, 'A. 事件直接加减情绪值（最朴素的做法）', '#999'),
        (ou, 'B. logit 空间均值回归 + 噪声（冲击直接打在情绪上）', '#8d6cab'),
        (pid, 'C. 事件改"设定点"，PID 平滑追踪 + 限速（本方案）', '#1d6fb8')]):
    draw_events(ax, script)
    if m is pid:
        ax.plot(tgt, color='#f4a261', lw=1.2, ls='--', label='设定点（她"想去"的情绪）')
        ax.fill_between(range(T + 1), 0, lon * 0.12, color='#bbb', alpha=.5, label='孤独度 ×0.12')
    ax.plot(m, color=c, lw=2, label='情绪 mood')
    ax.axhline(0.62, color='k', lw=.6, ls=':')
    mt = metrics(m)
    ax.set_title(f"{title}    相邻分钟最大变化 {mt['max_step']:.3f}，全天总变化量 {mt['tv']:.2f}", fontsize=11, loc='left')
    ax.set_ylim(0, 1); ax.set_ylabel('0 悲伤 ← → 1 开心')
    ax.legend(loc='lower left', fontsize=8)
fmt_time(axes[-1])
fig.tight_layout(); fig.savefig(OUT + '1_三种模型对比.png'); plt.close(fig)
lim=np.mean(np.abs(np.diff(pid))>Params().max_rate*0.98); print("limiter active fraction",lim); print('fig1', {k: metrics(v) for k, v in [('naive', naive), ('ou', ou), ('pid', pid)]})

# ====== 图2：单个事件的阶跃响应，拆解 PID 的作用 ======
def step_resp(params, amp=-1.2):
    e = Emotion(params, seed=0); e.p.noise_sigma = 0; e.p.lonely_grace = 1e9
    m, r = [], []
    for t in range(721):
        if t == 30: e.event('scold', t=30, amp=amp)
        e.advance(t); m.append(e.mood); r.append(sigmoid(e.r))
    return np.array(m), np.array(r)

fig, ax = plt.subplots(1, 3, figsize=(16, 4.6))
base = Params()
variants = [
    ('只有惯性（Kp=1, 无 I/D, 无限速）', dict(kp=1, ki=0, kd=0, max_rate=1), '-'),
    ('P+惯性（Kp=3）', dict(kp=3, ki=0, kd=0, max_rate=1), '-'),
    ('PID（无限速）', dict(max_rate=1), '-'),
    ('PID + 限速（最终）', dict(), '--'),
]
for k, (amp, ttl) in enumerate([(-1.2, '普通事件：被凶一次'), (-4.0, '极端事件：被狠狠骂（冲击×3.3）')]):
    for lab, kw, ls in variants:
        p = Params(**{**base.__dict__, **kw})
        m, r = step_resp(p, amp)
        ax[k].plot(m, lw=2, ls=ls, label=lab)
        if k == 1: ax[2].plot(np.abs(np.diff(m))[:200], lw=2, ls=ls, label=lab)
    ax[k].plot(r, 'k:', lw=1, label='设定点 r')
    ax[k].set_title(ttl, loc='left'); ax[k].set_xlabel('分钟'); ax[k].set_xlim(0, 720)
    ax[k].legend(fontsize=8)
ax[0].set_ylabel('mood')
ax[2].axhline(base.max_rate, color='r', ls=':', label=f'限速上限 {base.max_rate}/分钟')
ax[2].set_title('极端事件时每分钟变化量 |Δmood|', loc='left'); ax[2].set_xlabel('分钟'); ax[2].legend(fontsize=8)
fig.tight_layout(); fig.savefig(OUT + '2_单事件阶跃响应.png'); plt.close(fig)

# ====== 图3：概率性 —— 同一剧本跑 300 次 ======
N = 300
runs = np.array([run_pid(script, seed=s) for s in range(N)])
fig = plt.figure(figsize=(13, 5.2))
gs = fig.add_gridspec(1, 4)
ax = fig.add_subplot(gs[0, :3])
draw_events(ax, script)
q = np.quantile(runs, [0.05, 0.25, 0.5, 0.75, 0.95], axis=0)
ax.fill_between(range(T + 1), q[0], q[4], color='#1d6fb8', alpha=.15, label='90% 区间')
ax.fill_between(range(T + 1), q[1], q[3], color='#1d6fb8', alpha=.3, label='50% 区间')
ax.plot(q[2], color='#1d6fb8', lw=2, label='中位数')
for s in range(3): ax.plot(runs[s], lw=.8, alpha=.8)
ax.set_ylim(0, 1); fmt_time(ax); ax.legend(loc='lower left', fontsize=8)
ax.set_title(f'同一天、同样的事件，跑 {N} 次：每次反应强度随机，但曲线形状一致', loc='left')
ax2 = fig.add_subplot(gs[0, 3])
t_probe = 760  # 没钱了之后
labels = ['很难过', '低落', '有点闷', '平静', '心情不错', '开心', '非常开心']
edges = [0, .2, .35, .47, .58, .74, .87, 1]
vals = runs[:, t_probe]
cnt, _ = np.histogram(vals, bins=edges)
ax2.barh(labels, cnt / N * 100, color='#e76f51')
for i, c in enumerate(cnt): ax2.text(c / N * 100 + 1, i, f'{c/N*100:.0f}%', va='center', fontsize=9)
ax2.set_title(f'{hhmm(t_probe)}（冷落+没钱后）\n她处于各状态的概率', fontsize=10)
ax2.set_xlabel('%')
fig.tight_layout(); fig.savefig(OUT + '3_概率性_300次.png'); plt.close(fig)
print('fig3 dist at', hhmm(t_probe), dict(zip(labels, (cnt / N).round(2))))
print('fig3 max step over all runs', np.abs(np.diff(runs, axis=1)).max())

# ====== 图4：不同性格 ======
fig, ax = plt.subplots(figsize=(12, 4.6))
draw_events(ax, script)
for lab, kw, c in [
        ('乐观钝感（基线0.70，敏感度0.6）', dict(baseline=.70, sensitivity=.6), '#2a9d8f'),
        ('默认（基线0.62，敏感度1.0）', dict(), '#1d6fb8'),
        ('敏感玻璃心（基线0.55，敏感度1.6，孤独权重1.6）', dict(baseline=.55, sensitivity=1.6, w_lonely=1.6), '#e76f51')]:
    ms = np.array([run_pid(script, Params(**kw), seed=s) for s in range(60)])
    ax.plot(np.median(ms, 0), lw=2, color=c, label=lab)
    ax.fill_between(range(T + 1), *np.quantile(ms, [.25, .75], 0), color=c, alpha=.15)
ax.set_ylim(0, 1); fmt_time(ax); ax.legend(loc='lower left', fontsize=9)
ax.set_title('同样的一天，三种性格（只改 3 个参数）', loc='left')
fig.tight_layout(); fig.savefig(OUT + '4_不同性格.png'); plt.close(fig)

# ====== 图5：习惯化 + 持续冷落一周 ======
fig, ax = plt.subplots(1, 2, figsize=(13, 4.4))
for hk, lab in [(0, '无习惯化'), (Params().hab_k, f'习惯化 hab_k={Params().hab_k}')]:
    e = Emotion(Params(hab_k=hk, noise_sigma=0, lonely_grace=1e9), seed=0)
    m = []
    for t in range(481):
        if t in range(20, 200, 15): e.event('scold', t=t, amp=-0.6)
        e.advance(t); m.append(e.mood)
    ax[0].plot(m, lw=2, label=lab)
ax[0].set_title('每 15 分钟被轻微数落一次，连续 12 次', loc='left'); ax[0].legend(); ax[0].set_xlabel('分钟'); ax[0].set_ylim(0, 1)
# 一周：前 3 天每天有人聊，后 4 天没人理，看基线漂移
e = Emotion(seed=5); m, b = [], []
W = 7 * 1440
for t in range(0, W + 1):
    day, mod = divmod(t, 1440)
    if day < 3 and 600 <= mod <= 900 and mod % 10 == 0:
        e.event('chat', t=t)
        if mod == 700: e.event('praise', t=t)
    e.advance(t)
    if t % 10 == 0: m.append(e.mood); b.append(sigmoid(e.b))
tt = np.arange(0, W + 1, 10) / 1440
ax[1].plot(tt, m, lw=1.5, label='情绪')
ax[1].plot(tt, b, lw=2, ls='--', label='性格基线（极慢漂移）')
ax[1].axvspan(3, 7, color='#ccc', alpha=.3); ax[1].text(5, .9, '没人理', ha='center')
ax[1].set_ylim(0, 1); ax[1].set_xlabel('天'); ax[1].legend(fontsize=9)
ax[1].set_title('一周：3 天有人陪 → 4 天被冷落', loc='left')
fig.tight_layout(); fig.savefig(OUT + '5_习惯化与长期.png'); plt.close(fig)
print('week end mood', m[-1], 'baseline', b[-1])

# ====== 给 LLM 的描述样例 ======
mood, tgt, lon, _ = run_pid(script, seed=3, fixed_amp=amps, record_internal=True)
e = Emotion(seed=3); i = 0; prev = None; samples = []
for t in range(T + 1):
    while i < len(script) and script[i][0] <= t:
        e.event(script[i][1], t=script[i][0], amp=amps[i]); i += 1
    e.advance(t)
    if t % 60 == 0 or t in (125, 160, 700, 830):
        d = e.describe(prev); prev = e.label(prev)
        samples.append(f"{hhmm(t)}  mood={e.mood:.2f}  → {d}")
open('describe_samples.txt', 'w').write('\n'.join(samples))
print('\n'.join(samples))
