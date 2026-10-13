from functools import lru_cache
from pathlib import Path

from app.config import settings
from app.security import principal
from app.services.font_resolver import FontResolver
from app.services.pdf_service import PdfService
from app.services.storage import FileStorage


@lru_cache(maxsize=128)
def _storage_for_root(root: Path) -> FileStorage:
    return FileStorage(root)


def get_storage() -> FileStorage:
    owner = principal.get()
    root = settings.data_dir.resolve()
    if owner is not None:
        root = root / "owners" / owner
    return _storage_for_root(root)


get_storage.cache_clear = _storage_for_root.cache_clear


def get_pdf_service() -> PdfService:
    return PdfService(get_storage(), FontResolver(settings))


def get_editor_service():
    from app.services.editor_service import EditorService

    return EditorService(get_storage(), get_pdf_service())
