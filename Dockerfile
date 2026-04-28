ARG OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw:latest
FROM ${OPENCLAW_IMAGE}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

USER root

ARG OPENCLAW_UID=1000
ARG OPENCLAW_GID=1000

RUN set -eux; \
    if command -v apt-get >/dev/null 2>&1; then \
      apt-get update; \
      apt-get install -y --no-install-recommends bash ca-certificates curl python3 python3-pip python3-venv tini tesseract-ocr tesseract-ocr-eng tesseract-ocr-ita; \
      rm -rf /var/lib/apt/lists/*; \
    elif command -v apk >/dev/null 2>&1; then \
      apk add --no-cache bash ca-certificates curl python3 py3-pip tini tesseract-ocr tesseract-ocr-data-eng tesseract-ocr-data-ita \
      || apk add --no-cache bash ca-certificates curl python3 py3-pip tini tesseract-ocr; \
    else \
      echo "Unsupported base image: no apt-get or apk found" >&2; \
      exit 1; \
    fi; \
    if command -v groupadd >/dev/null 2>&1; then \
      getent group openclaw >/dev/null 2>&1 || groupadd -o --gid "${OPENCLAW_GID}" openclaw; \
      id -u openclaw >/dev/null 2>&1 || useradd -o --uid "${OPENCLAW_UID}" --gid "${OPENCLAW_GID}" --create-home --shell /bin/bash openclaw; \
    else \
      addgroup -g "${OPENCLAW_GID}" -S openclaw 2>/dev/null || addgroup -S openclaw; \
      id -u openclaw >/dev/null 2>&1 || adduser -S -D -h /home/openclaw -s /bin/bash -u "${OPENCLAW_UID}" -G openclaw openclaw; \
    fi

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt && rm -f /tmp/requirements.txt

COPY src /opt/firefly-openclaw/src
COPY scripts /opt/firefly-openclaw/bin
COPY workspace /opt/firefly-openclaw/workspace
COPY docker/entrypoint.sh /usr/local/bin/firefly-openclaw-entrypoint.sh
COPY openclaw.secure.json5.example /opt/firefly-openclaw/config/openclaw.base.json5
COPY .env.example /opt/firefly-openclaw/config/.env.example

RUN set -eux; \
    chmod 0755 /usr/local/bin/firefly-openclaw-entrypoint.sh; \
    for file in /opt/firefly-openclaw/bin/*.sh; do [ -e "${file}" ] || continue; chmod 0755 "${file}"; done; \
    if [ -f /opt/firefly-openclaw/workspace/tools/firefly-bridge ]; then chmod 0755 /opt/firefly-openclaw/workspace/tools/firefly-bridge; fi; \
    mkdir -p /home/openclaw/.openclaw /home/openclaw/workspace; \
    chmod 0700 /home/openclaw/.openclaw /home/openclaw/workspace; \
    chown -R "${OPENCLAW_UID}:${OPENCLAW_GID}" /home/openclaw /opt/firefly-openclaw

ENV HOME=/home/openclaw
ENV OPENCLAW_HOME=/home/openclaw
ENV OPENCLAW_WORKSPACE=/home/openclaw/workspace
ENV PYTHONPATH=/opt/firefly-openclaw/src
ENV PYTHONUNBUFFERED=1

USER ${OPENCLAW_UID}:${OPENCLAW_GID}

EXPOSE 18789

ENTRYPOINT ["tini", "--", "/usr/local/bin/firefly-openclaw-entrypoint.sh"]
CMD ["openclaw", "gateway"]
