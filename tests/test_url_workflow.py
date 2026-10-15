import io
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from test_mcp_api import call, data

from app import download_links
from app.config import settings
from app.routers import documents
from app.url_files import RemoteFile


def test_url_import_extract_and_signed_download(client, sample_pdf, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", {"alice": "alice-secret", "bob": "bob-secret"})
    auth = {"Authorization": "Bearer alice-secret"}
    streams = []

    async def fetch(url, filename, **kwargs):
        assert url == "https://cloud.example/input.pdf?signature=source"
        stream = io.BytesIO(sample_pdf.read_bytes())
        streams.append(stream)
        return RemoteFile(
            stream, filename or "input.pdf", "application/pdf", len(stream.getvalue())
        )

    monkeypatch.setattr(documents, "fetch_file", fetch)
    imported = data(
        call(
            client,
            "import_file_from_url",
            {
                "file_url": "https://cloud.example/input.pdf?signature=source",
            },
            auth,
        )
    )
    assert streams[0].closed
    assert imported["kind"] == "document"
    assert imported["page_count"] == 3
    result = data(
        call(
            client,
            "extract_pages",
            {
                "document_id": imported["id"],
                "page_numbers": [1],
                "filename": "result.pdf",
            },
            auth,
        )
    )
    artifact = result["files"][0]
    assert artifact["file_url"].startswith("http://")
    assert artifact["mime_type"] == "application/pdf"
    assert artifact["size_bytes"] > 0
    assert artifact["expires_at"]
    assert "alice-secret" not in artifact["file_url"]
    response = client.get(artifact["file_url"])
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")
    assert len(response.content) == artifact["size_bytes"]
    assert client.get(result["download_url"]).status_code == 401  # legacy URL still requires auth
    assert (
        client.get(
            result["download_url"], headers={"Authorization": "Bearer bob-secret"}
        ).status_code
        == 404
    )
    parts = urlsplit(artifact["file_url"])
    query = parse_qs(parts.query, keep_blank_values=True)
    query["filename"] = ["tampered.pdf"]
    tampered = urlunsplit(parts._replace(query=urlencode(query, doseq=True)))
    assert client.get(tampered).status_code == 401
    assert client.get(artifact["file_url"] + "&signature=duplicate").status_code == 401
    assert client.get(artifact["file_url"].replace(result["id"], imported["id"])).status_code == 401
    expires = int(parse_qs(parts.query)["expires"][0])
    monkeypatch.setattr(download_links.time, "time", lambda: expires + 1)
    assert client.get(artifact["file_url"]).status_code == 401


def test_rest_url_import_validates_pdf_and_cleans_up(client, monkeypatch):
    streams = []

    async def fetch(*args, **kwargs):
        stream = io.BytesIO(b"not a PDF")
        streams.append(stream)
        return RemoteFile(stream, "fake.pdf", "application/pdf", 9)

    monkeypatch.setattr(documents, "fetch_file", fetch)
    response = client.post(
        "/api/v1/files/import-url", json={"file_url": "https://cloud.example/fake.pdf"}
    )
    assert response.status_code == 422
    assert streams[0].closed
    assert not list((settings.data_dir / "originals").glob("*.pdf"))


def test_url_image_and_watermark_import(client, monkeypatch):
    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(image, "PNG")
    body = image.getvalue()

    async def fetch(*args, **kwargs):
        return RemoteFile(io.BytesIO(body), "asset.png", "image/png", len(body))

    monkeypatch.setattr(documents, "fetch_file", fetch)
    for kind in ("image", "watermark"):
        imported = data(
            call(
                client,
                "import_file_from_url",
                {
                    "file_url": "https://cloud.example/asset.png",
                    "kind": kind,
                },
            )
        )
        assert imported["kind"] == kind
        assert imported["id"]
        endpoint = "images" if kind == "image" else "watermarks"
        assert client.get(f"/api/v1/{endpoint}/{imported['id']}/preview").status_code == 200
