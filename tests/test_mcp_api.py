"""Exercise MCP on the actual mounted HTTP transport, not only Python handlers."""

import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient
from PIL import Image

from app.agent_schemas import AgentExportRequest
from app.config import settings
from app.dependencies import get_editor_service


def upload(client: TestClient, path: Path, headers=None):
    with path.open("rb") as stream:
        response = client.post(
            "/api/v1/documents", files={"files": (path.name, stream)}, headers=headers
        )
    assert response.status_code == 201, response.text
    return response.json()[0]


def rpc(client: TestClient, method: str, params=None, headers=None):
    response = client.post(
        "/mcp/",
        headers={
            "Host": "localhost",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "error" not in payload, payload
    return payload["result"]


def call(client: TestClient, name: str, arguments: dict, headers=None):
    return rpc(client, "tools/call", {"name": name, "arguments": arguments}, headers)


def data(result):
    assert not result.get("isError", False), result
    return result["structuredContent"]


def test_discovery_extract_third_page_and_import(client, sample_pdf):
    initialized = rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert initialized["serverInfo"]["name"] == "pdf-editor"
    tools = {tool["name"]: tool for tool in rpc(client, "tools/list")["tools"]}
    assert {"extract_pages", "compose_pdf", "find_text", "export_draft"} <= tools.keys()
    assert (
        tools["extract_pages"]["inputSchema"]["properties"]["page_numbers"]["items"]["minimum"] == 1
    )
    document = upload(client, sample_pdf)
    original = sample_pdf.read_bytes()
    result = data(
        call(
            client,
            "extract_pages",
            {
                "document_id": document["id"],
                "page_numbers": [3],
                "filename": "third.pdf",
            },
        )
    )
    assert result["page_count"] == 1
    downloaded = client.get(result["download_url"])
    with pymupdf.open(stream=downloaded.content, filetype="pdf") as output:
        assert len(output) == 1
        assert "Sample page 3" in output[0].get_text()
    assert sample_pdf.read_bytes() == original
    imported = data(call(client, "import_export", {"export_id": result["id"]}))
    assert imported["page_count"] == 1
    info = data(call(client, "get_document_info", {"document_id": imported["id"]}))
    assert info["pages"][0]["page_number"] == 1
    assert call(
        client,
        "extract_pages",
        {
            "document_id": document["id"],
            "page_numbers": [0],
        },
    )["isError"]
    assert call(
        client,
        "extract_pages",
        {
            "document_id": document["id"],
            "page_numbers": [4],
        },
    )["isError"]
    assert call(client, "get_document_info", {"document_id": "../../secret"})["isError"]


def test_search_preview_and_cumulative_draft_conflicts(client, sample_pdf):
    document = upload(client, sample_pdf)
    document_id = document["id"]
    found = data(call(client, "find_text", {"document_id": document_id, "query": "page"}))
    assert len(found["matches"]) == 3
    layout = data(
        call(
            client,
            "get_page_content",
            {
                "document_id": document_id,
                "page_number": 3,
            },
        )
    )
    span = layout["blocks"][0]["lines"][0]["spans"][0]
    assert "characters" not in span
    characters = data(
        call(
            client,
            "get_page_content",
            {
                "document_id": document_id,
                "page_number": 3,
                "detail": "characters",
            },
        )
    )
    assert characters["blocks"][0]["lines"][0]["spans"][0]["characters"]
    draft = data(call(client, "create_draft", {"document_id": document_id}))
    replacement = {
        "document_id": document_id,
        "page_number": 3,
        "block_id": layout["blocks"][0]["id"],
        "span_id": span["id"],
        "expected_text": span["text"],
        "new_text": "Changed 3",
    }
    args = {"draft_id": draft["draft_id"], "expected_revision": 1, "replacement": replacement}
    changed = data(call(client, "edit_draft_text", args))
    assert changed["revision"] == 2
    stale = call(client, "edit_draft_text", args)
    assert stale["isError"] and "revision conflict" in stale["content"][0]["text"]
    wrong_text = call(client, "edit_draft_text", {**args, "expected_revision": 2})
    assert wrong_text["isError"] and "expected_text" in wrong_text["content"][0]["text"]
    changed = data(
        call(
            client,
            "edit_draft_text",
            {
                **args,
                "expected_revision": 2,
                "replacement": {**replacement, "expected_text": "Changed 3", "new_text": "Again 3"},
            },
        )
    )
    assert changed["revision"] == 3
    image = call(
        client,
        "preview_draft_page",
        {
            "draft_id": draft["draft_id"],
            "expected_revision": 3,
            "page_number": 3,
        },
    )
    assert image["content"][0]["type"] == "image"
    with Image.open(BytesIO(base64.b64decode(image["content"][0]["data"]))) as preview:
        assert max(preview.size) == 1000
    exported = data(
        call(
            client,
            "export_draft",
            {
                "draft_id": draft["draft_id"],
                "expected_revision": 3,
            },
        )
    )
    with pymupdf.open(
        stream=client.get(exported["download_url"]).content, filetype="pdf"
    ) as output:
        assert len(output) == 3
        assert "Sample page 1" in output[0].get_text()
        assert "Again 3" in output[2].get_text()
        assert "Sample page 3" not in output[2].get_text()
    with pymupdf.open(stream=sample_pdf.read_bytes(), filetype="pdf") as source:
        assert "Sample page 3" in source[2].get_text()


def test_auth_and_owner_isolation_on_rest_and_mcp(client, sample_pdf, monkeypatch):
    monkeypatch.setattr(
        settings, "api_keys", {"alice": "alice-test-token", "bob": "bob-test-token"}
    )
    alice = {"Authorization": "Bearer alice-test-token"}
    bob = {"Authorization": "Bearer bob-test-token"}
    assert client.get("/health").status_code == 200
    assert client.post("/api/v1/documents").status_code == 401
    assert client.post("/mcp/", json={}).status_code == 401
    document = upload(client, sample_pdf, alice)
    assert (
        data(call(client, "get_document_info", {"document_id": document["id"]}, alice))[
            "page_count"
        ]
        == 3
    )
    assert call(client, "get_document_info", {"document_id": document["id"]}, bob)["isError"]
    assert (
        client.get(f"/api/v1/documents/{document['id']}/pages/0/content", headers=bob).status_code
        == 404
    )
    exported = data(
        call(
            client,
            "extract_pages",
            {
                "document_id": document["id"],
                "page_numbers": [3],
            },
            alice,
        )
    )
    assert client.get(exported["download_url"]).status_code == 401
    assert client.get(exported["download_url"], headers=bob).status_code == 404
    assert client.get(exported["download_url"], headers=alice).status_code == 200
    assert call(client, "import_export", {"export_id": exported["id"]}, bob)["isError"]
    draft = data(call(client, "create_draft", {"document_id": document["id"]}, alice))
    assert call(client, "get_draft", {"draft_id": draft["draft_id"]}, bob)["isError"]


def test_draft_persists_and_compare_and_swap_is_atomic(client, sample_pdf):
    document = upload(client, sample_pdf)
    editor = get_editor_service()
    draft = editor.create_draft(AgentExportRequest(pages=editor.page_specs(document["id"])))
    assert get_editor_service().get_draft(draft["draft_id"]) == draft
    request = AgentExportRequest.model_validate(draft["request"])

    def update():
        try:
            return get_editor_service().update_draft(draft["draft_id"], 1, request)["revision"]
        except Exception as error:
            return getattr(error, "status_code", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: update(), range(2)))
    assert sorted(results) == [2, 409]
    assert editor.get_draft(draft["draft_id"])["revision"] == 2


def test_merge_reorder_compose_and_dns_rebinding(client, sample_pdf):
    document = upload(client, sample_pdf)
    merged = data(
        call(client, "merge_documents", {"document_ids": [document["id"], document["id"]]})
    )
    assert merged["page_count"] == 6
    reordered = data(
        call(
            client,
            "reorder_pages",
            {
                "document_id": document["id"],
                "page_numbers": [3, 1, 3],
            },
        )
    )
    with pymupdf.open(
        stream=client.get(reordered["download_url"]).content, filetype="pdf"
    ) as output:
        assert [page.get_text().strip() for page in output] == [
            "Sample page 3",
            "Sample page 1",
            "Sample page 3",
        ]
    composed = data(
        call(
            client,
            "compose_pdf",
            {
                "request": {
                    "pages": [
                        {
                            "kind": "source",
                            "document_id": document["id"],
                            "page_number": 1,
                            "rotation": 90,
                            "crop": {"left": 0.1, "right": 0.1},
                        },
                        {"kind": "blank", "width": 100, "height": 200},
                    ]
                }
            },
        )
    )
    with pymupdf.open(
        stream=client.get(composed["download_url"]).content, filetype="pdf"
    ) as output:
        assert len(output) == 2
        assert round(output[0].rect.height) == 240
    blocked = client.post("/mcp/", headers={"Host": "attacker.example"}, json={})
    assert blocked.status_code == 421


def test_find_text_cross_span_and_multiple_matches(client, tmp_path):
    path = tmp_path / "styled.pdf"
    with pymupdf.open() as source:
        page = source.new_page()
        page.insert_text((40, 60), "HELLO", fontname="helv", fontsize=12)
        page.insert_text((79.35, 60), " WORLD", fontname="hebo", fontsize=12)
        page.insert_text((40, 100), "HELLO WORLD", fontname="helv", fontsize=12)
        source.save(path)
    document = upload(client, path)
    found = data(call(client, "find_text", {"document_id": document["id"], "query": "hello world"}))
    assert len(found["matches"]) == 2
    assert len(found["matches"][0]["spans"]) == 2
    limited = data(
        call(
            client,
            "find_text",
            {
                "document_id": document["id"],
                "query": "hello",
                "max_results": 1,
            },
        )
    )
    assert len(limited["matches"]) == 1 and limited["limit_reached"]


def test_mcp_can_be_disabled(tmp_path, monkeypatch):
    from app.main import create_app

    monkeypatch.setattr(settings, "mcp_enabled", False)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/mcp/").status_code == 404


def test_download_resource_link_with_asgi_proxy_prefix(client, sample_pdf):
    from app.main import create_app

    # Simulate a deployment with --root-path /pdf-editor after the proxy strips the prefix.
    with TestClient(create_app(), root_path="/pdf-editor") as prefixed:
        document = upload(prefixed, sample_pdf)
        # Uvicorn includes root_path in scope.path; reproduce that in TestClient.
        response = prefixed.post(
            "/pdf-editor/mcp/",
            headers={"Host": "localhost", "Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "extract_pages",
                    "arguments": {
                        "document_id": document["id"],
                        "page_numbers": [3],
                    },
                },
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        artifact = data(result)
        assert artifact["download_url"].startswith("http://localhost/pdf-editor/api/v1/exports/")
        link = next(item for item in result["content"] if item["type"] == "resource_link")
        assert link["uri"] == artifact["files"][0]["file_url"]
        assert prefixed.get(link["uri"]).status_code == 200
        assert link["mimeType"] == "application/pdf"


def test_public_download_origin_and_mcp_host(client, sample_pdf, monkeypatch):
    from app.main import create_app

    monkeypatch.setattr(settings, "public_base_url", "https://pdf.example/pdf-editor")
    with TestClient(create_app()) as public:
        document = upload(public, sample_pdf)
        result = call(
            public,
            "extract_pages",
            {
                "document_id": document["id"],
                "page_numbers": [3],
            },
            {"Host": "pdf.example"},
        )
        assert data(result)["download_url"].startswith("https://pdf.example/pdf-editor/api/v1/")
