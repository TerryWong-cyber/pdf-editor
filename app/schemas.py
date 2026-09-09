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


class SourcePage(BaseModel):
    kind: Literal["source"]
    document_id: str
    page_index: Annotated[int, Field(ge=0)]
    rotation: Literal[0, 90, 180, 270] = 0
    crop: CropBox | None = None


class BlankPage(BaseModel):
    kind: Literal["blank"]
    width: Annotated[float, Field(gt=0, le=2880)] = 595
    height: Annotated[float, Field(gt=0, le=2880)] = 842
    rotation: Literal[0, 90, 180, 270] = 0


PageSpec = Annotated[SourcePage | BlankPage, Field(discriminator="kind")]


class ExportRequest(BaseModel):
    filename: str = Field(default="edited-copy.pdf", min_length=1, max_length=180)
    pages: list[PageSpec] = Field(min_length=1, max_length=2000)


class ExportResponse(BaseModel):
    id: str
    filename: str
    page_count: int
    download_url: str

