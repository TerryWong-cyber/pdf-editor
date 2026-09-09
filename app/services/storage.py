import re
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile, status

SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


def safe_pdf_filename(filename: str, fallback: str = "document.pdf") -> str:
    name = Path(filename).name.strip()
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf" if name else fallback
    cleaned = SAFE_FILENAME_RE.sub("-", name).strip(".-")
    return cleaned or fallback


class FileStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.originals = self.root / "originals"
        self.exports = self.root / "exports"
        self.originals.mkdir(parents=True, exist_ok=True)
        self.exports.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validated_id(file_id: str) -> str:
        try:
            return str(UUID(file_id))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="file not found",
            ) from exc

    def original_path(self, document_id: str) -> Path:
        path = self.originals / f"{self._validated_id(document_id)}.pdf"
        if not path.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
        return path

    def export_path(self, export_id: str) -> Path:
        path = self.exports / f"{self._validated_id(export_id)}.pdf"
        if not path.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="export not found")
        return path

    async def save_original(self, upload: UploadFile, max_bytes: int) -> tuple[str, Path]:
        document_id = str(uuid4())
        destination = self.originals / f"{document_id}.pdf"
        total = 0
        try:
            with destination.open("xb") as stream:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"file exceeds {max_bytes // (1024 * 1024)} MB limit",
                        )
                    stream.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()
        return document_id, destination

    def allocate_export(self) -> tuple[str, Path]:
        export_id = str(uuid4())
        return export_id, self.exports / f"{export_id}.pdf"
