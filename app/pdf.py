"""File helpers: type detection, page counting, PDF building, thumbnails."""
from __future__ import annotations

import re
from pathlib import Path

PDF_MAGIC = b"%PDF-"
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

EXTENSIONS = {"pdf": ".pdf", "jpeg": ".jpg", "png": ".png", "text": ".txt"}

ZIP_MAGIC = b"PK\x03\x04"            # docx/xlsx/pptx/odt/ods/odp
OLE_MAGIC = b"\xd0\xcf\x11\xe0"      # doc/xls/ppt
RTF_MAGIC = b"{\\rtf"
OFFICE_EXTENSIONS = {
    ZIP_MAGIC: {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"},
    OLE_MAGIC: {".doc", ".xls", ".ppt"},
    RTF_MAGIC: {".rtf"},
}
ALL_OFFICE_EXTENSIONS = sorted(set().union(*OFFICE_EXTENSIONS.values()))


def detect_office(head: bytes, filename: str) -> str | None:
    """Return the file extension if this is an Office document Gotenberg can convert."""
    ext = Path(filename).suffix.lower()
    for magic, extensions in OFFICE_EXTENSIONS.items():
        if head.startswith(magic) and ext in extensions:
            return ext
    return None

_RANGE_RE = re.compile(r"^\s*\d+\s*(-\s*\d+\s*)?(,\s*\d+\s*(-\s*\d+\s*)?)*$")


def detect_type(head: bytes) -> str | None:
    """Identify an upload from its first bytes. Returns pdf/jpeg/png/text or None."""
    if head.startswith(PDF_MAGIC):
        return "pdf"
    if head.startswith(JPEG_MAGIC):
        return "jpeg"
    if head.startswith(PNG_MAGIC):
        return "png"
    if head and b"\x00" not in head:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as exc:
            # A multi-byte character may be cut at the end of the sample.
            if exc.start < len(head) - 4:
                return None
        return "text"
    return None


def is_valid_page_range(value: str) -> bool:
    if not _RANGE_RE.match(value):
        return False
    for part in value.split(","):
        bounds = [int(x) for x in part.split("-")]
        if bounds[0] < 1 or bounds[-1] < bounds[0]:
            return False
    return True


def page_range_count(page_range: str | None, total: int) -> int:
    """Number of pages of a ``total``-page document selected by an IPP page range."""
    if not page_range or not page_range.strip():
        return total
    selected: set[int] = set()
    for part in page_range.split(","):
        bounds = [int(x) for x in part.split("-")]
        start, end = bounds[0], bounds[-1]
        selected.update(range(max(start, 1), min(end, total) + 1))
    return len(selected)


def count_pages(path: Path, file_type: str) -> int | None:
    """Pages in a document, or None when we cannot know before printing (text)."""
    if file_type in ("jpeg", "png"):
        return 1
    if file_type == "pdf":
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    return None


def build_pdf(images: list[Path], out: Path, dpi: int | None = None) -> int:
    """Combine page images into one PDF without re-encoding. Returns page count."""
    import img2pdf

    kwargs = {}
    if dpi:
        kwargs["layout_fun"] = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))
    data = img2pdf.convert([str(p) for p in images], **kwargs)
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(out)
    return len(images)


def make_thumbnail(src: Path, dst: Path, width: int = 360) -> None:
    from PIL import Image

    with Image.open(src) as img:
        img.thumbnail((width, width * 2))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(dst, "JPEG", quality=80)
