from __future__ import annotations

import atexit
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv


FIXED_SERVICE_PORT = 8765


def load_service_environment(base_dir: Path) -> None:
    load_dotenv(base_dir / ".env.local", override=False)
    load_dotenv(base_dir / ".env", override=False)


def validate_service_port() -> int:
    configured = os.environ.get("INVOICE_APP_PORT")
    if configured in (None, ""):
        return FIXED_SERVICE_PORT
    try:
        port = int(configured, 10)
    except ValueError as exc:
        raise RuntimeError("INVOICE_APP_PORT must be unset or exactly 8765") from exc
    if port != FIXED_SERVICE_PORT:
        raise RuntimeError("INVOICE_APP_PORT must be unset or exactly 8765")
    return FIXED_SERVICE_PORT


_base_dir = Path(__file__).resolve().parent
load_service_environment(_base_dir)
_service_port = validate_service_port()

# Port validation intentionally precedes importing or constructing the service.
from waitress import serve  # noqa: E402

from invoice_assistant import create_app  # noqa: E402
from invoice_assistant.db import validate_database_schema  # noqa: E402
from invoice_assistant.persistence import DATABASE_NAME, DataRootMutex, default_data_dir  # noqa: E402


def configure_logging(data_dir: Path) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "service.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(
        isinstance(existing, RotatingFileHandler)
        and existing.baseFilename == handler.baseFilename
        for existing in root.handlers
    ):
        root.addHandler(handler)


def build_app(data_dir: Path | None = None):
    data_dir = data_dir or default_data_dir(_base_dir)
    # The first filesystem-sensitive operation is a read-only/no-create schema
    # check. In particular, no log directory exists merely because startup failed.
    validate_database_schema(data_dir / DATABASE_NAME)
    configure_logging(data_dir)
    try:
        return create_app({"DATA_DIR": str(data_dir)})
    except Exception:
        logging.getLogger(__name__).exception("Invoice Assistant failed during startup")
        raise


_service_data_dir = default_data_dir(_base_dir)
_service_mutex = DataRootMutex(_service_data_dir).acquire(timeout_ms=0)
try:
    app = build_app(_service_data_dir)
except Exception:
    _service_mutex.release()
    raise
atexit.register(_service_mutex.release)


if __name__ == "__main__":
    serve(
        app,
        host="127.0.0.1",
        port=FIXED_SERVICE_PORT,
        threads=8,
        channel_timeout=120,
        clear_untrusted_proxy_headers=True,
    )
