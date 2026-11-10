# API 路由审计

路由审计共核对 **71** 个应用路由：`src/` **58** 个（含 `/health`），三个 `base/` 服务合计 **13** 个；全部找到调用、测试或文档引用，无无依据的孤立路由。此处不计框架自动生成的 OpenAPI/docs 端点。

以下仅有测试/文档引用的接口有意保留；相同路径在不同服务中分别计数（审计清单称“9 个”，逐项实际列出 10 个路由）：

| 服务 | 方法与路径 | 保留原因 |
| --- | --- | --- |
| src | GET `/v1/models` | OpenAI 兼容、公开 HTTP 契约，供外部客户端使用 |
| src | POST `/v1/chat/completions` | OpenAI 兼容、公开 HTTP 契约 |
| src | POST `/v1/business/query` | 文档化的直接业务查询 HTTP 契约 |
| ingestion | GET `/health` | 部署健康探针 |
| ingestion | GET `/metrics` | Prometheus 抓取端点 |
| retrieval | GET `/health` | 部署健康探针 |
| retrieval | GET `/metrics` | Prometheus 抓取端点 |
| ingestion | GET `/api/v1/documents/{file_id}/chunks` | `ENABLE_INSPECTION_API` 控制的开发检查 API |
| search-agent | GET `/health`、POST `/api/v1/search` | 可选独立搜索基座的一组健康/搜索接口（见下） |

计数说明：上表最后一行含两个路由，表中合计为 **10** 个 method/path/service 组合；71 个总路由按独立路由计数。

`base/search-agent` 默认不部署；目前无仓库内消费者，因为网页搜索由 `src/agent/tools.py` 直接调用 SearXNG。保留它供可选独立使用，不应因缺少默认调用方而删除。
