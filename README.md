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

## MCP integration

The backend exposes 19 typed tools at `/mcp/` beside the existing REST API.
The adapter uses the official MCP Python SDK 1.x, Streamable HTTP, stateless requests,
and JSON responses. It shares `EditorService`, `PdfService`, and `FileStorage` with
REST; it does not send HTTP requests back to itself or duplicate PDF engine logic.
The parent FastAPI lifespan starts and stops the mounted MCP session manager.

```bash
uv sync --locked --extra dev
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Connect a compatible agent's Streamable HTTP client to `http://127.0.0.1:8000/mcp/`.
For Compose, substitute the configured host port (for example, 8020).
Use the trailing slash to avoid redirects. Tools are discoverable with `tools/list`;
all parameter descriptions and enums are owned by this service, not hardcoded in an agent.
This service executes structured edit requests; a separate agent/model interprets natural language.
No model or chat UI is embedded in the backend.

| Tool | Purpose |
| --- | --- |
| `import_file_from_url` | Import a public/signed URL as a document, image or watermark |
| `list_pdf_capabilities` | Operations, limits, unsupported features, uploads and coordinates |
| `get_document_info` | Original filename, page count, page sizes and rotations |
| `get_page_content` | Block/span/image IDs; summary, spans, or optional character detail |
| `find_text` | Literal line search across styled spans, with candidate context and offsets |
| `render_page` | Source page PNG image content |
| `extract_pages` | Export exactly the selected source pages |
| `merge_documents` | Merge complete documents in supplied order |
| `reorder_pages` | Explicit output order; omit or repeat pages to remove or duplicate |
| `compose_pdf` | Explicit page plan with blanks, crop, rotation, watermarks, text and images |
| `replace_text` | Replace one whole span, retaining all other pages in a new export |
| `preview_edits` | PNG preview of a proposed edited page, without persisting a PDF |
| `import_export` | Promote this service's export to a new immutable source document |
| `create_draft` | Create a persistent plan with all source pages and revision 1 |
| `get_draft` | Read the current revision and complete page/edit plan |
| `update_draft` | Replace the complete plan with optimistic revision checking |
| `edit_draft_text` | Change one span while retaining other draft edits |
| `preview_draft_page` | Preview an output page at a specific draft revision |
| `export_draft` | Export a snapshot of a specific draft revision |

Export tools return a validated structured artifact and a PDF resource link. PNG tools
return image content directly, without duplicating image bytes in structured output.
Resource links point to HTTP downloads; they are not a separate MCP `resources/read` endpoint.

### File references and page conventions

For a public/signed file URL, call `import_file_from_url(file_url, filename?, kind="document")`.
Use the returned `id` as `document_id`. `kind="image"` and `kind="watermark"` import replacement
images and watermark assets using the same validators as their REST upload endpoints.
REST clients can use `POST /api/v1/files/import-url` with the same JSON fields.
Existing `POST /api/v1/documents` multipart uploads (`files`) and their IDs remain supported.
Editing operations still use service references. Local paths and Base64 PDF input are not supported.
Upload image/watermark assets using the existing `/api/v1/images` or `/api/v1/watermarks`
endpoints (multipart field `file`). Then use their IDs in `compose_pdf`/`update_draft`.

**MCP page numbers start at 1; REST `page_index` remains zero-based.**
For “extract page 3”, call:

```json
{
  "name": "extract_pages",
  "arguments": {
    "document_id": "UPLOAD_RESULT_ID",
    "page_numbers": [3],
    "filename": "page-3.pdf"
  }
}
```

The adapter translates this to REST page index 2. Output preserves the supplied order.
`compose_pdf` uses `page_number` for each source page; page list order is output order.
Geometry is in PDF points (1/72 inch), in the source's **rotated-page** coordinate space,
with top-left origin, before the requested output crop and rotation. Crop values are fractions
removed from each edge. Page metadata/content give the dimensions needed for positioning.
Source files remain unchanged. Import an export with `import_export` before treating it as a
new source; read its layout again because block/span IDs can change after composition.

### Text editing and cumulative drafts

Use `find_text` to locate candidate lines and spans. It searches literal text within a line,
including across spans with different styles; it does not perform OCR, regex, cross-line
paragraph matching, or automatic semantic identification of table cells/headings.
It returns all candidates up to `max_results`, and a `limit_reached` flag. Do not silently
choose the first ambiguous result. Read context or ask the user to identify the intended target.
Span offsets are Python character offsets, not byte offsets; Unicode case conversion that
would change offsets requires `case_sensitive=true`.

`replace_text` accepts `replacement` with `document_id`, original `page_number`, `block_id`,
`span_id`, `expected_text`, and `new_text`. Both text fields describe the **complete span**;
preserve unaffected characters when replacing a substring. Source geometry and style are
recovered server-side. Default `overflow_policy=error` rejects text that does not fit.
Request `shrink` with an explicit `minimum_font_scale` only when shrinking is acceptable.
The export's `text_edit_results` report font substitution, actual font size and shrinking.

For several edits in one conversation:

1. `create_draft(document_id)` returns a draft ID and revision 1.
2. Locate targets on the original source using `find_text`/`get_page_content`.
3. Call `edit_draft_text` with the latest `expected_revision`. For an already edited span,
   `expected_text` must be its current draft text. Each successful change increments revision.
4. For images, page order, crop, rotation, watermarks or rich text, read `get_draft`, modify
   its complete `request`, and submit it through `update_draft`. Carry forward retained edits.
5. Inspect `preview_draft_page`, then call `export_draft` at that revision.

`edit_draft_text.replacement.page_number` means the **original source page**;
`preview_draft_page.page_number` means the **output list position**. Duplicate source pages
require explicit `update_draft` edits to disambiguate their output occurrence.
Draft updates use a SQLite compare-and-swap, so concurrent stale revisions return conflict
without overwriting newer work. Drafts and original upload metadata persist in
`editor.sqlite3` inside the active storage namespace. Saving a draft saves a plan; rendering
and layout/overflow validation occur during preview/export. Export reads an immutable
revision snapshot even if another client subsequently changes the draft.

### Authentication and deployment

| Setting | Default | Behavior |
| --- | --- | --- |
| `PDF_EDITOR_MCP_ENABLED` | `true` | Set false to remove the MCP mount |
| `PDF_EDITOR_MCP_MAX_CONCURRENCY` | `2` | Bound simultaneous MCP engine operations per worker |
| `PDF_EDITOR_API_KEYS` | `{}` | JSON map of stable identity names to distinct secret Bearer tokens |
| `PDF_EDITOR_PUBLIC_BASE_URL` | empty | Public origin plus optional proxy prefix for download links and MCP Host validation |

An empty key mapping retains the existing **local single-user** workflow. Before remote use,
configure strong tokens, for example `{"agent-user":"REPLACE_WITH_A_RANDOM_SECRET"}`.
Then send `Authorization: Bearer <secret>` on **both REST and MCP**, including legacy `download_url` downloads.
New `files[].file_url` links are signed, export-scoped and can be fetched without a Bearer token until expiry.
`/health` and CORS preflight remain unauthenticated. This is static Bearer authentication,
not OAuth discovery; use a client that supports custom authorization headers.
Existing browser clients also need to send the header if you enable authentication.

Each identity receives isolated originals, images, watermarks, exports, metadata and drafts
under `DATA_DIR/owners/<identity-hash>/`. Identity comes from authentication, never tool arguments.
Keep the identity name stable when rotating its secret. Enabling authentication does not
migrate local anonymous files into any identity; re-upload them under the intended identity.
Older anonymous uploads still work without authentication, but their original filename was
not persisted by earlier versions and is reported as `document.pdf`.

For a proxy deployment, set e.g. `PDF_EDITOR_PUBLIC_BASE_URL=https://example.com/pdf-editor`.
Forward `/pdf-editor/mcp/` to `/mcp/`, preserve `Authorization` and the configured public Host,
and use TLS. Localhost/loopback hosts are allowed by default; the configured public base URL
adds its authority to the SDK's DNS-rebinding allowlist. Without a public base URL, MCP builds
download links from the accepted HTTP request origin and ASGI deployment prefix. The REST
export response keeps its relative-link contract. Tool errors expose validation/conflict
status rather than pretending success. A 409 needs a fresh draft/content read before retrying.

MCP limits output to 2000 pages, search to 100 pages per call, search results to 100,
preview edge length to 1600px and concurrency to the configured worker limit.
It does not add a background job queue or per-user billing/rate-limit system.
PDF contents must be treated by the calling agent as untrusted document data, never instructions.

Composition retains visible page content but **does not guarantee preservation of bookmarks,
links, forms, annotations or signatures**. OCR, vertical span replacement, semantic
paragraph/table recognition and automatic cross-paragraph reflow remain unsupported.

### Verification

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
# Against a disposable running backend; creates PDFs and a draft:
.venv/bin/python script/smoke_mcp.py --url http://127.0.0.1:8000
# Authenticated smoke: provide PDF_EDITOR_SMOKE_TOKEN in the environment.
```

The HTTP tests cover MCP discovery/schema validation, third-page extraction, export import,
span search across fonts, cumulative edits, revision conflicts, atomic concurrent updates,
PNG preview, page composition, authentication, cross-identity isolation and Host rejection.
The smoke script uses the official SDK against a real HTTP listener and downloads/inspects
both the extracted and edited PDFs. These checks do not constitute a natural-language model
or production-proxy E2E test; that requires a configured agent/model and deployed endpoint.


### URL imports and portable result files

```json
{"file_url":"https://files.example.com/manual.pdf?signature=...","kind":"document"}
```

The importer streams to bounded temporary storage, verifies the actual file format using existing
upload validators, and preserves the authenticated caller's namespace. HTTP(S) sources must be
public or their exact hostname/IP must be explicitly allowed. Each redirect is checked again;
DNS answers are validated and TCP connections are pinned to a validated IP while HTTPS retains
hostname certificate verification. User credentials, fragments, arbitrary forwarded headers,
unsupported schemes and compressed HTTP transfer representations are rejected. Up to three
redirects are followed. Source query strings (including cloud signatures) are preserved. A cloud
share page must first be resolved to a real download URL. URL inputs are not persisted as metadata.

New configuration:

```dotenv
# JSON list of exact private storage hosts (no ports or wildcards); public hosts need no entry
PDF_EDITOR_URL_IMPORT_ALLOWED_HOSTS=["files.internal","192.168.3.67"]
PDF_EDITOR_URL_IMPORT_TIMEOUT_SECONDS=120
PDF_EDITOR_DOWNLOAD_URL_TTL_SECONDS=3600
# At least 32 random characters; share across workers/restarts
PDF_EDITOR_DOWNLOAD_SIGNING_KEY=replace-with-at-least-32-random-characters
```

The URL import byte limit is the existing document/image/watermark upload limit. Imports are
limited to two concurrent downloads per process. Downloads do not use ambient proxy settings.
Configure `PDF_EDITOR_PUBLIC_BASE_URL` to a downstream-accessible origin/prefix.
Exports now include `files[]` with `file_url`, `filename`, `mime_type`, `size_bytes`, and ISO UTC
`expires_at`. MCP resource links point to the signed URL. Legacy `download_url` stays compatible
and keeps its Bearer authentication. Signed URLs grant only GET access to one export under its
original identity; changing the filename, export, owner, expiry or signature invalidates the link.
They grant no editing/API access and should be handled as temporary credentials.
Without a configured signing key a process-local random key is used; restart invalidates old links
and multiple workers cannot share them. The signing key is independent of API keys. Changing it
revokes old links. Signed URL expiry does not delete the export; export again to get a fresh URL.
No cloud storage account or permanent cloud-file writeback is configured by this change.
