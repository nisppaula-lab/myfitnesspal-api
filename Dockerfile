# MyFitnessPal REST bridge for Muse.
#
# The upstream MyFitnessPal server uses Camoufox with a virtual display for
# login. This image keeps the browser profile under /home/mcp/.mfp_mcp so a
# Docker volume can preserve it on hosts that support persistent storage.
# Render's free filesystem is ephemeral, so a fresh login may be needed after
# a restart or deploy if MyFitnessPal expires the session.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    DISPLAY=:99 \
    HOME=/home/mcp \
    MFP_HOST=0.0.0.0

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    xvfb \
    openbox \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

COPY pyproject.toml README.md API.md DEPLOY.md UPSTREAM_LICENSE.txt UPSTREAM_LICENSE_NOTE.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e . && \
    python -m playwright install-deps firefox && \
    rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash mcp && \
    mkdir -p /home/mcp/.mfp_mcp/browser_profile /opt/playwright && \
    chown -R mcp:mcp /home/mcp /app /opt/playwright

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER mcp

# Fetch the browser as the runtime user so it lands in /home/mcp's cache,
# where the server actually looks for it. Fetching as root would hide it
# in /root/.cache and force a runtime download at first login.
RUN python -m camoufox fetch

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
