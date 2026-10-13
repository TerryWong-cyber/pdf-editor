"""Live SDK smoke: upload -> discovery -> extract -> draft edit -> preview -> export.

Run against a disposable backend data directory. This creates source/export/draft artifacts.
Use PDF_EDITOR_SMOKE_TOKEN for authenticated instances; the token is never printed.
"""

import argparse
import asyncio
import base64
import json
import os
from io import BytesIO

import httpx
import pymupdf
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from PIL import Image


async def smoke(base_url: str) -> None:
    headers = {}
    if token := os.environ.get("PDF_EDITOR_SMOKE_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    source = pymupdf.open()
    for number in range(1, 4):
        page = source.new_page(width=300, height=400)
        page.insert_text((40, 60), f"Sample page {number}", fontsize=18)
    source_bytes = source.tobytes()
    source.close()
    async with httpx.AsyncClient(headers=headers, timeout=30) as http:
        upload = await http.post(
            base_url + "/api/v1/documents",
            files={"files": ("sdk-smoke.pdf", source_bytes, "application/pdf")},
        )
        upload.raise_for_status()
        document_id = upload.json()[0]["id"]
        async with streamablehttp_client(base_url + "/mcp/", headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()

                async def call(name, args):
                    result = await session.call_tool(name, args)
                    assert not result.isError, result
                    return result

                result = await call(
                    "extract_pages",
                    {
                        "document_id": document_id,
                        "page_numbers": [3],
                        "filename": "third.pdf",
                    },
                )
                extracted = result.structuredContent
                assert any(block.type == "resource_link" for block in result.content)
                download_url = extracted["download_url"]
                if download_url.startswith("/"):
                    download_url = base_url + download_url
                response = await http.get(download_url)
                response.raise_for_status()
                with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
                    assert len(pdf) == 1 and "Sample page 3" in pdf[0].get_text()
                search = (
                    await call(
                        "find_text",
                        {
                            "document_id": document_id,
                            "query": "Sample page 3",
                        },
                    )
                ).structuredContent
                match = search["matches"][0]
                span = match["spans"][0]
                draft = (await call("create_draft", {"document_id": document_id})).structuredContent
                edited = (
                    await call(
                        "edit_draft_text",
                        {
                            "draft_id": draft["draft_id"],
                            "expected_revision": 1,
                            "replacement": {
                                "document_id": document_id,
                                "page_number": 3,
                                "block_id": match["block_id"],
                                "span_id": span["span_id"],
                                "expected_text": span["text"],
                                "new_text": "SDK edited",
                            },
                        },
                    )
                ).structuredContent
                preview = await call(
                    "preview_draft_page",
                    {
                        "draft_id": draft["draft_id"],
                        "expected_revision": edited["revision"],
                        "page_number": 3,
                    },
                )
                with Image.open(BytesIO(base64.b64decode(preview.content[0].data))) as image:
                    assert max(image.size) == 1000
                exported = (
                    await call(
                        "export_draft",
                        {
                            "draft_id": draft["draft_id"],
                            "expected_revision": edited["revision"],
                        },
                    )
                ).structuredContent
                download_url = exported["download_url"]
                if download_url.startswith("/"):
                    download_url = base_url + download_url
                response = await http.get(download_url)
                response.raise_for_status()
                with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
                    assert len(pdf) == 3 and "SDK edited" in pdf[2].get_text()
                original = (
                    await call(
                        "get_page_content",
                        {
                            "document_id": document_id,
                            "page_number": 3,
                        },
                    )
                ).structuredContent
                assert "Sample page 3" in original["blocks"][0]["text"]
                print(
                    json.dumps(
                        {
                            "status": "passed",
                            "tool_count": len(tools.tools),
                            "extracted_page_count": extracted["page_count"],
                            "draft_revision": edited["revision"],
                            "preview": "PNG 1000px",
                            "source_unchanged": True,
                            "authenticated": bool(headers),
                        }
                    )
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    arguments = parser.parse_args()
    asyncio.run(smoke(arguments.url.rstrip("/")))
