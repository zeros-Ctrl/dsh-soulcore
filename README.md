# dsh-soulcore · 人格与记忆

给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（DSH）里的 agent 加三样东西：

- **独立人格**：她有自己的性格基线，不会只顺着用户的情绪走。
- **会慢慢变化的情绪**：事件只改变她"想到达的心情"，实际心情由 PID 平滑地跟过去，相邻两分钟之间几乎不会跳变。
- **分层长期记忆**：按项目、会话归档；权重会随时间衰减，被想起时刷新；**上下文压缩之前会先把内容提炼落盘**，压缩后不会失忆。

不需要另外装 Python：插件直接使用 DSH 自带的 Python 3.12 运行时，而且核心只依赖标准库。

## 安装

```bash
# 从 GitHub 安装
dsh plugin --profile desktop add github:zeros-Crtl/dsh-soulcore

# 或者从本地目录安装（开发时用）
dsh plugin --profile desktop add "link:C:\Users\<你>\.dsh\plugins\dsh-soulcore"
```

装好后重启 DSH，打开 **设置 → 人格与记忆**：

1. 顶部状态卡显示心情条。"后端就绪"说明装好了。
2. 修改人设（persona.md），设置角色名和她对你的称呼。
3. （可选）填写 DeepSeek API Key，记忆提炼会更聪明。不填也能用，会改用规则提炼。

可以点状态卡上的"试试：夸她 / 凶她"，观察心情条在几十分钟内慢慢变化。

## 它挂在 DSH 的哪些地方

| DSH 挂点 | 做什么 |
|---|---|
| `systemPrompt.section` | 把人设写进 system prompt。内容稳定，不会破坏提示缓存 |
| `agent/pre-step`（每轮第一步） | 把用户这句话交给后端：判断情绪事件（被夸、被凶、道歉……），更新心情和好感，召回相关记忆，再追加一条 snapshot 消息（与 DSH 自带的 time-context 做法相同） |
| `session/event` → `turn/end` | 把她这一轮的最终回复写回记忆；每 6 轮提炼一次长期记忆 |
| `llm/stream`（`purpose: "compaction"`） | 压缩前拿到即将被压掉的原文，提炼成事实、决策、偏好后落盘 |
| `tools` | `memory_search`、`remember`、`mood_status`，让模型能主动查记忆、记东西 |
| `webServer` + 客户端 section | 设置页。走 DSH 信任栅栏；栅栏不可用时拒绝访问（fail-closed）；密钥永不回显 |

**这些都是旁路增强。** 后端挂了或超时（默认 4 秒），DSH 照常工作，只是这一轮不注入。

## 数据放在哪

```
~/.dsh/soulcore/
  config.json          配置（设置页改的就是它；密钥也在这，权限 600）
  persona.md           人设
  data/memory.db       记忆库（SQLite + FTS5）
  data/emotion.json    情绪状态（重启后接着上次的心情）
  data/memory/         Markdown 镜像，按目录树归档，可以直接打开看
    project/<工作目录名>/decisions.md
    core/people/local/owner/notes.md
    chat/qq/group/<群号>/...      （开启 QQ 接入后才有）
```

如果想改这个位置，设置环境变量 `SOULCORE_HOME`。

## 记忆是怎么工作的（借鉴 LSTM 的门控）

- **输入门**：每条记忆都有重要性分。寒暄打低分，"记住……"、决策、偏好打高分。
- **遗忘门**：权重 `w = 重要性 × e^(-Δt/τ) × (1 + 0.3·ln(1+被检索次数))`。τ 按类型设定：闲聊 3 天、事实 2 个月、决策 1 年。被想起一次就重新计时。太弱的记忆转为冷存储，不删除。
- **输出门**：先搜当前项目，不够再逐级回退到父级、core，最后是全局。
- **细胞状态**：冷掉的原始对话攒够 30 条，就合并成一条摘要。

隐私规则（有测试覆盖）：

- 原始聊天只在本会话内可见，跨会话只会召回提炼过的条目。
- 别人的私聊和人物档案永远查不到。
- 项目记忆默认不会进 QQ 和 B站。
- B站评论区是公开场景，看不到任何其他聊天分支。

## 情绪模型

情绪值的范围是 (0, 1)，0 是悲伤，1 是开心，两端都到不了。计算过程：

1. 事件和孤独感决定设定点。
2. 设定点先经过前置滤波。
3. PID 加情绪惯性让实际心情跟随设定点。
4. 限速：每分钟最多变化 0.008。

同一件事每次的冲击强度是随机抽样的（对数正态分布），所以同样被冷落，有时会"低落"，有时只是"有点闷"。实验和曲线图见 [`docs/emotion/`](docs/emotion)。

## 可选：接入 QQ / B站

后端同时也是一个 OneBot v11 上报接收端，以及 B站轮询器（带人工审核队列，遇到风控码会立即暂停）。在 `~/.dsh/soulcore/config.json` 里打开 `qq.enabled` 或 `bilibili.enabled`，并填写对应字段即可。

注意：在这两种场景下，回复由后端自己调用 DeepSeek 生成，所以**必须填写 API Key**。

详细说明见 [`docs/core.md`](docs/core.md)。请使用小号，在简介里声明是 AI，并遵守平台规则。

## 开发

```bash
npm test                       # 9 个 JS 集成测试（用模拟的 Cordis ctx 驱动真实的 Python 后端）+ 13 个 Python 测试
cd core && python -m soulcore --mock demo    # 离线演示：模拟一整天
cd core && python -m soulcore --mock chat    # 命令行里直接和她聊（/skip 快进时间，/event 触发事件）
```

目录：

| 路径 | 内容 |
|---|---|
| `lib/index.js` | 宿主侧插件（Cordis） |
| `lib/client.js` | 设置页 |
| `lib/core-process.js` | 管理后端子进程：握手、Bearer token 鉴权、崩溃自动重启、跟随 DSH 退出 |
| `core/soulcore/` | Python 后端（情绪、记忆、人格 prompt、HTTP 接口、QQ/B站适配） |

兼容性：基于 DSH 0.2.1-rc.2 的插件接口开发。DSH 还处于开发者预览阶段，接口可能会变，遇到问题请提 issue。

## License

MIT © zeros-Crtl
