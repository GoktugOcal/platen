from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_entries(raw: str) -> list[tuple[str | None, str]]:
    """Parse a comma-separated list of ``URI`` or ``Name=URI`` entries.

    The name is only split off when the text before the first ``=`` is not
    itself part of a URI, so ``dnssd://...?uuid=...`` stays intact.
    """
    entries: list[tuple[str | None, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, rest = item.partition("=")
        if sep and "://" not in name and rest.strip():
            entries.append((name.strip(), rest.strip()))
        else:
            entries.append((None, item))
    return entries


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATEN_")

    host: str = "0.0.0.0"
    port: int = 8631
    data_dir: Path = Path("/data")

    # Single-user login. Empty means no authentication (trusted LAN only).
    password: str = ""
    # Cookie signing key. Generated into data_dir/secret_key when empty.
    secret_key: str = ""

    # Printers: "ipp://host/ipp/print" or "Name=ipp://host/ipp/print", comma-separated.
    printers: str = ""
    # Find IPP Everywhere printers via mDNS when no printers are configured.
    discover_printers: bool = True
    # Scanners for sane-airscan: "Name=http://host:8080/eSCL", comma-separated.
    scanners: str = ""
    scanner_discovery: bool = True
    # SANE backends to enable (written to /etc/sane.d/dll.conf in the container).
    sane_backends: str = "airscan"

    retention_days: int = 30
    max_upload_mb: int = 50
    max_concurrent_jobs: int = 4
    # Seconds between CUPS job status polls.
    poll_interval: float = 2.0
    scan_timeout: int = 300

    # "real" uses CUPS and SANE; "fake" simulates devices (tests, UI demo).
    backend: str = "real"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "platen.db"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    def printer_entries(self) -> list[tuple[str | None, str]]:
        return parse_entries(self.printers)

    def scanner_entries(self) -> list[tuple[str | None, str]]:
        return parse_entries(self.scanners)


@lru_cache
def get_settings() -> Settings:
    return Settings()
