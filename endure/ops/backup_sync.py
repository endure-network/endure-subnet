"""Off-host snapshot sync for the validator database backups.

Uploads the newest file in the backup directory to an S3-compatible bucket
(Cloudflare R2) using stdlib-only SigV4 signing — the runtime image carries
no S3 SDK or curl. Exits nonzero when the newest snapshot is older than the
expected cadence, so a silently skipped snapshot schedule surfaces as a
failed task execution instead of a quietly aging backup set.

Never prints credentials, endpoint URLs, or filesystem paths outside the
backup directory.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib import error as urlerror
from urllib import parse, request

_DEFAULT_BACKUP_DIR = "/data/backups"
_DEFAULT_MAX_AGE_HOURS = 26
_DEFAULT_PREFIX = "validator-db/"
_R2_REGION = "auto"
_S3_SERVICE = "s3"
_URL_SAFE = "-_.~"
_HTTP_NOT_FOUND = 404
_REQUIRED_ENV = (
    "R2_ENDPOINT",
    "R2_BUCKET",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


class BackupSyncError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SyncConfig:
    endpoint: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    backup_dir: Path
    prefix: str
    max_age_hours: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SyncConfig:
        missing = [name for name in _REQUIRED_ENV if not env.get(name)]
        if missing:
            raise BackupSyncError(f"missing required env: {', '.join(missing)}")
        max_age_hours = int(
            env.get("ENDURE_BACKUP_MAX_AGE_HOURS", "") or _DEFAULT_MAX_AGE_HOURS
        )
        if max_age_hours <= 0:
            raise BackupSyncError("ENDURE_BACKUP_MAX_AGE_HOURS must be positive")
        endpoint = env["R2_ENDPOINT"].rstrip("/")
        if not endpoint.startswith("https://"):
            raise BackupSyncError("R2_ENDPOINT must be an https:// URL")
        return cls(
            endpoint=endpoint,
            bucket=env["R2_BUCKET"],
            access_key_id=env["R2_ACCESS_KEY_ID"],
            secret_access_key=env["R2_SECRET_ACCESS_KEY"],
            backup_dir=Path(env.get("ENDURE_BACKUP_DIR", "") or _DEFAULT_BACKUP_DIR),
            prefix=env.get("R2_PREFIX", "") or _DEFAULT_PREFIX,
            max_age_hours=max_age_hours,
        )


def sigv4_headers(  # noqa: PLR0913 — the SigV4 signing inputs are irreducible
    *,
    method: str,
    url: str,
    payload_sha256: str,
    access_key_id: str,
    secret_access_key: str,
    now: datetime,
    region: str = _R2_REGION,
    service: str = _S3_SERVICE,
    extra_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build SigV4 request headers (verified against the AWS S3 test vector)."""
    split = parse.urlsplit(url)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    canonical_uri = parse.quote(split.path or "/", safe="/" + _URL_SAFE)
    query_pairs = sorted(parse.parse_qsl(split.query, keep_blank_values=True))
    canonical_query = "&".join(
        f"{parse.quote(key, safe=_URL_SAFE)}={parse.quote(value, safe=_URL_SAFE)}"
        for key, value in query_pairs
    )
    headers = {
        "host": split.netloc,
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
    } | {name.lower(): value.strip() for name, value in (extra_headers or {}).items()}
    signed_header_names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical_request = "\n".join(
        (
            method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_header_names,
            payload_sha256,
        )
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        )
    )

    def _hmac(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode(), hashlib.sha256).digest()

    signing_key = _hmac(
        _hmac(
            _hmac(_hmac(b"AWS4" + secret_access_key.encode(), date_stamp), region),
            service,
        ),
        "aws4_request",
    )
    signature = hmac.new(
        signing_key, string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_header_names}, Signature={signature}"
    )
    return dict(headers) | {"Authorization": authorization}


def newest_snapshot(backup_dir: Path) -> Path | None:
    try:
        candidates = [path for path in backup_dir.iterdir() if path.is_file()]
    except OSError as error:
        raise BackupSyncError(
            f"cannot read backup dir {backup_dir}: {type(error).__name__}"
        ) from None
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def require_fresh(snapshot: Path, *, max_age_hours: int, now: datetime) -> None:
    age_seconds = now.timestamp() - snapshot.stat().st_mtime
    if age_seconds > max_age_hours * 3600:
        raise BackupSyncError(
            f"newest snapshot {snapshot.name} is {age_seconds / 3600:.1f}h old "
            f"(limit {max_age_hours}h) — the snapshot schedule is not running"
        )


_urlopen = request.urlopen


def _signed_request(  # noqa: PLR0913 — one keyword per request dimension
    config: SyncConfig,
    *,
    method: str,
    key: str,
    payload: bytes | None,
    payload_sha256: str,
    now: datetime,
) -> tuple[int, int | None]:
    url = f"{config.endpoint}/{config.bucket}/{parse.quote(key, safe='/' + _URL_SAFE)}"
    headers = sigv4_headers(
        method=method,
        url=url,
        payload_sha256=payload_sha256,
        access_key_id=config.access_key_id,
        secret_access_key=config.secret_access_key,
        now=now,
    )
    # S310: the scheme is constrained to https:// by SyncConfig.from_env.
    req = request.Request(  # noqa: S310
        url, method=method, headers=headers, data=payload
    )
    try:
        with _urlopen(req, timeout=120) as response:
            length = response.headers.get("Content-Length")
            return int(response.status), None if length is None else int(length)
    except urlerror.HTTPError as error:
        if int(error.code) == _HTTP_NOT_FOUND:
            return _HTTP_NOT_FOUND, None
        raise BackupSyncError(f"{method} failed with HTTP {error.code}") from None
    except urlerror.URLError as error:
        raise BackupSyncError(
            f"{method} failed: {type(error.reason).__name__}"
        ) from None


_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def sync_newest_snapshot(config: SyncConfig, *, now: datetime) -> str:
    snapshot = newest_snapshot(config.backup_dir)
    if snapshot is None:
        raise BackupSyncError(f"no snapshot files in {config.backup_dir}")
    require_fresh(snapshot, max_age_hours=config.max_age_hours, now=now)
    payload = snapshot.read_bytes()
    key = f"{config.prefix}{snapshot.name}"
    status, remote_size = _signed_request(
        config,
        method="HEAD",
        key=key,
        payload=None,
        payload_sha256=_EMPTY_SHA256,
        now=now,
    )
    if status != _HTTP_NOT_FOUND and remote_size == len(payload):
        return f"already synced: {key} ({len(payload)} bytes)"
    status, _ = _signed_request(
        config,
        method="PUT",
        key=key,
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        now=now,
    )
    return f"uploaded: {key} ({len(payload)} bytes, HTTP {status})"


def main() -> int:
    try:
        config = SyncConfig.from_env(os.environ)
        print(sync_newest_snapshot(config, now=datetime.now(UTC)))
    except BackupSyncError as error:
        print(f"backup sync failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
