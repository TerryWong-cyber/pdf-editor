from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pymupdf
from fastapi import HTTPException, status

from app.config import Settings


@dataclass(frozen=True)
class ResolvedFont:
    name: str
    path: Path | None
    buffer: bytes | None = None
    source: Literal["embedded", "configured", "builtin"] = "configured"
    substituted: bool = True


class FontResolver:
    """Select a configured Noto font for text written into exported PDFs."""

    def __init__(self, settings: Settings) -> None:
        self.paths = {
            "default": settings.font_file,
            "zh": settings.font_zh_file,
            "zh_cn": settings.font_zh_cn_file,
            "zh_tw": settings.font_zh_tw_file,
            "latin": settings.font_latin_file,
            "ja": settings.font_ja_file,
            "ko": settings.font_ko_file,
            "arabic": settings.font_arabic_file,
            "cyrillic": settings.font_cyrillic_file,
            "thai": settings.font_thai_file,
            "vietnamese": settings.font_vietnamese_file,
        }

    @staticmethod
    def _script(text: str) -> str:
        codepoints = [ord(character) for character in text if not character.isspace()]
        if any(0x0600 <= value <= 0x06FF for value in codepoints):
            return "arabic"
        if any(0x0E00 <= value <= 0x0E7F for value in codepoints):
            return "thai"
        if any(0xAC00 <= value <= 0xD7AF for value in codepoints):
            return "ko"
        if any(0x3040 <= value <= 0x30FF for value in codepoints):
            return "ja"
        if any(0x3400 <= value <= 0x9FFF for value in codepoints):
            return "zh_cn"
        if any(0x0400 <= value <= 0x052F for value in codepoints):
            return "cyrillic"
        if any(0x1EA0 <= value <= 0x1EFF for value in codepoints):
            return "vietnamese"
        return "latin"

    @staticmethod
    def builtin_latin_font(
        preferred_name: str,
        font_weight: int,
        italic: bool,
    ) -> str | None:
        key = "".join(character for character in preferred_name.lower() if character.isalnum())
        if "times" in key or "serif" in key:
            variants = {
                (400, False): "tiro",
                (700, False): "tibo",
                (400, True): "tiit",
                (700, True): "tibi",
            }
        elif "courier" in key or "mono" in key:
            variants = {
                (400, False): "cour",
                (700, False): "cobo",
                (400, True): "coit",
                (700, True): "cobi",
            }
        elif any(name in key for name in ("arial", "helvetica", "sans")):
            variants = {
                (400, False): "helv",
                (700, False): "hebo",
                (400, True): "heit",
                (700, True): "hebi",
            }
        else:
            return None
        return variants[(font_weight, italic)]

    @staticmethod
    def is_native_builtin(preferred_name: str, builtin_name: str) -> bool:
        key = "".join(character for character in preferred_name.lower() if character.isalnum())
        if builtin_name.startswith("he"):
            return key.startswith("helvetica") or key in {"helv", "hebo", "heit", "hebi"}
        if builtin_name.startswith("ti"):
            return key.startswith("times") or key in {"tiro", "tibo", "tiit", "tibi"}
        return key.startswith("courier") or key in {"cour", "cobo", "coit", "cobi"}

    def resolve(
        self,
        text: str,
        slot: int,
        preferred_name: str = "sans-serif",
        font_weight: int = 400,
        italic: bool = False,
    ) -> ResolvedFont:
        script = self._script(text)
        if script == "latin" and all(ord(character) <= 0xFF for character in text):
            builtin = self.builtin_latin_font(preferred_name, font_weight, italic)
            if builtin is not None:
                return ResolvedFont(
                    name=builtin,
                    path=None,
                    source="builtin",
                    substituted=not self.is_native_builtin(preferred_name, builtin),
                )

        candidates = [self.paths[script], self.paths["default"]]
        for path in candidates:
            if path.is_file():
                return ResolvedFont(
                    name=f"EditFont{slot}",
                    path=path,
                    source="configured",
                    substituted=True,
                )

        # PyMuPDF's built-in Helvetica is a safe local fallback for basic Latin text.
        if script == "latin" and all(ord(character) <= 0xFF for character in text):
            return ResolvedFont(
                name="helv",
                path=None,
                source="builtin",
                substituted=True,
            )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"configured {script} font file is unavailable: {candidates[0]}",
        )

    @staticmethod
    def resolve_embedded(
        document: pymupdf.Document,
        page_index: int,
        font_xref: int | None,
        text: str,
        slot: int,
    ) -> ResolvedFont | None:
        if font_xref is None:
            return None
        page_font_xrefs = {font[0] for font in document[page_index].get_fonts(full=True)}
        if font_xref not in page_font_xrefs:
            return None
        try:
            _name, _extension, _font_type, content = document.extract_font(font_xref)
            if not content:
                return None
            font = pymupdf.Font(fontbuffer=content)
            if any(
                not character.isspace() and font.has_glyph(ord(character)) == 0
                for character in text
            ):
                return None
            return ResolvedFont(
                name=f"OriginalFont{slot}",
                path=None,
                buffer=content,
                source="embedded",
                substituted=False,
            )
        except (RuntimeError, ValueError):
            return None
