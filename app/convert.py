"""Office document -> PDF.

Two converters:
- **LibreOffice inside Platen** (the ``-office`` image): ``soffice --headless
  --convert-to pdf`` runs only while a file is converted, one file at a time.
- **Gotenberg** (optional, for people who already run one elsewhere): its
  ``/forms/libreoffice/convert`` endpoint.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

import httpx


class ConversionError(Exception):
    pass


# LibreOffice cannot run twice on one profile; converting one file at a time also
# keeps memory use predictable on small machines.
_libreoffice_lock = threading.Lock()


def libreoffice_available(binary: str) -> bool:
    return bool(binary) and shutil.which(binary) is not None


def convert_libreoffice(binary: str, src: Path, out: Path, profile_dir: Path,
                        timeout: float = 180) -> None:
    """Convert ``src`` with a local headless LibreOffice and write the PDF to ``out``."""
    exe = shutil.which(binary) if binary else None
    if exe is None:
        raise ConversionError("LibreOffice is not installed (use the platen -office image)")
    profile_dir.mkdir(parents=True, exist_ok=True)
    with _libreoffice_lock, tempfile.TemporaryDirectory(prefix="platen-convert-") as tmp:
        outdir = Path(tmp)
        cmd = [
            exe, f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--headless", "--invisible", "--norestore", "--nolockcheck", "--nodefault",
            "--nologo", "--convert-to", "pdf", "--outdir", str(outdir), str(src),
        ]
        env = {**os.environ, "HOME": str(profile_dir)}
        # Own process group: soffice is a wrapper that starts soffice.bin, and a
        # timeout has to stop both.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True)
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise ConversionError(f"Conversion timed out after {timeout:.0f} s") from None
        result = outdir / f"{src.stem}.pdf"
        if proc.returncode != 0 or not result.is_file():
            detail = output.decode(errors="replace").strip()[-300:]
            raise ConversionError(
                f"Conversion failed (LibreOffice exit {proc.returncode}): {detail or 'no output'}")
        with result.open("rb") as fh:
            if fh.read(5) != b"%PDF-":
                raise ConversionError("Conversion failed: LibreOffice did not produce a PDF")
        tmp_out = out.with_suffix(".tmp")
        shutil.move(result, tmp_out)
        tmp_out.replace(out)


def convert_gotenberg(gotenberg_url: str, src: Path, out: Path, timeout: float = 180,
                      transport: httpx.BaseTransport | None = None) -> None:
    """Convert ``src`` with LibreOffice in a Gotenberg service and write the PDF to ``out``."""
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
