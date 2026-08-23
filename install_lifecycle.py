from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Mapping


_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_EXPANSION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class InstallConfigurationError(RuntimeError):
    pass


def _decode_dotenv_value(raw: str, source: Path, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] not in ('"', "'"):
        return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()

    quote = value[0]
    output: list[str] = []
    escaped = False
    closing: int | None = None
    for index, character in enumerate(value[1:], start=1):
        if quote == '"' and escaped:
            output.append(
                {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}.get(
                    character, "\\" + character
                )
            )
            escaped = False
        elif quote == '"' and character == "\\":
            escaped = True
        elif character == quote:
            closing = index
            break
        else:
            output.append(character)
    if closing is None or escaped:
        raise InstallConfigurationError(
            f"{source.name}:{line_number}: unterminated quoted value"
        )
    remainder = value[closing + 1 :].strip()
    if remainder and not remainder.startswith("#"):
        raise InstallConfigurationError(
            f"{source.name}:{line_number}: unexpected text after quoted value"
        )
    return "".join(output)


def resolve_runtime_configuration(
    project_root: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    root = Path(project_root).expanduser().resolve()
    source_environment = dict(os.environ if environment is None else environment)
    # Keep all variables in the parse context so a data-root setting may safely
    # reference a non-sensitive helper variable from the same dotenv files.
    values = dict(source_environment)

    for environment_file in (root / ".env.local", root / ".env"):
        if not environment_file.is_file():
            continue
        try:
            lines = environment_file.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise InstallConfigurationError(
                f"cannot read {environment_file.name}: {exc}"
            ) from exc
        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = _ASSIGNMENT.match(line)
            if not match:
                continue
            key, raw_value = match.groups()
            if key in values:
                continue
            value = _decode_dotenv_value(raw_value, environment_file, line_number)
            values[key] = _EXPANSION.sub(
                lambda item: values.get(item.group(1))
                or item.group(2)
                or "",
                value,
            )

    configured_port = values.get("INVOICE_APP_PORT")
    if configured_port not in (None, ""):
        try:
            port = int(configured_port, 10)
        except ValueError as exc:
            raise InstallConfigurationError(
                "INVOICE_APP_PORT must be unset or exactly 8765"
            ) from exc
        if port != 8765:
            raise InstallConfigurationError(
                "INVOICE_APP_PORT must be unset or exactly 8765"
            )

    configured_data = values.get("INVOICE_APP_DATA_DIR")
    if configured_data:
        candidate = Path(configured_data).expanduser()
        data_root = candidate if candidate.is_absolute() else root / candidate
        data_root = data_root.resolve()
    elif source_environment.get("LOCALAPPDATA"):
        data_root = (
            Path(source_environment["LOCALAPPDATA"]) / "InvoiceAssistant" / "data"
        ).resolve()
    else:
        data_root = (Path.home() / ".local" / "share" / "invoice-assistant").resolve()
    return {"data_dir": str(data_root)}


def inspect_data_root(data_dir: str | Path) -> dict[str, str]:
    # Imports are deliberately lazy: resolve-config must work before the virtual
    # environment and third-party dependencies exist.
    from invoice_assistant.db import validate_database_schema
    from invoice_assistant.migrate import database_sha256
    from invoice_assistant.persistence import DATABASE_NAME, data_root_mutex

    root = Path(data_dir).expanduser().resolve()
    database = root / DATABASE_NAME
    with data_root_mutex(root, timeout_ms=0):
        if database.exists() and not database.is_file():
            return {"state": "invalid_database_path"}
        if database.is_file():
            current_hash = database_sha256(database)
            try:
                validate_database_schema(database, check_integrity=True)
            except Exception as exc:
                return {
                    "state": "migration_required",
                    "database_sha256": current_hash,
                    "detail": str(exc),
                }
            return {"state": "current", "database_sha256": current_hash}
        if root.exists() and not root.is_dir():
            return {"state": "invalid_data_root"}
        if root.is_dir() and any(root.iterdir()):
            return {"state": "nonempty_without_database"}
        return {"state": "strictly_empty"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Invoice Assistant installation preflight")
    subparsers = parser.add_subparsers(dest="command", required=True)
    resolve = subparsers.add_parser("resolve-config")
    resolve.add_argument("--project-root", required=True)
    inspect = subparsers.add_parser("inspect-data-root")
    inspect.add_argument("--data-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "resolve-config":
            result = resolve_runtime_configuration(args.project_root)
        else:
            result = inspect_data_root(args.data_dir)
    except InstallConfigurationError as exc:
        print(f"install_configuration_invalid: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Avoid importing the application package in the dependency-free
        # resolve-config path merely to classify an unexpected exception.
        if (
            exc.__class__.__name__ == "DataRootBusyError"
            and exc.__class__.__module__ == "invoice_assistant.persistence"
        ):
            print(f"data_root_busy: {exc}", file=sys.stderr)
            return 3
        print(f"install_preflight_failed: {exc}", file=sys.stderr)
        return 4
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
