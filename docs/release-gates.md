# 发布门禁

Release 按标签发布可复现源码，支持 Windows x64 本地运行。用户可下载源码 ZIP 或 clone 标签，运行 `start.cmd`。首次需要网络、依赖与模型下载空间。安全使用边界见 [SECURITY.md](../SECURITY.md)。

## 必须通过的检查

1. 项目版本、API 版本、Git 标签和 CHANGELOG 对应，项目 Markdown 的本地链接有效，交付文件不含运行数据或凭据。
2. 四套 uv 项目的锁与配置一致，WebUI 后端使用独立固定依赖清单。Ruff 检查与格式检查通过，交付代码单文件不超过 1,000 行。
3. 固定上游提交可顺序重放四个 WebUI 补丁，组件编译和完整前端构建通过。
4. 核心、摄入、检索、可选搜索服务回归通过。Windows 文件链接边界用例在 CI 中通过。
5. 五套 Python 锁没有已知漏洞。完整 npm 锁不得新增高危或出现严重公告，已审核的可选朗读例外精确到公告 URL。
6. 发布前检查 Git 历史与候选源码的密钥，核对许可、版权、品牌与示例数据说明。

```powershell
uv sync --frozen --group dev --python 3.12
.\scripts\check-code.ps1 -IncludeWebUI -IncludeRagServices
.\scripts\audit-dependencies.ps1
node scripts/audit-webui.mjs
uv run python scripts/check-repository.py --release-tag v1.0.0
```

## 发布流程

先推送评审分支并等待 PR 的 CI 成功，再合并到 `main`。发布标签指向合并后的提交。标签工作流复用同一份 CI，全部通过后提取 CHANGELOG、生成源码 ZIP 和 SHA-256，再发布 GitHub Release。工作流失败时保留诊断，修正后重新验证。

发布说明列出启动步骤、实际验证范围、API 或配置迁移、许可和仍存在的限制。源码测试、服务健康、网页业务验收及长期运行效果分别记录。最新功能证据见 [逐项验收](acceptance/full-functional-acceptance.md)。
