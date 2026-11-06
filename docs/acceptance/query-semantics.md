# 只读业务库、同轮快照与查询粒度

2026-11-01。代码和服务集成通过；真实页面发现及修复见收尾记录。最终明确指标口径的复杂网页题通过，任意复杂问法稳定性未证明。

Agent SQL 只进入授权租户的内存分析库。SQLite源使用mode=ro、query_only和authorizer；PostgreSQL需要专用只读角色与只读事务。分析库另有AST写操作拒绝、函数白名单、执行超时及返回上限；应用自己的对话和知识数据仍可写入，不属于业务源写权限。

每次生产/评测Agent调用建立BusinessReadSession；第一次合法SQL复制授权视图并关闭源连接，之后SQL及空结果诊断共用不可变snapshot_id。SQLite在同一读事务复制，PostgreSQL使用只读REPEATABLE READ。ContextVar传入工作线程，锁处理并行，结束/异常/超时关闭。不能改租户或来源；原逐调用权限刷新及来源代数检查仍有效。下一轮读取源库更新，不是CDC或旧结果实时变化。

SQLGlot区分普通聚合、窗口和分组。未分组且无唯一键依赖的明细裸列拒绝；窗口总额不重复相加。连接页面由后端发现真实主键，不采信客户端自报键；快照再强制PRIMARY KEY和NOT NULL，重复/空键失败，不静默折叠。

允许从事实视图出发、覆盖目标完整主键的INNER/LEFT JOIN。按事实键分组可以保留明细；列身份包含别名。即使事实行不增加，lookup金额仍可能重复：对重复lookup字段的SUM、TOTAL、AVG、COUNT及窗口汇总拒绝；SUM DISTINCT也不能掩盖重复。COUNT DISTINCT及MIN/MAX保留真实SQL语义。完成校验不把商品行数当订单数，不把重复lookup金额相加，不把AVG/MAX的total_amount别名当总额。LIMIT/OFFSET不证明全量；未认证CTE/子查询不能满足全量指标义务，内层危险关联也检查。不支持的关联要求在同轮分别查询。

最终根回归519通过/2跳过；SQLite覆盖生产Agent工具接线、并行、异常关闭、更新及拒绝。跨表黄金例：订单100/200、商品40/60/200，合法商品合计300、不同订单2，重复订单金额400不能当合计。PostgreSQL独立合成集群22项通过，同快照更新后1250、下一读取1750，源库恢复且哈希不变。见core-checks-2026-11-01.json及postgres-native-2026-10-31.json。

最终补充：混合粒度查询需要显式计数；仅明确要求“结果中的不同订单数”时，可按完整且强制唯一的订单键去重计算。不得按金额去重，也不能用该结果证明包含无商品行的源订单总数。缺少完整订单键、LIMIT/OFFSET或未认证查询均不能采用此证明。

这不是任意SQL或自然语言口径的真伪证明。主键不能证明业务定义正确；单位、过滤范围与实体须以企业元数据和真实样本评估。任意多对多自动重写、分页全量导出、目标行业质量及大租户复制性能没有被本轮证明。

设计参考：[Snowflake实体键与关系](https://docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec)、[SQLite聚合裸列规则](https://www.sqlite.org/lang_select.html#bareagg)。未引入大型语义平台。

注册指标路径另见[business-metrics.md](../business-metrics.md)：同一视图的可信聚合由AST编译，不跨表复制指标；多个视图共用本轮快照，最终数值从实际结果呈现。任意自然语言意图的完整识别仍需独立质量评估。
