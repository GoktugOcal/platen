"""Printer access through CUPS (driverless / IPP Everywhere queues)."""
from __future__ import annotations

import itertools
import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger(__name__)


class PrinterError(Exception):
    pass


@dataclass
class Marker:
    name: str
    level: int      # percent; negative means unknown
    color: str = ""


@dataclass
class PrinterInfo:
    name: str                 # CUPS queue name
    uri: str
    label: str
    make_model: str = ""
    state: str = "unknown"    # idle / printing / stopped
    state_message: str = ""
    accepting: bool = True
    color: bool = False
    duplex: bool = False
    markers: list[Marker] = field(default_factory=list)


@dataclass
class CupsJobState:
    state: str                # pending held processing stopped cancelled aborted completed
    reasons: list[str] = field(default_factory=list)
    impressions: int | None = None

    @property
    def final(self) -> bool:
        return self.state in ("cancelled", "aborted", "completed")


class PrinterBackend(Protocol):
    def list_printers(self) -> list[PrinterInfo]: ...
    def discover(self) -> list[tuple[str, str]]: ...
    def ensure_queue(self, name: str, uri: str, label: str | None = None) -> str: ...
    def submit(self, printer: str, path: Path, title: str, options: dict[str, str]) -> int: ...
    def job_state(self, job_id: int) -> CupsJobState: ...
    def cancel(self, job_id: int) -> None: ...
    def supply_levels(self, name: str) -> list[Marker]: ...


MARKER_ATTRIBUTES = ["marker-names", "marker-levels", "marker-colors"]


def markers_from_attrs(attrs: dict) -> list[Marker]:
    names = _as_list(attrs.get("marker-names"))
    levels = _as_list(attrs.get("marker-levels"))
    colors = _as_list(attrs.get("marker-colors"))
    return [Marker(n, int(lv), colors[i] if i < len(colors) else "")
            for i, (n, lv) in enumerate(zip(names, levels))]


def queue_name(label: str) -> str:
    """CUPS queue names may only contain printable chars except space, / and #."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_")
    return name[:100] or "printer"


def name_from_uri(uri: str) -> str:
    """Readable printer name from a dnssd:// service name or an ipp:// host."""
    parsed = urlparse(uri)
    if parsed.scheme == "dnssd":
        service = unquote(parsed.netloc)
        return re.split(r"\._ipps?\._tcp", service)[0]
    return parsed.hostname or uri


def parse_lpinfo(output: str) -> list[tuple[str, str]]:
    """Pick driverless-capable (IPP/IPPS) printers from ``lpinfo -v`` output.

    Returns (label, uri) pairs, one per printer UUID, preferring plain IPP.
    """
    found: dict[str, tuple[str, str]] = {}
    for line in output.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        uri = parts[1].strip()
        if not uri.startswith("dnssd://") or not re.search(r"\._ipps?\._tcp", uri):
            continue
        uuid = parse_qs(urlparse(uri).query).get("uuid", [uri])[0]
        is_ipps = "._ipps._tcp" in uri
        if uuid in found and is_ipps:
            continue
        found[uuid] = (name_from_uri(uri), uri)
    return list(found.values())


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


class CupsPrinterBackend:
    """Talks to the CUPS daemon (default: the unix socket inside the container)."""

    PRINTER_STATES = {3: "idle", 4: "printing", 5: "stopped"}
    JOB_STATES = {
        3: "pending", 4: "held", 5: "processing", 6: "stopped",
        7: "cancelled", 8: "aborted", 9: "completed",
    }

    def __init__(self) -> None:
        # pycups connections are not thread safe; one call at a time is plenty here.
        self._lock = threading.Lock()

    def _conn(self):
        import cups  # Debian python3-cups

        return cups.Connection()

    def list_printers(self) -> list[PrinterInfo]:
        import cups

        with self._lock:
            conn = self._conn()
            result = []
            for name, a in sorted(conn.getPrinters().items()):
                info = PrinterInfo(
                    name=name,
                    uri=a.get("device-uri", ""),
                    label=a.get("printer-info") or name,
                    make_model=a.get("printer-make-and-model", ""),
                    state=self.PRINTER_STATES.get(a.get("printer-state"), "unknown"),
                    state_message=a.get("printer-state-message", ""),
                    accepting=bool(a.get("printer-is-accepting-jobs", True)),
                )
                try:
                    attrs = conn.getPrinterAttributes(
                        name,
                        requested_attributes=[
                            "sides-supported", "color-supported",
                            "print-color-mode-supported",
                            "marker-names", "marker-levels", "marker-colors",
                        ],
                    )
                except cups.IPPError:
                    attrs = {}
                info.duplex = any(
                    s.startswith("two-sided") for s in _as_list(attrs.get("sides-supported"))
                )
                info.color = bool(attrs.get("color-supported")) or "color" in _as_list(
                    attrs.get("print-color-mode-supported")
                )
                info.markers = markers_from_attrs(attrs)
                result.append(info)
            return result

    def discover(self) -> list[tuple[str, str]]:
        try:
            proc = subprocess.run(
                ["lpinfo", "--include-schemes", "dnssd", "-v"],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PrinterError(f"printer discovery failed: {exc}") from exc
        return parse_lpinfo(proc.stdout)

    def ensure_queue(self, name: str, uri: str, label: str | None = None) -> str:
        with self._lock:
            existing = self._conn().getPrinters().get(name)
        if existing and existing.get("device-uri") == uri:
            return name
        cmd = ["lpadmin", "-p", name, "-E", "-v", uri, "-m", "everywhere"]
        if label:
            cmd += ["-D", label]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PrinterError(f"lpadmin failed for {uri}: {exc}") from exc
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).strip()
            raise PrinterError(f"could not add {uri}: {msg}")
        log.info("added printer queue %s -> %s", name, uri)
        return name

    def submit(self, printer: str, path: Path, title: str, options: dict[str, str]) -> int:
        import cups

        with self._lock:
            try:
                return self._conn().printFile(printer, str(path), title, options)
            except cups.IPPError as exc:
                raise PrinterError(f"CUPS rejected the job: {exc.args[-1]}") from exc

    def job_state(self, job_id: int) -> CupsJobState:
        with self._lock:
            a = self._conn().getJobAttributes(
                job_id,
                requested_attributes=[
                    "job-state", "job-state-reasons", "job-impressions-completed",
                ],
            )
        impressions = a.get("job-impressions-completed")
        return CupsJobState(
            state=self.JOB_STATES.get(a.get("job-state"), "unknown"),
            reasons=_as_list(a.get("job-state-reasons")),
            impressions=int(impressions) if impressions else None,
        )

    def supply_levels(self, name: str) -> list[Marker]:
        """Toner/ink levels. Driverless CUPS queues often do not report them, so
        ask the printer itself over IPP when the queue has an ipp(s):// URI."""
        import cups

        with self._lock:
            conn = self._conn()
            attrs = conn.getPrinters().get(name)
            if attrs is None:
                return []
            uri = attrs.get("device-uri", "")
            try:
                markers = markers_from_attrs(conn.getPrinterAttributes(
                    name, requested_attributes=MARKER_ATTRIBUTES))
            except cups.IPPError:
                markers = []
        parsed = urlparse(uri)
        if markers or parsed.scheme not in ("ipp", "ipps") or not parsed.hostname:
            return markers
        try:
            kwargs = {"host": parsed.hostname, "port": parsed.port or 631}
            if parsed.scheme == "ipps":
                kwargs["encryption"] = cups.HTTP_ENCRYPT_ALWAYS
            with self._lock:
                direct = cups.Connection(**kwargs)
                return markers_from_attrs(direct.getPrinterAttributes(
                    uri=uri, requested_attributes=MARKER_ATTRIBUTES))
        except (cups.IPPError, RuntimeError, OSError) as exc:
            log.info("could not read supplies from %s: %s", uri, exc)
            return []

    def cancel(self, job_id: int) -> None:
        import cups

        with self._lock:
            try:
                self._conn().cancelJob(job_id)
            except cups.IPPError as exc:
                log.info("cancel of CUPS job %s ignored: %s", job_id, exc)


class FakePrinterBackend:
    """In-memory printer used by tests and PLATEN_BACKEND=fake."""

    def __init__(self, steps_to_complete: int = 2) -> None:
        self.printers = {
            "Fake_Printer": PrinterInfo(
                name="Fake_Printer", uri="ipp://fake/ipp/print", label="Fake Printer",
                make_model="Fake IPP Everywhere", state="idle", color=True, duplex=True,
                markers=[Marker("Black toner", 72, "#000000")],
            )
        }
        self.jobs: dict[int, dict] = {}
        self.steps_to_complete = steps_to_complete
        self.fail_next = False
        self._ids = itertools.count(1)

    def list_printers(self) -> list[PrinterInfo]:
        return list(self.printers.values())

    def discover(self) -> list[tuple[str, str]]:
        return []

    def ensure_queue(self, name: str, uri: str, label: str | None = None) -> str:
        self.printers.setdefault(
            name, PrinterInfo(name=name, uri=uri, label=label or name, state="idle")
        )
        return name

    def submit(self, printer: str, path: Path, title: str, options: dict[str, str]) -> int:
        if printer not in self.printers:
            raise PrinterError(f"unknown printer {printer}")
        job_id = next(self._ids)
        self.jobs[job_id] = {
            "printer": printer, "path": str(path), "title": title, "options": dict(options),
            "polls": 0, "state": "pending", "fail": self.fail_next,
        }
        self.fail_next = False
        return job_id

    def job_state(self, job_id: int) -> CupsJobState:
        job = self.jobs[job_id]
        if job["state"] in ("cancelled", "aborted", "completed"):
            return CupsJobState(job["state"])
        job["polls"] += 1
        if job["polls"] >= self.steps_to_complete:
            job["state"] = "aborted" if job["fail"] else "completed"
            reasons = ["printer-stopped"] if job["fail"] else ["job-completed-successfully"]
            return CupsJobState(job["state"], reasons)
        job["state"] = "processing"
        return CupsJobState("processing", ["job-printing"])

    def supply_levels(self, name: str) -> list[Marker]:
        info = self.printers.get(name)
        return list(info.markers) if info else []

    def cancel(self, job_id: int) -> None:
        if job_id in self.jobs:
            self.jobs[job_id]["state"] = "cancelled"
