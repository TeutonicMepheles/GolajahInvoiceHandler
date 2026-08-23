from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .db import MigrationRequiredError, close_db, get_db, validate_database_schema
from .persistence import create_database_backup, prepare_data_dir


class AppError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def create_app(test_config: dict | None = None) -> Flask:
    base_dir = Path(__file__).resolve().parent.parent
    load_dotenv(base_dir / ".env.local", override=False)
    load_dotenv(base_dir / ".env", override=False)

    requested_data_dir = test_config.get("DATA_DIR") if test_config and "DATA_DIR" in test_config else None
    data_dir, migrated_data = prepare_data_dir(base_dir, requested_data_dir)
    web_dir = base_dir / "web"
    app = Flask(__name__, static_folder=str(web_dir), static_url_path="/static")
    app.config.from_mapping(
        BASE_DIR=str(base_dir),
        DATA_DIR=str(data_dir),
        DATABASE=str(data_dir / "invoice_assistant.sqlite3"),
        IMPORT_DIR=str(data_dir / "imports"),
        DEFAULT_ARCHIVE_DIR=str(data_dir / "archives"),
        TEMP_DIR=str(data_dir / "tmp"),
        TRASH_DIR=str(data_dir / "trash"),
        BACKUP_DIR=str(data_dir / "backups"),
        BACKUP_RETENTION=30,
        TRASH_RETENTION_DAYS=30,
        AUTO_BACKUP=True,
        DATA_MIGRATED=migrated_data,
        QUOTATION_TEMPLATE=os.environ.get(
            "QUOTATION_TEMPLATE_PATH",
            str(base_dir / "invoice_assistant" / "templates" / "quotation_template.docx"),
        ),
        MAX_CONTENT_LENGTH=80 * 1024 * 1024,
        MAX_FILE_SIZE=20 * 1024 * 1024,
        MAX_IMAGE_PIXELS=60_000_000,
        SEND_FILE_MAX_AGE_DEFAULT=0,
        JSON_AS_ASCII=False,
        DEEPSEEK_BASE_URL=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        DEEPSEEK_MODEL=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash-vision-exp"),
        MAX_RECOGNITION_PDF_PAGES=20,
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)
        if "BACKUP_DIR" not in test_config:
            app.config["BACKUP_DIR"] = str(Path(app.config["DATA_DIR"]) / "backups")

    # Startup is deliberately read-only until the complete v4 schema has been
    # proven. Missing/stale databases must be handled by the explicit migrate CLI.
    validate_database_schema(app.config["DATABASE"])

    for key in ("DATA_DIR", "IMPORT_DIR", "DEFAULT_ARCHIVE_DIR", "TEMP_DIR", "TRASH_DIR", "BACKUP_DIR"):
        Path(app.config[key]).mkdir(parents=True, exist_ok=True)

    app.teardown_appcontext(close_db)
    from .batch_service import recover_incomplete_exports
    from .file_operations import recover_incomplete_file_operations
    from .storage import process_file_cleanup_queue, purge_expired_trash, storage_reconciliation

    with app.app_context():
        recovery = recover_incomplete_exports(app)
        file_recovery = recover_incomplete_file_operations(app)
        cleanup = process_file_cleanup_queue()
        trash = purge_expired_trash(app.config["TRASH_RETENTION_DAYS"])
        if recovery["recovered"] or recovery["rolled_back"]:
            app.logger.warning("Recovered interrupted exports: %s", recovery)
        if (
            file_recovery["recovered"]
            or file_recovery["resumable"]
            or file_recovery["failed"]
            or file_recovery["orphaned_cleaned"]
        ):
            app.logger.warning("Recovered interrupted file operations: %s", file_recovery)
        if cleanup["failed"] or trash["failed"]:
            app.logger.warning("Persistent storage cleanup requires retry: cleanup=%s trash=%s", cleanup, trash)
    if app.config["AUTO_BACKUP"] and not app.config["TESTING"]:
        create_database_backup(
            app.config["DATABASE"],
            app.config["BACKUP_DIR"],
            retention=app.config["BACKUP_RETENTION"],
        )
    backup_check_lock = threading.Lock()

    from .api import api
    from .features.agent_mcp.routes import agent_operations_api
    from .features.batch_management.routes import batch_management_api
    from .features.document_intake.routes import document_intake_api
    from .features.quotation.routes import quotation_api

    app.register_blueprint(api, url_prefix="/api")
    app.register_blueprint(agent_operations_api, url_prefix="/api")
    app.register_blueprint(batch_management_api, url_prefix="/api")
    app.register_blueprint(document_intake_api, url_prefix="/api")
    app.register_blueprint(quotation_api, url_prefix="/api")

    @app.get("/")
    def index():
        return send_from_directory(web_dir, "index.html")

    @app.get("/health")
    def health():
        database_check = get_db().execute("PRAGMA quick_check").fetchone()[0]
        foreign_key_violations = len(get_db().execute("PRAGMA foreign_key_check").fetchall())
        probes = {}
        archive_setting = get_db().execute("SELECT value FROM settings WHERE key='archive_root'").fetchone()
        probe_paths = {
            "import_dir": app.config["IMPORT_DIR"],
            "archive_root": archive_setting["value"] if archive_setting else app.config["DEFAULT_ARCHIVE_DIR"],
            "temp_dir": app.config["TEMP_DIR"],
            "trash_dir": app.config["TRASH_DIR"],
            "backup_dir": app.config["BACKUP_DIR"],
        }
        for label, value in probe_paths.items():
            root = Path(value).resolve()
            probe = root / f".health-{uuid.uuid4().hex}"
            try:
                probe.write_bytes(b"ok")
                probes[label] = True
            except OSError:
                probes[label] = False
            finally:
                probe.unlink(missing_ok=True)
        reconciliation = storage_reconciliation(get_db())
        ok = (
            database_check == "ok"
            and foreign_key_violations == 0
            and all(probes.values())
            and not reconciliation["missing_managed_files"]
        )
        return jsonify(
            {
                "ok": ok,
                "service": "invoice-assistant",
                "storage": "connected" if ok else "degraded",
                "database_check": database_check,
                "foreign_key_violations": foreign_key_violations,
                "writable": probes,
                "reconciliation": reconciliation,
            }
        ), (200 if ok else 503)

    @app.after_request
    def prevent_stale_frontend(response):
        if request.path == "/" or request.path.startswith("/static/") or request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        allows_same_origin_framing = request.endpoint == "document_intake_api.attachment_preview"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if allows_same_origin_framing else "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        frame_ancestors = "'self'" if allows_same_origin_framing else "'none'"
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; base-uri 'self'; frame-ancestors {frame_ancestors}; form-action 'self'; "
            "script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self' data:; connect-src 'self'; frame-src 'self'"
        )
        return response

    @app.before_request
    def daily_backup():
        if app.config["TESTING"] or not app.config["AUTO_BACKUP"] or request.path == "/health":
            return None
        if backup_check_lock.acquire(blocking=False):
            try:
                create_database_backup(
                    app.config["DATABASE"],
                    app.config["BACKUP_DIR"],
                    retention=app.config["BACKUP_RETENTION"],
                )
            finally:
                backup_check_lock.release()
        return None

    @app.errorhandler(AppError)
    def handle_app_error(exc: AppError):
        outcome = getattr(exc, "outcome", "not_applied")
        if outcome not in {"not_applied", "unknown"}:
            outcome = "not_applied"
        payload = {"error": exc.code, "message": exc.message, "outcome": outcome}
        if bool(getattr(exc, "replayed", False)):
            payload["meta"] = {"replayed": True}
        return jsonify(payload), exc.status_code

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        return jsonify({"error": exc.name.lower().replace(" ", "_"), "message": exc.description}), exc.code

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc: Exception):
        if app.config["TESTING"]:
            raise exc
        app.logger.exception("Unhandled application error")
        return jsonify({"error": "internal_error", "message": "操作失败，请稍后重试或查看服务日志。"}), 500

    return app
