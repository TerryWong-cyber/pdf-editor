from collections.abc import Iterable
from pathlib import Path

import pymupdf
from fastapi import HTTPException, status

from app.schemas import BlankPage, DocumentMetadata, ExportRequest, PageMetadata, SourcePage
from app.services.storage import FileStorage

PDF_SIGNATURE = b"%PDF-"


class PdfService:
    def __init__(self, storage: FileStorage) -> None:
        self.storage = storage

    @staticmethod
    def _open_pdf(path: Path) -> pymupdf.Document:
        try:
            document = pymupdf.open(path)
            if not document.is_pdf or document.needs_pass:
                document.close()
                raise ValueError("encrypted or non-PDF document")
            return document
        except (pymupdf.FileDataError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="invalid, damaged, or password-protected PDF",
            ) from exc

    def inspect(self, document_id: str, path: Path, filename: str) -> DocumentMetadata:
        with self._open_pdf(path) as document:
            pages = [
                PageMetadata(
                    index=index,
                    width=round(page.rect.width, 2),
                    height=round(page.rect.height, 2),
                    rotation=page.rotation,
                )
                for index, page in enumerate(document)
            ]
        return DocumentMetadata(
            id=document_id,
            filename=filename,
            page_count=len(pages),
            pages=pages,
        )

    def validate_signature(self, path: Path) -> None:
        with path.open("rb") as stream:
            if stream.read(len(PDF_SIGNATURE)) != PDF_SIGNATURE:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="uploaded file is not a PDF",
                )

    def render_preview(self, document_id: str, page_index: int, scale: float) -> bytes:
        path = self.storage.original_path(document_id)
        with self._open_pdf(path) as document:
            if page_index >= document.page_count:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="page not found")
            page = document[page_index]
            matrix = pymupdf.Matrix(scale, scale)
            return page.get_pixmap(matrix=matrix, alpha=False).tobytes("png")

    @staticmethod
    def _crop_rect(page: pymupdf.Page, spec: SourcePage) -> pymupdf.Rect:
        rect = page.rect
        if spec.crop is None:
            return rect
        crop = spec.crop
        return pymupdf.Rect(
            rect.x0 + rect.width * crop.left,
            rect.y0 + rect.height * crop.top,
            rect.x1 - rect.width * crop.right,
            rect.y1 - rect.height * crop.bottom,
        )

    def compose(self, request: ExportRequest, destination: Path) -> None:
        output = pymupdf.open()
        open_sources: dict[str, pymupdf.Document] = {}
        try:
            for spec in request.pages:
                if isinstance(spec, BlankPage):
                    width, height = spec.width, spec.height
                    if spec.rotation in (90, 270):
                        width, height = height, width
                    output.new_page(width=width, height=height)
                    continue

                source = open_sources.get(spec.document_id)
                if source is None:
                    source = self._open_pdf(self.storage.original_path(spec.document_id))
                    open_sources[spec.document_id] = source
                if spec.page_index >= source.page_count:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail=f"page {spec.page_index} does not exist in {spec.document_id}",
                    )

                source_page = source[spec.page_index]
                clip = self._crop_rect(source_page, spec)
                width, height = clip.width, clip.height
                if spec.rotation in (90, 270):
                    width, height = height, width
                target_page = output.new_page(width=width, height=height)
                target_page.show_pdf_page(
                    target_page.rect,
                    source,
                    spec.page_index,
                    clip=clip,
                    rotate=spec.rotation,
                    keep_proportion=False,
                )

            output.set_metadata({"producer": "PDF Editor", "title": request.filename})
            output.save(destination, garbage=4, deflate=True)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            output.close()
            for source in open_sources.values():
                source.close()

    def validate_sources(self, specs: Iterable[SourcePage | BlankPage]) -> None:
        for spec in specs:
            if isinstance(spec, SourcePage):
                self.storage.original_path(spec.document_id)
