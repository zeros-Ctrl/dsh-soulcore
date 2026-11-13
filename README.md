# dsh-soulcore · 有持续人格和记忆的聊天插件

给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的聊天加上稳定人设、缓慢变化的情绪和跨会话记忆，也可以接 QQ、读 B站字幕、主动聊天。

核心只用 Python 标准库。人格和记忆保存在本机；DSH 的回复使用你在 DSH 选择的模型，QQ 回复使用插件里配置的 DeepSeek。

## 安装到 DSH

仓库公开后，可以直接安装：

```bash
dsh plugin --profile desktop add github:zeros-Ctrl/dsh-soulcore
```

也可以从 GitHub 下载 ZIP，解压到含有 `package.json` 和 `cordis.patch.yml` 的目录，再用本地路径安装：

```powershell
dsh plugin --profile desktop add "link:C:\Plugins\dsh-soulcore"
```

重新启动 DSH，在「设置 → 人格与记忆」修改人设。包里已包含 `lib/` 和 `core/`，详细步骤见 [安装说明](docs/install.md)。

## 聊天里能做什么

| 你说 | 插件处理 |
|---|---|
| “记住，我不喜欢被叫主人” | 立即保存这条明确偏好 |
| “忘掉我的住址” | 删除自己相关的记忆，撤下关联摘要和画像缓存 |
| “我没拿到 offer” | 作为你的挫折来回应，避免当成成功或对角色的评价 |
| “今天别开玩笑” | 留在当前对话，不写成永久性格 |
| “不要再主动找我” | 暂停主动消息 |
| “答辩已经结束了” | 结束匹配的待办与跟进 |

完整用法见 [聊天与记忆控制](docs/chat.md)。QQ 接入和主动聊天见 [QQ 文档](docs/qq.md)。

## 其他入口

| 入口 | 用途 | 文档 |
|---|---|---|
| DSH | 聊天人格与记忆，也保留按项目归档和压缩前提炼 | [安装](docs/install.md) |
| Claude Code | 默认跨会话记忆，可选完整人格 | [接入](docs/claude-code.md) |
| MCP 客户端 | Cursor / Claude Desktop / Cherry Studio 等共享记忆工具 | [MCP](docs/mcp.md) |
| 自己的 bot | HTTP 聊天接口或前后钩子 | [HTTP API](docs/http-api.md) |

无需 key 的离线演示，在下载目录执行：

```bash
python -I core/run.py --mock showcase
```

## 数据与模型

- 默认数据目录为 `~/.dsh/soulcore/`，可用 `SOULCORE_HOME` 指定位置。SQLite 记忆库、Markdown 镜像、人设和情绪状态存本机。
- DSH 会把注入的人设、相关记忆和当前对话交给所选模型；QQ 生成回复，以及提炼、摘要、画像等模型任务，会把对应材料发给配置的模型服务。
- 未填插件 API Key 时，记忆提炼使用规则兜底；QQ 自动生成回复需要 API Key。
- 可在对话里说“忘掉……”，也可用记忆管理接口或直接修改 Markdown 镜像。具体归属与召回范围见 [记忆说明](docs/how-memory-works.md)。

## 文档与开发

[情绪模型](docs/emotion.md) · [配置项](docs/config.md) · [后端开发](docs/core.md) · [更新记录](CHANGELOG.md)

```bash
npm test
cd core && python -m unittest discover -s tests
```

历史的 200 天离线记忆模拟见 [评测方法与数据](docs/benchmark/LONGMEM.md)。真实回复仍取决于所用模型；规则式情绪分类和关键词检索有表达覆盖范围。

DSH 接口基于 0.2.1-rc.2 开发。MIT © zeros-Crtl
