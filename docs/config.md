# 配置项

配置与人设在 `<home>/config.json` 和 `<home>/persona.md`。DSH通常使用 `%USERPROFILE%\.dsh\soulcore`，`SOULCORE_HOME` 优先指定home，DSH还支持 `DSH_HOME`。MCP/Claude Code按 `SOULCORE_HOME` → 存在 `~/.dsh` 时的 `~/.dsh/soulcore` → `~/.soulcore` 查找。

DSH首次启动会生成配置和人设；设置页写的就是同一份JSON。独立CLI用 `--config` 指定文件，相对 `data_dir` 按配置目录解析；不要假设CLI任意cwd下默认 `config.json` 都指向DSH数据。

默认 `data_dir` 是home下的 `data/`，内含 `memory.db`、`emotion.json`、`memory/`镜像、`usage.sqlite3`；升级源码不需要迁移这些数据。高级JSON修改后重启后端。设置API不允许任意改人设文件路径、data_dir或server参数；这些路径在JSON中修改。

后端按默认配置深合并，局部片段只需覆盖目标键；**修改已有文件时，合并字段，不把片段整体替换整份配置**。JSON不能写注释或尾逗号。下面列有效默认值；DSH首次生成与独立Python存在差异：

| 字段 | DSH首次生成 | 独立Python默认 |
|---|---|---|
| `memory.top_k` | `6` | `8` |
| `server.port` | `0`（随机） | `8765` |
| `memory.extract_every` | `6` | `6` |
| `memory.recall_token_budget` | `800` | `800` |
| `memory.context_token_budget` | `1000` | `1000` |
| `memory.extract_token_budget` | `8000` | `8000` |

已有配置的值优先于默认值，升级不会自动改写你保存的参数。

## 设置页与JSON如何选择

| 位置 | 可调整内容 |
|---|---|
| 设置 → 人格与记忆 → 她是谁/人设卡 | 角色名、DSH称呼、人设正文 |
| 性格与情绪 | 基线、敏感度、表达开关、安静开关、QQ消息上限 |
| 记忆 | 插件Key、向量设置、提炼间隔、top_k、召回预算、用量开关 |
| 接入DSH | 插件、注入、回复记录、压缩钩子、工具、排除会话 |
| QQ接入/主动聊天 | QQ连接与白名单、主人QQ、草稿/主动开关、频率、勿扰、常驻 |
| 直接编辑JSON | 模型高级参数、上下文/提炼预算、唤醒度、安静概率、价格、CC风格、路径和服务参数 |

入门步骤见 [快速上手](quickstart.md)，实测参数取舍见 [预算曲线](benchmark/2026-11-memory-affect/README.md)。

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
| `llm.max_tokens` | `400` | 插件自己的生成回复上限，不控制DSH宿主输出 |
| `llm.timeout` | `60` | 请求超时（秒） |

DSH回复由宿主当前选择的模型生成，`llm.*` 用于插件QQ回复及提炼/摘要/画像等任务。未填Key自动规则提炼，插件生成回复不可用；Key为空读取 `api_key_env` 指定变量。修改环境变量后重启进程。兼容服务可修改base_url与model，是否兼容以实际响应为准。

## emotion：情绪

`emotion` 整段直接传给 `Emotion.Params`，常用项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `emotion.baseline` | `0.62` | 性格基线（0–1，越高越开朗） |
| `emotion.sensitivity` | `1.0` | 事件冲击的整体倍率 |
| `emotion.w_lonely` | `1.1` | 孤独感对心情的拉低强度 |
| `emotion.max_rate` | `0.008` | 每分钟心情最大变化量 |
| `emotion.arousal_baseline` | `0.35` | 无唤醒驱动时的基线，0–1；独立于心情好坏 |
| `emotion.arousal_max_rate` | `0.015` | 每分钟唤醒度最大变化量 |
| `emotion.tau_arousal` | `20` | 唤醒度响应时间尺度，分钟 |
| `emotion.tau_arousal_drive` | `45` | 事件唤醒驱动衰减时间尺度，分钟 |

其余PID、惯性、噪声参数见 `core/soulcore/emotion.py`，一般保留默认值。`sensitivity` 增大事件驱动，不取消限速；状态随时间演化，不是每条消息直接设置心情。人设稳定性格与临时情绪分开。

基线是恢复目标，但默认持续孤独驱动 `w_lonely=1.1` 会拉低状态，因此长期无人互动不一定回到基线。只有观察纯事件恢复时再设 `w_lonely=0`；本轮恢复曲线采用此实验条件，产品默认不变。

## appraisal：事件判断

| 配置 | 默认 | 说明 |
|---|---|---|
| `appraisal.mode` | `"heuristic"` | 本地规则判断，无评价模型调用；`llm` 增加模型评价调用，准确率需实际验证 |

## behavior：情绪如何影响表达

| 配置 | 默认 | 功能 |
|---|---|---|
| `behavior.enabled` | `true` | 启用本地表达与分享策略；关闭不等于关闭情绪状态 |
| `behavior.max_messages` | `3` | 开心且兴奋时QQ最多几条，范围1–3；一次生成，按自然段分条 |
| `behavior.quiet_enabled` | `false` | 极低情绪时允许概率短暂安静，需主动开启 |
| `behavior.quiet_threshold` | `0.20` | mood低于此值才可能安静 |
| `behavior.quiet_arousal_max` | `0.45` | 唤醒度超过此值不安静 |
| `behavior.quiet_max_probability` | `0.35` | 最大采样概率，实际概率随mood变化 |
| `behavior.quiet_duration_min` | `15` | 安静状态时间上界，分钟 |
| `behavior.quiet_cooldown_min` | `30` | 结束后再次触发的冷却，分钟 |
| `behavior.quiet_max_consecutive` | `1` | 连续跳过轻闲聊的最大次数 |

mood≥0.74且arousal≥0.50时允许多条QQ消息；mood<0.35且arousal<0.45的明确轻闲聊按0.65缩小插件生成预算，默认400→260。高唤醒低mood更偏担忧/不满；这些是策略条件，不保证模型一定生成指定数量。

**DSH/local/project按任务处理，不概率静默，不缩小宿主输出上限。** 安静与低落缩预算只针对封闭识别的短轻闲聊；私聊/private或dm、非local、≤40字、没有问题/求助/控制/任务等条件。未知文本保留完整回应，群聊也不走概率安静。

进入可采样条件后，概率为 `(quiet_threshold-mood)/quiet_threshold × quiet_max_probability`：默认mood=0.10约17.5%，不是每次固定35%。仍受连续次数、时间与冷却约束。下一条问题、任务、控制或求助会正常处理，15分钟不是无条件不回复时长。实际收到的用户消息仍先记录和处理。

按默认阈值开启示例（合并到已有配置）：

```json
{
  "behavior": {
    "enabled": true,
    "max_messages": 3,
    "quiet_enabled": true,
    "quiet_threshold": 0.2,
    "quiet_max_probability": 0.35,
    "quiet_max_consecutive": 1
  }
}
```

尚未进行真人感盲测。建议先保持安静关闭，观察状态和消息投递，再开启。

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
| `memory.top_k` | DSH `6` / 独立 `8` | 相关召回条数设置，仍受装配策略约束 |
| `memory.cold_threshold` | `0.04` | 权重低于它的转冷（不删除） |
| `memory.consolidate_min` | `30` | 冷掉的原始对话攒够多少条合并成摘要 |
| `memory.beta` | `0.3` | 被检索次数的加成系数 |
| `memory.tau_hours` | `{}` | 覆盖 `exp(-age/τ)` 的时间尺度τ（小时），不是半衰期；键：`turn/fact/preference/decision/todo/person/self/summary` |
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
| `memory.project_identity` | `"registry"` | 持久UUID注册模式，兼容旧路径；同名碰撞隔离。建议保持默认 |
| `memory.recall_token_budget` | `800` | 相关召回的估算软预算；0关闭此token筛选 |
| `memory.context_token_budget` | `1000` | 完整上下文装配的估算软预算；关键记录/附加说明可溢出 |
| `memory.extract_token_budget` | `8000` | 提炼材料分批的估算软预算 |
| `memory.embedding.*` | 空 | 可选语义检索（硅基流动 / 本地 fastembed），不配也能用 |

三种预算控制不同步骤，并非供应商tokenizer或费用硬上限。`inject_max_chars` 是装配字符约束，`cc.inject_max_chars` 另限制CC memory风格；关闭召回token筛选不取消其他字符限制。关键decision/todo/固定记录可超软预算，保证重要材料完整。

`extract_user_chars=800`、`extract_assistant_chars=300` 限制提炼摘录，**不是原文存储上限**。分批和缩注入不会因此删除完整原文，但预算过低仍会让本轮上下文看不到答案。

实测预算100只有43.14%完整答案记录可见，省约12.4%估算注入token；默认仍800。`extract_every` 调大可减少即时批次数，也会延迟新事实可召回；压缩/结束与维护还有排空逻辑，不能据此承诺总调用按比例减少。不变画像自动跳过，无需额外开关。

### memory.embedding：可选语义检索

| 配置 | 默认/示例 | 说明 |
|---|---|---|
| `provider` | `""` | 空关闭；`http`调用接口；`fastembed`本地模型 |
| `base_url` | DSH预填 `https://api.siliconflow.cn/v1` | HTTP embeddings服务根地址 |
| `model` | DSH预填 `BAAI/bge-m3` | 以供应商支持列表为准；fastembed使用相应本地模型 |
| `api_key` | `""` | HTTP服务的独立Key |
| `space_id` | 未配置 | 可显式标识向量模型空间，用于区分索引 |

字段完整路径是 `memory.embedding.*`。失败回退词法；fastembed需给运行后端的Python安装可选依赖。切换模型/空间可能重建向量并产生时间或费用开销，不能把一个服务的向量与另一个模型混用。

## usage：实际用量与费用估算

| 配置 | 默认 | 功能 |
|---|---|---|
| `usage.enabled` | `true` | 记录实际usage至data_dir下usage.sqlite3；不保存正文或密钥 |
| `usage.prices` | `{}` | 按模型配置单价，缺价不编造金额 |

先在设置页或 `GET /v1/usage` 查看 `by_model` 中真实provider和model，再填写 `provider:model` 键。DSH来源可能是 `dsh`，不能假设全是 `deepseek`。接口鉴权和token获取见 [HTTP API](http-api.md)。

下面 `null` 表示尚未填价，**不会算出完整金额**。把对应null替换为实际每百万token价格，并替换模型标识；不要将示例当官方报价。

```json
{
  "usage": {
    "enabled": true,
    "prices": {
      "deepseek:deepseek-chat": {
        "currency": "CNY",
        "input_per_million": null,
        "cached_input_per_million": null,
        "output_per_million": null,
        "reasoning_in_output": true,
        "reasoning_per_million": null
      }
    }
  }
}
```

| 价格字段 | 意义 |
|---|---|
| `currency` | 币种；不同币种分开汇总 |
| `input_per_million` | 未缓存输入单价 |
| `cached_input_per_million` | 缓存输入单价；显式null表示未知 |
| `output_per_million` | 普通输出单价 |
| `reasoning_in_output` | 供应商的output计数是否包含reasoning，默认true，用于避免重复计费 |
| `reasoning_per_million` | 独立推理单价；按服务实际计费语义填写 |

流式DSH累计usage只记最后完整累计块，避免逐块重复计费。无usage、价格或缓存拆分会显示未知/部分覆盖；以供应商账单为最终收费依据。价格修改只影响后续记录，不追溯重算历史。画像20→1是特定不变场景省95%，不是总费用下降95%。

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
| `server.port` | 独立 `8765` / DSH `0` | `0`为随机端口，实际端口看锁文件 |
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

`enabled=true`且`dry_run=true`只生成草稿；`dry_run=false`才自动发送，需要QQ连接与模型Key。只给主人私聊，受勿扰、限频、近期活跃、未回复和发送前复核约束；不是每小时固定发送0.15条。常驻运行设置见 [QQ](qq.md)。

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

CC配置需配合插件/hooks安装才生效。只装MCP不自动每轮记录；共享同一home与项目身份见 [Claude Code](claude-code.md)和[MCP](mcp.md)。

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

## 推荐起点与配置验证

以人格和项目记忆为主：保留默认预算800/1000/8000、registry项目身份、规则评价、安静关闭；开启DSH注入/回复记录/压缩钩子/工具。先在新会话验证明确决策可见，再考虑向量和QQ。先看实际usage再调参数，不能只比较prompt短了多少。

已有用户数据升级时不要清库；同名旧历史有歧义时先确认归属，不强制认领。修改后重新加载插件并重启后端。JSON语法检查示例：

```powershell
Get-Content -Raw "$env:USERPROFILE\.dsh\soulcore\config.json" | ConvertFrom-Json | Out-Null
```

如果使用SOULCORE_HOME或独立--config，检查对应实际文件。完整操作验收见 [快速上手](quickstart.md)。
