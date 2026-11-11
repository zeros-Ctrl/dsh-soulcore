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
python -m unittest discover -s tests -v          # 35 个测试
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

推荐 **NapCat + 正向 WebSocket**：NapCat 那边只配一项，soulcore 主动连过去，收发同一条连接，断线自动重连。

1. 装 NapCat（Windows 一键包），用**小号**扫码登录。
2. NapCat WebUI →「网络配置 → 新建 → WebSocket 服务器」：Host `127.0.0.1`、Port `3001`、消息格式 **Array**、Token 随便设一个，启用并保存。
3. `config.json` 里：
   ```json
   "qq": { "enabled": true, "transport": "ws", "ws_url": "ws://127.0.0.1:3001", "access_token": "和 NapCat 一致",
           "group_whitelist": ["群号"], "private_whitelist": ["你的QQ号"], "group_known": [] },
   "owner": { "qq": "你的QQ号", "name": "主人" }
   ```
4. 启动方式二选一：
   - 跟随 DSH：什么都不用做，DSH 会拉起后端；
   - 常驻（推荐，想让 QQ 一直在）：`python -m soulcore -c <config.json> install-service`，之后登录 Windows 就自动在后台起，卸载用 `uninstall-service`，查状态用 `service-status`。
5. 连上后会自动 `get_login_info` 拿机器人自己的 QQ 号，不用手填 `self_id`。

旧的 **HTTP 上报** 仍然可用：`"transport": "http"` + NapCat 里配 HTTP 服务/上报，地址 `http://127.0.0.1:8765/onebot`。

她的行为规则：
- 白名单以外的群，消息不看也不存。
- 白名单群里的消息她都会"看到"并记下来，但只在被 @ 或被叫名字时回复。想让她偶尔主动插话，可以调高 `interject_prob`。
- 私聊默认只回复 `private_whitelist` 里的人。
- **主人身份合并**：`owner.qq` 那个号在私聊里和本地 DSH 共用同一份人物档案（`core/people/owner`），并且能召回项目里的决策/摘要；群里和其他人的私聊看不到项目记忆。

### 单实例与"接入已有进程"

后端启动时会在 `~/.dsh/soulcore/soulcore.lock` 写 `{pid, port, token, started, version}` 并加操作系统锁。
DSH 插件启动时先读这个锁：进程活着、`/v1/health` 能通就直接接入，**不再起第二个进程**；
抢不到锁的第二个实例会打印信息后退出，不会出现两个进程同时写 `memory.db`。

DSH 退出时只会断开常驻进程，不会杀掉它——这样"她一直在"才成立。常驻模式下改配置需要重启常驻进程才生效（设置页会提示）。

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
| 遗忘门 | `w = 重要性 × e^(-Δt/τ) × (1 + 0.3·ln(1+被检索次数))`。τ 按类型设定：闲聊 3 天，事实 2 个月，决策 1 年。被检索一次就刷新计时，相当于"复习"。权重太低的记忆**转为冷存储，不会删除**。`pinned` 的永久事实（名字/生日/过敏）不衰减 |
| 输出门 | 检索分数 = 相关度 × 权重 × 层级折扣。先搜当前分支，结果不够再逐级回退到父分支、core，最后是全局；回退时按 `trust` 过滤来源 |
| 细胞状态 | 某个分支冷掉的原始对话超过 30 条时，压缩成一条摘要，保留指向原文的指针 |

**信任级别（trust）**：写入时按"说话人 + 场景"定级。

| 来源 | trust |
|---|---|
| 本地 DSH（`platform=local`）、主人的 QQ 私聊 | `owner` |
| 其他私聊（白名单）、`qq.group_known` 里的人 | `known` |
| 群里的其他人 | `stranger` |
| B站评论区 | `public` |
| B站私信 | `known` |

跨分支回退时允许的级别：本地项目/会话只认 `owner`；QQ 私聊、群、B站私信认 `owner` + `known`；B站评论区只认 `public`。本群/本私聊内部的条目不受 trust 限制。合并去重时取两边里更不可信的级别。

**"注入"算不算"用到"**由 `memory.touch_mode` 决定：

| 值 | 注入时 | 主动检索/回复里用到时 | 30 天模拟（seed 42 / 7） |
|---|---|---|---|
| `inject` | 刷新计时 + 访问次数 +1 | 同上 | 88.6% / 95.7% |
| `recency`（默认） | 只刷新计时 | 刷新计时 + 访问次数 +1 | 88.6% / 94.3% |
| `used` | 不刷新 | 刷新计时 + 访问次数 +1 | 90.0%（只跑了 seed 42） |

同一随机种子下 `inject` 与 `recency` 最多差 1 题；不固定种子时指标本身在 88.6%–95.7% 之间波动。默认 `recency`：保留复习效果，但不再把"被注入"折算成重要性。

目录树对应分支，SQLite 里存的是权威数据，Markdown 文件是给你看的双向镜像（直接在文件里改/删/加，之后会自动同步回数据库）：

```
data/memory/
  chat/qq/private/<QQ号>/   chat/qq/group/<群号>/   chat/bilibili/dm/<UID>/   chat/bilibili/comment/<BV号>/
  project/<项目名>/decisions.md
  core/people/owner/notes.md     core/people/<平台>/<uid>/notes.md     core/self/
```

隐私规则（测试里有覆盖）：
- 原始聊天记录只在本会话内可见，跨会话只会召回提炼过的条目。
- 别人的私聊和人物档案永远查不到。
- 项目记忆默认不进 QQ 和 B站；**主人的 QQ 私聊**例外。
- B站评论区是公开场景，看不到任何其他聊天分支。
- 群聊/评论区提炼出的条目不会出现在本地项目的上下文里（trust 过滤）；主人在群里说的话除外。
- 在 Markdown 里删掉的条目会留墓碑，自动提炼不会把它写回来（`remember` 工具等主动写入不受限）。

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
- 知识更新还没做：用户先说"喜欢深色"、后说"改用浅色"，两条都会留着（`invalid_at` 字段已预留）。
- `trust` 按说话人定级，代价是：群里她自己的承诺（`core/self`）在本地项目里召回不到；想放宽需要单独设计。
- 主动聊天（她自己找你）还没做。
- B站接口没有在真实账号上测过，只用模拟接口测试了流程。第一次请用 `bili --once` 配合人审小心试。
