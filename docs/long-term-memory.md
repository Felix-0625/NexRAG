# 长期记忆

本文说明 NexRAG 的个人长期记忆：隔离边界、写入与防投毒、冲突消解和用户控制。评测与自我进化见 [evaluation-and-rsi.md](evaluation-and-rsi.md)。

## 1. 隔离与层级

硬边界是用户本人，即 Open WebUI 签名 JWT 中的 `sub`。不同用户、不同角色的记忆互不可见，管理员也没有读取他人记忆的接口。在同一用户内部分三层：

| 层 | 可见范围 | 允许的内容 |
| :--- | :--- | :--- |
| `global` | 该用户的所有租户、所有会话 | 只允许 `preference` / `profile`（语言、格式、角色等） |
| `tenant` | 该用户在同一业务租户下 | 业务事实、口径约定、关注指标 |
| `session` | 同一个 Open WebUI 会话（`X-OpenWebUI-Chat-Id`） | 当前任务状态 |

- scope 由服务端注入，LLM 无法指定。抽取器若把业务事实标成 `global`，会被降级为 `tenant`，所以租户事实不会跨租户外溢。
- 召回时三层并行检索，分别加权（session 1.0 / tenant 0.85 / global 0.7）。综合分 = 0.5·相关性 + 0.2·时效 + 0.15·重要度 + 0.15·置信度。结果以 `<memory>` 数据块注入，每条带日期和来源。Agent 也可以主动调用 `recall_memory`。

## 2. 写入与防投毒

- 只从用户原话抽取。工具输出、检索文档、网页、文件都不是记忆来源。
- 指令样式的文本整条丢弃；手机号、证件号、邮箱、卡号、密钥先脱敏。
- 记忆在对话结束后异步写入，不阻塞回答。写入顺序是先写 SQLite 事务，再写 Qdrant；Qdrant 写失败的记录进补偿队列，由 `scripts/evolve.py --memory-only` 重放。
- 个人记忆的辅助抽取模型、Embedding 与向量存储必须全部为 loopback，禁止 `:cloud`/`-cloud` 模型标签；否则跳过自动记忆处理，不向外发送原话。该规则也覆盖 CLI/API/维护入口，主模型的外发许可不会自动授权另一个记忆厂商。原生启动器另外验证托管 Ollama 的 cloud inference 已关闭。

## 3. 冲突消解（L0–L4，先规则后 LLM，全部软失效）

| 级别 | 判定 | 动作 |
| :--- | :--- | :--- |
| L0 | 同槽位同值，或近似重复 | 不新增，只刷新置信度 |
| L1 | 新值包含并扩展旧值 | 生成新版本，旧版本标 `superseded` |
| L2 | 同槽位值变化，且新证据明显更晚 | 旧版本 `valid_to=now`，新版本通过 `supersedes_id` 指向旧版本（双时间轴） |
| L3 | 用户显式更正（"不对、其实、改为…"） | 无条件覆盖，来源记为 `user_explicit` |
| L4 | 来源可信度相近、时间接近的矛盾，或低可信来源挑战高可信记忆 | 双方都标 `disputed`，召回时并列给出，由 Agent 或用户确认 |

- 可信度排序：`user_explicit > user_implicit > agent_inferred`，推断出的记忆永远不能覆盖用户说过的话。
- 规则无法判定时才调用可选的 LLM 仲裁；仲裁失败一律按 L4 处理，保证不会误删。
- 物理删除只发生在用户主动"遗忘"时，会同时删除整条版本链和对应向量。

## 4. 维护与用户控制

在“设置 → 个性化 → NexRAG 长期记忆”控制本人记忆开关。开关保存在运营 SQLite 中，适用于本账号的全部会话，关闭后停止自动抽取、自动注入和主动 `recall_memory`，但保留已有记录供本人查看、确认或遗忘。关闭期间已经启动的抽取/Embedding 在提交新记录前重新检查开关。管理员没有替员工修改这个个人开关的接口。

部署者的 `ENABLE_MEMORY=false` 或不满足本机辅助模型配置时，个人开关不能强行开启服务。上游 Open WebUI 的独立记忆引擎在原生 profile 中关闭，避免两个外观相同但控制不同后端的开关。默认延续现有启用状态；用户明确关闭后跨重启保留。


- 运行 `python scripts/evolve.py --memory-only` 做三件事：
  - 会话空闲超过 `MEMORY_SESSION_IDLE_HOURS` 后，把重要的任务状态经冲突消解晋升到租户层；
  - 按"时效 × 重要度 × 使用次数"衰减并归档；用户显式陈述的记忆不衰减；
  - 已失效的记录超过 `MEMORY_RETENTION_DAYS` 后清除。
- 在 Open WebUI 的"设置 → 个性化 → NexRAG 长期记忆"里可以查看、确认冲突、遗忘单条或全部记忆。对应接口为 `GET/DELETE /v1/memory`、`POST /v1/memory/{id}/confirm`、`DELETE /v1/memory/{id}`，只作用于本人。

## 接口

| 接口 | 权限 | 用途 |
| --- | --- | --- |
| `GET /v1/memory`、`GET/POST /v1/memory/settings` | 本人 | 查看记忆与个人开关 |
| `POST /v1/memory/{id}/confirm` | 本人 | 确认一条有争议的记忆 |
| `DELETE /v1/memory/{id}`、`DELETE /v1/memory` | 本人 | 遗忘单条或全部记忆 |

Open WebUI 补丁 `nexrag/ops-dashboard.diff` 在个人设置中加入“NexRAG 长期记忆”面板，并通过 `/api/nexrag/*` 代理以已验证的用户身份转发请求。

## 已知局限

- 默认的 `qwen3:4b-instruct` 做记忆抽取的准确率有限。冲突处理以规则为主，可选仲裁器失败时挂起冲突，保证不会误删。
- Qdrant 不可达时，记忆退化为进程内索引，记录会保留在 SQLite 并在日志中告警，重启后无法被召回，直到补偿队列重放完成。

## 上下文与验收

历史只接收当前账号对话中的 user/assistant 消息，保留最近 6 条，总计最多 6000 字符，并以引用数据形式提供；不会将历史中的 system/developer 角色作为新的高权限指令。当前问题完整保留。检索父块按文档边界放入总计 12000 字符预算，超出时明确报告省略数量，不再把每份完整父块任意切到 1500 字符。以上是明确的截断策略，不是自动语义压缩或无损长上下文。

独立的真实本地集成命令（不要与前端测试同时抢占同一模型）：

```powershell
.venv\Scripts\python.exe scripts/verify_agent_lifecycle.py --with-memory --json-out .local-run/agent-live.json
```

命令只接受 loopback Ollama，创建临时合成业务库和独立运营库；不读取真实员工知识库。它不是前端验收，也不伪造三天灰度观察窗。当前实现与实际验证状态见 [Agent 生命周期验收](acceptance/agent-lifecycle.md)。
