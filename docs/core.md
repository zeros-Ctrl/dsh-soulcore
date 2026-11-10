# soulcore 后端（独立运行 / QQ / B站）

> 这是 dsh-soulcore 的 Python 后端说明。作为 DSH 插件使用时不需要手动启动它；下面的命令都在 `core/` 目录下执行。

核心只用 Python 标准库，没有第三方依赖；只有接 B站时需要额外装一个包。harness、QQ、B站都通过统一的事件格式接进来。

```
QQ ── LLBot/NapCat ──(OneBot v11 HTTP)──┐
B站 ── bilibili-api(轮询) ── 人审队列 ───┼─► Event ─► Engine ─► DeepSeek
你的 harness ──(HTTP 钩子 / JS 客户端)───┘          │
                                     ┌──────────┼───────────┐
                                  情绪(PID)   关系(好感)   记忆(SQLite+Markdown)
```

## 1. 先离线跑一遍（不需要 key 和账号）

需要 Python 3.9 或更高版本。在本目录下运行：

```bash
python -m soulcore demo                          # 模拟一整天：QQ 私聊/群聊、B站评论、项目讨论、余额变化
python -m unittest discover -s tests -v          # 13 个测试
python -m soulcore --mock chat --project 毕设     # 命令行聊天，/help 查看命令（/skip 快进时间、/event 触发情绪、/mem 查记忆）
```

## 2. 接 DeepSeek

```bash
copy config.example.json config.json             # 填 llm.api_key，或设置环境变量 DEEPSEEK_API_KEY
python -m soulcore chat                          # 真实对话
python -m soulcore serve                         # 启动服务：http://127.0.0.1:8765
```

人格设定写在 `persona.md` 里，可以直接修改。`{name}` 会被替换成配置里的 `persona.name`。

## 3. 接入你的 harness（钩子式）

harness 自己调用模型，soulcore 只负责提供"人格、情绪、记忆"这部分上下文，需要挂三个钩子：

| 时机 | 调用 | 作用 |
|---|---|---|
| 发给模型之前 | `POST /v1/hooks/before {event}` | 返回 `system`（人格、心情、关系、召回的记忆）和 `history` |
| 模型回复之后 | `POST /v1/hooks/after {event, reply}` | 把回复写入记忆；每 6 轮自动抽取一次长期记忆 |
| 上下文压缩之前 | `POST /v1/hooks/pre_compact {event, messages}` | 把即将被压掉的内容先提炼并存盘，**防止压缩导致失忆** |

JS 示例见 `clients/soulcore-client.js`。如果 harness 只能发消息、不能改 prompt，可以改用全托管模式 `POST /v1/chat`，由 soulcore 自己调用 DeepSeek。

事件格式：
```json
{"platform":"local","scene":"project","channel_id":"毕设","user_id":"owner","user_name":"阿明","text":"..."}
```

## 4. 接 QQ（OneBot v11）

1. 装一个协议端：推荐 LLBot，NapCat 作为备选。**一定要用小号**。
2. 在协议端里开启 **HTTP 服务**，例如 `127.0.0.1:3000`，设置一个 token。再开启 **HTTP 上报**，地址填 `http://127.0.0.1:8765/onebot`，上报格式选 array。
3. 在 `config.json` 里设置：
   - `qq.enabled=true`
   - 填写 `api_base`、`access_token`、`self_id`
   - 把允许她说话的群号加进 `group_whitelist`
4. 运行 `python -m soulcore serve`。

她的行为规则：
- 白名单以外的群，消息不看也不存。
- 白名单群里的消息她都会"看到"并记下来，但只在被 @ 或被叫名字时回复。想让她偶尔主动插话，可以调高 `interject_prob`。
- 私聊默认只回复 `private_whitelist` 里的人。

## 5. 接 B站（评论区 @ / 回复 + 私信）

```bash
pip install bilibili-api-python==17.4.2   # 原仓库已经归档，必须锁定这个版本
```

1. 在 `bilibili.credential` 里填浏览器 cookie：SESSDATA、bili_jct、buvid3、DedeUserID。
2. 设置 `enabled=true`。

默认的安全策略：
- **不会自动发送**。生成的回复先进人审队列，用 `python -m soulcore review` 逐条确认、编辑或跳过。
- 第一次运行只记录位置，不会回复历史积压的消息。
- 私信只处理 `dm_whitelist` 里的人。
- 评论默认每 15 秒最多 1 条、每小时最多 20 条。
- 遇到 -352、-412 或验证码会**立即暂停**。确认账号没问题后，运行 `python -m soulcore bili --resume` 恢复。
- 你否掉的草稿不会写进记忆。

建议在账号简介里写明这是 AI。

## 6. 记忆是怎么工作的（借鉴 LSTM 的门控思想）

| 门 | 实现 |
|---|---|
| 输入门 | 每条记忆都有重要性分。原始对话按规则打分；每 6 轮由 LLM 提炼一次"事实、偏好、决策、待办"，并给出重要性 |
| 遗忘门 | `w = 重要性 × e^(-Δt/τ) × (1 + 0.3·ln(1+被检索次数))`。τ 按类型设定：闲聊 3 天，事实 2 个月，决策 1 年。被检索一次就刷新计时，相当于"复习"。权重太低的记忆**转为冷存储，不会删除** |
| 输出门 | 检索分数 = 相关度 × 权重 × 层级折扣。先搜当前分支，结果不够再逐级回退到父分支、core，最后是全局 |
| 细胞状态 | 某个分支冷掉的原始对话超过 30 条时，压缩成一条摘要，保留指向原文的指针 |

目录树对应分支，SQLite 里存的是权威数据，Markdown 文件是给你看的镜像：

```
data/memory/
  chat/qq/private/<QQ号>/   chat/qq/group/<群号>/   chat/bilibili/dm/<UID>/   chat/bilibili/comment/<BV号>/
  project/<项目名>/decisions.md
  core/people/<平台>/<uid>/notes.md     core/self/
```

隐私规则（测试里有覆盖）：
- 原始聊天记录只在本会话内可见，跨会话只会召回提炼过的条目。
- 别人的私聊和人物档案永远查不到。
- 项目记忆默认不进 QQ 和 B站。
- B站评论区是公开场景，看不到任何其他聊天分支。

## 7. 情绪（PID 设定点模型）

事件改变的是她"想要的心情"，也就是设定点。实际情绪通过 PID 慢慢追上去，外面还有一道限速，每分钟最多变化 0.008。完整实验见 `docs/emotion/`。

常用参数（`config.json` 的 `emotion` 段）：

| 参数 | 含义 |
|---|---|
| `baseline` | 性格基线 |
| `sensitivity` | 敏感度 |
| `w_lonely` | 孤独权重 |
| `max_rate` | 每分钟最大变化量 |
| `lonely_grace` | 多少分钟没人理会开始感到孤独 |

触发情绪的事件：
- 消息内容：按关键词判断，也可以设置 `appraisal.mode=llm` 交给 LLM 判断。
- DeepSeek 余额：每小时查一次。余额低、用完、充值都会成为事件。
- 好感度：对方的好感度越高，对她情绪的影响越大。

## 已知局限

- 关键词判断事件比较粗糙。有预算的话把 `appraisal.mode` 改成 `llm`。
- 检索用的是二元组加 FTS5，没有语义向量，"同义不同词"的情况召回不到。后续可以加 embedding。
- 手动修改 Markdown 镜像不会同步回数据库。
- B站接口没有在真实账号上测过，只用模拟接口测试了流程。第一次请用 `bili --once` 配合人审小心试。
