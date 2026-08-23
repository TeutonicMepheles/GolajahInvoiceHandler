from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, current_app, g


SCHEMA_VERSION = 4


class MigrationRequiredError(RuntimeError):
    """Raised when the application is pointed at a missing or stale database."""

    code = "migration_required"

    def __init__(self, detail: str):
        super().__init__(f"migration_required: {detail}")
        self.detail = detail


class DatabaseIntegrityError(RuntimeError):
    pass


# Statements are deliberately kept separate because script-wide execution can
# issue an implicit COMMIT and break all-or-nothing schema migration.
SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        code TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS expense_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        merchant TEXT NOT NULL DEFAULT '',
        expense_date TEXT NOT NULL DEFAULT '',
        amount REAL NOT NULL DEFAULT 0 CHECK(amount >= 0),
        amount_cents INTEGER NOT NULL DEFAULT 0 CHECK(amount_cents >= 0),
        currency TEXT NOT NULL DEFAULT 'CNY',
        converted_amount REAL,
        converted_amount_cents INTEGER CHECK(converted_amount_cents IS NULL OR converted_amount_cents >= 0),
        purpose TEXT NOT NULL DEFAULT '',
        project_id INTEGER REFERENCES projects(id),
        status TEXT NOT NULL CHECK(status IN ('pending_confirmation','pending_reimbursement','in_batch','submitted','reimbursed','merged')),
        ai_raw_json TEXT,
        confirmed_json TEXT,
        uncertainties_json TEXT NOT NULL DEFAULT '[]',
        recognition_error TEXT,
        merged_into_item_id INTEGER REFERENCES expense_items(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        confirmed_at TEXT,
        submitted_at TEXT,
        reimbursed_at TEXT,
        row_version INTEGER NOT NULL DEFAULT 0 CHECK(row_version >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        expense_item_id INTEGER NOT NULL REFERENCES expense_items(id) ON DELETE CASCADE,
        category TEXT NOT NULL CHECK(category IN ('invoice','foreign_invoice','receipt','purchase_list','payment_record','unknown')),
        original_name TEXT NOT NULL,
        normalized_name TEXT NOT NULL,
        managed_path TEXT NOT NULL UNIQUE,
        sha256 TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        ai_raw_json TEXT,
        recognition_error TEXT,
        rename_history_json TEXT NOT NULL DEFAULT '[]',
        name_locked INTEGER NOT NULL DEFAULT 0,
        page_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reimbursement_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        project_id INTEGER REFERENCES projects(id),
        purpose TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK(status IN ('draft','submitted','reimbursed')) DEFAULT 'draft',
        total_amount REAL NOT NULL DEFAULT 0,
        total_amount_cents INTEGER NOT NULL DEFAULT 0 CHECK(total_amount_cents >= 0),
        archive_path TEXT,
        pdf_path TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        export_time TEXT,
        submitted_date TEXT,
        reimbursed_date TEXT,
        reimbursement_notes TEXT NOT NULL DEFAULT '',
        export_token TEXT,
        export_started_at TEXT,
        export_error TEXT,
        row_version INTEGER NOT NULL DEFAULT 0 CHECK(row_version >= 0),
        superseded_archive_path TEXT,
        superseded_pdf_path TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS batch_items (
        batch_id INTEGER NOT NULL REFERENCES reimbursement_batches(id) ON DELETE CASCADE,
        expense_item_id INTEGER NOT NULL UNIQUE REFERENCES expense_items(id),
        sort_order INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(batch_id, expense_item_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS material_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        min_amount REAL NOT NULL DEFAULT 0,
        max_amount REAL,
        required_json TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS material_types (
        code TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        object_type TEXT NOT NULL,
        object_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        details_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_cleanup_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('managed_file','archive_tree','temporary_tree')),
        allowed_root TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK(status IN ('pending','completed')) DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        UNIQUE(path, kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS requirements_state (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        requirements_version INTEGER NOT NULL DEFAULT 0 CHECK(requirements_version >= 0),
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_operations (
        operation_id TEXT PRIMARY KEY,
        operation_name TEXT NOT NULL,
        request_fingerprint TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('in_progress','succeeded','failed')),
        operation_result_json TEXT,
        http_status INTEGER,
        error_code TEXT,
        error_outcome TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_operation_staging (
        operation_id TEXT PRIMARY KEY REFERENCES agent_operations(operation_id) ON DELETE CASCADE,
        tool_name TEXT NOT NULL,
        phase TEXT NOT NULL,
        file_sha256 TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        display_basename TEXT NOT NULL,
        attachment_kind TEXT NOT NULL,
        target_item_id INTEGER,
        expected_item_version INTEGER CHECK(expected_item_version IS NULL OR expected_item_version >= 0),
        expected_batch_version INTEGER CHECK(expected_batch_version IS NULL OR expected_batch_version >= 0),
        managed_file_id TEXT NOT NULL,
        recognition_result_json TEXT,
        recognition_error TEXT,
        resource_type TEXT,
        resource_id INTEGER,
        resource_version INTEGER CHECK(resource_version IS NULL OR resource_version >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS duplicate_review_sessions (
        session_id TEXT PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES expense_items(id) ON DELETE CASCADE,
        item_version INTEGER NOT NULL CHECK(item_version >= 0),
        review_digest TEXT NOT NULL,
        blocking_count INTEGER NOT NULL CHECK(blocking_count >= 0),
        next_offset INTEGER NOT NULL DEFAULT 0 CHECK(next_offset >= 0),
        last_candidate_id INTEGER,
        expires_at TEXT NOT NULL,
        completed_at TEXT,
        consumed_at TEXT,
        overflow_token_hash TEXT,
        allowed_merge_ids_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS export_operations (
        token TEXT PRIMARY KEY,
        batch_id INTEGER NOT NULL REFERENCES reimbursement_batches(id) ON DELETE CASCADE,
        temp_path TEXT NOT NULL,
        final_path TEXT NOT NULL,
        pdf_path TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('preparing','files_ready','completed','failed')),
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        operation_id TEXT REFERENCES agent_operations(operation_id),
        expected_batch_version INTEGER CHECK(expected_batch_version IS NULL OR expected_batch_version >= 0),
        working_batch_version INTEGER CHECK(working_batch_version IS NULL OR working_batch_version >= 0),
        expected_requirements_version INTEGER CHECK(expected_requirements_version IS NULL OR expected_requirements_version >= 0),
        phase TEXT,
        outcome TEXT,
        cleanup_error TEXT
    )
    """,
)


INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_items_status ON expense_items(status)",
    "CREATE INDEX IF NOT EXISTS idx_items_project ON expense_items(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_items_expense_date ON expense_items(expense_date)",
    "CREATE INDEX IF NOT EXISTS idx_items_created_id ON expense_items(created_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_items_duplicate_lookup ON expense_items(currency, amount_cents, expense_date, id)",
    "CREATE INDEX IF NOT EXISTS idx_attachments_item ON attachments(expense_item_id)",
    "CREATE INDEX IF NOT EXISTS idx_attachments_sha_item ON attachments(sha256, expense_item_id)",
    "CREATE INDEX IF NOT EXISTS idx_batches_status ON reimbursement_batches(status)",
    "CREATE INDEX IF NOT EXISTS idx_batches_created_id ON reimbursement_batches(created_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_object ON audit_logs(object_type, object_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_cleanup_status ON file_cleanup_queue(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_operations_status ON agent_operations(status, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_file_staging_phase ON file_operation_staging(phase, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_duplicate_sessions_item ON duplicate_review_sessions(item_id, item_version)",
    "CREATE INDEX IF NOT EXISTS idx_duplicate_sessions_expiry ON duplicate_review_sessions(expires_at, completed_at, consumed_at)",
    "CREATE INDEX IF NOT EXISTS idx_export_batch ON export_operations(batch_id, state)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_export_operation_id ON export_operations(operation_id) WHERE operation_id IS NOT NULL",
)


REQUIRED_COLUMNS = {
    "projects": {"id", "name", "code", "notes", "enabled", "created_at", "updated_at"},
    "expense_items": {
        "id", "merchant", "expense_date", "amount", "amount_cents", "currency", "converted_amount",
        "converted_amount_cents", "purpose", "project_id", "status", "ai_raw_json", "confirmed_json",
        "uncertainties_json", "recognition_error", "merged_into_item_id", "created_at", "updated_at",
        "confirmed_at", "submitted_at", "reimbursed_at", "row_version",
    },
    "attachments": {
        "id", "expense_item_id", "category", "original_name", "normalized_name", "managed_path", "sha256",
        "mime_type", "size_bytes", "ai_raw_json", "recognition_error", "rename_history_json", "name_locked",
        "page_order", "created_at", "updated_at",
    },
    "reimbursement_batches": {
        "id", "name", "project_id", "purpose", "notes", "status", "total_amount", "total_amount_cents",
        "archive_path", "pdf_path", "created_at", "updated_at", "export_time", "submitted_date",
        "reimbursed_date", "reimbursement_notes", "export_token", "export_started_at", "export_error",
        "row_version", "superseded_archive_path", "superseded_pdf_path",
    },
    "batch_items": {"batch_id", "expense_item_id", "sort_order"},
    "material_rules": {"id", "label", "min_amount", "max_amount", "required_json", "sort_order", "updated_at"},
    "material_types": {"code", "label", "updated_at"},
    "settings": {"key", "value", "updated_at"},
    "audit_logs": {"id", "object_type", "object_id", "action", "details_json", "created_at"},
    "file_cleanup_queue": {
        "id", "path", "kind", "allowed_root", "reason", "status", "attempts", "last_error", "created_at",
        "completed_at",
    },
    "requirements_state": {"id", "requirements_version", "updated_at"},
    "agent_operations": {
        "operation_id", "operation_name", "request_fingerprint", "status", "operation_result_json", "http_status",
        "error_code", "error_outcome", "created_at", "updated_at", "completed_at",
    },
    "file_operation_staging": {
        "operation_id", "tool_name", "phase", "file_sha256", "mime_type", "display_basename", "attachment_kind",
        "target_item_id", "expected_item_version", "expected_batch_version", "managed_file_id",
        "recognition_result_json", "recognition_error", "resource_type", "resource_id", "resource_version",
        "created_at", "updated_at",
    },
    "duplicate_review_sessions": {
        "session_id", "item_id", "item_version", "review_digest", "blocking_count", "next_offset",
        "last_candidate_id", "expires_at", "completed_at", "consumed_at", "overflow_token_hash",
        "allowed_merge_ids_json", "created_at", "updated_at",
    },
    "export_operations": {
        "token", "batch_id", "temp_path", "final_path", "pdf_path", "state", "error", "created_at", "updated_at",
        "operation_id", "expected_batch_version", "working_batch_version", "expected_requirements_version", "phase",
        "outcome", "cleanup_error",
    },
}


REQUIRED_INDEXES = {
    "idx_items_status", "idx_items_project", "idx_items_expense_date", "idx_items_created_id",
    "idx_items_duplicate_lookup", "idx_attachments_item", "idx_attachments_sha_item", "idx_batches_status",
    "idx_batches_created_id", "idx_audit_object", "idx_cleanup_status", "idx_agent_operations_status",
    "idx_file_staging_phase", "idx_duplicate_sessions_item", "idx_duplicate_sessions_expiry", "idx_export_batch",
    "idx_export_operation_id",
}


ADDITIVE_COLUMNS = (
    ("attachments", "name_locked", "INTEGER NOT NULL DEFAULT 0"),
    ("attachments", "recognition_error", "TEXT"),
    ("expense_items", "amount_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("expense_items", "converted_amount_cents", "INTEGER"),
    ("expense_items", "row_version", "INTEGER NOT NULL DEFAULT 0 CHECK(row_version >= 0)"),
    ("reimbursement_batches", "total_amount_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("reimbursement_batches", "export_token", "TEXT"),
    ("reimbursement_batches", "export_started_at", "TEXT"),
    ("reimbursement_batches", "export_error", "TEXT"),
    ("reimbursement_batches", "row_version", "INTEGER NOT NULL DEFAULT 0 CHECK(row_version >= 0)"),
    ("reimbursement_batches", "superseded_archive_path", "TEXT"),
    ("reimbursement_batches", "superseded_pdf_path", "TEXT"),
    ("export_operations", "operation_id", "TEXT"),
    ("export_operations", "expected_batch_version", "INTEGER CHECK(expected_batch_version IS NULL OR expected_batch_version >= 0)"),
    ("export_operations", "working_batch_version", "INTEGER CHECK(working_batch_version IS NULL OR working_batch_version >= 0)"),
    ("export_operations", "expected_requirements_version", "INTEGER CHECK(expected_requirements_version IS NULL OR expected_requirements_version >= 0)"),
    ("export_operations", "phase", "TEXT"),
    ("export_operations", "outcome", "TEXT"),
    ("export_operations", "cleanup_error", "TEXT"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_load(value, fallback=None):
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def connect_db(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=10, detect_types=sqlite3.PARSE_DECLTYPES)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _connect_read_only(path: str | Path) -> sqlite3.Connection:
    database = Path(path).resolve()
    if not database.is_file():
        raise MigrationRequiredError("database is missing")
    # immutable=1 prevents even WAL/SHM sidecar creation during startup checks.
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA query_only = ON")
    return connection


def _pending_recovery_sidecars(database: Path) -> list[Path]:
    pending = []
    for suffix in ("-wal", "-journal"):
        candidate = Path(f"{database}{suffix}")
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                pending.append(candidate)
        except OSError as exc:
            raise MigrationRequiredError(
                f"cannot inspect SQLite recovery sidecar {candidate.name}"
            ) from exc
    return pending


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _schema_problems(connection: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        problems.append(f"user_version is {version}, expected {SCHEMA_VERSION}")

    tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for table, expected_columns in REQUIRED_COLUMNS.items():
        if table not in tables:
            problems.append(f"missing table {table}")
            continue
        missing = expected_columns - _table_columns(connection, table)
        if missing:
            problems.append(f"table {table} missing columns {','.join(sorted(missing))}")

    indexes = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    missing_indexes = REQUIRED_INDEXES - indexes
    if missing_indexes:
        problems.append(f"missing indexes {','.join(sorted(missing_indexes))}")
    if "requirements_state" in tables and {
        "id", "requirements_version"
    } <= _table_columns(connection, "requirements_state"):
        requirement_rows = connection.execute(
            "SELECT id,requirements_version FROM requirements_state"
        ).fetchall()
        if (
            len(requirement_rows) != 1
            or requirement_rows[0]["id"] != 1
            or not isinstance(requirement_rows[0]["requirements_version"], int)
            or requirement_rows[0]["requirements_version"] < 0
        ):
            problems.append("requirements_state singleton is missing or invalid")
    return problems


def _assert_integrity(connection: sqlite3.Connection) -> None:
    integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
    if integrity_rows != ["ok"]:
        raise DatabaseIntegrityError("SQLite integrity_check failed: " + "; ".join(integrity_rows[:10]))
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise DatabaseIntegrityError(f"SQLite foreign_key_check found {len(violations)} violation(s)")


def validate_database_schema(path: str | Path, *, check_integrity: bool = False) -> None:
    """Validate a database without creating it, its parent, or SQLite sidecars."""
    database = Path(path).resolve()
    pending = _pending_recovery_sidecars(database)
    if pending:
        names = ",".join(candidate.name for candidate in pending)
        raise MigrationRequiredError(
            f"uncheckpointed SQLite recovery state exists ({names}); run explicit migration"
        )
    connection = _connect_read_only(database)
    try:
        problems = _schema_problems(connection)
        if problems:
            raise MigrationRequiredError("; ".join(problems))
        if check_integrity:
            _assert_integrity(connection)
    finally:
        connection.close()


def database_schema_is_current(path: str | Path, *, check_integrity: bool = False) -> bool:
    try:
        validate_database_schema(path, check_integrity=check_integrity)
    except MigrationRequiredError:
        return False
    return True


def _ensure_column(connection: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _table_columns(connection, table):
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')


def apply_schema_migrations(database: str | Path, default_archive_dir: str | Path) -> None:
    """Create/upgrade the schema in one explicit write transaction."""
    database_path = Path(database).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database_path), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version > SCHEMA_VERSION:
            raise MigrationRequiredError(
                f"database user_version {current_version} is newer than supported version {SCHEMA_VERSION}"
            )

        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            for table, name, definition in ADDITIVE_COLUMNS:
                _ensure_column(connection, table, name, definition)

            connection.execute(
                "UPDATE expense_items SET amount_cents=CAST(ROUND(amount*100) AS INTEGER) "
                "WHERE amount_cents=0 AND amount<>0"
            )
            connection.execute(
                "UPDATE expense_items SET converted_amount_cents=CAST(ROUND(converted_amount*100) AS INTEGER) "
                "WHERE converted_amount IS NOT NULL AND converted_amount_cents IS NULL"
            )
            connection.execute(
                "UPDATE reimbursement_batches SET total_amount_cents=CAST(ROUND(total_amount*100) AS INTEGER) "
                "WHERE total_amount_cents=0 AND total_amount<>0"
            )

            now = utc_now()
            if not connection.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
                connection.execute(
                    "INSERT INTO projects(name, code, notes, enabled, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                    ("默认报销项目", "DEFAULT", "可在设置中修改或新增项目", 1, now, now),
                )
            if not connection.execute("SELECT 1 FROM material_rules LIMIT 1").fetchone():
                rules = (
                    ("低于 500 元", 0, 500, ["primary_receipt"], 0),
                    ("500 元至 1,000 元", 500, 1000, ["primary_receipt"], 1),
                    ("1,000 元及以上", 1000, None, ["primary_receipt", "purchase_list", "payment_record"], 2),
                )
                connection.executemany(
                    "INSERT INTO material_rules(label,min_amount,max_amount,required_json,sort_order,updated_at) VALUES(?,?,?,?,?,?)",
                    [(label, low, high, json_dump(required), order, now) for label, low, high, required, order in rules],
                )
            connection.executemany(
                "INSERT OR IGNORE INTO material_types(code,label,updated_at) VALUES(?,?,?)",
                (
                    ("primary_receipt", "主凭据", now),
                    ("purchase_list", "购入清单", now),
                    ("payment_record", "支付记录", now),
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES('archive_root',?,?)",
                (str(Path(default_archive_dir).resolve()), now),
            )
            connection.execute(
                "INSERT OR IGNORE INTO requirements_state(id,requirements_version,updated_at) VALUES(1,0,?)",
                (now,),
            )
            for statement in INDEX_STATEMENTS:
                connection.execute(statement)

            # user_version is the final write in the migration transaction.
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            problems = _schema_problems(connection)
            if problems:
                raise MigrationRequiredError("; ".join(problems))
            _assert_integrity(connection)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect_db(current_app.config["DATABASE"])
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def transaction(db: sqlite3.Connection | None = None):
    db = db or get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def init_db(app: Flask) -> None:
    """Compatibility entry point: startup validation only, never migration."""
    validate_database_schema(app.config["DATABASE"])


def audit(db: sqlite3.Connection, object_type: str, object_id: int, action: str, details: dict | None = None):
    db.execute(
        "INSERT INTO audit_logs(object_type,object_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
        (object_type, object_id, action, json_dump(details or {}), utc_now()),
    )
