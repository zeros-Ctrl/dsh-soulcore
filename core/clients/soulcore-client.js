/**
 * soulcore 的 JS 客户端（Node 18+ / 任何有 fetch 的环境）。
 * 用途：在你的 DeepSeek harness 插件里调用 soulcore 的钩子，不需要改 soulcore 本身。
 *
 * harness 的插件 API 我还没看到，所以下面 example 里的 onUserMessage / onCompact 等
 * 只是示意——把三个钩子挂到 harness 对应的生命周期上即可：
 *   1) 发给模型之前  → before(event)   拿到 system（人格+情绪+记忆）和 history
 *   2) 模型回复之后  → after(event, reply)
 *   3) 上下文压缩前  → preCompact(event, messagesToBeDropped)
 */
class SoulcoreClient {
  constructor(base = "http://127.0.0.1:8765", token = "") {
    this.base = base.replace(/\/$/, "");
    this.token = token;
  }
  async _req(method, path, body) {
    const r = await fetch(this.base + path, {
      method,
      headers: { "Content-Type": "application/json", ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) throw new Error(`soulcore ${path} -> HTTP ${r.status}: ${await r.text()}`);
    return r.status === 204 ? null : r.json();
  }
  /** 构造事件。platform: qq|bilibili|local；scene: private|group|dm|comment|project|session */
  static event({ platform = "local", scene = "session", channelId = "default", userId = "owner",
                 userName = "", text = "", mentionsSelf = true } = {}) {
    return { platform, scene, channel_id: String(channelId), user_id: String(userId), user_name: userName,
             text, mentions_self: mentionsSelf, ts: Date.now() / 1000 };
  }
  before(event) { return this._req("POST", "/v1/hooks/before", { event }); }
  after(event, reply) { return this._req("POST", "/v1/hooks/after", { event, reply }); }
  preCompact(event, messages) { return this._req("POST", "/v1/hooks/pre_compact", { event, messages }); }
  chat(event) { return this._req("POST", "/v1/chat", { event }); }          // 全托管：soulcore 自己调 DeepSeek
  state() { return this._req("GET", "/v1/state"); }
  search(q, path = "core") { return this._req("GET", `/v1/memory/search?q=${encodeURIComponent(q)}&path=${encodeURIComponent(path)}`); }
  emotionEvent(name) { return this._req("POST", "/v1/event", { name }); }
}

/* ------------------------- 示意：挂到 harness 上 ------------------------- */
async function example() {
  const soul = new SoulcoreClient();
  const ev = SoulcoreClient.event({ platform: "local", scene: "project", channelId: "毕设", userName: "阿明",
                                    text: "检索那块我们之前为什么不用向量库？" });
  // 1) 发给模型之前
  const ctx = await soul.before(ev);
  const messages = [{ role: "system", content: ctx.system }, ...ctx.history];
  console.log("情绪:", ctx.label, ctx.mood.toFixed(2), "召回记忆:", ctx.memories.length);
  // 2) harness 自己调模型（这里假装）
  const reply = "因为本地部署要轻，SQLite+FTS5 就够用了。";
  await soul.after(ev, reply);
  // 3) harness 压缩上下文前，把要被丢掉的那段交给 soulcore 先落盘
  // await soul.preCompact(ev, droppedMessages);
  return messages;
}

module.exports = { SoulcoreClient, example };
if (require.main === module) example().then(() => console.log("ok")).catch(e => { console.error(e); process.exit(1); });
