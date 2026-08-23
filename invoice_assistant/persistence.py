from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import uuid
import hashlib
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


DATABASE_NAME = "invoice_assistant.sqlite3"
_backup_lock = threading.Lock()


class DataRootBusyError(RuntimeError):
    pass


def canonical_data_root(data_dir: str | Path) -> Path:
    return Path(data_dir).expanduser().resolve()


def data_root_lock_name(data_dir: str | Path) -> str:
    canonical = str(canonical_data_root(data_dir))
    if os.name == "nt":
        canonical = os.path.normcase(canonical)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"InvoiceAssistant.DataRoot.{digest}"


class DataRootMutex:
    """Cross-process data-root lease without placing a lock file in the data tree."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = canonical_data_root(data_dir)
        self.name = data_root_lock_name(self.data_dir)
        self._handle = None
        self._lock_file = None

    def acquire(self, timeout_ms: int = 0) -> "DataRootMutex":
        if self._handle is not None or self._lock_file is not None:
            return self
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
            kernel32.ReleaseMutex.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL

            # A scheduled task and an interactive/manual launcher can live in
            # different Windows sessions.  A Global named mutex keeps the same
            # data root single-writer across those sessions.
            handle = kernel32.CreateMutexW(None, False, f"Global\\{self.name}")
            if not handle:
                raise OSError(ctypes.get_last_error(), "could not create the data-root mutex")
            result = kernel32.WaitForSingleObject(handle, max(0, int(timeout_ms)))
            if result not in (0x00000000, 0x00000080):  # WAIT_OBJECT_0 / WAIT_ABANDONED
                kernel32.CloseHandle(handle)
                if result == 0x00000102:  # WAIT_TIMEOUT
                    raise DataRootBusyError("the invoice-assistant data root is already in use")
                raise OSError(ctypes.get_last_error(), "could not acquire the data-root mutex")
            self._handle = (kernel32, handle)
            return self

        # The product target is Windows. This fallback keeps tests and development
        # safe on POSIX while still keeping the lock outside the business data tree.
        import fcntl

        lock_root = Path(tempfile.gettempdir()) / "invoice-assistant-mutexes"
        lock_root.mkdir(parents=True, exist_ok=True)
        lock_file = (lock_root / f"{self.name}.lock").open("a+b")
        try:
            flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if timeout_ms == 0 else 0)
            fcntl.flock(lock_file.fileno(), flags)
        except BlockingIOError as exc:
            lock_file.close()
            raise DataRootBusyError("the invoice-assistant data root is already in use") from exc
        self._lock_file = lock_file
        return self

    def release(self) -> None:
        if self._handle is not None:
            kernel32, handle = self._handle
            self._handle = None
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        if self._lock_file is not None:
            import fcntl

            lock_file = self._lock_file
            self._lock_file = None
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def __enter__(self) -> "DataRootMutex":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


@contextmanager
def data_root_mutex(data_dir: str | Path, *, timeout_ms: int = 0):
    mutex = DataRootMutex(data_dir).acquire(timeout_ms=timeout_ms)
    try:
        yield mutex
    finally:
        mutex.release()


def default_data_dir(base_dir: Path) -> Path:
    """Return a data directory that is independent from the application package."""
    configured = os.environ.get("INVOICE_APP_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / "InvoiceAssistant" / "data").resolve()

    return (Path.home() / ".local" / "share" / "invoice-assistant").resolve()


def _relocated_path(value: str | None, source_root: Path, target_root: Path) -> str | None:
    if not value:
        return value
    try:
        relative = Path(value).resolve().relative_to(source_root.resolve())
    except (OSError, ValueError):
        return value
    return str((target_root / relative).resolve())


def _rewrite_operational_paths(database: Path, source_root: Path, target_root: Path) -> None:
    connection = sqlite3.connect(str(database), timeout=30)
    try:
        path_columns = (
            ("attachments", "id", "managed_path", None),
            ("reimbursement_batches", "id", "archive_path", None),
            ("reimbursement_batches", "id", "pdf_path", None),
            ("settings", "key", "value", "archive_root"),
        )
        for table, key_column, value_column, required_key in path_columns:
            query = f"SELECT {key_column}, {value_column} FROM {table}"
            params: tuple[str, ...] = ()
            if required_key is not None:
                query += f" WHERE {key_column}=?"
                params = (required_key,)
            for key, value in connection.execute(query, params).fetchall():
                relocated = _relocated_path(value, source_root, target_root)
                if relocated != value:
                    connection.execute(
                        f"UPDATE {table} SET {value_column}=? WHERE {key_column}=?",
                        (relocated, key),
                    )
        connection.commit()
    finally:
        connection.close()


def migrate_legacy_data(source_root: Path, target_root: Path) -> bool:
    """Copy legacy package-local data once, leaving the original as a safety copy."""
    source_root = source_root.resolve()
    target_root = target_root.resolve()
    source_database = source_root / DATABASE_NAME
    target_database = target_root / DATABASE_NAME
    if source_root == target_root or not source_database.is_file() or target_database.is_file():
        return False

    if target_root.exists():
        if not target_root.is_dir() or any(target_root.iterdir()):
            raise RuntimeError(f"持久数据目录已存在但没有数据库，无法自动迁移：{target_root}")
        target_root.rmdir()

    target_root.parent.mkdir(parents=True, exist_ok=True)
    staging = target_root.parent / f".{target_root.name}.migrating-{uuid.uuid4().hex}"
    ignored_names = {DATABASE_NAME, f"{DATABASE_NAME}-wal", f"{DATABASE_NAME}-shm"}
    try:
        shutil.copytree(
            source_root,
            staging,
            ignore=lambda _directory, names: [name for name in names if name in ignored_names],
        )
        staging_database = staging / DATABASE_NAME
        source = sqlite3.connect(str(source_database), timeout=30)
        destination = sqlite3.connect(str(staging_database), timeout=30)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        _rewrite_operational_paths(staging_database, source_root, target_root)
        os.replace(staging, target_root)
        return True
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def prepare_data_dir(base_dir: Path, requested: str | Path | None = None) -> tuple[Path, bool]:
    """Resolve the data root without creating or migrating anything.

    Legacy copying and directory creation belong to ``invoice_assistant.migrate``.
    The tuple shape is retained for callers that previously consumed the migrated
    flag; startup always observes ``False`` because it is now read-only.
    """
    target = Path(requested).expanduser().resolve() if requested else default_data_dir(base_dir)
    return target, False


def create_database_backup(
    database: str | Path,
    backup_dir: str | Path,
    *,
    retention: int = 30,
    manual: bool = False,
) -> Path:
    database = Path(database).resolve()
    backup_dir = Path(backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    if manual:
        filename = f"manual-{now.strftime('%Y-%m-%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}.sqlite3"
    else:
        filename = f"auto-{now.strftime('%Y-%m-%d')}.sqlite3"
    target = backup_dir / filename

    with _backup_lock:
        if target.is_file():
            return target
        temporary = backup_dir / f".{filename}.{uuid.uuid4().hex}.tmp"
        source = None
        destination = None
        try:
            source = sqlite3.connect(str(database), timeout=30)
            destination = sqlite3.connect(str(temporary), timeout=30)
            source.backup(destination)
            destination.close()
            destination = None
            source.close()
            source = None
            os.replace(temporary, target)
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
            temporary.unlink(missing_ok=True)

        keep = max(1, retention)
        for prefix in ("auto-", "manual-"):
            snapshots = sorted(
                backup_dir.glob(f"{prefix}*.sqlite3"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for expired in snapshots[keep:]:
                expired.unlink(missing_ok=True)
    return target


def list_database_backups(backup_dir: str | Path) -> list[dict]:
    directory = Path(backup_dir).resolve()
    if not directory.is_dir():
        return []
    entries = []
    for path in sorted(directory.glob("*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True):
        stat = path.stat()
        entries.append(
            {
                "name": path.name,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                "kind": "manual" if path.name.startswith("manual-") else "automatic",
            }
        )
    return entries


def storage_status(data_dir: str | Path, database: str | Path, backup_dir: str | Path, retention: int) -> dict:
    database_path = Path(database).resolve()
    backups = list_database_backups(backup_dir)
    return {
        "data_dir": str(Path(data_dir).resolve()),
        "database_size_bytes": database_path.stat().st_size if database_path.is_file() else 0,
        "backup_count": len(backups),
        "latest_backup": backups[0] if backups else None,
        "backup_retention": retention,
    }
