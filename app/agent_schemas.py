"""Agent contracts use human page numbers; REST contracts remain zero based."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas import BlankPage, CropBox, ExportResponse, ImageEdit, TextEdit, WatermarkSpec


class AgentSourcePage(BaseModel):
    kind: Literal["source"] = "source"
    document_id: str
    page_number: int = Field(ge=1, description="Human page number, starting at 1.")
    rotation: Literal[0, 90, 180, 270] = 0
    crop: CropBox | None = None
    watermark: WatermarkSpec | None = None
    text_edits: list[TextEdit] = Field(default_factory=list, max_length=500)
    image_edits: list[ImageEdit] = Field(default_factory=list, max_length=500)


AgentPage = Annotated[AgentSourcePage | BlankPage, Field(discriminator="kind")]


class AgentExportRequest(BaseModel):
    filename: str = Field(default="edited-copy.pdf", min_length=1, max_length=180)
    pages: list[AgentPage] = Field(min_length=1, max_length=2000)


class ExportArtifact(ExportResponse):
    source_unchanged: bool = True
    can_import_as_document: bool = True
    warnings: list[str] = Field(default_factory=list)
    draft_id: str | None = None
    revision: int | None = None


class SpanReplacement(BaseModel):
    document_id: str
    page_number: int = Field(ge=1)
    block_id: str = Field(min_length=1, max_length=80)
    span_id: str = Field(min_length=1, max_length=120)
    expected_text: str = Field(max_length=20_000)
    new_text: str = Field(max_length=20_000)
    overflow_policy: Literal["error", "shrink"] = "error"
    minimum_font_scale: float = Field(default=0.75, ge=0.5, le=1)

    @model_validator(mode="after")
    def validate_line_breaks(self):
        if "\r" in self.new_text:
            raise ValueError("use LF line breaks")
        return self
