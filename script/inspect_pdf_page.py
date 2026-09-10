#!/usr/bin/env python3
"""Dump the finest-grained information PyMuPDF can extract from one PDF page.

Page numbers are one-based by default. The output is JSON so that coordinates,
character-level text, resources, and PDF object details remain machine-readable.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import math
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pymupdf

FONT_FIELDS = (
    "xref",
    "extension",
    "type",
    "base_font",
    "resource_name",
    "encoding",
    "referencer_xref",
)
IMAGE_FIELDS = (
    "xref",
    "smask_xref",
    "width",
    "height",
    "bits_per_component",
    "colorspace",
    "alternate_colorspace",
    "resource_name",
    "filter",
    "referencer_xref",
)
XOBJECT_FIELDS = ("xref", "resource_name", "invoker_xref", "bbox")
WORD_FIELDS = ("x0", "y0", "x1", "y1", "text", "block_no", "line_no", "word_no")
BLOCK_FIELDS = ("x0", "y0", "x1", "y1", "content", "block_no", "block_type")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract character-level text and all practical page resources from one PDF page. "
            "The page argument is one-based unless --zero-based is supplied."
        )
    )
    parser.add_argument("pdf", type=Path, help="PDF file to inspect")
    parser.add_argument("page", type=int, help="page number (one-based by default)")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="write JSON to this file (default: stdout)",
    )
    parser.add_argument("--password", help="password for an encrypted PDF")
    parser.add_argument(
        "--zero-based",
        action="store_true",
        help="interpret PAGE as a zero-based page index",
    )
    parser.add_argument(
        "--binary",
        choices=("metadata", "base64"),
        default="metadata",
        help="record binary data as size/hash only, or include lossless base64 (default: metadata)",
    )
    parser.add_argument(
        "--include-streams",
        action="store_true",
        help="include decoded and raw PDF stream bytes using the selected --binary mode",
    )
    parser.add_argument(
        "--skip-tables",
        action="store_true",
        help="skip PyMuPDF table detection (useful for faster inspection)",
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation (default: 2)")
    return parser.parse_args()


class JsonConverter:
    """Convert PyMuPDF values into stable JSON-compatible structures."""

    def __init__(self, binary_mode: str) -> None:
        self.binary_mode = binary_mode

    def binary(self, value: bytes | bytearray | memoryview) -> dict[str, Any]:
        content = bytes(value)
        result: dict[str, Any] = {
            "byte_length": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        if self.binary_mode == "base64":
            result["encoding"] = "base64"
            result["data"] = base64.b64encode(content).decode("ascii")
        return result

    def convert(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return self.binary(value)
        if isinstance(value, Mapping):
            return {str(key): self.convert(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self.convert(item) for item in value]

        class_name = type(value).__name__
        if class_name == "Point" and hasattr(value, "x") and hasattr(value, "y"):
            return {"x": value.x, "y": value.y}
        if class_name in {"Rect", "IRect"}:
            return {"x0": value.x0, "y0": value.y0, "x1": value.x1, "y1": value.y1}
        if class_name == "Quad":
            return {
                "upper_left": self.convert(value.ul),
                "upper_right": self.convert(value.ur),
                "lower_left": self.convert(value.ll),
                "lower_right": self.convert(value.lr),
            }
        if class_name == "Matrix":
            return {
                "a": value.a,
                "b": value.b,
                "c": value.c,
                "d": value.d,
                "e": value.e,
                "f": value.f,
            }
        if hasattr(value, "__dict__"):
            return self.convert(vars(value))
        return repr(value)


def named_rows(rows: Iterable[Sequence[Any]], fields: Sequence[str]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        values = list(row)
        item = {name: values[index] for index, name in enumerate(fields) if index < len(values)}
        if len(values) > len(fields):
            item["extra"] = values[len(fields) :]
        result.append(item)
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_value(obj: Any, attribute: str) -> Any:
    try:
        value = getattr(obj, attribute)
        return value() if callable(value) else value
    except Exception as exc:  # PyMuPDF properties may reject some annotation/widget types.
        return {"error": f"{type(exc).__name__}: {exc}"}


def capture(
    target: dict[str, Any],
    errors: list[dict[str, str]],
    name: str,
    operation: Callable[[], Any],
) -> None:
    try:
        target[name] = operation()
    except Exception as exc:
        target[name] = None
        errors.append({"section": name, "error": f"{type(exc).__name__}: {exc}"})


def xref_details(
    document: pymupdf.Document,
    xref: int,
    converter: JsonConverter,
    include_streams: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {"xref": xref}
    try:
        result["object"] = document.xref_object(xref, compressed=False)
    except Exception as exc:
        result["object_error"] = f"{type(exc).__name__}: {exc}"

    try:
        result["keys"] = {
            key: {"type": kind, "value": value}
            for key in document.xref_get_keys(xref)
            for kind, value in [document.xref_get_key(xref, key)]
        }
    except Exception as exc:
        result["keys_error"] = f"{type(exc).__name__}: {exc}"

    try:
        is_stream = bool(document.xref_is_stream(xref))
        result["is_stream"] = is_stream
        if is_stream:
            decoded = document.xref_stream(xref)
            raw = document.xref_stream_raw(xref)
            result["decoded_stream"] = converter.binary(decoded)
            result["raw_stream"] = converter.binary(raw)
            if not include_streams:
                result["decoded_stream"].pop("data", None)
                result["raw_stream"].pop("data", None)
    except Exception as exc:
        result["stream_error"] = f"{type(exc).__name__}: {exc}"
    return result


def extract_fonts(
    document: pymupdf.Document,
    page: pymupdf.Page,
    converter: JsonConverter,
    include_streams: bool,
) -> list[dict[str, Any]]:
    fonts = named_rows(page.get_fonts(full=True), FONT_FIELDS)
    for font in fonts:
        xref = int(font["xref"])
        if xref <= 0:
            continue
        font["pdf_object"] = xref_details(document, xref, converter, include_streams)
        try:
            basename, extension, font_type, content = document.extract_font(xref)
            font["extracted_font"] = {
                "basename": basename,
                "extension": extension,
                "type": font_type,
                "content": converter.binary(content),
            }
        except Exception as exc:
            font["extraction_error"] = f"{type(exc).__name__}: {exc}"
    return fonts


def extract_images(
    document: pymupdf.Document,
    page: pymupdf.Page,
    converter: JsonConverter,
    include_streams: bool,
) -> dict[str, Any]:
    resources = named_rows(page.get_images(full=True), IMAGE_FIELDS)
    seen_xrefs: set[int] = set()
    extracted: list[dict[str, Any]] = []
    for image in resources:
        xref = int(image["xref"])
        if xref <= 0 or xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)
        item: dict[str, Any] = {
            "xref": xref,
            "pdf_object": xref_details(document, xref, converter, include_streams),
        }
        try:
            payload = document.extract_image(xref)
            item["metadata"] = {
                key: converter.binary(value)
                if isinstance(value, (bytes, bytearray, memoryview))
                else value
                for key, value in payload.items()
            }
        except Exception as exc:
            item["extraction_error"] = f"{type(exc).__name__}: {exc}"
        extracted.append(item)
    return {
        "resource_entries": resources,
        "displayed_instances": page.get_image_info(hashes=True, xrefs=True),
        "extracted_unique_images": extracted,
    }


def extract_annotations(page: pymupdf.Page) -> list[dict[str, Any]]:
    attributes = (
        "xref",
        "type",
        "rect",
        "info",
        "flags",
        "colors",
        "border",
        "opacity",
        "blendmode",
        "vertices",
        "has_popup",
        "popup_xref",
        "popup_rect",
        "line_ends",
    )
    annotations = []
    iterator = page.annots()
    if iterator is None:
        return annotations
    for annotation in iterator:
        annotations.append({name: safe_value(annotation, name) for name in attributes})
    return annotations


def extract_widgets(page: pymupdf.Page) -> list[dict[str, Any]]:
    attributes = (
        "xref",
        "rect",
        "field_name",
        "field_label",
        "field_type",
        "field_type_string",
        "field_value",
        "field_flags",
        "field_display",
        "field_choices",
        "text_font",
        "text_fontsize",
        "text_color",
        "fill_color",
        "border_color",
        "border_width",
        "border_style",
        "button_states",
        "on_state",
    )
    widgets = []
    iterator = page.widgets()
    if iterator is None:
        return widgets
    for widget in iterator:
        widgets.append({name: safe_value(widget, name) for name in attributes})
    return widgets


def extract_tables(page: pymupdf.Page) -> list[dict[str, Any]]:
    tables = []
    # PyMuPDF prints an optional pymupdf-layout recommendation to stdout. Suppress
    # it so stdout remains valid JSON when the caller does not use --output.
    with contextlib.redirect_stdout(io.StringIO()):
        finder = page.find_tables()
    for index, table in enumerate(finder.tables):
        header = getattr(table, "header", None)
        tables.append(
            {
                "index": index,
                "bbox": table.bbox,
                "row_count": table.row_count,
                "col_count": table.col_count,
                "cells": table.cells,
                "rows": [row.cells for row in table.rows],
                "header": (
                    {
                        "bbox": header.bbox,
                        "cells": header.cells,
                        "names": header.names,
                        "external": header.external,
                    }
                    if header is not None
                    else None
                ),
                "extracted_data": table.extract(),
            }
        )
    return tables


def document_info(document: pymupdf.Document, converter: JsonConverter) -> dict[str, Any]:
    property_names = (
        "is_pdf",
        "is_encrypted",
        "needs_pass",
        "is_repaired",
        "is_fast_webaccess",
        "is_form_pdf",
        "is_reflowable",
        "page_count",
        "permissions",
        "metadata",
        "page_layout",
        "page_mode",
        "language",
        "xref_length",
    )
    result = {name: safe_value(document, name) for name in property_names}
    result["pymupdf_version"] = pymupdf.VersionBind
    result["table_of_contents"] = document.get_toc(simple=False)
    result["page_labels"] = document.get_page_labels()
    result["optional_content_groups"] = converter.convert(document.get_ocgs())
    result["embedded_file_names"] = document.embfile_names()
    return result


def page_info(
    document: pymupdf.Document,
    page: pymupdf.Page,
    requested_page: int,
    page_index: int,
    converter: JsonConverter,
    include_streams: bool,
    include_tables: bool,
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    result: dict[str, Any] = {
        "requested_page": requested_page,
        "page_index_zero_based": page_index,
        "page_number_one_based": page_index + 1,
        "label": page.get_label(),
        "xref": page.xref,
        "rotation": page.rotation,
        "rect": page.rect,
        "bound": page.bound(),
        "mediabox": page.mediabox,
        "cropbox": page.cropbox,
        "cropbox_position": page.cropbox_position,
        "artbox": page.artbox,
        "bleedbox": page.bleedbox,
        "trimbox": page.trimbox,
        "transformation_matrix": page.transformation_matrix,
        "rotation_matrix": page.rotation_matrix,
        "derotation_matrix": page.derotation_matrix,
        "text_extraction_flags": {
            "rawdict": pymupdf.TEXTFLAGS_RAWDICT,
            "words": pymupdf.TEXTFLAGS_WORDS,
            "blocks": pymupdf.TEXTFLAGS_BLOCKS,
            "note": "Coordinates are in PyMuPDF's unrotated page coordinate system.",
        },
    }

    capture(result, errors, "plain_text", lambda: page.get_text("text", sort=False))
    capture(
        result,
        errors,
        "raw_text_dictionary",
        lambda: page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT, sort=False),
    )
    capture(result, errors, "text_trace", page.get_texttrace)
    capture(
        result,
        errors,
        "words",
        lambda: named_rows(
            page.get_text("words", flags=pymupdf.TEXTFLAGS_WORDS, sort=False),
            WORD_FIELDS,
        ),
    )
    capture(
        result,
        errors,
        "blocks",
        lambda: named_rows(
            page.get_text("blocks", flags=pymupdf.TEXTFLAGS_BLOCKS, sort=False),
            BLOCK_FIELDS,
        ),
    )
    capture(result, errors, "display_list_bbox_log", lambda: page.get_bboxlog(layers=True))
    capture(result, errors, "vector_drawings", lambda: page.get_drawings(extended=True))
    capture(result, errors, "links", page.get_links)
    capture(result, errors, "annotations", lambda: extract_annotations(page))
    capture(result, errors, "widgets", lambda: extract_widgets(page))
    capture(
        result,
        errors,
        "fonts",
        lambda: extract_fonts(document, page, converter, include_streams),
    )
    capture(
        result,
        errors,
        "images",
        lambda: extract_images(document, page, converter, include_streams),
    )
    capture(
        result,
        errors,
        "form_xobjects",
        lambda: named_rows(page.get_xobjects(), XOBJECT_FIELDS),
    )
    if include_tables:
        capture(result, errors, "detected_tables", lambda: extract_tables(page))

    related_xrefs = {page.xref, *page.get_contents()}
    related_xrefs.update(int(row[0]) for row in page.get_fonts(full=True) if int(row[0]) > 0)
    related_xrefs.update(int(row[0]) for row in page.get_images(full=True) if int(row[0]) > 0)
    related_xrefs.update(int(row[0]) for row in page.get_xobjects() if int(row[0]) > 0)
    result["content_stream_xrefs"] = page.get_contents()
    result["related_pdf_objects"] = [
        xref_details(document, xref, converter, include_streams) for xref in sorted(related_xrefs)
    ]
    result["section_errors"] = errors
    return result


def inspect_pdf(args: argparse.Namespace) -> dict[str, Any]:
    pdf_path = args.pdf.expanduser().resolve()
    if not pdf_path.is_file():
        raise ValueError(f"PDF does not exist or is not a file: {pdf_path}")

    converter = JsonConverter(args.binary)
    document = pymupdf.open(pdf_path)
    try:
        if not document.is_pdf:
            raise ValueError(f"not a PDF document: {pdf_path}")
        if document.needs_pass:
            if not args.password or not document.authenticate(args.password):
                raise ValueError("the PDF is encrypted; provide the correct --password")

        page_index = args.page if args.zero_based else args.page - 1
        if page_index < 0 or page_index >= document.page_count:
            basis = "zero-based index" if args.zero_based else "one-based page number"
            raise ValueError(
                f"invalid {basis} {args.page}; document has {document.page_count} pages"
            )

        page = document.load_page(page_index)
        result = {
            "source": {
                "path": str(pdf_path),
                "file_size": pdf_path.stat().st_size,
                "sha256": sha256_file(pdf_path),
            },
            "document": document_info(document, converter),
            "page": page_info(
                document,
                page,
                args.page,
                page_index,
                converter,
                args.include_streams,
                not args.skip_tables,
            ),
        }
        return converter.convert(result)
    finally:
        document.close()


def main() -> int:
    args = parse_args()
    try:
        result = inspect_pdf(args)
        serialized = json.dumps(
            result,
            ensure_ascii=False,
            indent=args.indent,
            allow_nan=False,
        )
        if args.output:
            output_path = args.output.expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(serialized + "\n", encoding="utf-8")
        else:
            print(serialized)
        return 0
    except (ValueError, OSError, pymupdf.FileDataError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
