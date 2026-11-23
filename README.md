# Platen

A self-hosted print & scan manager. Platen adds a web UI, a job log, and (later) users,
statistics and document routing on top of **CUPS** and **SANE**. It does not ship its own
printer drivers or protocol code.

> **Status: MVP (v0.1).** Single user. Scan to PDF and download it, upload a file and print
> it, and a job log. OCR, scan-to-email, Paperless-ngx, multi-user/OIDC and statistics are
> on the roadmap.

## What it does

- **Print**: upload a PDF, JPEG, PNG or plain-text file. Choose the printer, copies,
  one- or two-sided, color mode and page range. Platen counts pages itself, sends the job
  to CUPS, and follows it until it completes.
- **Scan**: scan page by page from the browser with a thumbnail preview. You can rescan or
  delete pages before finishing. Finishing combines the pages into one PDF (lossless,
  via `img2pdf`) for download.
- **Job log**: every print and scan is a job with device, pages, options, status, times
  and any error. The status updates live.
- **Devices**: printer state, duplex/color support, supply levels (when reported) and the
  scanners SANE can see.
- **JSON API** for everything above (`/api/...`, docs at `/docs`).

## Quick start

```bash
cp .env.example .env      # set your printer/scanner and a password
docker compose up -d --build
```

Open `http://<host>:8631`.

Tested on `arm64` (Raspberry Pi, Ubuntu 24.04). Every dependency is a Debian package or a
pure-Python/manylinux wheel, so `amd64` should build too, but that hasn't been tested yet.

## Configuration

All settings are environment variables (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `PLATEN_PORT` | `8631` | Web UI port |
| `PLATEN_PASSWORD` | *(empty)* | Login password. Empty means no login, so only use that on a trusted network |
| `PLATEN_PRINTERS` | *(empty)* | Comma-separated `Name=ipp://host/ipp/print` (or just the URI). Empty means discover via mDNS |
| `PLATEN_DISCOVER_PRINTERS` | `true` | Look for IPP Everywhere printers when none are configured |
| `PLATEN_SCANNERS` | *(empty)* | Comma-separated `Name=http://host:port/eSCL` for sane-airscan (WSD URLs also work) |
| `PLATEN_SCANNER_DISCOVERY` | `true` | Let sane-airscan find scanners via mDNS |
| `PLATEN_SANE_BACKENDS` | `airscan` | SANE backends to enable |
| `PLATEN_RETENTION_DAYS` | `30` | Days before stored files (uploads, PDFs) are deleted. Job records stay |
| `PLATEN_MAX_UPLOAD_MB` | `50` | Upload size limit |
| `PLATEN_SECRET_KEY` | *(generated)* | Cookie signing key. Generated into `/data/secret_key` if unset |
| `CUPS_SERVER` | *(bundled)* | Use an external CUPS (e.g. `localhost:631`) instead of the bundled one |
| `TZ` | `UTC` | Timezone for timestamps in the UI |

To find your printer's IPP path and scanner URL, run `avahi-browse -rt _ipp._tcp` and
`avahi-browse -rt _uscan._tcp` on the host and look at the `rp=` and `rs=` TXT records.
They are usually `ipp/print` and `eSCL`.

## How it is built

One container runs:

- **cupsd**, listening only on `/run/cups/cups.sock`. It has no TCP port, so it does not
  clash with a CUPS on the host. Printers are added as driverless queues
  (`lpadmin -m everywhere`).
- **SANE** with **sane-airscan** (eSCL/WSD), so most network MFPs work without vendor drivers.
- **FastAPI** app with Jinja2 + HTMX pages, SQLite in `/data`, and an in-process job runner.
  Each job runs a list of steps (`app/jobs/steps.py`), so features like OCR or email are
  added as new steps.

### Device discovery

mDNS does not cross Docker's bridge network, so the container uses `network_mode: host`.
CUPS and sane-airscan reach the host's avahi-daemon over D-Bus (`/run/dbus` is mounted).

**Ubuntu/AppArmor:** Docker's default AppArmor profile blocks D-Bus calls from
containers. You will see `apparmor="DENIED" operation="dbus_method_call"` in the kernel
log, and discovery finds nothing. You can either:

1. Configure devices by address with `PLATEN_PRINTERS` / `PLATEN_SCANNERS` (recommended;
   give the printer a fixed DHCP lease), or
2. Run the container with `security_opt: ["apparmor:unconfined"]`. This weakens the
   container's isolation.

### USB devices

Not needed for network printers and scanners. For USB, pass the device in
(`devices: ["/dev/bus/usb:/dev/bus/usb"]`) and add the vendor SANE backend to
`PLATEN_SANE_BACKENDS`. A detailed, least-privilege USB guide is still to come.

## API

Interactive docs are at `/docs`. When a password is set, authenticate with the session
cookie or `Authorization: Bearer <PLATEN_PASSWORD>`.

```bash
# print
curl -F file=@doc.pdf -F printer=HP_LaserJet_MFP_M234dw -F duplex=two-sided-long-edge \
     http://localhost:8631/api/print
# scan two pages and download the PDF
JOB=$(curl -s -H 'content-type: application/json' \
      -d '{"scanner":"airscan:e0:HP LaserJet MFP M234dw","dpi":200}' \
      http://localhost:8631/api/scan | jq -r .id)
curl -X POST http://localhost:8631/api/scan/$JOB/pages
curl -X POST http://localhost:8631/api/scan/$JOB/pages
curl -X POST http://localhost:8631/api/scan/$JOB/finish
curl -o scan.pdf http://localhost:8631/api/jobs/$JOB/file
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest                                   # uses fake printer/scanner backends
PLATEN_BACKEND=fake PLATEN_DATA_DIR=./data \
  .venv/bin/uvicorn app.main:create_app --factory --reload --port 8631   # UI without hardware
docker build --target test .                       # run the tests inside the image
```

## Roadmap

1. **v0.1 MVP**: single user, scan to PDF, upload to print, job log. *(this release)*
2. **v0.2**: multi-user + OIDC, scan-to-email, Paperless-ngx upload, OCR (OCRmyPDF),
   Office documents via Gotenberg, and moving to a worker/Redis/Postgres compose stack.
3. **v0.3**: statistics dashboard, quotas, Tailscale sidecar, Prometheus metrics.
4. **v1**: native IPP queue over the tailnet, Home Assistant, push-button scanning.

## License

To be decided (AGPL-3.0 or MIT).
