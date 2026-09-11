from io import BytesIO
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.services.pdf_service import PdfService


def upload_sample(client: TestClient, sample_pdf: Path) -> dict:
    with sample_pdf.open("rb") as stream:
        response = client.post(
            "/api/v1/documents",
            files=[("files", ("original.pdf", stream, "application/pdf"))],
        )
    assert response.status_code == 201
    return response.json()[0]


def test_font_key_normalizes_linux_dejavu_book_style() -> None:
    assert PdfService._font_key("DejaVu Sans Book") == PdfService._font_key("DejaVuSans")


def test_upload_and_preview(client: TestClient, sample_pdf: Path) -> None:
    document = upload_sample(client, sample_pdf)
    assert document["filename"] == "original.pdf"
    assert document["page_count"] == 3
    assert document["pages"][1]["width"] == 400

    preview = client.get(f"/api/v1/documents/{document['id']}/pages/0/preview")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert preview.content.startswith(b"\x89PNG")


def test_extract_and_edit_page_text(client: TestClient, sample_pdf: Path) -> None:
    document = upload_sample(client, sample_pdf)
    content_response = client.get(
        f"/api/v1/documents/{document['id']}/pages/0/content"
    )
    assert content_response.status_code == 200, content_response.text
    content = content_response.json()
    assert content["layout_version"] == 2
    assert content["coordinate_space"] == "rotated_page"
    assert content["width"] == 300
    block = next(item for item in content["blocks"] if "Sample page 1" in item["text"])

    export = client.post(
        "/api/v1/exports",
        json={
            "filename": "text-edit.pdf",
            "pages": [
                {
                    "kind": "source",
                    "document_id": document["id"],
                    "page_index": 0,
                    "text_edits": [
                        {
                            "block_id": block["id"],
                            "bbox": block["bbox"],
                            "text": "Edited page 1",
                            "font_size": block["font_size"],
                            "color": block["color"],
                            "origin": block["origin"],
                        }
                    ],
                }
            ],
        },
    )
    assert export.status_code == 201, export.text
    result = client.get(export.json()["download_url"])
    output = pymupdf.open(stream=result.content, filetype="pdf")
    exported_text = output[0].get_text()
    output.close()
    assert "Edited page 1" in exported_text
    assert "Sample page 1" not in exported_text


def test_edit_text_on_natively_rotated_page(client: TestClient, tmp_path: Path) -> None:
    rotated_path = tmp_path / "rotated.pdf"
    source = pymupdf.open()
    page = source.new_page(width=300, height=400)
    page.insert_text((40, 60), "Rotated original", fontsize=16)
    page.set_rotation(90)
    source.save(rotated_path)
    source.close()

    document = upload_sample(client, rotated_path)
    content = client.get(
        f"/api/v1/documents/{document['id']}/pages/0/content"
    ).json()
    assert content["width"] == 400
    assert content["height"] == 300
    block = content["blocks"][0]
    assert 0 <= block["bbox"]["x0"] < block["bbox"]["x1"] <= 400
    assert 0 <= block["bbox"]["y0"] < block["bbox"]["y1"] <= 300

    export = client.post(
        "/api/v1/exports",
        json={
            "pages": [
                {
                    "kind": "source",
                    "document_id": document["id"],
                    "page_index": 0,
                    "text_edits": [
                        {
                            "block_id": block["id"],
                            "bbox": block["bbox"],
                            "text": "Rotated edit",
                            "font_size": block["font_size"],
                            "color": block["color"],
                            "origin": block["origin"],
                        }
                    ],
                }
            ]
        },
    )
    assert export.status_code == 201, export.text
    result = client.get(export.json()["download_url"])
    output = pymupdf.open(stream=result.content, filetype="pdf")
    assert "Rotated edit" in output[0].get_text()
    assert "Rotated original" not in output[0].get_text()
    output.close()


def test_edit_background_removes_text_without_white_fill(
    client: TestClient,
    tmp_path: Path,
) -> None:
    path = tmp_path / "colored-background.pdf"
    source = pymupdf.open()
    page = source.new_page(width=220, height=140)
    page.draw_rect(page.rect, color=None, fill=(0.2, 0.6, 0.8))
    page.insert_text((35, 70), "Background stays blue", fontsize=16, color=(0, 0, 0))
    source.save(path)
    source.close()

    document = upload_sample(client, path)
    preview = client.get(
        f"/api/v1/documents/{document['id']}/pages/0/edit-background"
    )
    assert preview.status_code == 200
    image = Image.open(BytesIO(preview.content)).convert("RGB")
    expected = image.getpixel((10, 10))
    text_area = image.crop((30, 45, 200, 75))
    assert all(
        max(abs(channel - expected[index]) for index, channel in enumerate(pixel)) <= 2
        for pixel in text_area.get_flattened_data()
    )


def test_extracts_previews_moves_replaces_and_deletes_page_images(
    client: TestClient,
    tmp_path: Path,
) -> None:
    path = tmp_path / "page-image.pdf"
    red = BytesIO()
    Image.new("RGB", (60, 40), (230, 25, 25)).save(red, "PNG")
    source = pymupdf.open()
    page = source.new_page(width=260, height=160)
    page.insert_image(pymupdf.Rect(30, 50, 90, 90), stream=red.getvalue())
    source.save(path)
    source.close()

    document = upload_sample(client, path)
    content_response = client.get(
        f"/api/v1/documents/{document['id']}/pages/0/content"
    )
    assert content_response.status_code == 200, content_response.text
    image = content_response.json()["images"][0]
    assert image["id"] == "image-0"
    assert image["bbox"] == {"x0": 30.0, "y0": 50.0, "x1": 90.0, "y1": 90.0}
    image_preview = client.get(image["preview_url"])
    assert image_preview.status_code == 200
    assert image_preview.content.startswith(b"\x89PNG")

    clean_background = client.get(
        f"/api/v1/documents/{document['id']}/pages/0/edit-background",
        params={"remove_text": "false", "remove_images": "true"},
    )
    clean_image = Image.open(BytesIO(clean_background.content)).convert("RGB")
    assert all(channel > 240 for channel in clean_image.getpixel((60, 70)))

    moved = client.post(
        "/api/v1/exports",
        json={
            "pages": [
                {
                    "kind": "source",
                    "document_id": document["id"],
                    "page_index": 0,
                    "image_edits": [
                        {
                            "image_id": image["id"],
                            "action": "replace",
                            "bbox": {"x0": 150, "y0": 55, "x1": 210, "y1": 95},
                        }
                    ],
                }
            ]
        },
    )
    assert moved.status_code == 201, moved.text
    moved_pdf = client.get(moved.json()["download_url"])
    output = pymupdf.open(stream=moved_pdf.content, filetype="pdf")
    moved_render = Image.open(BytesIO(output[0].get_pixmap().tobytes("png"))).convert("RGB")
    output.close()
    assert all(channel > 240 for channel in moved_render.getpixel((60, 70)))
    moved_target = moved_render.getpixel((180, 75))
    assert moved_target[0] > 200 and moved_target[1] < 60

    blue = BytesIO()
    Image.new("RGBA", (80, 50), (20, 70, 230, 255)).save(blue, "PNG")
    upload = client.post(
        "/api/v1/images",
        files={"file": ("edited.png", blue.getvalue(), "image/png")},
    )
    assert upload.status_code == 201, upload.text
    asset = upload.json()
    assert asset["width"] == 80
    assert client.get(asset["preview_url"]).status_code == 200

    export = client.post(
        "/api/v1/exports",
        json={
            "pages": [
                {
                    "kind": "source",
                    "document_id": document["id"],
                    "page_index": 0,
                    "image_edits": [
                        {
                            "image_id": image["id"],
                            "action": "replace",
                            "asset_id": asset["id"],
                            "bbox": {"x0": 150, "y0": 55, "x1": 230, "y1": 105},
                        }
                    ],
                }
            ]
        },
    )
    assert export.status_code == 201, export.text
    result = client.get(export.json()["download_url"])
    output = pymupdf.open(stream=result.content, filetype="pdf")
    rendered = Image.open(BytesIO(output[0].get_pixmap().tobytes("png"))).convert("RGB")
    output.close()
    assert all(channel > 240 for channel in rendered.getpixel((60, 70)))
    target = rendered.getpixel((190, 80))
    assert target[2] > 200 and target[0] < 60

    deleted = client.post(
        "/api/v1/exports",
        json={
            "pages": [
                {
                    "kind": "source",
                    "document_id": document["id"],
                    "page_index": 0,
                    "image_edits": [{"image_id": image["id"], "action": "delete"}],
                }
            ]
        },
    )
    assert deleted.status_code == 201, deleted.text
    deleted_pdf = client.get(deleted.json()["download_url"])
    output = pymupdf.open(stream=deleted_pdf.content, filetype="pdf")
    rendered = Image.open(BytesIO(output[0].get_pixmap().tobytes("png"))).convert("RGB")
    output.close()
    assert all(channel > 240 for channel in rendered.getpixel((60, 70)))


def test_moves_text_span_using_target_bbox_and_origin(
    client: TestClient,
    sample_pdf: Path,
) -> None:
    document = upload_sample(client, sample_pdf)
    content = client.get(
        f"/api/v1/documents/{document['id']}/pages/0/content"
    ).json()
    block = content["blocks"][0]
    span = block["lines"][0]["spans"][0]
    dx, dy = 90, 75
    moved_bbox = {
        key: value + (dx if key.startswith("x") else dy)
        for key, value in span["bbox"].items()
    }
    moved_origin = {"x": span["origin"]["x"] + dx, "y": span["origin"]["y"] + dy}
    export = client.post(
        "/api/v1/exports",
        json={
            "pages": [
                {
                    "kind": "source",
                    "document_id": document["id"],
                    "page_index": 0,
                    "text_edits": [
                        {
                            "block_id": block["id"],
                            "runs": [
                                {
                                    "span_id": span["id"],
                                    "text": span["text"],
                                    "bbox": moved_bbox,
                                    "origin": moved_origin,
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    )
    assert export.status_code == 201, export.text
    result = client.get(export.json()["download_url"])
    output = pymupdf.open(stream=result.content, filetype="pdf")
    words = output[0].get_text("words")
    output.close()
    sample = next(word for word in words if word[4] == "Sample")
    assert sample[0] > 120
    assert sample[1] > 110


def test_reuses_embedded_font_when_new_glyphs_are_available(
    client: TestClient,
    tmp_path: Path,
) -> None:
    font_path = next(
        (
            candidate
            for candidate in (
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            )
            if candidate.is_file()
        ),
        None,
    )
    if font_path is None:
        pytest.skip("no portable TrueType font available for embedded-font test")

    path = tmp_path / "embedded-font.pdf"
    source = pymupdf.open()
    page = source.new_page(width=320, height=180)
    page.insert_text(
        (40, 70),
        "Original font",
        fontname="sourcefont",
        fontfile=str(font_path),
        fontsize=19,
    )
    source.save(path)
    source.close()

    document = upload_sample(client, path)
    content = client.get(
        f"/api/v1/documents/{document['id']}/pages/0/content"
    ).json()
    block = content["blocks"][0]
    assert block["font_xref"] is not None
    assert block["font_url"]
    font_response = client.get(block["font_url"])
    assert font_response.status_code == 200
    assert font_response.headers["content-type"] in {"font/ttf", "font/otf"}

    text_edit = {
        "block_id": block["id"],
        "bbox": block["bbox"],
        "text": "Original edit",
        "font_size": block["font_size"],
        "color": block["color"],
        "font_name": block["font_name"],
        "font_xref": block["font_xref"],
        "line_height": block["line_height"],
        "origin": block["origin"],
    }
    export = client.post(
        "/api/v1/exports",
        json={
            "pages": [
                {
                    "kind": "source",
                    "document_id": document["id"],
                    "page_index": 0,
                    "text_edits": [text_edit],
                }
            ]
        },
    )
    assert export.status_code == 201, export.text
    result = client.get(export.json()["download_url"])
    output = pymupdf.open(stream=result.content, filetype="pdf")
    assert "Original edit" in output[0].get_text().replace("\xa0", " ")
    assert any(font[4] == "OriginalFont0" for font in output[0].get_fonts(full=True))
    output.close()


def test_extracts_and_edits_individual_styled_span(
    client: TestClient,
    tmp_path: Path,
) -> None:
    path = tmp_path / "mixed-spans.pdf"
    source = pymupdf.open()
    page = source.new_page(width=360, height=180)
    page.insert_text((40, 70), "Regular ", fontname="helv", fontsize=16, color=(0, 0, 0))
    page.insert_text((105, 70), "Red bold", fontname="hebo", fontsize=16, color=(1, 0, 0))
    source.save(path)
    source.close()

    document = upload_sample(client, path)
    content_response = client.get(
        f"/api/v1/documents/{document['id']}/pages/0/content"
    )
    assert content_response.status_code == 200, content_response.text
    block = content_response.json()["blocks"][0]
    assert len(block["lines"]) == 1
    line = block["lines"][0]
    assert line["text"] == "Regular Red bold"
    assert len(line["spans"]) == 2
    red_span = line["spans"][1]
    assert red_span["text"] == "Red bold"
    assert red_span["font_name"] == "Helvetica-Bold"
    assert red_span["font_weight"] == 700
    assert red_span["color"] == "#ff0000"
    assert red_span["advance"] > 0
    assert "".join(character["text"] for character in red_span["characters"]) == "Red bold"
    assert all(
        character["bbox"]["x1"] > character["bbox"]["x0"]
        for character in red_span["characters"]
    )

    run = {
        "span_id": red_span["id"],
        "text": "Newbold",
    }
    export = client.post(
        "/api/v1/exports",
        json={
            "filename": "span-edit.pdf",
            "pages": [
                {
                    "kind": "source",
                    "document_id": document["id"],
                    "page_index": 0,
                    "text_edits": [{"block_id": block["id"], "runs": [run]}],
                }
            ],
        },
    )
    assert export.status_code == 201, export.text
    edit_result = export.json()["text_edit_results"][0]
    assert edit_result["span_id"] == red_span["id"]
    assert edit_result["font_source"] == "builtin"
    assert edit_result["font_substituted"] is False
    assert edit_result["effective_font_size"] == 16
    assert edit_result["overflow_action"] == "none"

    result = client.get(export.json()["download_url"])
    output = pymupdf.open(stream=result.content, filetype="pdf")
    output_text = output[0].get_text()
    assert "Regular" in output_text
    assert "Newbold" in output_text
    assert "Red bold" not in output_text
    replacement = next(
        span
        for block_data in output[0].get_text("dict")["blocks"]
        for line_data in block_data.get("lines", [])
        for span in line_data.get("spans", [])
        if "Newbold" in span["text"]
    )
    assert replacement["font"] == "Helvetica-Bold"
    assert replacement["color"] == 0xFF0000
    assert replacement["size"] == pytest.approx(16, abs=0.05)
    output.close()


def test_span_edit_reports_overflow_and_can_explicitly_shrink(
    client: TestClient,
    tmp_path: Path,
) -> None:
    path = tmp_path / "span-overflow.pdf"
    source = pymupdf.open()
    page = source.new_page(width=260, height=140)
    page.insert_text((30, 65), "Short text", fontname="helv", fontsize=14)
    source.save(path)
    source.close()

    document = upload_sample(client, path)
    content = client.get(
        f"/api/v1/documents/{document['id']}/pages/0/content"
    ).json()
    block = content["blocks"][0]
    line = block["lines"][0]
    span = line["spans"][0]
    base_run = {
        "span_id": span["id"],
        "text": "Some longer text",
    }
    page_spec = {
        "kind": "source",
        "document_id": document["id"],
        "page_index": 0,
        "text_edits": [{"block_id": block["id"], "runs": [base_run]}],
    }

    unknown_span = client.post(
        "/api/v1/exports",
        json={
            "pages": [
                {
                    **page_spec,
                    "text_edits": [
                        {
                            "block_id": block["id"],
                            "runs": [{**base_run, "span_id": "missing-span"}],
                        }
                    ],
                }
            ]
        },
    )
    assert unknown_span.status_code == 422
    assert "does not belong" in unknown_span.json()["detail"]

    rejected = client.post("/api/v1/exports", json={"pages": [page_spec]})
    assert rejected.status_code == 422
    assert "does not fit" in rejected.json()["detail"]

    base_run["overflow_policy"] = "shrink"
    base_run["minimum_font_scale"] = 0.5
    accepted = client.post("/api/v1/exports", json={"pages": [page_spec]})
    assert accepted.status_code == 201, accepted.text
    result = accepted.json()["text_edit_results"][0]
    assert result["overflow_action"] == "shrink"
    assert 7 <= result["effective_font_size"] < 14


def test_span_edit_preserves_arbitrary_text_direction(
    client: TestClient,
    tmp_path: Path,
) -> None:
    path = tmp_path / "angled-text.pdf"
    source = pymupdf.open()
    page = source.new_page(width=320, height=260)
    origin = pymupdf.Point(100, 160)
    page.insert_text(
        origin,
        "Angle",
        fontname="helv",
        fontsize=18,
        morph=(origin, pymupdf.Matrix(30)),
    )
    source.save(path)
    source.close()

    document = upload_sample(client, path)
    content = client.get(
        f"/api/v1/documents/{document['id']}/pages/0/content"
    ).json()
    block = content["blocks"][0]
    line = block["lines"][0]
    span = line["spans"][0]
    export = client.post(
        "/api/v1/exports",
        json={
            "pages": [
                {
                    "kind": "source",
                    "document_id": document["id"],
                    "page_index": 0,
                    "text_edits": [
                        {
                            "block_id": block["id"],
                            "runs": [
                                {
                                    "span_id": span["id"],
                                    "text": "Slope",
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    )
    assert export.status_code == 201, export.text
    result = client.get(export.json()["download_url"])
    output = pymupdf.open(stream=result.content, filetype="pdf")
    output_line = next(
        line_data
        for block_data in output[0].get_text("rawdict")["blocks"]
        for line_data in block_data.get("lines", [])
        if "Slope"
        in "".join(
            character["c"]
            for span_data in line_data.get("spans", [])
            for character in span_data.get("chars", [])
        )
    )
    assert output_line["dir"][0] == pytest.approx(line["direction"]["x"], abs=0.001)
    assert output_line["dir"][1] == pytest.approx(line["direction"]["y"], abs=0.001)
    output.close()


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
