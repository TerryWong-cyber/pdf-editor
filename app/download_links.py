"""Short-lived, export-scoped bearer URLs; never disclose a service API key."""

import hashlib
import hmac
import json
import secrets
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from urllib.parse import urlencode

from app.config import settings

_process_key = secrets.token_bytes(32)
request_origin: ContextVar[str] = ContextVar("pdf_editor_request_origin", default="")


def _signature(export_id: str, filename: str, owner: str, expires: int) -> str:
    key = settings.download_signing_key.get_secret_value().encode() or _process_key
    payload = json.dumps([export_id, filename, owner, expires], separators=(",", ":")).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def signed_export_url(export_id: str, filename: str, owner: str | None) -> tuple[str, str]:
    expires = int(time.time()) + settings.download_url_ttl_seconds
    owner = owner or ""
    query = urlencode(
        {
            "filename": filename,
            "expires": expires,
            "owner": owner,
            "signature": _signature(export_id, filename, owner, expires),
        }
    )
    base = settings.public_base_url.rstrip("/") or request_origin.get()
    url = f"{base}/api/v1/exports/{export_id}/download?{query}"
    return url, datetime.fromtimestamp(expires, UTC).isoformat()


def verify_export_url(export_id: str, query: dict[str, list[str]]) -> tuple[bool, str | None]:
    try:
        if any(
            len(query.get(key, [])) != 1 for key in ("filename", "expires", "owner", "signature")
        ):
            return False, None
        filename, owner, supplied = (query[key][0] for key in ("filename", "owner", "signature"))
        expires = int(query["expires"][0])
        expected = _signature(export_id, filename, owner, expires)
        valid = expires > time.time() and hmac.compare_digest(supplied.encode(), expected.encode())
        return valid, (owner or None) if valid else None
    except (ValueError, TypeError, OverflowError):
        return False, None
