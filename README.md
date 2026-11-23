# Platen

A self-hosted print & scan manager. Platen adds a web UI, a job log, and (later) users,
statistics and document routing on top of **CUPS** and **SANE**. It does not ship its own
printer drivers or protocol code.

> **Status: v0.3.** Multiple users, printing and scanning to PDF, scan-to-email,
> Paperless-ngx upload, printing Office documents (optional), a job log, a usage dashboard
> with toner history, and page quotas. OCR is on the roadmap.

## What it does

- **Print**: upload a PDF, JPEG, PNG or plain-text file. Choose the printer, copies,
  one- or two-sided, color mode and page range. Platen counts pages itself, sends the job
  to CUPS, and follows it until it completes.
- **Scan**: scan page by page from the browser with a thumbnail preview. You can rescan or
  delete pages before finishing. Finishing combines the pages into one PDF (lossless,
  via `img2pdf`) for download.
- **Send scans** by email (SMTP, e.g. Gmail) and/or to **Paperless-ngx**, with title, tags,
  correspondent and document type chosen at scan time. Failed deliveries can be retried, and
  the PDF always stays downloadable.
- **Office documents** (docx, xlsx, pptx, odt, …) print through an optional Gotenberg
  sidecar.
- **Users**: local accounts with admin and user roles. Users only see their own jobs.
  Admins can limit which printers each user may use. Each user gets a personal API token
  and their own Paperless token, so documents are owned by the right Paperless user.
- **Statistics**: a built-in dashboard (no Prometheus or Grafana needed). It shows pages
  printed and scanned over time, paper saved by two-sided printing, colour vs black and
  white, per printer and per user, the most common errors, and toner level history read
  directly from the printer. It's also available as JSON at `/api/stats`.
- **Page quotas**: an optional per-user page limit per week or month. Jobs over the limit
  are refused (or only flagged, if you choose), and users see what's left on the Print page.
- **Job log**: every print and scan is a job with user, device, pages, options, status,
  times and any error. The status updates live.
- **Devices**: printer state, duplex/color support, supply levels (when reported) and the
  scanners SANE can see.
- **JSON API** for everything above (`/api/...`, docs at `/docs`).

## Quick start

```bash
cp .env.example .env      # set your printer/scanner and a password
docker compose up -d --build
```

Open `http://<host>:8631`. On first start you are asked to create the admin account,
unless `PLATEN_PASSWORD` is set, in which case an admin `admin` is created with that password.
Do this right away: until the first admin exists, anyone who can reach the port could create it.

Tested on `arm64` (Raspberry Pi, Ubuntu 24.04). Every dependency is a Debian package or a
pure-Python/manylinux wheel, so `amd64` should build too, but that hasn't been tested yet.

## Configuration

All settings are environment variables (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `PLATEN_PORT` | `8631` | Web UI port |
| `PLATEN_PASSWORD` | *(empty)* | Creates admin `admin` with this password if there are no users yet. Otherwise use `/setup` |
| `PLATEN_PRINTERS` | *(empty)* | Comma-separated `Name=ipp://host/ipp/print` (or just the URI). Empty means discover via mDNS |
| `PLATEN_DISCOVER_PRINTERS` | `true` | Look for IPP Everywhere printers when none are configured |
| `PLATEN_SCANNERS` | *(empty)* | Comma-separated `Name=http://host:port/eSCL` for sane-airscan (WSD URLs also work) |
| `PLATEN_SCANNER_DISCOVERY` | `true` | Let sane-airscan find scanners via mDNS |
| `PLATEN_SANE_BACKENDS` | `airscan` | SANE backends to enable |
| `PLATEN_SMTP_HOST` / `_PORT` / `_SECURITY` | *(empty)* / `587` / `starttls` | SMTP server for scan-to-email (`ssl` for port 465) |
| `PLATEN_SMTP_USER` / `_PASSWORD` / `_FROM` | *(empty)* | SMTP login. FROM defaults to the user |
| `PLATEN_EMAIL_MAX_MB` | `18` | Largest PDF to email (18 MB is about 24 MB once encoded; Gmail's limit is 25 MB) |
| `PLATEN_PAPERLESS_URL` | *(empty)* | Paperless-ngx base URL; enables "Send to Paperless" |
| `PLATEN_PAPERLESS_TOKEN` | *(empty)* | Fallback token for users who have not set their own |
| `PLATEN_PAPERLESS_WAIT` | `120` | Seconds to wait for Paperless to finish consuming |
| `PLATEN_GOTENBERG_URL` | *(empty)* | Enables Office documents, e.g. `http://127.0.0.1:3100` |
| `PLATEN_QUOTA_MODE` | `block` | `block` refuses jobs over a user's quota; `warn` only shows usage |
| `PLATEN_SUPPLY_SAMPLE_MINUTES` | `360` | How often toner levels are recorded for the dashboard (0 = off) |
| `PLATEN_TRUSTED_PROXIES` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted (see Remote access) |
| `PLATEN_HOST` | `0.0.0.0` | Address to listen on; `127.0.0.1` when only a local proxy or tunnel should reach it |
| `PLATEN_DATABASE_URL` | SQLite in `/data` | e.g. `postgresql+psycopg://user:pw@host/db` |
| `PLATEN_RETENTION_DAYS` | `30` | Days before stored files (uploads, PDFs) are deleted. Job records stay |
| `PLATEN_MAX_UPLOAD_MB` | `50` | Upload size limit |
| `PLATEN_SECRET_KEY` | *(generated)* | Cookie signing key. Generated into `/data/secret_key` if unset |
| `CUPS_SERVER` | *(bundled)* | Use an external CUPS (e.g. `localhost:631`) instead of the bundled one |
| `TZ` | `UTC` | Timezone for timestamps in the UI |

To find your printer's IPP path and scanner URL, run `avahi-browse -rt _ipp._tcp` and
`avahi-browse -rt _uscan._tcp` on the host and look at the `rp=` and `rs=` TXT records.
They are usually `ipp/print` and `eSCL`.

## Users and access

- **Roles**: *admin* (everything, user management, device refresh) and *user* (print, scan,
  own jobs only). There is always at least one active admin.
- **Passwords** are hashed with scrypt. Changing a password or disabling a user logs them out
  everywhere. Repeated failed logins are slowed down.
- **API tokens**: create one on the Account page and use `Authorization: Bearer <token>`.
- There is no external identity provider (OIDC) on purpose: Platen stays light enough for a
  Raspberry Pi. Tailscale identity headers may come later.

## Quotas

An admin sets a quota per user (Users → Edit): pages per week or per month, where 0 means
unlimited. Printed pages count × copies, including jobs still in progress, so a burst of
jobs can't go over the limit. In `block` mode a job that would go over is refused with a
message saying how many pages are left. Plain-text files only report their page count
after printing, so they count afterwards.

## Remote access

Platen deliberately ships no remote-access component. Use whatever you already run; Platen
only needs to see the right client address and scheme. Bind it to localhost when
everything comes in through a local proxy or tunnel (`PLATEN_HOST=127.0.0.1`), and list
the proxy in `PLATEN_TRUSTED_PROXIES` if it isn't on the same machine. With HTTPS in
front, the login cookie is automatically marked `Secure`.

Serve Platen on its own hostname or port; a sub-path such as `/platen/` is not supported.
Allow uploads up to `PLATEN_MAX_UPLOAD_MB` (default 50 MB) in your proxy.

**Tailscale** (on the host):
```bash
tailscale serve --bg --https=8443 http://127.0.0.1:8631
# -> https://<machine>.<tailnet>.ts.net:8443
```

**Caddy**:
```
print.example.com {
    reverse_proxy 127.0.0.1:8631
}
```

**nginx**:
```nginx
location / {
    proxy_pass http://127.0.0.1:8631;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 50m;
}
```

**Cloudflare Tunnel**: point an ingress rule at `http://localhost:8631`. Consider putting
Cloudflare Access in front, because Platen's login is then the only thing guarding it on
the public internet.

**WireGuard / other VPNs**: nothing to configure. Open `http://<lan-ip>:8631` while
connected.

Whatever you choose, finish `/setup` (create the admin) **before** exposing Platen.

## Scan-to-email with Gmail

1. Turn on 2-step verification for the Google account, then create an app password at
   <https://myaccount.google.com/apppasswords>.
2. In `.env`: `PLATEN_SMTP_HOST=smtp.gmail.com`, `PLATEN_SMTP_USER=you@gmail.com`,
   `PLATEN_SMTP_PASSWORD=<app password>`.
3. Run `docker compose up -d`. Users set their default address on the Account page.

## Paperless-ngx

Set `PLATEN_PAPERLESS_URL`. Each user pastes their Paperless API token (Paperless → profile →
API Auth Token) on the Platen Account page, so uploads belong to them. Documents go through
the REST API (`/api/documents/post_document/`), not the consume folder. Platen waits for
Paperless to finish and links to the new document, and reports duplicates and other failures
on the job page.

## Office documents (optional)

```bash
echo 'PLATEN_GOTENBERG_URL=http://127.0.0.1:3100' >> .env
docker compose --profile office up -d
```

Gotenberg (LibreOffice) is a large image, about 1.5 GB, and uses several hundred MB of RAM
while converting, so it is off by default.

## How it is built

One container runs:

- **cupsd**, listening only on `/run/cups/cups.sock`. It has no TCP port, so it does not
  clash with a CUPS on the host. Printers are added as driverless queues
  (`lpadmin -m everywhere`).
- **SANE** with **sane-airscan** (eSCL/WSD), so most network MFPs work without vendor drivers.
- **FastAPI** app with Jinja2 + HTMX pages, SQLite in `/data` (Postgres optional), and an
  in-process job runner. Each job runs a list of steps (`app/jobs/steps.py`): print is
  *convert → count pages → submit → wait*, scan is *build PDF → deliveries*. New features
  are added as new steps.

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

Interactive docs are at `/docs`. Authenticate with `Authorization: Bearer <api token>`
(create a token on the Account page).

```bash
H="Authorization: Bearer plt_..."
# print
curl -H "$H" -F file=@doc.pdf -F printer=HP_LaserJet_MFP_M234dw -F duplex=two-sided-long-edge \
     http://localhost:8631/api/print
# scan two pages and download the PDF
JOB=$(curl -s -H "$H" -H 'content-type: application/json' \
      -d '{"scanner":"airscan:e0:HP LaserJet MFP M234dw","dpi":200}' \
      http://localhost:8631/api/scan | jq -r .id)
curl -H "$H" -X POST http://localhost:8631/api/scan/$JOB/pages
curl -H "$H" -X POST http://localhost:8631/api/scan/$JOB/pages
# finish; optionally deliver: {"email": "...", "paperless": {"title": "...", "tags": [1]}}
curl -H "$H" -H 'content-type: application/json' -d '{"email":"me@example.com"}' \
     http://localhost:8631/api/scan/$JOB/finish
curl -H "$H" -o scan.pdf http://localhost:8631/api/jobs/$JOB/file
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest                                   # uses fake printer/scanner backends
PLATEN_BACKEND=fake PLATEN_DATA_DIR=./data PLATEN_PASSWORD=dev-password \
  .venv/bin/uvicorn app.main:create_app --factory --reload --port 8631   # UI without hardware
docker build --target test .                       # run the tests inside the image
# against Postgres:
PLATEN_TEST_DATABASE_URL=postgresql+psycopg://user:pw@localhost/platen_test .venv/bin/pytest
```

## Roadmap

1. **v0.1 MVP**: single user, scan to PDF, upload to print, job log.
2. **v0.2**: local multi-user accounts, scan-to-email, Paperless-ngx, Office documents via
   Gotenberg, optional Postgres.
3. **v0.3**: built-in statistics dashboard with toner history, page quotas, and
   documented remote access through any proxy, tunnel or VPN. *(this release)*
4. **Later / optional**: OCR (OCRmyPDF), Prometheus `/metrics`, a separate worker with Redis
   for large installs.
5. **v1**: native IPP queue over the tailnet, Home Assistant, push-button scanning.

## License

Copyright 2026 Göktuğ Öcal. Licensed under the [Apache License 2.0](LICENSE).
Bundled third-party files are listed in [NOTICE](NOTICE).
