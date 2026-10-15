"""Application workflows shared by HTTP routes and MCP tools."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import HTTPException, UploadFile

from app.agent_schemas import AgentExportRequest, AgentSourcePage, SpanReplacement
from app.config import settings
from app.download_links import signed_export_url
from app.schemas import (
    DocumentMetadata,
    ExportRequest,
    ExportResponse,
    FileArtifact,
    SourcePage,
    TextEdit,
    TextRunEdit,
)
from app.security import principal
from app.services.pdf_service import PdfService
from app.services.storage import FileStorage, safe_pdf_filename


class EditorService:
    def __init__(self, storage: FileStorage, pdf: PdfService) -> None:
        self.storage = storage
        self.pdf = pdf
        with self.database() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, metadata TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS drafts "
                "(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, request TEXT NOT NULL)"
            )

    @contextmanager
    def database(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.storage.root / "editor.sqlite3", timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def upload(self, upload: UploadFile) -> DocumentMetadata:
        filename = safe_pdf_filename(upload.filename or "document.pdf")
        document_id, path = await self.storage.save_original(
            upload, settings.max_upload_mb * 1024 * 1024
        )
        try:
            self.pdf.validate_signature(path)
            metadata = self.pdf.inspect(document_id, path, filename)
            with self.database() as connection:
                connection.execute(
                    "INSERT INTO documents VALUES (?, ?)",
                    (document_id, metadata.model_dump_json()),
                )
            return metadata
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def document(self, document_id: str) -> DocumentMetadata:
        path = self.storage.original_path(document_id)
        with self.database() as connection:
            row = connection.execute(
                "SELECT metadata FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
        if row:
            return DocumentMetadata.model_validate_json(row[0])
        # Existing uploads remain usable after upgrading; original filename was not persisted.
        return self.pdf.inspect(document_id, path, "document.pdf")

    async def import_export(self, export_id: str, filename: str) -> DocumentMetadata:
        path = self.storage.export_path(export_id)
        if path.stat().st_size > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "export exceeds document import size limit")
        return await self.upload(UploadFile(filename=filename, file=BytesIO(path.read_bytes())))

    @staticmethod
    def rest_request(request: AgentExportRequest) -> ExportRequest:
        pages = []
        for page in request.pages:
            if isinstance(page, AgentSourcePage):
                payload = page.model_dump(exclude={"page_number"})
                pages.append(SourcePage(**payload, page_index=page.page_number - 1))
            else:
                pages.append(page)
        return ExportRequest(filename=request.filename, pages=pages)

    def validate(self, request: AgentExportRequest) -> None:
        rest = self.rest_request(request)
        self.pdf.validate_sources(rest.pages)
        for page in request.pages:
            if isinstance(page, AgentSourcePage):
                metadata = self.document(page.document_id)
                if page.page_number > metadata.page_count:
                    raise HTTPException(422, f"page {page.page_number} does not exist")

    def export(self, request: ExportRequest) -> ExportResponse:
        self.pdf.validate_sources(request.pages)
        export_id, destination = self.storage.allocate_export()
        filename = safe_pdf_filename(request.filename, "edited-copy.pdf")
        results = self.pdf.compose(request.model_copy(update={"filename": filename}), destination)
        url, expires = signed_export_url(export_id, filename, principal.get())
        return ExportResponse(
            id=export_id,
            filename=filename,
            page_count=len(request.pages),
            download_url=(
                f"/api/v1/exports/{export_id}/download?{urlencode({'filename': filename})}"
            ),
            files=[FileArtifact(
                file_url=url, filename=filename, mime_type="application/pdf",
                size_bytes=destination.stat().st_size, expires_at=expires,
            )],
            text_edit_results=results,
        )

    def agent_export(self, request: AgentExportRequest) -> dict:
        self.validate(request)
        result = self.export(self.rest_request(request)).model_dump()
        if result["download_url"].startswith("/"):
            result["download_url"] = settings.public_base_url.rstrip("/") + result["download_url"]
        result["source_unchanged"] = True
        result["can_import_as_document"] = True
        result["warnings"] = [
            "Page composition does not guarantee preservation of links, bookmarks, "
            "forms or annotations."
        ]
        return result

    def page_specs(self, document_id: str, page_numbers: list[int] | None = None):
        metadata = self.document(document_id)
        numbers = (
            page_numbers if page_numbers is not None else list(range(1, metadata.page_count + 1))
        )
        return [AgentSourcePage(document_id=document_id, page_number=number) for number in numbers]

    def content(self, document_id: str, page_number: int, detail: str) -> dict:
        content = self.pdf.extract_page_content(document_id, page_number - 1)
        payload = content.model_dump()
        payload.pop("page_index")
        payload["page_number"] = page_number
        payload["coordinate_units"] = "PDF points (1/72 inch); origin top-left"
        for block in payload["blocks"]:
            if detail == "summary":
                block.pop("lines")
            elif detail == "spans":
                for line in block["lines"]:
                    for span in line["spans"]:
                        span.pop("characters")
        payload["text_available"] = bool(content.blocks)
        payload["ocr_available"] = False
        return payload

    def find_text(
        self,
        document_id: str,
        query: str,
        page_numbers: list[int] | None,
        case_sensitive: bool,
        max_results: int,
    ) -> dict:
        # Search each line's concatenated spans, so mixed fonts do not break matches.
        pages = self.page_specs(document_id, page_numbers)
        if len(pages) > 100:
            raise HTTPException(422, "search at most 100 pages per call; provide page_numbers")
        needle = query if case_sensitive else query.lower()
        matches = []
        for page in pages:
            content = self.pdf.extract_page_content(document_id, page.page_number - 1)
            for block in content.blocks:
                for line in block.lines:
                    text = "".join(span.text for span in line.spans)
                    haystack = text if case_sensitive else text.lower()
                    # Unicode lower() can expand code points; keep offsets trustworthy.
                    if len(haystack) != len(text) or len(needle) != len(query):
                        raise HTTPException(422, "use case_sensitive=true for this Unicode text")
                    start = 0
                    while (offset := haystack.find(needle, start)) >= 0:
                        end = offset + len(needle)
                        spans = []
                        cursor = 0
                        for span in line.spans:
                            span_end = cursor + len(span.text)
                            if offset < span_end and end > cursor:
                                spans.append(
                                    {
                                        "span_id": span.id,
                                        "text": span.text,
                                        "start": max(0, offset - cursor),
                                        "end": min(len(span.text), end - cursor),
                                        "bbox": span.bbox.model_dump(),
                                    }
                                )
                            cursor = span_end
                        matches.append(
                            {
                                "document_id": document_id,
                                "page_number": page.page_number,
                                "block_id": block.id,
                                "line_id": line.id,
                                "context": text[max(0, offset - 80) : end + 80],
                                "spans": spans,
                            }
                        )
                        if len(matches) >= max_results:
                            return {"matches": matches, "limit_reached": True}
                        start = end
        return {"matches": matches, "limit_reached": False}

    def replace_span(self, request: AgentExportRequest, replacement: SpanReplacement) -> None:
        matching_pages = [
            page
            for page in request.pages
            if isinstance(page, AgentSourcePage)
            and page.document_id == replacement.document_id
            and page.page_number == replacement.page_number
        ]
        if len(matching_pages) != 1:
            raise HTTPException(
                409, "source page must appear exactly once; use update_draft for copies"
            )
        page = matching_pages[0]
        content = self.pdf.extract_page_content(page.document_id, page.page_number - 1)
        source = next(
            (
                span
                for block in content.blocks
                if block.id == replacement.block_id
                for line in block.lines
                for span in line.spans
                if span.id == replacement.span_id
            ),
            None,
        )
        if source is None:
            raise HTTPException(404, "span not found in specified block and page")
        block_edit = next(
            (edit for edit in page.text_edits if edit.block_id == replacement.block_id), None
        )
        if block_edit is not None and not block_edit.runs:
            raise HTTPException(409, "block-level edit already exists; use update_draft")
        existing = (
            next((run for run in block_edit.runs if run.span_id == replacement.span_id), None)
            if block_edit
            else None
        )
        current_text = existing.text if existing else source.text
        if current_text != replacement.expected_text:
            raise HTTPException(
                409, "expected_text differs from current span; read content/draft again"
            )
        if block_edit is None:
            block_edit = TextEdit(
                block_id=replacement.block_id,
                runs=[
                    TextRunEdit(
                        span_id=replacement.span_id,
                        text=replacement.new_text,
                        overflow_policy=replacement.overflow_policy,
                        minimum_font_scale=replacement.minimum_font_scale,
                    )
                ],
            )
            page.text_edits.append(block_edit)
        elif existing:
            existing.text = replacement.new_text
            existing.overflow_policy = replacement.overflow_policy
            existing.minimum_font_scale = replacement.minimum_font_scale
            existing.segments = []
        else:
            block_edit.runs.append(
                TextRunEdit(
                    span_id=replacement.span_id,
                    text=replacement.new_text,
                    overflow_policy=replacement.overflow_policy,
                    minimum_font_scale=replacement.minimum_font_scale,
                )
            )

    def create_draft(self, request: AgentExportRequest) -> dict:
        self.validate(request)
        draft_id = str(uuid4())
        with self.database() as connection:
            connection.execute(
                "INSERT INTO drafts VALUES (?, 1, ?)", (draft_id, request.model_dump_json())
            )
        return {"draft_id": draft_id, "revision": 1, "request": request.model_dump()}

    def get_draft(self, draft_id: str) -> dict:
        draft_id = self.storage._validated_id(draft_id)
        with self.database() as connection:
            row = connection.execute(
                "SELECT revision, request FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(404, "draft not found")
        return {"draft_id": draft_id, "revision": row[0], "request": json.loads(row[1])}

    def draft_request(self, draft_id: str, expected_revision: int) -> AgentExportRequest:
        draft = self.get_draft(draft_id)
        if draft["revision"] != expected_revision:
            raise HTTPException(409, "draft revision conflict; read get_draft before retrying")
        return AgentExportRequest.model_validate(draft["request"])

    def update_draft(
        self,
        draft_id: str,
        expected_revision: int,
        request: AgentExportRequest,
    ) -> dict:
        self.validate(request)
        with self.database() as connection:
            cursor = connection.execute(
                "UPDATE drafts SET revision = revision + 1, request = ? "
                "WHERE id = ? AND revision = ?",
                (request.model_dump_json(), draft_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise HTTPException(409, "draft revision conflict; read get_draft before retrying")
        # Return the exact committed revision, not a later concurrent update.
        return {
            "draft_id": draft_id,
            "revision": expected_revision + 1,
            "request": request.model_dump(),
        }
