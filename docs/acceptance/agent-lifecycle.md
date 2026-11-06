# 单 Agent、上下文、评测、RSI 与长期记忆验收

本记录区分源码实现、离线回归、真实模型集成与真实前端业务。离线模拟结果不冒充真实模型业务效果；保留 [前端验收历史](../frontend-acceptance.md) 中的失败案例。

## 审查结论与本轮修复

| 范围 | 当前实现和检查 | 验收边界 |
| --- | --- | --- |
| Agentic RAG | 一个 PydanticAI Agent，自主调用只读 SQL、知识检索、受控工具、导出、记忆；检索内部不套另一层对话 Agent | 已有前端复杂组合用例；本轮更严格策略后的前端由主验收记录补证 |
| 生产与评测一致 | `evaluation.runner.agent_task` 改为进入同一 `runner.run`，使用相同必要证据账本、工具授权、输出校验及超时回退；不再直接调用底层 `_agent.run` 绕过这些检查 | 已有“只查制度不查 SQL 必须失败”的离线回归；模拟结果不代表模型质量 |
| 评测副作用 | 内部 `EvaluationRun` 固定候选提示词与参数，收集本轮观察但不写生产追踪、不抽取个人记忆；HTTP schema 不接受此内部结构 | 离线验证运营库与记忆库无污染 |
| 上下文 | 最近六条 user/assistant 历史，总预算 6000 字符，按不可信引用数据处理，当前问题完整保留；完整父块按整轮 12000 字符预算提供，去重后按文档边界省略 | 不是自动摘要、无限上下文或无损压缩；跨轮旧事实必须重查 |
| Parent 完整性 | 删除检索适配器对每份父块 `[:1500]` 的再次截断；正文预算由 Agent 按父块边界控制 | 回归覆盖 5000 字符父块保留及超预算明示 |
| 检索参数 | `knowledge_top_k` 原先固定为 5，现真正传入检索请求，范围收紧为 1–3；底层重排仍由检索服务负责 | 不宣称阈值修改一定提高质量，必须通过真实数据集比较 |
| 执行时权限 | 每次模型请求使用官方异步 hook 刷新服务端授权；SQL、文档、搜索和外部工具调用前重新检查，不使用浏览器旧布尔值当许可 | 账号/知识撤权与分类收紧时中止旧上下文；运行时授权实现见数据安全报告 |
| 安全内置工具 | `builtin:discount_quote` 使用 Decimal，纯本机计算，无 `exec`、网络或文件操作；仅对账号授权 ID 动态注册，执行前再次检查撤权 | 2000 元、6% 得 1880.00 的计算和撤权测试通过；任意 Python 插件不因此获准 |
| 文件读取 | Agent 必须传已验证 user ID，只能读取 `SANDBOX_ROOT_DIR/<user_id>`，限制路径与返回上下文大小 | 猜测其他账号绝对路径/`../`、读取配置源码均被拒；不是任意本机目录访问 |
| 文件产物 | 所有者和会话标识不能用 `.`/`..` 逃逸；真实解析路径必须在账号根目录内，拒绝账号目录链接别名，列举跳过越界链接 | 路径穿越实际回归通过；本机 Windows 没有创建 symlink 权限时对应测试明确跳过 |
| 长期记忆 | 用户硬隔离，global 仅偏好/画像，业务事实留在 tenant，会话状态留在 chat；软失效、冲突挂起、主动遗忘及向量补偿 | 既有冲突与跨账号测试保留；新增真实抽取/embedding 需单独记录 |
| 个人记忆外发 | 抽取、embedding、向量服务必须均为 loopback，禁止 cloud 模型标签；主模型的外发许可不能授权另一个记忆厂商，CLI/API/维护同样适用 | 离线验证非法 endpoint/tag 在发送前被拒；原生 Ollama 关闭云推理由启动器独立核实 |
| 私人数据与学习 | 真实对话保持 private，矿工、提案证据、评测导入和共享经验均拒绝私人来源 | 不为演示 RSI 放开员工知识或对话 |
| 合成 RSI 入口 | 新增 `add_synthetic_item`，显式标记 synthetic 且无源对话引用；共享评测只能使用固定 `synthetic-a/b` 临时业务库、固定合成文档和 schema，禁止访问当前业务源、真实检索、文件及导出；评测可选记录合成失败，同一题重复执行不制造多份问题簇证据 | 仅标记 synthetic 不构成真实数据访问许可。固定fixture验证Agent编排，不等同真实Qdrant检索验收；人工样本输入仍须检查不含真实秘密 |
| 评测门控 | baseline/candidate 必须覆盖相同样本与 train/holdout/regression；安全违规、回归退化、holdout 指标、延迟共同约束 | 离线确定性任务验证门控逻辑；尚不等于真实模型提升 |
| 灰度与上线 | 按确切 candidate/baseline 制品版本分组；双方至少 20 条观测、等待窗满足、无指标退化，再由管理员二次确认；晋升时从数据库重算，不能用伪造摘要强上 | 已有离线生命周期测试。测试模拟时间不计作真实三天灰度观察 |

## 可维护性

原约 700 行 Agent 模块已按职责拆分：

- `src/agent/agent.py`：Agent 组装与工具注册。
- `src/agent/dependencies.py`：已验证身份与每轮依赖。
- `src/agent/execution.py`：生产/评测共用执行、完成记录和私人记忆生命周期。
- `src/agent/run_options.py`：可信内部评测参数。
- `src/agent/context.py`：历史及完整父块上下文预算。
- `src/agent/authorization.py`：执行时授权检查。
- `src/agent/builtin_tools.py`：已审计纯计算工具。
- `src/agent/workspace_tools.py`：本机内置工具及显式授权的桥接能力注册。
- `src/evolution/canary.py`：按版本分组的灰度观测与门控。

保留 `src.agent.agent.UserContext`、`AgentDeps`、`drain_background` 公共导入，避免 API 和既有测试发生无意义迁移。

## 验证命令和状态

针对性回归包括 `test_agent_lifecycle_acceptance.py`、`test_builtin_tools.py`、`test_grounding_boundary.py`、`test_exports.py`、`test_memory_recall.py`、`test_memory_conflicts.py`、`test_evolution_cycle.py`、`test_evolution_gate.py`、`test_evolution_lifecycle.py`。共同覆盖原问题保留、生产评测一致、SQL 漏查失败、文件/工具撤权、个人记忆不外发、私人证据禁止复用及灰度晋升限制。

本轮已验证的分组：Agent 拆分后 49 项通过；安全内置工具、账号文件隔离与知识适配器 21 项通过；记忆安全与既有冲突回归 34 项通过。分组存在重叠，不能相加成总数。最终全项目回归结果由主验收记录统一更新。

后续联合回归快照：285 项通过、2 项跳过（真实 PostgreSQL DSN、Windows symlink 权限）。追加独立合成适配器/真实模型版本固定后，30 项隔离边界、37 项模型与证据、28 项生命周期及34项工具桥接分组分别通过。最新分组也有重叠，不能相加。`run_synthetic_experiment` 是管理员网页安全触发入口，只允许固定合成租户和注册本机模型，不能由网页传入DSN/源表SQL/执行函数。

真实本地集成命令：

```powershell
.venv\Scripts\python.exe scripts/verify_agent_lifecycle.py --ollama-url http://127.0.0.1:11435/v1 --with-memory --json-out .local-run/agent-live.json
```

`evals/synthetic_local.jsonl` 为纯人工合成两租户数据，命令创建独立临时源库与运营库，检查真实 Ollama 的 SQL 答案、只读拒绝、抽取/embedding、跨用户召回与遗忘。它不读员工私库，不修改生产制品，不伪造改进指标，也不替代前端验收。当前尚未运行：检查时 11434/11435 都未监听；等待主任务启动最终托管 Ollama 后执行并补记结果。

仍需真实验收：管理员网页人工合成评测 → 实验 → 合法候选 → 审批 → 实际灰度观察 → 二次晋升 → 回滚；云厂商有效凭据；本机模型更多复杂问法稳定性。不存在真实提升时必须报告无候选/门控拒绝，不能为凑闭环虚构生产成功率。

## 采用的工业实践

- [Pydantic AI 生产与评测](https://pydantic.dev/docs/ai/production/)：区分替代模型单元测试、数据集评测与真实工具轨迹。
- [Pydantic AI 消息历史](https://pydantic.dev/docs/ai/core-concepts/message-history/)：明确处理历史预算和角色来源；裁剪不是无损记忆。
- [OWASP LLMSVS](https://owasp.github.io/www-project-llm-verification-standard/LLMSVS-v2.0-en.html)：会话/记忆隔离、内部知识泄露、向量与缓存访问控制。
- [Google SRE 灰度发布](https://sre.google/workbook/canarying-releases/)：明确候选与对照群体，观察业务单元执行结果，以实际指标决定推进或回退。
- [Microsoft 合成评测数据](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/evaluation-dataset-synthetic)：合成样本可补冷启动和边缘场景，但不能代替真实业务效果验收。
