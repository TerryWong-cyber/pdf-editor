from io import BytesIO
from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw


def upload_sample(client: TestClient, sample_pdf: Path) -> dict:
    with sample_pdf.open("rb") as stream:
        response = client.post(
            "/api/v1/documents",
            files=[("files", ("original.pdf", stream, "application/pdf"))],
        )
    assert response.status_code == 201
    return response.json()[0]


def test_upload_and_preview(client: TestClient, sample_pdf: Path) -> None:
    document = upload_sample(client, sample_pdf)
    assert document["filename"] == "original.pdf"
    assert document["page_count"] == 3
    assert document["pages"][1]["width"] == 400

    preview = client.get(f"/api/v1/documents/{document['id']}/pages/0/preview")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert preview.content.startswith(b"\x89PNG")


def test_compose_copy_reorder_rotate_crop_and_blank(client: TestClient, sample_pdf: Path) -> None:
    document = upload_sample(client, sample_pdf)
    original_bytes = sample_pdf.read_bytes()
    pages = [
        {"kind": "source", "document_id": document["id"], "page_index": 2},
        {
            "kind": "source",
            "document_id": document["id"],
            "page_index": 0,
            "rotation": 90,
            "crop": {"left": 0.1, "top": 0.1, "right": 0.1, "bottom": 0.1},
        },
        {"kind": "source", "document_id": document["id"], "page_index": 0},
        {"kind": "blank", "width": 595, "height": 842},
    ]
    response = client.post("/api/v1/exports", json={"filename": "my edit.pdf", "pages": pages})
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["page_count"] == 4
    assert payload["filename"] == "my-edit.pdf"

    download = client.get(payload["download_url"])
    assert download.status_code == 200
    result = pymupdf.open(stream=download.content, filetype="pdf")
    assert result.page_count == 4
    assert round(result[1].rect.width) == 320
    assert round(result[1].rect.height) == 240
    assert "Sample page 3" in result[0].get_text()
    assert "Sample page 1" in result[2].get_text()
    result.close()
    assert sample_pdf.read_bytes() == original_bytes


def test_rejects_non_pdf_and_invalid_crop(client: TestClient, sample_pdf: Path) -> None:
    response = client.post(
        "/api/v1/documents",
        files=[("files", ("fake.pdf", b"not a pdf", "application/pdf"))],
    )
    assert response.status_code == 422

    document = upload_sample(client, sample_pdf)
    response = client.post(
        "/api/v1/exports",
        json={
            "pages": [
                {
                    "kind": "source",
                    "document_id": document["id"],
                    "page_index": 0,
                    "crop": {"left": 0.6, "right": 0.4},
                }
            ]
        },
    )
    assert response.status_code == 422


def test_local_development_cors(client: TestClient) -> None:
    response = client.options(
        "/api/v1/documents",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_upload_and_apply_watermark_to_multiple_pages(
    client: TestClient,
    sample_pdf: Path,
) -> None:
    watermark_image = Image.new("RGBA", (320, 100), (255, 255, 255, 0))
    draw = ImageDraw.Draw(watermark_image)
    draw.rounded_rectangle((2, 2, 317, 97), radius=14, fill=(210, 45, 45, 220))
    draw.text((85, 38), "WATERMARK", fill="white")
    watermark_stream = BytesIO()
    watermark_image.save(watermark_stream, format="PNG")

    upload = client.post(
        "/api/v1/watermarks",
        files=[("file", ("stamp.png", watermark_stream.getvalue(), "image/png"))],
    )
    assert upload.status_code == 201, upload.text
    watermark = upload.json()
    assert watermark["filename"] == "stamp.png"
    preview = client.get(watermark["preview_url"])
    assert preview.status_code == 200
    assert preview.content.startswith(b"\x89PNG")

    document = upload_sample(client, sample_pdf)
    watermark_spec = {
        "watermark_id": watermark["id"],
        "rotation": 37,
        "scale": 0.42,
        "opacity": 0.3,
        "x": 0.62,
        "y": 0.44,
    }
    export = client.post(
        "/api/v1/exports",
        json={
            "filename": "watermarked.pdf",
            "pages": [
                {
                    "kind": "source",
                    "document_id": document["id"],
                    "page_index": 0,
                    "watermark": watermark_spec,
                },
                {
                    "kind": "source",
                    "document_id": document["id"],
                    "page_index": 1,
                    "watermark": watermark_spec,
                },
            ],
        },
    )
    assert export.status_code == 201, export.text
    result = client.get(export.json()["download_url"])
    output = pymupdf.open(stream=result.content, filetype="pdf")
    assert output.page_count == 2
    assert len(output[0].get_images(full=True)) >= 1
    assert len(output[1].get_images(full=True)) >= 1
    output.close()


def test_rejects_pdf_and_oversized_watermarks_before_decoding(client: TestClient) -> None:
    pdf_response = client.post(
        "/api/v1/watermarks",
        files=[("file", ("mistake.pdf", b"%PDF-1.7\n", "application/pdf"))],
    )
    assert pdf_response.status_code == 415
    assert "only supports PNG" in pdf_response.json()["detail"]

    oversized_response = client.post(
        "/api/v1/watermarks",
        files=[("file", ("too-large.png", b"x" * (20 * 1024 * 1024 + 1), "image/png"))],
    )
    assert oversized_response.status_code == 413
    assert oversized_response.json()["detail"] == "watermark exceeds 20 MB limit"
