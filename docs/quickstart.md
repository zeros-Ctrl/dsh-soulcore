# 快速上手与功能验收

以下用Windows示例；仓库、配置和Python路径请替换为自己的实际位置。核心需要Python 3.10+，DSH插件可使用宿主自带解释器。

## 1. 安装与首次启动

```bash
dsh plugin --profile desktop add github:zeros-Ctrl/dsh-soulcore
```

或下载ZIP解压，找到同时包含 `package.json`、`cordis.patch.yml`、`lib/`、`core/` 的目录：

```powershell
dsh plugin --profile desktop add "link:C:\Plugins\dsh-soulcore"
```

link安装直接引用该目录，更新后重启DSH。打开「设置 → 人格与记忆」，确认后端就绪，修改角色名、人设卡和称呼后保存。人设写长期性格，例如“温和直接，表达有根据，认真处理任务”；“今天别开玩笑”直接在本次聊天中表达。详见 [安装](install.md)。

## 2. 找配置与数据

```text
%USERPROFILE%\.dsh\soulcore\
  config.json         设置页与后端使用的配置
  persona.md          人设卡
  data\
    memory.db         原文、记忆、来源、项目注册
    emotion.json      情绪与持久状态
    memory\           Markdown镜像
    usage.sqlite3     独立用量账本
  logs\               后端及hook日志
```

实际以 `data_dir` 为准，源码更新沿用这份数据。DSH支持 `DSH_HOME`，`SOULCORE_HOME` 优先指定SoulCore home。启动客户端前设置环境变量，修改后重启客户端。

高级配置：停止后端 → 备份 → 编辑现有JSON目标字段 → 检查语法 → 重启后端。下面的JSON都是局部片段，**合并到已有配置，不整体覆盖**。设置页保存写同一份文件，并按运行方式重启后端。

```powershell
Get-Content -Raw "$env:USERPROFILE\.dsh\soulcore\config.json" | ConvertFrom-Json | Out-Null
```

## 3. 验证长期记忆

先保留DSH注入、回复记录、压缩钩子和工具开启，召回预算800。

1. 项目A会话里说：“记住，本项目的巡航速度上限是0.3米每秒，不要超过这个值。”
2. 要求模型用 `remember` 保存明确决策；普通原文先记录，自动提炼按批次/维护时机发生。
3. 新开项目A会话，问：“我们的巡航速度上限是什么？请查长期记忆。”
4. 必要时要求 `memory_search`，给“巡航速度 上限”线索，检查返回记录ID和内容。
5. 在另一个同名项目B记不同上限，分别查询，验证不混用。
6. 试“这个待办已经结束了”“忘掉我的住址”，检查对应记忆及关联摘要不再召回。见 [聊天控制](chat.md)。

同一home和规范cwd对应同一项目UUID；新会话仍应属于该项目。桌面客户端进程cwd可能是安装目录。重命名、搬家、跨机器不保证自动关联，使用 [HTTP项目接口](http-api.md)携带原 `project_id`，检索可传项目UUID。接续卡和 `memory_expand` 暂缓。

完整保存不等于每轮全文注入；没召回也不一定丢了原文。先查home、项目身份、提炼状态、查询词和预算。

## 4. 配置插件模型

DSH宿主模型在DSH配置；插件 `llm.*` 是另一套。未填插件Key可规则提炼，DSH仍用宿主模型；插件生成QQ回复需要Key。

设置页填Key，或在启动进程的环境中设置 `DEEPSEEK_API_KEY`。高级字段示例：

```json
{
  "llm": {
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com",
    "api_key_env": "DEEPSEEK_API_KEY",
    "model": "deepseek-chat",
    "extract_model": "deepseek-chat",
    "max_tokens": 400
  }
}
```

`max_tokens` 不改变DSH宿主输出上限。事件判断默认 `appraisal.mode=heuristic`，改 `llm` 增加调用，准确性需自己验证。

无Key演示，在仓库运行：

```powershell
Set-Location C:\Plugins\dsh-soulcore
python -X utf8 -I core/run.py --mock showcase
```

独立HTTP后端使用真实配置路径：

```powershell
python -X utf8 -I core/run.py --config C:/Users/you/.dsh/soulcore/config.json serve
```

## 5. 可选向量与情绪

设置页「记忆」选HTTP向量，填地址、模型和独立Key，例如 `https://api.siliconflow.cn/v1`、`BAAI/bge-m3`。可用性、价格和额度以供应商为准。fastembed需给**实际后端使用的Python**额外安装依赖及模型。关闭或失败时使用词法；切模型可能重新计算历史向量并产生开销。

人格先用默认基线0.62、敏感度1、最多3条、安静关闭。mood按时间限速推进，唤醒度独立变化；不要用一句“开心一点”验证全部模型。重启会恢复持久状态。

开心多气泡主要在QQ，一次生成；DSH项目、问题和任务完整回应。安静只覆盖明确短私聊寒暄，默认连续最多1次，任务/求助正常回应，15分钟是状态上界。[配置](config.md#behavior情绪如何影响表达)列出概率和阈值。

## 6. 接QQ与主动聊天

1. NapCat登录机器人QQ，启用OneBot v11正向WebSocket服务，记录WS地址和Token。WebUI端口不是WS端口。
2. SoulCore设置页填 `owner.qq`、`qq.ws_url`、`qq.access_token`，开启QQ并配置插件模型Key。
3. 点「测试连接」，确认机器人身份，再私聊测试；主人自动放行，其他人/群按白名单。
4. 如需DSH退出后继续运行，使用「安装常驻」，Windows注册计划任务，重启后查看状态。
5. 主动聊天先 `initiative.enabled=true`、`dry_run=true` 看草稿；改 `dry_run=false` 后才自动给主人发送，需QQ连接与模型Key。

默认勿扰00:00–09:00、每天最多3次、间隔至少3小时；近期活跃、未回复、暂停与待办状态也影响投递。“不要再主动找我”暂停，“恢复主动聊天”恢复。[QQ文档](qq.md)提供详细步骤。

`qq.bili` 是可选只读视频辅助，依赖外部工具；`bilibili` 是独立轮询/审核入口，默认不自动发送。只做人格和记忆可保持关闭。

## 7. 共享到其他客户端

Claude Code安装插件或hooks，默认 `cc.style=memory`；需要人格才选 `companion`。见 [Claude Code](claude-code.md)。

MCP客户端配置示例，合并服务器并替换路径：

```json
{
  "mcpServers": {
    "soulcore": {
      "command": "python",
      "args": ["-I", "C:/Plugins/dsh-soulcore/core/run.py", "mcp"],
      "env": {"SOULCORE_HOME": "C:/Users/you/.dsh/soulcore"}
    }
  }
}
```

只有MCP时，模型主动调 `remember`、`memory_search`，不自动每轮记录。共享home还要匹配项目身份；同名有歧义用UUID。见 [MCP](mcp.md)。

## 8. 用量、预算与排错

开启 `usage.enabled`，在设置页看覆盖情况，以 `by_model` 中真实provider/model填写 `usage.prices`。[价格格式](config.md#usage实际用量与费用估算)中数值需按实际套餐填写；缺信息为未知，改价不追溯重算历史。

先观察自己项目，一次调一个参数，同步检查决策可见、有效事实、隐私范围和费用。不要把预算100当推荐：实测完整答案记录可见率43.14%。

| 现象 | 优先检查 |
|---|---|
| 没有设置页 | desktop profile、清单完整、重启DSH |
| 后端未就绪 | Python 3.10+、设置页Python路径、home下日志 |
| 新会话找不到旧决策 | home/data_dir、项目UUID、提炼状态、查询词与预算 |
| MCP没有自动记忆 | 自动流程需要hooks/DSH，MCP只有工具 |
| QQ不回复 | 模型Key、WS端口/Token、登录、白名单 |
| 情绪没马上恢复 | 时间平滑及持续孤独驱动；纯恢复测试可设w_lonely=0 |
| 账本金额未知 | 对应usage、缓存拆分或价格缺失 |
| JSON改了没生效 | 当前home是否正确，后端是否重启 |

备份先停止后端，再复制整个home和数据库伴随文件。提交日志前检查对话和身份信息。
