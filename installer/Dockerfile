# Wi-Fi Survey server, container image.
#
# Build from the repository root (the build context has to include both
# packages):
#
#   docker build -f installer/Dockerfile -t wifi-survey:local .
#   docker compose -f installer/docker-compose.yml up -d
#
# The entrypoint generates /etc/wifisurvey/config.toml with fresh tokens on
# first boot and prints the viewer token once. Both paths are volumes, so
# the database and the token survive `docker compose down`.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WIFISURVEY_CONFIG=/etc/wifisurvey/config.toml \
    WIFISURVEY_DATA_DIR=/var/lib/wifisurvey

WORKDIR /app

# Only the two installable trees are copied: no tests, no docs, no git.
COPY server/ ./server/
COPY modelkit/ ./modelkit/
COPY installer/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN pip install --upgrade pip \
 && pip install ./server \
 && if [ -f ./modelkit/pyproject.toml ] || [ -f ./modelkit/setup.py ]; then \
        pip install ./modelkit; \
    else \
        echo "modelkit has no packaging metadata: skipped"; \
    fi \
 && chmod +x /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /etc/wifisurvey /var/lib/wifisurvey

# Config (token) and data (SQLite database, imported models) live here.
VOLUME ["/etc/wifisurvey", "/var/lib/wifisurvey"]
EXPOSE 8800

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["wifi-survey", "serve"]
