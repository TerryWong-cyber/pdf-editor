# syntax=docker/dockerfile:1
FROM debian:bookworm-slim AS fonts
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-core fonts-noto-cjk python3-fonttools \
    && rm -rf /var/lib/apt/lists/*
COPY script/prepare_container_fonts.py /tmp/prepare_container_fonts.py
RUN python3 /tmp/prepare_container_fonts.py

FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM python:3.12-slim-bookworm AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PDF_EDITOR_DATA_DIR=/data \
    FONT_FILE=/opt/fonts/NotoSansCJKsc-Regular.otf \
    FONT_ZH_FILE=/opt/fonts/NotoSansCJKsc-Regular.otf \
    FONT_ZH_CN_FILE=/opt/fonts/NotoSansCJKsc-Regular.otf \
    FONT_ZH_TW_FILE=/opt/fonts/NotoSansCJKtc-Regular.otf \
    FONT_LATIN_FILE=/opt/fonts/NotoSans-Regular.ttf \
    FONT_JA_FILE=/opt/fonts/NotoSansCJKjp-Regular.otf \
    FONT_KO_FILE=/opt/fonts/NotoSansCJKkr-Regular.otf \
    FONT_ARABIC_FILE=/opt/fonts/NotoSansArabic-Regular.ttf \
    FONT_CYRILLIC_FILE=/opt/fonts/NotoSans-Regular.ttf \
    FONT_THAI_FILE=/opt/fonts/NotoSansThai-Regular.ttf \
    FONT_VIETNAMESE_FILE=/opt/fonts/NotoSans-Regular.ttf
WORKDIR /app
RUN groupadd --gid 10001 pdfeditor \
    && useradd --uid 10001 --gid pdfeditor --no-create-home pdfeditor \
    && mkdir /data && chown pdfeditor:pdfeditor /data
COPY --from=dependencies /app/.venv /app/.venv
COPY --from=fonts /opt/fonts /opt/fonts
COPY app ./app
# Fail the build if bundled fonts cannot be opened or rendered by PyMuPDF.
RUN python - <<'PY'
import pymupdf
from app.config import Settings

settings = Settings()
samples = {
    "font_file": "中", "font_zh_file": "中", "font_zh_cn_file": "中",
    "font_zh_tw_file": "中", "font_latin_file": "A", "font_ja_file": "あ",
    "font_ko_file": "한", "font_arabic_file": "ع", "font_cyrillic_file": "Я",
    "font_thai_file": "ก", "font_vietnamese_file": "ệ",
}
for field in type(settings).model_fields:
    if field.startswith("font_"):
        path = getattr(settings, field)
        font = pymupdf.Font(fontfile=str(path))
        text = samples[field]
        assert font.has_glyph(ord(text)), path
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((40, 60), text, fontname="CheckFont", fontfile=str(path))
            assert document.tobytes().startswith(b"%PDF-"), path
PY
USER pdfeditor
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
