# PDF Editor API

FastAPI service for coarse-grained, non-destructive PDF editing. Uploaded originals are
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
- `POST /api/v1/exports`: compose arbitrary source pages and blank pages into a new PDF.
- `GET /api/v1/exports/{id}/download`: download the generated copy.

Delete, copy, reorder, extract, merge, rotate, crop, and blank-page creation all map to the
ordered `pages` array accepted by the export endpoint.

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check .
```
