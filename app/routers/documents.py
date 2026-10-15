import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response
from starlette.datastructures import Headers

from app.config import settings
from app.dependencies import get_editor_service, get_pdf_service, get_storage
from app.schemas import (
    DocumentMetadata,
    EditedPreviewRequest,
    ExportRequest,
    ExportResponse,
    ImageAssetMetadata,
    PageContent,
    URLImportRequest,
    WatermarkMetadata,
)
from app.services.editor_service import EditorService
from app.services.pdf_service import PdfService
from app.services.storage import FileStorage, safe_pdf_filename
from app.url_files import fetch_file

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
    editor: Annotated[EditorService, Depends(get_editor_service)],
) -> list[DocumentMetadata]:
    if not files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="no files supplied",
        )

    results: list[DocumentMetadata] = []
    for upload in files:
        results.append(await editor.upload(upload))
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
    image_id: str | None = None,
) -> Response:
    return Response(
        content=pdf_service.render_edit_background(
            document_id,
            page_index,
            scale,
            remove_text=remove_text,
            remove_images=remove_images,
            image_id=image_id,
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


@router.post("/previews")
def edited_page_preview(
    request: EditedPreviewRequest,
    pdf_service: Annotated[PdfService, Depends(get_pdf_service)],
) -> Response:
    return Response(
        content=pdf_service.render_edited_preview(request.page, request.max_edge),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/exports", response_model=ExportResponse, status_code=status.HTTP_201_CREATED)
def create_export(
    request: ExportRequest,
    editor: Annotated[EditorService, Depends(get_editor_service)],
) -> ExportResponse:
    return editor.export(request)


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
        headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"},
    )


url_import_slots = asyncio.Semaphore(2)


@router.post("/files/import-url", status_code=201)
async def import_file_from_url(request: URLImportRequest):
    """Import a signed/public URL into the caller's existing file namespace."""
    async with url_import_slots:
        return await _import_url(request)


async def _import_url(request: URLImportRequest):
    limits = {
        "document": settings.max_upload_mb * 1024 * 1024,
        "image": IMAGE_MAX_BYTES, "watermark": WATERMARK_MAX_BYTES,
    }
    remote = await fetch_file(
        request.file_url, request.filename, max_bytes=limits[request.kind],
        timeout_seconds=settings.url_import_timeout_seconds,
        allowed_hosts=settings.url_import_allowed_hosts,
    )
    upload = UploadFile(
        file=remote.file, filename=remote.filename, size=remote.size,
        headers=Headers({"content-type": remote.content_type}),
    )
    try:
        if request.kind == "document":
            result = await get_editor_service().upload(upload)
        elif request.kind == "image":
            result = await upload_image(upload, get_storage(), get_pdf_service())
        else:
            result = await upload_watermark(upload, get_storage(), get_pdf_service())
        return {**result.model_dump(), "kind": request.kind}
    finally:
        await upload.close()
