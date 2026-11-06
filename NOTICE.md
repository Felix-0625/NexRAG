# 来源与许可声明

NexRAG 自有源码采用根目录 MPL-2.0。包含和修改的 AarhusAI ingestion-service、retrieval-agent、search-agent 分别保留其目录中的原始许可证及版权声明。

Open WebUI 不包含在源码交付中，由准备脚本获取固定版本并应用补丁。使用时仍须遵守该固定上游版本的许可证和品牌条款。补丁来源与本项目独立实现的桥接边界见 [patches/open-webui/NOTICE.md](patches/open-webui/NOTICE.md)。本项目未获得 Open WebUI 或 AarhusAI 的官方背书。

示例 Olist 数据的来源和使用限制见 [docs/data-sources.md](docs/data-sources.md)。模型权重、运行时二进制与第三方依赖分别受其自身许可约束；本项目许可证不重新授权它们。

## 第三方许可核对（2026-11-05）

以下是对随项目分发或默认安装的依赖做的抽查，不是完整的法律审查。

| 项目 | 许可 | 说明 |
| --- | --- | --- |
| Inter、Vazirmatn 字体（WebUI 静态资源） | SIL OFL 1.1 | 字体文件的 name 表中写明 OFL 1.1，可免费商用与再分发；不得单独售卖字体本身。两个字体都被 `@font-face` 引用，没有遗留未使用的字体文件 |
| KaTeX 字体（随 `katex` npm 包） | SIL OFL 1.1 | 公式渲染使用，随依赖安装 |
| Python 依赖 | 绝大多数为 MIT / BSD / Apache-2.0 | 另有 `certifi`、`tqdm`（MPL-2.0 部分）、`py_rust_stemmers`（元数据缺失，上游 LICENSE 文件为 MIT） |
| `psycopg` / `psycopg-binary` | LGPL-3.0 | 作为独立 Python 包导入使用（连接 PostgreSQL 业务源），本项目未修改其源码。若需再分发包含它的二进制包，请先核对 LGPL-3.0 的具体义务 |
| 前端 npm 生产依赖 | 660 个包中 647 个（98%）为 MIT / ISC / BSD / Apache-2.0 等宽松许可 | 少量 MPL-2.0（`lightningcss`、`pyodide`）、`jszip`（MIT OR GPL-3.0，取 MIT）、`@img/sharp-*` 原生库包（Apache-2.0 AND LGPL-3.0-or-later） |
| Open WebUI v0.11.4 | BSD-3-Clause 加品牌条款 | 条款 4 禁止在超过 50 名终端用户（30 天滚动）的部署中修改或移除 “Open WebUI” 品牌，除非获得书面许可或企业授权。本项目的补丁保留其品牌标识，规模超过该阈值前须单独取得授权 |

此文件是归属与使用边界说明，不是对全部传递依赖或适用法律的法律意见。
