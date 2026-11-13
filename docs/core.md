# soulcore 后端：架构 / 接入方式 / 开发入口

> 这是 dsh-soulcore 的 Python 后端说明。作为 DSH 插件使用时不需要手动启动它；
> 下面的命令都在 `core/` 目录下执行。面向"要改代码 / 要接自己的 harness"的人，
> 使用者文档看 [README](../README.md) 和 `docs/` 下其它几篇。

核心只用 Python 标准库（Python **3.10+**），不装任何第三方包；只有"后端自带的 B站轮询"是例外，
需要可选安装 `bilibili-api-python==17.4.2`（不装也能用其它功能）。

```
QQ ── NapCat ──(WebSocket / HTTP)──┐
B站 ── 轮询 ── 人审队列 ───────────┼─► Event ─► Engine ─► DeepSeek（可选）
Claude Code ── hooks ─────────────┤           │
任何 harness ──(HTTP / MCP)───────┘    ┌──────┼──────────┐
                                    情绪(PID)  关系(好感)  记忆(SQLite+Markdown)
```

## 1. 先离线跑一遍

```bash
python -m soulcore --mock showcase               # 三段对照演示（新会话还记得 / 过时说法 / 心情变化），< 30 秒
python -m soulcore --mock demo                   # 模拟一整天：QQ 私聊/群聊、B站评论、项目讨论、余额变化
python -m soulcore --mock chat --project 毕设     # 命令行聊天：/help、/skip 快进时间、/event 触发情绪、/mem 查记忆
python -m unittest discover -s tests             # 离线 Python 测试
```

## 2. 配置与启动

```bash
cp config.example.json config.json               # 填 llm.api_key，或设置 DEEPSEEK_API_KEY
python -I run.py --config <config.json> serve    # 默认 127.0.0.1:8765，实际端口和 token 写进 soulcore.lock
python -I run.py --config <config.json> serve --port 0 --idle-exit-min 240   # 随机端口 + 四小时空闲自退
```

人格写在 `persona.md`，`{name}` 会替换成 `persona.name`。
常驻（开机自启）用 `install-service` / `uninstall-service` / `service-status`。
完整配置项见 [config.md](./config.md)。

## 3. 三种接入方式

| 方式 | 入口 | 适合 | 文档 |
|---|---|---|---|
| 全托管 | `POST /v1/chat` | 不想自己拼人格和记忆的 bot | [http-api.md](./http-api.md) |
| 钩子式 | `POST /v1/hooks/{before,context,after,pre_compact,flush}`、`GET /v1/persona/stable` | 已经有自己 system prompt 的 harness（DSH 用它） | [http-api.md](./http-api.md) |
| MCP | `python -m soulcore mcp`（stdio） | Claude Code / Cursor / Claude Desktop | [mcp.md](./mcp.md) |
| Claude Code hook | `python -m soulcore cc-hook <event>` | 想要"自动注入 + 自动记"而不是手动调工具 | [claude-code.md](./claude-code.md) |

事件格式（所有 HTTP 接口共用）：

```json
{"platform": "local", "scene": "project", "channel_id": "毕设", "user_id": "owner",
 "user_name": "主人", "text": "...", "mentions_self": true}
```

`(platform, scene, channel_id)` 决定记忆路径和可信度，隐私规则见
[how-memory-works.md](./how-memory-works.md)。

## 4. 后端模块

| 文件 | 作用 |
|---|---|
| `engine.py` | 把情绪 / 关系 / 记忆 / LLM 串起来；`stable_block()`、`context_block()`、`_maybe_extract()` |
| `memory.py` | 分层记忆库：写入、衰减、分层检索、冷回想、知识更新、Markdown 镜像（`md_lock` 串行化） |
| `persona.py` | 人设卡、人格锚点、`system` prompt 和每轮注入文本的拼装 |
| `emotion.py` | PID 情绪模型（设定点 → 前置滤波 → PID → 限速） |
| `extractor.py` | 记忆提炼（LLM）/ 规则兜底、提炼门控、成本统计 |
| `conflicts.py` | 规则版冲突检测（"搬家了""换导师了""戒咖啡了"） |
| `timeparse.py` | "明天三点答辩" → 跟进时刻；"昨天" → 检索时间窗 |
| `server.py` | HTTP 服务、锁文件、空闲退出 |
| `client.py` | 短命进程（hook / MCP / CLI）用的后端客户端：`resolve_home`、`Backend.ensure/request` |
| `cc_hook.py` | Claude Code 的 5 个 hook 事件适配（失败即放行） |
| `mcp_server.py` | 标准库实现的 stdio MCP server（6 个工具） |
| `cc_install.py` | 把 hooks 合并进 Claude Code 的 `settings.json` |
| `showcase.py` | `--mock showcase` 的三段离线演示 |
| `adapters/` | QQ（OneBot v11）、B站轮询、主动聊天 |

## 5. 数据目录

```
~/.dsh/soulcore/                （或 SOULCORE_HOME；第一次运行自动创建）
  config.json                   配置（密钥也在这，权限 600）
  persona.md                    人设
  soulcore.lock                 后端单实例锁：{pid, port, token, version}
  logs/serve.log                后端日志
  logs/cc-hook.log              Claude Code hook 日志（>1MB 轮转）
  data/memory.db                记忆库（SQLite + FTS5）
  data/emotion.json             情绪状态（重启后接着上次的心情）
  data/memory/                  Markdown 镜像，按目录树归档，可以直接改/删/加
    project/<项目名>/decisions.md
    core/people/owner/notes.md
    chat/qq/group/<群号>/...
```

SQLite 是权威数据，Markdown 是给你看的双向镜像：直接改/删/加，下一次同步会回到数据库。

## 6. 单实例与自启动

后端启动时先抢 `soulcore.lock`（操作系统文件锁），再初始化数据库；抢不到的进程打印
`SOULCORE_ALREADY_RUNNING` 后退出，不会出现两个进程同时写 `memory.db`。

DSH 插件、Claude Code hook、MCP server 这些短命进程都通过 `client.Backend` 接入已有后端：
`discover()` 读锁 + `/v1/health`，没有就后台分离启动一个（带 `--idle-exit-min`，会自己退），
401 / 连接断开时重读锁文件重试一次。所以 DSH 和 Claude Code 共用同一份记忆、同一个进程。

## 7. 测试与评测

```bash
npm test                                             # JS 集成测试 + Python 单元/集成测试
python -m unittest discover -s tests                 # 只跑 Python
python -I bench/eval_longmem.py . ../docs/benchmark/longmem_v09.json --split-stable
```

`bench/` 下还有 `eval_retrieval.py`（检索消融）和 `ab_emotion.py`（情绪对照），
数据与结论见 [benchmark/LONGMEM.md](./benchmark/LONGMEM.md)。

## 8. 已知局限

- **关键词判断事件比较粗糙**：有预算就把 `appraisal.mode` 改成 `llm`。
- **语义向量默认关闭**：检索是二元组 + FTS5，同义不同词召回不到；配了 embedding 才会走语义那一支。
- **后端自带的 B站轮询**（`bilibili.enabled`）只在模拟接口上测过流程，真实账号请先用 `bili --once` + 人审小心试；
  QQ 里刷 B站走的是 dsh-bili-whale 的只读命令，那条链路已经用真账号跑过。
- **Claude Code 的 `/plugin install` 和"DSH 里说过的决策在 Claude Code 里能答"** 还没有在真机上逐条验证
  （`claude plugin validate` 通过；两个真实会话的 hook 注入、MCP 工具调用已经验证）。
- **K7（真实 LLM 小规模评测）/ K8（OpenAI 兼容代理）没有实现**：任务书里是可选任务。

## 9. 历史文档

- [v0.8-review.md](./v0.8-review.md)、[v0.8.1-fixes.md](./v0.8.1-fixes.md)：上一轮复审与修复记录
- [v0.9-delivery.md](./v0.9-delivery.md)：0.9 交付说明（任务落点与验证记录）
- [chat.md](./chat.md)、[install.md](./install.md)：0.10 的聊天控制与下载安装说明
- [benchmark/LONGMEM.md](./benchmark/LONGMEM.md)：200 天长记忆评测的方法和数据
