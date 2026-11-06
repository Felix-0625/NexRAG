# 业务答案、只读数据源与私人知识库

本系统继续使用一个 Agent 调用工具。可靠性由工具执行、数据权限和输出验证共同约束，不能只依赖提示词或增加一个“审稿 Agent”。

维护时按职责查找代码：`task_intent.py` 识别显式请求与禁止项，`grounding.py` 保存本轮证据并验证完成条件，`evidence_fallback.py` 只呈现已有工具结果。旧的 `grounding.requested_capabilities`、`grounding.database_write_request` 和 `EvidenceLedger.safe_answer` 接口仍可使用。提示词组装与工具执行分离；移动显示逻辑不能绕过权限复查或数字校验。

## 答案证据

- 服务端从当前请求识别 SQL、明细、计数、金额汇总、文档检索、导出等必要交付项；Agent 的分析计划只能增加要求，不能取消要求。只查 COUNT/SUM 不能满足订单明细要求。
- 只有成功执行 `query_database` 的实际返回行能够满足 SQL 要求。表结构、空泛的反思文字、历史对话和个人记忆均不能代替查询。
- 文档检索无命中时允许说明未找到依据；检索故障不算成功证据。
- 本轮证据账本校验数字、订单标识和常见总金额/笔数；导出的数据行必须来自实际工具结果。新指标应在 SQL 中计算。
- 校验失败后由同一个 Agent 补查或修正；重试耗尽或超时时，仅在必要工具均已执行后展示实际结果表格和文档原文。缺少必要工具结果则返回明确的未完成错误。
- SSE 只发送经过校验的最终文本，不提前泄露尚未验证的生成内容。

边界：目前必要工具识别和数字校验是针对支持业务的确定性规则，并非通用语义证明。它不能保证任意自然语言的所有推论正确，也不能识别所有“数字存在但关系理解错”的情况。复杂问题必须持续加入真实模型回归集；通过一次演示不能宣称零幻觉。

知识检索授权独立于业务数据库选择。显式只查制度/文档且禁止SQL的请求，仅向模型暴露知识检索工具；该工具属于应用内部检索，不是MCP外部工具。缺少实际检索时拒绝输出“无匹配”或“无权限”等猜测，并反馈具体补查工具。Ollama的OpenAI兼容接口不保证强制工具选择，因此不能把`tool_choice=required`当作已经调用工具的证据；参见[Ollama官方兼容范围](https://github.com/ollama/ollama/blob/main/docs/api/openai-compatibility.mdx)。

外部搜索在实际发送查询之前重新核对账号、知识库分级与读取授权，并在正式业务请求中复查业务范围。授权服务异常、分级收紧或权限撤销时不发送查询，不沿用模型生成前的授权快照。这落实了[OWASP逐请求授权检查](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html#validate-the-permissions-on-every-request)；四种拒绝分支由`tests/test_web_search.py`验证，不冒充网页中真实运行期间撤权的验收。

## 业务数据库只读

- Agent 只接触租户授权行复制形成的内存分析库；SQL 不直接在源库执行。
- SQL 只接受单条 SELECT，禁止 DML、DDL、SELECT INTO、锁表、多语句、附加数据库和未允许函数。
- SQLite 源连接 `mode=ro`、`query_only=ON`，并安装 SELECT/READ/允许函数的授权器。分析库也有独立只读授权器。
- PostgreSQL 使用只读、可重复读事务和执行超时，并检查连接角色。超级用户、管理角色、具有表写入/模式建表/数据库建表权限的账号会被拒绝；请由企业 DBA 提供专用只读账号。本应用不代替 DBA 修改生产授权。
- 显式业务写入请求在调用模型前直接拒绝；底层只读约束仍适用于绕过自然语言识别的攻击。
- 知识库维护、版本发布和应用自身配置写入不属于业务源库写入。

## 私人内容的所有权

在 `NEXRAG_WORKSPACE=true` 模式下，管理员身份不再提供私人内容访问特权。所有者或明确 ACL 授权是读取的必要条件，编辑必须另有写权限。

这一规则覆盖知识库列表、详情、文件正文、下载、文件处理、检索集合、版本记录和历史 ZIP。相关响应使用 `Cache-Control: private, no-store` 并区分 Cookie/Authorization，避免同一浏览器切换账号后复用新响应的正文缓存；此前已经合法下载到本机的文件不属于服务端权限可撤回的范围。全局重置/删除入口被关闭，防止管理员误删他人私人资料；批量索引只处理自己可写的知识库。共享企业知识仍由管理员维护，通过显式授权供员工只读使用。

管理员保留账号和企业授权管理职责，但不能借运行追踪浏览员工提问、回答或工具正文。新增私人运行追踪仅存工具名、状态、耗时、计数等运行元数据；历史追踪正文对其他账号隐藏，历史个人记录标记为私人范围，禁止进入全局评测和提示词优化。现有会话和文件不被删除。应用层权限不等于对拥有操作系统/数据库直接访问权限的机器所有者提供加密隔离。

## 工业实践参考

- [LangChain SQL 工具文档](https://reference.langchain.com/python/langchain-community/agent_toolkits/sql/base)：数据库最小权限、限定表、只读账号和查询约束。
- [Azure AI Search 文档级访问控制](https://learn.microsoft.com/en-us/azure/search/search-document-level-access-overview)：在检索时按调用者权限裁剪文档。
- [Amazon Bedrock contextual grounding](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-contextual-grounding-check.html)：根据引用依据与问题检查回答的支撑程度；这类概率性检查仍不能代替确定性数据库权限和业务校验。
