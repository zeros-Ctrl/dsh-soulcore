# dsh-soulcore · 给 AI 编程助手和聊天机器人加上跨会话长期记忆和稳定人格，纯本地、零依赖

只用 Python 标准库，不装任何第三方包；记忆、情绪、人设全在你自己机器上。

| 用户 | 用在哪 | 一行安装 | 得到什么 |
|---|---|---|---|
| 写代码的（DSH） | [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) | `dsh plugin --profile desktop add github:zeros-Crtl/dsh-soulcore` | 人设 + 情绪 + 按项目归档的记忆，压缩前自动抢救 |
| 写代码的（Claude Code） | Claude Code CLI | `/plugin marketplace add zeros-Crtl/dsh-soulcore` + `/plugin install soulcore@dsh-soulcore` | 跨会话记忆（可切完整人格）+ 6 个 MCP 工具 |
| 任何 MCP 客户端 | Cursor / Claude Desktop / Cherry Studio | 见 [docs/mcp.md](docs/mcp.md) | 同一份记忆，工具调用 |
| 聊天机器人 | QQ（NapCat） | 见 [docs/qq.md](docs/qq.md) | QQ 私聊/群 + B站 + 主动聊天 |
| 开发者 | 自己的 bot | 见 [docs/http-api.md](docs/http-api.md) | 一套 HTTP 钩子，30 行接入 |

> 上面两条 GitHub 安装命令要等仓库推送到 GitHub 之后才能用（本版本没有替你 push）。
> 本地先用 `dsh plugin add "link:<本地目录>"` 或 `claude --plugin-dir <本地目录>`。

```
① 新会话还记得（第 1 天说，第 30 天问"我们为什么不用向量库"）
  【没有插件】模型只看得到这一句，之前 30 天的对话一条都看不到
  【装了 soulcore】- [decision·30天前] 主人说：检索层决定用 SQLite+FTS5，不上向量库，因为要本地轻量部署

② 知道哪些说法过时了（问"以前部署在哪"）
  - （已过时）[fact·2个月前] 博客部署在香橙派上
  - [fact·刚才] 博客已经迁到阿里云服务器上了

③ 心情是慢慢变的：夸一次 0.62 → 30 分钟 0.70；凶一次 0.62 → 60 分钟 0.47（不会跳变）
```

| 200 天长记忆评测（规则提炼、离线模拟、seed 42） | 结果 |
|---|---|
| 知识更新（旧说法不再冒充现状） | 8/8 |
| 时间问题（"昨天/3天前决定了什么"） | 4/4 |
| 超长间隔 + 热干扰下的冷回想 | 6/6 |
| 普通召回对照 | 4/4 |
| 隐私泄漏（群里的陌生人打听主人私事） | 0 |
| 每轮注入 | 平均 470 字，纯寒暄 90 字（v0.9 stable 拆分后） |

**先试试**（不用 key、不联网、30 秒）：

```bash
python -m soulcore --mock showcase
```

## 文档

- [记忆是怎么工作的](docs/how-memory-works.md)：门控、衰减、知识更新、时间检索、冷回想、隐私规则
- [情绪模型](docs/emotion.md)
- [QQ / B站 / 主动聊天](docs/qq.md)
- [Claude Code 接入](docs/claude-code.md)
- [MCP 接入](docs/mcp.md)（Claude Code / Cursor / Claude Desktop / Cherry Studio）
- [HTTP 接口](docs/http-api.md)（AstrBot / NoneBot / 自写 bot）
- [配置项全表](docs/config.md)
- [DSH 插件开发说明](docs/core.md)

## 隐私与数据

- **所有数据都在本地**：`~/.dsh/soulcore/`（或 `SOULCORE_HOME`）下的 `memory.db`、Markdown 镜像、情绪状态；
- 只有**提炼、摘要、画像**这几步会把**压缩后的对话片段**发给 DeepSeek（填了 key 才会发）；
- **没填 key 时完全离线**：记忆用规则提炼，不联网；
- 想删掉全部数据：直接删掉 `<home>` 目录；想删单条：`forget` 接口 / `/soulcore:memories` 命令 / 直接编辑 Markdown 镜像。

## 开发

```bash
npm test                                             # JS 集成测试 + Python 单元/集成测试
cd core && python -m unittest discover -s tests      # 只跑 Python
cd core && python -I bench/eval_longmem.py . ../docs/benchmark/longmem_v09.json --split-stable
cd core && python -m soulcore --mock demo            # 离线演示：模拟一整天
cd core && python -m soulcore --mock chat            # 命令行聊天（/skip 快进时间，/event 触发事件）
```

目录：

| 路径 | 内容 |
|---|---|
| `lib/index.js` | DSH 宿主侧插件（Cordis） |
| `lib/core-process.js` | 后端子进程：握手、Bearer token、崩溃重启、跟随 DSH 退出 |
| `core/soulcore/` | Python 后端（情绪、记忆、人格 prompt、HTTP、MCP、Claude Code hook） |
| `hooks/` `bin/` `.claude-plugin/` | Claude Code 插件打包 |
| `docs/` | 上面那些文档 |

兼容性：DSH 插件基于 DSH 0.2.1-rc.2 的接口开发（DSH 还在开发者预览阶段，接口可能变）；
Claude Code 部分按 2026 年 11 月的 hooks / plugins / MCP 官方文档实现。遇到问题请提 issue。

## License

MIT © zeros-Crtl
