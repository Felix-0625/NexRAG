"""FastAPI entry point for the NexRAG backend."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src import __version__
from src.api.directory_routes import router as directory_router
from src.api.exports_routes import router as exports_router
from src.api.mcp_routes import router as mcp_router
from src.api.memory_routes import router as memory_router
from src.api.model_routes import router as model_router
from src.api.ops_routes import router as ops_router
from src.api.policy_routes import router as policy_router
from src.api.routes import router as api_router
from src.api.tenant_routes import router as tenant_router
from src.api.workspace_routes import router as workspace_router
from src.config import settings

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="NexRAG local agentic RAG backend service.",
    version=__version__,
)

# Open WebUI uses server-to-server Authorization headers, not cookies.
# CORS protects direct browser access; wildcard credentialed origins
# would reflect arbitrary sites, so require an explicit configurable allowlist.
# （settings.ALLOWED_ORIGINS）。
_allowed_origins = [
    origin.strip() for origin in settings.ALLOWED_ORIGINS.split(",") if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(api_router, prefix=settings.API_V1_STR)
app.include_router(ops_router, prefix=settings.API_V1_STR)
app.include_router(tenant_router, prefix=settings.API_V1_STR)
app.include_router(memory_router, prefix=settings.API_V1_STR)
app.include_router(exports_router, prefix=settings.API_V1_STR)
app.include_router(workspace_router, prefix=settings.API_V1_STR)
app.include_router(model_router, prefix=settings.API_V1_STR)
app.include_router(policy_router, prefix=settings.API_V1_STR)
app.include_router(directory_router, prefix=settings.API_V1_STR)
app.include_router(mcp_router, prefix=settings.API_V1_STR)


@app.on_event("startup")
def protect_historical_conversation_traces():
    # Preserve history without silently reusing private conversations
    # as shared training or optimization data after a privacy-policy change.
    """Prevent historical private conversations from contributing shared learning."""
    from src.midplatform.ops_store import get_ops_store

    get_ops_store().execute(
        "UPDATE traces SET artifact_versions=json_set(artifact_versions,'$.content_scope','private') "
        "WHERE json_extract(artifact_versions,'$.content_scope') IS NULL"
    )


@app.get("/health")
async def health_check():
    """Probe service liveness and readiness."""
    return {"status": "ok", "project": settings.PROJECT_NAME}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host=settings.HOST, port=settings.PORT, reload=True)
