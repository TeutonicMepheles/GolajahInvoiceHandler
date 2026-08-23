from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import invoice_assistant.db as db_module
from invoice_assistant import create_app
from invoice_assistant.db import MigrationRequiredError, validate_database_schema
from invoice_assistant.migrate import (
    MigrationPreconditionError,
    database_sha256,
    migrate_database,
)
from invoice_assistant.persistence import DataRootBusyError, prepare_data_dir


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_NAME = "invoice_assistant.sqlite3"


def _tree_bytes(root: Path):
    if not root.exists():
        return None
    entries = []
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        relative = path.relative_to(root).as_posix()
        entries.append((relative, "directory" if path.is_dir() else "file", None if path.is_dir() else path.read_bytes()))
    return tuple(entries)


def _app_config(root: Path, database: Path | None = None) -> dict:
    return {
        "TESTING": True,
        "AUTO_BACKUP": False,
        "DATA_DIR": str(root),
        "DATABASE": str(database or root / DATABASE_NAME),
        "IMPORT_DIR": str(root / "imports"),
        "DEFAULT_ARCHIVE_DIR": str(root / "archives"),
        "TEMP_DIR": str(root / "tmp"),
        "TRASH_DIR": str(root / "trash"),
        "BACKUP_DIR": str(root / "backups"),
    }


def _downgrade_v4_to_v3(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        for index in (
            "idx_export_operation_id",
            "idx_items_created_id",
            "idx_items_duplicate_lookup",
            "idx_attachments_sha_item",
            "idx_batches_created_id",
            "idx_agent_operations_status",
            "idx_file_staging_phase",
            "idx_duplicate_sessions_item",
            "idx_duplicate_sessions_expiry",
        ):
            connection.execute(f'DROP INDEX "{index}"')
        connection.execute("DROP TABLE file_operation_staging")
        connection.execute("DROP TABLE duplicate_review_sessions")
        for column in (
            "cleanup_error",
            "outcome",
            "phase",
            "expected_requirements_version",
            "working_batch_version",
            "expected_batch_version",
            "operation_id",
        ):
            connection.execute(f'ALTER TABLE export_operations DROP COLUMN "{column}"')
        connection.execute("DROP TABLE agent_operations")
        connection.execute("DROP TABLE requirements_state")
        connection.execute("ALTER TABLE expense_items DROP COLUMN row_version")
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
    finally:
        connection.close()


def test_fresh_migration_builds_v4_and_current_hash_is_a_byte_stable_no_op(tmp_path):
    root = tmp_path / "fresh-data"
    result = migrate_database(root, expect_no_database=True)
    database = root / DATABASE_NAME

    assert result.status == "migrated"
    assert result.schema_version == 4
    validate_database_schema(database, check_integrity=True)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT id,requirements_version FROM requirements_state"
        ).fetchall() == [(1, 0)]
        item_columns = {row[1] for row in connection.execute("PRAGMA table_info(expense_items)")}
        export_columns = {row[1] for row in connection.execute("PRAGMA table_info(export_operations)")}
        assert "row_version" in item_columns
        assert {
            "operation_id",
            "expected_batch_version",
            "working_batch_version",
            "expected_requirements_version",
            "phase",
            "outcome",
            "cleanup_error",
        } <= export_columns
    finally:
        connection.close()

    expected_hash = database_sha256(database)
    before = _tree_bytes(root)
    replay = migrate_database(root, expect_db_sha256=expected_hash)
    assert replay.status == "no_op"
    assert replay.database_sha256 == expected_hash
    assert _tree_bytes(root) == before


def test_uncheckpointed_wal_fails_closed_and_migration_normalizes_logical_state(tmp_path):
    root = tmp_path / "wal-recovery"
    migrate_database(root, expect_no_database=True)
    database = root / DATABASE_NAME
    clean_hash = database_sha256(database)
    crash_writer = """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute('BEGIN IMMEDIATE')
connection.execute('DROP INDEX idx_export_operation_id')
connection.execute('PRAGMA user_version=3')
connection.execute('COMMIT')
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", crash_writer, str(database)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    wal = Path(f"{database}-wal")
    assert wal.is_file() and wal.stat().st_size > 0
    logical_hash = database_sha256(database)
    assert logical_hash != clean_hash

    with pytest.raises(MigrationRequiredError, match="uncheckpointed SQLite recovery state"):
        validate_database_schema(database)

    result = migrate_database(root, expect_db_sha256=logical_hash)
    assert result.status == "migrated"
    validate_database_schema(database, check_integrity=True)
    assert not wal.exists() or wal.stat().st_size == 0
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_export_operation_id'"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_v3_upgrade_is_additive_and_backfills_versioned_schema(tmp_path):
    root = tmp_path / "old-data"
    migrate_database(root, expect_no_database=True)
    database = root / DATABASE_NAME
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """INSERT INTO expense_items(
                   merchant,expense_date,amount,amount_cents,currency,purpose,status,
                   uncertainties_json,created_at,updated_at
               ) VALUES('旧记录','2026-08-01',12.34,0,'CNY','迁移测试',
                        'pending_confirmation','[]','2026-08-01T00:00:00+00:00','2026-08-01T00:00:00+00:00')"""
        )
        item_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.commit()
    finally:
        connection.close()
    _downgrade_v4_to_v3(database)

    result = migrate_database(root, expect_db_sha256=database_sha256(database))
    assert result.status == "migrated"
    connection = sqlite3.connect(database)
    try:
        item = connection.execute(
            "SELECT amount_cents,row_version FROM expense_items WHERE id=?", (item_id,)
        ).fetchone()
        assert item == (1234, 0)
        assert connection.execute("SELECT requirements_version FROM requirements_state WHERE id=1").fetchone() == (0,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()


def test_migration_expectations_reject_without_touching_data(tmp_path):
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.bin").write_bytes(b"keep")
    before = _tree_bytes(nonempty)
    with pytest.raises(MigrationPreconditionError, match="strictly empty"):
        migrate_database(nonempty, expect_no_database=True)
    assert _tree_bytes(nonempty) == before

    root = tmp_path / "hashed"
    migrate_database(root, expect_no_database=True)
    before = _tree_bytes(root)
    with pytest.raises(MigrationPreconditionError, match="mismatch"):
        migrate_database(root, expect_db_sha256="0" * 64)
    assert _tree_bytes(root) == before


def test_failed_v3_migration_rolls_back_schema_and_bytes(tmp_path, monkeypatch):
    root = tmp_path / "rollback"
    migrate_database(root, expect_no_database=True)
    database = root / DATABASE_NAME
    _downgrade_v4_to_v3(database)
    expected_hash = database_sha256(database)
    before = _tree_bytes(root)
    monkeypatch.setattr(
        db_module,
        "INDEX_STATEMENTS",
        db_module.INDEX_STATEMENTS + ("CREATE INDEX broken migration syntax",),
    )

    with pytest.raises(sqlite3.Error):
        migrate_database(root, expect_db_sha256=expected_hash)

    assert database_sha256(database) == expected_hash
    assert _tree_bytes(root) == before
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        connection.close()

    fresh_root = tmp_path / "fresh-rollback"
    with pytest.raises(sqlite3.Error):
        migrate_database(fresh_root, expect_no_database=True)
    assert not fresh_root.exists()


def test_create_app_rejects_missing_or_old_schema_without_writes(tmp_path):
    missing_root = tmp_path / "missing"
    with pytest.raises(MigrationRequiredError, match="migration_required"):
        create_app(_app_config(missing_root))
    assert not missing_root.exists()

    old_root = tmp_path / "old"
    migrate_database(old_root, expect_no_database=True)
    database = old_root / DATABASE_NAME
    _downgrade_v4_to_v3(database)
    before = _tree_bytes(old_root)
    with pytest.raises(MigrationRequiredError, match="migration_required"):
        create_app(_app_config(old_root))
    assert _tree_bytes(old_root) == before


def test_prepare_data_dir_is_resolution_only(tmp_path):
    target = tmp_path / "not-created"
    resolved, migrated = prepare_data_dir(PROJECT_ROOT, target)
    assert resolved == target.resolve()
    assert migrated is False
    assert not target.exists()


def test_migration_cli_is_independent_and_requires_absolute_data_dir(tmp_path):
    root = tmp_path / "cli-data"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "invoice_assistant.migrate",
            "--data-dir",
            str(root.resolve()),
            "--expect-no-database",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["schema_version"] == 4
    assert (root / DATABASE_NAME).is_file()

    relative = subprocess.run(
        [
            sys.executable,
            "-m",
            "invoice_assistant.migrate",
            "--data-dir",
            "relative-data",
            "--expect-no-database",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert relative.returncode == 2
    assert "absolute path" in relative.stderr
    assert not (PROJECT_ROOT / "relative-data").exists()


def test_migration_refuses_data_root_held_by_service_mutex(tmp_path):
    root = tmp_path / "locked-data"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from invoice_assistant.persistence import DataRootMutex; "
                "mutex=DataRootMutex(sys.argv[1]).acquire(); "
                "print('locked', flush=True); "
                "sys.stdin.readline(); mutex.release()"
            ),
            str(root.resolve()),
        ],
        cwd=PROJECT_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(DataRootBusyError):
            migrate_database(root, expect_no_database=True)
        assert not root.exists()
    finally:
        if holder.stdin is not None:
            holder.stdin.write("\n")
            holder.stdin.flush()
        holder.communicate(timeout=10)


def test_run_rejects_non_fixed_port_before_app_or_log_creation(tmp_path):
    root = tmp_path / "wrong-port-data"
    environment = os.environ.copy()
    environment["INVOICE_APP_PORT"] = "9999"
    environment["INVOICE_APP_DATA_DIR"] = str(root.resolve())
    completed = subprocess.run(
        [sys.executable, "run.py"],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "INVOICE_APP_PORT must be unset or exactly 8765" in completed.stderr
    assert not root.exists()


def test_run_rejects_missing_schema_without_creating_the_data_tree(tmp_path):
    root = tmp_path / "missing-run-data"
    environment = os.environ.copy()
    environment["INVOICE_APP_PORT"] = "8765"
    environment["INVOICE_APP_DATA_DIR"] = str(root.resolve())
    completed = subprocess.run(
        [sys.executable, "run.py"],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "migration_required" in completed.stderr
    assert not root.exists()


def test_run_accepts_migrated_schema_on_the_fixed_port(tmp_path):
    root = tmp_path / "valid-run-data"
    migrate_database(root, expect_no_database=True)
    environment = os.environ.copy()
    environment["INVOICE_APP_PORT"] = "8765"
    environment["INVOICE_APP_DATA_DIR"] = str(root.resolve())
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import run; print(run._service_port, run.app.name)",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "8765 invoice_assistant"
    assert (root / "logs" / "service.log").is_file()


def test_build_app_binds_the_validated_custom_data_root(tmp_path):
    service_root = tmp_path / "service-data"
    custom_root = tmp_path / "custom-data"
    migrate_database(service_root, expect_no_database=True)
    migrate_database(custom_root, expect_no_database=True)
    environment = os.environ.copy()
    environment["INVOICE_APP_PORT"] = "8765"
    environment["INVOICE_APP_DATA_DIR"] = str(service_root.resolve())
    script = (
        "import json,sys; from pathlib import Path; import run; "
        "app=run.build_app(Path(sys.argv[1])); "
        "print(json.dumps({'data_dir': app.config['DATA_DIR'], "
        "'database': app.config['DATABASE']}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(custom_root.resolve())],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    configured = json.loads(completed.stdout)
    assert Path(configured["data_dir"]) == custom_root.resolve()
    assert Path(configured["database"]) == custom_root.resolve() / DATABASE_NAME
    assert (custom_root / "logs" / "service.log").is_file()
