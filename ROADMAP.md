# Platen roadmap

Last updated: 2026-11-23. Based on the competitor review (cups-web, scanservjs,
openwebprint, rPrint, PaperCut Mobility Print).

**Direction:** close the everyday gaps first (scanning and printing depth, easy install),
then build what nobody in the self-hosted space has: a **native print queue with per-user
accounting**, on top of the deliveries, statistics and quotas Platen already has.

**Constraints for every release:**
- Stays light enough for a Raspberry Pi: no new always-on services in the default setup.
- Optional heavy parts (LibreOffice for Office files, OCR) are off by default.
- No external identity provider.
- Remote access stays the user's choice.
- Database changes go through the `schema_version` migrations, and upgrades from any
  earlier release keep working.

Effort: **S** about a day, **M** a few days, **L** a week or more.

---

## Released
| Version | Contents |
|---|---|
| v0.1 | Single container (CUPS + SANE + FastAPI), scan to PDF, upload to print, job log |
| v0.2 | Local users and roles, scan-to-email, Paperless-ngx, Office files via Gotenberg, optional Postgres |
| v0.3 | Statistics dashboard, toner history, page quotas, remote access through any proxy or tunnel, Apache-2.0 |

## v0.4: Polish and ship (in progress)
Goal: easy to install and trust.

| Item | Effort | Notes |
|---|---|---|
| shadcn-style UI with left sidebar and light/dark theme | done | |
| Own mDNS discovery (zeroconf), works under AppArmor | done | |
| Public GitHub repo, CI (tests on amd64 and arm64), multi-arch image on GHCR | done | standard and `-office` images |
| Office printing with LibreOffice bundled in Platen (`-office` image), replaces the Gotenberg sidecar | done | |
| Releases + CHANGELOG, screenshots in README, `compose.demo.yaml` (simulated devices) | S | |
| Security hardening: cross-site request blocking (Fetch Metadata / Origin), rate limits, early upload size check, strict CSP and security headers | done | |
| `.env` cleanup on the Pi (rely on discovery) | done | |

## v0.5: Scanning depth
Goal: match scanservjs on scanning while keeping Platen's routing.

| Item | Effort | Notes |
|---|---|---|
| Source selection: flatbed / feeder / two-sided feeder, from the scanner's own options | S | today `--source` is never passed |
| Feeder batch: scan until the feeder is empty (`scanimage --batch`) | M | |
| Page tools: rotate, reorder (drag), delete, simple crop | M | Pillow, server-side |
| Paper size: A4 / Letter / Legal / full glass | S | today it always scans the full glass |
| Output: PDF, JPEG, PNG, ZIP of images; quality and compression slider | S | |
| Blank-page removal (option) | S | from the original design doc |
| Scan presets, e.g. "Letter → Paperless, 200 DPI gray" | M | per user, plus shared admin presets |
| Same page tools in the API | S | |

## v0.6: Printing depth
Goal: match cups-web on printing.

| Item | Effort | Notes |
|---|---|---|
| Print preview (page thumbnails) | M | `pdftoppm` (poppler is already in the image) |
| Options: paper size, orientation, scaling (fit/actual), pages per sheet, quality | S | read from the printer's capabilities |
| Print presets per user | S | |
| Several images in one job (merge into one PDF), HEIC photos | S | `pillow-heif` |
| Reprint from history | S | while the file is still kept |
| Hold and release: a job waits until the user releases it from their phone at the printer | M | a "secure print" without special hardware |

## v0.7: Native print queue (the main differentiator)
Goal: phones and laptops add Platen as a normal printer (AirPrint / Mopria / IPP Everywhere),
and every job still goes through Platen's users, quotas, statistics and job log.

| Item | Effort | Notes |
|---|---|---|
| Prototype first: Python IPP server inside Platen (Print-Job, Validate-Job, Get-Printer-Attributes, Get-Jobs, Cancel-Job) that feeds the existing print pipeline | L | proves iOS, macOS, Android and Windows compatibility first |
| AirPrint / Mopria announcement via zeroconf (`_ipp`/`_ipps`, URF and PDF formats) | M | the zeroconf library is already used for discovery |
| Per-user sign-in on the device's print dialog: HTTP Basic over IPPS with a per-user "print password" | M | separate from the login password |
| One virtual queue per real printer; jobs count toward quotas and statistics | M | |
| Guest / unauthenticated mode (admin option) for trusted home networks | S | |

Risk: this is the largest item, and client quirks (AirPrint's URF format, Windows IPP) can take
time. The prototype comes first and decides whether this becomes v0.7 or moves later. The
fallback, CUPS printer sharing plus a backend hook, was considered and rejected: the user name
it reports cannot be verified.

## v0.8: Integrations and alerts
| Item | Effort | Notes |
|---|---|---|
| Destinations: network folder (SMB), WebDAV / Nextcloud | M | |
| Webhooks for job and delivery events (JSON, signed) | S | also covers n8n, Node-RED, etc. |
| Alerts: toner below a threshold, printer stopped, job failed (email and webhook) | M | toner history already exists |
| Home Assistant: MQTT discovery sensors (toner, pages today, printer state, last job) | M | HA already runs on the Pi |
| Optional Prometheus `/metrics` | S | low priority, for other self-hosters |

## v0.9: Accounting and administration
| Item | Effort | Notes |
|---|---|---|
| Groups (family, office, class), with quotas and printer access per group | M | |
| Cost per page (black and white, colour, two-sided discount), credits and balances | M | rPrint and PaperCut have this |
| CSV export of the job log and statistics | S | |
| Audit log of admin actions | S | |
| Optional two-factor login (TOTP) | M | |

## v1.0: Stable
| Item | Effort | Notes |
|---|---|---|
| Backup and restore (export and import of settings, users and job history) | M | |
| USB devices: printers via `ipp-usb`, guide for least-privilege USB passthrough | M | |
| Optional "full" image with vendor drivers (HPLIP, brscan) for non-driverless devices | M | separate tag, keeps the default image small |
| Documentation site, upgrade guarantees from 0.x, performance check on a Pi | M | |

## Later / on request
- OCR (OCRmyPDF) as an optional pipeline step. Paperless already OCRs uploads.
- Scanning started from the scanner's own button (vendor specific).
- Several Platen servers or sites.
- Single sign-on: deliberately not planned.

## Decisions
- **Publishing:** public GitHub repository, multi-arch image on GHCR (2026-11-23).
- **Native queue:** Python IPP server inside Platen (2026-11-23).
- **Language:** English only. Translations are not planned (2026-11-23).
- **OCR:** stays in "Later" (2026-11-23).
