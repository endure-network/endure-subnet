#!/bin/sh
# Fail a release image build whose identity the runtime would refuse.
# endure/runtime/identity.py rejects any non-dev ENDURE_IMAGE_VERSION unless
# ENDURE_SOURCE_REVISION is a full 40-hex commit and the version is
# sha-<that commit>; catching that here fails the build in seconds instead of
# the first container start. Dev builds (no build args) pass untouched.
set -eu

version="${ENDURE_IMAGE_VERSION:-dev}"
revision="${ENDURE_SOURCE_REVISION:-unknown}"

if [ "$version" = "dev" ]; then
  if [ "$revision" != "unknown" ]; then
    echo "release build refused: ENDURE_SOURCE_REVISION requires the matching ENDURE_IMAGE_VERSION=sha-<commit>; omit both arguments for a dev build" >&2
    exit 1
  fi
  exit 0
fi

if ! printf '%s\n' "$revision" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "release build refused: ENDURE_SOURCE_REVISION must be the full 40-hex commit (got '$revision'); pass --build-arg ENDURE_SOURCE_REVISION=\$(git rev-parse HEAD)" >&2
  exit 1
fi

if [ "$version" != "sha-$revision" ]; then
  echo "release build refused: ENDURE_IMAGE_VERSION must be sha-$revision (got '$version')" >&2
  exit 1
fi
