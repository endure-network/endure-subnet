"""Owner-only filesystem setup for file-backed SQLite databases."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy.engine import make_url

SQLITE_DATABASE_MODE = 0o600
SQLITE_PARENT_MODE = 0o700


def sqlite_database_path(url: str | None) -> Path | None:
    """Return the ordinary file path for a SQLite URL, if it has one."""
    if url is None:
        return None
    parsed = make_url(url)
    database = parsed.database
    if parsed.get_backend_name() != "sqlite" or not database:
        return None
    if database == ":memory:":
        return None
    uri_enabled = str(parsed.query.get("uri", "")).lower() == "true"
    if database.startswith("file:") and uri_enabled:
        mode = str(parsed.query.get("mode", "")).lower()
        parsed_uri = urlsplit(database)
        if mode == "memory" or parsed_uri.path == ":memory:":
            return None
        if parsed_uri.netloc not in ("", "localhost"):
            raise ValueError("SQLite file URI must not name a remote authority")
        database = unquote(parsed_uri.path)
        if not database:
            return None
    return Path(database).expanduser()


def ensure_secure_sqlite_path(url: str | None) -> None:
    """Create/tighten a SQLite database before its first connection.

    A newly created immediate parent is owner-only. Existing parents are not
    chmodded because a configured database may live below an operator-managed
    shared mount; the database itself is always opened without following a
    symlink and forced to owner-only permissions. SQLite derives WAL/SHM modes
    from the main database mode.
    """
    database_path = sqlite_database_path(url)
    if database_path is None:
        return
    parsed = make_url(url) if url is not None else None
    query = {} if parsed is None else parsed.query
    mode = str(query.get("mode", "")).lower()
    immutable = str(query.get("immutable", "")).lower() in ("1", "true")

    parent = database_path.parent
    parent_existed = parent.exists()
    parent.mkdir(mode=SQLITE_PARENT_MODE, parents=True, exist_ok=True)
    if not parent_existed:
        parent.chmod(SQLITE_PARENT_MODE)
    if database_path.is_symlink():
        raise ValueError("SQLite database path must not be a symbolic link")

    flags = os.O_RDONLY if immutable or mode == "ro" else os.O_WRONLY | os.O_APPEND
    if not immutable and mode not in ("ro", "rw"):
        flags |= os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(database_path, flags, SQLITE_DATABASE_MODE)
    try:
        os.fchmod(descriptor, SQLITE_DATABASE_MODE)
    finally:
        os.close(descriptor)
