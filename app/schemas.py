from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


class PageMetadata(BaseModel):
    index: int
    width: float
    height: float
    rotation: int


class DocumentMetadata(BaseModel):
    id: str
    filename: str
    page_count: int
    pages: list[PageMetadata]


class CropBox(BaseModel):
    left: Annotated[float, Field(ge=0, lt=1)] = 0
    top: Annotated[float, Field(ge=0, lt=1)] = 0
    right: Annotated[float, Field(ge=0, lt=1)] = 0
    bottom: Annotated[float, Field(ge=0, lt=1)] = 0

    @model_validator(mode="after")
    def validate_remaining_area(self) -> "CropBox":
        if self.left + self.right >= 1 or self.top + self.bottom >= 1:
            raise ValueError("crop margins must leave a non-empty page area")
        return self


class WatermarkSpec(BaseModel):
    kind: Literal["image", "text"] = "image"
    watermark_id: str | None = None
    text: str = Field(default="", max_length=500)
    font_name: str = Field(default="Helvetica", max_length=120)
    font_size: Annotated[float, Field(gt=0, le=512)] = 48
    color: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    font_weight: Literal[400, 700] = 400
    italic: bool = False
    underline: bool = False
    rotation: Annotated[float, Field(ge=-180, le=180)] = 0
    scale: Annotated[float, Field(ge=0.05, le=1)] = 0.35
    opacity: Annotated[float, Field(ge=0.05, le=1)] = 0.35
    x: Annotated[float, Field(ge=0, le=1)] = 0.5
    y: Annotated[float, Field(ge=0, le=1)] = 0.5
    tiled: bool = False
    tile_rows: Annotated[int, Field(ge=1, le=8)] = 3

    @model_validator(mode="after")
    def validate_payload(self) -> "WatermarkSpec":
        if self.kind == "image" and not self.watermark_id:
            raise ValueError("image watermark requires watermark_id")
        if self.kind == "text" and not self.text.strip():
            raise ValueError("text watermark requires text")
        return self


class PdfRect(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def validate_area(self) -> "PdfRect":
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("PDF rectangle coordinates must be ordered")
        return self

    @property
    def has_positive_area(self) -> bool:
        return self.x1 > self.x0 and self.y1 > self.y0


class PdfPoint(BaseModel):
    x: float
    y: float


class TextCharacter(BaseModel):
    index: int
    text: str
    bbox: PdfRect
    origin: PdfPoint
    synthetic: bool = False


class TextSpan(BaseModel):
    id: str
    text: str
    bbox: PdfRect
    origin: PdfPoint
    advance: Annotated[float, Field(ge=0)]
    font_size: Annotated[float, Field(gt=0, le=512)]
    font_name: str
    font_xref: int | None = None
    font_xref_candidates: list[int] = Field(default_factory=list)
    font_url: str | None = None
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    alpha: Annotated[int, Field(ge=0, le=255)] = 255
    flags: int = 0
    char_flags: int = 0
    bidi: int = 0
    font_weight: Literal[400, 700] = 400
    italic: bool = False
    serif: bool = False
    monospace: bool = False
    superscript: bool = False
    ascender: float
    descender: float
    characters: list[TextCharacter] = Field(default_factory=list)


class TextLine(BaseModel):
    id: str
    text: str
    bbox: PdfRect
    direction: PdfPoint
    writing_mode: Literal[0, 1] = 0
    spans: list[TextSpan] = Field(default_factory=list)


class TextBlock(BaseModel):
    id: str
    bbox: PdfRect
    text: str
    font_size: Annotated[float, Field(gt=0, le=512)]
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    font_name: str = "sans-serif"
    font_xref: int | None = None
    font_url: str | None = None
    font_weight: Literal[400, 700] = 400
    italic: bool = False
    line_height: Annotated[float, Field(ge=0.7, le=4)] = 1.2
    origin: PdfPoint
    lines: list[TextLine] = Field(default_factory=list)


class ImageBlock(BaseModel):
    id: str
    bbox: PdfRect
    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]
    xref: int | None = None
    bits_per_component: Annotated[int, Field(ge=0)] = 0
    colorspace: str = ""
    has_mask: bool = False
    preview_url: str


class PageContent(BaseModel):
    layout_version: Literal[2] = 2
    coordinate_space: Literal["rotated_page"] = "rotated_page"
    document_id: str
    page_index: int
    width: float
    height: float
    blocks: list[TextBlock]
    images: list[ImageBlock] = Field(default_factory=list)


class TextStyleSegment(BaseModel):
    text: str = Field(max_length=20_000)
    font_size: Annotated[float, Field(gt=0, le=512)] | None = None
    color: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] | None = None
    alpha: Annotated[int, Field(ge=0, le=255)] | None = None
    font_name: Annotated[str, Field(max_length=200)] | None = None
    font_xref: int | None = Field(default=None, ge=1)
    font_weight: Literal[400, 700] | None = None
    italic: bool | None = None
    underline: bool = False
    strikethrough: bool = False
    script: Literal["normal", "superscript", "subscript"] = "normal"
    highlight_color: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] | None = None

    @model_validator(mode="after")
    def validate_segment(self) -> "TextStyleSegment":
        if "\r" in self.text:
            raise ValueError("styled text segment must use LF line breaks")
        return self


class TextRunEdit(BaseModel):
    span_id: str = Field(min_length=1, max_length=120)
    text: str = Field(max_length=20_000)
    inserted: bool = False
    wrap: bool = False
    bbox: PdfRect | None = None
    font_size: Annotated[float, Field(gt=0, le=512)] | None = None
    color: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] | None = None
    alpha: Annotated[int, Field(ge=0, le=255)] | None = None
    font_name: Annotated[str, Field(max_length=200)] | None = None
    font_xref: int | None = Field(default=None, ge=1)
    font_weight: Literal[400, 700] | None = None
    italic: bool | None = None
    origin: PdfPoint | None = None
    direction: PdfPoint | None = None
    writing_mode: Literal[0, 1] | None = None
    source_advance: Annotated[float, Field(gt=0)] | None = None
    render_mode: Annotated[int, Field(ge=0, le=3)] = 0
    overflow_policy: Literal["error", "shrink"] = "error"
    minimum_font_scale: Annotated[float, Field(ge=0.5, le=1)] = 0.75
    segments: list[TextStyleSegment] = Field(default_factory=list, max_length=2_000)

    @model_validator(mode="after")
    def validate_run(self) -> "TextRunEdit":
        if self.bbox is not None and not self.bbox.has_positive_area:
            raise ValueError("span text box must have a positive area")
        if "\r" in self.text:
            raise ValueError("span text must use LF line breaks")
        if self.direction is not None and (
            abs(self.direction.x) < 1e-9 and abs(self.direction.y) < 1e-9
        ):
            raise ValueError("text direction must be a non-zero vector")
        if self.writing_mode not in (None, 0):
            raise ValueError("vertical writing mode is not supported for span replacement")
        if self.segments and "".join(segment.text for segment in self.segments) != self.text:
            raise ValueError("styled text segments must concatenate to the run text")
        if self.inserted:
            missing = [
                name
                for name, value in (
                    ("bbox", self.bbox),
                    ("font_size", self.font_size),
                    ("color", self.color),
                    ("alpha", self.alpha),
                    ("font_name", self.font_name),
                    ("font_weight", self.font_weight),
                    ("italic", self.italic),
                    ("origin", self.origin),
                    ("direction", self.direction),
                    ("source_advance", self.source_advance),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"inserted text run is missing: {', '.join(missing)}")
        return self


class TextEdit(BaseModel):
    block_id: str = Field(min_length=1, max_length=80)
    bbox: PdfRect | None = None
    text: Annotated[str, Field(max_length=100_000)] | None = None
    font_size: Annotated[float, Field(gt=0, le=512)] | None = None
    color: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    alpha: Annotated[int, Field(ge=0, le=255)] = 255
    font_name: str = Field(default="sans-serif", max_length=200)
    font_xref: int | None = Field(default=None, ge=1)
    font_weight: Literal[400, 700] = 400
    italic: bool = False
    line_height: Annotated[float, Field(ge=0.7, le=4)] = 1.2
    origin: PdfPoint | None = None
    overflow_policy: Literal["error", "shrink"] = "shrink"
    minimum_font_scale: Annotated[float, Field(ge=0.5, le=1)] = 0.55
    runs: list[TextRunEdit] = Field(default_factory=list, max_length=2_000)

    @model_validator(mode="after")
    def validate_edit_mode(self) -> "TextEdit":
        if self.runs:
            return self
        missing = [
            name
            for name, value in (
                ("bbox", self.bbox),
                ("text", self.text),
                ("font_size", self.font_size),
            )
            if value is None
        ]
        if missing:
            raise ValueError(f"legacy text edit is missing: {', '.join(missing)}")
        if self.bbox is not None and not self.bbox.has_positive_area:
            raise ValueError("text box must have a positive area")
        return self


class ImageEdit(BaseModel):
    image_id: str = Field(min_length=1, max_length=120)
    action: Literal["delete", "replace", "insert"]
    bbox: PdfRect | None = None
    asset_id: str | None = None
    rotation: Annotated[float, Field(ge=-180, le=180)] = 0

    @model_validator(mode="after")
    def validate_edit(self) -> "ImageEdit":
        if self.bbox is not None and not self.bbox.has_positive_area:
            raise ValueError("image box must have a positive area")
        if self.action == "delete" and self.asset_id is not None:
            raise ValueError("deleted images cannot include an asset_id")
        if self.action == "insert" and (self.asset_id is None or self.bbox is None):
            raise ValueError("inserted images require an asset_id and bbox")
        return self


class SourcePage(BaseModel):
    kind: Literal["source"]
    document_id: str
    page_index: Annotated[int, Field(ge=0)]
    rotation: Literal[0, 90, 180, 270] = 0
    crop: CropBox | None = None
    watermark: WatermarkSpec | None = None
    text_edits: list[TextEdit] = Field(default_factory=list, max_length=500)
    image_edits: list[ImageEdit] = Field(default_factory=list, max_length=500)


class BlankPage(BaseModel):
    kind: Literal["blank"]
    width: Annotated[float, Field(gt=0, le=2880)] = 595
    height: Annotated[float, Field(gt=0, le=2880)] = 842
    rotation: Literal[0, 90, 180, 270] = 0
    watermark: WatermarkSpec | None = None
    text_edits: list[TextEdit] = Field(default_factory=list, max_length=500)
    image_edits: list[ImageEdit] = Field(default_factory=list, max_length=500)


PageSpec = Annotated[SourcePage | BlankPage, Field(discriminator="kind")]


class ExportRequest(BaseModel):
    filename: str = Field(default="edited-copy.pdf", min_length=1, max_length=180)
    pages: list[PageSpec] = Field(min_length=1, max_length=2000)


class TextEditResult(BaseModel):
    page_index: int
    block_id: str
    span_id: str | None = None
    requested_font_name: str
    resolved_font_name: str
    font_source: Literal["embedded", "configured", "builtin"]
    font_substituted: bool
    requested_font_size: float
    effective_font_size: float
    overflow_action: Literal["none", "shrink"] = "none"


class ExportResponse(BaseModel):
    id: str
    filename: str
    page_count: int
    download_url: str
    text_edit_results: list[TextEditResult] = Field(default_factory=list)


class WatermarkMetadata(BaseModel):
    id: str
    filename: str
    width: int
    height: int
    preview_url: str


class ImageAssetMetadata(BaseModel):
    id: str
    filename: str
    width: int
    height: int
    preview_url: str
