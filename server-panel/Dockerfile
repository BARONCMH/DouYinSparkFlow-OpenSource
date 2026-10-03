FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

LABEL org.opencontainers.image.title="DouYinSparkFlow Multi-user Panel" \
      org.opencontainers.image.source="https://github.com/BARONCMH/DouYinSparkFlow-OpenSource" \
      org.opencontainers.image.description="Standalone multi-user DouYinSparkFlow web panel"

WORKDIR /app
ARG CLOAK_CHROMIUM_VERSION=146.0.7680.177.5
ARG CLOAK_BINARY_SHA256=4a12bcde95fa1bb1beef2b41ab5e5c27c36be78e3be3d0dac8c64d705216670e

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt

# Preinstall the pinned CloakBrowser Chromium and verify the published checksum.
RUN set -eux; \
    tag="chromium-v${CLOAK_CHROMIUM_VERSION}"; \
    url="https://github.com/CloakHQ/CloakBrowser/releases/download/${tag}/cloakbrowser-linux-x64.tar.gz"; \
    curl -fL --connect-timeout 15 --max-time 600 --retry 2 --retry-delay 2 -o /tmp/cb.tar.gz "${url}"; \
    echo "${CLOAK_BINARY_SHA256}  /tmp/cb.tar.gz" | sha256sum -c -; \
    mkdir -p /tmp/cb_extract /opt/cloakbrowser; \
    tar -xzf /tmp/cb.tar.gz -C /tmp/cb_extract; \
    chrome="$(find /tmp/cb_extract -maxdepth 6 -type f -name chrome -perm -u+x | head -n 1)"; \
    test -n "${chrome}"; \
    cp -a "$(dirname "${chrome}")/." /opt/cloakbrowser/; \
    chmod +x /opt/cloakbrowser/chrome; \
    rm -rf /tmp/cb_extract /tmp/cb.tar.gz; \
    test -f /opt/cloakbrowser/icudtl.dat; \
    /opt/cloakbrowser/chrome --version

RUN mkdir -p /app/core /app/utils /app/panel /app/config /app/logs

COPY main.py /app/main.py
COPY core/ /app/core/
COPY utils/ /app/utils/

# Replace the base worker with the multi-user panel worker.
COPY server-panel/tasks.py /app/core/tasks.py
COPY server-panel/panel.py /app/panel/panel.py

ENV PANEL_HOST=0.0.0.0 \
    PANEL_PORT=8080 \
    PYTHONUNBUFFERED=1 \
    GITHUB_ACTIONS=true \
    CLOAKBROWSER_BINARY_PATH=/opt/cloakbrowser/chrome \
    CLOAKBROWSER_AUTO_UPDATE=false

EXPOSE 8080
ENTRYPOINT ["python", "/app/panel/panel.py"]
