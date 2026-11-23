# syntax=docker/dockerfile:1

# ---- runtime: CUPS + SANE (airscan) + the Python app -------------------------
FROM debian:trixie-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PLATEN_DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      cups cups-client cups-filters \
      sane-utils sane-airscan \
      python3 python3-venv python3-cups python3-img2pdf python3-pil \
      tini curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && sed -i -E 's/^SystemGroup .*/SystemGroup root lpadmin/' /etc/cups/cups-files.conf \
 && printf 'ErrorLog stderr\n' >> /etc/cups/cups-files.conf

COPY docker/cupsd.conf /etc/cups/cupsd.conf

WORKDIR /opt/platen
COPY pyproject.toml ./
# Install dependencies from pyproject (Debian already provides pycups, img2pdf, Pillow).
RUN python3 -m venv --system-site-packages "$VIRTUAL_ENV" \
 && pip install --no-cache-dir $(python3 -c "import tomllib; print(' '.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))")

COPY docker/entrypoint.sh ./docker/entrypoint.sh
COPY app ./app
RUN chmod +x docker/entrypoint.sh && mkdir -p /data

# ---- test: run the test suite inside the runtime image -------------------------
FROM runtime AS test
RUN pip install --no-cache-dir pytest httpx
COPY tests ./tests
RUN pytest

# ---- app (default target) ------------------------------------------------------
FROM runtime AS app
VOLUME ["/data"]
EXPOSE 8631
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PLATEN_PORT:-8631}/healthz" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/platen/docker/entrypoint.sh"]
