from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.discovery import FakeDiscoverer, Service, interpret
from app.main import create_app

from .conftest import login, make_settings

UUID = "c5c07fc0-11af-4015-9149-6865a79fe16e"


def hp_services(ip="192.168.129.80"):
    name = "HP LaserJet MFP M234dw (6A1E1A)"
    addrs = [ip, "fe80::522e:66ff:fe47:5066"]
    return [
        Service("_ipps._tcp.local.", f"{name}._ipps._tcp.local.", addrs, 631, {"rp": "ipp/print", "UUID": UUID}),
        Service("_ipp._tcp.local.", f"{name}._ipp._tcp.local.", addrs, 631, {"rp": "ipp/print", "UUID": UUID}),
        Service("_uscan._tcp.local.", f"{name}._uscan._tcp.local.", addrs, 8080, {"rs": "eSCL", "uuid": UUID}),
        Service("_uscans._tcp.local.", f"{name}._uscans._tcp.local.", addrs, 443, {"rs": "eSCL", "uuid": UUID}),
    ]


def test_interpret_dedupes_and_prefers_plain():
    r = interpret(hp_services())
    assert [(p.label, p.uri) for p in r.printers] == [
        ("HP LaserJet MFP M234dw", "ipp://192.168.129.80:631/ipp/print")]
    assert [(s.label, s.uri) for s in r.scanners] == [
        ("HP LaserJet MFP M234dw", "http://192.168.129.80:8080/eSCL")]


def test_interpret_labels_and_addresses():
    same_model = [
        Service("_ipp._tcp.local.", "Brother HL (AAAA11)._ipp._tcp.local.", ["10.0.0.2"], 631, {"rp": "ipp/print", "UUID": "a"}),
        Service("_ipp._tcp.local.", "Brother HL (BBBB22)._ipp._tcp.local.", ["10.0.0.3"], 631, {"rp": "/ipp/print", "UUID": "b"}),
        Service("_ipp._tcp.local.", "v6 only._ipp._tcp.local.", ["fe80::1", "2001:db8::5"], 631, {}),
        Service("_ipp._tcp.local.", "link local only._ipp._tcp.local.", ["fe80::1"], 631, {}),
    ]
    r = interpret(same_model)
    assert [(p.label, p.uri) for p in r.printers] == [
        ("Brother HL (AAAA11)", "ipp://10.0.0.2:631/ipp/print"),
        ("Brother HL (BBBB22)", "ipp://10.0.0.3:631/ipp/print"),
        ("v6 only", "ipp://[2001:db8::5]:631/ipp/print"),
    ]


def _app(tmp_path, printer_backend, scanner_backend, discoverer, **settings):
    sane = tmp_path / "sane"
    sane.mkdir(exist_ok=True)
    s = make_settings(tmp_path, sane_config_dir=sane, discovery_interval=0, **settings)
    return create_app(s, printer_backend, scanner_backend, discoverer=discoverer), sane


def test_discovered_devices_are_set_up(tmp_path, printers, scanners):
    disc = FakeDiscoverer(hp_services())
    app, sane = _app(tmp_path, printers, scanners, disc, discover_printers=True)
    with TestClient(app) as c:
        login(c)
        services = app.state.services
        asyncio.run(services.sync_devices())
        assert printers.printers["HP_LaserJet_MFP_M234dw"].uri == "ipp://192.168.129.80:631/ipp/print"
        assert services.device_origin["HP_LaserJet_MFP_M234dw"] == "discovered"
        conf = (sane / "airscan.conf").read_text()
        assert '"HP LaserJet MFP M234dw" = http://192.168.129.80:8080/eSCL, eSCL' in conf
        assert services.scanner_source("airscan:e0:HP LaserJet MFP M234dw") == "discovered"
        html = c.get("/devices").text
        assert "Auto-discovered" in html and "found 1 printer and 1 scanner" in html

        # The printer moves to a new address: the queue follows it.
        disc.services = hp_services("192.168.129.99")
        asyncio.run(services.sync_devices())
        assert printers.printers["HP_LaserJet_MFP_M234dw"].uri == "ipp://192.168.129.99:631/ipp/print"
        assert "192.168.129.99" in (sane / "airscan.conf").read_text()


def test_manual_entries_win_over_discovery(tmp_path, printers, scanners):
    disc = FakeDiscoverer(hp_services())
    app, sane = _app(tmp_path, printers, scanners, disc, **{
        "discover_printers": True, "printers": "Office=ipp://192.168.129.80/ipp/print",
        "scanners": "Office scanner=http://192.168.129.80:8080/eSCL"})
    with TestClient(app) as c:
        login(c)
        services = app.state.services
        asyncio.run(services.sync_devices())
        assert services.device_origin == {"Office": "manual"}
        assert "HP_LaserJet_MFP_M234dw" not in printers.printers
        conf = (sane / "airscan.conf").read_text()
        assert conf.count(" = http") == 1 and '"Office scanner"' in conf


def test_discovery_can_be_disabled(tmp_path, printers, scanners):
    disc = FakeDiscoverer(hp_services())
    app, sane = _app(tmp_path, printers, scanners, disc, discover_printers=False,
                     scanner_discovery=False)
    with TestClient(app) as c:
        login(c)
        asyncio.run(app.state.services.sync_devices())
        assert disc.calls == 0
        assert "HP_LaserJet_MFP_M234dw" not in printers.printers


def test_vanished_printers_are_forgotten(tmp_path, printers, scanners):
    disc = FakeDiscoverer(hp_services())
    app, _ = _app(tmp_path, printers, scanners, disc, discover_printers=True,
                  discovery_forget_hours=0)
    with TestClient(app) as c:
        login(c)
        services = app.state.services
        asyncio.run(services.sync_devices())
        assert "HP_LaserJet_MFP_M234dw" in printers.printers
        disc.services = []
        messages = asyncio.run(services.sync_devices())
        assert "HP_LaserJet_MFP_M234dw" not in printers.printers
        assert any("not seen on the network" in m for m in messages)


def test_scanner_survives_a_missed_discovery_run(tmp_path, printers, scanners):
    disc = FakeDiscoverer(hp_services())
    app, sane = _app(tmp_path, printers, scanners, disc, discover_printers=True)
    with TestClient(app) as c:
        login(c)
        services = app.state.services
        asyncio.run(services.sync_devices())
        disc.services = []                      # one run where the answers got lost
        asyncio.run(services.sync_devices())
        assert "192.168.129.80:8080/eSCL" in (sane / "airscan.conf").read_text()
        assert "HP_LaserJet_MFP_M234dw" in printers.printers
