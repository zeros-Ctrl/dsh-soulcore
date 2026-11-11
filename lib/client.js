/**
 * dsh-soulcore —— 客户端半边：DSH「设置 → 人格与记忆」
 * 数据流：本 section ⇄ /dsh-soulcore/api/*（同源 fetch）⇄ ~/.dsh/soulcore/config.json + 后端子进程
 */
window.__ModuleLoader__.load({
  id: "dsh-soulcore",
  factory: (require) => {
    const module = { exports: {} };
    const exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });

    const React = require("react");
    const { createElement: h, useCallback, useEffect, useState } = React;
    const API = "/dsh-soulcore/api";

    const CSS_ID = "dsh-soulcore-css";
    const CSS = [
      ".slc{max-width:760px;color:var(--dsw-alias-label-primary);display:flex;flex-direction:column;gap:6px}",
      ".slc-title{margin:0;font-size:16px;font-weight:500;line-height:24px}",
      ".slc-intro{color:var(--dsw-alias-label-tertiary);margin:0 0 4px;font-size:13px;line-height:20px}",
      ".slc-group{margin-top:16px;font-size:13px;font-weight:600;line-height:20px}",
      ".slc-row{display:flex;justify-content:space-between;align-items:flex-start;gap:12px 20px;padding:9px 0;border-bottom:.5px solid var(--dsw-alias-border-l2);flex-wrap:wrap}",
      ".slc-row>div:first-child{flex:1;min-width:190px}",
      ".slc-label{font-size:13px;line-height:19px}",
      ".slc-hint{color:var(--dsw-alias-label-tertiary);margin-top:3px;font-size:11.5px;line-height:17px;white-space:pre-line}",
      ".slc-input{background:transparent;border:1px solid var(--dsw-alias-border-l2);border-radius:6px;color:var(--dsw-alias-label-primary);font-size:12.5px;height:28px;padding:0 8px;min-width:200px;outline:none}",
      ".slc-input:focus-visible,.slc-area:focus-visible{border-color:var(--dsw-alias-brand-primary)}",
      ".slc-area{width:100%;min-height:200px;background:transparent;border:1px solid var(--dsw-alias-border-l2);border-radius:8px;color:var(--dsw-alias-label-primary);font-size:12.5px;line-height:19px;padding:8px 10px;outline:none;resize:vertical;font-family:inherit;box-sizing:border-box}",
      ".slc-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}",
      ".slc-btn{background:var(--dsw-alias-brand-primary);color:#fff;border:none;border-radius:6px;font-size:12.5px;height:30px;padding:0 12px;cursor:pointer}",
      ".slc-btn:disabled{opacity:.5;cursor:default}",
      ".slc-btn2{background:transparent;border:1px solid var(--dsw-alias-border-l2);color:var(--dsw-alias-label-primary)}",
      ".slc-card{border:1px solid var(--dsw-alias-border-l2);border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:8px}",
      ".slc-bar{height:10px;border-radius:999px;background:rgba(127,127,127,.18);overflow:hidden}",
      ".slc-bar>div{height:100%;border-radius:999px;transition:width .6s ease}",
      ".slc-kv{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12px;color:var(--dsw-alias-label-secondary)}",
      ".slc-out{white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;line-height:17px;color:var(--dsw-alias-label-secondary);background:rgba(127,127,127,.08);border-radius:8px;padding:10px 12px;max-height:260px;overflow:auto}",
      ".slc-err{color:var(--dsw-alias-state-error-primary);font-size:12px;line-height:18px;white-space:pre-line}",
      ".slc-ok{color:var(--dsw-alias-label-secondary);font-size:12px}",
    ].join("");
    function ensureCss() {
      if (document.getElementById(CSS_ID)) return;
      const s = document.createElement("style"); s.id = CSS_ID; s.textContent = CSS; document.head.appendChild(s);
    }

    const FIELDS = [
      { group: "她是谁", path: ["persona", "name"], label: "角色名", hint: "人设里的 {name} 会被替换成它", kind: "text" },
      { group: "她是谁", path: ["dsh", "ownerName"], label: "她怎么称呼你", kind: "text" },
      { group: "性格与情绪", path: ["emotion", "baseline"], label: "性格基线（0.1~0.9）", hint: "没有事情发生时，心情会慢慢回到这里。0.62 ≈ 偏乐观", kind: "number", step: 0.01 },
      { group: "性格与情绪", path: ["emotion", "sensitivity"], label: "敏感度", hint: "同样一句话对她的影响倍数。0.6 钝感 / 1 默认 / 1.6 玻璃心", kind: "number", step: 0.1 },
      { group: "记忆", path: ["llm", "api_key"], label: "DeepSeek API Key（可选）", hint: "只用于后台提炼记忆、合并摘要、查余额。不填 = 用规则提炼（够用但粗糙）。\n不会回显；留空保存表示不修改。", kind: "secret" },
      { group: "记忆", path: ["memory", "embedding", "provider"], label: "语义向量（可选）", hint: "留空 = 只用字词+概念词典（离线）。填 http = 用 OpenAI 兼容的 /embeddings 接口，\n例如硅基流动 bge-m3；意思相近但用词完全不同的记忆也能找到", kind: "select", options: [["", "关闭"], ["http", "HTTP 接口"], ["fastembed", "本地 fastembed（需自行安装）"]] },
      { group: "记忆", path: ["memory", "embedding", "base_url"], label: "向量接口地址", kind: "text" },
      { group: "记忆", path: ["memory", "embedding", "model"], label: "向量模型", kind: "text" },
      { group: "记忆", path: ["memory", "embedding", "api_key"], label: "向量接口 Key", kind: "secret" },
      { group: "记忆", path: ["memory", "extract_every"], label: "每几轮提炼一次长期记忆", kind: "number", step: 1 },
      { group: "记忆", path: ["memory", "top_k"], label: "每轮最多召回几条记忆", kind: "number", step: 1 },
      { group: "接入 DSH", path: ["dsh", "enabled"], label: "启用", kind: "switch" },
      { group: "接入 DSH", path: ["dsh", "injectPersona"], label: "人设写入 system prompt", kind: "switch" },
      { group: "接入 DSH", path: ["dsh", "injectContext"], label: "每轮注入心情和相关记忆", kind: "switch" },
      { group: "接入 DSH", path: ["dsh", "recordReplies"], label: "把她的回复写回记忆", kind: "switch" },
      { group: "接入 DSH", path: ["dsh", "compactionHook"], label: "上下文压缩前先提炼落盘", hint: "防止压缩导致失忆", kind: "switch" },
      { group: "接入 DSH", path: ["dsh", "tools"], label: "提供 memory_search / remember / mood_status 工具", kind: "switch" },
      { group: "接入 DSH", path: ["dsh", "excludeSessions"], label: "不注入的会话（逗号分隔）", hint: "默认排除 bili-whale（它有自己的人设）", kind: "list" },
    ];

    const get = (o, p) => p.reduce((a, k) => (a == null ? a : a[k]), o);
    const set = (o, p, v) => {
      const out = JSON.parse(JSON.stringify(o || {}));
      let cur = out;
      for (const k of p.slice(0, -1)) { if (typeof cur[k] !== "object" || cur[k] == null) cur[k] = {}; cur = cur[k]; }
      cur[p[p.length - 1]] = v;
      return out;
    };
    async function call(path, opts) {
      const r = await fetch(API + path, { headers: { "Content-Type": "application/json" }, ...opts });
      const t = await r.text();
      let j = null; try { j = JSON.parse(t); } catch { j = { ok: false, error: t.slice(0, 200) }; }
      if (!r.ok && j && j.ok !== false) j.ok = false;
      return j;
    }
    function moodColor(m) {
      const hue = Math.round(220 * (1 - m) + 40 * m);   // 蓝(低落) → 橙(开心)
      return `hsl(${hue} 75% 55%)`;
    }

    function Field({ f, cfg, onChange }) {
      const v = get(cfg, f.path);
      let input;
      if (f.kind === "switch") {
        input = h("input", { type: "checkbox", checked: v !== false, onChange: (e) => onChange(f.path, e.target.checked) });
      } else if (f.kind === "list") {
        input = h("input", { className: "slc-input", value: Array.isArray(v) ? v.join(", ") : (v ?? ""),
          onChange: (e) => onChange(f.path, e.target.value.split(/[,，\s]+/).filter(Boolean)) });
      } else if (f.kind === "number") {
        input = h("input", { className: "slc-input", type: "number", step: f.step ?? 1, value: v ?? "",
          onChange: (e) => onChange(f.path, e.target.value === "" ? "" : Number(e.target.value)) });
      } else if (f.kind === "select") {
        input = h("select", { className: "slc-input", value: v ?? "", onChange: (e) => onChange(f.path, e.target.value) },
          ...f.options.map(([val, lab]) => h("option", { key: val, value: val }, lab)));
      } else if (f.kind === "secret") {
        input = h("input", { className: "slc-input", type: "password", placeholder: v ? "已设置（••••••）" : "未设置", value: v && v !== "••••••" ? v : "",
          onChange: (e) => onChange(f.path, e.target.value || "••••••") });
      } else {
        input = h("input", { className: "slc-input", value: v ?? "", onChange: (e) => onChange(f.path, e.target.value) });
      }
      return h("div", { className: "slc-row" },
        h("div", null, h("div", { className: "slc-label" }, f.label), f.hint ? h("div", { className: "slc-hint" }, f.hint) : null),
        input);
    }

    // ───────────── QQ 接入卡片 ─────────────
    const QQ_FIELDS = [
      { path: ["qq", "enabled"], label: "启用 QQ", kind: "switch", hint: "需要先装好 NapCat 并扫码登录（建议用小号）" },
      { path: ["owner", "qq"], label: "你的 QQ 号", kind: "text", hint: "她会把这个号认成你：和 DSH 里的你共用一份档案，私聊自动放行，\n还能聊起你们在 DSH 里做的项目" },
      { path: ["qq", "ws_url"], label: "NapCat WebSocket 地址", kind: "text", hint: "NapCat WebUI →「网络配置」→ 新建「WebSocket 服务器」，\nHost 填 127.0.0.1、端口 3001、消息格式 Array，保存并启用" },
      { path: ["qq", "access_token"], label: "Token", kind: "secret", hint: "和 NapCat WebSocket 服务器里填的 Token 一致（不是 WebUI 的登录密码）" },
      { path: ["qq", "group_whitelist"], label: "群白名单（群号，逗号分隔）", kind: "list", hint: "只有这些群的消息她会看；被 @ 或叫名字时才回复。留空 = 不进任何群" },
      { path: ["qq", "group_known"], label: "群里的熟人（QQ 号，逗号分隔）", kind: "list", hint: "这些人在群里说的话可信度更高；其他群友算陌生人，\n他们说的话不会进入你在 DSH 里的编程会话" },
      { path: ["qq", "private_whitelist"], label: "还允许谁私聊她（QQ 号，逗号分隔）", kind: "list", hint: "你自己不用填" },
    ];

    function QQCard({ cfg, onChange, st }) {
      const [test, setTest] = useState(null);
      const [svc, setSvc] = useState(null);
      const [busy, setBusy] = useState("");
      const loadSvc = useCallback(async () => { setSvc(await call("/service")); }, []);
      useEffect(() => { loadSvc(); }, [loadSvc]);

      const doTest = async () => {
        setBusy("test"); setTest(null);
        const r = await call("/qq/test", { method: "POST", body: JSON.stringify({
          ws_url: get(cfg, ["qq", "ws_url"]), access_token: get(cfg, ["qq", "access_token"]) }) });
        setBusy(""); setTest(r);
      };
      const doSvc = async (action) => {
        const tip = action === "install"
          ? "安装后：登录 Windows 时她会在后台自动启动，DSH 关掉她也在线（QQ 照常回复）。\n切换过程中后端会重启一次（约 10 秒）。继续？"
          : "卸载后：她只在 DSH 开着的时候在线。继续？";
        if (!window.confirm(tip)) return;
        setBusy(action);
        const r = await call("/service", { method: "POST", body: JSON.stringify({ action }) });
        setBusy(""); setSvc(r);
      };
      const setEnabled = (p, v) => {
        onChange(p, v);
        if (p[0] === "qq" && p[1] === "enabled" && v) onChange(["qq", "transport"], "ws");   // 设置页只管 WebSocket 方式
      };

      const qq = st && st.qq;
      const core = st && st.core;
      const http = get(cfg, ["qq", "transport"]) === "http";
      let status = "未启用";
      if (qq && qq.enabled) {
        if (qq.transport !== "ws") status = "使用 HTTP 上报方式（高级配置，设置页不管理）";
        else status = qq.connected ? `✓ 已连接 NapCat${qq.self_id ? `（机器人 QQ ${qq.self_id}）` : ""}` : "✗ 还没连上 NapCat（会自动重连）";
      } else if (get(cfg, ["qq", "enabled"])) status = "保存设置后生效";

      return h("div", { className: "slc-card", style: { marginTop: 8 } },
        h("div", { style: { display: "flex", justifyContent: "space-between", fontSize: "13px" } },
          h("span", null, "QQ 连接"), h("span", { className: qq && qq.enabled && qq.connected === false ? "slc-err" : "slc-ok" }, status)),
        h("div", { className: "slc-hint" },
          "三步接好：① 装 NapCat 并用小号扫码登录 ② 在 NapCat WebUI 新建 WebSocket 服务器（127.0.0.1:3001，设一个 Token）\n③ 在下面填你的 QQ 号、地址和 Token → 点「测试连接」→ 打开「启用 QQ」→ 保存设置"),
        http ? h("div", { className: "slc-hint" }, "当前配置是 HTTP 上报方式；打开「启用 QQ」并保存会切换到 WebSocket。") : null,
        ...QQ_FIELDS.map((f) => h(Field, { key: f.path.join("."), f, cfg, onChange: setEnabled })),
        h("div", { className: "slc-actions", style: { marginTop: 4 } },
          h("button", { className: "slc-btn slc-btn2", onClick: doTest, disabled: !!busy || !(core && core.ready) },
            busy === "test" ? "连接中…" : "测试连接")),
        test ? (test.ok
          ? h("div", { className: "slc-ok" }, `✓ 连上了：机器人 QQ ${test.user_id}${test.nickname ? `（${test.nickname}）` : ""}。打开「启用 QQ」并保存设置即可。`)
          : h("div", { className: "slc-err" }, `✗ ${test.error || "连接失败"}${test.hint ? "\n" + test.hint : ""}`)) : null,

        h("div", { className: "slc-group", style: { marginTop: 8 } }, "常驻运行"),
        h("div", { className: "slc-hint" },
          "默认她跟着 DSH 开关：DSH 关了，QQ 上也就不回了。\n装成常驻后，登录 Windows 时自动在后台启动，DSH 开着时直接连上她，不会多开。"),
        !svc ? h("div", { className: "slc-ok" }, "读取中…")
          : svc.supported === false ? h("div", { className: "slc-hint" }, svc.text || "常驻模式目前只支持 Windows")
          : h(React.Fragment, null,
              h("div", { className: "slc-ok" },
                svc.installed ? `✓ 已安装常驻${core && core.attached ? "，DSH 正连着常驻进程" : "（常驻进程当前没在运行，DSH 自己启动了后端）"}`
                  : "未安装：她只在 DSH 开着时在线"),
              h("div", { className: "slc-actions", style: { marginTop: 4 } },
                svc.installed
                  ? h("button", { className: "slc-btn slc-btn2", onClick: () => doSvc("uninstall"), disabled: !!busy },
                      busy === "uninstall" ? "卸载中…" : "卸载常驻")
                  : h("button", { className: "slc-btn", onClick: () => doSvc("install"), disabled: !!busy },
                      busy === "install" ? "安装中…（约 10 秒）" : "安装常驻（开机自动启动）")),
              svc.ok === false && svc.error ? h("div", { className: "slc-err" }, svc.error) : null));
    }

    function SoulcoreSection() {
      useEffect(ensureCss, []);
      const [st, setSt] = useState(null);
      const [cfg, setCfg] = useState(null);
      const [saved, setSaved] = useState("");
      const [persona, setPersona] = useState("");
      const [personaSaved, setPersonaSaved] = useState("");
      const [q, setQ] = useState("");
      const [hits, setHits] = useState(null);
      const [busy, setBusy] = useState("");
      const [msg, setMsg] = useState("");
      const [err, setErr] = useState("");

      const refresh = useCallback(async () => {
        try { setSt(await call("/state")); } catch (e) { setErr(String(e.message || e)); }
      }, []);
      const load = useCallback(async () => {
        setErr("");
        const s = await call("/settings"); if (s.ok) { setCfg(s.config); setSaved(JSON.stringify(s.config)); } else setErr(s.error || "读取设置失败");
        const p = await call("/persona"); if (p.ok) { setPersona(p.text); setPersonaSaved(p.text); }
        refresh();
      }, [refresh]);
      useEffect(() => { load(); const t = setInterval(refresh, 5000); return () => clearInterval(t); }, [load, refresh]);

      const onChange = (p, v) => setCfg((c) => set(c, p, v));
      const save = async () => {
        setBusy("save"); setMsg(""); setErr("");
        const r = await call("/settings", { method: "POST", body: JSON.stringify({ config: cfg }) });
        setBusy("");
        if (r.ok) {
          setCfg(r.config); setSaved(JSON.stringify(r.config));
          if (r.note) { if (r.restarted) setMsg("✓ 已保存。" + r.note); else setErr("已保存，但" + r.note); }
          else setMsg("✓ 已保存，后端正在重启");
          setTimeout(refresh, 2500);
        }
        else setErr(r.error || "保存失败");
      };
      const savePersona = async () => {
        setBusy("persona"); setMsg(""); setErr("");
        const r = await call("/persona", { method: "POST", body: JSON.stringify({ text: persona }) });
        setBusy("");
        if (r.ok) { setPersonaSaved(persona); setMsg(`✓ 人设已保存（${r.bytes} 字节），新一轮对话生效`); } else setErr(r.error || "保存失败");
      };
      const search = async () => {
        if (!q.trim()) return;
        setBusy("search");
        const r = await call("/search?q=" + encodeURIComponent(q) + "&path=project");
        setBusy(""); setHits(r.ok ? r.items : []); if (!r.ok) setErr(r.error || "搜索失败");
      };
      const restart = async () => {
        setBusy("restart"); const r = await call("/restart", { method: "POST" }); setBusy("");
        if (!r.ok) setErr(r.error || "重启失败"); refresh();
      };
      const syncMd = async () => {
        setBusy("sync"); const r = await call("/sync", { method: "POST" }); setBusy("");
        if (r.ok) setMsg(`✓ Markdown 已同步：改 ${r.updated} · 删 ${r.deleted} · 新增 ${r.added}`); else setErr(r.error || "同步失败");
      };
      const fire = async (name) => { await call("/event", { method: "POST", body: JSON.stringify({ name }) }); refresh(); };

      const s = st && st.state;
      const core = st && st.core;
      const groups = [...new Set(FIELDS.map((f) => f.group))];
      const name = (cfg && cfg.persona && cfg.persona.name) || "她";

      return h("div", { className: "slc" },
        h("h3", { className: "slc-title" }, "人格与记忆"),
        h("p", { className: "slc-intro" }, `给 agent 一个独立人格：会慢慢变化的心情（PID 平滑，不会一句话就大起大落），按项目/会话分层的长期记忆，上下文压缩前自动提炼落盘。`),

        h("div", { className: "slc-card" },
          !core ? h("div", { className: "slc-ok" }, "读取中…")
            : !core.ready
              ? h("div", null,
                  h("div", { className: "slc-err" }, "后端未就绪" + (core.lastError ? "：" + core.lastError : "（启动中…）")),
                  h("div", { className: "slc-hint" }, "Python：" + (core.python || "?")),
                  core.stderrTail && core.stderrTail.length ? h("div", { className: "slc-out" }, core.stderrTail.join("\n")) : null)
              : s ? h(React.Fragment, null,
                  h("div", { style: { display: "flex", justifyContent: "space-between", fontSize: "13px" } },
                    h("span", null, `${name} · ${s.label}`), h("span", { className: "slc-ok" }, `mood ${s.mood.toFixed(3)} → 目标 ${s.target.toFixed(3)}`)),
                  h("div", { className: "slc-bar" }, h("div", { style: { width: `${(s.mood * 100).toFixed(1)}%`, background: moodColor(s.mood) } })),
                  h("div", { className: "slc-hint" }, s.describe),
                  h("div", { className: "slc-kv" },
                    h("span", null, `孤独 ${s.loneliness}`), h("span", null, `性格基线 ${s.baseline}`),
                    h("span", null, `记忆 ${Object.entries(s.memory || {}).map(([k, v]) => `${k}:${(v.hot || 0)}热/${(v.cold || 0)}冷`).join("  ") || "空"}`),
                    st.stats ? h("span", null, `本次启动：注入 ${st.stats.injected} · 写回 ${st.stats.recorded} · 压缩抢救 ${st.stats.compactions}${st.stats.failures ? " · 失败 " + st.stats.failures : ""}`) : null),
                  st.stats && st.stats.lastError ? h("div", { className: "slc-err" }, "最近错误：" + st.stats.lastError) : null,
                  h("div", { className: "slc-actions", style: { marginTop: 4 } },
                    h("button", { className: "slc-btn slc-btn2", onClick: () => fire("praise") }, "试试：夸她"),
                    h("button", { className: "slc-btn slc-btn2", onClick: () => fire("scold") }, "试试：凶她"),
                    h("button", { className: "slc-btn slc-btn2", onClick: restart, disabled: !!busy }, busy === "restart" ? "重启中…" : "重启后端")))
                : h("div", { className: "slc-ok" }, "读取状态…")),

        cfg ? h(React.Fragment, null,
          ...groups.map((g) => h(React.Fragment, { key: g },
            h("div", { className: "slc-group" }, g),
            ...FIELDS.filter((f) => f.group === g).map((f) => h(Field, { key: f.path.join("."), f, cfg, onChange })))),
          h("div", { className: "slc-group" }, "QQ 接入"),
          h(QQCard, { cfg, onChange, st }),
          h("div", { className: "slc-actions" },
            h("button", { className: "slc-btn", onClick: save, disabled: !!busy || JSON.stringify(cfg) === saved }, busy === "save" ? "保存中…" : "保存设置"),
            JSON.stringify(cfg) !== saved ? h("span", { style: { color: "#ffb020", fontSize: "12.5px", alignSelf: "center" } }, "● 有改动未保存") : null)) : null,

        h("div", { className: "slc-group" }, "人设（persona.md）"),
        h("div", { className: "slc-hint" }, "写她是谁、性格、说话方式、底线。{name} 会替换成角色名。情绪和记忆是自动注入的，不用写在这里。"),
        h("textarea", { className: "slc-area", value: persona, onChange: (e) => setPersona(e.target.value) }),
        h("div", { className: "slc-actions" },
          h("button", { className: "slc-btn", onClick: savePersona, disabled: !!busy || persona === personaSaved }, busy === "persona" ? "保存中…" : "保存人设")),

        h("div", { className: "slc-group" }, "翻翻她的记忆"),
        h("div", { className: "slc-actions", style: { marginTop: 0 } },
          h("input", { className: "slc-input", style: { flex: 1 }, placeholder: "关键词，例如：数据库 方案", value: q,
            onChange: (e) => setQ(e.target.value), onKeyDown: (e) => { if (e.key === "Enter") search(); } }),
          h("button", { className: "slc-btn slc-btn2", onClick: search, disabled: !!busy }, "搜索"),
          h("button", { className: "slc-btn slc-btn2", onClick: syncMd, disabled: !!busy, title: "你在 memory/ 下手动改过 notes.md / decisions.md / todo.md 后点这里（每 15 分钟也会自动同步）" },
            busy === "sync" ? "同步中…" : "同步 Markdown 修改")),
        hits ? h("div", { className: "slc-out" }, hits.length ? hits.map((it) => `${it.score.toFixed(3)}  [${it.path} · ${it.kind}]  ${it.content}`).join("\n") : "没有找到。") : null,

        msg ? h("div", { className: "slc-ok" }, msg) : null,
        err ? h("div", { className: "slc-err" }, err) : null,
        st && st.home ? h("div", { className: "slc-hint" }, `数据目录：${st.home}（memory.db 是记忆库，memory/ 下是按目录树归档的 Markdown）`) : null);
    }

    const inject = ["slots"];
    const NS = "soulcore";
    function apply(ctx) {
      ctx.effect(
        () => ctx.slots.inject("settings.section", () =>
          ctx.slots.register({ name: "settings.section", id: NS, order: 7, label: () => "人格与记忆" }, SoulcoreSection),
          "soulcore: settings section"),
        "soulcore: settings section");
    }
    exports.apply = apply;
    exports.inject = inject;
    exports.SoulcoreSection = SoulcoreSection;
    exports.FIELDS = FIELDS;
    exports.QQ_FIELDS = QQ_FIELDS;
    return module.exports;
  },
});
