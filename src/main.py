"""NexRAG 后端服务的 FastAPI 应用入口。"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.directory_routes import router as directory_router
from src.api.exports_routes import router as exports_router
from src.api.mcp_routes import router as mcp_router
from src.api.memory_routes import router as memory_router
from src.api.model_routes import router as model_router
from src.api.ops_routes import router as ops_router
from src.api.policy_routes import router as policy_router
from src.api.routes import router as api_router
from src.api.workspace_routes import router as workspace_router
from src.config import settings

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="NexRAG local agentic RAG backend service.",
    version="0.1.0",
)

# Open WebUI 后端以服务端到服务端方式调用 NexRAG（Authorization 头，无 cookie），
# 不受此策略影响。CORS 只对浏览器直连 NexRAG 有意义；"*" 搭配 allow_credentials
# 会让任意来源都能反射带凭据的请求，所以改用显式、可配置的来源列表
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

# 注册 OpenAI 兼容的 API 路由。
app.include_router(api_router, prefix=settings.API_V1_STR)
app.include_router(ops_router, prefix=settings.API_V1_STR)
app.include_router(memory_router, prefix=settings.API_V1_STR)
app.include_router(exports_router, prefix=settings.API_V1_STR)
app.include_router(workspace_router, prefix=settings.API_V1_STR)
app.include_router(model_router, prefix=settings.API_V1_STR)
app.include_router(policy_router, prefix=settings.API_V1_STR)
app.include_router(directory_router, prefix=settings.API_V1_STR)
app.include_router(mcp_router, prefix=settings.API_V1_STR)


@app.on_event("startup")
def protect_historical_conversation_traces():
    # 保留历史记录，但隐私策略变更后，不得再把旧的个人对话
    # 默默复用为全局训练/优化素材。
    from src.midplatform.ops_store import get_ops_store

    get_ops_store().execute(
        "UPDATE traces SET artifact_versions=json_set(artifact_versions,'$.content_scope','private') "
        "WHERE json_extract(artifact_versions,'$.content_scope') IS NULL"
    )


@app.get("/health")
async def health_check():
    """存活与就绪健康检查探针。"""
    return {"status": "ok", "project": settings.PROJECT_NAME}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host=settings.HOST, port=settings.PORT, reload=True)
