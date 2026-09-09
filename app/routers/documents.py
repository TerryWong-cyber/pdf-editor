from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response

from app.config import settings
from app.dependencies import get_pdf_service, get_storage
from app.schemas import DocumentMetadata, ExportRequest, ExportResponse, WatermarkMetadata
from app.services.pdf_service import PdfService
from app.services.storage import FileStorage, safe_pdf_filename

router = APIRouter()
WATERMARK_MAX_BYTES = 20 * 1024 * 1024
WATERMARK_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}


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


@router.post("/exports", response_model=ExportResponse, status_code=status.HTTP_201_CREATED)
def create_export(
    request: ExportRequest,
    storage: Annotated[FileStorage, Depends(get_storage)],
    pdf_service: Annotated[PdfService, Depends(get_pdf_service)],
) -> ExportResponse:
    pdf_service.validate_sources(request.pages)
    export_id, destination = storage.allocate_export()
    filename = safe_pdf_filename(request.filename, "edited-copy.pdf")
    pdf_service.compose(request.model_copy(update={"filename": filename}), destination)
    return ExportResponse(
        id=export_id,
        filename=filename,
        page_count=len(request.pages),
        download_url=f"/api/v1/exports/{export_id}/download?filename={filename}",
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
