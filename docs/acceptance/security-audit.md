# 原生模式安全专项验收记录

日期：2026-10-29。对象：本地运行的 NexRAG 与复用的 Open WebUI 后端。状态：**专项修复的后端契约验证通过，仓库完整安全验收未完成**。本文不把静态检查、隔离测试、实际部署、真实浏览器业务验收混为一谈。

## 已确认的产品边界

用户明确选择企业常规权限隔离：私人知识库仅所属账号可访问，业务管理员身份不具有读取员工私库的权限；管理员能够分发账号、重置密码，重置及登录必须有审计。部署者及操作系统管理员受信任，不要求用户自持密钥的端到端加密。

因此，应用角色 ACL 应当阻止管理员用自己的会话读取私库，但无法防范能够读取本机存储的部署者，也不能声称防止管理员滥用重置流程后以员工身份登录。审计使这种操作可追溯，不等于密码学上的不可访问。若未来需要抵抗恶意部署者或重置后冒用，必须重新设计恢复、密钥和身份验证机制。

## 审查方法与覆盖

使用 Bug Hunter 的 local-sequential 流程，按 Triage → Recon → Hunter → Skeptic → Referee → 有明确授权的修复执行。三个候选缺陷均重读调用链、检查反证后确认，没有使用真实业务数据执行攻击。证据在仓库 `.bug-hunter/` 中：`triage.json`、`recon.json`、`hunter-findings.json`、`skeptic.json`、`referee.json`、`scan-report.json`、`coverage.json`、`report.md`。

最初 `src` 风险清单为 56 个文件、extended 范围；本专项完整阅读了其中的较小权限模块及原生适配模块，对大文件只阅读相关函数及其调用者。`coverage.json` 中的 `in_progress` 表示没有完整阅读该文件，整体维持 `IN_PROGRESS`。正式 STRIDE 全仓威胁模型、全部依赖漏洞扫描、所有上游 API 及部署配置没有在本专项完成。三个发现完成裁决，不代表仓库不存在其他缺陷。

## 已确认缺陷与处理

| 编号 | 严重性 | 可达问题 | 当前处理和证明 |
| --- | --- | --- | --- |
| SEC-001 | High / CWE-613 | 无 Redis 的原生启动中，`is_valid_token` 直接放行，管理员改密后旧 JWT 仍有效。 | SQLite 会话代数及单 token 撤销；账号创建、改密、重置、角色变化贯通实际认证函数。隔离测试通过；真实用户改密浏览器验收另行执行。 |
| SEC-002 | High / CWE-200 | 资源读取 ACL 没有约束处理目的地；远程模型可接到业务/私库上下文，生成的检索词可外发。 | 主验收代理正在落实 core 数据分级、账号目的地授权及每次工具调用刷新。本文确认修复前缺陷；不以本专项 108 项测试宣称该 core 修复已完成联合验收。另关闭上游绕行入口，见下文。 |
| SEC-003 | High / CWE-94 | 员工具备工具维护权限时，`tools/create`/更新可把提交的 Python 送到 `load_tool_module_by_id` 的 `exec`，创建时即执行。 | 原生模式默认禁止工具/函数源码插件执行及安装依赖，连业务管理员也默认禁止；路由与执行加载器同时拦截。隔离测试验证调用链在接触 `exec` 前停止。 |

另确认了账号邮箱复用与按 email 存储的业务授权冲突：通用用户编辑可改变邮箱或物理删除账号后，新账号可能继承同邮箱旧授权。本轮采用保守修复：**原生账号的登录邮箱不可修改，不允许物理删除；用 `pending` 停用并保留身份、授权记录及审计**。姓名、个人资料仍可修改。需要更换登录身份时新建账号并显式分权，不进行静默授权迁移。

导出路径中 `用户A/../用户B` 的跨账号解析风险已经交给主验收代理处理，`tests/test_exports.py` 包含解析后的用户目录边界回归。文件沙箱共享根目录及特定工具访问方式仍需联合权限审查，不能因一个下载代理拒绝 `..` 就宣称整个文件系统安全。

## 账号生命周期与接口

状态保存在 `DATA_DIR/nexrag-identity.sqlite3`，使用 Python 标准库 SQLite，没有新增 Redis 或其他服务。`accounts` 只存用户 ID、会话代数、强制改密标志；`revoked_tokens` 存 token 标识及过期时间；不保存密码、Bearer token 或业务正文。

1. 仅空库首次初始化可使用普通 signup；已有账号后，原生模式服务端拒绝公开注册，即使上游配置重新开启注册。
2. 管理员使用原 `auths/add` 分发的账号立即标记 `must_change_password=true`。初始凭据只能检查自己的会话、退出和改密，不能访问业务接口。API key 在原生模式禁用，避免绕过生命周期约束。
3. 用户提供旧密码和新密码完成改密，撤销之前的全部会话，清除强制标记；新的专用接口签发新 JWT 并更新 HttpOnly cookie。旧浏览器、账号记忆和旧 token 不会自动升级。
4. 管理员通过原 `users/update` 重置任意目标密码，同样撤销旧会话并强制目标下次改密。CAS 检查避免较慢的用户改密完成请求清除较新的管理员重置标记。
5. 新密码至少 15 字符，拒绝少量常见口令和单字符重复，沿用 bcrypt 字节长度上限。没有声称凭长度和一个小型黑名单就符合全部 NIST 要求。历史账号不会因上线修复而自动改密或重写密码。
6. 已验证 WebUI JWT 的原始 `nsv` 写入 `request.state.nexrag_session_version`；转发 JWT 使用该原始值生成 `session_version`，不读最新数据库代数来升级旧请求。长运行的 core 每次执行应比较当前代数，相关 core 接入由主代理负责。

前端合同（全部位于 `/api/v1/auths`）：

| 方法 / 路径 | 请求 / 返回 | 权限 |
| --- | --- | --- |
| `GET /nexrag/identity` | `session_version`, `must_change_password`, `password_min_length:15`, `distributed_accounts_only:true` | 当前会话；强制改密时可用 |
| `POST /nexrag/change-password` | 输入 `{password,new_password}`；返回原 SessionUserResponse，增加 `must_change_password`，含新 token 并设置会话 cookie | 当前会话，必须验证旧密码 |
| `GET /nexrag/security-events?target_id=...&limit=100` | `{data:[{id,action,actor_id,target_id,created_at}]}`；`created_at` 为 Unix 秒；limit 为 1–200 | 仅管理员 |

普通业务请求处于强制改密状态时返回 HTTP 403，`detail.code=password_change_required`。前端应在登录/恢复会话及全局业务页挂载强制改密界面；界面不能成为唯一门禁。账号目录的安全记录可按 `target_id` 查询。

审计独立于上游可选 `publish_event` 订阅落盘，只允许 `account.provisioned`、`account.bootstrap`、`account.role_changed`、`password.reset`、`password.changed`、`session.signed_in`、`session.signed_out`。仅存 actor/target/action/time 与事件 ID，不保存密码、token、邮箱、私库正文。该轻量审计不是防部署者篡改的外部 WORM 日志，也尚无完整归档保留策略。

## 工具、MCP 与上游连接的默认拒绝

默认 `NEXRAG_WORKSPACE=true` 时禁止上游 Python 工具、函数、pipe 源码执行，包括业务管理员。`NEXRAG_ALLOW_TRUSTED_PLUGINS=true` 是部署者显式选择的高信任模式：只允许管理员维护的源码插件，旧员工创建的插件仍隔离。该模式给予插件 WebUI 进程可访问的文件及网络能力；不能以应用 ACL 声称隔离恶意源码作者与员工私库。平台已有确定性的 SQL、知识检索、导出工具不受源码插件开关影响。

`NativeIntegrationGuard` 是服务端 ASGI 门禁；必须在 `main.py` 注册中间件并通过原生补丁发布。它阻止：

- 原始 `/openai/*`、`/ollama/*`、上游 pipelines/terminal 路由及上游连接配置入口。
- 上游 URL/web/youtube ingestion、检索 embedding/config 更新绕路。
- 对话结构化 `direct`、`model_item.direct`、`tool_servers`、raw `tools/functions`、远程 `server:*`/MCP/terminal 工具 ID、上游 web search、代码解释器和图像生成特性。
- 个人 direct connections、个人 tool servers，以及个人外部 webhook 设置。
- 结构化 HTTP(S)/协议相对的远程附件及图片 URL；本地已上传附件仍由原有文件 ACL 控制，普通消息里的文字链接不会被当成权限开关。

`utils/webhook.py` 的执行入口也在原生模式直接拒绝，覆盖历史存量个人/全局 webhook，拒绝发生在日志正文、DNS 校验与 HTTP 发送之前。仅禁止新设置不足以防止旧通知自动外发。

远程 MCP 本轮暂不支持；需要经 core 的统一账号、资源与目的地政策接入后才能宣布可用。显示开关或成功保存配置不是验收证据。上游 provider 清单和功能配置仍应由原生 profile 强制收敛，不能只靠前端隐藏。没有把 guard 视为网络沙箱；用户、部署者及浏览器之外的软件仍能自行联网。

本地 Ollama 地址也不能单独证明模型完全本地运行：`:cloud` 模型可能通过本地进程转发。应禁用 Ollama 云功能（部署者设置 `OLLAMA_NO_CLOUD=1`）并拒绝 cloud 模型标签；仍需根据真正部署配置验证，而非只检查 loopback URL。

## 实际验证与剩余验收

本次最终专项命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_native_identity.py tests/test_native_tool_authoring.py tests/test_native_integration_guard.py tests/test_private_content_boundary.py tests/test_readonly_boundary.py tests/test_tenant_api.py tests/test_tenant_management.py tests/test_exports.py -q -rs --tb=short --basetemp=.local-run/pytest-security-final-webhooks
```

结果：**108 passed，1 skipped**。跳过的是 Windows 真正创建文件系统 symlink 的测试，当前环境没有开发者模式/所需权限；其余导出路径拒绝用例通过。首次沙箱内运行因 Windows 临时目录 `WinError 5` 失败，使用工作区隔离临时目录并经执行权限审查后重跑得到上述结果；没有把环境错误算成产品通过。

身份测试加载实际待发布函数体，替换数据库用户模型及外部依赖，在 disposable SQLite 与 mock 用户上执行；工具路由使用真实路由依赖注册到隔离 FastAPI；integration guard 使用真实 ASGI 中间件及无副作用 endpoint。外部网络发送、真实密码变更均未发生。它们证明后端合同与门禁，不证明完整上游依赖启动或浏览器改密闭环已经验收。

已有私库、只读 SQL、租户及导出测试被一并复测；这不能替代真实 PostgreSQL 专用只读角色、数据库权限变更实时生效及不同组织账号的浏览器业务操作。尚需联合验收：

1. 原生补丁重新生成、全新 clone 一条命令启动、中间件实际挂载和真实路由 403。
2. 浏览器分发临时账号 → 强制改密 → 业务访问 → 管理员重置 → 旧会话拒绝 → 再改密；使用专门的验收账号，不修改历史管理员/员工密码。
3. 管理员自己会话访问员工私库的 UI、列表、检索、文档、下载全部拒绝；企业库授权及发布更新正常。
4. core 目的地策略、撤权/重置后长运行中止、公开/内部/核心机密矩阵以及输入带敏感信息的联网搜索拒绝。
5. 真正数据库只读身份、文件/技能/自动化全路径、剩余上游 API、依赖安全和发布配置复核。

本轮代码哈希在 `.bug-hunter/security-postfix-hashes.json`；供主代理整合前后对照。共享工作区仍有其他代理修改，哈希对应时间点，后续格式化或整合后需重新生成。没有提交或推送。

## 对照的工业界资料

- [OWASP Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)：默认拒绝、每次请求检查授权；提示词不能充当数据边界。
- [OWASP Forgot Password Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html)：重置后的会话撤销及恢复流程。这里的管理员重置不自动登录；用户已认证并提交旧密码的主动改密另外签发新会话。
- [NIST SP 800-63B-4](https://pages.nist.gov/800-63-4/sp800-63b.html)：单因素密码最小长度与常见口令检查；本项目未宣称完整认证等级合规。
- [OWASP AI Agent Security](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html)、[MCP Security](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html)、[LLM Prompt Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)：最小能力、明确目的地、工具门禁、将不可信文档与可执行指令分离。
- [Ollama Cloud 官方说明](https://github.com/ollama/ollama/blob/main/docs/cloud.mdx)：本地客户端的云模型能力与禁用方式。

## 2026-10-29 追加：私库的会话旁路与依赖修复

进一步对照真实调用链，确认了两处原先清单外的遗漏；裁决和反证见 `.bug-hunter/supplement-chat-dependencies-20261029.json`。这些是后续修复，不能倒推为之前已经安全。

- **SEC-004，会话间接暴露私人内容**：上游关闭 `ENABLE_ADMIN_CHAT_ACCESS` 只保护部分读接口，管理员仍能通过消息修改接口得到员工的完整 `ChatResponse`，或操作员工会话的分享授权。员工聊到的私人知识库内容会因此再次暴露。现对消息更新、删除、事件、分享授权、复制及既有会话写入统一使用原生内容权限；管理员与普通用户一样需拥有会话。任务列表与停止也按所属账号检查。明确分享的会话继续按实际分享 ACL 读取。应用管理员可以管理账号，不因此取得员工私聊正文。
- **SEC-005，兼容 API 旁路**：`/api/message`、`/api/v1/messages` 及裸 embeddings 路由没有经过原生能力检查，且 Anthropic 接口存在直接 provider passthrough。原生模式现在拒绝这些上游代理入口。Claude 等实际模型继续由 core 模型配置及统一 Agent 服务使用；没有以“禁止 Claude”代替模型授权。

新增实际函数体回归验证管理员被拒绝发生在返回正文、修改消息、发事件或写分享授权之前，并验证会话所有者仍可操作。更新后的安全组合测试 **129 passed、1 skipped**；跳过仍是 Windows 文件系统 symlink 权限。启动器、会话和 integration guard 的新冻结核心环境专项 **65 passed**。这些隔离测试没有读取或修改历史员工私聊，不能替代重启后浏览器验收。上游大文件仍只读取相关函数，仓库安全覆盖状态保持 `IN_PROGRESS`。

### Python 依赖：发现、修复与复验

使用固定 `pip-audit==2.10.1` 查询生产锁中的包名、版本；不向漏洞服务发送账号、提示词或文档。旧锁确实发现已知漏洞：核心的 Pillow；摄入侧的 certifi、filelock、Pillow、pypdf；检索侧另有 OpenTelemetry 与 PydanticAI；WebUI 的 aiohttp、Authlib、Black、Brotli、cryptography、MCP、OpenTelemetry、Pillow 和 PyJWT。Advisory 的别名或重复记录不等于同数量独立漏洞。

修复后本次查询结果：

| 锁/运行配置 | 查询包数 | 已知漏洞记录 | 未查询/跳过包 |
| --- | ---: | ---: | ---: |
| 核心 `uv.lock`（含官方 MCP SDK） | 89 | 0 | 0 |
| 摄入服务 `uv.lock` | 105 | 0 | 0 |
| 检索服务 `uv.lock` | 66 | 0 | 0 |
| 原生 WebUI 全部冻结依赖 | 174 | 0 | 0 |

机器可读结果是 `docs/acceptance/dependency-audit-2026-10-29.json`。完整 advisory 查询原文仅存在本机忽略目录。复现命令：

```powershell
./scripts/audit-dependencies.ps1
```

该脚本和 CI 分别覆盖四套生产包，不安装到正在运行的环境。发现新的已知问题时应失败；查询结果只代表当时公开数据库中的已知漏洞，不证明不存在漏洞，也不涵盖 JavaScript 依赖、Qdrant/Ollama 二进制或模型权重。

WebUI 上游的精确旧 pin 不能通过额外 `pip install` 叠加掩盖。现在 `scripts/webui-security-requirements.txt` 明确替换受影响 pin，`scripts/webui-requirements.lock` 冻结全部 174 包；`scripts/webui_dependencies.py` 核对固定上游、替换清单和锁一致后，启动器使用 `uv pip sync` 及 `uv pip check`，避免旧环境遗留与传递依赖漂移。此锁针对原生 Windows/Python 3.12 配置；没有声称 Linux 已一键交付。首次把 `pip sync` 错写成 `-r` 导致一次启动失败，已改为位置参数，并在独立环境真实运行同一命令验证成功。

MCP 升级至 2.x 后，实际 SDK 将 transport 名称改为 `streamable_http_client`、HTTP 客户端改为 `httpx2`，返回值改为两个 stream。上游适配器已按安装的实际 API 修正；在独立空 SQLite、真实 slim/native profile 中，整个 WebUI `main` 导入成功，`uv pip check` 174 包全部兼容。原生上游直连仍被 guard 拒绝；受统一政策审批的 core MCP 能力由对应功能另行验收。

FastEmbed 0.8.1/0.9.0 仍声明 Pillow `<13`，但 [Pillow 13.0.0 官方安全修复](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html) 已发布；[FastEmbed 上游约束](https://github.com/qdrant/fastembed/blob/main/pyproject.toml) 尚未放宽。因此三套 uv 项目显式使用有注释的临时 Pillow 13 override，不能声称完全遵循上游兼容矩阵。删除条件是 FastEmbed 正式允许安全版本，并在升级后保持回归通过。

已做的兼容验证：

- 更新依赖后的独立冻结摄入环境：**389 passed**；检索环境：**179 passed**。
- 核心 FastEmbed 0.9.0，以及摄入/检索的 0.8.1，分别实际运行已缓存 BM42 的文档和查询 embedding，均非空且有共同索引。
- 生成的合成图片分别执行 PNG/JPEG/TIFF/BMP/WEBP 保存、加载、EXIF 转正、缩放以及 PDF 编码；Pillow 13 均成功。可用 `scripts/verify_pillow_compat.py --cache-dir <本机FastEmbed缓存目录>` 重现；三个非敏感 JSON 报告位于本目录。

这些路径没有做恶意图片 fuzzing，也不证明 FastEmbed 的所有图像模型均兼容 Pillow 13。真实用户上传文件、知识库更新、鉴权和问答需要在升级后运行版本单独检查。

### 开源交付检查的边界

本次对当时 Git 追踪/待追踪且未忽略的 **447 份候选文件** 做了有限模式检查：私钥头、常见 provider token 格式与已知本机验收密码均无命中；不等同于完整历史密钥扫描或熵分析。运行目录、上传文件、向量数据、凭据及日志继续被忽略；另排除 `.claude/scheduled_tasks.lock` 和 `.qdrant-initialized` 等机器状态。

现有三套维护基座保留其 MPL 许可；生成的 Open WebUI 继续受固定上游许可和品牌条款约束，见 `patches/open-webui/NOTICE.md`。旧 AarhusAI 编排补丁没有取得可再分发许可，已由独立桥接实现替换，不将本地备份列为交付。尚未完成示例数据、全部传递依赖和模型权重的完整许可证法律审查。

本地 `scripts/check-code.ps1` 已补 lock、ruff、实际 WebUI patch 准备和核心测试，可选择 Svelte/RAG 服务检查；CI 先生成 WebUI 再执行依赖源码的安全契约测试，并编译所有补丁中的 Svelte 组件。本地已编译 46 个补丁组件。CI 配置修改不等于远端 CI 已运行或通过；仍未提交、推送或发布。

## 2026-11-05 追加：交付前复扫

对象是当前主干；范围是依赖漏洞、静态安全规则和历史密钥模式，不含渗透测试。

### 依赖

| 范围 | 工具 | 结果 |
| --- | --- | --- |
| Python：核心、摄入、检索、WebUI 后端 | `pip-audit`（`scripts/audit-dependencies.ps1`） | 4 套锁均无已知漏洞 |
| 前端生产依赖 | `npm audit --omit=dev` | 修复前 32 项（1 critical、19 high），修复后 17 项（0 critical、4 high） |

前端修复方式是 `npm audit fix`（只做非破坏性升级，不加 `--force`），并按 SheetJS 官方文档把 `xlsx` 从已停止更新的 npm 0.18.5 换成其 CDN 发布的 0.20.3，解决原型污染与 ReDoS。改动保存在 `patches/open-webui/v0.11.4/nexrag/security-npm.diff`，仅含 `package.json` 与 `package-lock.json`，和其他补丁一样可从固定上游提交重放；验证了 `npm ci`、54 个补丁 Svelte 组件编译与 `xlsx` 实际加载。

剩余 4 项 high 需要主版本升级或没有修复版本，未强行处理，原因如下：

| 包 | 问题 | 为什么不修 |
| --- | --- | --- |
| `vite` 5.4.21 | 开发服务器路径穿越与 `launch-editor`（`server.fs` 绕过） | 修复版本需要升级到 vite 6 以上，随之要升级 SvelteKit 3，属于上游框架的破坏性升级。只影响开发服务器，而本项目的启动器只在 `127.0.0.1` 上运行它 |
| `sharp`、`@huggingface/transformers`、`kokoro-js` | libvips、libheif、librsvg 的原生库漏洞 | 无修复版本。仅被浏览器端 Kokoro TTS 的 worker 使用；朗读功能已按用户决定取消，且这些包运行在用户自己的浏览器中，不处理服务器端上传 |

### 代码

对 `src/` 与 `scripts/` 运行 `ruff --select S`（Bandit 规则），修复前 61 条、修复后 55 条。数量列写作“前 → 后”的是有变化的规则；S608 少掉的 2 条来自已删除的 `experience.py`。逐类处理如下：

| 规则 | 数量 | 处理 |
| --- | ---: | --- |
| S314 `xml` 解析不可信数据 | 1 | **已修复**：`src/rag/document_reader.py` 解析上传 DOCX 改用 `defusedxml`，拒绝实体膨胀与外部实体，并新增回归测试 |
| S104 绑定所有接口 | 1 | **已修复**：`HOST` 默认值由 `0.0.0.0` 改为 `127.0.0.1`；启动器本来就固定回环地址，这里避免直接运行 `python -m src.main` 时意外对外暴露 |
| S101 `assert` | 22 → 20 | **已修复 2 处**：运行路径上的 `assert` 在 `python -O` 下会被去掉，改为显式异常。其余 `assert` 都在 `scripts/verify_*.py` 验收脚本里，属于预期用法 |
| S608 字符串拼接 SQL | 25 → 23 | **逐条核对，无注入**：拼接的只有占位符个数（`?,?,?`）、模块常量列清单（`memory/store.py` 的 `COLUMNS`）、代码内固定的子句，或已用 `^[A-Za-z_][A-Za-z0-9_]*$` 校验过的库表列名；所有取值都走参数绑定。`set_status(**fields)` 的列名来自调用方的关键字参数名，调用点均在代码内固定，不接受外部输入。`agent/grounding.py` 一条是误报（拼的是给模型的提示文本，不是 SQL）。业务库查询另有 SQL AST 白名单和只读快照约束 |
| S310 `urlopen` | 5 | 不修：URL 来自启动器内置的固定下载源与回环 Ollama 地址，不接受用户输入 |
| S603/S607 `subprocess` | 5 + 2 | 不修：命令是固定列表，不经过 shell，不拼接用户输入（文件夹选择器、启动器管理的子进程） |

### 历史密钥

对全部 Git 历史搜索私钥头、`sk-`、`AKIA`、`ghp_` 等常见格式，无命中。不等同于熵分析，也不覆盖本机未提交的 `.env`（已被 `.gitignore` 排除）。

## 2026-11-07 发布后复扫

基线为远程 `main` 的 `95714bf`。本轮覆盖固定依赖、Git 历史和高风险调用链，并对修改处运行回归。历史验收结果保留在上文。

| 范围 | 复扫结果 |
| --- | --- |
| Python 五套锁文件 | 核心、摄入、检索、搜索、WebUI 后端均未命中已知漏洞。原先遗漏的可选搜索服务由 12 个包命中 61 条记录降到零，搜索锁已加入 CI 门禁。 |
| npm 生产锁 | 移除无用适配器前 15 个条目，其中 3 个 high 来自同一条 `kokoro-js → @huggingface/transformers → sharp` 依赖链，0 个 critical。项目保留可选浏览器朗读，原生运行默认不自动朗读。npm 未给出该链的可用修复。 |
| npm 全锁 | 最终 25 个条目，3 个 high、0 个 critical。移除未使用的 Cypress、Vitest、自动和 Node 部署适配器后，Cypress 解压链告警消除。仅保留可选朗读的高危链。 |
| Git 历史 | Gitleaks v8.30.1 扫描 `origin/main` 全部 13 个提交。4 处命中均核对为测试夹具，未发现实际凭据。 |

Node 更新至 22.23.4，启动器按官方 SHA-256 校验回退下载。Vite 6.4.5 消除了开发服务相关告警，未使用的 Vitest 与 Cypress 已从最终交付依赖中移除。新增高危或任何严重公告会使发布门禁失败，例外按包名和三条已审核公告精确匹配。见 [Node 发布说明](https://nodejs.org/en/blog/release/v22.23.4) 和 [Vite 发布说明](https://github.com/vitejs/vite/releases/tag/v6.4.5)。

可选搜索服务的网页补全文本以前先检查 DNS，再把原域名交给 HTTP 客户端连接，两次解析之间可被重绑定。现在每一跳都连接到通过校验的数值 IP，沿用原 Host 和 TLS SNI，拒绝内网、回环、保留地址和恶意重定向，网页抓取与搜索提供商连接使用不同客户端。回归验证了固定 IP、端口、SNI 和拒绝内网跳转。见 [HTTPX 的 SNI 扩展](https://www.python-httpx.org/advanced/extensions/#sni_hostname) 与 [OWASP SSRF 防护](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)。

运营 API 移除了旧知识库面板专用的三个路由。管理员权限依赖独立维护，业务租户管理按原路径保留。反馈按回答保存最近一票，防止重复请求放大灰度评分。RSI 的验证集隔离、事务状态转换及指定版本回滚见 [评测与 RSI](../evaluation-and-rsi.md)。

### 发布前追加检查

启用仓库依赖告警后，确认搜索服务的开发依赖 filelock、msgpack 和 pip 仍有已知漏洞。审计已扩大为 uv 的全部可选依赖与开发组，修正三项锁定版本。v1.1.0 标签的发布被取消，最终交付使用 v1.1.1。npm 最终高危例外仍限定为可选浏览器朗读的三条公告。
