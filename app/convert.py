"""Office document -> PDF through an optional Gotenberg sidecar."""
from __future__ import annotations

from pathlib import Path

import httpx


class ConversionError(Exception):
    pass


def convert_office(gotenberg_url: str, src: Path, out: Path, timeout: float = 180,
                   transport: httpx.BaseTransport | None = None) -> None:
    """Convert ``src`` with LibreOffice in Gotenberg and write the PDF to ``out``."""
    if not gotenberg_url:
        raise ConversionError("Office files need Gotenberg (PLATEN_GOTENBERG_URL)")
    url = gotenberg_url.rstrip("/") + "/forms/libreoffice/convert"
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client, src.open("rb") as fh:
            response = client.post(url, files={"files": (src.name, fh)})
    except httpx.HTTPError as exc:
        raise ConversionError(f"Cannot reach Gotenberg: {exc}") from exc
    if response.status_code != 200 or not response.content.startswith(b"%PDF-"):
        raise ConversionError(
            f"Conversion failed ({response.status_code}): {response.text[:300]}"
        )
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(response.content)
    tmp.replace(out)
