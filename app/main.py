from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.dependencies import get_storage
from app.routers.documents import router
from app.security import IdentityMiddleware


def create_app() -> FastAPI:
    get_storage()
    mcp_server = None
    if settings.mcp_enabled:
        from app.mcp_server import create_mcp_server

        mcp_server = create_mcp_server()
        mcp_app = mcp_server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if mcp_server is None:
            yield
        else:
            # Mounted ASGI apps do not receive the parent lifespan automatically.
            async with mcp_server.session_manager.run():
                yield

    application = FastAPI(
        title="PDF Editor API",
        version="0.1.0",
        description="Non-destructive page-level PDF editing and export.",
        lifespan=lifespan,
    )
    application.add_middleware(IdentityMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "MCP-Protocol-Version", "Mcp-Session-Id"],
    )
    application.include_router(router, prefix="/api/v1")
    if mcp_server is not None:
        application.mount("/mcp", mcp_app)
        application.state.mcp_server = mcp_server

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
