# 本地长期记忆集成验收

2026-10-29 的真实服务验证通过。可复现命令（先运行 `start.cmd`，同一模型不要同时运行其他验收）：

```powershell
.venv\Scripts\python.exe scripts/verify_memory_native.py --json-out .local-run/memory-qdrant-live.json
```

默认使用 loopback Ollama 11434、Qdrant 6333；端口改变时传入 `--ollama-url` 和 `--qdrant-url`。命令先向运行中的 Ollama 查询 cloud inference 已禁用，然后创建随机命名的专用 Qdrant 测试集合和独立运营 SQLite。输入只有合成偏好，不打开员工记忆、知识库或业务数据库。测试结束删除本次专用集合，合成 SQLite 留在 `.local-run/memory-acceptance-*` 便于追查。

本次使用真实 `qwen3:4b-instruct`、真实 `bge-m3` 和 Qdrant HTTP 服务，抽取到 2 条偏好。原始非敏感结果见 memory-qdrant-live-2026-10-29.json。验证覆盖：

- 使用 Pydantic JSON Schema 约束实际抽取输出的字段及数字类型，本人能召回。
- 在同一集合写入两名合成用户的不同记录，另一用户只召回自己的记录；第三个模拟管理员身份的用户 ID 不能召回其他人的记录。服务按用户 ID 强制隔离，无管理员角色特例。
- 原始 Qdrant 点实际存在；Payload 仅含用户/租户/会话/状态等过滤字段和记录 ID，不含记忆正文。
- 关闭 Qdrant 客户端和 SQLite 连接，重新创建服务后仍能召回持久化记录。
- 遗忘本人记录后，本人的 SQL 记录和 Qdrant 点都被移除，无法再召回；另一用户的记录仍存在并可召回。
- 本次写入没有遗留失败补偿任务。

这项验证是服务集成检查，不能替代设置页面的查看、冲突确认和遗忘操作验收；它重建了服务及连接，没有重启 Qdrant 进程或操作系统，也不能证明一年保留期或灰度观察时间已经过去。

此前仅使用进程内向量索引的结果保留在 memory-live-2026-10-29.json，不要把它和真实 Qdrant 结果混为一谈。更早一次抽取失败的结果也继续保留在 agent-live-2026-10-29.json：模型曾将数字置信度输出为字符串，导致候选校验失败；现在通过 JSON Schema 约束输出修正，没有把无效字符串猜成数字。

审查同时修复了补偿队列的一处持久化问题：Qdrant 故障时，`InMemoryVectorIndex(fallback=True)` 接受写入不能算完成持久化，重放现在保留任务，等真实索引恢复再确认。`tests/test_memory_runtime.py`、`tests/test_memory_recall.py`、`tests/test_memory_conflicts.py` 共 19 项回归通过，包含抽取协议、云端调用提前拒绝和故障队列保留。
