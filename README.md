# NexRAG · 本地 Agentic RAG

NexRAG 是一个以知识检索和业务数据库联合分析为核心的本地项目。它复用 Open WebUI 网页与 AarhusAI 摄入/检索管线，由一个 Agent 调用受权限约束的 SQL、知识检索、工具和导出能力。轻量数据中台负责账号授权、业务连接、知识权限、模型配置和运行治理，不引入分布式数仓组件。

## 一条命令启动

目前支持 **Windows x64**。安装 Git 并 clone 仓库后，在项目根目录执行：

```powershell
.\start.cmd
```

启动器自动准备项目内的 Python 3.12、Node.js、定制 Open WebUI、Qdrant 和依赖，再启动后端并打开浏览器。对话和 Embedding 默认使用本地 Ollama；若未安装则准备独立运行包。首次需要联网下载依赖和模型，后续复用 `.local-run/` 缓存。所有服务仅监听本机回环地址；默认网页 `http://127.0.0.1:3000`，端口冲突时以终端实际地址为准。

保持终端打开；按 Ctrl+C 停止本次启动的进程，账号、知识和产物保留。项目的启动和交付方式是原生进程，不提供容器部署入口，也不会操作机器上的其他项目或服务。

可选参数：`.\start.cmd -NoBrowser`、`-PrepareOnly`、`-SkipModels`。详细说明见 [本地运行](docs/local-run.md)。首次模型加载较慢，待终端提示 `All local services ready` 再开始检索。

## 核心设计

- **单 Agent**：选择实际对话模型，运行同一个工具循环；数据库与知识权限不会因切换模型扩大。
- **业务库只读**：服务端验证账号和业务范围，将授权行复制到独立分析快照。Agent 的 SQL 只在快照上执行，源连接强制只读。
- **独立知识权限**：个人库按所有权隔离，管理员身份不自动获得员工私人文件访问权；企业知识按明确授权下发。
- **单库父子块**：仅 Child 计算 Dense 和 BM42 Sparse 向量，Parent 正文与 `parent_id` 存进 Child 的 Qdrant JSON Payload，不额外建立 Parent 数据库。
- **检索链路**：Qdrant 原生 Dense + Sparse RRF 粗召回 → 本地 BGE cross-encoder 对 Child 打分、剔除低分并保留 Top-3 → 展开 Parent、按来源去重。旧索引的 `parent_content` 仍可读取，新摄入使用 `parent_text`。
- **轻量治理**：运营 SQLite 存储权限和运行元数据，与业务源库分离。评测、候选提示词/参数、人工批准和灰度回滚均有独立生命周期。私人内容不自动进入全局学习。
- **产物目录**：管理员绑定一个本机目录，产物按账号与对话分目录落盘；下载仍校验当前账号。

BM42 是指定方案，复用 Qdrant/FastEmbed 实现。Qdrant 当前将其标为实验性方法，不能据此宣称优于 BM25 或保证中文召回质量。BGE 阈值也不是校准后的正确率；应使用目标语料的独立评测集验证，详见 [RAG 与原生交付验收](docs/acceptance/rag-native.md)。

## 文档

| 主题 | 文档 |
| --- | --- |
| 设计与范围 | [设计说明](docs/design.md)、[交付状态](docs/delivery-status.md) |
| **评测与自我进化（RSI）** | [评测集、规则评测器、门控、灰度与回滚](docs/evaluation-and-rsi.md) |
| 长期记忆 | [隔离边界、写入防投毒、冲突消解](docs/long-term-memory.md) |
| 数据与权限 | [数据可信边界](docs/data-trust-boundaries.md)、[业务数据库接入](docs/connect-business-database.md)、[注册业务指标](docs/business-metrics.md)、[业务查询](docs/business-query.md) |
| 使用与配置 | [本地运行](docs/local-run.md)、[模型配置](docs/model-configuration.md)、[工作空间](docs/workspace-ui.md)、[知识库版本](docs/knowledge-versions.md) |
| 验收与发布 | [逐项验收清单](docs/acceptance/full-functional-acceptance.md)、[前端业务验收](docs/frontend-acceptance.md)、[发布门禁](docs/release-gates.md) |
| 项目约定 | [安全说明](SECURITY.md)、[参与维护](CONTRIBUTING.md)、[来源与许可](NOTICE.md) |

常用检查：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/doctor.ps1       # 环境自检
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check-code.ps1   # 锁文件 + Ruff + 测试
python scripts/run_experiment.py --splits holdout regression --repeats 2     # 手动评测
```

测试、协议连通、页面可达和真实业务正确性分别记录。一次用例通过不意味着任意复杂问题都能零幻觉。云模型需实际凭据，真实 PostgreSQL、目标语料检索质量和定时触发也必须单独验收。

## 目录

| 路径 | 职责 |
| --- | --- |
| `src/` | Agent、数据中台、授权、评测、进化、记忆和文件边界 |
| `base/ingestion-service/`、`base/retrieval-agent/` | 保留许可的 AarhusAI 基座源码和本项目修改 |
| `base/search-agent/` | 可选搜索适配基座，不自动部署外部搜索服务器 |
| `patches/open-webui/v0.11.4/` | 固定上游版本上的可重放网页补丁；修改生成的 `webui/` 后也必须更新补丁 |
| `scripts/` | 原生启动、准备、摄入、诊断和评测脚本 |
| `examples/`、`evals/` | 可替换的演示配置和独立评测种子，不放业务机密 |
| `data/`、`.local-run/` | 被忽略的运行数据、账号、模型缓存和产物；不要提交到公开仓库 |

## 许可证与上游归属

本项目自身代码遵循 [MPL-2.0](LICENSE)。AarhusAI 基座各自保留 `LICENSE.txt` 和原作者版权，修改应按其许可公开；不得将上游源码当作本项目原创。

Open WebUI 源码不随仓库提交。`scripts/prepare-webui.ps1` 检出 `AarhusAI/open-webui` 的固定版本 `v0.11.4`（提交 `8bd8b4fac5e059578ac0c74b3c18d11139f88b7d`），依次应用 `patches/open-webui/v0.11.4/nexrag/` 内的 tenant-selector、ops-dashboard、workspace 三个补丁。上游品牌与许可条款仍适用，见 [Open WebUI License](https://docs.openwebui.com/license/)。复用的原始补丁归属见 [补丁声明](patches/open-webui/NOTICE.md)。本项目与 Open WebUI、AarhusAI 官方没有隶属或背书关系。

Olist 示例仅用于演示，原始许可与使用限制见 [数据来源](docs/data-sources.md)。开源代码不等于附带商用示例数据许可，也不包含模型权重的重新授权。
