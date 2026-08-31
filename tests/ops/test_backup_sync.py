"""Backup sync: SigV4 correctness, freshness gate, and upload flow."""

from __future__ import annotations

import hashlib
import io
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib import error as urlerror

import pytest

from endure.ops import backup_sync
from endure.ops.backup_sync import (
    BackupSyncError,
    SyncConfig,
    newest_snapshot,
    require_fresh,
    sigv4_headers,
    sync_newest_snapshot,
)

_NOW = datetime(2026, 8, 31, 4, 0, 0, tzinfo=UTC)
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _config(tmp_path: Path, **overrides: object) -> SyncConfig:
    env: dict[str, str] = {
        "R2_ENDPOINT": "https://account.r2.cloudflarestorage.com",
        "R2_BUCKET": "endure-soak-backups",
        "R2_ACCESS_KEY_ID": "test-key",
        "R2_SECRET_ACCESS_KEY": "test-secret",
        "ENDURE_BACKUP_DIR": str(tmp_path),
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return SyncConfig.from_env(env)


class TestSigV4:
    def test_matches_the_official_aws_s3_test_vector(self) -> None:
        """The GET object example from the AWS SigV4 header-based-auth docs;
        an exact signature match anchors the whole signing chain."""
        headers = sigv4_headers(
            method="GET",
            url="https://examplebucket.s3.amazonaws.com/test.txt",
            payload_sha256=_EMPTY_SHA256,
            access_key_id="AKIAIOSFODNN7EXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            now=datetime(2013, 5, 24, 0, 0, 0, tzinfo=UTC),
            region="us-east-1",
            service="s3",
            extra_headers={"range": "bytes=0-9"},
        )
        assert headers["Authorization"] == (
            "AWS4-HMAC-SHA256 "
            "Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
            "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
            "Signature="
            "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
        )

    def test_signed_extra_headers_are_returned_for_the_wire_request(self) -> None:
        headers = sigv4_headers(
            method="GET",
            url="https://example.com/object",
            payload_sha256=_EMPTY_SHA256,
            access_key_id="k",
            secret_access_key="s",
            now=_NOW,
            extra_headers={"Range": " bytes=0-9 "},
        )
        assert headers["range"] == "bytes=0-9"
        assert "range" in headers["Authorization"]


class TestConfig:
    def test_missing_required_env_names_every_absent_variable(self) -> None:
        with pytest.raises(BackupSyncError, match="R2_BUCKET.*R2_SECRET_ACCESS_KEY"):
            SyncConfig.from_env({"R2_ENDPOINT": "https://x", "R2_ACCESS_KEY_ID": "k"})

    def test_non_positive_max_age_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(BackupSyncError, match="ENDURE_BACKUP_MAX_AGE_HOURS"):
            _config(tmp_path, ENDURE_BACKUP_MAX_AGE_HOURS=0)

    def test_plaintext_endpoint_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(BackupSyncError, match="https://"):
            _config(tmp_path, R2_ENDPOINT="http://account.r2.cloudflarestorage.com")


class TestFreshness:
    def test_newest_snapshot_prefers_latest_mtime_and_ignores_dirs(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "old.db").write_bytes(b"old")
        (tmp_path / "new.db").write_bytes(b"new")
        (tmp_path / "subdir").mkdir()
        os.utime(tmp_path / "old.db", times=(1_000, 1_000))
        os.utime(tmp_path / "new.db", times=(2_000, 2_000))

        found = newest_snapshot(tmp_path)

        assert found is not None
        assert found.name == "new.db"

    def test_empty_backup_dir_yields_none(self, tmp_path: Path) -> None:
        assert newest_snapshot(tmp_path) is None

    def test_missing_backup_dir_fails_cleanly(self, tmp_path: Path) -> None:
        with pytest.raises(BackupSyncError, match="cannot read backup dir"):
            newest_snapshot(tmp_path / "absent")

    def test_stale_snapshot_fails_the_run(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snap.db"
        snapshot.write_bytes(b"data")
        stale = (_NOW - timedelta(hours=27)).timestamp()
        os.utime(snapshot, times=(stale, stale))

        with pytest.raises(BackupSyncError, match="not running"):
            require_fresh(snapshot, max_age_hours=26, now=_NOW)


class _FakeResponse:
    def __init__(self, status: int, length: int | None) -> None:
        self.status = status
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class TestSyncFlow:
    @pytest.fixture
    def snapshot(self, tmp_path: Path) -> Path:
        path = tmp_path / "validator-live-20260831.db"
        path.write_bytes(b"sqlite-bytes")
        fresh = (_NOW - timedelta(hours=1)).timestamp()
        os.utime(path, times=(fresh, fresh))
        return path

    def test_uploads_when_the_object_is_absent(
        self, tmp_path: Path, snapshot: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str, bytes | None]] = []

        def fake_urlopen(req, timeout):  # noqa: ANN001, ANN202
            calls.append((req.get_method(), req.full_url, req.data))
            if req.get_method() == "HEAD":
                raise urlerror.HTTPError(
                    req.full_url, 404, "Not Found", req.headers, io.BytesIO()
                )
            return _FakeResponse(200, None)

        monkeypatch.setattr(backup_sync, "_urlopen", fake_urlopen)

        message = sync_newest_snapshot(_config(tmp_path), now=_NOW)

        assert "uploaded" in message
        assert [method for method, _, _ in calls] == ["HEAD", "PUT"]
        put_method, put_url, put_body = calls[1]
        assert put_url.endswith(
            "/endure-soak-backups/validator-db/validator-live-20260831.db"
        )
        assert put_body == b"sqlite-bytes"

    def test_skips_when_the_object_already_matches(
        self, tmp_path: Path, snapshot: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def fake_urlopen(req, timeout):  # noqa: ANN001, ANN202
            calls.append(req.get_method())
            return _FakeResponse(200, len(b"sqlite-bytes"))

        monkeypatch.setattr(backup_sync, "_urlopen", fake_urlopen)

        message = sync_newest_snapshot(_config(tmp_path), now=_NOW)

        assert "already synced" in message
        assert calls == ["HEAD"]

    def test_http_failures_never_leak_the_endpoint(
        self, tmp_path: Path, snapshot: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_urlopen(req, timeout):  # noqa: ANN001, ANN202
            raise urlerror.HTTPError(
                req.full_url, 403, "Forbidden", req.headers, io.BytesIO()
            )

        monkeypatch.setattr(backup_sync, "_urlopen", fake_urlopen)

        with pytest.raises(BackupSyncError) as failure:
            sync_newest_snapshot(_config(tmp_path), now=_NOW)

        assert "HTTP 403" in str(failure.value)
        assert "cloudflarestorage" not in str(failure.value)
