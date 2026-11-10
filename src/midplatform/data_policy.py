"""Default-deny egress policy shared by chat and tools.

Authorization determines readable records; additionally control where their contents may
be processed. Provider labels alone do not prove local execution.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import httpx

from src.midplatform.ops_store import get_ops_store, now

CLASSIFICATIONS = {"public", "internal", "secret"}


def ensure_policy_schema():
    """Ensure resource and account-egress tables exist and return operations storage."""
    store = get_ops_store()
    with store.transaction() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS data_policies (
            kind TEXT NOT NULL, resource_id TEXT NOT NULL, classification TEXT NOT NULL,
            external_models INTEGER NOT NULL DEFAULT 0, external_search INTEGER NOT NULL DEFAULT 0,
            external_tools INTEGER NOT NULL DEFAULT 0, changed_by TEXT NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(kind, resource_id)
        );
        CREATE TABLE IF NOT EXISTS account_egress (
            email TEXT PRIMARY KEY, config TEXT NOT NULL, changed_by TEXT NOT NULL, updated_at REAL NOT NULL
        );
        """)
    return store


def is_loopback_endpoint(url: str) -> bool:
    """Accept credential-free localhost or loopback-IP endpoints only."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return False
    if parsed.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        return False


def model_is_local(model: dict) -> bool:
    """Require a loopback Ollama or custom endpoint and noncloud model name."""
    name = model.get("model_name", "").lower()
    return (
        model.get("provider") in {"ollama", "custom"}
        and ":cloud" not in name
        and not name.endswith("-cloud")
        and is_loopback_endpoint(model.get("base_url", ""))
    )


def resource_policy(kind: str, identifier: str) -> dict:
    """Read resource policy, defaulting unknown resources to internal with no egress."""
    row = ensure_policy_schema().query_one(
        "SELECT * FROM data_policies WHERE kind=? AND resource_id=?", (kind, identifier)
    )
    return row or dict(
        kind=kind,
        resource_id=identifier,
        classification="internal",
        external_models=False,
        external_search=False,
        external_tools=False,
    )


def save_resource_policy(
    kind: str,
    identifier: str,
    classification: str,
    external_models: bool,
    external_search: bool,
    external_tools: bool,
    changed_by: str,
):
    """Save classification and egress flags; secrets cannot leave and only public data permits
    search.
    """
    if kind not in {"tenant", "knowledge"} or classification not in CLASSIFICATIONS:
        raise ValueError("无效的数据类型或分级")
    if classification == "secret" and any((external_models, external_search, external_tools)):
        raise ValueError("核心机密禁止外部模型、搜索和工具")
    if classification != "public" and external_search:
        raise ValueError("仅公开数据允许对外搜索；内部数据应先整理成独立的公开资料")
    ensure_policy_schema().execute(
        "INSERT OR REPLACE INTO data_policies VALUES (?,?,?,?,?,?,?,?)",
        (
            kind,
            identifier,
            classification,
            int(external_models),
            int(external_search),
            int(external_tools),
            changed_by,
            now(),
        ),
    )


def account_policy(email: str) -> dict:
    """Read case-insensitive account egress grants, defaulting to none."""
    row = ensure_policy_schema().query_one(
        "SELECT config FROM account_egress WHERE email=?", (email.lower(),)
    )
    return json.loads(row["config"]) if row else {}


def save_account_policy(email: str, config: dict, changed_by: str):
    """Replace normalized account-egress grants and record actor and time."""
    ensure_policy_schema().execute(
        "INSERT OR REPLACE INTO account_egress VALUES (?,?,?,?)",
        (email.lower(), json.dumps(config), changed_by, now()),
    )


@dataclass(frozen=True)
class DataAccessPolicy:
    """Effective intersection of account grants and resource egress restrictions."""

    classification: str = "internal"
    external_models: bool = False
    external_search: bool = False
    external_tools: bool = False
    allowed_model_ids: tuple[str, ...] = ()
    allowed_tool_ids: tuple[str, ...] = ()

    def to_dict(self):
        """Convert policy to a plain dictionary."""
        return asdict(self)

    def check_model(self, model: dict):
        """Require model distribution and enforce HTTPS and explicit egress grants for remote
        models.
        """
        if self.allowed_model_ids and model["id"] not in self.allowed_model_ids:
            raise ValueError("当前账号未获授权使用此模型")
        if not model_is_local(model) and (
            self.classification == "secret" or not self.external_models
        ):
            raise ValueError("本次数据仅允许本机模型处理，请选择本机 Ollama 模型")
        if not model_is_local(model) and model["id"] not in self.allowed_model_ids:
            raise ValueError("此外部模型尚未列入当前账号的明确授权清单")
        if not model_is_local(model) and urlsplit(model["base_url"]).scheme != "https":
            raise ValueError("外部模型必须使用 HTTPS 接口")


def effective_policy(
    email: str, tenant_id: str, knowledge_ids, knowledge_labels=None
) -> DataAccessPolicy:
    """Intersect account approval with every selected readable resource.

    Include all readable knowledge libraries because automatic retrieval may use any of them.
    Unknown classifications are internal and never implicitly enable egress.
    """
    resources = [resource_policy("tenant", tenant_id)] if tenant_id else []
    resources += [
        (knowledge_labels or {}).get(identifier) or resource_policy("knowledge", identifier)
        for identifier in knowledge_ids
    ]
    account = account_policy(email)
    rank = {"public": 0, "internal": 1, "secret": 2}
    classification = max(
        (row["classification"] for row in resources), key=rank.get, default="internal"
    )

    def allowed(capability):
        """Require account and resource approval for this egress capability."""
        return (
            classification != "secret"
            and bool(account.get(capability))
            and all(row.get(capability, False) for row in resources)
        )

    return DataAccessPolicy(
        classification,
        allowed("external_models"),
        classification == "public" and allowed("external_search"),
        allowed("external_tools"),
        tuple(account.get("allowed_model_ids", [])),
        tuple(account.get("allowed_tool_ids", [])),
    )


async def refresh_knowledge_authorization(context):
    """Refresh knowledge ACLs before tools or models, failing closed on bridge errors."""
    if not getattr(context, "enforce_live_authorization", False):
        return
    from src.midplatform.workspace import get_setting

    if (
        getattr(context, "tenant_id", "")
        and getattr(context, "business_source_generation", None) is not None
    ):
        if context.business_source_generation != get_setting("business_source_generation", 0):
            raise ValueError("业务数据源已经切换，请重新开始对话并确认授权")
    from src.config import settings

    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(
            settings.WEBUI_TOOL_BRIDGE_URL.rstrip("/") + "/api/v1/nexrag/authorization/check",
            headers={"Authorization": f"Bearer {settings.NEXRAG_SHARED_API_KEY}"},
            json={
                "user_id": context.user_id,
                "attachment_ids": [
                    scope[5:] for scope in context.knowledge_scopes if scope.startswith("file-")
                ],
            },
        )
    if response.status_code != 200:
        raise ValueError("账号或知识库授权检查失败，请重新登录或联系管理员")
    snapshot = response.json()
    expected_version = getattr(context, "session_version", None)
    if expected_version is not None and snapshot["session_version"] != expected_version:
        raise ValueError("账号凭据已更新，本轮旧会话已撤销，请重新登录")
    previous = set(context.knowledge_scopes)
    current = set(snapshot["knowledge_scopes"])
    ranks = {"public": 0, "internal": 1, "secret": 2}
    tightened = any(
        ranks[snapshot["knowledge_labels"][identifier]["classification"]]
        > ranks[
            (getattr(context, "knowledge_labels", {}) or {})
            .get(identifier, {})
            .get("classification", "internal")
        ]
        for identifier in previous & current
    )
    if previous - current or tightened:
        raise ValueError("本轮知识库权限或内容分级已变更，请重新开始对话，避免继续使用旧上下文")
    context.knowledge_scopes = snapshot["knowledge_scopes"]
    context.knowledge_labels = snapshot["knowledge_labels"]
    context.role = snapshot["role"]
    context.email = snapshot["email"]
    context.session_version = snapshot.get("session_version", expected_version)
