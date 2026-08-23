from __future__ import annotations

import sqlite3
from pathlib import Path

from invoice_assistant.persistence import create_database_backup, migrate_legacy_data


def test_legacy_data_is_copied_and_operational_paths_are_rewritten(tmp_path):
    source = tmp_path / "package" / "data"
    target = tmp_path / "persistent" / "data"
    managed_file = source / "imports" / "items" / "item_1" / "invoice.pdf"
    managed_file.parent.mkdir(parents=True)
    managed_file.write_bytes(b"invoice")
    database = source / "invoice_assistant.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE attachments (id INTEGER PRIMARY KEY, managed_path TEXT);
        CREATE TABLE reimbursement_batches (id INTEGER PRIMARY KEY, archive_path TEXT, pdf_path TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    connection.execute("INSERT INTO attachments VALUES(1,?)", (str(managed_file.resolve()),))
    connection.execute(
        "INSERT INTO reimbursement_batches VALUES(1,?,?)",
        (str((source / "archives" / "batch").resolve()), str((source / "archives" / "batch.pdf").resolve())),
    )
    connection.execute("INSERT INTO settings VALUES('archive_root',?)", (str((source / "archives").resolve()),))
    connection.commit()
    connection.close()

    assert migrate_legacy_data(source, target) is True
    assert database.is_file()
    assert (target / "imports" / "items" / "item_1" / "invoice.pdf").read_bytes() == b"invoice"

    migrated = sqlite3.connect(target / "invoice_assistant.sqlite3")
    try:
        attachment_path = migrated.execute("SELECT managed_path FROM attachments").fetchone()[0]
        archive_root = migrated.execute("SELECT value FROM settings WHERE key='archive_root'").fetchone()[0]
        assert attachment_path == str((target / "imports" / "items" / "item_1" / "invoice.pdf").resolve())
        assert archive_root == str((target / "archives").resolve())
    finally:
        migrated.close()

    assert migrate_legacy_data(source, target) is False


def test_database_backups_are_valid_and_retained(tmp_path):
    database = tmp_path / "source.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE records(value TEXT)")
    connection.execute("INSERT INTO records VALUES('kept')")
    connection.commit()
    connection.close()

    backup_dir = tmp_path / "backups"
    snapshots = [
        create_database_backup(database, backup_dir, retention=2, manual=True)
        for _ in range(3)
    ]
    remaining = list(backup_dir.glob("*.sqlite3"))
    assert len(remaining) == 2
    assert not snapshots[0].exists()
    restored = sqlite3.connect(remaining[0])
    try:
        assert restored.execute("SELECT value FROM records").fetchone()[0] == "kept"
    finally:
        restored.close()


def test_settings_exposes_storage_and_manual_backup(client, app):
    settings = client.get("/api/settings")
    assert settings.status_code == 200
    assert settings.get_json()["storage"]["data_dir"] == str(Path(app.config["DATA_DIR"]).resolve())

    created = client.post("/api/system/backup", json={})
    assert created.status_code == 200
    storage = created.get_json()["storage"]
    assert storage["backup_count"] == 1
    assert storage["latest_backup"]["name"].startswith("manual-")
