from collections.abc import Iterator
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.dependencies import get_storage
from app.main import app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings.data_dir = tmp_path / "data"
    get_storage.cache_clear()
    with TestClient(app) as test_client:
        yield test_client
    get_storage.cache_clear()


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "sample.pdf"
    document = pymupdf.open()
    for index, size in enumerate(((300, 400), (400, 300), (300, 400)), start=1):
        page = document.new_page(width=size[0], height=size[1])
        page.insert_text((40, 60), f"Sample page {index}", fontsize=18)
    document.save(path)
    document.close()
    return path

