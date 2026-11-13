# MCP 接入：让任何 MCP 客户端用上她的长期记忆

`soulcore mcp` 是一个标准 MCP server（stdio 传输，按行分隔的 JSON-RPC 2.0）。Claude Code、
Cursor、Claude Desktop、Cherry Studio 之类的客户端都能直接接，不需要改 soulcore 的代码。

它不自己开数据库，而是连**本地常驻后端**（和 DSH 用的是同一个进程、同一份记忆），
所以你在 DSH 里说过的项目决策，在 Claude Code 里问也能想起来。

## 1. 先确认后端能起来

```bash
python -I <仓库>/core/run.py --mock showcase   # 不需要 key，先看效果
```

MCP server 第一次调用工具时会自己把后端拉起来（`--idle-exit-min 240`，四小时没人用会自己退）。
数据目录按下面的顺序找：

1. 环境变量 `SOULCORE_HOME`
2. `~/.dsh/soulcore`（存在 `~/.dsh` 时，和 DSH 共享）
3. `~/.soulcore`

第一次运行会自动生成 `config.json`（`api_key` 留空）和 `persona.md`。

## 2. 工具

| 工具 | 参数 | 作用 |
|---|---|---|
| `memory_search` | `query`、`k=8`、`include_history=false`、`project` | 搜长期记忆，返回 `#id [kind·多久以前] 内容` |
| `remember` | `content`、`kind=fact` | 写一条记忆，返回新 id |
| `forget` | `id` | 忘掉一条（加墓碑，不会被自动提炼复活） |
| `restore` | `id` | 恢复被忘掉/被取代的记忆 |
| `memory_timeline` | `limit=10` | 最近记了什么、哪些说法过时了 |
| `mood_status` | 无 | 心情、标签、孤独度 |

`project` 默认取 MCP 进程启动时的 `cwd`：有 `.git` 时用仓库根目录名，否则用最后一级目录名。
规则和 DSH、Claude Code hook 完全一致，所以同一个项目在三个地方共用 `project/<名>`。

## 3. 各客户端配置

### Claude Code（推荐直接用插件）

插件会同时挂 hooks 和 MCP，见 [claude-code.md](./claude-code.md)。只要 MCP 不想注入：

```bash
claude mcp add soulcore -- python -I <仓库>/core/run.py mcp
```

这条命令把服务器写进 Claude Code 的用户级配置（`claude mcp add --scope user ...` 是同一个意思）。

### 项目级 `.mcp.json`（Claude Code / 其它认这个文件名的客户端）

放在项目根目录：

```json
{
  "mcpServers": {
    "soulcore": {
      "command": "python",
      "args": ["-I", "C:/path/to/dsh-soulcore/core/run.py", "mcp"],
      "env": { "SOULCORE_HOME": "C:/Users/you/.dsh/soulcore" }
    }
  }
}
```

### Cursor

`~/.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "soulcore": {
      "command": "python",
      "args": ["-I", "C:/path/to/dsh-soulcore/core/run.py", "mcp"]
    }
  }
}
```

### Claude Desktop

`claude_desktop_config.json`（macOS：`~/Library/Application Support/Claude/`；
Windows：`%APPDATA%\Claude\`）：

```json
{
  "mcpServers": {
    "soulcore": {
      "command": "python3",
      "args": ["-I", "/Users/you/dsh-soulcore/core/run.py", "mcp"]
    }
  }
}
```

### Cherry Studio / 其它图形界面

新建 MCP 服务器，类型选 **stdio**：

- 命令：`python`（Windows 上也可以填 `py`，但 `args` 要以 `["-3", "-I", ...]` 开头）
- 参数：`["-I", "<仓库>/core/run.py", "mcp"]`
- 环境变量：`SOULCORE_HOME=<你的 soulcore 数据目录>`（可选，不填就按上面的顺序找）

## 4. 排查

- **工具返回 `soulcore 后端没有启动`**：看日志 `<home>/logs/serve.log`；也可以先手动
  `python -I <仓库>/core/run.py serve` 起一次，确认配置没问题。
- **stdout 里出现非 JSON 内容**：说明有东西在往 stdout 打日志，这是 bug；日志只应该写 stderr。
  提交 issue 时把 `serve.log` 和客户端的 MCP 日志一起贴上。
- **找不到 Python**：MCP 客户端启动子进程时不读 shell 的 `PATH` 别名，写 Python 的绝对路径最稳。

## 5. 隐私

MCP 工具只读写本机 `<home>/data/memory.db` 和 Markdown 镜像；只有"提炼/摘要/画像"这几步会把
压缩后的对话片段发给 DeepSeek（要填了 key 才会发）。没填 key 时完全离线（规则提炼）。
`forget` 之后检索不到；想彻底删掉所有数据，直接删 `<home>` 目录。
