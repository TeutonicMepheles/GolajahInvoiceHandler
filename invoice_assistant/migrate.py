from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .db import (
    DatabaseIntegrityError,
    MigrationRequiredError,
    apply_schema_migrations,
    validate_database_schema,
)
from .persistence import DATABASE_NAME, DataRootBusyError, data_root_mutex, migrate_legacy_data


SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")


class MigrationPreconditionError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationResult:
    status: str
    schema_version: int
    database_sha256: str
    legacy_data_copied: bool


def database_sha256(database: str | Path) -> str:
    """Hash the complete committed SQLite state, including recovery logs."""
    database = Path(database).resolve()
    sidecars = [
        candidate
        for suffix in ("-wal", "-journal")
        if (candidate := Path(f"{database}{suffix}")).is_file()
        and candidate.stat().st_size > 0
    ]
    digest = hashlib.sha256()
    if not sidecars:
        # Preserve the original CLI contract for a normalized database: the
        # value is the ordinary SHA-256 of the main file.
        with database.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    digest.update(b"invoice-assistant-sqlite-state-v1\0")
    for candidate in (database, *sidecars):
        label = "main" if candidate == database else candidate.name.removeprefix(database.name)
        digest.update(label.encode("ascii") + b"\0")
        digest.update(candidate.stat().st_size.to_bytes(8, "big"))
        with candidate.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _checkpoint_database_state(database: Path) -> None:
    """Resolve hot journals and checkpoint WAL while the data-root mutex is held."""
    if not database.is_file():
        return
    connection = sqlite3.connect(str(database), timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        # Opening the database and reading the schema performs any required hot
        # rollback-journal recovery before the WAL checkpoint.
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and int(checkpoint[0]) != 0:
            raise MigrationPreconditionError(
                "SQLite WAL checkpoint is busy; stop every database user and retry"
            )
    finally:
        connection.close()
    pending = []
    for suffix in ("-wal", "-journal"):
        candidate = Path(f"{database}{suffix}")
        if candidate.is_file() and candidate.stat().st_size > 0:
            pending.append(candidate.name)
    if pending:
        raise MigrationPreconditionError(
            "SQLite recovery state could not be normalized: " + ",".join(pending)
        )


def _validate_expectation(
    data_dir: Path,
    database: Path,
    *,
    expect_db_sha256: str | None,
    expect_no_database: bool,
) -> None:
    if (expect_db_sha256 is None) == (not expect_no_database):
        raise MigrationPreconditionError(
            "exactly one of expect_db_sha256 or expect_no_database is required"
        )

    if expect_no_database:
        if database.exists():
            raise MigrationPreconditionError("a database already exists; use its current SHA-256 expectation")
        if data_dir.exists():
            if not data_dir.is_dir():
                raise MigrationPreconditionError("the data root exists but is not a directory")
            if any(data_dir.iterdir()):
                raise MigrationPreconditionError(
                    "expect-no-database requires a strictly empty data root"
                )
        return

    assert expect_db_sha256 is not None
    if not SHA256_PATTERN.fullmatch(expect_db_sha256):
        raise MigrationPreconditionError("expect_db_sha256 must be exactly 64 hexadecimal characters")
    if not database.is_file():
        raise MigrationPreconditionError("the expected database does not exist")
    actual = database_sha256(database)
    if actual != expect_db_sha256.lower():
        raise MigrationPreconditionError(
            f"database SHA-256 mismatch (actual {actual})"
        )


def migrate_database(
    data_dir: str | Path,
    *,
    expect_db_sha256: str | None = None,
    expect_no_database: bool = False,
    database_path: str | Path | None = None,
    default_archive_dir: str | Path | None = None,
    legacy_source_root: str | Path | None = None,
) -> MigrationResult:
    """Apply the v4 migration after an explicit state expectation succeeds."""
    root = Path(data_dir).expanduser().resolve()
    database = Path(database_path).expanduser().resolve() if database_path else root / DATABASE_NAME
    archive_dir = Path(default_archive_dir).expanduser().resolve() if default_archive_dir else root / "archives"
    if database.parent != root:
        raise MigrationPreconditionError("database_path must be a direct child of data_dir")

    root_existed = root.exists()
    database_existed = database.exists()
    legacy_copied = False
    with data_root_mutex(root, timeout_ms=0):
        # This is intentionally inside the mutex so the checked bytes remain the
        # bytes on which the migration operates.
        _validate_expectation(
            root,
            database,
            expect_db_sha256=expect_db_sha256,
            expect_no_database=expect_no_database,
        )
        root_existed = root.exists()
        database_existed = database.exists()

        if database_existed:
            _checkpoint_database_state(database)

        if database_existed:
            try:
                validate_database_schema(database, check_integrity=True)
            except MigrationRequiredError:
                pass
            else:
                return MigrationResult(
                    status="no_op",
                    schema_version=4,
                    database_sha256=database_sha256(database),
                    legacy_data_copied=False,
                )

        try:
            if (
                expect_no_database
                and database.name == DATABASE_NAME
                and legacy_source_root is not None
            ):
                legacy_copied = migrate_legacy_data(Path(legacy_source_root), root)
                if legacy_copied:
                    database_existed = True
                    _checkpoint_database_state(database)

            apply_schema_migrations(database, archive_dir)
            _checkpoint_database_state(database)
            validate_database_schema(database, check_integrity=True)
        except Exception:
            if legacy_copied:
                # The source is intentionally left untouched by legacy copying;
                # removal restores the previously strict-empty target state.
                shutil.rmtree(root, ignore_errors=True)
                if root_existed:
                    root.mkdir(parents=True, exist_ok=True)
            elif not database_existed:
                for candidate in (
                    database,
                    Path(f"{database}-journal"),
                    Path(f"{database}-wal"),
                    Path(f"{database}-shm"),
                ):
                    candidate.unlink(missing_ok=True)
                if not root_existed and root.is_dir() and not any(root.iterdir()):
                    root.rmdir()
            raise

        return MigrationResult(
            status="migrated",
            schema_version=4,
            database_sha256=database_sha256(database),
            legacy_data_copied=legacy_copied,
        )


def _absolute_data_dir(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("--data-dir must be an absolute path")
    return path.resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate the Invoice Assistant data schema")
    parser.add_argument("--data-dir", required=True, type=_absolute_data_dir)
    expectation = parser.add_mutually_exclusive_group(required=True)
    expectation.add_argument("--expect-db-sha256")
    expectation.add_argument("--expect-no-database", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(__file__).resolve().parent.parent
    try:
        result = migrate_database(
            args.data_dir,
            expect_db_sha256=args.expect_db_sha256,
            expect_no_database=args.expect_no_database,
            legacy_source_root=project_root / "data",
        )
    except MigrationPreconditionError as exc:
        print(f"migration_precondition_failed: {exc}", file=sys.stderr)
        return 2
    except DataRootBusyError as exc:
        print(f"migration_locked: {exc}", file=sys.stderr)
        return 3
    except (MigrationRequiredError, DatabaseIntegrityError, OSError, sqlite3.Error) as exc:
        print(f"migration_failed: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:
        print(f"migration_failed: {exc}", file=sys.stderr)
        return 4
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
