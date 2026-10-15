"""Request identity shared by REST and MCP; no identity supplied by tool arguments."""

import hashlib
import re
import secrets
from contextvars import ContextVar
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings
from app.download_links import request_origin, verify_export_url

principal: ContextVar[str | None] = ContextVar("pdf_editor_principal", default=None)


class IdentityMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] == "/health":
            await self.app(scope, receive, send)
            return
        owner = None
        signed = False
        path = scope["path"].removeprefix(scope.get("root_path", ""))
        match = re.fullmatch(r"/api/v1/exports/([a-f0-9-]{36})/download", path)
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        if scope["method"] == "GET" and match and "signature" in query:
            signed, owner = verify_export_url(match[1], query)
            if not signed:
                await JSONResponse(
                    {"detail": "download link is invalid or expired"}, status_code=401
                )(
                    scope,
                    receive,
                    send,
                )
                return
        if settings.api_keys and not signed and scope["method"] != "OPTIONS":
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
        origin_token = request_origin.set(str(Request(scope).base_url).rstrip("/"))
        try:
            await self.app(scope, receive, send)
        finally:
            principal.reset(identity_token)
            request_origin.reset(origin_token)
