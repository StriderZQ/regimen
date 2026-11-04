FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# rclone for the off-box copy. It goes in the image rather than on the host: the service
# performs the upload and reports its own failure. Splitting the mechanism between host
# and container is how that report gets lost.
ARG RCLONE_VERSION=v1.75.1
RUN set -eux; \
    apt-get update; apt-get install -y --no-install-recommends curl unzip ca-certificates; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSL -o /tmp/rclone.zip \
      "https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-${arch}.zip"; \
    unzip -q /tmp/rclone.zip -d /tmp; \
    install -m 0755 "/tmp/rclone-${RCLONE_VERSION}-linux-${arch}/rclone" /usr/local/bin/rclone; \
    rm -rf /tmp/rclone.zip "/tmp/rclone-${RCLONE_VERSION}-linux-${arch}"; \
    apt-get purge -y unzip; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*

RUN adduser --system --group --home /srv kag \
    && mkdir -p /data && chown kag:kag /data

COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
# The nutrition reference table and the example programme and rules ship in the image, so
# they can be loaded with one command inside the container.
COPY data ./data
COPY examples ./examples
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
# The restore drill must be runnable where the database lives
COPY scripts ./scripts

RUN pip install --no-cache-dir -e . && chown -R kag:kag /srv

USER kag
VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
