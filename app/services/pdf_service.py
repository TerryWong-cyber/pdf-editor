from collections.abc import Iterable
from io import BytesIO
from math import cos, radians, sin
from pathlib import Path

import pymupdf
from fastapi import HTTPException, status
from PIL import Image, ImageEnhance, UnidentifiedImageError

from app.schemas import (
    BlankPage,
    DocumentMetadata,
    ExportRequest,
    PageMetadata,
    SourcePage,
    WatermarkMetadata,
    WatermarkSpec,
)
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

    def create_watermark(
        self,
        content: bytes,
        filename: str,
        destination: Path,
        watermark_id: str,
    ) -> WatermarkMetadata:
        try:
            with Image.open(BytesIO(content)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("unsupported watermark image format")
                normalized = image.convert("RGBA")
                normalized.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
                normalized.save(destination, format="PNG", optimize=True)
                width, height = normalized.size
        except (ValueError, UnidentifiedImageError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="watermark must be a valid PNG, JPEG, or WebP image",
            ) from exc
        return WatermarkMetadata(
            id=watermark_id,
            filename=filename,
            width=width,
            height=height,
            preview_url=f"/api/v1/watermarks/{watermark_id}/preview",
        )

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

    def _apply_watermark(self, page: pymupdf.Page, spec: WatermarkSpec | None) -> None:
        if spec is None:
            return
        watermark_path = self.storage.watermark_path(spec.watermark_id)
        try:
            with Image.open(watermark_path) as source:
                image = source.convert("RGBA")
                alpha = image.getchannel("A")
                image.putalpha(ImageEnhance.Brightness(alpha).enhance(spec.opacity))
                base_width = page.rect.width * spec.scale
                base_height = base_width * image.height / image.width
                if spec.rotation:
                    image = image.rotate(
                        -spec.rotation,
                        expand=True,
                        resample=Image.Resampling.BICUBIC,
                    )
                angle = radians(spec.rotation)
                target_width = abs(base_width * cos(angle)) + abs(base_height * sin(angle))
                target_height = abs(base_width * sin(angle)) + abs(base_height * cos(angle))
                center_x = page.rect.width * spec.x
                center_y = page.rect.height * spec.y
                target = pymupdf.Rect(
                    center_x - target_width / 2,
                    center_y - target_height / 2,
                    center_x + target_width / 2,
                    center_y + target_height / 2,
                )
                buffer = BytesIO()
                image.save(buffer, format="PNG", optimize=True)
                page.insert_image(
                    target,
                    stream=buffer.getvalue(),
                    overlay=True,
                    keep_proportion=False,
                )
        except (UnidentifiedImageError, OSError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"watermark {spec.watermark_id} could not be rendered",
            ) from exc

    def compose(self, request: ExportRequest, destination: Path) -> None:
        output = pymupdf.open()
        open_sources: dict[str, pymupdf.Document] = {}
        try:
            for spec in request.pages:
                if isinstance(spec, BlankPage):
                    width, height = spec.width, spec.height
                    if spec.rotation in (90, 270):
                        width, height = height, width
                    target_page = output.new_page(width=width, height=height)
                    self._apply_watermark(target_page, spec.watermark)
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
                self._apply_watermark(target_page, spec.watermark)

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
            if spec.watermark is not None:
                self.storage.watermark_path(spec.watermark.watermark_id)
