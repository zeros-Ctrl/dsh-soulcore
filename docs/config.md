# 配置项

配置文件在 `<home>/config.json`：

- 常驻/独立运行：`~/.dsh/soulcore/config.json`（或者 `SOULCORE_HOME` 指定的目录）；
- 第一次运行会自动生成（复制 `core/config.example.json`，`api_key` 留空）；
- DSH 设置页改的就是它；命令行用 `--config` 指到别的文件也可以。

下面的默认值就是 `core/soulcore/config.py` 里的 `DEFAULTS`，代码里是深合并：只写想改的键就行。

## persona：人设

| 配置 | 默认 | 说明 |
|---|---|---|
| `persona.name` | `"小汐"` | 角色名 |
| `persona.card_file` | `"persona.md"` | 人设文件（相对配置文件目录；也可以用绝对路径） |
| `persona.anchor` | `""` | 手动指定人格锚点；留空就从人设卡的"性格/说话方式/语气/口头禅/称呼"里自动摘（≤120 字） |
| `persona.anchor_enabled` | `true` | 关掉后不再注入锚点 |
| `persona.slip_ttl_h` | `72` | "最近注意"（助手腔提醒）保留多少小时 |

## 顶层

| 配置 | 默认 | 说明 |
|---|---|---|
| `data_dir` | `"./data"` | 数据目录（相对配置文件目录）。`memory.db`、`emotion.json`、Markdown 镜像都在里面 |

## llm：模型

| 配置 | 默认 | 说明 |
|---|---|---|
| `llm.provider` | `"deepseek"` | `deepseek` / `mock` / `heuristic`（`none` 同 heuristic） |
| `llm.base_url` | `"https://api.deepseek.com"` | OpenAI 兼容地址 |
| `llm.api_key` | `""` | 密钥。留空时读环境变量 `llm.api_key_env` |
| `llm.api_key_env` | `"DEEPSEEK_API_KEY"` | 密钥环境变量名 |
| `llm.model` | `"deepseek-chat"` | 生成回复用的模型 |
| `llm.extract_model` | `"deepseek-chat"` | 提炼/摘要/画像用的模型 |
| `llm.temperature` | `1.0` | 回复温度 |
| `llm.max_tokens` | `400` | 回复上限 |
| `llm.timeout` | `60` | 请求超时（秒） |

> 没填 key 时会自动降级成规则模式：记忆提炼用关键词规则，回复生成不可用（QQ/B站场景需要 key）。

## emotion：情绪

`emotion` 整段直接传给 `Emotion.Params`，常用项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `emotion.baseline` | `0.62` | 性格基线（0–1，越高越开朗） |
| `emotion.sensitivity` | `1.0` | 事件冲击的整体倍率 |
| `emotion.w_lonely` | `1.1` | 孤独感对心情的拉低强度 |
| `emotion.max_rate` | `0.008` | 每分钟心情最大变化量 |

其余 PID / 惯性 / 噪声参数见 `core/soulcore/emotion.py`，一般不用改。

## appraisal：事件判断

| 配置 | 默认 | 说明 |
|---|---|---|
| `appraisal.mode` | `"heuristic"` | `heuristic` = 规则判断（零成本）；`llm` = 让小模型判断（更准一点，多一次调用） |

## owner：主人

| 配置 | 默认 | 说明 |
|---|---|---|
| `owner.qq` | `""` | 你的 QQ 号：用来把 QQ 上的你认成 `owner`，和 DSH 共用一份人物档案 |
| `owner.name` | `"主人"` | 她怎么称呼你（也用于 Claude Code hook 的 `user_name`） |

## memory：记忆

| 配置 | 默认 | 说明 |
|---|---|---|
| `memory.extract_every` | `6` | 每多少轮原始对话提炼一次长期记忆 |
| `memory.recent_turns` | `12` | 每轮带多少条最近对话作为上下文 |
| `memory.top_k` | `8` | 每轮最多注入几条记忆 |
| `memory.cold_threshold` | `0.04` | 权重低于它的转冷（不删除） |
| `memory.consolidate_min` | `30` | 冷掉的原始对话攒够多少条合并成摘要 |
| `memory.input_floor` | `0.05` | 重要性低于它直接不存 |
| `memory.beta` | `0.3` | 被检索次数的加成系数 |
| `memory.tau_hours` | `{}` | 覆盖各类型的衰减半衰期（小时），键：`turn/fact/preference/decision/todo/person/self/summary` |
| `memory.touch_mode` | `"recency"` | `inject` / `recency` / `used`，见 [how-memory-works.md](./how-memory-works.md) |
| `memory.share_projects_in_chat` | `false` | QQ/B站里能不能召回项目记忆（主人的 QQ 私聊不受此限） |
| `memory.inject_max_chars` | `1600` | 每轮注入文本的预算 |
| `memory.auto_supersede` | `true` | 写入时做规则版冲突检测（v0.8） |
| `memory.time_boost` | `2.0` | 落在问句时间窗内的条目得分倍数（v0.8） |
| `memory.cold_recall` | `true` | 正常检索没想起来时再翻一次冷存储（v0.8） |
| `memory.cold_min_rel` | `0.45` | 冷条目要多相关才值得想起来（v0.8） |
| `memory.gate_retrieval` | `true` | 寒暄不检索（v0.8） |
| `memory.extract_assistant_chars` | `300` | 提炼时她的发言最多保留多少字（v0.8） |
| `memory.extract_user_chars` | `800` | 提炼时对方的发言最多保留多少字（v0.8） |
| `memory.segment_gap_min` | `30` | 两轮间隔超过多少分钟当作话题段结束（v0.8） |
| `memory.reflect_every_h` | `20` | 多久重算一次画像（v0.8） |
| `memory.embedding.*` | 空 | 可选语义检索（硅基流动 / 本地 fastembed），不配也能用 |

## balance：余额

| 配置 | 默认 | 说明 |
|---|---|---|
| `balance.check_every_min` | `60` | 多久查一次 DeepSeek 余额 |
| `balance.low_threshold` | `5.0` | 低于多少元触发"余额不足"情绪事件 |
| `balance.recharge_min` | `5.0` | 一次充值多少元以上算"充值"事件 |

## server：HTTP 服务

| 配置 | 默认 | 说明 |
|---|---|---|
| `server.host` | `"127.0.0.1"` | 监听地址 |
| `server.port` | `8765` | 端口（`0` = 随机端口，DSH 拉起的子进程用 `0`） |
| `server.token` | `""` | 固定 token；留空时自己常驻启动会随机生成并写进锁文件 |
| `server.tick_every_min` | `15` | 后台维护（冷却/摘要/画像/余额）间隔 |

## service：常驻方式

| 配置 | 默认 | 说明 |
|---|---|---|
| `service.mode` | `"follow_dsh"` | `follow_dsh`（DSH 关了就退）/ `resident`（计划任务常驻） |

## initiative：主动聊天

| 配置 | 默认 | 说明 |
|---|---|---|
| `initiative.enabled` | `false` | 总开关 |
| `initiative.dry_run` | `true` | 只生成草稿，不真的发 |
| `initiative.base_rate_per_hour` | `0.15` | 每小时基础触发概率 |
| `initiative.quiet_hours` | `["00:00","09:00"]` | 勿扰时段 |
| `initiative.max_per_day` | `3` | 每天上限 |
| `initiative.min_gap_hours` | `3` | 两条之间最小间隔 |
| `initiative.recent_activity_skip_min` | `30` | 你刚聊过就跳过 |
| `initiative.max_unanswered` | `2` | 最多连发几条不等回复 |
| `initiative.reply_window_hours` | `4` | 多久之内算"等你回" |
| `initiative.thought_threshold` | `0.45` | 念头强度阈值 |
| `initiative.check_every_min` | `5` | 多久检查一次念头 |

## qq：QQ（NapCat）

| 配置 | 默认 | 说明 |
|---|---|---|
| `qq.enabled` | `false` | 启用 QQ |
| `qq.transport` | `"ws"` | `ws`（正向 WebSocket，推荐）/ `http`（OneBot v11 HTTP 上报，旧） |
| `qq.ws_url` | `"ws://127.0.0.1:3001"` | NapCat 的 WebSocket 地址 |
| `qq.api_base` | `"http://127.0.0.1:3000"` | HTTP 上报模式的 API 地址 |
| `qq.access_token` | `""` | NapCat 的 Token |
| `qq.self_id` | `""` | 机器人 QQ（一般留空，连上后自动取） |
| `qq.group_whitelist` | `[]` | 允许互动的群号 |
| `qq.private_whitelist` | `[]` | 允许私聊的 QQ |
| `qq.allow_all_private` | `false` | 允许所有陌生人私聊（仍按 `stranger` 可信度） |
| `qq.group_known` | `[]` | 群里算"熟人"的 QQ（`known`） |
| `qq.min_interval_s` / `max_per_min` | `3` / `10` | 发送限流 |
| `qq.interject_prob` | `0.0` | 群里没被 @ 时插话的概率 |
| `qq.typing_delay` | `true` | 假装"正在输入" |
| `qq.sticker.enabled` / `dir` / `max_images` | `true` / `""` / `1` | 表情包 |
| `qq.bili.enabled` | `false` | 在 QQ 里刷 B站 / 搜视频 / 读字幕 |
| `qq.bili.tools_path` / `node` | `""` | dsh-bili-whale 的位置 |
| `qq.bili.every_min` | `180` | 后台多久自己刷一次首页 |
| `qq.bili.max_candidates` / `timeout_s` | `3` / `45` | 每次刷几个候选、超时 |
| `qq.bili.chat_trigger` / `chat_min_gap_s` | `true` / `600` | 聊天里触发刷 B站的冷却 |
| `qq.bili.search_per_hour` / `video_per_hour` | `10` / `5` | 每小时上限（防风控） |
| `qq.bili.search_timeout_s` / `subtitle_timeout_s` | `20` / `45` | 搜索、读字幕的超时 |

## bilibili：B站独立轮询

| 配置 | 默认 | 说明 |
|---|---|---|
| `bilibili.enabled` | `false` | 启用轮询 |
| `bilibili.credential.sessdata/bili_jct/buvid3/dedeuserid` | `""` | B站登录 cookie |
| `bilibili.poll_interval_s` | `90` | 轮询间隔 |
| `bilibili.reply_at` / `reply_replies` | `true` / `true` | 回复 @ 和回复 |
| `bilibili.dm_whitelist` | `[]` | 允许私信的人 |
| `bilibili.auto_send` | `false` | 自动发送（默认进审核队列） |
| `bilibili.comment_min_interval_s` / `comment_max_per_hour` | `15` / `20` | 评论限流 |
| `bilibili.dm_min_interval_s` / `dm_max_per_hour` | `10` / `20` | 私信限流 |

## cc：Claude Code（v0.9）

| 配置 | 默认 | 说明 |
|---|---|---|
| `cc.enabled` | `true` | 关掉后 hook 什么都不做（直接 exit 0） |
| `cc.style` | `"memory"` | `memory` = 只注入跨会话记忆；`companion` = 完整人格 |
| `cc.inject_max_chars` | `900` | memory 风格的注入预算（字符） |
| `cc.record_replies` | `true` | Stop 时把她的回复写回记忆；`false` 只记你说的话 |

## dsh：DSH 插件（JS 侧，写在同一个 config.json 里）

| 配置 | 默认 | 说明 |
|---|---|---|
| `dsh.enabled` | `true` | 关掉插件（后端也不拉） |
| `dsh.injectPersona` | `true` | 人设写进 system prompt |
| `dsh.injectContext` | `true` | 每轮注入心情 + 记忆 |
| `dsh.recordReplies` | `true` | 把她的回复写回记忆 |
| `dsh.compactionHook` | `true` | 压缩前抢救 |
| `dsh.tools` | `true` | 注册 `memory_search` / `remember` / `mood_status` |
| `dsh.ownerName` | `"主人"` | 她怎么称呼你 |
| `dsh.excludeSessions` | `["bili-whale"]` | 这些会话不注入 |
| `dsh.contextTimeoutMs` | `4000` | 每轮注入的超时（超时就这一轮不注入） |
| `dsh.pythonPath` | `""` | 指定 Python（留空自动找 DSH 自带运行时） |
