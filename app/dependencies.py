from functools import lru_cache

from app.config import settings
from app.services.pdf_service import PdfService
from app.services.storage import FileStorage


@lru_cache
def get_storage() -> FileStorage:
    return FileStorage(settings.data_dir)


def get_pdf_service() -> PdfService:
    return PdfService(get_storage())

