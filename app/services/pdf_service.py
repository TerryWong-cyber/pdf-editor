from collections.abc import Iterable
from io import BytesIO
from math import atan2, cos, degrees, hypot, radians, sin
from pathlib import Path
from statistics import median
from typing import Any

import pymupdf
from fastapi import HTTPException, status
from PIL import Image, ImageEnhance, UnidentifiedImageError

from app.schemas import (
    BlankPage,
    DocumentMetadata,
    ExportRequest,
    ImageAssetMetadata,
    ImageBlock,
    ImageEdit,
    PageContent,
    PageMetadata,
    SourcePage,
    TextBlock,
    TextCharacter,
    TextEdit,
    TextEditResult,
    TextLine,
    TextRunEdit,
    TextSpan,
    TextStyleSegment,
    WatermarkMetadata,
    WatermarkSpec,
)
from app.services.font_resolver import FontResolver, ResolvedFont
from app.services.storage import FileStorage

PDF_SIGNATURE = b"%PDF-"
BROWSER_FONT_EXTENSIONS = {"otf", "ttf", "woff", "woff2"}


class PdfService:
    def __init__(self, storage: FileStorage, font_resolver: FontResolver) -> None:
        self.storage = storage
        self.font_resolver = font_resolver

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

    @staticmethod
    def _rotate_image_for_rect(
        image_bytes: bytes,
        rect: pymupdf.Rect,
        rotation: float,
    ) -> tuple[bytes, pymupdf.Rect]:
        """Stretch to the page box, then rotate the pixels around its center."""
        if abs(rotation) <= 0.01:
            return image_bytes, rect
        try:
            with Image.open(BytesIO(image_bytes)) as source:
                width_points = max(1.0, abs(rect.width))
                height_points = max(1.0, abs(rect.height))
                density = max(
                    2.0,
                    min(4.0, source.width / width_points, source.height / height_points),
                )
                density = min(density, 8192 / max(width_points, height_points))
                stretched = source.convert("RGBA").resize(
                    (
                        max(1, round(width_points * density)),
                        max(1, round(height_points * density)),
                    ),
                    Image.Resampling.LANCZOS,
                )
                rotated = stretched.rotate(
                    -rotation,
                    resample=Image.Resampling.BICUBIC,
                    expand=True,
                )
                output = BytesIO()
                rotated.save(output, format="PNG")
        except (UnidentifiedImageError, OSError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="edited image cannot be rotated",
            ) from exc

        angle = radians(rotation)
        rotated_width = abs(rect.width * cos(angle)) + abs(rect.height * sin(angle))
        rotated_height = abs(rect.width * sin(angle)) + abs(rect.height * cos(angle))
        center = (rect.tl + rect.br) / 2
        return output.getvalue(), pymupdf.Rect(
            center.x - rotated_width / 2,
            center.y - rotated_height / 2,
            center.x + rotated_width / 2,
            center.y + rotated_height / 2,
        )

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

    def render_edit_background(
        self,
        document_id: str,
        page_index: int,
        scale: float,
        *,
        remove_text: bool = True,
        remove_images: bool = False,
    ) -> bytes:
        path = self.storage.original_path(document_id)
        with self._open_pdf(path) as source:
            if page_index >= source.page_count:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="page not found")
            background = pymupdf.open()
            try:
                background.insert_pdf(source, from_page=page_index, to_page=page_index)
                page = background[0]
                if remove_text:
                    for block in page.get_text("dict").get("blocks", []):
                        if block.get("type") == 0 and block.get("lines"):
                            page.add_redact_annot(
                                pymupdf.Rect(block["bbox"]),
                                fill=False,
                                cross_out=False,
                            )
                    page.apply_redactions(images=0, graphics=0, text=0)
                if remove_images:
                    for image in page.get_image_info(xrefs=True):
                        page.add_redact_annot(
                            pymupdf.Rect(image["bbox"]),
                            fill=False,
                            cross_out=False,
                        )
                    page.apply_redactions(images=2, graphics=0, text=0)
                return page.get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale),
                    alpha=False,
                ).tobytes("png")
            finally:
                background.close()

    @staticmethod
    def _font_key(font_name: str) -> str:
        key = "".join(
            character
            for character in font_name.split("+")[-1].lower()
            if character.isalnum()
        )
        for suffix in ("regular", "roman", "book", "psmt", "mt", "ps"):
            if key.endswith(suffix):
                return key[: -len(suffix)]
        return key

    def extract_font(self, document_id: str, font_xref: int) -> tuple[bytes, str]:
        path = self.storage.original_path(document_id)
        with self._open_pdf(path) as document:
            available_xrefs = {
                font[0]
                for page in document
                for font in page.get_fonts(full=True)
                if font[0] > 0
            }
            if font_xref not in available_xrefs:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="font not found")
            _name, extension, _font_type, content = document.extract_font(font_xref)
            if not content or extension not in BROWSER_FONT_EXTENSIONS:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="font is not embedded in a browser-compatible format",
                )
            return content, extension

    @staticmethod
    def _rect_payload(rect: pymupdf.Rect) -> dict[str, float]:
        return {
            "x0": round(rect.x0, 3),
            "y0": round(rect.y0, 3),
            "x1": round(rect.x1, 3),
            "y1": round(rect.y1, 3),
        }

    @staticmethod
    def _point_payload(point: pymupdf.Point) -> dict[str, float]:
        return {"x": round(point.x, 3), "y": round(point.y, 3)}

    @staticmethod
    def _display_rect(page: pymupdf.Page, value: Any) -> pymupdf.Rect:
        rect = pymupdf.Rect(value)
        return rect * page.rotation_matrix if page.rotation else rect

    @staticmethod
    def _display_point(page: pymupdf.Page, value: Any) -> pymupdf.Point:
        point = pymupdf.Point(value)
        return point * page.rotation_matrix if page.rotation else point

    @staticmethod
    def _display_direction(page: pymupdf.Page, value: Any) -> pymupdf.Point:
        direction = pymupdf.Point(value)
        if page.rotation:
            start = pymupdf.Point(0, 0) * page.rotation_matrix
            end = direction * page.rotation_matrix
            direction = end - start
        length = hypot(direction.x, direction.y)
        if length == 0:
            return pymupdf.Point(1, 0)
        return pymupdf.Point(direction.x / length, direction.y / length)

    def _span_advance(
        self,
        raw_span: dict[str, Any],
        direction: Any,
    ) -> float:
        characters = raw_span.get("chars", [])
        if not characters:
            return 0.0
        # PDF text operators may add character spacing, word spacing, or horizontal
        # scaling. Font metrics alone lose those adjustments, so measure the visual
        # advance from the source glyph geometry instead.
        unit = pymupdf.Point(direction)
        length = hypot(unit.x, unit.y)
        if length == 0:
            unit = pymupdf.Point(1, 0)
        else:
            unit = pymupdf.Point(unit.x / length, unit.y / length)
        origin = pymupdf.Point(raw_span["origin"])
        start = origin.x * unit.x + origin.y * unit.y
        trailing_edges = []
        for character in characters:
            rect = pymupdf.Rect(character["bbox"])
            trailing_edges.extend(
                point.x * unit.x + point.y * unit.y
                for point in (rect.tl, rect.tr, rect.bl, rect.br)
            )
        return max(0.0, max(trailing_edges) - start)

    def _page_font_map(self, page: pymupdf.Page) -> dict[str, list[tuple[int, str]]]:
        font_map: dict[str, list[tuple[int, str]]] = {}
        for font in page.get_fonts(full=True):
            xref, extension, _font_type, base_font = font[:4]
            if xref > 0:
                font_map.setdefault(self._font_key(base_font), []).append((xref, extension))
        for candidates in font_map.values():
            candidates.sort(
                key=lambda candidate: (
                    candidate[1].lower() not in BROWSER_FONT_EXTENSIONS,
                    candidate[0],
                )
            )
        return font_map

    def _page_images(self, page: pymupdf.Page) -> list[tuple[str, dict[str, Any]]]:
        return [
            (f"image-{index}", image)
            for index, image in enumerate(page.get_image_info(xrefs=True))
            if pymupdf.Rect(image["bbox"]).get_area() > 0
        ]

    def _image_info(
        self,
        page: pymupdf.Page,
        image_id: str,
    ) -> dict[str, Any]:
        image = dict(self._page_images(page)).get(image_id)
        if image is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="image not found")
        return image

    @staticmethod
    def _pixmap_png(document: pymupdf.Document, page: pymupdf.Page, image: dict[str, Any]) -> bytes:
        xref = int(image.get("xref", 0))
        if xref > 0:
            pixmap = pymupdf.Pixmap(document, xref)
            image_row = next((row for row in page.get_images(full=True) if row[0] == xref), None)
            smask = int(image_row[1]) if image_row else 0
            if smask > 0:
                mask = pymupdf.Pixmap(document, smask)
                try:
                    pixmap = pymupdf.Pixmap(pixmap, mask)
                finally:
                    mask = None
            if pixmap.colorspace and pixmap.colorspace.n > 3:
                pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
            return pixmap.tobytes("png")

        clip = pymupdf.Rect(image["bbox"])
        return page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip, alpha=True).tobytes("png")

    def extract_page_image(self, document_id: str, page_index: int, image_id: str) -> bytes:
        path = self.storage.original_path(document_id)
        with self._open_pdf(path) as document:
            if page_index >= document.page_count:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="page not found")
            page = document[page_index]
            image = self._image_info(page, image_id)
            return self._pixmap_png(document, page, image)

    def extract_page_content(self, document_id: str, page_index: int) -> PageContent:
        path = self.storage.original_path(document_id)
        with self._open_pdf(path) as document:
            if page_index >= document.page_count:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="page not found")
            page = document[page_index]
            font_map = self._page_font_map(page)
            blocks: list[TextBlock] = []
            for block_index, block in enumerate(page.get_text("rawdict").get("blocks", [])):
                if block.get("type") != 0:
                    continue
                lines: list[TextLine] = []
                spans: list[TextSpan] = []
                line_origins: list[pymupdf.Point] = []
                for line_index, raw_line in enumerate(block.get("lines", [])):
                    line_spans: list[TextSpan] = []
                    for span_index, raw_span in enumerate(raw_line.get("spans", [])):
                        raw_characters = raw_span.get("chars", [])
                        span_text = "".join(character.get("c", "") for character in raw_characters)
                        if not span_text and not raw_characters:
                            continue
                        font_name = str(raw_span.get("font", "sans-serif"))
                        font_candidates = font_map.get(self._font_key(font_name), [])
                        font_xrefs = list(
                            dict.fromkeys(xref for xref, _extension in font_candidates)
                        )
                        font_xref = font_xrefs[0] if font_xrefs else None
                        font_extension = font_candidates[0][1] if font_candidates else None
                        flags = int(raw_span.get("flags", 0))
                        span_origin = self._display_point(page, raw_span["origin"])
                        raw_direction = raw_line.get("dir", (1, 0))
                        characters = [
                            TextCharacter(
                                index=character_index,
                                text=str(character.get("c", "")),
                                bbox=self._rect_payload(
                                    self._display_rect(page, character["bbox"])
                                ),
                                origin=self._point_payload(
                                    self._display_point(page, character["origin"])
                                ),
                                synthetic=bool(character.get("synthetic", False)),
                            )
                            for character_index, character in enumerate(raw_characters)
                        ]
                        span = TextSpan(
                            id=f"block-{block_index}-line-{line_index}-span-{span_index}",
                            text=span_text,
                            bbox=self._rect_payload(self._display_rect(page, raw_span["bbox"])),
                            origin=self._point_payload(span_origin),
                            advance=round(
                                self._span_advance(
                                    raw_span,
                                    raw_direction,
                                ),
                                3,
                            ),
                            font_size=round(float(raw_span.get("size", 11)), 3),
                            font_name=font_name,
                            font_xref=font_xref,
                            font_xref_candidates=font_xrefs,
                            font_url=(
                                f"/api/v1/documents/{document_id}/fonts/{font_xref}"
                                if font_xref and font_extension in BROWSER_FONT_EXTENSIONS
                                else None
                            ),
                            color=f"#{int(raw_span.get('color', 0)) & 0xFFFFFF:06x}",
                            alpha=int(raw_span.get("alpha", 255)),
                            flags=flags,
                            char_flags=int(raw_span.get("char_flags", 0)),
                            bidi=int(raw_span.get("bidi", 0)),
                            font_weight=700 if flags & 16 else 400,
                            italic=bool(flags & 2),
                            serif=bool(flags & 4),
                            monospace=bool(flags & 8),
                            superscript=bool(flags & 1),
                            ascender=float(raw_span.get("ascender", 0)),
                            descender=float(raw_span.get("descender", 0)),
                            characters=characters,
                        )
                        line_spans.append(span)
                        spans.append(span)
                    if not line_spans:
                        continue
                    line_origin = pymupdf.Point(
                        line_spans[0].origin.x,
                        line_spans[0].origin.y,
                    )
                    line_origins.append(line_origin)
                    direction = self._display_direction(page, raw_line.get("dir", (1, 0)))
                    lines.append(
                        TextLine(
                            id=f"block-{block_index}-line-{line_index}",
                            text="".join(span.text for span in line_spans),
                            bbox=self._rect_payload(self._display_rect(page, raw_line["bbox"])),
                            direction=self._point_payload(direction),
                            writing_mode=int(raw_line.get("wmode", 0)),
                            spans=line_spans,
                        )
                    )
                text = "\n".join(line.text for line in lines).strip("\n")
                if not text.strip() or not spans:
                    continue
                bbox = self._display_rect(page, block["bbox"])
                dominant = max(spans, key=lambda span: len(span.text))
                line_gaps = [
                    hypot(current.x - previous.x, current.y - previous.y)
                    for previous, current in zip(line_origins, line_origins[1:], strict=False)
                    if current != previous
                ]
                font_size = dominant.font_size
                line_height = (
                    median(line_gaps) / font_size
                    if line_gaps
                    else max(1.0, min(2.0, bbox.height / font_size))
                )
                blocks.append(
                    TextBlock(
                        id=f"block-{block_index}",
                        bbox=self._rect_payload(bbox),
                        text=text,
                        font_size=round(font_size, 2),
                        color=dominant.color,
                        font_name=dominant.font_name,
                        font_xref=dominant.font_xref,
                        font_url=dominant.font_url,
                        font_weight=dominant.font_weight,
                        italic=dominant.italic,
                        line_height=round(max(0.7, min(4.0, line_height)), 3),
                        origin=spans[0].origin,
                        lines=lines,
                    )
                )
            images = [
                ImageBlock(
                    id=image_id,
                    bbox=self._rect_payload(self._display_rect(page, image["bbox"])),
                    width=int(image["width"]),
                    height=int(image["height"]),
                    xref=int(image.get("xref", 0)) or None,
                    bits_per_component=int(image.get("bpc", 0)),
                    colorspace=str(image.get("cs-name", "")),
                    has_mask=bool(image.get("has-mask", False)),
                    preview_url=(
                        f"/api/v1/documents/{document_id}/pages/{page_index}"
                        f"/images/{image_id}/preview"
                    ),
                )
                for image_id, image in self._page_images(page)
            ]
            return PageContent(
                document_id=document_id,
                page_index=page_index,
                width=round(page.rect.width, 3),
                height=round(page.rect.height, 3),
                blocks=blocks,
                images=images,
            )

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

    def create_image_asset(
        self,
        content: bytes,
        filename: str,
        destination: Path,
        image_id: str,
    ) -> ImageAssetMetadata:
        try:
            with Image.open(BytesIO(content)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("unsupported image format")
                normalized = image.convert("RGBA")
                normalized.thumbnail((8192, 8192), Image.Resampling.LANCZOS)
                normalized.save(destination, format="PNG", optimize=True)
                width, height = normalized.size
        except (ValueError, UnidentifiedImageError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="image must be a valid PNG, JPEG, or WebP file",
            ) from exc
        return ImageAssetMetadata(
            id=image_id,
            filename=filename,
            width=width,
            height=height,
            preview_url=f"/api/v1/images/{image_id}/preview",
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
        if spec.kind == "text":
            self._apply_text_watermark(page, spec)
            return
        assert spec.watermark_id is not None
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

    def _apply_text_watermark(self, page: pymupdf.Page, spec: WatermarkSpec) -> None:
        """Render a text watermark without requiring an uploaded image asset."""
        font = self.font_resolver.resolve(
            spec.text,
            0,
            preferred_name=spec.font_name,
            font_weight=spec.font_weight,
            italic=spec.italic,
        )
        self._register_font(page, font)
        font_size = spec.font_size * spec.scale
        metrics = self._font_metrics(font)
        lines = spec.text.splitlines() or [spec.text]
        line_height = font_size * 1.2
        text_height = line_height * len(lines)
        row_positions = (
            [(index + 1) / (spec.tile_rows + 1) for index in range(spec.tile_rows)]
            if spec.tiled
            else [spec.y]
        )
        color = self._pdf_color(spec.color)
        for center_y_fraction in row_positions:
            center_y = page.rect.y0 + page.rect.height * center_y_fraction
            start_y = center_y - text_height / 2
            for index, line in enumerate(lines):
                line_width = metrics.text_length(line, fontsize=font_size)
                baseline = pymupdf.Point(
                    page.rect.x0 + page.rect.width * spec.x - line_width / 2,
                    start_y + font_size + index * line_height,
                )
                morph = (
                    (pymupdf.Point(page.rect.x0 + page.rect.width * spec.x, center_y), pymupdf.Matrix(spec.rotation))
                    if abs(spec.rotation) > 0.01
                    else None
                )
                page.insert_text(
                    baseline,
                    line,
                    fontname=font.name,
                    fontfile=str(font.path) if font.path else None,
                    fontsize=font_size,
                    color=color,
                    fill_opacity=spec.opacity,
                    stroke_opacity=spec.opacity,
                    morph=morph,
                    overlay=True,
                )
                if spec.underline and line:
                    underline_y = baseline.y + font_size * 0.12
                    page.draw_line(
                        pymupdf.Point(baseline.x, underline_y),
                        pymupdf.Point(baseline.x + line_width, underline_y),
                        color=color,
                        width=max(0.45, font_size * 0.045),
                        morph=morph,
                        stroke_opacity=spec.opacity,
                        overlay=True,
                    )

    @staticmethod
    def _pdf_color(hex_color: str) -> tuple[float, float, float]:
        return tuple(int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5))

    @staticmethod
    def _source_point(page: pymupdf.Page, point: Any) -> pymupdf.Point:
        result = pymupdf.Point(point.x, point.y)
        return result * page.derotation_matrix if page.rotation else result

    @staticmethod
    def _source_direction(page: pymupdf.Page, direction: Any) -> pymupdf.Point:
        result = pymupdf.Point(direction.x, direction.y)
        if page.rotation:
            start = pymupdf.Point(0, 0) * page.derotation_matrix
            end = result * page.derotation_matrix
            result = end - start
        length = hypot(result.x, result.y)
        if length == 0:
            return pymupdf.Point(1, 0)
        return pymupdf.Point(result.x / length, result.y / length)

    @staticmethod
    def _font_metrics(font: ResolvedFont) -> pymupdf.Font:
        if font.buffer is not None:
            return pymupdf.Font(fontbuffer=font.buffer)
        if font.path is not None:
            return pymupdf.Font(fontfile=str(font.path))
        return pymupdf.Font(fontname=font.name)

    @staticmethod
    def _register_font(page: pymupdf.Page, font: ResolvedFont) -> None:
        if font.buffer is not None:
            page.insert_font(fontname=font.name, fontbuffer=font.buffer)

    def _resolve_edit_font(
        self,
        source: pymupdf.Document,
        page_index: int,
        text: str,
        font_xref: int | None,
        font_name: str,
        font_weight: int,
        italic: bool,
        slot: int,
    ) -> ResolvedFont:
        return self.font_resolver.resolve_embedded(
            source,
            page_index,
            font_xref,
            text,
            slot,
        ) or self.font_resolver.resolve(
            text,
            slot,
            preferred_name=font_name,
            font_weight=font_weight,
            italic=italic,
        )

    @staticmethod
    def _materialize_text_run(
        run: TextRunEdit,
        source_span: TextSpan,
        source_line: TextLine,
    ) -> TextRunEdit:
        payload = run.model_dump()
        defaults = {
            "bbox": source_span.bbox.model_dump(),
            "font_size": source_span.font_size,
            "color": source_span.color,
            "alpha": source_span.alpha,
            "font_name": source_span.font_name,
            "font_weight": source_span.font_weight,
            "italic": source_span.italic,
            "origin": source_span.origin.model_dump(),
            "direction": source_line.direction.model_dump(),
            "writing_mode": source_line.writing_mode,
            "source_advance": source_span.advance,
        }
        for field, value in defaults.items():
            if payload[field] is None:
                payload[field] = value
        if "font_xref" not in run.model_fields_set:
            payload["font_xref"] = source_span.font_xref
        return TextRunEdit.model_validate(payload)

    def _write_text_run(
        self,
        source: pymupdf.Document,
        page: pymupdf.Page,
        page_index: int,
        block_id: str,
        run: TextRunEdit,
        rect: pymupdf.Rect,
        slot: int,
    ) -> TextEditResult | None:
        if not run.text:
            return None
        if run.wrap or "\n" in run.text:
            return self._write_multiline_text_run(
                source,
                page,
                page_index,
                block_id,
                run,
                rect,
                slot,
            )
        if run.segments:
            return self._write_rich_text_run(
                source,
                page,
                page_index,
                block_id,
                run,
                rect,
                slot,
            )
        assert run.bbox is not None
        assert run.font_size is not None
        assert run.color is not None
        assert run.alpha is not None
        assert run.font_name is not None
        assert run.font_weight is not None
        assert run.italic is not None
        assert run.origin is not None
        assert run.direction is not None
        font = self._resolve_edit_font(
            source,
            page_index,
            run.text,
            run.font_xref,
            run.font_name,
            run.font_weight,
            run.italic,
            slot,
        )
        self._register_font(page, font)
        metrics = self._font_metrics(font)
        direction = self._source_direction(page, run.direction)
        available_length = run.source_advance or (
            abs(direction.x) * rect.width + abs(direction.y) * rect.height
        )
        requested_length = metrics.text_length(run.text, fontsize=run.font_size)
        effective_font_size = run.font_size
        overflow_action = "none"
        tolerance = max(1.5, run.font_size * 0.08)
        if requested_length > available_length + tolerance:
            scale = available_length / requested_length
            if run.overflow_policy == "error" or scale < run.minimum_font_scale:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"edited text does not fit in {run.span_id}: requires "
                        f"{requested_length:.2f}pt, available {available_length:.2f}pt"
                    ),
                )
            effective_font_size = run.font_size * scale
            overflow_action = "shrink"

        origin = self._source_point(page, run.origin)
        angle = -degrees(atan2(direction.y, direction.x))
        morph = None
        if abs(angle) > 0.01:
            morph = (origin, pymupdf.Matrix(angle))
        page.insert_text(
            origin,
            run.text,
            fontname=font.name,
            fontfile=str(font.path) if font.path else None,
            fontsize=effective_font_size,
            color=self._pdf_color(run.color),
            fill_opacity=run.alpha / 255,
            stroke_opacity=run.alpha / 255,
            render_mode=run.render_mode,
            morph=morph,
        )
        return TextEditResult(
            page_index=page_index,
            block_id=block_id,
            span_id=run.span_id,
            requested_font_name=run.font_name,
            resolved_font_name=font.name,
            font_source=font.source,
            font_substituted=font.substituted,
            requested_font_size=round(run.font_size, 3),
            effective_font_size=round(effective_font_size, 3),
            overflow_action=overflow_action,
        )

    def _write_multiline_text_run(
        self,
        source: pymupdf.Document,
        page: pymupdf.Page,
        page_index: int,
        block_id: str,
        run: TextRunEdit,
        rect: pymupdf.Rect,
        slot: int,
    ) -> TextEditResult:
        assert run.font_size is not None
        assert run.color is not None
        assert run.alpha is not None
        assert run.font_name is not None
        assert run.font_weight is not None
        assert run.italic is not None
        assert run.direction is not None
        style = next((segment for segment in run.segments if segment.text), None)
        font_size = style.font_size if style and style.font_size else run.font_size
        font_name = style.font_name if style and style.font_name else run.font_name
        font_weight = (
            style.font_weight if style and style.font_weight is not None else run.font_weight
        )
        italic = style.italic if style and style.italic is not None else run.italic
        font_xref = (
            style.font_xref
            if style and "font_xref" in style.model_fields_set
            else run.font_xref
        )
        color = style.color if style and style.color else run.color
        alpha = style.alpha if style and style.alpha is not None else run.alpha
        font = self._resolve_edit_font(
            source,
            page_index,
            run.text,
            font_xref,
            font_name,
            font_weight,
            italic,
            slot,
        )
        self._register_font(page, font)
        direction = self._source_direction(page, run.direction)
        angle = -degrees(atan2(direction.y, direction.x))
        morph = None
        if abs(angle) > 0.01:
            center = pymupdf.Point(
                (rect.x0 + rect.x1) / 2,
                (rect.y0 + rect.y1) / 2,
            )
            morph = (center, pymupdf.Matrix(angle))

        effective_font_size = font_size
        minimum_size = max(4.0, font_size * run.minimum_font_scale)
        overflow_action = "none"
        inserted = -1.0
        while effective_font_size >= minimum_size:
            inserted = page.insert_textbox(
                rect,
                run.text,
                fontname=font.name,
                fontfile=str(font.path) if font.path else None,
                fontsize=effective_font_size,
                color=self._pdf_color(color),
                lineheight=1.2,
                fill_opacity=alpha / 255,
                stroke_opacity=alpha / 255,
                render_mode=run.render_mode,
                morph=morph,
            )
            if inserted >= 0:
                break
            if run.overflow_policy == "error":
                break
            effective_font_size -= 0.5
            overflow_action = "shrink"
        if inserted < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"edited multiline text does not fit in {run.span_id}",
            )
        return TextEditResult(
            page_index=page_index,
            block_id=block_id,
            span_id=run.span_id,
            requested_font_name=font_name,
            resolved_font_name=font.name,
            font_source=font.source,
            font_substituted=font.substituted,
            requested_font_size=round(font_size, 3),
            effective_font_size=round(effective_font_size, 3),
            overflow_action=overflow_action,
        )

    def _write_rich_text_run(
        self,
        source: pymupdf.Document,
        page: pymupdf.Page,
        page_index: int,
        block_id: str,
        run: TextRunEdit,
        rect: pymupdf.Rect,
        slot: int,
    ) -> TextEditResult:
        assert run.font_size is not None
        assert run.color is not None
        assert run.alpha is not None
        assert run.font_name is not None
        assert run.font_weight is not None
        assert run.italic is not None
        assert run.origin is not None
        assert run.direction is not None
        prepared: list[
            tuple[TextStyleSegment, ResolvedFont, float, float, tuple[float, float, float]]
        ] = []
        requested_length = 0.0
        for index, segment in enumerate(run.segments):
            if not segment.text:
                continue
            font_size = segment.font_size or run.font_size
            rendered_size = font_size * (0.68 if segment.script != "normal" else 1)
            font_name = segment.font_name or run.font_name
            font_weight = segment.font_weight or run.font_weight
            italic = segment.italic if segment.italic is not None else run.italic
            font_xref = (
                segment.font_xref
                if "font_xref" in segment.model_fields_set
                else run.font_xref
            )
            font = self._resolve_edit_font(
                source,
                page_index,
                segment.text,
                font_xref,
                font_name,
                font_weight,
                italic,
                slot * 2_000 + index,
            )
            self._register_font(page, font)
            width = self._font_metrics(font).text_length(segment.text, fontsize=rendered_size)
            requested_length += width
            prepared.append(
                (
                    segment,
                    font,
                    rendered_size,
                    width,
                    self._pdf_color(segment.color or run.color),
                )
            )

        available_length = run.source_advance or rect.width
        effective_scale = 1.0
        overflow_action = "none"
        tolerance = max(1.5, run.font_size * 0.08)
        if requested_length > available_length + tolerance:
            effective_scale = available_length / requested_length
            if run.overflow_policy == "error" or effective_scale < run.minimum_font_scale:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"edited text does not fit in {run.span_id}: requires "
                        f"{requested_length:.2f}pt, available {available_length:.2f}pt"
                    ),
                )
            overflow_action = "shrink"

        direction = self._source_direction(page, run.direction)
        perpendicular = pymupdf.Point(-direction.y, direction.x)
        cursor = self._source_point(page, run.origin)
        angle = -degrees(atan2(direction.y, direction.x))
        for segment, font, requested_size, requested_width, color in prepared:
            font_size = requested_size * effective_scale
            advance = requested_width * effective_scale
            script_shift = 0.0
            if segment.script == "superscript":
                script_shift = -font_size * 0.48
            elif segment.script == "subscript":
                script_shift = font_size * 0.25
            baseline = cursor + perpendicular * script_shift
            end = baseline + direction * advance
            if segment.highlight_color:
                upper = baseline - perpendicular * font_size * 0.88
                lower = baseline + perpendicular * font_size * 0.24
                page.draw_quad(
                    pymupdf.Quad(
                        upper,
                        upper + direction * advance,
                        lower,
                        lower + direction * advance,
                    ),
                    color=None,
                    fill=self._pdf_color(segment.highlight_color),
                    overlay=True,
                )
            morph = None if abs(angle) <= 0.01 else (baseline, pymupdf.Matrix(angle))
            page.insert_text(
                baseline,
                segment.text,
                fontname=font.name,
                fontfile=str(font.path) if font.path else None,
                fontsize=font_size,
                color=color,
                fill_opacity=(segment.alpha if segment.alpha is not None else run.alpha) / 255,
                stroke_opacity=(segment.alpha if segment.alpha is not None else run.alpha) / 255,
                render_mode=run.render_mode,
                morph=morph,
            )
            line_width = max(0.45, font_size * 0.045)
            if segment.underline:
                offset = perpendicular * font_size * 0.1
                page.draw_line(
                    baseline + offset,
                    end + offset,
                    color=color,
                    width=line_width,
                    overlay=True,
                )
            if segment.strikethrough:
                offset = perpendicular * -font_size * 0.32
                page.draw_line(
                    baseline + offset,
                    end + offset,
                    color=color,
                    width=line_width,
                    overlay=True,
                )
            cursor += direction * advance

        first_font = prepared[0][1]
        return TextEditResult(
            page_index=page_index,
            block_id=block_id,
            span_id=run.span_id,
            requested_font_name=run.font_name,
            resolved_font_name=first_font.name,
            font_source=first_font.source,
            font_substituted=any(font.substituted for _, font, _, _, _ in prepared),
            requested_font_size=round(run.font_size, 3),
            effective_font_size=round(run.font_size * effective_scale, 3),
            overflow_action=overflow_action,
        )

    def _write_legacy_text_edit(
        self,
        source: pymupdf.Document,
        page: pymupdf.Page,
        page_index: int,
        text_edit: TextEdit,
        rect: pymupdf.Rect,
        slot: int,
    ) -> TextEditResult | None:
        assert text_edit.text is not None
        assert text_edit.font_size is not None
        if not text_edit.text:
            return None
        font = self._resolve_edit_font(
            source,
            page_index,
            text_edit.text,
            text_edit.font_xref,
            text_edit.font_name,
            text_edit.font_weight,
            text_edit.italic,
            slot,
        )
        self._register_font(page, font)
        effective_font_size = text_edit.font_size
        overflow_action = "none"
        if text_edit.origin is not None:
            origin = self._source_point(page, text_edit.origin)
            page.insert_text(
                origin,
                text_edit.text.split("\n"),
                fontname=font.name,
                fontfile=str(font.path) if font.path else None,
                fontsize=text_edit.font_size,
                color=self._pdf_color(text_edit.color),
                lineheight=text_edit.line_height,
                fill_opacity=text_edit.alpha / 255,
                stroke_opacity=text_edit.alpha / 255,
            )
        else:
            write_rect = pymupdf.Rect(
                rect.x0,
                rect.y0,
                min(page.cropbox.x1, rect.x1 + 2),
                min(page.cropbox.y1, rect.y1 + max(2, text_edit.font_size * 0.35)),
            )
            minimum_size = max(4.0, text_edit.font_size * text_edit.minimum_font_scale)
            inserted = -1.0
            while effective_font_size >= minimum_size:
                inserted = page.insert_textbox(
                    write_rect,
                    text_edit.text,
                    fontname=font.name,
                    fontfile=str(font.path) if font.path else None,
                    fontsize=effective_font_size,
                    color=self._pdf_color(text_edit.color),
                    lineheight=text_edit.line_height,
                    fill_opacity=text_edit.alpha / 255,
                    stroke_opacity=text_edit.alpha / 255,
                )
                if inserted >= 0:
                    break
                if text_edit.overflow_policy == "error":
                    break
                effective_font_size -= 0.5
                overflow_action = "shrink"
            if inserted < 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"edited text does not fit in {text_edit.block_id}",
                )
        return TextEditResult(
            page_index=page_index,
            block_id=text_edit.block_id,
            requested_font_name=text_edit.font_name,
            resolved_font_name=font.name,
            font_source=font.source,
            font_substituted=font.substituted,
            requested_font_size=round(text_edit.font_size, 3),
            effective_font_size=round(effective_font_size, 3),
            overflow_action=overflow_action,
        )

    def _edited_page_document(
        self,
        source: pymupdf.Document,
        spec: SourcePage,
    ) -> tuple[pymupdf.Document, list[TextEditResult]]:
        edited = pymupdf.open()
        edited.insert_pdf(source, from_page=spec.page_index, to_page=spec.page_index)
        page = edited[0]
        try:
            prepared: list[
                tuple[TextEdit, TextRunEdit | None, pymupdf.Rect, pymupdf.Rect]
            ] = []
            source_spans: dict[tuple[str, str], tuple[TextSpan, TextLine]] = {}
            if any(
                run
                for text_edit in spec.text_edits
                for run in text_edit.runs
                if not run.inserted
            ):
                source_layout = self.extract_page_content(spec.document_id, spec.page_index)
                source_spans = {
                    (block.id, span.id): (span, line)
                    for block in source_layout.blocks
                    for line in block.lines
                    for span in line.spans
                }
            seen_span_ids: set[str] = set()
            has_text_redactions = False
            for text_edit in spec.text_edits:
                edit_runs: list[TextRunEdit | None] = list(text_edit.runs) or [None]
                for run in edit_runs:
                    if run is not None:
                        if run.span_id in seen_span_ids:
                            raise HTTPException(
                                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                                detail=f"text span {run.span_id} is edited more than once",
                            )
                        seen_span_ids.add(run.span_id)
                        if run.inserted:
                            source_bbox = None
                        else:
                            source_entry = source_spans.get((text_edit.block_id, run.span_id))
                            if source_entry is None:
                                raise HTTPException(
                                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                                    detail=(
                                        f"text span {run.span_id} does not belong to "
                                        f"{text_edit.block_id} on source page {spec.page_index}"
                                    ),
                                )
                            source_span, source_line = source_entry
                            run = self._materialize_text_run(run, source_span, source_line)
                            source_bbox = source_span.bbox
                    else:
                        source_bbox = text_edit.bbox
                    bbox = run.bbox if run is not None else text_edit.bbox
                    assert bbox is not None
                    target_rect = pymupdf.Rect(bbox.x0, bbox.y0, bbox.x1, bbox.y1)
                    source_rect = (
                        pymupdf.Rect(
                            source_bbox.x0,
                            source_bbox.y0,
                            source_bbox.x1,
                            source_bbox.y1,
                        )
                        if source_bbox is not None
                        else pymupdf.Rect(target_rect)
                    )
                    if page.rotation:
                        target_rect = target_rect * page.derotation_matrix
                        source_rect = source_rect * page.derotation_matrix
                    target_rect = target_rect.intersect(page.cropbox)
                    source_rect = source_rect.intersect(page.cropbox)
                    if target_rect.is_empty:
                        target_id = run.span_id if run is not None else text_edit.block_id
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail=f"text box {target_id} is outside the page",
                        )
                    if run is None or not run.inserted:
                        page.add_redact_annot(source_rect, fill=False, cross_out=False)
                        has_text_redactions = True
                    prepared.append((text_edit, run, source_rect, target_rect))
            if has_text_redactions:
                page.apply_redactions(images=0, graphics=0, text=0)

            source_page = source[spec.page_index]
            page_images = dict(self._page_images(source_page))
            prepared_images: list[
                tuple[ImageEdit, dict[str, Any] | None, pymupdf.Rect]
            ] = []
            seen_image_ids: set[str] = set()
            has_image_redactions = False
            for image_edit in spec.image_edits:
                if image_edit.image_id in seen_image_ids:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail=f"image {image_edit.image_id} is edited more than once",
                    )
                seen_image_ids.add(image_edit.image_id)
                if image_edit.action == "insert":
                    assert image_edit.bbox is not None
                    target_rect = pymupdf.Rect(
                        image_edit.bbox.x0,
                        image_edit.bbox.y0,
                        image_edit.bbox.x1,
                        image_edit.bbox.y1,
                    )
                    if page.rotation:
                        target_rect = target_rect * page.derotation_matrix
                    visible_target_rect = pymupdf.Rect(target_rect).intersect(page.cropbox)
                    if visible_target_rect.is_empty:
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail=f"image box {image_edit.image_id} is outside the page",
                        )
                    prepared_images.append((image_edit, None, target_rect))
                    continue
                image_info = page_images.get(image_edit.image_id)
                if image_info is None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail=(
                            f"image {image_edit.image_id} does not belong to source page "
                            f"{spec.page_index}"
                        ),
                    )
                source_rect = self._display_rect(source_page, image_info["bbox"])
                target_bbox = image_edit.bbox
                target_rect = (
                    pymupdf.Rect(
                        target_bbox.x0,
                        target_bbox.y0,
                        target_bbox.x1,
                        target_bbox.y1,
                    )
                    if target_bbox is not None
                    else pymupdf.Rect(source_rect)
                )
                if page.rotation:
                    source_rect = source_rect * page.derotation_matrix
                    target_rect = target_rect * page.derotation_matrix
                source_rect = source_rect.intersect(page.cropbox)
                visible_target_rect = pymupdf.Rect(target_rect).intersect(page.cropbox)
                if visible_target_rect.is_empty:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail=f"image box {image_edit.image_id} is outside the page",
                    )
                page.add_redact_annot(source_rect, fill=False, cross_out=False)
                has_image_redactions = True
                prepared_images.append((image_edit, image_info, target_rect))
            if has_image_redactions:
                page.apply_redactions(images=2, graphics=0, text=0)

            for image_edit, image_info, target_rect in prepared_images:
                if image_edit.action == "delete":
                    continue
                if image_edit.asset_id is not None:
                    image_bytes = self.storage.image_path(image_edit.asset_id).read_bytes()
                else:
                    assert image_info is not None
                    image_bytes = self._pixmap_png(source, source_page, image_info)
                image_bytes, insert_rect = self._rotate_image_for_rect(
                    image_bytes,
                    target_rect,
                    image_edit.rotation,
                )
                page.insert_image(
                    insert_rect,
                    stream=image_bytes,
                    keep_proportion=False,
                    overlay=True,
                )

            results = []
            for slot, (text_edit, run, _source_rect, target_rect) in enumerate(prepared):
                if run is not None:
                    result = self._write_text_run(
                        source,
                        page,
                        spec.page_index,
                        text_edit.block_id,
                        run,
                        target_rect,
                        slot,
                    )
                else:
                    result = self._write_legacy_text_edit(
                        source,
                        page,
                        spec.page_index,
                        text_edit,
                        target_rect,
                        slot,
                    )
                if result is not None:
                    results.append(result)
            return edited, results
        except Exception:
            edited.close()
            raise

    def compose(self, request: ExportRequest, destination: Path) -> list[TextEditResult]:
        output = pymupdf.open()
        open_sources: dict[str, pymupdf.Document] = {}
        text_edit_results: list[TextEditResult] = []
        try:
            for spec in request.pages:
                if isinstance(spec, BlankPage):
                    width, height = spec.width, spec.height
                    if spec.rotation in (90, 270):
                        width, height = height, width
                    target_page = output.new_page(width=width, height=height)
                    for image_edit in spec.image_edits:
                        if image_edit.action != "insert":
                            raise HTTPException(
                                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                                detail="blank pages only accept inserted images",
                            )
                        assert image_edit.asset_id is not None
                        assert image_edit.bbox is not None
                        target_rect = pymupdf.Rect(
                            image_edit.bbox.x0,
                            image_edit.bbox.y0,
                            image_edit.bbox.x1,
                            image_edit.bbox.y1,
                        )
                        visible_target_rect = pymupdf.Rect(target_rect).intersect(target_page.rect)
                        if visible_target_rect.is_empty:
                            raise HTTPException(
                                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                                detail=f"image box {image_edit.image_id} is outside the page",
                            )
                        image_bytes = self.storage.image_path(image_edit.asset_id).read_bytes()
                        image_bytes, insert_rect = self._rotate_image_for_rect(
                            image_bytes,
                            target_rect,
                            image_edit.rotation,
                        )
                        target_page.insert_image(
                            insert_rect,
                            stream=image_bytes,
                            keep_proportion=False,
                            overlay=True,
                        )
                    for text_edit in spec.text_edits:
                        if not text_edit.runs or any(not run.inserted for run in text_edit.runs):
                            raise HTTPException(
                                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                                detail="blank pages only accept inserted text runs",
                            )
                        for slot, run in enumerate(text_edit.runs):
                            assert run.bbox is not None
                            target_rect = pymupdf.Rect(
                                run.bbox.x0,
                                run.bbox.y0,
                                run.bbox.x1,
                                run.bbox.y1,
                            ).intersect(target_page.rect)
                            if target_rect.is_empty:
                                raise HTTPException(
                                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                                    detail=f"text box {run.span_id} is outside the page",
                                )
                            result = self._write_text_run(
                                output,
                                target_page,
                                target_page.number,
                                text_edit.block_id,
                                run,
                                target_rect,
                                slot,
                            )
                            if result is not None:
                                text_edit_results.append(result)
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

                edited_source = None
                try:
                    render_source = source
                    render_index = spec.page_index
                    if spec.text_edits or spec.image_edits:
                        edited_source, page_results = self._edited_page_document(source, spec)
                        text_edit_results.extend(page_results)
                        render_source = edited_source
                        render_index = 0

                    source_page = render_source[render_index]
                    clip = self._crop_rect(source_page, spec)
                    width, height = clip.width, clip.height
                    if spec.rotation in (90, 270):
                        width, height = height, width
                    target_page = output.new_page(width=width, height=height)
                    target_page.show_pdf_page(
                        target_page.rect,
                        render_source,
                        render_index,
                        clip=clip,
                        rotate=spec.rotation,
                        keep_proportion=False,
                    )
                    self._apply_watermark(target_page, spec.watermark)
                finally:
                    if edited_source is not None:
                        edited_source.close()

            output.set_metadata({"producer": "PDF Editor", "title": request.filename})
            output.save(destination, garbage=4, deflate=True)
            return text_edit_results
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
            for image_edit in spec.image_edits:
                if image_edit.asset_id is not None:
                    self.storage.image_path(image_edit.asset_id)
            if spec.watermark is not None:
                if spec.watermark.kind == "image":
                    assert spec.watermark.watermark_id is not None
                    self.storage.watermark_path(spec.watermark.watermark_id)
