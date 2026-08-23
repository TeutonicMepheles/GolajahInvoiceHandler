from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

import invoice_assistant.batch_service as batch_service
import invoice_assistant.storage as storage
from invoice_assistant import AppError
from invoice_assistant.db import connect_db, get_db, transaction, utc_now
from invoice_assistant.idempotency import ReplayedOperationFailure, request_fingerprint


def _seed_exportable_batch(app, *, name: str = "精确确认报销包") -> dict:
    with app.app_context():
        db = get_db()
        source = Path(app.config["IMPORT_DIR"]) / "blobs" / "source.png"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"managed invoice")
        now = utc_now()
        with transaction(db):
            item_id = db.execute(
                """INSERT INTO expense_items(
                       merchant,expense_date,amount,amount_cents,currency,purpose,project_id,status,
                       uncertainties_json,created_at,updated_at,row_version
                   ) VALUES('导出测试商户','2026-08-23',128.5,12850,'CNY','导出状态机',1,
                            'in_batch','[]',?,?,7)""",
                (now, now),
            ).lastrowid
            db.execute(
                """INSERT INTO attachments(
                       expense_item_id,category,original_name,normalized_name,managed_path,sha256,
                       mime_type,size_bytes,rename_history_json,name_locked,page_order,created_at,updated_at
                   ) VALUES(?, 'invoice','source.png','invoice.png',?,'test-sha','image/png',15,
                            '[]',1,0,?,?)""",
                (item_id, str(source.resolve()), now, now),
            )
            batch_id = db.execute(
                """INSERT INTO reimbursement_batches(
                       name,project_id,purpose,notes,status,total_amount,total_amount_cents,
                       created_at,updated_at,row_version
                   ) VALUES(?,1,'导出状态机','', 'draft',128.5,12850,?,?,5)""",
                (name, now, now),
            ).lastrowid
            db.execute(
                "INSERT INTO batch_items(batch_id,expense_item_id,sort_order) VALUES(?,?,0)",
                (batch_id, item_id),
            )
        return {
            "batch_id": batch_id,
            "item_id": item_id,
            "name": name,
            "batch_version": 5,
            "item_version": 7,
            "requirements_version": 0,
        }


def _parameters(seed: dict) -> dict:
    return {
        "batch_id": seed["batch_id"],
        "expected_version": seed["batch_version"],
        "expected_requirements_version": seed["requirements_version"],
        "confirmation_name": seed["name"],
    }


def _operation(seed: dict) -> tuple[str, str]:
    operation_id = str(uuid.uuid4())
    return operation_id, request_fingerprint(batch_service.EXPORT_OPERATION_NAME, _parameters(seed))


def _fake_pdf(expected_working_version: int | None = None):
    def generate(batch, items, archive_files, output_path):
        if expected_working_version is not None:
            assert batch["version"] == expected_working_version
        output_path.write_bytes(b"%PDF-1.4\nexport-test\n")
        return {"pages": 1, "attachments": len(archive_files)}

    return generate


def test_export_reserves_versions_then_commits_business_and_ledger_together(app, monkeypatch):
    seed = _seed_exportable_batch(app)
    operation_id, fingerprint = _operation(seed)
    monkeypatch.setattr(
        batch_service,
        "generate_material_package",
        _fake_pdf(seed["batch_version"] + 1),
    )

    with app.app_context():
        db = get_db()
        result = batch_service.export_batch(
            db,
            seed["batch_id"],
            expected_version=seed["batch_version"],
            expected_requirements_version=seed["requirements_version"],
            confirmation_name=seed["name"],
            operation_id=operation_id,
            request_fingerprint=fingerprint,
        )
        assert result["meta"] == {"replayed": False}
        assert result["operation_result"]["artifact_available"] is True
        assert result["batch"]["status"] == "submitted"
        assert result["batch"]["version"] == seed["batch_version"] + 2
        assert Path(result["batch"]["pdf_path"]).is_file()

        item = db.execute(
            "SELECT status,row_version FROM expense_items WHERE id=?", (seed["item_id"],)
        ).fetchone()
        assert (item["status"], item["row_version"]) == (
            "submitted",
            seed["item_version"] + 1,
        )
        export = db.execute(
            "SELECT * FROM export_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        assert export["expected_batch_version"] == seed["batch_version"]
        assert export["working_batch_version"] == seed["batch_version"] + 1
        assert export["expected_requirements_version"] == seed["requirements_version"]
        assert (export["state"], export["phase"], export["outcome"]) == (
            "completed",
            "completed",
            "applied",
        )
        ledger = db.execute(
            "SELECT * FROM agent_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        assert (ledger["status"], ledger["error_outcome"]) == ("succeeded", None)

        replay = batch_service.export_batch(
            db,
            seed["batch_id"],
            expected_version=seed["batch_version"],
            expected_requirements_version=seed["requirements_version"],
            confirmation_name=seed["name"],
            operation_id=operation_id,
            request_fingerprint=fingerprint,
        )
        assert replay == {
            "operation_result": result["operation_result"],
            "meta": {"replayed": True},
        }
        archive_root = Path(app.config["DEFAULT_ARCHIVE_DIR"])
        assert len([path for path in archive_root.iterdir() if path.is_dir()]) == 1


def test_exact_confirmation_failure_is_zero_side_effect_and_replays_failure(app):
    seed = _seed_exportable_batch(app)
    operation_id = str(uuid.uuid4())
    parameters = {**_parameters(seed), "confirmation_name": f" {seed['name']}"}
    fingerprint = request_fingerprint(batch_service.EXPORT_OPERATION_NAME, parameters)

    with app.app_context():
        db = get_db()
        with pytest.raises(AppError) as caught:
            batch_service.export_batch(
                db,
                seed["batch_id"],
                expected_version=seed["batch_version"],
                expected_requirements_version=seed["requirements_version"],
                confirmation_name=parameters["confirmation_name"],
                operation_id=operation_id,
                request_fingerprint=fingerprint,
            )
        assert caught.value.code == "batch_confirmation_mismatch"
        batch = db.execute(
            "SELECT row_version,export_token FROM reimbursement_batches WHERE id=?",
            (seed["batch_id"],),
        ).fetchone()
        assert (batch["row_version"], batch["export_token"]) == (
            seed["batch_version"],
            None,
        )
        assert not db.execute("SELECT 1 FROM export_operations").fetchone()
        ledger = db.execute(
            "SELECT status,error_code,error_outcome FROM agent_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(ledger) == (
            "failed",
            "batch_confirmation_mismatch",
            "not_applied",
        )
        with pytest.raises(ReplayedOperationFailure):
            batch_service.export_batch(
                db,
                seed["batch_id"],
                expected_version=seed["batch_version"],
                expected_requirements_version=seed["requirements_version"],
                confirmation_name=parameters["confirmation_name"],
                operation_id=operation_id,
                request_fingerprint=fingerprint,
            )


def test_export_route_prevalidation_failure_is_durable_and_parameter_bound(app):
    seed = _seed_exportable_batch(app)
    operation_id = str(uuid.uuid4())
    headers = {
        "Idempotency-Key": operation_id,
        "X-Invoice-Agent-Tool": batch_service.EXPORT_OPERATION_NAME,
    }
    invalid = {
        "expected_version": seed["batch_version"],
        "confirmation_name": seed["name"],
    }
    client = app.test_client()
    first = client.post(
        f"/api/batches/{seed['batch_id']}/export", json=invalid, headers=headers
    )
    assert first.status_code == 400
    assert first.get_json()["error"] == "requirements_version_required"
    replay = client.post(
        f"/api/batches/{seed['batch_id']}/export", json=invalid, headers=headers
    )
    assert replay.status_code == 400
    assert replay.get_json()["meta"] == {"replayed": True}
    changed = client.post(
        f"/api/batches/{seed['batch_id']}/export",
        json={**invalid, "expected_requirements_version": seed["requirements_version"]},
        headers=headers,
    )
    assert changed.status_code == 409
    assert changed.get_json()["error"] == "idempotency_mismatch"
    operation = client.get(f"/api/agent-operations/{operation_id}").get_json()["operation"]
    assert (
        operation["status"],
        operation["http_status"],
        operation["error_code"],
        operation["outcome"],
    ) == ("failed", 400, "requirements_version_required", "not_applied")


def test_response_loss_after_commit_replays_snapshot_without_second_archive(app, monkeypatch):
    seed = _seed_exportable_batch(app)
    operation_id, fingerprint = _operation(seed)
    monkeypatch.setattr(batch_service, "generate_material_package", _fake_pdf())
    real_response = batch_service._operation_response

    def lose_first_response(payload, safe_result, *, replayed):
        response = real_response(payload, safe_result, replayed=replayed)
        if not replayed:
            raise ConnectionError("injected response loss")
        return response

    monkeypatch.setattr(batch_service, "_operation_response", lose_first_response)
    with app.app_context():
        db = get_db()
        with pytest.raises(ConnectionError, match="response loss"):
            batch_service.export_batch(
                db,
                seed["batch_id"],
                expected_version=seed["batch_version"],
                expected_requirements_version=seed["requirements_version"],
                confirmation_name=seed["name"],
                operation_id=operation_id,
                request_fingerprint=fingerprint,
            )
        ledger = db.execute(
            "SELECT status,operation_result_json FROM agent_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert ledger["status"] == "succeeded" and ledger["operation_result_json"]

    monkeypatch.setattr(batch_service, "_operation_response", real_response)
    with app.app_context():
        replay = batch_service.export_batch(
            get_db(),
            seed["batch_id"],
            expected_version=seed["batch_version"],
            expected_requirements_version=seed["requirements_version"],
            confirmation_name=seed["name"],
            operation_id=operation_id,
            request_fingerprint=fingerprint,
        )
        assert replay["meta"] == {"replayed": True}
        assert replay["operation_result"]["artifact_available"] is True
        archive_root = Path(app.config["DEFAULT_ARCHIVE_DIR"])
        assert len([path for path in archive_root.iterdir() if path.is_dir()]) == 1


def test_requirements_change_during_build_releases_reservation_without_archive(app, monkeypatch):
    seed = _seed_exportable_batch(app)
    operation_id, fingerprint = _operation(seed)

    def change_requirements(batch, items, archive_files, output_path):
        output_path.write_bytes(b"%PDF-1.4\nchanged-rules\n")
        other = connect_db(app.config["DATABASE"])
        try:
            with transaction(other):
                other.execute(
                    "UPDATE requirements_state SET requirements_version=requirements_version+1,updated_at=? WHERE id=1",
                    (utc_now(),),
                )
        finally:
            other.close()
        return {"pages": 1}

    monkeypatch.setattr(batch_service, "generate_material_package", change_requirements)
    with app.app_context():
        db = get_db()
        with pytest.raises(AppError) as caught:
            batch_service.export_batch(
                db,
                seed["batch_id"],
                expected_version=seed["batch_version"],
                expected_requirements_version=seed["requirements_version"],
                confirmation_name=seed["name"],
                operation_id=operation_id,
                request_fingerprint=fingerprint,
            )
        assert caught.value.code == "stale_requirements_version"
        batch = db.execute(
            "SELECT status,row_version,export_token,archive_path FROM reimbursement_batches WHERE id=?",
            (seed["batch_id"],),
        ).fetchone()
        assert tuple(batch) == ("draft", seed["batch_version"] + 2, None, None)
        export = db.execute(
            "SELECT state,phase,outcome FROM export_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(export) == ("failed", "failed", "not_applied")
        ledger = db.execute(
            "SELECT status,error_code,error_outcome FROM agent_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(ledger) == ("failed", "stale_requirements_version", "not_applied")
        archive_root = Path(app.config["DEFAULT_ARCHIVE_DIR"])
        assert not any(archive_root.iterdir())


def test_preexisting_publish_destination_is_never_overwritten_or_cleaned(app, monkeypatch):
    seed = _seed_exportable_batch(app)
    operation_id, fingerprint = _operation(seed)
    sentinel = b"external-content"

    def create_destination(batch, items, archive_files, output_path):
        output_path.write_bytes(b"%PDF-1.4\npreexisting-destination\n")
        other = connect_db(app.config["DATABASE"])
        try:
            row = other.execute(
                "SELECT final_path FROM export_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        finally:
            other.close()
        destination = Path(row["final_path"])
        destination.mkdir(parents=True)
        (destination / "sentinel.bin").write_bytes(sentinel)
        return {"pages": 1}

    monkeypatch.setattr(batch_service, "generate_material_package", create_destination)
    with app.app_context():
        db = get_db()
        with pytest.raises(AppError) as caught:
            batch_service.export_batch(
                db,
                seed["batch_id"],
                expected_version=seed["batch_version"],
                expected_requirements_version=seed["requirements_version"],
                confirmation_name=seed["name"],
                operation_id=operation_id,
                request_fingerprint=fingerprint,
            )
        assert caught.value.code == "export_destination_exists"
        export = db.execute(
            "SELECT final_path,phase,outcome FROM export_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert Path(export["final_path"], "sentinel.bin").read_bytes() == sentinel
        assert (export["phase"], export["outcome"]) == ("failed", "not_applied")
        batch = db.execute(
            "SELECT row_version,export_token FROM reimbursement_batches WHERE id=?",
            (seed["batch_id"],),
        ).fetchone()
        assert tuple(batch) == (seed["batch_version"] + 2, None)


def test_published_cleanup_failure_stays_unknown_until_recovery_cleans_it(app, monkeypatch):
    seed = _seed_exportable_batch(app)
    operation_id, fingerprint = _operation(seed)
    monkeypatch.setattr(batch_service, "generate_material_package", _fake_pdf())
    real_publish = batch_service._atomic_publish_export
    real_rmtree = batch_service.shutil.rmtree

    def publish_then_change_rules(source, target, token):
        result = real_publish(source, target, token)
        other = connect_db(app.config["DATABASE"])
        try:
            with transaction(other):
                other.execute(
                    "UPDATE requirements_state SET requirements_version=requirements_version+1,updated_at=? WHERE id=1",
                    (utc_now(),),
                )
        finally:
            other.close()
        return result

    def fail_archive_cleanup(path, *args, **kwargs):
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(batch_service, "_atomic_publish_export", publish_then_change_rules)
    monkeypatch.setattr(batch_service.shutil, "rmtree", fail_archive_cleanup)

    with app.app_context():
        db = get_db()
        with pytest.raises(AppError) as caught:
            batch_service.export_batch(
                db,
                seed["batch_id"],
                expected_version=seed["batch_version"],
                expected_requirements_version=seed["requirements_version"],
                confirmation_name=seed["name"],
                operation_id=operation_id,
                request_fingerprint=fingerprint,
            )
        assert caught.value.code == "export_cleanup_pending"
        batch = db.execute(
            "SELECT status,row_version,export_token,archive_path,pdf_path FROM reimbursement_batches WHERE id=?",
            (seed["batch_id"],),
        ).fetchone()
        assert batch["status"] == "draft"
        assert batch["row_version"] == seed["batch_version"] + 1
        assert batch["export_token"]
        assert batch["archive_path"] is None and batch["pdf_path"] is None
        export = db.execute(
            "SELECT * FROM export_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        assert (export["state"], export["phase"], export["outcome"]) == (
            "failed",
            "cleanup_pending",
            "unknown",
        )
        assert Path(export["final_path"]).is_dir()
        ledger = db.execute(
            "SELECT status,operation_result_json,error_code,error_outcome FROM agent_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(ledger) == (
            "failed",
            None,
            "export_cleanup_pending",
            "unknown",
        )

    monkeypatch.setattr(batch_service.shutil, "rmtree", real_rmtree)
    with app.app_context():
        report = batch_service.recover_incomplete_exports(app)
        assert report == {"recovered": 0, "rolled_back": 1, "cleanup_pending": 0}
        db = get_db()
        batch = db.execute(
            "SELECT row_version,export_token,archive_path FROM reimbursement_batches WHERE id=?",
            (seed["batch_id"],),
        ).fetchone()
        assert tuple(batch) == (seed["batch_version"] + 2, None, None)
        export = db.execute(
            "SELECT phase,outcome FROM export_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        assert tuple(export) == ("failed", "not_applied")
        ledger = db.execute(
            "SELECT status,error_outcome,operation_result_json FROM agent_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(ledger) == ("failed", "not_applied", None)


def test_publish_race_never_deletes_external_destination(app, monkeypatch):
    seed = _seed_exportable_batch(app)
    operation_id, fingerprint = _operation(seed)
    monkeypatch.setattr(batch_service, "generate_material_package", _fake_pdf())
    real_rename = batch_service.os.rename
    sentinel = b"concurrent-external-content"

    def external_creator_wins(source, target):
        destination = Path(target)
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "sentinel.bin").write_bytes(sentinel)
        return real_rename(source, target)

    monkeypatch.setattr(batch_service.os, "rename", external_creator_wins)
    with app.app_context():
        db = get_db()
        with pytest.raises(AppError) as caught:
            batch_service.export_batch(
                db,
                seed["batch_id"],
                expected_version=seed["batch_version"],
                expected_requirements_version=seed["requirements_version"],
                confirmation_name=seed["name"],
                operation_id=operation_id,
                request_fingerprint=fingerprint,
            )
        assert caught.value.code == "export_destination_exists"
        operation = db.execute(
            "SELECT final_path,phase,outcome FROM export_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        final_dir = Path(operation["final_path"])
        assert (final_dir / "sentinel.bin").read_bytes() == sentinel
        assert (operation["phase"], operation["outcome"]) == ("failed", "not_applied")
        assert not batch_service._publishing_path(operation).exists()
        batch = db.execute(
            "SELECT row_version,export_token FROM reimbursement_batches WHERE id=?",
            (seed["batch_id"],),
        ).fetchone()
        assert tuple(batch) == (seed["batch_version"] + 2, None)


def test_files_ready_crash_recovers_only_after_rechecking_versions_and_completeness(app, monkeypatch):
    seed = _seed_exportable_batch(app)
    operation_id, fingerprint = _operation(seed)
    monkeypatch.setattr(batch_service, "generate_material_package", _fake_pdf())
    real_finalize = batch_service._finalize_export

    class SimulatedCrash(BaseException):
        pass

    def crash_before_final_transaction(*args, **kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(batch_service, "_finalize_export", crash_before_final_transaction)
    with app.app_context():
        db = get_db()
        with pytest.raises(SimulatedCrash):
            batch_service.export_batch(
                db,
                seed["batch_id"],
                expected_version=seed["batch_version"],
                expected_requirements_version=seed["requirements_version"],
                confirmation_name=seed["name"],
                operation_id=operation_id,
                request_fingerprint=fingerprint,
            )
        export = db.execute(
            "SELECT state,phase FROM export_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        assert tuple(export) == ("files_ready", "files_ready")
        batch = db.execute(
            "SELECT status,row_version,export_token FROM reimbursement_batches WHERE id=?",
            (seed["batch_id"],),
        ).fetchone()
        assert batch["status"] == "draft"
        assert batch["row_version"] == seed["batch_version"] + 1
        assert batch["export_token"]

    monkeypatch.setattr(batch_service, "_finalize_export", real_finalize)
    with app.app_context():
        report = batch_service.recover_incomplete_exports(app)
        assert report == {"recovered": 1, "rolled_back": 0, "cleanup_pending": 0}
        db = get_db()
        batch = db.execute(
            "SELECT status,row_version,export_token,pdf_path FROM reimbursement_batches WHERE id=?",
            (seed["batch_id"],),
        ).fetchone()
        assert (batch["status"], batch["row_version"], batch["export_token"]) == (
            "submitted",
            seed["batch_version"] + 2,
            None,
        )
        assert Path(batch["pdf_path"]).is_file()
        item = db.execute(
            "SELECT status,row_version FROM expense_items WHERE id=?", (seed["item_id"],)
        ).fetchone()
        assert tuple(item) == ("submitted", seed["item_version"] + 1)
        ledger = db.execute(
            "SELECT status,operation_result_json,error_outcome FROM agent_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert ledger["status"] == "succeeded"
        assert ledger["operation_result_json"]
        assert ledger["error_outcome"] is None


def test_attachment_copy_uses_native_long_path_boundaries(app, monkeypatch):
    seed = _seed_exportable_batch(app)
    operation_id, fingerprint = _operation(seed)
    monkeypatch.setattr(batch_service, "generate_material_package", _fake_pdf())
    real_copy2 = batch_service.shutil.copy2
    copies: list[tuple[str, str]] = []

    def observe_copy(source, target, *args, **kwargs):
        copies.append((str(source), str(target)))
        return real_copy2(source, target, *args, **kwargs)

    monkeypatch.setattr(batch_service.shutil, "copy2", observe_copy)
    with app.app_context():
        result = batch_service.export_batch(
            get_db(),
            seed["batch_id"],
            expected_version=seed["batch_version"],
            expected_requirements_version=seed["requirements_version"],
            confirmation_name=seed["name"],
            operation_id=operation_id,
            request_fingerprint=fingerprint,
        )

    assert result["batch"]["status"] == "submitted"
    material_copy = next(entry for entry in copies if "原始材料" in entry[1])
    if os.name == "nt":
        assert material_copy[0].startswith("\\\\?\\")
        assert material_copy[1].startswith("\\\\?\\")


def test_raw_export_oserror_is_stable_on_first_response_and_replay(app, monkeypatch):
    seed = _seed_exportable_batch(app)
    operation_id = str(uuid.uuid4())
    headers = {
        "Idempotency-Key": operation_id,
        "X-Invoice-Agent-Tool": batch_service.EXPORT_OPERATION_NAME,
    }
    payload = {
        "expected_version": seed["batch_version"],
        "expected_requirements_version": seed["requirements_version"],
        "confirmation_name": seed["name"],
    }

    def fail_pdf(*_args, **_kwargs):
        raise OSError(r"C:\private\invoice-source.png")

    monkeypatch.setattr(batch_service, "generate_material_package", fail_pdf)
    client = app.test_client()
    first = client.post(
        f"/api/batches/{seed['batch_id']}/export", json=payload, headers=headers
    )
    replay = client.post(
        f"/api/batches/{seed['batch_id']}/export", json=payload, headers=headers
    )

    assert first.status_code == replay.status_code == 500
    assert first.get_json() == {
        "error": "export_failed",
        "message": "导出失败，未提交报销包。",
        "outcome": "not_applied",
    }
    assert replay.get_json() == {
        **first.get_json(),
        "meta": {"replayed": True},
    }
    assert "private" not in first.get_json()["message"]


def test_startup_cleans_completed_export_work_tree_after_commit_crash(app, monkeypatch):
    seed = _seed_exportable_batch(app)
    operation_id, fingerprint = _operation(seed)
    monkeypatch.setattr(batch_service, "generate_material_package", _fake_pdf())
    real_cleanup = batch_service._cleanup_operation_paths

    class SimulatedCrash(BaseException):
        pass

    def crash_before_work_cleanup(operation, *, include_final):
        if operation["state"] == "completed":
            raise SimulatedCrash()
        return real_cleanup(operation, include_final=include_final)

    monkeypatch.setattr(batch_service, "_cleanup_operation_paths", crash_before_work_cleanup)
    with app.app_context(), pytest.raises(SimulatedCrash):
        batch_service.export_batch(
            get_db(),
            seed["batch_id"],
            expected_version=seed["batch_version"],
            expected_requirements_version=seed["requirements_version"],
            confirmation_name=seed["name"],
            operation_id=operation_id,
            request_fingerprint=fingerprint,
        )

    with app.app_context():
        operation = get_db().execute(
            "SELECT * FROM export_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        temp_path = Path(operation["temp_path"])
        final_path = Path(operation["final_path"])
        assert (operation["state"], operation["phase"]) == ("completed", "completed")
        assert temp_path.is_dir()
        assert final_path.is_dir()

    monkeypatch.setattr(batch_service, "_cleanup_operation_paths", real_cleanup)
    with app.app_context():
        report = batch_service.recover_incomplete_exports(app)
    assert report == {"recovered": 0, "rolled_back": 0, "cleanup_pending": 0}
    assert not temp_path.exists()
    assert final_path.is_dir()


def test_cleanup_queue_quarantines_then_rechecks_marker_before_delete(app, monkeypatch):
    token = uuid.uuid4().hex
    original = Path(app.config["TEMP_DIR"]) / f"export-race-{token}"
    original.mkdir()
    (original / storage.EXPORT_OWNER_MARKER).write_text(
        storage.export_owner_marker_text(token), encoding="utf-8"
    )
    (original / "owned.bin").write_bytes(b"owned")
    sentinel = b"external-replacement"
    real_rename = storage._atomic_rename
    swapped = False

    def replace_before_claim(source, target):
        nonlocal swapped
        if not swapped:
            swapped = True
            source_path = Path(source)
            storage.shutil.rmtree(source)
            source_path.mkdir()
            (source_path / "sentinel.bin").write_bytes(sentinel)
        return real_rename(source, target)

    with app.app_context():
        db = get_db()
        with transaction(db):
            storage.enqueue_file_cleanup(
                db,
                original,
                "temporary_tree",
                app.config["TEMP_DIR"],
                f"{storage.EXPORT_CLEANUP_REASON_PREFIX}{token}",
            )
        monkeypatch.setattr(storage, "_atomic_rename", replace_before_claim)
        result = storage.process_file_cleanup_queue(db)

        row = db.execute(
            "SELECT status,attempts,last_error FROM file_cleanup_queue WHERE path=?",
            (str(original.resolve()),),
        ).fetchone()

    assert result["completed"] == 0
    assert len(result["failed"]) == 1
    assert tuple(row[:2]) == ("pending", 1)
    assert row["last_error"]
    assert (original / "sentinel.bin").read_bytes() == sentinel
    assert not storage.export_cleanup_quarantine_path(original, token).exists()
