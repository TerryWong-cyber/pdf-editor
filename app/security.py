"""Request identity shared by REST and MCP; no identity supplied by tool arguments."""

import hashlib
import secrets
from contextvars import ContextVar

from starlette.responses import JSONResponse

from app.config import settings

principal: ContextVar[str | None] = ContextVar("pdf_editor_principal", default=None)


class IdentityMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] == "/health":
            await self.app(scope, receive, send)
            return
        owner = None
        if settings.api_keys and scope["method"] != "OPTIONS":
            headers = dict(scope["headers"])
            authorization = headers.get(b"authorization", b"").decode("latin-1")
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() == "bearer":
                for name, secret in settings.api_keys.items():
                    if secret and secrets.compare_digest(token.encode(), secret.encode()):
                        owner = hashlib.sha256(name.encode()).hexdigest()
                        break
            if owner is None:
                response = JSONResponse(
                    {"detail": "valid Bearer token required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        identity_token = principal.set(owner)
        try:
            await self.app(scope, receive, send)
        finally:
            principal.reset(identity_token)
