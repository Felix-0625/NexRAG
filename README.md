# NexRAG · 本地 Agentic RAG

NexRAG 是一款在本机运行的知识检索与业务数据分析应用。通过基于 Open WebUI 的工作空间，一个 Agent 可在授权范围内检索知识、查询只读业务数据库，并按实际证据生成和导出结果。数据中台统一管理账号授权、数据源、知识权限、模型配置及运行治理。

## 一条命令启动

目前支持 **Windows x64**。安装 Git 后，从 [GitHub Releases](https://github.com/Felix-0625/NexRAG/releases/latest) 下载 `NexRAG-<版本>-source.zip` 并解压，或直接克隆本仓库。进入解压或克隆后的目录运行：

```powershell
.\start.cmd
```

Release 源码包包含启动器和 NexRAG 的前端补丁。首次运行时，启动器联网获取固定版本的 Open WebUI 源码，在本机应用补丁、安装前端依赖，然后启动定制界面并打开浏览器。生成的前端位于 `webui/`，无需另行下载。启动器也会准备项目使用的 Python 3.12、Node.js、Qdrant 和默认的本地 Ollama 模型。后续启动复用本机文件和缓存。服务只监听本机回环地址，网页默认是 `http://127.0.0.1:3000`，端口冲突时以终端实际地址为准。

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

## 评测与 RSI

合成评测复用生产 Agent，对回答、工具调用、SQL、引用和安全边界按规则打分。RSI 只用合成训练失败生成提示词和参数候选。通过独立评测门控后，管理员批准灰度，再依据真实运行指标确认晋升。维护入口、数据边界和状态流转见 [评测与 RSI](docs/evaluation-and-rsi.md)。

## 文档

| 主题 | 文档 |
| --- | --- |
| 设计与范围 | [设计说明](docs/design.md)、[交付状态](docs/delivery-status.md) |
| 长期记忆 | [隔离边界、写入防投毒、冲突消解](docs/long-term-memory.md) |
| 数据与权限 | [数据可信边界](docs/data-trust-boundaries.md)、[业务数据库接入](docs/connect-business-database.md)、[注册业务指标](docs/business-metrics.md)、[业务查询](docs/business-query.md) |
| 使用与配置 | [本地运行](docs/local-run.md)、[模型配置](docs/model-configuration.md)、[工作空间](docs/workspace-ui.md)、[知识库版本](docs/knowledge-versions.md) |
| 验收与发布 | [逐项验收清单](docs/acceptance/full-functional-acceptance.md)、[前端业务验收](docs/frontend-acceptance.md)、[发布门禁](docs/release-gates.md) |
| 项目约定 | [安全说明](SECURITY.md)、[参与维护](CONTRIBUTING.md)、[来源与许可](NOTICE.md) |
| 测试与示例 | [测试导航](tests/README.md)、[评测材料](evals/README.md)、[演示配置](examples/README.md) |

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
| `base/ingestion-service/`、`base/retrieval-agent/` | 文档摄入与知识检索服务 |
| `base/search-agent/` | 可选搜索适配基座，不自动部署外部搜索服务器 |
| `patches/open-webui/v0.11.4/` | 固定上游版本上的可重放网页补丁；修改生成的 `webui/` 后也必须更新补丁 |
| `scripts/` | 原生启动、准备、摄入、诊断和评测脚本 |
| `examples/`、`evals/` | 可替换的演示配置和独立评测种子，不放业务机密 |
| `data/`、`.local-run/` | 被忽略的运行数据、账号、模型缓存和产物；不要提交到公开仓库 |

## 许可证

自有代码采用 [MPL-2.0](LICENSE)。第三方组件、字体、模型与示例数据的来源和许可见 [NOTICE](NOTICE.md)。
