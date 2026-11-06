"""NexRAG 服务的全局配置与环境变量设置。"""

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量和 .env 加载的运行时配置。"""

    # 共享的 .env 还包含其他原生服务的设置；
    # 校验 NexRAG 自身配置时忽略这些键。
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # API 服务设置。
    PROJECT_NAME: str = "NexRAG"
    API_V1_STR: str = "/v1"
    PORT: int = 8000
    # 默认只监听本机回环；需要对外暴露时由部署者显式设置 HOST。
    HOST: str = "127.0.0.1"

    # LLM 设置：OpenAI 兼容的托管或本地服务商。
    LLM_API_BASE: str = "http://localhost:11434/v1"
    LLM_API_KEY: str = "ollama"
    LLM_MODEL_NAME: str = "qwen3:4b-instruct"
    AGENT_TIMEOUT_SECONDS: int = 90
    WEBUI_TOOL_BRIDGE_URL: str = "http://127.0.0.1:8080"

    # 知识检索委托给现有的 AarhusAI 服务。
    # 本进程从不做文本嵌入，也不直接访问 Qdrant；
    # scripts/ingest_documents.py 读取自己的 embedding/Qdrant 设置。
    RETRIEVAL_API_BASE_URL: str = "http://localhost:8002"
    RETRIEVAL_API_KEY: Optional[str] = None
    RETRIEVAL_TIMEOUT_SECONDS: float = 45.0
    RETRIEVAL_MIN_RELEVANCE: float = 0.35
    MAX_TOOL_RETRIES: int = 2
    KNOWLEDGE_COLLECTION_NAME: str = "nexrag_docs"

    # 由管理员维护的共享知识库：NexRAG 把上传/删除转发给现有的
    # AarhusAI 摄取服务，不自行实现分块与嵌入。
    INGESTION_API_BASE_URL: str = "http://localhost:8001"
    INGESTION_API_KEY: Optional[str] = None
    INGESTION_TIMEOUT_SECONDS: float = 120.0

    # 外部 Web 搜索设置（SearXNG 或其他外部引擎）。
    ENABLE_WEB_SEARCH: bool = True
    SEARXNG_URL: str = ""
    WEB_SEARCH_TIMEOUT_SECONDS: float = 8.0

    # Olist 是内置示例；替换这些文件即可接入其他业务数据库，
    # 无需改动 NexRAG 的 agent 或查询代码。
    BUSINESS_SOURCE_PATH: str = "examples/olist/business-source.yaml"
    BUSINESS_DB_PATH: Optional[str] = None
    BUSINESS_DB_DSN: Optional[str] = None
    METADATA_PATH: str = "examples/olist/metadata.yaml"
    TENANT_ACCESS_PATH: str = "examples/olist/tenant-access.yaml"

    # Open WebUI 用此共享密钥对转发的用户身份签名。
    # 两个密钥都没有开发默认值；缺失时数据路由默认拒绝（fail closed）。
    NEXRAG_SHARED_API_KEY: Optional[str] = None
    NEXRAG_USER_JWT_SECRET: Optional[str] = None
    NEXRAG_USER_JWT_HEADER: str = "X-OpenWebUI-User-Jwt"

    # 只有浏览器直连 NexRAG 时才需要 CORS（如本地开发访问
    # http://localhost:3000）；Open WebUI 后端是服务端到服务端调用，不经过此检查。
    # 取值为逗号分隔的允许来源列表，默认是 Open WebUI 的本地开发来源而非 "*"，
    # 避免任意站点的带凭据跨域请求被反射放行。
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    # inspect_local_file 被限制在此根目录内。默认使用专用子目录而非进程工作目录，
    # 使沙箱无法触及应用源码或配置文件。
    SANDBOX_ROOT_DIR: str = "data/sandbox_uploads"

    # 原生 NexRAG 与 Web 后端共用的根目录。用户绑定一个本地文件夹，
    # 生成的报告位于其下按账户/对话划分的子目录中。
    EXPORTS_DIR: str = "data/artifacts"

    # 运维数据域：trace、评分、数据集、产物、提案和记忆元数据。
    # 可写，且与业务数据库分离。
    OPS_DB_PATH: str = "data/ops/ops.db"
    ENABLE_TRACING: bool = True
    # "redacted" 在存储提示词/回答前先遮蔽 PII；"none" 只保留结构和评分；
    # "full" 存储原始文本（仅限本地调试）。
    TRACE_STORE_CONTENT: str = "redacted"
    TRACE_RETENTION_DAYS: int = 30
    # 服务端用于对 trace 中用户 ID 做哈希的密钥；未设置时回退到 JWT 密钥，
    # 保证 trace 中不会出现原始的 Open WebUI 用户 ID。
    TRACE_HASH_SECRET: Optional[str] = None

    # 长期记忆。向量存放在独立的 Qdrant collection；
    # 权威记录和版本历史存放在 OPS_DB_PATH。
    ENABLE_MEMORY: bool = True
    QDRANT_URI: str = "http://localhost:6333"
    MEMORY_COLLECTION: str = "nexrag_memory"
    EMBEDDING_API_BASE_URL: str = "http://localhost:11434/v1"
    EMBEDDING_API_KEY: str = "ollama"
    EMBEDDING_MODEL: str = "bge-m3"
    MEMORY_TOKEN_BUDGET_CHARS: int = 1_500
    MEMORY_TOP_K: int = 6
    MEMORY_RETENTION_DAYS: int = 365
    MEMORY_SESSION_IDLE_HOURS: float = 6.0

    # 自进化循环。晋升（promotion）始终需要管理员批准。
    EVOLUTION_MIN_CLUSTER_SIZE: int = 5
    EVOLUTION_WINDOW_DAYS: int = 7
    EVOLUTION_MIN_GAIN: float = 0.02
    EVOLUTION_MAX_REGRESSION: float = 0.01
    EVOLUTION_MAX_LATENCY_GROWTH: float = 0.20
    EVOLUTION_REPEATS: int = 2
    PROPOSAL_TTL_DAYS: int = 14
    PROPOSAL_QUEUE_LIMIT: int = 10
    CANARY_RATIO: float = 0.10
    CANARY_MIN_DAYS: float = 3.0
    CANARY_MAX_DROP: float = 0.05


settings = Settings()
