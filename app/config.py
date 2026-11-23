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

    # Database. Empty means SQLite in data_dir; Postgres: postgresql+psycopg://user:pw@host/db
    database_url: str = ""

    # Bootstrap: when no users exist yet, create admin "admin" with this password.
    # Without it, the first visit shows a one-time /setup page.
    password: str = ""
    # Cookie signing key. Generated into data_dir/secret_key when empty.
    secret_key: str = ""

    # Scan-to-email (SMTP). Gmail: smtp.gmail.com, 587, starttls, app password.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"   # starttls | ssl | none
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""                # defaults to smtp_user
    email_max_mb: float = 18           # ~24 MB once base64-encoded (Gmail limit: 25 MB)

    # Paperless-ngx. Users can set their own API token on the account page.
    paperless_url: str = ""
    paperless_token: str = ""
    paperless_wait: int = 120          # seconds to wait for Paperless to consume a document

    # Office documents (docx, xlsx, odt, ...) -> PDF. The platen "-office" image bundles
    # LibreOffice, which Platen runs only while converting. Set PLATEN_LIBREOFFICE="" to
    # turn that off. Alternatively point PLATEN_GOTENBERG_URL at an existing Gotenberg.
    libreoffice: str = "soffice"
    gotenberg_url: str = ""
    office_timeout: int = 180

    # Printers: "ipp://host/ipp/print" or "Name=ipp://host/ipp/print", comma-separated.
    printers: str = ""
    # Find IPP Everywhere / AirPrint printers on the network (mDNS), in addition to
    # the ones listed in PLATEN_PRINTERS.
    discover_printers: bool = True
    # Scanners for sane-airscan: "Name=http://host:8080/eSCL", comma-separated.
    scanners: str = ""
    # Find eSCL (AirScan) scanners on the network (mDNS), in addition to PLATEN_SCANNERS.
    scanner_discovery: bool = True
    # Minutes between network discovery runs (0 = only at start and on "Search again").
    discovery_interval: int = 10
    # Seconds to listen for mDNS answers per run.
    discovery_timeout: float = 5.0
    # Remove auto-discovered printers that have not been seen for this many hours.
    discovery_forget_hours: int = 24
    # Where sane-airscan reads its config (the app rewrites airscan.conf).
    sane_config_dir: Path = Path("/etc/sane.d")
    # SANE backends to enable (written to /etc/sane.d/dll.conf in the container).
    sane_backends: str = "airscan"

    # Page quotas: "block" refuses jobs over a user's quota, "warn" only shows it.
    quota_mode: str = "block"
    # Minutes between toner/supply level samples (for the dashboard history). 0 = off.
    supply_sample_minutes: int = 360

    retention_days: int = 30
    max_upload_mb: int = 50

    # Abuse protection (0 turns a limit off). Counted per logged-in user, else per IP.
    rate_limit_per_minute: int = 600          # all requests
    heavy_rate_limit_per_minute: int = 30     # uploads, scans, retries, device searches
    # Extra origins allowed to send forms/API writes with a browser, comma-separated,
    # e.g. "https://home.example.com". Same-origin requests never need this.
    trusted_origins: str = ""
    max_concurrent_jobs: int = 4
    # Seconds between CUPS job status polls.
    poll_interval: float = 2.0
    scan_timeout: int = 300

    # "real" uses CUPS and SANE; "fake" simulates devices (tests, UI demo).
    backend: str = "real"

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'platen.db'}"

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host)

    @property
    def trusted_origin_set(self) -> frozenset[str]:
        return frozenset(o.strip().rstrip("/").lower()
                         for o in self.trusted_origins.split(",") if o.strip())

    @property
    def paperless_enabled(self) -> bool:
        return bool(self.paperless_url)

    @property
    def office_backend(self) -> str | None:
        """How Office documents are converted: "gotenberg", "libreoffice" or None."""
        if self.gotenberg_url:
            return "gotenberg"
        from .convert import libreoffice_available
        return "libreoffice" if libreoffice_available(self.libreoffice) else None

    @property
    def office_enabled(self) -> bool:
        return self.office_backend is not None

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
