from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response

from app.config import settings
from app.dependencies import get_pdf_service, get_storage
from app.schemas import (
    DocumentMetadata,
    ExportRequest,
    ExportResponse,
    ImageAssetMetadata,
    PageContent,
    WatermarkMetadata,
)
from app.services.pdf_service import PdfService
from app.services.storage import FileStorage, safe_pdf_filename

router = APIRouter()
WATERMARK_MAX_BYTES = 20 * 1024 * 1024
WATERMARK_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}
IMAGE_MAX_BYTES = 30 * 1024 * 1024
IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}


@router.post(
    "/documents",
    response_model=list[DocumentMetadata],
    status_code=status.HTTP_201_CREATED,
)
async def upload_documents(
    files: Annotated[list[UploadFile], File(description="One or more PDF files")],
    storage: Annotated[FileStorage, Depends(get_storage)],
    pdf_service: Annotated[PdfService, Depends(get_pdf_service)],
) -> list[DocumentMetadata]:
    if not files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="no files supplied",
        )

    results: list[DocumentMetadata] = []
    for upload in files:
        filename = safe_pdf_filename(upload.filename or "document.pdf")
        document_id, path = await storage.save_original(
            upload,
            max_bytes=settings.max_upload_mb * 1024 * 1024,
        )
        try:
            pdf_service.validate_signature(path)
            results.append(pdf_service.inspect(document_id, path, filename))
        except Exception:
            path.unlink(missing_ok=True)
            raise
    return results


@router.get("/documents/{document_id}/pages/{page_index}/preview")
def page_preview(
    document_id: str,
    page_index: int,
    pdf_service: Annotated[PdfService, Depends(get_pdf_service)],
    scale: Annotated[float, Query(ge=0.25, le=3)] = 1,
) -> Response:
    return Response(
        content=pdf_service.render_preview(document_id, page_index, scale),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/documents/{document_id}/pages/{page_index}/edit-background")
def page_edit_background(
    document_id: str,
    page_index: int,
    pdf_service: Annotated[PdfService, Depends(get_pdf_service)],
    scale: Annotated[float, Query(ge=0.25, le=3)] = 1,
    remove_text: bool = True,
    remove_images: bool = False,
) -> Response:
    return Response(
        content=pdf_service.render_edit_background(
            document_id,
            page_index,
            scale,
            remove_text=remove_text,
            remove_images=remove_images,
        ),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/documents/{document_id}/fonts/{font_xref}")
def document_font(
    document_id: str,
    font_xref: int,
    pdf_service: Annotated[PdfService, Depends(get_pdf_service)],
) -> Response:
    content, extension = pdf_service.extract_font(document_id, font_xref)
    media_type = {
        "otf": "font/otf",
        "ttf": "font/ttf",
        "woff": "font/woff",
        "woff2": "font/woff2",
    }[extension]
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get(
    "/documents/{document_id}/pages/{page_index}/content",
    response_model=PageContent,
)
def page_content(
    document_id: str,
    page_index: int,
    pdf_service: Annotated[PdfService, Depends(get_pdf_service)],
) -> PageContent:
    return pdf_service.extract_page_content(document_id, page_index)


@router.get("/documents/{document_id}/pages/{page_index}/images/{image_id}/preview")
def document_image_preview(
    document_id: str,
    page_index: int,
    image_id: str,
    pdf_service: Annotated[PdfService, Depends(get_pdf_service)],
) -> Response:
    return Response(
        content=pdf_service.extract_page_image(document_id, page_index, image_id),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post(
    "/watermarks",
    response_model=WatermarkMetadata,
    status_code=status.HTTP_201_CREATED,
)
async def upload_watermark(
    file: Annotated[UploadFile, File(description="PNG, JPEG, or WebP watermark image")],
    storage: Annotated[FileStorage, Depends(get_storage)],
    pdf_service: Annotated[PdfService, Depends(get_pdf_service)],
) -> WatermarkMetadata:
    if file.content_type not in WATERMARK_CONTENT_TYPES:
        await file.close()
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="watermark only supports PNG, JPEG, and WebP images",
        )
    if file.size is not None and file.size > WATERMARK_MAX_BYTES:
        await file.close()
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="watermark exceeds 20 MB limit",
        )
    content = await file.read(WATERMARK_MAX_BYTES + 1)
    filename = file.filename or "watermark"
    await file.close()
    if len(content) > WATERMARK_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="watermark exceeds 20 MB limit",
        )
    watermark_id, destination = storage.allocate_watermark()
    return pdf_service.create_watermark(content, filename, destination, watermark_id)


@router.get("/watermarks/{watermark_id}/preview")
def watermark_preview(
    watermark_id: str,
    storage: Annotated[FileStorage, Depends(get_storage)],
) -> FileResponse:
    return FileResponse(
        storage.watermark_path(watermark_id),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post(
    "/images",
    response_model=ImageAssetMetadata,
    status_code=status.HTTP_201_CREATED,
)
async def upload_image(
    file: Annotated[UploadFile, File(description="Processed PNG, JPEG, or WebP image")],
    storage: Annotated[FileStorage, Depends(get_storage)],
    pdf_service: Annotated[PdfService, Depends(get_pdf_service)],
) -> ImageAssetMetadata:
    if file.content_type not in IMAGE_CONTENT_TYPES:
        await file.close()
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="image only supports PNG, JPEG, and WebP files",
        )
    if file.size is not None and file.size > IMAGE_MAX_BYTES:
        await file.close()
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="image exceeds 30 MB limit",
        )
    content = await file.read(IMAGE_MAX_BYTES + 1)
    filename = file.filename or "image"
    await file.close()
    if len(content) > IMAGE_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="image exceeds 30 MB limit",
        )
    image_id, destination = storage.allocate_image()
    return pdf_service.create_image_asset(content, filename, destination, image_id)


@router.get("/images/{image_id}/preview")
def image_preview(
    image_id: str,
    storage: Annotated[FileStorage, Depends(get_storage)],
) -> FileResponse:
    return FileResponse(
        storage.image_path(image_id),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/exports", response_model=ExportResponse, status_code=status.HTTP_201_CREATED)
def create_export(
    request: ExportRequest,
    storage: Annotated[FileStorage, Depends(get_storage)],
    pdf_service: Annotated[PdfService, Depends(get_pdf_service)],
) -> ExportResponse:
    pdf_service.validate_sources(request.pages)
    export_id, destination = storage.allocate_export()
    filename = safe_pdf_filename(request.filename, "edited-copy.pdf")
    text_edit_results = pdf_service.compose(
        request.model_copy(update={"filename": filename}),
        destination,
    )
    return ExportResponse(
        id=export_id,
        filename=filename,
        page_count=len(request.pages),
        download_url=f"/api/v1/exports/{export_id}/download?filename={filename}",
        text_edit_results=text_edit_results,
    )


@router.get("/exports/{export_id}/download")
def download_export(
    export_id: str,
    storage: Annotated[FileStorage, Depends(get_storage)],
    filename: str = "edited-copy.pdf",
) -> FileResponse:
    return FileResponse(
        storage.export_path(export_id),
        media_type="application/pdf",
        filename=safe_pdf_filename(filename, "edited-copy.pdf"),
    )
