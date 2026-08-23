from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from install_lifecycle import InstallConfigurationError, inspect_data_root, resolve_runtime_configuration
from invoice_assistant.migrate import database_sha256, migrate_database


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _source(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8-sig")


def test_install_configuration_precedence_and_absolute_custom_root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(
        "INVOICE_APP_DATA_DIR=ignored-base\nINVOICE_APP_PORT=9999\n",
        encoding="utf-8",
    )
    (project / ".env.local").write_text(
        'DATA_FOLDER="custom data"\nINVOICE_APP_DATA_DIR="${DATA_FOLDER}" # fixed at install\nINVOICE_APP_PORT=8765\n',
        encoding="utf-8",
    )

    resolved = resolve_runtime_configuration(project, environment={"LOCALAPPDATA": str(tmp_path / "local")})
    assert Path(resolved["data_dir"]) == (project / "custom data").resolve()

    process_override = resolve_runtime_configuration(
        project,
        environment={
            "LOCALAPPDATA": str(tmp_path / "local"),
            "INVOICE_APP_DATA_DIR": str(tmp_path / "process data"),
            "INVOICE_APP_PORT": "08765",
        },
    )
    assert Path(process_override["data_dir"]) == (tmp_path / "process data").resolve()

    with pytest.raises(InstallConfigurationError, match="exactly 8765"):
        resolve_runtime_configuration(
            project,
            environment={"INVOICE_APP_PORT": "8766", "LOCALAPPDATA": str(tmp_path)},
        )


def test_install_data_inspection_reports_hash_and_never_migrates(tmp_path):
    root = tmp_path / "data"
    migrate_database(root, expect_no_database=True)
    database = root / "invoice_assistant.sqlite3"
    current_hash = database_sha256(database)

    assert inspect_data_root(root) == {
        "state": "current",
        "database_sha256": current_hash,
    }

    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA user_version=3")
        connection.commit()
    finally:
        connection.close()
    stale_hash = database_sha256(database)
    inspected = inspect_data_root(root)
    assert inspected["state"] == "migration_required"
    assert inspected["database_sha256"] == stale_hash
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
    finally:
        connection.close()


def test_windows_data_root_mutex_is_global_across_logon_sessions():
    source = _source("invoice_assistant/persistence.py")
    assert 'CreateMutexW(None, False, f"Global\\\\{self.name}")' in source
    assert 'CreateMutexW(None, False, f"Local\\\\{self.name}")' not in source


def test_install_freezes_data_root_and_checks_runtime_boundary_before_dependency_update():
    source = _source("install.ps1")
    lifecycle_source = _source("install_lifecycle.py")
    pip_install = source.index("-m pip install")

    assert source.index("Stop-ScheduledTask") < pip_install
    assert source.index("Get-NetTCPConnection -LocalPort 8765") < pip_install
    assert source.index("$runtimeConfigJson =") < pip_install
    assert "with data_root_mutex(root, timeout_ms=0):" in lifecycle_source
    assert lifecycle_source.index("current_hash = database_sha256(database)") < lifecycle_source.index(
        "validate_database_schema(database, check_integrity=True)"
    )
    assert '$serviceArguments = "-NoLogo -NoProfile -NonInteractive' in source
    assert '-File `"$serviceScript`" -DataDir `"$dataRoot`"' in source
    assert "New-ScheduledTaskAction -Execute $powershell" in source
    assert "pythonw.exe" not in source
    assert '$logPath = Join-Path $dataRoot "logs\\service.log"' in source


def test_service_binds_absolute_data_root_and_fixed_port():
    source = _source("service.ps1")
    assert "[string]$DataDir" in source
    assert "[IO.Path]::IsPathRooted($DataDir)" in source
    assert "$env:INVOICE_APP_DATA_DIR = $resolvedDataDir" in source
    assert '$env:INVOICE_APP_PORT = "8765"' in source


def test_start_and_uninstall_do_not_emit_cwd_or_default_data_root_hints():
    start_source = _source("start.ps1")
    uninstall_source = _source("uninstall.ps1")

    assert "-DataDir `\"$activeDataRoot`\"" in start_source
    assert '$logPath = Join-Path $activeDataRoot "logs\\service.log"' in start_source
    assert 'Join-Path $env:LOCALAPPDATA "InvoiceAssistant\\data\\logs\\service.log"' not in start_source
    assert '$registrationScript = Join-Path $PSScriptRoot "register-agents.ps1"' in uninstall_source
    assert '& `"$registrationScript`" -Agent all -Remove' in uninstall_source
    assert ".\\register-agents.ps1 -Agent all -Remove" not in uninstall_source


@pytest.mark.skipif(os.name != "nt", reason="PowerShell lifecycle contract")
def test_lifecycle_scripts_parse_without_execution():
    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    scripts = [str(PROJECT_ROOT / name) for name in ("install.ps1", "service.ps1", "start.ps1", "uninstall.ps1")]
    quoted_scripts = ",".join("'" + path.replace("'", "''") + "'" for path in scripts)
    parser = (
        f"$paths=@({quoted_scripts});$failed=$false;"
        "foreach($path in $paths){"
        "$tokens=$null;$errors=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors.Count -gt 0){$failed=$true;$errors|ForEach-Object{Write-Error $_.Message}}"
        "};if($failed){exit 1}"
    )
    parsed = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", parser],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    assert parsed.returncode == 0, parsed.stdout + parsed.stderr
