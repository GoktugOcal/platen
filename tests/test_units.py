from __future__ import annotations

from PIL import Image
from pypdf import PdfReader

from app.config import parse_entries
from app.container_setup import airscan_conf, dll_conf, scanner_entries
from app.devices.printers import name_from_uri, queue_name
from app.devices.scanners import parse_capabilities, parse_device_list
from app.pdf import (
    build_pdf,
    count_pages,
    detect_type,
    is_valid_page_range,
    make_thumbnail,
    page_range_count,
)

from .conftest import pdf_bytes


def test_parse_entries():
    assert parse_entries("") == []
    assert parse_entries("ipp://10.0.0.5/ipp/print") == [(None, "ipp://10.0.0.5/ipp/print")]
    assert parse_entries(" Office = ipp://h/ipp/print , ipp://x/y") == [
        ("Office", "ipp://h/ipp/print"), (None, "ipp://x/y"),
    ]
    uri = "dnssd://HP%20M234dw._ipp._tcp.local/?uuid=abc"
    assert parse_entries(uri) == [(None, uri)]
    assert parse_entries(f"HP={uri}") == [("HP", uri)]


def test_detect_type():
    assert detect_type(b"%PDF-1.7\n...") == "pdf"
    assert detect_type(b"\xff\xd8\xff\xe0rest") == "jpeg"
    assert detect_type(b"\x89PNG\r\n\x1a\nrest") == "png"
    assert detect_type("Hello, wörld\n".encode()) == "text"
    assert detect_type(b"PK\x03\x04\x00\x00") is None  # zip / docx
    assert detect_type(b"\x00\x01\x02") is None
    assert detect_type(b"") is None


def test_page_ranges():
    assert is_valid_page_range("1-3,5")
    assert is_valid_page_range(" 2 ")
    assert not is_valid_page_range("3-1")
    assert not is_valid_page_range("0")
    assert not is_valid_page_range("1-")
    assert not is_valid_page_range("a")
    assert page_range_count(None, 10) == 10
    assert page_range_count("1-3,5", 10) == 4
    assert page_range_count("2-3,3-4", 10) == 3
    assert page_range_count("8-20", 10) == 3


def test_count_pages_and_build_pdf(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(pdf_bytes(3))
    assert count_pages(pdf, "pdf") == 3
    assert count_pages(pdf, "jpeg") == 1
    assert count_pages(pdf, "text") is None

    images = []
    for i in range(2):
        path = tmp_path / f"p{i}.jpg"
        Image.new("L", (1654, 2339), "white").save(path, "JPEG")
        images.append(path)
    out = tmp_path / "scan.pdf"
    assert build_pdf(images, out, dpi=200) == 2
    reader = PdfReader(str(out))
    assert len(reader.pages) == 2
    # 1654 px at 200 DPI = 8.27 in = ~595 pt (A4 width)
    assert abs(float(reader.pages[0].mediabox.width) - 595) < 2

    thumb = tmp_path / "t.jpg"
    make_thumbnail(images[0], thumb)
    with Image.open(thumb) as t:
        assert t.width == 360


def test_queue_names():
    assert queue_name("HP LaserJet MFP M234dw (6A1E1A)") == "HP_LaserJet_MFP_M234dw_6A1E1A"
    assert queue_name("###") == "printer"
    assert name_from_uri("ipp://192.168.1.5/ipp/print") == "192.168.1.5"


def test_parse_scanimage_output():
    listing = "airscan:e0:HP LaserJet MFP M234dw (6A1E1A)\teSCL HP LaserJet MFP M234dw (6A1E1A)\n"
    assert [(s.id, s.label) for s in parse_device_list(listing)] == [
        ("airscan:e0:HP LaserJet MFP M234dw (6A1E1A)", "HP LaserJet MFP M234dw (6A1E1A)")
    ]

    options = """
All options specific to device `airscan:e0:HP':
  Standard:
    --resolution 200|300|600dpi [300]
        Sets the resolution of the scanned image.
    --mode Color|Gray [Color]
        Selects the scan mode.
    --source Flatbed [Flatbed]
        Selects the scan source.
"""
    caps = parse_capabilities(options)
    assert caps.resolutions == [200, 300, 600]
    assert caps.modes == ["Color", "Gray"]
    assert caps.sources == ["Flatbed"]
    assert caps.default_resolution() == 200
    assert caps.default_mode() == "Gray"

    ranged = parse_capabilities("    --resolution 50..1200dpi (in steps of 1) [300]\n")
    assert ranged.resolutions == [75, 100, 150, 200, 300, 600, 1200]


def test_container_config():
    entries = scanner_entries("Office=http://10.0.0.9:8080/eSCL, http://10.0.0.8/wsd")
    assert entries == [("Office", "http://10.0.0.9:8080/eSCL"), ("Scanner 2", "http://10.0.0.8/wsd")]
    conf = airscan_conf(entries)
    assert '"Office" = http://10.0.0.9:8080/eSCL, eSCL' in conf
    assert '"Scanner 2" = http://10.0.0.8/wsd, WSD' in conf
    assert "discovery = disable" in conf
    assert dll_conf("airscan, hpaio").splitlines()[1:] == ["airscan", "hpaio"]
