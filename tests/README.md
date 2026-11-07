# 测试导航

根目录 `tests/` 验证 NexRAG 的 Agent、接口和集成边界；基座测试保留在各服务的 `tests/` 内。测试文件按行为命名，同一行为的成功、拒绝和边界用例放在一起。共享环境和清理逻辑见 `conftest.py`，临时文件使用 `tmp_path`，不读取真实业务库。

| 范围 | 主要用例 |
| --- | --- |
| 回答与证据 | `test_answer_contract*.py`、`test_grounding_boundary.py`、`test_limited_detail_contract.py`、`test_evidence_arithmetic.py` |
| 业务数据与权限 | `test_business_*.py`、`test_readonly_boundary.py`、`test_registered_metrics.py`、`test_postgresql_source.py` |
| 知识、账号与工具桥 | `test_knowledge_*.py`、`test_native_*.py`、`test_mcp_*.py`、`test_private_content_boundary.py` |
| 记忆与文件 | `test_memory_*.py`、`test_file_sandbox.py`、`test_exports.py` |
| 评测与 RSI | `test_evaluation_rules.py`、`test_evolution_*.py`、`test_agent_lifecycle_acceptance.py` |
| 源码交付与启动 | `test_source_delivery.py`、`test_local_launcher.py`、`test_search_configuration.py` |

从仓库根目录运行：

```powershell
uv sync --frozen --group dev --python 3.12
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check-code.ps1
# 加上基座回归与前端构建：
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check-code.ps1 -IncludeRagServices -IncludeWebUI
```

部分隐私边界测试直接加载已打补丁的 WebUI 函数，上述脚本会先准备固定上游检出。单独运行 `uv run pytest tests/test_evolution_gate.py -q` 等用例前，也应按需运行 `scripts/prepare-webui.ps1`。

PostgreSQL 用例需要专用测试 DSN，Windows 原生符号链接需要相应权限；本机缺少条件时会跳过，CI 单独要求链接用例实际通过。具体条件见对应测试文件和 [发布门禁](../docs/release-gates.md)。用例通过与实际模型、网页业务验收分别记录，评测种子及历史输出回放见 [evals](../evals/README.md)。
