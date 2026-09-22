from io import BytesIO
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.dependencies import get_storage


def upload(client: TestClient, content: bytes) -> dict:
    response = client.post(
        "/api/v1/documents",
        files={"files": ("preview.pdf", content, "application/pdf")},
    )
    assert response.status_code == 201
    return response.json()[0]


def assert_matches_export(
    client: TestClient, spec: dict, preview: bytes, max_edge: int = 360,
) -> None:
    response = client.post("/api/v1/exports", json={"pages": [spec]})
    assert response.status_code == 201, response.text
    download = client.get(response.json()["download_url"])
    with pymupdf.open(stream=download.content, filetype="pdf") as document:
        page = document[0]
        scale = max_edge / max(page.rect.width, page.rect.height)
        expected = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        actual = pymupdf.Pixmap(preview)
        assert (actual.width, actual.height) == (expected.width, expected.height)
        assert actual.samples == expected.samples


def test_text_preview_matches_export_without_retaining_files(
    client: TestClient, sample_pdf: Path,
) -> None:
    original = sample_pdf.read_bytes()
    document = upload(client, original)
    content = client.get(f"/api/v1/documents/{document['id']}/pages/0/content").json()
    block = content["blocks"][0]
    span = block["lines"][0]["spans"][0]
    spec = {
        "kind": "source", "document_id": document["id"], "page_index": 0,
        "text_edits": [{"block_id": block["id"], "runs": [
            {"span_id": span["id"], "text": "Updated", "color": "#cc0000"},
        ]}],
    }
    before = set(get_storage().exports.iterdir())
    response = client.post("/api/v1/previews", json={"page": spec})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert set(get_storage().exports.iterdir()) == before
    assert get_storage().original_path(document["id"]).read_bytes() == original
    assert_matches_export(client, spec, response.content)
    reverted = client.post("/api/v1/previews", json={"page": {
        "kind": "source", "document_id": document["id"], "page_index": 0,
    }})
    assert reverted.status_code == 200
    assert reverted.content != response.content


@pytest.mark.parametrize("action", ["delete", "replace"])
def test_image_preview_reflects_deletion_and_movement(client: TestClient, action: str) -> None:
    image = BytesIO()
    Image.new("RGB", (30, 30), "red").save(image, "PNG")
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=300, height=400)
        page.insert_text((30, 30), "Image preview")
        page.insert_image(pymupdf.Rect(40, 80, 100, 140), stream=image.getvalue())
        document = upload(client, pdf.tobytes())
    content = client.get(f"/api/v1/documents/{document['id']}/pages/0/content").json()
    edit = {"image_id": content["images"][0]["id"], "action": action}
    if action == "replace":
        edit["bbox"] = {"x0": 180, "y0": 220, "x1": 240, "y1": 280}
    spec = {
        "kind": "source", "document_id": document["id"], "page_index": 0,
        "image_edits": [edit],
    }
    response = client.post("/api/v1/previews", json={"page": spec})
    assert response.status_code == 200, response.text
    bitmap = Image.open(BytesIO(response.content)).convert("RGB")
    assert bitmap.getpixel((63, 99)) == (255, 255, 255)
    if action == "replace":
        assert bitmap.getpixel((189, 225)) == (255, 0, 0)
    assert_matches_export(client, spec, response.content)


def test_preview_rejects_multiple_pages(client: TestClient) -> None:
    response = client.post("/api/v1/previews", json={"pages": [
        {"kind": "blank"}, {"kind": "blank"},
    ]})
    assert response.status_code == 422


def test_dialog_preview_resolution_with_edits_crop_rotation_and_watermark(
    client: TestClient, sample_pdf: Path,
) -> None:
    document = upload(client, sample_pdf.read_bytes())
    content = client.get(f"/api/v1/documents/{document['id']}/pages/0/content").json()
    block = content["blocks"][0]
    span = block["lines"][0]["spans"][0]
    spec = {
        "kind": "source", "document_id": document["id"], "page_index": 0,
        "rotation": 90,
        "crop": {"left": 0.1, "right": 0.05, "top": 0.05, "bottom": 0.1},
        "watermark": {"kind": "text", "text": "DRAFT", "font_name": "Helvetica"},
        "text_edits": [{"block_id": block["id"], "runs": [
            {"span_id": span["id"], "text": "Updated", "color": "#cc0000"},
        ]}],
    }
    response = client.post("/api/v1/previews", json={"page": spec, "max_edge": 1200})
    assert response.status_code == 200, response.text
    bitmap = pymupdf.Pixmap(response.content)
    assert max(bitmap.width, bitmap.height) == 1200
    assert_matches_export(client, spec, response.content, max_edge=1200)


@pytest.mark.parametrize("max_edge", [0, 127, 1601, 100000])
def test_preview_rejects_unbounded_resolution(client: TestClient, max_edge: int) -> None:
    response = client.post("/api/v1/previews", json={
        "page": {"kind": "blank"}, "max_edge": max_edge,
    })
    assert response.status_code == 422
