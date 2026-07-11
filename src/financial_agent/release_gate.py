"""Read-only release identity gate for database writers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .build_info import VERSION
from .schema import LATEST_SCHEMA_VERSION, ensure_app_schema, get_schema_version


class StaleReleaseError(RuntimeError):
    """Raised when a database cannot be verified for this release."""


class IncompatibleSchemaError(RuntimeError):
    """Raised when a database schema does not match this release."""


@dataclass(frozen=True)
class ReleaseStatus:
    status: str
    warning: str | None
    db_version: str | None
    runtime_version: str


def _semantic_version(version: str) -> tuple[int, int, int]:
    parts = version.split(".") if isinstance(version, str) else []
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise StaleReleaseError(f"Invalid release version: {version!r}")
    return tuple(map(int, parts))


def require_current_release_connection(conn: sqlite3.Connection) -> None:
    """Require the connection's release record to match this package version."""

    try:
        row = conn.execute(
            "SELECT version FROM finance_release WHERE id = 1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise StaleReleaseError("Release record could not be read") from exc

    if row is None or row[0] != VERSION:
        raise StaleReleaseError(
            f"Release record does not match running version {VERSION}"
        )
    _semantic_version(row[0])


def require_current_schema_connection(conn: sqlite3.Connection) -> None:
    """Require the connection's schema to match this package version."""

    current = get_schema_version(conn)
    if current != LATEST_SCHEMA_VERSION:
        raise IncompatibleSchemaError(
            f"Schema version {current} does not match latest version "
            f"{LATEST_SCHEMA_VERSION}"
        )


def require_current_release(db_path: str) -> None:
    """Require the database release record to match this package version."""

    try:
        conn = sqlite3.connect(f"{Path(db_path).absolute().as_uri()}?mode=ro", uri=True)
        try:
            require_current_release_connection(conn)
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        raise StaleReleaseError("Release record could not be read") from exc


def promote_release(db_path: str) -> None:
    """Create or advance the authoritative release record to this version."""

    path = Path(db_path).absolute()
    if not path.is_file():
        raise StaleReleaseError(f"Database is not an existing file: {db_path}")

    conn = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_schema = get_schema_version(conn)
        if current_schema > LATEST_SCHEMA_VERSION:
            raise IncompatibleSchemaError(
                f"Schema version {current_schema} is newer than latest version "
                f"{LATEST_SCHEMA_VERSION}"
            )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'finance_release'"
        ).fetchone():
            row = conn.execute(
                "SELECT version FROM finance_release WHERE id = 1"
            ).fetchone()
            if row is not None and _semantic_version(row[0]) > _semantic_version(VERSION):
                raise StaleReleaseError(
                    f"Release {row[0]} is newer than running version {VERSION}"
                )
        ensure_app_schema(conn)
        require_current_schema_connection(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS finance_release (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version TEXT NOT NULL
            )
            """
        )
        runtime_version = _semantic_version(VERSION)
        row = conn.execute(
            "SELECT version FROM finance_release WHERE id = 1"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO finance_release (id, version) VALUES (1, ?)",
                (VERSION,),
            )
        else:
            stored_version = _semantic_version(row[0])
            if stored_version > runtime_version:
                raise StaleReleaseError(
                    f"Release {row[0]} is newer than running version {VERSION}"
                )
            if stored_version < runtime_version:
                conn.execute(
                    "UPDATE finance_release SET version = ? WHERE id = 1",
                    (VERSION,),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def guarded_write(db_path: str):
    """Yield a locked writable connection for the current release and schema."""

    require_current_release(db_path)
    path = Path(db_path).absolute()
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise StaleReleaseError("Database could not be opened for writing") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        require_current_release_connection(conn)
        require_current_schema_connection(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def guarded_read(db_path: str):
    """Yield a read-only connection and its release status."""

    path = Path(db_path).absolute()
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        require_current_schema_connection(conn)
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'finance_release'"
        ).fetchone() is None:
            status = ReleaseStatus(
                "missing_table", "Release table is missing", None, VERSION
            )
        else:
            row = conn.execute(
                "SELECT version FROM finance_release WHERE id = 1"
            ).fetchone()
            if row is None:
                status = ReleaseStatus(
                    "missing_row", "Release row is missing", None, VERSION
                )
            elif row["version"] == VERSION:
                status = ReleaseStatus("current", None, row["version"], VERSION)
            else:
                status = ReleaseStatus(
                    "stale",
                    f"Database release {row['version']!r} does not match runtime {VERSION}",
                    row["version"],
                    VERSION,
                )
    except IncompatibleSchemaError:
        if "conn" in locals():
            conn.close()
        raise
    except (OSError, sqlite3.Error) as exc:
        if "conn" in locals():
            conn.close()
        raise StaleReleaseError("Database could not be opened for reading") from exc

    try:
        yield conn, status
    finally:
        conn.close()
