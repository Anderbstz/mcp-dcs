"""Google Docs domain logic: document-ID parsing and Docs API operations.

Everything here is deliberately free of MCP concerns so it can be unit-tested
and reused. Functions that need Google credentials receive an already-built
``googleapiclient`` service object; authentication lives in ``auth.py``.
"""

from __future__ import annotations

import array
import difflib
import re
import time
import unicodedata
from collections import Counter
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# A Google Docs URL looks like:
#   https://docs.google.com/document/d/<ID>/edit#heading=h.xxxx
#   https://docs.google.com/document/d/<ID>/edit?usp=sharing
#   https://docs.google.com/document/u/0/d/<ID>/edit        (signed-in account form)
# The optional `u/<n>/` segment appears when the user is logged into several
# Google accounts, so it must be tolerated.
DOC_ID_IN_URL = re.compile(r"/document/(?:u/\d+/)?d/([A-Za-z0-9_-]+)")

# A bare document ID: URL-safe base64-ish characters only, no spaces.
BARE_DOC_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def extract_doc_id(url_or_id: str) -> str:
    """Return the Google Docs document ID from a URL, or a bare ID unchanged.

    Accepts:
      * a full URL such as ``https://docs.google.com/document/d/<ID>/edit``
      * the same URL with a ``?usp=sharing`` query string
      * a raw document ID

    Raises:
      ValueError: if the input is empty or does not contain a plausible ID.
    """
    if not isinstance(url_or_id, str):
        raise ValueError("url_or_id must be a string")

    candidate = url_or_id.strip()
    if not candidate:
        raise ValueError("Empty document URL or ID")

    match = DOC_ID_IN_URL.search(candidate)
    if match:
        return match.group(1)

    if BARE_DOC_ID.match(candidate):
        return candidate

    raise ValueError(
        f"Could not extract a Google Docs document ID from {url_or_id!r}. "
        "Pass a full docs.google.com/document/d/<ID>/... URL or the bare ID."
    )


def build_docs_service(credentials: Any) -> Any:
    """Build the Google Docs v1 API client from credentials."""
    return build("docs", "v1", credentials=credentials, cache_discovery=False)


def _paragraph_plain_text(paragraph: dict[str, Any]) -> str:
    """Concatenate the text runs of a single paragraph element."""
    text = ""
    for element in paragraph.get("elements", []):
        text_run = element.get("textRun")
        if text_run:
            text += text_run.get("content", "")
    return text


def document_to_text(document: dict[str, Any]) -> str:
    """Flatten a Docs API document into plain text.

    Headings and list structure are annotated with lightweight markers so the
    model gets a sense of structure without the raw API JSON.
    """
    lines: list[str] = []
    for element in document.get("body", {}).get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue

        style = paragraph.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
        text = _paragraph_plain_text(paragraph).rstrip("\n")

        if style == "TITLE":
            lines.append(f"# {text}")
        elif style == "SUBTITLE":
            lines.append(f"## {text}")
        elif style.startswith("HEADING_"):
            level = style.removeprefix("HEADING_")
            hashes = "#" * min(int(level) + 1, 6) if level.isdigit() else "##"
            lines.append(f"{hashes} {text}")
        else:
            lines.append(text)

    return "\n".join(lines)


def get_document(service: Any, doc_id: str) -> dict[str, Any]:
    """Fetch the full document resource."""
    return service.documents().get(documentId=doc_id).execute()


def read_document(service: Any, doc_id: str) -> str:
    """Return a readable text + heading summary for a document."""
    document = get_document(service, doc_id)
    title = document.get("title", "(untitled)")
    body_text = document_to_text(document)
    return f"Title: {title}\n\n{body_text}"


def replace_all_text(
    service: Any, doc_id: str, find: str, replace: str, match_case: bool = True
) -> dict[str, Any]:
    """Replace every occurrence of ``find`` with ``replace`` in the document."""
    requests: list[dict[str, Any]] = [
        {
            "replaceAllText": {
                "containsText": {"text": find, "matchCase": match_case},
                "replaceText": replace,
            }
        }
    ]
    return (
        service.documents()
        .batchUpdate(documentId=doc_id, body={"requests": requests})
        .execute()
    )


def insert_text(service: Any, doc_id: str, text: str, index: int) -> dict[str, Any]:
    """Insert ``text`` at a 1-based character ``index``."""
    if index < 1:
        raise ValueError("index must be >= 1 (the document body starts at index 1)")
    requests = [{"insertText": {"location": {"index": index}, "text": text}}]
    return (
        service.documents()
        .batchUpdate(documentId=doc_id, body={"requests": requests})
        .execute()
    )


def append_text(service: Any, doc_id: str, text: str) -> dict[str, Any]:
    """Append ``text`` at the end of the document body."""
    document = get_document(service, doc_id)
    content = document.get("body", {}).get("content", [])
    end_index = content[-1].get("endIndex") if content else 2
    # The body's final newline occupies the last position; insert just before it.
    insert_at = max((end_index or 2) - 1, 1)
    return insert_text(service, doc_id, text, insert_at)


def create_document(service: Any, title: str) -> dict[str, Any]:
    """Create a new empty Google Doc and return the API response (with ``documentId``)."""
    return service.documents().create(body={"title": title}).execute()


def append_styled_text(
    service: Any,
    doc_id: str,
    text: str,
    font_family: str = "Arial",
    font_size_pt: float = 11.0,
) -> dict[str, Any]:
    """Append ``text`` at the end of the document body with an explicit font.

    Applies ``font_family`` (e.g. "Arial") and ``font_size_pt`` (points) to the
    inserted range using ``updateTextStyle``. Returns the styling response.

    Note: Docs API indices are UTF-16 code units. For typical ASCII/BMP text
    this matches ``len(text)``; astral characters (emoji) would shift indices.
    """
    document = get_document(service, doc_id)
    content = document.get("body", {}).get("content", [])
    end_index = content[-1].get("endIndex") if content else 2
    insert_at = max((end_index or 2) - 1, 1)

    # 1) Insert the raw text at the end of the body.
    service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": insert_at}, "text": text}}]},
    ).execute()

    # 2) Style exactly the inserted range.
    start = insert_at
    end = insert_at + len(text)
    return (
        service.documents()
        .batchUpdate(
            documentId=doc_id,
            body={
                "requests": [
                    {
                        "updateTextStyle": {
                            "range": {"startIndex": start, "endIndex": end},
                            "textStyle": {
                                "weightedFontFamily": {
                                    "fontFamily": font_family,
                                    "weight": 400,
                                },
                                "fontSize": {"magnitude": font_size_pt, "unit": "PT"},
                            },
                            "fields": "weightedFontFamily,fontSize",
                        }
                    }
                ]
            },
        )
        .execute()
    )

# ---------------------------------------------------------------------------
# Formatting engine (formato-ander)
# ---------------------------------------------------------------------------

FORMATO_ANDER_SOURCE_ID = "13qJlYtdxS_CxEjZ95akzmqTJjiS3Elg4"

_PT = "PT"
_DEFAULT_BATCH_SIZE = 50


def _range(start_index: int, end_index: int) -> dict[str, Any]:
    return {"startIndex": start_index, "endIndex": end_index}


def _run_batch(
    service: Any, doc_id: str, requests: list[dict[str, Any]], batch_size: int = _DEFAULT_BATCH_SIZE
) -> list[dict[str, Any]]:
    """Execute ``requests`` in chunks to stay below batchUpdate payload limits.

    Transient network failures and 429/5xx responses are retried with
    exponential backoff; the chunk is atomic server-side, so a retry cannot
    duplicate a successfully applied chunk.
    """
    if not requests:
        return []
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    responses: list[dict[str, Any]] = []
    for offset in range(0, len(requests), batch_size):
        chunk = requests[offset : offset + batch_size]
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                responses.append(
                    service.documents()
                    .batchUpdate(documentId=doc_id, body={"requests": chunk})
                    .execute()
                )
                last_error = None
                break
            except HttpError as exc:
                status = getattr(exc.resp, "status", 0)
                last_error = exc
                if status < 500 and status != 429:
                    raise
            except (ConnectionError, OSError) as exc:
                last_error = exc
            time.sleep(min(2**attempt, 20))
        if last_error is not None:
            raise last_error
    return responses


def update_paragraph_style(
    service: Any,
    doc_id: str,
    start_index: int,
    end_index: int,
    *,
    named_style: str | None = None,
    alignment: str | None = None,
    indent_first_line_pt: float | None = None,
    indent_start_pt: float | None = None,
    line_spacing: float | None = None,
    space_above_pt: float | None = None,
    space_below_pt: float | None = None,
) -> dict[str, Any]:
    """Apply paragraph-level formatting to ``[start_index, end_index)``.

    ``line_spacing`` is a percentage (100 = single, 150 = 1.5). The ``fields``
    mask is built only from the parameters actually supplied.
    """
    paragraph_style: dict[str, Any] = {}
    fields: list[str] = []

    if named_style is not None:
        paragraph_style["namedStyleType"] = named_style
        fields.append("namedStyleType")
    if alignment is not None:
        paragraph_style["alignment"] = alignment
        fields.append("alignment")
    if indent_first_line_pt is not None:
        paragraph_style["indentFirstLine"] = {"magnitude": indent_first_line_pt, "unit": _PT}
        fields.append("indentFirstLine")
    if indent_start_pt is not None:
        paragraph_style["indentStart"] = {"magnitude": indent_start_pt, "unit": _PT}
        fields.append("indentStart")
    if line_spacing is not None:
        paragraph_style["lineSpacing"] = line_spacing
        fields.append("lineSpacing")
    if space_above_pt is not None:
        paragraph_style["spaceAbove"] = {"magnitude": space_above_pt, "unit": _PT}
        fields.append("spaceAbove")
    if space_below_pt is not None:
        paragraph_style["spaceBelow"] = {"magnitude": space_below_pt, "unit": _PT}
        fields.append("spaceBelow")

    if not fields:
        raise ValueError("At least one paragraph style property must be provided")

    request = {
        "updateParagraphStyle": {
            "range": _range(start_index, end_index),
            "paragraphStyle": paragraph_style,
            "fields": ",".join(fields),
        }
    }
    return _run_batch(service, doc_id, [request])[0]


def update_text_style(
    service: Any,
    doc_id: str,
    start_index: int,
    end_index: int,
    *,
    bold: bool | None = None,
    italic: bool | None = None,
    underline: bool | None = None,
    font_family: str | None = None,
    font_size_pt: float | None = None,
) -> dict[str, Any]:
    """Apply text-run formatting to ``[start_index, end_index)``.

    The ``fields`` mask is built only from the parameters actually supplied.
    """
    text_style: dict[str, Any] = {}
    fields: list[str] = []

    if bold is not None:
        text_style["bold"] = bold
        fields.append("bold")
    if italic is not None:
        text_style["italic"] = italic
        fields.append("italic")
    if underline is not None:
        text_style["underline"] = underline
        fields.append("underline")
    if font_family is not None:
        text_style["weightedFontFamily"] = {"fontFamily": font_family, "weight": 400}
        fields.append("weightedFontFamily")
    if font_size_pt is not None:
        text_style["fontSize"] = {"magnitude": font_size_pt, "unit": _PT}
        fields.append("fontSize")

    if not fields:
        raise ValueError("At least one text style property must be provided")

    request = {
        "updateTextStyle": {
            "range": _range(start_index, end_index),
            "textStyle": text_style,
            "fields": ",".join(fields),
        }
    }
    return _run_batch(service, doc_id, [request])[0]


def create_bullets(
    service: Any, doc_id: str, start_index: int, end_index: int, bullet_preset: str
) -> dict[str, Any]:
    """Turn the paragraphs in ``[start_index, end_index)`` into a bulleted list."""
    if not bullet_preset:
        raise ValueError("bullet_preset is required")
    request = {
        "createParagraphBullets": {
            "range": _range(start_index, end_index),
            "bulletPreset": bullet_preset,
        }
    }
    return _run_batch(service, doc_id, [request])[0]


def insert_toc(service: Any, doc_id: str, index: int) -> dict[str, Any]:
    """Insert a table of contents at ``index`` (1-based)."""
    if index < 1:
        raise ValueError("index must be >= 1")
    request = {"insertTableOfContents": {"location": {"index": index}}}
    return _run_batch(service, doc_id, [request])[0]


def _iter_paragraph_elements(content: list[dict[str, Any]]):
    """Yield paragraph elements from body content, descending into tables."""
    for element in content:
        if "paragraph" in element:
            yield element
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    yield from _iter_paragraph_elements(cell.get("content", []))


def paragraph_spans(document: dict[str, Any]) -> list[tuple[int, int, str, str]]:
    """Return ``(start_index, end_index, named_style, text)`` for body paragraphs.

    Paragraphs inside tables are included as well. Indices are the Docs API
    UTF-16 code-unit offsets from ``element.startIndex``/``element.endIndex``.
    """
    spans: list[tuple[int, int, str, str]] = []
    for element in _iter_paragraph_elements(document.get("body", {}).get("content", [])):
        paragraph = element["paragraph"]
        style = paragraph.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
        text = _paragraph_plain_text(paragraph)
        spans.append((element["startIndex"], element["endIndex"], style, text))
    return spans


def _normalize_text(text: str) -> str:
    """Lowercase, strip accents and collapse whitespace for fuzzy matching."""
    text = text.replace("\x0b", " ").replace("\u00a0", " ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text).strip().lower()


def _weighted_dominant(values: list[tuple[int, Any]], *, include_none: bool = False) -> Any:
    """Return the value with the largest total text length (None ignored)."""
    counts: Counter = Counter()
    for weight, value in values:
        if weight <= 0:
            continue
        if value is None and not include_none:
            continue
        counts[value] += weight
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _mode(values: list[Any]) -> Any:
    """Most common value, counting ``None`` as a real value."""
    if not values:
        return None
    return Counter(values).most_common(1)[0][0]


def derive_style_profile(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Derive per-named-style formatting from a document.

    Run properties are weighted by run text length so a single stray run cannot
    win. When a run does not set a property, the document's ``namedStyles``
    definition is used, then the NORMAL_TEXT baseline.
    """
    run_values: dict[str, dict[str, list[tuple[int, Any]]]] = {}
    paragraph_values: dict[str, dict[str, list[Any]]] = {}

    for element in _iter_paragraph_elements(document.get("body", {}).get("content", [])):
        paragraph = element["paragraph"]
        style = paragraph.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
        run_bucket = run_values.setdefault(
            style,
            {
                "font_family": [],
                "font_size": [],
                "bold": [],
                "italic": [],
                "underline": [],
            },
        )
        for run in paragraph.get("elements", []):
            text_run = run.get("textRun")
            if not text_run:
                continue
            text = text_run.get("content", "")
            weight = len(text.strip()) or (1 if text else 0)
            text_style = text_run.get("textStyle", {})
            run_bucket["font_family"].append(
                (weight, text_style.get("weightedFontFamily", {}).get("fontFamily"))
            )
            run_bucket["font_size"].append(
                (weight, text_style.get("fontSize", {}).get("magnitude"))
            )
            for prop in ("bold", "italic", "underline"):
                run_bucket[prop].append((weight, text_style.get(prop)))

        para_bucket = paragraph_values.setdefault(
            style,
            {
                "alignment": [],
                "line_spacing": [],
                "indent_first_line": [],
                "indent_start": [],
                "space_above": [],
                "space_below": [],
            },
        )
        para_style = paragraph.get("paragraphStyle", {})
        para_bucket["alignment"].append(para_style.get("alignment"))
        para_bucket["line_spacing"].append(para_style.get("lineSpacing"))
        para_bucket["indent_first_line"].append(
            (para_style.get("indentFirstLine") or {}).get("magnitude")
        )
        para_bucket["indent_start"].append((para_style.get("indentStart") or {}).get("magnitude"))
        para_bucket["space_above"].append((para_style.get("spaceAbove") or {}).get("magnitude"))
        para_bucket["space_below"].append((para_style.get("spaceBelow") or {}).get("magnitude"))

    named_styles = {
        item.get("namedStyleType"): item
        for item in document.get("namedStyles", {}).get("styles", [])
    }

    def named_text(style: str, prop: str) -> Any:
        text_style = named_styles.get(style, {}).get("textStyle", {})
        if prop == "font_family":
            return text_style.get("weightedFontFamily", {}).get("fontFamily")
        if prop == "font_size":
            return text_style.get("fontSize", {}).get("magnitude")
        return text_style.get(prop)

    def named_para(style: str, prop: str) -> Any:
        para_style = named_styles.get(style, {}).get("paragraphStyle", {})
        if prop in ("indent_first_line", "indent_start", "space_above", "space_below"):
            key = {
                "indent_first_line": "indentFirstLine",
                "indent_start": "indentStart",
                "space_above": "spaceAbove",
                "space_below": "spaceBelow",
            }[prop]
            return (para_style.get(key) or {}).get("magnitude")
        key = {"alignment": "alignment", "line_spacing": "lineSpacing"}[prop]
        return para_style.get(key)

    profile: dict[str, dict[str, Any]] = {}
    base: dict[str, Any] = {}

    for style in run_values:
        bucket = run_values[style]
        family = _weighted_dominant(bucket["font_family"])
        size = _weighted_dominant(bucket["font_size"])
        bold = _weighted_dominant(bucket["bold"])
        italic = _weighted_dominant(bucket["italic"])
        underline = _weighted_dominant(bucket["underline"])

        paragraphs = paragraph_values[style]
        alignment = _mode(paragraphs["alignment"])
        line_spacing = _mode(paragraphs["line_spacing"])
        indent_first = _mode(paragraphs["indent_first_line"])
        indent_start = _mode(paragraphs["indent_start"])
        space_above = _mode(paragraphs["space_above"])
        space_below = _mode(paragraphs["space_below"])

        if style == "NORMAL_TEXT":
            base = {
                "font_family": family or "Arial",
                "font_size_pt": size or 11.0,
                "bold": bool(bold),
                "italic": bool(italic),
                "underline": bool(underline),
                "alignment": alignment or "JUSTIFIED",
                "line_spacing": line_spacing or 150,
                "indent_first_line_pt": indent_first or 0.0,
                "indent_start_pt": indent_start or 0.0,
                "space_above_pt": space_above or 0.0,
                "space_below_pt": space_below or 0.0,
            }

        profile[style] = {
            "font_family": family or named_text(style, "font_family"),
            "font_size_pt": size or named_text(style, "font_size"),
            "bold": bold if bold is not None else named_text(style, "bold"),
            "italic": italic if italic is not None else named_text(style, "italic"),
            "underline": underline if underline is not None else named_text(style, "underline"),
            "alignment": alignment or named_para(style, "alignment"),
            "line_spacing": line_spacing or named_para(style, "line_spacing"),
            "indent_first_line_pt": (
                indent_first
                if indent_first is not None
                else named_para(style, "indent_first_line")
            )
            or 0.0,
            "indent_start_pt": (
                indent_start if indent_start is not None else named_para(style, "indent_start")
            )
            or 0.0,
            "space_above_pt": (
                space_above if space_above is not None else named_para(style, "space_above")
            )
            or 0.0,
            "space_below_pt": (
                space_below if space_below is not None else named_para(style, "space_below")
            )
            or 0.0,
        }

    # Fill gaps for headings that only override a few properties in the source.
    default_heading_sizes = {
        "HEADING_1": 20.0,
        "HEADING_2": 16.0,
        "HEADING_3": 14.0,
        "HEADING_4": 12.0,
        "HEADING_5": 11.0,
        "HEADING_6": 11.0,
    }
    for style, values in profile.items():
        if style != "NORMAL_TEXT" and base:
            if values.get("font_size_pt") is None:
                values["font_size_pt"] = default_heading_sizes.get(style)
            for prop in ("font_family", "font_size_pt", "bold", "italic", "underline"):
                if values.get(prop) is None:
                    values[prop] = base.get(prop)
            if values.get("alignment") is None:
                values["alignment"] = "START"
            for prop in (
                "line_spacing",
                "indent_first_line_pt",
                "indent_start_pt",
                "space_above_pt",
                "space_below_pt",
            ):
                if values.get(prop) is None:
                    values[prop] = 0.0 if prop.endswith("_pt") else base.get(prop)

    if "NORMAL_TEXT" not in profile:
        profile["NORMAL_TEXT"] = {
            "font_family": "Arial",
            "font_size_pt": 11.0,
            "bold": False,
            "italic": False,
            "underline": False,
            "alignment": "JUSTIFIED",
            "line_spacing": 150,
            "indent_first_line_pt": 0.0,
            "indent_start_pt": 0.0,
            "space_above_pt": 0.0,
            "space_below_pt": 0.0,
        }

    profile["CAPTION"] = {
        "font_family": profile["NORMAL_TEXT"]["font_family"],
        "font_size_pt": profile["NORMAL_TEXT"]["font_size_pt"],
        "bold": True,
        "italic": False,
        "underline": False,
        "alignment": "START",
        "line_spacing": profile["NORMAL_TEXT"]["line_spacing"],
        "indent_first_line_pt": 0.0,
        "indent_start_pt": 0.0,
        "space_above_pt": 0.0,
        "space_below_pt": 0.0,
    }
    profile["NOTE"] = {
        "font_family": profile["NORMAL_TEXT"]["font_family"],
        "font_size_pt": max(profile["NORMAL_TEXT"]["font_size_pt"] - 1.0, 1.0),
        "bold": False,
        "italic": True,
        "underline": False,
        "alignment": "JUSTIFIED",
        "line_spacing": profile["NORMAL_TEXT"]["line_spacing"],
        "indent_first_line_pt": 0.0,
        "indent_start_pt": 0.0,
        "space_above_pt": 0.0,
        "space_below_pt": 0.0,
    }
    return profile

_INDEX_HEADERS = {"indice", "indice de figuras", "indice de tablas", "indice de anexos"}
_CAPTION_NUMBER = re.compile(r"^(figura|tabla)\s+(\d+)$")
_CAPTION_INLINE = re.compile(r"^(figura|tabla)\s+(\d+)\s+\S")
_NOTE_PREFIX = re.compile(r"^nota[.:]\s*")
_ANNEX_NUMBER = re.compile(r"^anexo\s+(\d+)$")


def _find_body_start(spans: list[tuple[int, int, str, str]]) -> int:
    for index, (_, _, _, text) in enumerate(spans):
        if _normalize_text(text) == "introduccion":
            return index
    raise ValueError("Could not find the body start ('INTRODUCCIÓN') in the document")


def parse_toc(
    spans: list[tuple[int, int, str, str]], body_start: int
) -> list[dict[str, Any]]:
    """Parse the flat TOC block before the body into ordered ``(level, title)``.

    Entries look like ``INTRODUCCIÓN\\t9``, ``1.1.\\tObjetivo General\\t10`` or
    ``2.1.1.\\tVisión:\\t11``. Figure/table/annex index lines are skipped.
    """
    entries: list[dict[str, Any]] = []
    toc_start = next(
        (index for index in range(body_start) if _normalize_text(spans[index][3]) == "indice"),
        0,
    )
    for _, _, _, text in spans[toc_start:body_start]:
        raw = text.strip()
        if not raw or "\t" not in raw:
            continue
        parts = [part.strip() for part in raw.split("\t") if part.strip()]
        if parts and re.fullmatch(r"\d+", parts[-1]):
            parts = parts[:-1]
        title = " ".join(parts).strip()
        normalized = _normalize_text(title)
        if not normalized or normalized in _INDEX_HEADERS:
            continue
        if re.match(r"^(figura|tabla|anexo)\s+\d+", normalized):
            continue
        numbering = re.match(r"^(\d+(?:\.\d+)*)\.?\s+", title)
        if numbering:
            level = min(numbering.group(1).count(".") + 1, 6)
            clean_title = title[numbering.end() :].strip()
        else:
            level = 1
            clean_title = title
        entries.append(
            {
                "level": level,
                "title": clean_title,
                "norm": _normalize_text(clean_title),
            }
        )
    return entries


def _heading_sequence(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordered headings (level + normalized text) from a source document."""
    headings: list[dict[str, Any]] = []
    for _, _, style, text in paragraph_spans(document):
        match = re.fullmatch(r"HEADING_([1-6])", style)
        normalized = _normalize_text(text)
        if not match or not normalized:
            continue
        headings.append(
            {"level": int(match.group(1)), "text": text.strip(), "norm": normalized}
        )
    return headings


def _next_non_empty(spans: list[tuple[int, int, str, str]], index: int) -> int | None:
    for candidate in range(index + 1, len(spans)):
        if spans[candidate][3].strip():
            return candidate
        if candidate - index > 3:
            break
    return None


def _previous_non_empty(spans: list[tuple[int, int, str, str]], index: int) -> int | None:
    for candidate in range(index - 1, -1, -1):
        if spans[candidate][3].strip():
            return candidate
    return None


def _looks_like_fallback_heading(
    spans: list[tuple[int, int, str, str]], index: int, decisions: list[str]
) -> bool:
    """Very conservative fallback for short titles not present in the source."""
    text = spans[index][3].strip()
    if not (3 <= len(text) <= 60) or "\t" in text:
        return False
    if text.endswith((".", ",", ";", ":")):
        return False
    words = text.split()
    if not (2 <= len(words) <= 8):
        return False
    previous = _previous_non_empty(spans, index)
    following = _next_non_empty(spans, index)
    if previous is None or following is None:
        return False
    previous_text = spans[previous][3].strip()
    following_text = spans[following][3].strip()
    if len(previous_text) < 80 or len(following_text) < 120:
        return False
    if decisions[following].startswith("HEADING") or decisions[following] != "NORMAL":
        return False
    # Very conservative: only ALL-CAPS titles qualify. Title-case body labels
    # such as "Segunda Versión" are common prose and must not become headings.
    return text.isupper()


def classify_document(
    spans: list[tuple[int, int, str, str]],
    body_start: int,
    toc_entries: list[dict[str, Any]],
    source_headings: list[dict[str, Any]],
) -> tuple[list[str], dict[str, int]]:
    """Assign a label to each paragraph span.

    Labels: ``HEADING_1``..``HEADING_6``, ``CAPTION``, ``NOTE``, ``BULLET``,
    ``LABEL``, ``FRONT_TITLE``, ``FRONT_INFO``, ``TOC_HEADER``, ``TOC_ENTRY``,
    ``NORMAL`` and ``BLANK``.
    """
    decisions = ["NORMAL"] * len(spans)

    first_index_header = next(
        (
            index
            for index in range(body_start)
            if _normalize_text(spans[index][3]) == "indice"
        ),
        None,
    )
    for index in range(body_start):
        text = spans[index][3]
        normalized = _normalize_text(text)
        if not normalized:
            decisions[index] = "BLANK"
        elif normalized in _INDEX_HEADERS:
            decisions[index] = "TOC_HEADER"
        elif "\t" in text:
            parts = [part.strip() for part in text.split("\t") if part.strip()]
            if parts and re.fullmatch(r"\d+", parts[-1]):
                decisions[index] = "TOC_ENTRY"
            else:
                decisions[index] = "FRONT_INFO"
        elif first_index_header is None or index < first_index_header:
            decisions[index] = "FRONT_TITLE"
        else:
            decisions[index] = "TOC_ENTRY"

    pointer = 0
    for index in range(body_start, len(spans)):
        text = spans[index][3]
        normalized = _normalize_text(text)
        if not normalized:
            decisions[index] = "BLANK"
            continue

        if _NOTE_PREFIX.match(normalized):
            decisions[index] = "NOTE"
            if pointer < len(source_headings) and source_headings[pointer]["norm"] == normalized:
                pointer += 1
            continue

        if _CAPTION_NUMBER.match(normalized):
            decisions[index] = "CAPTION"
            following = _next_non_empty(spans, index)
            if following is not None and decisions[following] == "NORMAL":
                following_normalized = _normalize_text(spans[following][3])
                if not _NOTE_PREFIX.match(following_normalized):
                    decisions[following] = "CAPTION"
            continue

        if _CAPTION_INLINE.match(normalized):
            decisions[index] = "CAPTION"
            continue

        annex = _ANNEX_NUMBER.match(normalized)
        if (
            annex
            and pointer < len(source_headings)
            and source_headings[pointer]["norm"].startswith(normalized + " ")
        ):
            decisions[index] = f"HEADING_{source_headings[pointer]['level']}"
            pointer += 1
            following = _next_non_empty(spans, index)
            if following is not None and decisions[following] == "NORMAL":
                decisions[following] = decisions[index]
            continue

        if pointer < len(source_headings) and source_headings[pointer]["norm"] == normalized:
            decisions[index] = f"HEADING_{source_headings[pointer]['level']}"
            pointer += 1
            continue

        if normalized in ("observacion:", "proposicion:"):
            decisions[index] = "LABEL"
            continue

        if _looks_like_fallback_heading(spans, index, decisions):
            decisions[index] = "HEADING_3"

    # Conservative bullet pass, driven by explicit labels in the body.
    for index in range(body_start, len(spans)):
        normalized = _normalize_text(spans[index][3])
        if normalized == "objetivos especificos" and decisions[index] == "HEADING_2":
            candidate = index + 1
            while candidate < len(spans):
                if not spans[candidate][3].strip():
                    break
                if decisions[candidate] in ("LABEL", "NOTE", "CAPTION") or decisions[
                    candidate
                ].startswith("HEADING"):
                    break
                if decisions[candidate] == "NORMAL":
                    decisions[candidate] = "BULLET"
                candidate += 1
        if normalized in ("observacion:", "proposicion:"):
            candidate = index + 1
            while candidate < len(spans):
                if not spans[candidate][3].strip():
                    break
                if decisions[candidate] in ("LABEL", "NOTE", "CAPTION") or decisions[
                    candidate
                ].startswith("HEADING"):
                    break
                if decisions[candidate] == "NORMAL":
                    decisions[candidate] = "BULLET"
                candidate += 1

    stats = Counter(decisions)
    return decisions, dict(stats)


def _build_paragraph_request(
    start_index: int,
    end_index: int,
    *,
    named_style: str,
    alignment: str,
    indent_first_line_pt: float,
    indent_start_pt: float,
    line_spacing: float,
    space_above_pt: float = 0.0,
    space_below_pt: float = 0.0,
) -> dict[str, Any]:
    return {
        "updateParagraphStyle": {
            "range": _range(start_index, end_index),
            "paragraphStyle": {
                "namedStyleType": named_style,
                "alignment": alignment,
                "indentFirstLine": {"magnitude": indent_first_line_pt, "unit": _PT},
                "indentStart": {"magnitude": indent_start_pt, "unit": _PT},
                "lineSpacing": line_spacing,
                "spaceAbove": {"magnitude": space_above_pt, "unit": _PT},
                "spaceBelow": {"magnitude": space_below_pt, "unit": _PT},
            },
            "fields": (
                "namedStyleType,alignment,indentFirstLine,indentStart,"
                "lineSpacing,spaceAbove,spaceBelow"
            ),
        }
    }


def _build_text_request(
    start_index: int,
    end_index: int,
    *,
    font_family: str,
    font_size_pt: float,
    bold: bool,
    italic: bool,
    underline: bool = False,
) -> dict[str, Any]:
    return {
        "updateTextStyle": {
            "range": _range(start_index, end_index),
            "textStyle": {
                "weightedFontFamily": {"fontFamily": font_family, "weight": 400},
                "fontSize": {"magnitude": font_size_pt, "unit": _PT},
                "bold": bold,
                "italic": italic,
                "underline": underline,
            },
            "fields": "weightedFontFamily,fontSize,bold,italic,underline",
        }
    }


def _inspect_from_document(document: dict[str, Any]) -> dict[str, Any]:
    paragraphs = list(_iter_paragraph_elements(document.get("body", {}).get("content", [])))
    styles = Counter(
        paragraph["paragraph"].get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
        for paragraph in paragraphs
    )
    alignments = Counter(
        paragraph["paragraph"].get("paragraphStyle", {}).get("alignment", "START")
        for paragraph in paragraphs
    )
    named_styles = {
        item.get("namedStyleType"): item
        for item in document.get("namedStyles", {}).get("styles", [])
    }

    def effective(style: str, text_style: dict[str, Any], prop: str) -> bool:
        # Explicit run formatting wins; otherwise the named style's definition
        # applies (e.g. HEADING_1 is bold only through namedStyles).
        if prop in text_style:
            return bool(text_style[prop])
        inherited = (
            named_styles.get(style, {}).get("textStyle", {}).get(prop)
        )
        return bool(inherited)

    bold_runs = italic_runs = underline_runs = 0
    for paragraph in paragraphs:
        style = paragraph["paragraph"].get("paragraphStyle", {}).get(
            "namedStyleType", "NORMAL_TEXT"
        )
        for run in paragraph["paragraph"].get("elements", []):
            text_run = run.get("textRun")
            if not text_run:
                continue
            text_style = text_run.get("textStyle", {})
            bold_runs += 1 if effective(style, text_style, "bold") else 0
            italic_runs += 1 if effective(style, text_style, "italic") else 0
            underline_runs += 1 if effective(style, text_style, "underline") else 0
    bullets = sum(1 for paragraph in paragraphs if paragraph["paragraph"].get("bullet"))
    content = document.get("body", {}).get("content", [])
    return {
        "title": document.get("title"),
        "paragraphs": len(paragraphs),
        "headings": {
            f"HEADING_{level}": styles.get(f"HEADING_{level}", 0) for level in range(1, 7)
        },
        "heading_total": sum(value for key, value in styles.items() if key.startswith("HEADING_")),
        "list_items": bullets,
        "toc": sum(1 for element in content if "tableOfContents" in element),
        "tables": sum(1 for element in content if "table" in element),
        "italic_runs": italic_runs,
        "bold_runs": bold_runs,
        "underline_runs": underline_runs,
        "alignment": dict(alignments),
    }


def inspect_document(service: Any, doc_id: str) -> dict[str, Any]:
    """Return structural and formatting counts for a document."""
    return _inspect_from_document(get_document(service, doc_id))


def apply_formato_ander(
    service: Any,
    doc_id: str,
    profile: dict[str, dict[str, Any]] | None = None,
    source_doc_id: str | None = None,
    *,
    apply_bullets: bool = True,
) -> dict[str, Any]:
    """Apply the formato-ander structure to ``doc_id``.

    The source document is only read. Returns a summary with the derived
    profile, classification stats and before/after document counts.
    """
    source_id = source_doc_id or FORMATO_ANDER_SOURCE_ID
    if doc_id == source_id:
        raise ValueError("Refusing to apply formatting to the formato-ander source document")

    source_document = get_document(service, source_id)
    derived = profile if profile is not None else derive_style_profile(source_document)
    source_headings = _heading_sequence(source_document)

    target_document = get_document(service, doc_id)
    before = _inspect_from_document(target_document)
    spans = paragraph_spans(target_document)
    body_start = _find_body_start(spans)
    toc_entries = parse_toc(spans, body_start)
    decisions, stats = classify_document(spans, body_start, toc_entries, source_headings)

    base = derived["NORMAL_TEXT"]
    caption = derived["CAPTION"]
    note = derived["NOTE"]

    requests: list[dict[str, Any]] = []
    for index, (start, end, _, text) in enumerate(spans):
        decision = decisions[index]
        if decision == "BLANK":
            continue

        if decision.startswith("HEADING_"):
            style = derived.get(decision) or derived.get("HEADING_1", base)
            named_style = decision
            values = {
                "named_style": named_style,
                "alignment": style.get("alignment") or "START",
                "indent_first_line_pt": style.get("indent_first_line_pt") or 0.0,
                "indent_start_pt": style.get("indent_start_pt") or 0.0,
                "line_spacing": style.get("line_spacing") or 150,
                "space_above_pt": style.get("space_above_pt") or 0.0,
                "space_below_pt": style.get("space_below_pt") or 0.0,
            }
            run_values = {
                "font_family": style.get("font_family") or base["font_family"],
                "font_size_pt": style.get("font_size_pt") or base["font_size_pt"],
                "bold": bool(style.get("bold")),
                "italic": bool(style.get("italic")),
            }
        elif decision == "CAPTION":
            values = {
                "named_style": "NORMAL_TEXT",
                "alignment": caption["alignment"],
                "indent_first_line_pt": 0.0,
                "indent_start_pt": 0.0,
                "line_spacing": caption["line_spacing"],
                "space_above_pt": 0.0,
                "space_below_pt": 0.0,
            }
            run_values = {
                "font_family": caption["font_family"],
                "font_size_pt": caption["font_size_pt"],
                "bold": True,
                "italic": False,
            }
        elif decision == "NOTE":
            values = {
                "named_style": "NORMAL_TEXT",
                "alignment": note["alignment"],
                "indent_first_line_pt": 0.0,
                "indent_start_pt": 0.0,
                "line_spacing": note["line_spacing"],
                "space_above_pt": 0.0,
                "space_below_pt": 0.0,
            }
            run_values = {
                "font_family": note["font_family"],
                "font_size_pt": note["font_size_pt"],
                "bold": False,
                "italic": True,
            }
        elif decision == "FRONT_TITLE":
            values = {
                "named_style": "NORMAL_TEXT",
                "alignment": "CENTER",
                "indent_first_line_pt": 0.0,
                "indent_start_pt": 0.0,
                "line_spacing": 150,
                "space_above_pt": 0.0,
                "space_below_pt": 0.0,
            }
            run_values = {
                "font_family": base["font_family"],
                "font_size_pt": 12.0,
                "bold": True,
                "italic": False,
            }
        elif decision in ("FRONT_INFO", "LABEL"):
            values = {
                "named_style": "NORMAL_TEXT",
                "alignment": "START",
                "indent_first_line_pt": 0.0,
                "indent_start_pt": 0.0,
                "line_spacing": 150,
                "space_above_pt": 0.0,
                "space_below_pt": 0.0,
            }
            run_values = {
                "font_family": base["font_family"],
                "font_size_pt": base["font_size_pt"],
                "bold": True,
                "italic": False,
            }
        elif decision == "TOC_ENTRY":
            values = {
                "named_style": "NORMAL_TEXT",
                "alignment": "START",
                "indent_first_line_pt": 0.0,
                "indent_start_pt": 0.0,
                "line_spacing": 150,
                "space_above_pt": 0.0,
                "space_below_pt": 0.0,
            }
            run_values = {
                "font_family": base["font_family"],
                "font_size_pt": base["font_size_pt"],
                "bold": False,
                "italic": False,
            }
        else:
            values = {
                "named_style": "NORMAL_TEXT",
                "alignment": base["alignment"],
                "indent_first_line_pt": 0.0,
                "indent_start_pt": 0.0,
                "line_spacing": base["line_spacing"],
                "space_above_pt": 0.0,
                "space_below_pt": 0.0,
            }
            run_values = {
                "font_family": base["font_family"],
                "font_size_pt": base["font_size_pt"],
                "bold": False,
                "italic": False,
            }

        if text.strip():
            requests.append(_build_paragraph_request(start, end, **values))
            requests.append(_build_text_request(start, end, **run_values))

    _run_batch(service, doc_id, requests)

    bullet_groups: list[tuple[int, int]] = []
    bullets_converted = 0
    if apply_bullets:
        current_start = None
        current_end = None
        bullet_requests: list[dict[str, Any]] = []
        for index, (start, end, _, _) in enumerate(spans):
            if decisions[index] == "BULLET":
                if current_start is None:
                    current_start = start
                current_end = end
                bullets_converted += 1
            else:
                if current_start is not None and current_end is not None:
                    bullet_groups.append((current_start, current_end))
                    bullet_requests.append(
                        {
                            "createParagraphBullets": {
                                "range": _range(current_start, current_end),
                                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                            }
                        }
                    )
                current_start = None
                current_end = None
        if current_start is not None and current_end is not None:
            bullet_groups.append((current_start, current_end))
            bullet_requests.append(
                {
                    "createParagraphBullets": {
                        "range": _range(current_start, current_end),
                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                    }
                }
            )
        _run_batch(service, doc_id, bullet_requests)

    after = inspect_document(service, doc_id)
    return {
        "document_id": doc_id,
        "source_document_id": source_id,
        "body_start_index": spans[body_start][0],
        "profile": derived,
        "toc_entries": len(toc_entries),
        "classification": stats,
        "bullet_ranges": len(bullet_groups),
        "bullets_converted": bullets_converted,
        "before": before,
        "after": after,
    }


# ---------------------------------------------------------------------------
# List structure replication from a source document
# ---------------------------------------------------------------------------

# ``glyphType`` is only present for ordered lists; bullet lists expose a
# ``glyphSymbol`` instead. Anything not in this set is treated as a bullet.
_ORDERED_GLYPH_TYPES = frozenset(
    {
        "DECIMAL",
        "ALPHA",
        "ROMAN",
        "UPPER_ALPHA",
        "LOWER_ALPHA",
        "UPPER_ROMAN",
        "LOWER_ROMAN",
    }
)

_BULLET_PRESET = "BULLET_DISC_CIRCLE_SQUARE"
_NUMBERED_PRESET = "NUMBERED_DECIMAL_ALPHA_ROMAN"


def _body_paragraph_elements(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only body-level paragraph elements (tables are not descended)."""
    return [
        element
        for element in document.get("body", {}).get("content", [])
        if "paragraph" in element
    ]


def _is_ordered_list(document: dict[str, Any], list_id: str, level: int) -> bool:
    """Whether ``list_id`` at ``level`` renders numbers/letters instead of bullets."""
    list_properties = (
        document.get("lists", {})
        .get(list_id, {})
        .get("listProperties", {})
        .get("nestingLevels", [])
    )
    glyph_type = list_properties[level].get("glyphType") if level < len(list_properties) else None
    return (glyph_type or "").upper() in _ORDERED_GLYPH_TYPES


def _body_paragraph_records(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Body-level paragraphs with indices, plain text and normalized text."""
    records: list[dict[str, Any]] = []
    for element in _body_paragraph_elements(document):
        paragraph = element["paragraph"]
        text = _paragraph_plain_text(paragraph).replace("\x0b", " ").rstrip("\n")
        bullet = paragraph.get("bullet")
        records.append(
            {
                "start": element["startIndex"],
                "end": element["endIndex"],
                "text": text,
                "norm": _normalize_text(text),
                "named_style": (
                    paragraph.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
                ),
                "has_bullet": bool(bullet),
                "listId": bullet.get("listId", "") if bullet else "",
                "nesting_level": bullet.get("nestingLevel", 0) if bullet else 0,
            }
        )
    return records


def _add_next_norms(records: list[dict[str, Any]]) -> None:
    """Attach the next non-empty normalized text to each record (context cue)."""
    for index, record in enumerate(records):
        record["next_norm"] = ""
        for candidate in range(index + 1, len(records)):
            if records[candidate]["norm"]:
                record["next_norm"] = records[candidate]["norm"]
                break
            if candidate - index > 4:
                break


def _list_items_from_document(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordered body-level list items with their list identity and nesting."""
    paragraphs = _body_paragraph_records(document)
    _add_next_norms(paragraphs)

    items: list[dict[str, Any]] = []
    for record in paragraphs:
        if not record["has_bullet"]:
            continue
        record.update(
            {
                "level": record["nesting_level"],
                "numbered": _is_ordered_list(document, record["listId"], record["nesting_level"]),
            }
        )
        items.append(record)
    return items


def _context_score(
    source_items: list[dict[str, Any]], target_paragraphs: list[dict[str, Any]], i: int, j: int
) -> float:
    """Score how well a target paragraph's context follows a source list item."""
    source_next = source_items[i].get("next_norm", "")
    target_next = target_paragraphs[j].get("next_norm", "")
    if not source_next or not target_next:
        return 0.0
    if source_next == target_next:
        return 3.0
    return 2.0 * difflib.SequenceMatcher(None, source_next, target_next).ratio()


def _align_list_items_to_paragraphs(
    source_items: list[dict[str, Any]], target_paragraphs: list[dict[str, Any]]
) -> dict[int, int]:
    """Map source list-item indices to target paragraph indices.

    A classic LCS on normalized text keeps document order, then duplicate texts
    (e.g. "Observación:", "Programa creado") are re-assigned within each group
    using neighboring-paragraph context so the right occurrence wins.
    """
    n = len(source_items)
    m = len(target_paragraphs)
    if not n or not m:
        return {}

    table = [array.array("H", [0]) * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        source_norm = source_items[i - 1]["norm"]
        row = table[i]
        previous_row = table[i - 1]
        for j in range(1, m + 1):
            if source_norm == target_paragraphs[j - 1]["norm"]:
                row[j] = previous_row[j - 1] + 1
            else:
                row[j] = previous_row[j] if previous_row[j] >= row[j - 1] else row[j - 1]

    match: dict[int, int] = {}
    i, j = n, m
    while i > 0 and j > 0:
        if source_items[i - 1]["norm"] == target_paragraphs[j - 1]["norm"]:
            match[i - 1] = j - 1
            i -= 1
            j -= 1
        elif table[i - 1][j] >= table[i][j - 1]:
            i -= 1
        else:
            j -= 1

    # Refine groups that share the same normalized text.
    source_groups: dict[str, list[int]] = {}
    target_groups: dict[str, list[int]] = {}
    for index, item in enumerate(source_items):
        source_groups.setdefault(item["norm"], []).append(index)
    for index, paragraph in enumerate(target_paragraphs):
        target_groups.setdefault(paragraph["norm"], []).append(index)

    for norm, source_indices in source_groups.items():
        target_indices = target_groups.get(norm, [])
        if not target_indices:
            for index in source_indices:
                match.pop(index, None)
            continue
        if len(target_indices) == len(source_indices):
            for source_index, target_index in zip(source_indices, target_indices):
                match[source_index] = target_index
            continue
        if len(target_indices) < len(source_indices):
            for source_index, target_index in zip(source_indices, target_indices):
                match[source_index] = target_index
            for source_index in source_indices[len(target_indices) :]:
                match.pop(source_index, None)
            continue

        # More target occurrences than source items: choose the best ordered
        # subset of target paragraphs, maximizing context similarity.
        source_count = len(source_indices)
        target_count = len(target_indices)
        scores = [[0.0] * (target_count + 1) for _ in range(source_count + 1)]
        for a in range(1, source_count + 1):
            for b in range(1, target_count + 1):
                take = scores[a - 1][b - 1] + _context_score(
                    source_items, target_paragraphs, source_indices[a - 1], target_indices[b - 1]
                )
                skip = scores[a][b - 1]
                scores[a][b] = take if take >= skip else skip
        a, b = source_count, target_count
        while a > 0 and b > 0:
            take = scores[a - 1][b - 1] + _context_score(
                source_items, target_paragraphs, source_indices[a - 1], target_indices[b - 1]
            )
            if abs(scores[a][b] - take) < 1e-9:
                match[source_indices[a - 1]] = target_indices[b - 1]
                a -= 1
                b -= 1
            else:
                b -= 1

    return match


def _list_breakdown(document: dict[str, Any]) -> dict[str, Any]:
    items = _list_items_from_document(document)
    return {
        "list_items": len(items),
        "bullet": sum(1 for item in items if not item["numbered"]),
        "numbered": sum(1 for item in items if item["numbered"]),
        "nesting": dict(Counter(item["level"] for item in items)),
    }


def apply_lists_from_source(
    service: Any, target_id: str, source_id: str
) -> dict[str, Any]:
    """Reproduce the source document's list structure in the target document.

    The source is read-only. Existing target bullets are removed first, then
    leading tabs encode nesting and ``createParagraphBullets`` (with the right
    preset) turns each contiguous run into a list. Returns match/apply stats.
    """
    if target_id == source_id:
        raise ValueError("Refusing to modify the source document while replicating lists")

    source_document = get_document(service, source_id)
    source_items = _list_items_from_document(source_document)

    target_document = get_document(service, target_id)
    before_counts = _inspect_from_document(target_document)
    before_lists = _list_breakdown(target_document)
    target_paragraphs = _body_paragraph_records(target_document)
    _add_next_norms(target_paragraphs)
    match = _align_list_items_to_paragraphs(source_items, target_paragraphs)
    unmatched_source = [
        source_items[index]
        for index in range(len(source_items))
        if index not in match
    ]

    # Phase 1: remove every existing bullet in the target body.
    deleted_bullets = before_counts["list_items"]
    if deleted_bullets:
        body_end = target_document["body"]["content"][-1]["endIndex"]
        _run_batch(
            service,
            target_id,
            [{"deleteParagraphBullets": {"range": _range(1, body_end)}}],
        )
        target_document = get_document(service, target_id)
        target_paragraphs = _body_paragraph_records(target_document)
        _add_next_norms(target_paragraphs)
        match = _align_list_items_to_paragraphs(source_items, target_paragraphs)

    # Phase 2: insert leading tabs (highest index first so offsets stay valid).
    tab_requests: list[dict[str, Any]] = []
    for source_index, target_index in sorted(
        match.items(), key=lambda pair: target_paragraphs[pair[1]]["start"], reverse=True
    ):
        level = source_items[source_index]["level"]
        current_tabs = len(target_paragraphs[target_index]["text"]) - len(
            target_paragraphs[target_index]["text"].lstrip("\t")
        )
        if level > current_tabs:
            tab_requests.append(
                {
                    "insertText": {
                        "location": {"index": target_paragraphs[target_index]["start"]},
                        "text": "\t" * (level - current_tabs),
                    }
                }
            )
    _run_batch(service, target_id, tab_requests, batch_size=100)

    # Re-read because tab insertions shift every subsequent index.
    target_document = get_document(service, target_id)
    target_paragraphs = _body_paragraph_records(target_document)
    _add_next_norms(target_paragraphs)
    match = _align_list_items_to_paragraphs(source_items, target_paragraphs)

    # Phase 3: group adjacent matched paragraphs that belong to the same list.
    pairs = sorted(match.items(), key=lambda pair: pair[1])
    runs: list[dict[str, Any]] = []
    for source_index, target_index in pairs:
        item = source_items[source_index]
        if runs:
            previous = runs[-1]
            if (
                target_index == previous["last_target"] + 1
                and item["listId"] == previous["listId"]
                and item["numbered"] == previous["numbered"]
            ):
                previous["last_target"] = target_index
                previous["end"] = target_paragraphs[target_index]["end"]
                previous["count"] += 1
                continue
        runs.append(
            {
                "listId": item["listId"],
                "numbered": item["numbered"],
                "start": target_paragraphs[target_index]["start"],
                "end": target_paragraphs[target_index]["end"],
                "last_target": target_index,
                "count": 1,
            }
        )

    bullet_requests: list[dict[str, Any]] = []
    for run in runs:
        bullet_requests.append(
            {
                "createParagraphBullets": {
                    "range": _range(run["start"], run["end"]),
                    "bulletPreset": _NUMBERED_PRESET if run["numbered"] else _BULLET_PRESET,
                }
            }
        )
    _run_batch(service, target_id, bullet_requests)

    after_document = get_document(service, target_id)
    after_counts = _inspect_from_document(after_document)
    after_lists = _list_breakdown(after_document)
    applied = sum(run["count"] for run in runs)

    return {
        "document_id": target_id,
        "source_document_id": source_id,
        "before": {**before_lists, "headings_total": before_counts["heading_total"]},
        "after": {**after_lists, "headings_total": after_counts["heading_total"]},
        "source": {
            "list_items": len(source_items),
            "bullet": sum(1 for item in source_items if not item["numbered"]),
            "numbered": sum(1 for item in source_items if item["numbered"]),
            "nesting": dict(Counter(item["level"] for item in source_items)),
        },
        "matched": len(match),
        "applied": applied,
        "match_rate": len(match) / len(source_items) if source_items else 0.0,
        "deleted_bullets": deleted_bullets,
        "tabs_inserted": len(tab_requests),
        "runs": len(runs),
        "unmatched_source": [
            {
                "text": item["text"],
                "level": item["level"],
                "numbered": item["numbered"],
            }
            for item in unmatched_source
        ],
        "unmatched_examples": [
            {
                "text": item["text"],
                "level": item["level"],
                "numbered": item["numbered"],
            }
            for item in unmatched_source[:5]
        ],
    }
