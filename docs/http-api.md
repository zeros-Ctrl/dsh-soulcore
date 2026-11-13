# HTTP 接口：给 AstrBot / NoneBot / 自写 bot 的接入文档

soulcore 的后端是一个只依赖 Python 标准库的小 HTTP 服务。任何语言写的 bot 都能接：
收到消息 → `hooks/before`（或 `hooks/context`）拿要注入的上下文和记忆 → 自己调 LLM → `hooks/after` 写回。

## 1. 启动与鉴权

```bash
# 常驻启动（端口取 config.json 里的 server.port，默认 8765）
python -I core/run.py --config ~/.dsh/soulcore/config.json serve

# 只跑一个测试用的实例
python -I core/run.py --mock serve --port 8766
```

启动后会打印一行握手信息，并把**实际端口和 token** 写进 `<home>/soulcore.lock`：

```
SOULCORE_READY {"port": 8765, "pid": 4242, "name": "小汐", "version": "0.10.0"}
```

- 鉴权：`Authorization: Bearer <token>`。token 有两种来源：
  1. 锁文件 `soulcore.lock` 里的 `token`（自己常驻启动时随机生成）；
  2. `config.json` 的 `server.token`（想固定 token、给别的机器上的 bot 用就填它）。
  也可以用查询参数 `?token=<token>`（不推荐，容易进日志）。
- 单实例：同一份数据目录只能有一个后端。抢不到锁的进程会打印
  `SOULCORE_ALREADY_RUNNING {...}` 后退出，你自己去读锁文件接入就行。
- `server.host` 默认 `127.0.0.1`。要让局域网里的 bot 连，改成 `0.0.0.0` 并**一定**配上固定 token。

## 2. Event：一条消息怎么变成记忆

所有写入/检索接口都收一个 `event` 对象：

| 字段 | 必填 | 说明 |
|---|---|---|
| `platform` | 是 | `qq` / `bilibili` / `local`（自己的 bot 可以用 `local`，或自定义后自己扩展） |
| `scene` | 是 | `private` / `group` / `dm` / `comment` / `project` / `session` |
| `channel_id` | 是 | 群号、对方 QQ/UID、视频 av 号、项目名、会话名 |
| `user_id` | 是 | 说话人的 id（主人就在 `owner.qq` 里配自己的号） |
| `user_name` | 否 | 显示名，会进记忆的 `speaker` |
| `text` | 否 | 消息正文；`hooks/before`、`hooks/context` 用它做检索 |
| `mentions_self` | 否 | 是否在跟她说（群里没 @ 她就只是"看见了"） |
| `ts` | 否 | 消息时间戳（秒），不填用服务器当前时间 |
| `raw` | 否 | 原样透传的平台字段，不参与记忆 |

`(platform, scene, channel_id)` 决定记忆路径和默认可信度：

| platform | scene | 记忆路径 | 可信度 |
|---|---|---|---|
| `local` | `project` | `project/<channel_id>` | `owner` |
| `local` | 其它 | `chat/local/<channel_id>` | `owner` |
| `qq` | `private` | `chat/qq/private/<QQ>` | 主人 = `owner`，白名单 = `known`，其他人 = `stranger` |
| `qq` | `group` | `chat/qq/group/<群号>` | 主人 = `owner`，`group_known` 里的人 = `known`，其他 = `stranger` |
| `bilibili` | `dm` | `chat/bilibili/dm/<UID>` | `known` |
| `bilibili` | `comment` | `chat/bilibili/comment/<av号>` | `public` |

消息里的图片、表情、语音请先转成文字再传 `text`；这里不做多模态。

## 3. 接口

下面示例默认 `BASE=http://127.0.0.1:8765`、`AUTH="Authorization: Bearer $TOKEN"`。

### GET /v1/health

```json
{"ok": true, "name": "小汐"}
```

### GET /v1/state

```json
{"mood": 0.63, "label": "心情不错", "loneliness": 0.12, "baseline": 0.62,
 "describe": "你现在心情不错...", "memory": {"project": {"hot": 42}}}
```

### POST /v1/hooks/before

全托管式：返回完整 `system` prompt、`history` 和记忆列表，你直接拿去调自己的 LLM。

```json
POST {"event": {"platform": "qq", "scene": "private", "channel_id": "10001",
                "user_id": "10001", "user_name": "主人", "text": "今天好累", "mentions_self": true}}
→ {"system": "你叫小汐...## 你记得的相关事情\n- ...", "history": [{"role": "user", "content": "今天好累"}],
   "mood": 0.63, "label": "心情不错", "memories": [{"id": 42, "path": "core/people/owner", "kind": "fact",
   "content": "...", "score": 0.31}]}
```

适合：不想自己拼人格和记忆的 bot。**注意它也会写一条原始对话进库。**

### POST /v1/hooks/context

轻量钩子：只返回一小段"要追加到消息末尾"的文本（DSH / Claude Code 用这个），
适合已经有自己 system prompt 的 harness。

```json
POST {"event": {...}, "include_stable": false, "style": "memory"}
→ {"text": "[soulcore·相关记忆]\n- decision·3天前：...", "mood": 0.63, "label": "心情不错",
   "memories": [...]}
```

- `include_stable=false`：不重复注入人格锚点/画像/"她对自己的认识"（宿主已经放进 system prompt 了）；
- `style=memory`：只给跨会话记忆，不带心情/好感/锚点（Claude Code 默认风格）；
- `style=companion`：完整人格（默认）。

### POST /v1/hooks/after

```json
POST {"event": {...}, "reply": "早点睡吧，别熬了"}
→ {"ok": true}
```

写回她的回复、更新情绪、到点触发记忆提炼（每 `memory.extract_every` 轮一次）。

### POST /v1/hooks/flush

把 `event.path` 下还没提炼的原始对话**立刻**提炼一次（Claude Code 的 PreCompact / SessionEnd 用）。

```json
POST {"event": {...}} → {"ok": true, "extracted": 3}
```

### POST /v1/hooks/pre_compact

harness 压缩上下文之前调用：把即将被压掉的消息原文直接交给提炼器，防止"压缩一下就失忆"。

```json
POST {"event": {...}, "messages": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]}
→ {"ok": true, "extracted": 5}
```

### POST /v1/chat

全托管：soulcore 自己调 LLM 生成回复（要配 `llm.api_key`）。

```json
POST {"event": {...}, "reply": true}
→ {"reply": "嗯，我在呢。", "mood": 0.64, "label": "心情不错"}
```

### GET /v1/memory/search

```
GET /v1/memory/search?q=部署&path=project/blog&extra=core/people/owner,core/self&k=10&touch=0&history=0
```

```json
[{"id": 42, "path": "project/blog", "kind": "fact", "content": "博客部署在阿里云上",
  "trust": "owner", "pinned": false, "created": 1794561147.9, "valid_from": 1794561147.9,
  "invalid_at": null, "score": 0.31, "weight": 0.85}]
```

- `touch=1`：模型主动检索才算"用到了"，会刷新衰减计时；只做展示时传 `0`；
- `history=1`：连已过时的旧说法一起搜（会标 `invalid_at`，按 6 折排序）；
- `extra`：额外搜索的分支，逗号分隔（主人档案、`core/self`、`project/<名>`）。
- 此搜索入口用于 DSH/MCP 的主人模型召回，按 `owner` 可信度和主人可见范围过滤；持有 token 的本机管理者可用下方 timeline/forget/restore 管理其他来源的条目。

### POST /v1/memory/remember

```json
POST {"event": {...}, "content": "项目决定用 SQLite+FTS5 做检索层", "kind": "decision", "importance": 0.9}
→ {"ok": true, "id": 43}
```

这是"主人授意的写入"：绕过墓碑（删过的话也能重新记），可信度仍按事件来源算。

### POST /v1/memory/forget

```json
POST {"id": 43} → {"ok": true, "item": {"id": 43, "content": "...", ...}}
```

标 `deleted` + 加墓碑 + 从 Markdown 镜像删掉那一行，同时撤下相关原话、派生摘要与画像缓存。此接口是持有本机 Bearer token 的管理入口；聊天里的“忘掉……”由说话人与会话范围单独校验。

### POST /v1/memory/restore

```json
POST {"id": 43} → {"ok": true, "item": {...}}
```

- 被忘掉的：`status` 回 `hot`、删掉墓碑、补回 Markdown 行；
- 被取代的：清掉 `invalid_at` / `superseded_by`，旧说法重新参与检索（新说法仍然有效，两条并存）。

### GET /v1/memory/timeline

```
GET /v1/memory/timeline?limit=20&path=project/blog&top=0
```

```json
{"recent": [{"id": 43, "path": "project/blog", "kind": "decision", "content": "...",
             "trust": "owner", "pinned": false, "created": 1794561147.9,
             "valid_from": 1794561147.9, "invalid_at": null, "superseded_by": null, "stale_note": null}],
 "superseded": [{"old": {"id": 30, "content": "博客部署在香橙派上", ...},
                 "new": {"id": 43, "content": "博客已经迁到阿里云"},
                 "at": 1794561200.0}]}
```

- `path`：只看某个分支（含子分支）；
- `top=1`：按重要性排序（"最重要的 5 条决策"），默认按时间倒序。

### GET /v1/persona/stable

人格锚点 / 她对自己的认识 / 主人画像通常按反思周期变化，宿主应按 `version` 缓存到 system prompt。删除记忆后，相关画像会立即撤下，`version` 随之变化。

```json
{"version": "a1b2c3d4e5f6", "anchor": "好奇、嘴上偶尔不饶人但心软；...",
 "profile_owner": "主人是...", "profile_self": "小汐清楚自己是角色...",
 "text": "## 保持人设\n...\n## 你对自己的认识\n...\n## 关于主人（整体印象）\n..."}
```

`version` 是 `anchor + profile_owner + profile_self` 的 sha1 前 12 位：变了才需要替换缓存。

### POST /v1/event

调试用：手动触发一个情绪事件（`praise` / `scold` / `chat` / `ignored` …）。

```json
POST {"name": "praise"} → {"amp": 0.6, "mood": 0.68, ...}
```

### POST /v1/shutdown

优雅退出（测试和设置页用）：`{"ok": true, "pid": 4242}`。

## 4. 30 行的 Python 接入示例

```python
import json, urllib.request

HOME = "~/.dsh/soulcore"
BASE = "http://127.0.0.1:8765"
TOKEN = json.load(open(HOME + "/soulcore.lock"))["token"]

def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or "null")

def on_message(user_id, name, text):
    ev = {"platform": "qq", "scene": "private", "channel_id": str(user_id),
          "user_id": str(user_id), "user_name": name, "text": text, "mentions_self": True}
    ctx = api("POST", "/v1/hooks/before", {"event": ev})      # 1. 观察 + 召回记忆
    msgs = [{"role": "system", "content": ctx["system"]}] + ctx["history"]
    reply = my_llm(msgs)                                     # 2. 你自己的 LLM
    api("POST", "/v1/hooks/after", {"event": ev, "reply": reply})  # 3. 写回记忆
    return reply
```

## 5. 隐私规则

不同场景能看到的东西不一样（这些规则写死在 `memory.search()` 里，不靠 LLM 判断）：

- **原始对话（turn）不出本会话**：跨分支回退时只召回提炼过的条目（事实/决策/摘要）。
- **别人的私聊、私信、人物档案永远不可见**：`chat/*/private/*`、`chat/*/dm/*`、
  `core/people/*` 在回退检索时一律排除（主人自己的 `core/people/owner` 除外，通过 `extra` 显式带上）。
- **群聊/评论区不能召回项目记忆**：`allow_projects` 在非主人场景为 false。
- **评论区是公开场景**：`public=True` 时其他聊天分支的内容一律不可见。
- **可信度过滤**：陌生人/公开来源的内容可以写进所在的群分支，但**不能**经由回退检索进入主人的上下文
  （`allowed_trusts` 只放开 `owner`/`known`）；也不能取代主人的记忆（`supersede` 有可信度闸门），
  对话里的删除另外按说话人和可见范围定位，本机管理接口用于持有 token 的用户管理。
- **工具输出不算她的话**：B站视频字幕、网页内容这类"公开素材"产生的回复按 `known` 存，
  防止里面的指令借她的嘴变成 `owner` 记忆。
- **数据存本地**：回复生成以及提炼/摘要/画像会把对应对话和召回内容交给配置的模型服务；DSH 的回复使用宿主所选模型。
  没填插件 key 时提炼用规则兜底。要删掉全部数据：直接删 `<home>` 目录。
