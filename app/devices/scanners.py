"""Scanner access through SANE (sane-airscan for eSCL/WSD network scanners)."""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

STANDARD_DPI = [75, 100, 150, 200, 300, 600, 1200]


class ScannerError(Exception):
    pass


@dataclass
class ScannerInfo:
    id: str          # SANE device name, e.g. "airscan:e0:HP LaserJet MFP M234dw"
    label: str


@dataclass
class ScannerCaps:
    resolutions: list[int] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def default_resolution(self) -> int:
        for dpi in (200, 300, 150):
            if dpi in self.resolutions:
                return dpi
        return self.resolutions[0] if self.resolutions else 200

    def default_mode(self) -> str:
        for mode in self.modes:
            if mode.lower().startswith("gray") or mode.lower().startswith("grey"):
                return mode
        return self.modes[0] if self.modes else ""


class ScannerBackend(Protocol):
    def list_scanners(self) -> list[ScannerInfo]: ...
    def capabilities(self, device: str) -> ScannerCaps: ...
    def scan(self, device: str, dpi: int, mode: str, out: Path) -> None: ...


def parse_device_list(output: str) -> list[ScannerInfo]:
    """Parse ``scanimage -f '%d\\t%v %m%n'`` output."""
    result = []
    for line in output.splitlines():
        if "\t" not in line:
            continue
        dev, label = line.split("\t", 1)
        # sane-airscan reports the protocol as the vendor ("eSCL HP ...").
        label = re.sub(r"^(eSCL|WSD)\s+", "", label.strip())
        if dev.strip():
            result.append(ScannerInfo(dev.strip(), label.strip() or dev.strip()))
    return result


def _option_values(line: str) -> tuple[list[str], str | None]:
    """Split the value part of a ``scanimage -A`` option line."""
    m = re.match(r"\s*--?[\w-]+\s+(.*?)\s*(\[(.*?)\])?\s*$", line)
    if not m:
        return [], None
    return [v.strip() for v in m.group(1).split("|")], m.group(3)


def parse_capabilities(output: str) -> ScannerCaps:
    """Parse the resolution, mode and source options from ``scanimage -A``."""
    caps = ScannerCaps()
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("--resolution"):
            values, _ = _option_values(stripped)
            text = " ".join(values)
            rng = re.match(r"(\d+)\.\.(\d+)", text)
            if rng:
                lo, hi = int(rng.group(1)), int(rng.group(2))
                caps.resolutions = [d for d in STANDARD_DPI if lo <= d <= hi]
            else:
                caps.resolutions = sorted(
                    {int(m) for v in values for m in re.findall(r"\d+", v)}
                )
        elif stripped.startswith("--mode"):
            caps.modes, _ = _option_values(stripped)
        elif stripped.startswith("--source"):
            caps.sources, _ = _option_values(stripped)
    return caps


class SaneScannerBackend:
    def __init__(self, scan_timeout: int = 300) -> None:
        self.scan_timeout = scan_timeout

    def _run(self, cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise ScannerError("scanimage is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise ScannerError(f"scanner did not respond within {timeout}s") from exc

    def list_scanners(self) -> list[ScannerInfo]:
        proc = self._run(["scanimage", "-f", "%d\t%v %m%n"], timeout=60)
        return parse_device_list(proc.stdout)

    def capabilities(self, device: str) -> ScannerCaps:
        proc = self._run(["scanimage", "-d", device, "-A"], timeout=60)
        if proc.returncode != 0:
            raise ScannerError(_last_line(proc.stderr) or "could not read scanner options")
        return parse_capabilities(proc.stdout)

    def scan(self, device: str, dpi: int, mode: str, out: Path) -> None:
        cmd = [
            "scanimage", "-d", device, "--resolution", str(dpi), "--mode", mode,
            "--format=jpeg", "-o", str(out),
        ]
        proc = self._run(cmd, timeout=self.scan_timeout)
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)
            raise ScannerError(_last_line(proc.stderr) or "scan failed")


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


class FakeScannerBackend:
    """Produces blank A4 pages with a label; used by tests and PLATEN_BACKEND=fake."""

    def __init__(self) -> None:
        self.scans = 0
        self.fail_next = False

    def list_scanners(self) -> list[ScannerInfo]:
        return [ScannerInfo("fake:scanner", "Fake Scanner")]

    def capabilities(self, device: str) -> ScannerCaps:
        return ScannerCaps([150, 200, 300, 600], ["Color", "Gray"], ["Flatbed"])

    def scan(self, device: str, dpi: int, mode: str, out: Path) -> None:
        from PIL import Image, ImageDraw

        if self.fail_next:
            self.fail_next = False
            raise ScannerError("fake scanner jammed")
        self.scans += 1
        size = (round(8.27 * dpi), round(11.69 * dpi))
        img = Image.new("RGB" if mode == "Color" else "L", size, "white")
        ImageDraw.Draw(img).text((dpi // 2, dpi // 2), f"Fake scan {self.scans}", fill="black")
        img.save(out, "JPEG", quality=75, dpi=(dpi, dpi))
