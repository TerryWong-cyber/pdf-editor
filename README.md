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

## Main API

- `POST /api/v1/documents`: upload one or more PDFs. Multiple files are appended in upload order.
- `GET /api/v1/documents/{id}/pages/{page}/preview`: render a page thumbnail.
- `GET /api/v1/documents/{id}/pages/{page}/edit-background`: render the page background with text removed.
- `GET /api/v1/documents/{id}/pages/{page}/content`: parse text blocks, styled spans,
  character geometry, and editable image regions.
- `GET /api/v1/documents/{id}/pages/{page}/images/{image_id}/preview`: return a PNG source
  for the image editor.
- `POST /api/v1/images`: normalize a processed PNG, JPEG, or WebP image before insertion.
- `GET /api/v1/documents/{id}/fonts/{xref}`: serve a browser-compatible embedded font.
- `POST /api/v1/watermarks`: upload and normalize a PNG, JPEG, or WebP watermark image.
- `POST /api/v1/exports`: compose arbitrary source pages and blank pages into a new PDF.
- `GET /api/v1/exports/{id}/download`: download the generated copy.

Delete, copy, reorder, extract, merge, rotate, crop, and blank-page creation all map to the
ordered `pages` array accepted by the export endpoint. Each page can also carry a watermark
configuration with arbitrary rotation, page-relative scale, opacity, and normalized position.
Source pages may additionally carry `text_edits` and `image_edits`; export redacts only the
changed original regions and writes moved text or deleted/replaced images into the new copy.
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
