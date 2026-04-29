ARG PICOCLAW_IMAGE=docker.io/sipeed/picoclaw:latest
FROM ${PICOCLAW_IMAGE} AS picoclaw-upstream

FROM debian:stable-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

USER root

ARG PICOCLAW_UID=1000
ARG PICOCLAW_GID=1000

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      python3 \
      python3-pip \
      python3-venv \
      tini \
      tesseract-ocr \
      tesseract-ocr-eng \
      tesseract-ocr-ita; \
    rm -rf /var/lib/apt/lists/*; \
    getent group picoclaw >/dev/null 2>&1 || groupadd -o --gid "${PICOCLAW_GID}" picoclaw; \
    id -u picoclaw >/dev/null 2>&1 || useradd -o --uid "${PICOCLAW_UID}" --gid "${PICOCLAW_GID}" --create-home --shell /bin/bash picoclaw

COPY --from=picoclaw-upstream /usr/local/bin/picoclaw /usr/local/bin/picoclaw

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt && rm -f /tmp/requirements.txt

COPY src /opt/firefly-picoclaw/src
COPY scripts /opt/firefly-picoclaw/bin
COPY workspace /opt/firefly-picoclaw/workspace
COPY docker/entrypoint.sh /usr/local/bin/firefly-picoclaw-entrypoint.sh
COPY config.example.json /opt/firefly-picoclaw/config/config.example.json
COPY .env.example /opt/firefly-picoclaw/config/.env.example

RUN set -eux; \
    chmod 0755 /usr/local/bin/firefly-picoclaw-entrypoint.sh; \
    sed -i 's/\r$//' /usr/local/bin/firefly-picoclaw-entrypoint.sh; \
    for file in /opt/firefly-picoclaw/bin/*.sh; do \
      [ -e "${file}" ] || continue; \
      sed -i 's/\r$//' "${file}"; \
      chmod 0755 "${file}"; \
    done; \
    if [ -f /opt/firefly-picoclaw/workspace/tools/firefly-bridge ]; then \
      sed -i 's/\r$//' /opt/firefly-picoclaw/workspace/tools/firefly-bridge; \
      chmod 0755 /opt/firefly-picoclaw/workspace/tools/firefly-bridge; \
      install -m 0755 /opt/firefly-picoclaw/workspace/tools/firefly-bridge /opt/firefly-picoclaw/bin/firefly-bridge; \
    fi; \
    mkdir -p /home/picoclaw/.picoclaw/workspace /home/picoclaw/.picoclaw/logs; \
    chmod 0700 /home/picoclaw/.picoclaw /home/picoclaw/.picoclaw/workspace; \
    chmod 0750 /home/picoclaw/.picoclaw/logs; \
    chown -R "${PICOCLAW_UID}:${PICOCLAW_GID}" /home/picoclaw /opt/firefly-picoclaw

ENV HOME=/home/picoclaw
ENV PICOCLAW_HOME=/home/picoclaw
ENV PICOCLAW_WORKSPACE=/home/picoclaw/.picoclaw/workspace
ENV PYTHONPATH=/opt/firefly-picoclaw/src
ENV PYTHONUNBUFFERED=1

USER ${PICOCLAW_UID}:${PICOCLAW_GID}

EXPOSE 18790

ENTRYPOINT ["tini", "--", "/usr/local/bin/firefly-picoclaw-entrypoint.sh"]
CMD ["picoclaw", "gateway"]
