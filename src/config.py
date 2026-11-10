"""Global NexRAG settings loaded from environment variables and .env."""

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_KNOWLEDGE_TOP_K = 3
MAX_KNOWLEDGE_TOP_K = 20


class Settings(BaseSettings):
    """Runtime configuration loaded from the environment and .env."""

    # The shared .env also configures other native services;
    # ignore those keys when validating NexRAG settings.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    PROJECT_NAME: str = "NexRAG"
    API_V1_STR: str = "/v1"
    PORT: int = 8000
    # Only explicit deployment configuration may expose the API beyond loopback.
    HOST: str = "127.0.0.1"

    LLM_API_BASE: str = "http://localhost:11434/v1"
    LLM_API_KEY: str = "ollama"
    LLM_MODEL_NAME: str = "qwen3:4b-instruct"
    AGENT_TIMEOUT_SECONDS: int = 90
    WEBUI_TOOL_BRIDGE_URL: str = "http://127.0.0.1:8080"

    # Delegate retrieval to the existing AarhusAI service.
    # This process neither embeds text nor accesses Qdrant directly;
    # scripts/ingest_documents.py loads its own embedding and Qdrant settings.
    RETRIEVAL_API_BASE_URL: str = "http://localhost:8002"
    RETRIEVAL_API_KEY: Optional[str] = None
    RETRIEVAL_TIMEOUT_SECONDS: float = 45.0
    RETRIEVAL_MIN_RELEVANCE: float = 0.35
    # An explicit deployment value takes precedence over stored RSI parameters.
    KNOWLEDGE_TOP_K: int | None = Field(default=None, ge=1, le=MAX_KNOWLEDGE_TOP_K)
    REQUIRE_KNOWLEDGE_EVIDENCE: bool = False
    MAX_TOOL_RETRIES: int = 2
    KNOWLEDGE_COLLECTION_NAME: str = "nexrag_docs"

    # Forward shared-library uploads and deletes to AarhusAI ingestion;
    # do not duplicate its chunking and embedding pipeline.
    INGESTION_API_BASE_URL: str = "http://localhost:8001"
    INGESTION_API_KEY: Optional[str] = None
    INGESTION_TIMEOUT_SECONDS: float = 120.0

    ENABLE_WEB_SEARCH: bool = True
    SEARXNG_URL: str = ""
    WEB_SEARCH_TIMEOUT_SECONDS: float = 8.0

    # Replace the Olist example files to connect another business database
    # without changing agent or query code.
    BUSINESS_SOURCE_PATH: str = "examples/olist/business-source.yaml"
    BUSINESS_DB_PATH: Optional[str] = None
    BUSINESS_DB_DSN: Optional[str] = None
    METADATA_PATH: str = "examples/olist/metadata.yaml"
    TENANT_ACCESS_PATH: str = "examples/olist/tenant-access.yaml"

    # Open WebUI signs forwarded identities with this shared secret.
    # Neither secret has a development default: missing secrets fail closed.
    NEXRAG_SHARED_API_KEY: Optional[str] = None
    NEXRAG_USER_JWT_SECRET: Optional[str] = None
    NEXRAG_USER_JWT_HEADER: str = "X-OpenWebUI-User-Jwt"

    # CORS applies only to direct browser requests, such as localhost:3000;
    # server-to-server Open WebUI requests bypass it.
    # Use explicit comma-separated origins rather than a wildcard
    # to avoid reflecting credentialed requests from arbitrary sites.
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    # Use a dedicated sandbox rather than the process working directory
    # so file inspection cannot reach application source or configuration.
    SANDBOX_ROOT_DIR: str = "data/sandbox_uploads"

    # Share this root with the Web backend; generated reports are isolated
    # in account and conversation subdirectories under the user-bound folder.
    EXPORTS_DIR: str = "data/artifacts"

    # Keep writable operations data separate from business databases.

    OPS_DB_PATH: str = "data/ops/ops.db"
    ENABLE_TRACING: bool = True
    # 'redacted' masks PII; 'none' retains only structure and scores;
    # 'full' preserves raw text for local debugging only.
    TRACE_STORE_CONTENT: str = "redacted"
    TRACE_RETENTION_DAYS: int = 30
    # Fall back to the JWT secret for keyed identity hashing
    # so traces never contain raw Open WebUI user IDs.
    TRACE_HASH_SECRET: Optional[str] = None

    # Keep vectors in a separate Qdrant collection;
    # OPS_DB_PATH owns authoritative records and version history.
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

    # Promotion always requires administrator approval.
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
