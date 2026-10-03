ARG PANEL_BASE_IMAGE=ghcr.io/2061360308/douyinsparkflow:3.2.2
FROM ${PANEL_BASE_IMAGE}

LABEL org.opencontainers.image.title="DouYinSparkFlow Multi-user Panel" \
      org.opencontainers.image.source="https://github.com/BARONCMH/DouYinSparkFlow-OpenSource" \
      org.opencontainers.image.description="Standalone multi-user DouYinSparkFlow web panel"

WORKDIR /app
RUN mkdir -p /app/core /app/panel /app/config /app/logs

# The upstream image supplies Python, Playwright, Chromium and the shared runtime.
COPY core/douyin_im.py /app/core/douyin_im.py
COPY server-panel/tasks.py /app/core/tasks.py
COPY server-panel/panel.py /app/panel/panel.py

ENV PANEL_HOST=0.0.0.0 \
    PANEL_PORT=8080 \
    PYTHONUNBUFFERED=1 \
    GITHUB_ACTIONS=true

EXPOSE 8080
ENTRYPOINT ["python", "/app/panel/panel.py"]
