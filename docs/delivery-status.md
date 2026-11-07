# 交付状态

本项目以 Windows x64 本地原生运行和 GitHub 源码发布交付。网页来自固定版本 Open WebUI，本项目维护 Agent、数据权限、集成桥、运营能力与补丁。入口为 `start.cmd`，首次运行需要联网下载依赖和模型。

## v1.0.0 的交付范围

- 单 Agent 联合查询知识与只读业务数据，包含同轮证据校验、文件分析和导出。
- 账号分发、业务租户和知识授权、模型及 MCP 管理，外发受资源分级约束。
- Child Dense + BM42/RRF → BGE 重排 → Parent 展开。
- 用户隔离的长期记忆、合成评测、版本化提示词与参数、人工审批和灰度回滚。
- 可重放的四个 WebUI 补丁、依赖锁、Ruff/Pytest、依赖审计和发布门禁。

## 验证范围

发布门禁运行核心、摄入、检索、可选搜索服务回归，检查文档链接、版本、补丁重放和前端构建，并审计五套 Python 锁和完整 npm 锁。Windows 文件链接边界须在 CI 中实际通过。真实模型、PostgreSQL 和网页业务流程的历史证据分别保存在 [逐项验收](acceptance/full-functional-acceptance.md) 与 [前端验收](frontend-acceptance.md)。

当前 npm 仍有可选浏览器朗读链的已审核高危公告，具体边界和补丁版本见 [安全记录](acceptance/security-audit.md)。复杂自然语言回答的正确率、目标语料检索质量和真实长期灰度收益仍需独立评测。

## 维护入口

测试、示例和评测材料分别见 [测试导航](../tests/README.md)、[演示配置](../examples/README.md)、[评测材料](../evals/README.md)。2026-11-07 的仓库复核补齐了导航、实例无关的评测命令与关键维护注释；超千行的摄入测试按工厂与分块行为拆分，原测试断言保留。维护的交付代码均不超过 1,000 行，生成的上游 WebUI 含大文件，继续通过补丁维护。历史敏感信息扫描及其局限见 [安全记录](acceptance/security-audit.md)。

- `scripts/check-code.ps1 -IncludeWebUI -IncludeRagServices` 检查代码、锁文件、补丁与服务回归。
- `scripts/audit-dependencies.ps1` 审计五套 Python 锁，`node scripts/audit-webui.mjs` 审计 npm 锁。
- `scripts/check-repository.py` 检查源码交付、代码文件 1,000 行上限、项目文档链接和版本一致性。
- `scripts/verify_source_delivery.py` 导出当前候选源码并检查已知本机凭据。
- GitHub Release 使用标签对应的 CHANGELOG，须在全部 CI 门禁成功后发布源码 ZIP 与 SHA-256。
