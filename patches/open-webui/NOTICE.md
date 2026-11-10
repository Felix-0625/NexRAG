# Open WebUI 来源与原生适配

固定基座：[AarhusAI/open-webui](https://github.com/AarhusAI/open-webui)，版本 `v0.11.4`，提交 `8bd8b4fac5e059578ac0c74b3c18d11139f88b7d`。保留该版本源码中的 Open WebUI License、版权声明及来源/品牌标识；这些文件不因本项目的 MPL 声明改变许可。品牌条款及例外以固定版本 `LICENSE` 原文为准。

准备脚本依次应用 `nexrag/tenant-selector.diff`、`nexrag/ops-dashboard.diff`、`nexrag/workspace.diff`、`nexrag/security-npm.diff` 和 `nexrag/fonts.diff`。依赖补丁维护 `package.json`、锁文件和 ESLint 配置，移除未使用的测试工具及部署适配器，固定 Vite 6.4.5 与 Svelte 插件 5.1.1，并使用 SheetJS 官方 CDN 发布的 `xlsx` 0.20.3。字体补丁移除 Vazirmatn 声明及字体栈引用；因准备流程的索引比对仅处理文本，TTF 二进制由准备脚本在重放后及幂等快路径中删除，`node scripts/check-webui.mjs` 验证仅保留 Inter。生成的 `webui/` 被 Git 忽略，项目修改以补丁为准。见 [安全复扫记录](../../docs/acceptance/security-audit.md)。

2026-10-29 替换了先前依赖的 AarhusAI 编排仓库补丁 `template-for-rag-queries.diff` 和 `added-support-for-external-rag-ingestion-and-retrieval.diff`。核查其原始 [编排仓库](https://github.com/AarhusAI/aarhusai-docker) 时未取得可用的独立再分发许可文本，因此没有声称本项目许可可覆盖它们：这两个补丁及历史编排/补丁资产均已从交付文件移除；生成源码通过三方对照恢复为固定上游与本项目修改，旧 `retrieval/external_service.py` 也已移除。历史备份仅存放于被忽略的本地 `.local-run/`，不属于开源交付。

文档集成改为独立编写的 `utils/nexrag_native_rag.py`：依据本仓库摄入服务的公开 HTTP 契约 `PUT /api/v1/ingest`、`POST /api/v1/extract`、`DELETE /api/v1/documents/{file_id}` 进行本机认证请求。它复用 WebUI 文件元数据及先行 ACL 校验，不包含旧补丁的配置接口、远端 S3 分支或另一套聊天检索循环。查询仍由 NexRAG 核心授权 Agent 调用检索工具；原生桥只负责文档摄入、文本更新与 scoped 删除。`utils/nexrag_ingestion.py` 的本机 SQLite 注册表仅存文件/库的标识映射，不存 Parent 正文。

本项目补丁仍受适用的上游许可和版权要求约束。此说明仅解决上述两补丁的来源问题，不等同于所有依赖、模型权重、示例数据的完整法律审查。模型与数据集须逐项遵守各自条款。不要把账号、密钥、上传文件、模型权重或运行日志写入补丁。
