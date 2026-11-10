# 来源与许可声明

NexRAG 自有源码采用 [MPL-2.0](LICENSE)。摄入、检索和搜索服务沿用各自的 AarhusAI 源码与 MPL-2.0 许可，分发时保留版权和原许可证。见 [摄入](base/ingestion-service/LICENSE.txt)、[检索](base/retrieval-agent/LICENSE.txt)、[搜索](base/search-agent/LICENSE.txt)。

Open WebUI 由准备脚本获取固定的 v0.11.4 源码并应用本项目补丁。该版本含 Open WebUI License 及历史许可，适用源码与二进制分发的署名要求。按固定版本第 4 条，在滚动 30 天内超过 50 名直接终端用户的部署或分发中，改动或移除其品牌须取得书面许可或相应企业授权。NexRAG 是独立集成项目。见 [固定版本许可](https://raw.githubusercontent.com/AarhusAI/open-webui/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d/LICENSE) 与 [补丁来源](patches/open-webui/NOTICE.md)。

## 前端字体

- **Inter** 为 SIL OFL 1.1，允许免费商用和随应用分发；保留 TTF 内嵌版权和许可声明，字体本身不可单独销售。Vazirmatn 已由字体补丁及准备脚本移除，不再声明、引用或分发。见 [Inter](https://github.com/rsms/inter/blob/master/LICENSE.txt)、[OFL FAQ](https://openfontlicense.org/ofl-faq/)。
- **KaTeX** 程序采用 MIT，随 npm 包安装的数学字体文件自带 OFL 1.1 声明及保留字体名。公式渲染依赖这些字体。见 [KaTeX 许可](https://github.com/KaTeX/KaTeX/blob/main/LICENSE)。

## 组件与其他资源

- 前端组件的许可证以锁定 npm 版本为准。Svelte、SvelteKit、Vite、Mermaid 等采用 MIT。DOMPurify 可选 Apache-2.0 或 MPL-2.0，JSZip 可选 MIT 或 GPL-3.0-or-later。Lightning CSS、Pyodide 使用 MPL-2.0，部分 sharp 平台二进制含 LGPL-3.0-or-later。锁文件缺少许可元数据的 `khroma`、`svelte-toolbelt` 和 `xmlhttprequest-ssl` 已对照安装包的许可文件。
- Python 依赖中，`certifi` 和 `tqdm` 含 MPL-2.0 许可，`psycopg` 及其二进制包使用 LGPL-3.0。本项目通过独立依赖包调用它们。打包再分发时按目标平台和实际包含的文件核对相应义务。
- Olist 原始数据仅限非商业用途。项目不附带原始数据或模型权重，所下载的模型和运行时二进制各自适用原许可。见 [数据来源](docs/data-sources.md)。

字体、Open WebUI 品牌条款与锁定依赖的许可摘要见 [第三方许可](docs/third-party-licenses.md)；实际分发仍以各组件附带的许可证为准。
