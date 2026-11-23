"""Network discovery of driverless printers and eSCL scanners via mDNS.

Platen talks multicast DNS itself (python-zeroconf) instead of going through
avahi over D-Bus, which Docker's default AppArmor profile blocks. Results are
turned into fixed addresses (ipp://IP:port/path, http://IP:port/eSCL) so
printing and scanning never depend on mDNS afterwards.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)

PRINTER_TYPES = {"_ipp._tcp.local.": "ipp", "_ipps._tcp.local.": "ipps"}
SCANNER_TYPES = {"_uscan._tcp.local.": "http", "_uscans._tcp.local.": "https"}
_SUFFIX_RE = re.compile(r"\s*\([0-9A-Fa-f]{4,}\)\s*$")


@dataclass
class Service:
    """One resolved mDNS service instance."""
    type: str
    name: str                          # instance name, e.g. "HP LaserJet MFP M234dw (6A1E1A)"
    addresses: list[str]
    port: int
    txt: dict[str, str] = field(default_factory=dict)


@dataclass
class Found:
    name: str                          # unique instance name
    label: str                         # friendly label (unique suffix removed)
    uri: str
    host: str
    uuid: str = ""


@dataclass
class DiscoveryResult:
    printers: list[Found] = field(default_factory=list)
    scanners: list[Found] = field(default_factory=list)


class Discoverer(Protocol):
    def browse(self, timeout: float) -> list[Service]: ...


def _pick_address(addresses: list[str]) -> str | None:
    """Prefer IPv4, then global IPv6; skip link-local (needs a zone id)."""
    v4 = [a for a in addresses if ipaddress.ip_address(a).version == 4]
    if v4:
        return v4[0]
    v6 = [a for a in addresses if not ipaddress.ip_address(a).is_link_local]
    return f"[{v6[0]}]" if v6 else None


def _resource(txt: dict[str, str], key: str, default: str) -> str:
    value = txt.get(key, default).strip("/")
    return value


def _instance_name(service_name: str, service_type: str) -> str:
    suffix = "." + service_type
    return service_name[: -len(suffix)] if service_name.endswith(suffix) else service_name


def interpret(services: list[Service]) -> DiscoveryResult:
    """Turn raw services into printers and scanners, one per device UUID,
    preferring plain IPP/HTTP over the TLS variants."""
    printers: dict[str, Found] = {}
    scanners: dict[str, Found] = {}
    for svc in services:
        host = _pick_address(svc.addresses)
        if host is None:
            continue
        txt = {k.lower(): v for k, v in svc.txt.items()}
        name = _instance_name(svc.name, svc.type)
        uuid = (txt.get("uuid") or "").lower()
        key = uuid or f"{host}:{name}"
        if svc.type in PRINTER_TYPES:
            scheme = PRINTER_TYPES[svc.type]
            uri = f"{scheme}://{host}:{svc.port}/{_resource(txt, 'rp', 'ipp/print')}"
            target = printers
        elif svc.type in SCANNER_TYPES:
            scheme = SCANNER_TYPES[svc.type]
            uri = f"{scheme}://{host}:{svc.port}/{_resource(txt, 'rs', 'eSCL')}"
            target = scanners
        else:
            continue
        found = Found(name=name, label=name, uri=uri, host=host.strip("[]"), uuid=uuid)
        existing = target.get(key)
        if existing is None or (existing.uri.startswith(("ipps:", "https:"))
                                and not uri.startswith(("ipps:", "https:"))):
            target[key] = found

    result = DiscoveryResult(sorted(printers.values(), key=lambda f: f.name),
                             sorted(scanners.values(), key=lambda f: f.name))
    for group in (result.printers, result.scanners):
        # Drop the "(6A1E1A)" suffix from labels unless it is needed to tell devices apart.
        bases = [_SUFFIX_RE.sub("", f.name) for f in group]
        for f, base in zip(group, bases):
            f.label = base if bases.count(base) == 1 else f.name
    return result


class ZeroconfDiscoverer:
    """Browses the local network for a few seconds (blocking; run in a thread)."""

    def browse(self, timeout: float) -> list[Service]:
        from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf

        seen: set[tuple[str, str]] = set()

        class Listener(ServiceListener):
            def add_service(self, zc, type_, name):  # noqa: D401
                seen.add((type_, name))

            def update_service(self, zc, type_, name):
                seen.add((type_, name))

            def remove_service(self, zc, type_, name):
                pass

        # IPv4 only: every LAN printer has an IPv4 address, and IPv6 multicast fails
        # noisily on loopback / v6-less interfaces in containers.
        zc = Zeroconf(ip_version=IPVersion.V4Only)
        try:
            ServiceBrowser(zc, list(PRINTER_TYPES) + list(SCANNER_TYPES), Listener())
            time.sleep(timeout)
            services = []
            for type_, name in sorted(seen):
                info = zc.get_service_info(type_, name, timeout=3000)
                if info is None:
                    continue
                txt = {k.decode(errors="replace"): (v.decode(errors="replace") if v else "")
                       for k, v in (info.properties or {}).items()}
                services.append(Service(type_, name, info.parsed_addresses(), info.port, txt))
            return services
        finally:
            zc.close()


class FakeDiscoverer:
    """Returns a fixed list of services (tests, PLATEN_BACKEND=fake)."""

    def __init__(self, services: list[Service] | None = None):
        self.services = services or []
        self.calls = 0

    def browse(self, timeout: float) -> list[Service]:
        self.calls += 1
        return list(self.services)


def discover(discoverer: Discoverer, timeout: float) -> DiscoveryResult:
    try:
        return interpret(discoverer.browse(timeout))
    except Exception as exc:  # noqa: BLE001 - discovery must never break the app
        log.warning("network discovery failed: %s", exc)
        return DiscoveryResult()
