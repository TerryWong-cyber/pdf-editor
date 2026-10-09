# PDF Editor API

FastAPI service for non-destructive, layout-aware PDF editing. Uploaded originals are
immutable; every save or extraction creates a new PDF in the export store.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/uvicorn app.main:app --reload --port 8000
```

The API docs are available at `http://localhost:8000/docs`. Copy `.env.example` to `.env`
to override defaults. Both `localhost:5173` and `127.0.0.1:5173` are allowed for local
frontend development by default.

## Deploy with Docker Compose

The Compose deployment runs the backend on port 8000 with one Uvicorn worker, a health
check, automatic restart, and a non-root user (UID/GID 10001). No database or other service
is required. Python dependencies are installed from `uv.lock`. The image includes Noto
fonts for Chinese, Japanese, Korean, Latin, Arabic, Cyrillic, Thai, and Vietnamese text.

```bash
cp .env.docker.example .env.docker
# Edit .env.docker for the frontend origin and host port.
docker compose --env-file .env.docker config --quiet
docker compose --env-file .env.docker up -d --build
docker compose --env-file .env.docker ps
curl --fail http://127.0.0.1:8000/health
```

The health endpoint should return `{"status":"ok"}`; API docs are at
`http://127.0.0.1:8000/docs`. Substitute your configured `PDF_EDITOR_PORT` in these URLs.
`PDF_EDITOR_PORT` controls only the **host** port. The container always listens on 8000;
for example, `PDF_EDITOR_PORT=8020` maps host port 8020 to container port 8000. Keep the
Dockerfile command and Compose healthcheck on container port 8000. Compose explicitly
sets the startup command so that older images with another default port can be reused.
Inspect startup failures with:

```bash
docker compose --env-file .env.docker logs --tail=100 pdf-editor-api
```

Container settings are isolated from the local `.env`, which is neither copied into the
image nor loaded by the container. Adapt the following deployment values:

| Setting | Container behavior |
| --- | --- |
| `PDF_EDITOR_CORS_ORIGINS` | Set to the browser frontend origin, e.g. `https://editor.example.com`; multiple origins are comma-separated. Do not include `/pdf-editor` or other paths. |
| `PDF_EDITOR_MAX_UPLOAD_MB` | Defaults to 100 MB per uploaded file. Align the reverse proxy's body-size limit with your intended multi-file request size. |
| `PDF_EDITOR_DATA_DIR` | Compose fixes this to `/data` and mounts the `pdf-editor-data` named volume. |
| `FONT_*` | `.env.docker` overrides image defaults with the host font paths; Compose mounts `/usr/share/fonts/opentype/noto` read-only at the same container path. |
| `PDF_EDITOR_BIND_ADDRESS` / `PDF_EDITOR_PORT` | Control the host port mapping; default `127.0.0.1:8000`. Set the bind address to `0.0.0.0` for direct access from other machines. |

The Dockerfile uses Debian's static Noto fonts, so the container font filenames differ
from the variable-font paths in the local `.env.example`. CJK font collections are split
into standalone regional faces during the build. The provided Compose configuration uses
the host variable fonts instead: all `FONT_*` entries in `.env.docker.example` match the
absolute paths in the local `.env`. No font copy is needed. The directory and the configured
files must exist on the **Docker host** (the machine running the Docker daemon), and be
readable by UID/GID 10001. A missing host directory fails deployment rather than silently
creating an empty directory. Verify the mounted files after startup:

```bash
docker compose --env-file .env.docker exec pdf-editor-api python -c \
  'from app.config import settings; from pathlib import Path; import pymupdf; paths = {getattr(settings, name) for name in type(settings).model_fields if name.startswith("font_")}; [(print(path), pymupdf.Font(fontfile=str(path))) for path in sorted(paths)]'
```

To use the bundled image fonts instead, remove the host font bind mount from Compose
and remove the `FONT_*` overrides from `.env.docker`. Other custom fonts can be mounted
read-only, for example `./fonts:/custom-fonts:ro`, with matching `FONT_*` entries.
Use the existing `FONT_*` names when overriding image defaults rather than their
`PDF_EDITOR_FONT_*` aliases, because `FONT_*` has priority in the settings loader.

Original PDFs, exports, watermarks, and edited images persist across container recreation
in the named volume. The existing host `./data` directory is **not** migrated automatically.
To use that directory instead, replace the volume mount with `./data:/data` and ensure it
is writable by UID/GID 10001. Back up the data before migration. `docker compose down`
preserves the named volume; adding `-v` deletes it.

For a reverse proxy on the host, forward to `http://127.0.0.1:8000`. A proxy in another
container needs a shared Docker network and should forward to `http://pdf-editor-api:8000`.
If serving under `/pdf-editor/`, strip that prefix before forwarding: the backend routes
remain `/api/v1/...`, and the frontend API base should be `/pdf-editor/api/v1`. Download
URLs returned by the backend are relative to the backend root, so the frontend must
resolve them through its configured public API base.

After startup, verify an actual PDF operation as well as the health endpoint:

```bash
curl --fail -F 'files=@/absolute/path/sample.pdf;type=application/pdf' \
  http://127.0.0.1:8000/api/v1/documents
curl --fail -H 'Content-Type: application/json' \
  -d '{"filename":"smoke.pdf","pages":[{"kind":"blank"}]}' \
  http://127.0.0.1:8000/api/v1/exports
# Download the download_url returned by the second request using the same server origin.
```

## Main API

- `POST /api/v1/documents`: upload one or more PDFs. Multiple files are appended in upload order.
- `GET /api/v1/documents/{id}/pages/{page}/preview`: render a page thumbnail.
- `GET /api/v1/documents/{id}/pages/{page}/edit-background`: render the page background
  with text removed. Pass `image_id` to remove one independently editable image while
  preserving overlapping page graphics; `remove_images=true` remains available for legacy
  clients and removes all independently editable images.
- `GET /api/v1/documents/{id}/pages/{page}/content`: parse text blocks, styled spans,
  character geometry, and editable image regions.
- `GET /api/v1/documents/{id}/pages/{page}/images/{image_id}/preview`: return a PNG source
  for the image editor.
- `POST /api/v1/images`: normalize a processed PNG, JPEG, or WebP image before insertion.
- `GET /api/v1/documents/{id}/fonts/{xref}`: serve a browser-compatible embedded font.
- `POST /api/v1/watermarks`: upload and normalize a PNG, JPEG, or WebP watermark image.
- `POST /api/v1/exports`: compose arbitrary source pages and blank pages into a new PDF.
- `POST /api/v1/previews`: render one edited page as a PNG. Accepts `page` and optional
  `max_edge` (128–1600 pixels, default 360; dialogs use 1200).
  Send `{"page": <one export page specification>}`; text/image edits, crop, rotation,
  and watermarks use the same composition path as export. Temporary files are cleaned up;
  no persistent export is created. The frontend refreshes both thumbnail views after
  selection changes with a short debounce and discards superseded responses.
- `GET /api/v1/exports/{id}/download`: download the generated copy.

Delete, copy, reorder, extract, merge, rotate, crop, and blank-page creation all map to the
ordered `pages` array accepted by the export endpoint. Each page can also carry a watermark
configuration with arbitrary rotation, page-relative scale, opacity, and normalized position.
Source pages may additionally carry `text_edits` and `image_edits`; export redacts changed
text regions, removes edited image XObjects by identity, and writes moved or replaced images
into the new copy. Render-only layers, reused image resources, and page-edge decorations are
kept in the page background instead of being exposed as independently editable images.
Multilingual output selects the configured Noto font by script. The server paths are listed in
`.env.example`.
When the source PDF embeds a compatible font and it contains every newly entered glyph, the
exporter reuses that font program. Otherwise it falls back to the configured Noto font rather
than risking missing glyphs from a subset font.

## Fine-grained text editing

Each text block returned by the content endpoint retains its legacy summary fields and now also
contains `lines[].spans[].characters[]`. A span includes its stable ID, text, bounding box,
baseline origin, direction (on its parent line), font resource candidates, font size, color,
opacity, style flags, ascender, descender, and per-character positions. `layout_version` is `2`,
and all returned geometry uses the rotated-page coordinate space displayed by the preview API.

For format-preserving edits, send one run per changed span. Unchanged spans are left untouched:

```json
{
  "filename": "edited-copy.pdf",
  "pages": [
    {
      "kind": "source",
      "document_id": "DOCUMENT_ID",
      "page_index": 0,
      "text_edits": [
        {
          "block_id": "block-3",
          "runs": [
            {
              "span_id": "block-3-line-0-span-1",
              "text": "Newbold",
              "overflow_policy": "error"
            }
          ]
        }
      ]
    }
  ]
}
```

`overflow_policy` defaults to `error`, preventing silent layout damage. Set it to `shrink` with
an explicit `minimum_font_scale` when shrinking is acceptable. The export response includes a
`text_edit_results` entry for every rendered run, reporting the actual font source, whether the
font was substituted, the effective font size, and whether shrinking occurred. Existing block-
level `text_edits` remain supported for older clients. A run only requires `span_id` and `text`;
the source bbox, origin, direction, font, size, color, opacity, style, and measured advance are
filled from the original PDF. Supply any of those fields only when intentionally overriding that
specific property.

## Image editing

`PageContent.images` identifies every editable image occurrence with a stable page-local ID,
rotated-page bounding box, source pixel metadata, and PNG preview URL. Upload the browser's
processed crop/rotation result to `/api/v1/images`, then move, replace, or delete the image while
exporting:

```json
{
  "filename": "edited-images.pdf",
  "pages": [
    {
      "kind": "source",
      "document_id": "DOCUMENT_ID",
      "page_index": 0,
      "image_edits": [
        {
          "image_id": "image-0",
          "action": "replace",
          "asset_id": "UPLOADED_IMAGE_ID",
          "bbox": { "x0": 120, "y0": 80, "x1": 320, "y1": 230 }
        },
        { "image_id": "image-1", "action": "delete" }
      ]
    }
  ]
}
```

Omit `asset_id` from a `replace` edit to reuse and move the original image. Bounding boxes use
the same rotated-page coordinate space as text edits and previews.

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check .
```
