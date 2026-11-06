# Open WebUI 来源与原生适配

固定基座：[AarhusAI/open-webui](https://github.com/AarhusAI/open-webui)，版本 `v0.11.4`，提交 `8bd8b4fac5e059578ac0c74b3c18d11139f88b7d`。保留该版本源码中的 Open WebUI License、版权声明及来源/品牌标识；这些文件不因本项目的 MPL 声明改变许可。品牌条款及例外以固定版本 `LICENSE` 原文为准。

当前准备链按顺序应用本项目维护的 `nexrag/tenant-selector.diff`、`nexrag/ops-dashboard.diff`、`nexrag/workspace.diff`、`nexrag/security-npm.diff`。最后一个只修改 `package.json` 与 `package-lock.json`：用 `npm audit fix` 升级有安全修复的前端依赖，并按 SheetJS 官方文档改用其 CDN 发布的 `xlsx` 0.20.3（npm 仓库上的 0.18.5 已停止更新且存在已知漏洞）。`webui/` 是按固定提交生成的忽略目录，不是另一个可独立发布的本项目原创源码树。

2026-10-29 替换了先前依赖的 AarhusAI 编排仓库补丁 `template-for-rag-queries.diff` 和 `added-support-for-external-rag-ingestion-and-retrieval.diff`。核查其原始 [编排仓库](https://github.com/AarhusAI/aarhusai-docker) 时未取得可用的独立再分发许可文本，因此没有声称本项目许可可覆盖它们：这两个补丁及历史编排/补丁资产均已从交付文件移除；生成源码通过三方对照恢复为固定上游与本项目修改，旧 `retrieval/external_service.py` 也已移除。历史备份仅存放于被忽略的本地 `.local-run/`，不属于开源交付。

文档集成改为独立编写的 `utils/nexrag_native_rag.py`：依据本仓库摄入服务的公开 HTTP 契约 `PUT /api/v1/ingest`、`POST /api/v1/extract`、`DELETE /api/v1/documents/{file_id}` 进行本机认证请求。它复用 WebUI 文件元数据及先行 ACL 校验，不包含旧补丁的配置接口、远端 S3 分支或另一套聊天检索循环。查询仍由 NexRAG 核心授权 Agent 调用检索工具；原生桥只负责文档摄入、文本更新与 scoped 删除。`utils/nexrag_ingestion.py` 的本机 SQLite 注册表仅存文件/库的标识映射，不存 Parent 正文。

本项目补丁仍受适用的上游许可和版权要求约束。此说明仅解决上述两补丁的来源问题，不等同于所有依赖、模型权重、示例数据的完整法律审查。模型与数据集须逐项遵守各自条款。不要把账号、密钥、上传文件、模型权重或运行日志写入补丁。
