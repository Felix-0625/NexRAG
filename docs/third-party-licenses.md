# 第三方许可摘要

本页记录当前锁定依赖的审计摘要，不替代各组件许可证或法律意见。NexRAG 自有代码及三个 base 服务为 MPL-2.0，见 [NOTICE](../NOTICE.md)。再分发须保留实际包含组件的版权、许可及所需源码/修改说明。

## 字体

| 字体 | 许可 | 使用情况 |
| --- | --- | --- |
| [Inter](https://github.com/rsms/inter/blob/master/LICENSE.txt) | SIL OFL 1.1；允许商用及随应用分发，保留版权/许可，字体不可单独销售 | 使用中，界面字体 |
| [KaTeX fonts](https://github.com/KaTeX/KaTeX/blob/main/LICENSE) | 字体 OFL 1.1 / KaTeX 程序 MIT | 使用中，数学公式按需懒加载 |

Vazirmatn 是波斯语/阿拉伯语字体，本项目中英界面不需要；`fonts.diff` 移除声明及字体栈引用，`prepare-webui.ps1` 在重放后（及幂等快路径）删除其 TTF。生成的 `static/assets/fonts/` 仅保留 Inter；KaTeX 字体由依赖另行提供。

## Open WebUI v0.11.4

固定版本采用 [Open WebUI License](https://raw.githubusercontent.com/AarhusAI/open-webui/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d/LICENSE)：BSD-3-style 的版权、二进制声明及禁止冒名背书条件，加品牌条款，并非纯 BSD-3-Clause。历史文件仍适用其原许可。

本仓库补丁保留 **Open WebUI** 名称、标识和品牌。第 4 条规定：滚动 30 天内超过 50 名直接终端用户时，**修改、移除、遮蔽或替换品牌**须事先书面许可或允许该修改的企业许可；保留品牌的部署并不因超过 50 人就必须购买授权。50 人以内的品牌例外及其他条件以原文为准。

## 依赖许可

npm production 依赖审计约 **636** 个包；主要许可计数为 MIT **471**、ISC **60**、BSD-3-Clause **54**、Apache-2.0 **22**、BSD-2-Clause **7**、BlueOak-1.0.0 **4**。计数随锁文件和平台可选依赖变化，以上不是穷尽且互斥的分类。Python 各锁定列表同样以 MIT、BSD、Apache 为主。

需特别保留义务或明确选择的组件：

| 组件 | 许可与采用方式 |
| --- | --- |
| psycopg、psycopg-binary | LGPL-3.0，独立依赖/动态链接；分发二进制时保留替换、重新链接及对应源码权利 |
| certifi、orjson、tqdm | 含 MPL-2.0 文件级义务；保留对应文件许可，修改受覆盖文件时提供源码 |
| tld（base/search-agent） | MPL/GPL/LGPL 三重许可，采用 MPL 选项 |
| @img/sharp-libvips | LGPL-3.0，可选平台预构建二进制；按实际分发内容履行义务 |
| lightningcss、pyodide | MPL-2.0，文件级义务 |
| jszip | MIT OR GPL，采用 MIT 选项 |
| py_rust_stemmers | MIT；包元数据虽为 UNKNOWN，许可文件为准 |

按上述许可选择，审计的依赖未引入必须按 GPL、AGPL、SSPL 或专有许可使用的组件；LGPL、MPL 义务仍须遵守。此结论**不包含**单独说明的 Open WebUI 品牌限制，也不覆盖另行下载的模型权重、运行时或数据集。分发前应重新核对目标平台的锁定依赖和附带许可。
