# Endure validator image serving the Alpha Risk vertical.
# Wallets are mounted at runtime (/root/.bittensor/wallets, read-only) and
# the SQLite database lives on the /data volume. Secrets arrive as environment
# variables at runtime; never bake keys or tokens into the image.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ARG ENDURE_SOURCE_REVISION="unknown"
ARG ENDURE_SOURCE_URL="https://github.com/endure-network/endure-subnet"
ARG ENDURE_IMAGE_VERSION="dev"

# PYTHONPATH makes the /app copies win over site-packages, so the process runs
# the checkout layout (`neurons/` beside `endure/`) that content_revision hashes.
ENV ENDURE_SOURCE_REVISION=$ENDURE_SOURCE_REVISION \
    ENDURE_IMAGE_VERSION=$ENDURE_IMAGE_VERSION \
    PYTHONPATH=/app

LABEL org.opencontainers.image.revision=$ENDURE_SOURCE_REVISION \
      org.opencontainers.image.source=$ENDURE_SOURCE_URL \
      org.opencontainers.image.version=$ENDURE_IMAGE_VERSION

WORKDIR /app

# Refuse a release build the runtime identity check would reject, before any
# dependency work happens.
COPY docker/check-release-identity.sh ./check-release-identity.sh
RUN sh check-release-identity.sh && rm check-release-identity.sh

COPY pyproject.toml uv.lock README.md alembic.ini docker/build-requirements.txt docker/uv-bootstrap-requirements.txt ./

RUN python -m venv /opt/uv \
    && /opt/uv/bin/python -m pip install --no-cache-dir --require-hashes --no-deps -r uv-bootstrap-requirements.txt \
    && /opt/uv/bin/uv export --locked --no-dev --no-emit-project --format requirements.txt \
        --output-file requirements.txt \
    && pip install --no-cache-dir --require-hashes -r requirements.txt \
    && pip install --no-cache-dir --require-hashes --no-deps -r build-requirements.txt

COPY endure/ endure/
COPY neurons/ neurons/
COPY typings/ typings/

RUN pip install --no-cache-dir --no-deps --no-build-isolation .

# Pre-create every mount target, including the nested backups mountpoint.
# This keeps nested mount assembly independent of bind-source state and Compose
# rendering behavior.
RUN install -d /data /data/backups /root/.bittensor

VOLUME ["/data", "/root/.bittensor"]

ENTRYPOINT ["python", "neurons/validator.py"]
