"""服务端持有的单一 NexRAG agent 模型注册表，不做服务路由。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field, SecretStr, field_validator

from src.config import settings
from src.midplatform.ops_store import get_ops_store

DEFAULT_ID = "nexrag-model-default"
PROTOCOLS = {"openai", "responses", "anthropic", "google"}
PROVIDERS = {"ollama", "openai", "deepseek", "anthropic", "google", "custom"}


class ModelForm(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str
    protocol: str = "openai"
    model_name: str = Field(min_length=1, max_length=150)
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr | None = None
    clear_api_key: bool = False
    enabled: bool = True
    audience: str = Field(default="all", pattern="^(all|selected)$")
    allowed_user_ids: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("allowed_user_ids")
    @classmethod
    def valid_account_ids(cls, values):
        """账号标识不得为空白或超过 128 字符；去除首尾空白、去重并排序。"""
        if any(not value.strip() or len(value) > 128 for value in values):
            raise ValueError("模型使用账号标识无效")
        return sorted({value.strip() for value in values})

    @field_validator("provider")
    @classmethod
    def provider_supported(cls, value):
        """厂商必须在受支持的集合内。"""
        if value not in PROVIDERS:
            raise ValueError("不支持的模型厂商")
        return value

    @field_validator("protocol")
    @classmethod
    def protocol_supported(cls, value):
        """接口协议必须在受支持的集合内。"""
        if value not in PROTOCOLS:
            raise ValueError("不支持的接口协议")
        return value

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        """接口地址须为 HTTP(S)，且不含凭据、查询参数或片段；去除首尾空白与末尾斜杠。"""
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("接口地址须为 HTTP(S) 地址，不包含凭据、查询参数或片段")
        return value.strip().rstrip("/")


def _cipher():
    secret = settings.NEXRAG_USER_JWT_SECRET
    if not secret or len(secret) < 32:
        raise ValueError("服务端密钥未配置，不能保存模型凭据")
    key = hashlib.sha256(("nexrag-model-credentials:" + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _ensure():
    store = get_ops_store()
    with store.transaction() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_models (id TEXT PRIMARY KEY, config TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_model_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        seeded = conn.execute(
            "SELECT 1 FROM agent_model_settings WHERE key='initialized'"
        ).fetchone()
        if not seeded:
            record = dict(
                name=settings.LLM_MODEL_NAME,
                provider="ollama" if "11434" in settings.LLM_API_BASE else "custom",
                protocol="openai",
                model_name=settings.LLM_MODEL_NAME,
                base_url=settings.LLM_API_BASE.rstrip("/"),
                enabled=True,
                encrypted_key="",
                environment_key=True,
            )
            conn.execute(
                "INSERT OR IGNORE INTO agent_models VALUES (?,?)", (DEFAULT_ID, json.dumps(record))
            )
            conn.execute("INSERT INTO agent_model_settings VALUES ('default',?)", (DEFAULT_ID,))
            conn.execute("INSERT INTO agent_model_settings VALUES ('initialized','1')")
    return store


def records():
    """返回全部已登记模型的完整记录（含加密密钥字段，仅限服务端内部使用，对外须经 public_model）。"""
    return [
        {**json.loads(row["config"]), "id": row["id"]}
        for row in _ensure().query("SELECT * FROM agent_models ORDER BY rowid")
    ]


def default_id():
    """返回当前系统默认模型的 ID。"""
    return _ensure().query_one("SELECT value FROM agent_model_settings WHERE key='default'")[
        "value"
    ]


def resolve_model(identifier: str | None = None, *, enabled_only=True):
    """按 ID 取模型记录；ID 为空或 "NexRAG" 时取系统默认模型。

    模型不存在（或 enabled_only 时已停用）抛出 ValueError。不检查调用账号是否有权使用。
    """
    identifier = default_id() if identifier in {None, "", "NexRAG"} else identifier
    row = _ensure().query_one("SELECT * FROM agent_models WHERE id=?", (identifier,))
    if not row or (enabled_only and not json.loads(row["config"])["enabled"]):
        raise ValueError("模型不存在或已停用，请选择可用模型")
    record = {**json.loads(row["config"]), "id": row["id"]}
    managed = os.environ.get("NEXRAG_MANAGED_OLLAMA_API_BASE")
    parsed = urlsplit(record["base_url"])
    if (
        managed
        and record.get("provider") == "ollama"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        and parsed.port in {11434, 11435}
    ):
        # 只有原生启动器确认云端推理已禁用后才会提供此 URL。
        # 不要改写已持久化的 provider 凭据。
        record["base_url"] = managed.rstrip("/")
    return record


def model_available(record, principal):
    """模型的分发范围与数据外发权限是两回事。"""
    return bool(record.get("enabled")) and (
        principal.role == "admin"
        or record.get("audience", "all") == "all"
        or principal.user_id in record.get("allowed_user_ids", [])
    )


def require_model_access(record, principal):
    """要求该模型已启用且分发给当前账号（管理员除外的受众限制），否则抛出 ValueError；通过则原样返回记录。"""
    if not model_available(record, principal):
        raise ValueError("此模型未分发给当前账号或使用权限已撤销，请选择其他模型")
    return record


def key_for(record):
    """取出模型的明文 API 密钥：环境变量托管的模型用配置中的密钥，否则解密已存储的密文；无法解密抛出 ValueError。仅限服务端内部使用。"""
    if record.get("environment_key"):
        return settings.LLM_API_KEY
    try:
        return (
            _cipher().decrypt(record["encrypted_key"].encode()).decode()
            if record.get("encrypted_key")
            else ""
        )
    except InvalidToken as exc:
        raise ValueError("模型密钥无法解密，请管理员重新填写") from exc


def public_model(record, *, admin=False):
    """返回模型的对外视图，永不含密钥；admin 为真时才附带受众、可用账号、接口地址和是否已设置密钥。"""
    item = {
        key: record[key] for key in ("id", "name", "provider", "protocol", "model_name", "enabled")
    }
    item["is_default"] = record["id"] == default_id()
    if admin:
        item["audience"] = record.get("audience", "all")
        item["allowed_user_ids"] = record.get("allowed_user_ids", [])
        item["base_url"] = record["base_url"]
        item["has_api_key"] = bool(
            record.get("encrypted_key") or (record.get("environment_key") and settings.LLM_API_KEY)
        )
    return item


def save_model(form: ModelForm, identifier=None):
    """新增模型（identifier 为空）或更新已有模型，密钥加密保存，返回管理员视图。

    指定账号必须已在组织目录登记；更换厂商或接口地址时须重新填写或明确清除旧密钥；
    默认模型不能被停用或限定受众。违反上述规则抛出 ValueError。
    """
    store = _ensure()
    previous = resolve_model(identifier, enabled_only=False) if identifier else {}
    identifier = identifier or "nexrag-model-" + uuid4().hex
    if form.audience == "selected" and form.allowed_user_ids:
        from src.midplatform.directory import store as directory_store

        registered = {
            row["user_id"]
            for row in directory_store().query("SELECT user_id FROM company_accounts")
        }
        if set(form.allowed_user_ids) - registered:
            raise ValueError("模型分发包含不存在的账号，请刷新组织目录后重试")
    supplied = form.api_key.get_secret_value().strip() if form.api_key else ""
    target_changed = previous and (
        form.base_url != previous["base_url"] or form.provider != previous["provider"]
    )
    if (
        target_changed
        and (previous.get("encrypted_key") or previous.get("environment_key"))
        and not supplied
        and not form.clear_api_key
    ):
        raise ValueError("更换厂商或接口时，请重新填写密钥或明确清除旧密钥")
    record = form.model_dump(exclude={"api_key", "clear_api_key"})
    record.update(
        encrypted_key=previous.get("encrypted_key", ""),
        environment_key=previous.get("environment_key", False),
    )
    if form.clear_api_key or supplied:
        record.update(
            encrypted_key=_cipher().encrypt(supplied.encode()).decode() if supplied else "",
            environment_key=False,
        )
    with store.transaction() as conn:
        # 在写事务内重新检查：默认模型必须始终可用。
        current_default = conn.execute(
            "SELECT value FROM agent_model_settings WHERE key='default'"
        ).fetchone()[0]
        if identifier == current_default and not form.enabled:
            raise ValueError("请先选择另一个默认模型，再停用此模型")
        if identifier == current_default and form.audience != "all":
            raise ValueError("系统默认模型须对全体账号可用，请先更换默认模型")
        conn.execute(
            "INSERT OR REPLACE INTO agent_models VALUES (?,?)", (identifier, json.dumps(record))
        )
    return public_model({**record, "id": identifier}, admin=True)


def set_default(identifier):
    """把指定模型设为系统默认；该模型须存在、已启用且对全体账号可用，否则抛出 ValueError。"""
    store = _ensure()
    with store.transaction() as conn:
        row = conn.execute("SELECT config FROM agent_models WHERE id=?", (identifier,)).fetchone()
        if not row or not json.loads(row[0])["enabled"]:
            raise ValueError("默认模型必须存在且已启用")
        if json.loads(row[0]).get("audience", "all") != "all":
            raise ValueError("系统默认模型须对全体账号可用")
        conn.execute("UPDATE agent_model_settings SET value=? WHERE key='default'", (identifier,))


def deepseek_profile(model_name):
    """借助 SDK 已有的 V4 能力解析当前的 Flash 别名。"""
    from pydantic_ai.providers.deepseek import DeepSeekProvider

    profile_name = "deepseek-v4-flash" if model_name == "deepseek-flash" else model_name
    return DeepSeekProvider.model_profile(profile_name)


def build_model(record):
    """使用各 provider 的原生协议，同时共用一个 Agent/工具循环。"""
    key = key_for(record)
    if record["protocol"] == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return AnthropicModel(
            record["model_name"],
            provider=AnthropicProvider(api_key=key, base_url=record["base_url"]),
        )
    if record["protocol"] == "google":
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleModel(
            record["model_name"], provider=GoogleProvider(api_key=key, base_url=record["base_url"])
        )
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
    from pydantic_ai.profiles.openai import OpenAIModelProfile, openai_model_profile
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key=key or "local-no-key", base_url=record["base_url"])
    profile = openai_model_profile(record["model_name"])
    if record["provider"] == "deepseek":
        from openai import AsyncOpenAI
        from pydantic_ai.providers.deepseek import DeepSeekProvider

        provider = DeepSeekProvider(
            openai_client=AsyncOpenAI(api_key=key, base_url=record["base_url"])
        )
        # DeepSeek 当前的 Flash 别名是 V4 系列 thinking 模型。锁定版本的 SDK 按前缀
        # 识别该系列的强制工具调用限制，而 HTTP 模型 ID 必须保持为目录中的真实 ID。
        profile = deepseek_profile(record["model_name"]) or profile
    if record["protocol"] == "responses":
        return OpenAIResponsesModel(record["model_name"], provider=provider)
    return OpenAIChatModel(
        record["model_name"],
        provider=provider,
        profile=OpenAIModelProfile(**{**profile, "openai_supports_strict_tool_definition": False}),
    )
