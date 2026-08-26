# Endure miner image serving the Alpha Risk vertical.
# Wallets are mounted at runtime (/root/.bittensor/wallets, read-only);
# miner round state persists under /root/.bittensor/miners (volume) so a
# container restart can still reveal a pre-restart commit.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ARG ENDURE_SOURCE_REVISION="unknown"
ARG ENDURE_SOURCE_URL="https://github.com/endure-network/endure-subnet"
ARG ENDURE_IMAGE_VERSION="dev"

ENV ENDURE_SOURCE_REVISION=$ENDURE_SOURCE_REVISION \
    ENDURE_IMAGE_VERSION=$ENDURE_IMAGE_VERSION

LABEL org.opencontainers.image.revision=$ENDURE_SOURCE_REVISION \
      org.opencontainers.image.source=$ENDURE_SOURCE_URL \
      org.opencontainers.image.version=$ENDURE_IMAGE_VERSION

WORKDIR /app

COPY pyproject.toml uv.lock README.md docker/build-requirements.txt docker/uv-bootstrap-requirements.txt ./

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

VOLUME ["/root/.bittensor"]

ENTRYPOINT ["python", "neurons/miner.py"]
