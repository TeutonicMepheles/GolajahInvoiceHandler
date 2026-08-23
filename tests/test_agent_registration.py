from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from invoice_assistant.features.agent_mcp.contracts import exposed_contracts
from invoice_assistant.features.agent_mcp.client import LeaseGuard
import invoice_assistant.features.agent_mcp.registration as registration

from invoice_assistant.features.agent_mcp.registration import (
    EXIT_CAPABILITY_UNSUPPORTED,
    EXIT_CLIENT_MISSING,
    EXIT_CONFLICT_OR_DRIFT,
    EXIT_OK,
    EXIT_POST_WRITE_ROLLED_BACK,
    EXIT_RECOVERY_INCOMPLETE,
    CapabilityUnsupported,
    ClientTarget,
    MCP_DISCOVERY_TIMEOUT_SECONDS,
    RegistrationError,
    RegistrationRequest,
    RegistrationServices,
    SimulatedInterruption,
    _default_secure_state_root,
    build_managed_entry,
    canonical_entry_hash,
    execute_registration,
    extract_managed_entry,
    normalize_allowed_roots,
    render_config,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request(tmp_path: Path, *, agent: str = "all", roots=(), remove: bool = False):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    launcher = project / "run-agent-mcp.ps1"
    launcher.touch(exist_ok=True)
    powershell = tmp_path / "powershell.exe"
    powershell.touch(exist_ok=True)
    return RegistrationRequest(
        agent=agent,
        allowed_roots=tuple(roots),
        state_root=tmp_path / "state",
        project_root=project,
        powershell_path=powershell,
        launcher_path=launcher,
        remove=remove,
    )


def _services(tmp_path: Path, *, installed=("codex", "hermes"), hook=None, probe=None):
    executables = {}
    for client in installed:
        path = tmp_path / f"{client}.exe"
        path.touch()
        executables[client] = path

    def secure(path, _powershell):
        path.mkdir(parents=True, exist_ok=True)

    return RegistrationServices(
        resolve_executable=lambda client: executables.get(client),
        capability_probe=probe or (lambda _target: None),
        client_validate=lambda _target: None,
        mcp_validate=lambda _client, _entry, _expected: None,
        secure_state_root=secure,
        failure_hook=hook or (lambda _stage, _target: None),
    )


@pytest.fixture
def isolated_homes(tmp_path, monkeypatch):
    codex = tmp_path / "codex-home"
    hermes = tmp_path / "hermes-home"
    codex.mkdir()
    hermes.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    return codex, hermes


def test_entry_contract_is_exact_and_hermes_never_uses_empty_include(tmp_path):
    request = _request(tmp_path, agent="hermes")
    lease = request.state_root / "leases" / "hermes.json"
    entry = build_managed_entry("hermes", request, lease, "generation")
    include = entry["tools"]["include"]
    assert len(include) == 15
    assert include
    assert not any("*" in name or "?" in name or "[" in name for name in include)
    assert entry["trust"] == "untrusted"
    assert entry["tools"]["resources"] is False
    assert entry["tools"]["prompts"] is False
    assert entry["supports_parallel_tool_calls"] is False
    assert entry["timeout"] == 360
    assert entry["connect_timeout"] == 15

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    request_with_roots = _request(tmp_path, agent="codex", roots=(allowed,))
    codex = build_managed_entry("codex", request_with_roots, lease, "generation")
    assert len(codex["enabled_tools"]) == 17
    assert codex["default_tools_approval_mode"] == "writes"
    assert codex["startup_timeout_sec"] == 15
    assert codex["tool_timeout_sec"] == 360


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_state_root_is_created_with_verified_private_acl(tmp_path):
    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    state_root = tmp_path / "private-state"
    _default_secure_state_root(state_root, powershell)
    assert state_root.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher contract")
def test_windows_launcher_is_cwd_independent_and_stdout_is_valid_mcp(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    launcher = project_root / "run-agent-mcp.ps1"
    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    generation = "launcher-test"
    lease = tmp_path / "lease.json"
    lease.write_text(
        json.dumps({"generation": generation, "active": True}), encoding="utf-8"
    )
    env = os.environ.copy()
    env.update(
        {
            "INVOICE_MCP_ALLOWED_ROOTS_JSON": "[]",
            "INVOICE_MCP_REGISTRATION_LEASE_PATH": str(lease),
            "INVOICE_MCP_REGISTRATION_GENERATION": generation,
        }
    )

    async def discover():
        params = StdioServerParameters(
            command=str(powershell),
            args=[
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
            ],
            cwd=tmp_path,
            env=env,
        )
        async with asyncio.timeout(30):
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    return {tool.name for tool in (await session.list_tools()).tools}

    assert asyncio.run(discover()) == {contract.name for contract in exposed_contracts(False)}


def test_register_noop_update_and_remove_preserve_other_config(
    tmp_path, monkeypatch, isolated_homes
):
    codex_home, hermes_home = isolated_homes
    codex_config = codex_home / "config.toml"
    hermes_config = hermes_home / "config.yaml"
    codex_config.write_bytes(
        b'# keep codex comment\r\nmodel = "local"\r\n\r\n[mcp_servers.other]\r\ncommand = "other.exe"\r\n'
    )
    hermes_config.write_bytes(
        b'# keep hermes comment\r\ntheme: dark\r\nmcp_servers:\r\n  other:\r\n    command: other.exe\r\n'
    )
    request = _request(tmp_path)
    services = _services(tmp_path)

    assert execute_registration(request, services) == EXIT_OK
    assert b"# keep codex comment\r\n" in codex_config.read_bytes()
    assert b"# keep hermes comment\r\n" in hermes_config.read_bytes()
    assert b"other.exe" in codex_config.read_bytes()
    assert b"other.exe" in hermes_config.read_bytes()
    codex_entry = extract_managed_entry("codex", codex_config.read_bytes())
    hermes_entry = extract_managed_entry("hermes", hermes_config.read_bytes())
    assert len(codex_entry["enabled_tools"]) == 15
    assert hermes_entry["tools"]["include"] == codex_entry["enabled_tools"]

    state_path = request.state_root / "managed-state.json"
    lease_path = request.state_root / "leases" / "codex.json"
    before = {
        path: (path.stat().st_mtime_ns, _sha(path))
        for path in (codex_config, hermes_config, state_path, lease_path)
    }
    assert execute_registration(request, services) == EXIT_OK
    assert before == {
        path: (path.stat().st_mtime_ns, _sha(path))
        for path in (codex_config, hermes_config, state_path, lease_path)
    }

    old_lease = json.loads(lease_path.read_text(encoding="utf-8"))
    old_guard = LeaseGuard(str(lease_path), old_lease["generation"])
    assert old_guard.stale() is False
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    update = _request(tmp_path, roots=(allowed,))
    assert execute_registration(update, services) == EXIT_OK
    new_lease = json.loads(lease_path.read_text(encoding="utf-8"))
    assert new_lease["active"] is True
    assert new_lease["generation"] != old_lease["generation"]
    new_guard = LeaseGuard(str(lease_path), new_lease["generation"])
    assert old_guard.stale() is True
    assert new_guard.stale() is False
    assert len(extract_managed_entry("codex", codex_config.read_bytes())["enabled_tools"]) == 17

    remove = _request(tmp_path, roots=(allowed,), remove=True)
    assert execute_registration(remove, services) == EXIT_OK
    assert extract_managed_entry("codex", codex_config.read_bytes()) is None
    assert extract_managed_entry("hermes", hermes_config.read_bytes()) is None
    assert b"other.exe" in codex_config.read_bytes()
    assert b"other.exe" in hermes_config.read_bytes()
    assert not state_path.exists()
    assert json.loads(lease_path.read_text(encoding="utf-8"))["active"] is False
    assert new_guard.stale() is True
    assert execute_registration(remove, services) == EXIT_OK


def test_unmanaged_conflict_and_managed_drift_are_never_overwritten(
    tmp_path, isolated_homes
):
    codex_home, _ = isolated_homes
    config = codex_home / "config.toml"
    unmanaged = b'[mcp_servers.invoice_assistant]\ncommand = "user-owned.exe"\n'
    config.write_bytes(unmanaged)
    request = _request(tmp_path, agent="codex")
    services = _services(tmp_path, installed=("codex",))
    assert execute_registration(request, services) == EXIT_CONFLICT_OR_DRIFT
    assert config.read_bytes() == unmanaged
    assert not (request.state_root / "managed-state.json").exists()

    config.unlink()
    assert execute_registration(request, services) == EXIT_OK
    state = json.loads((request.state_root / "managed-state.json").read_text(encoding="utf-8"))
    managed_hash = state["targets"]["codex"]["canonical_hash"]
    data = config.read_bytes().replace(b'command = "', b'command = "changed-')
    config.write_bytes(data)
    assert canonical_entry_hash(extract_managed_entry("codex", data)) != managed_hash
    remove = _request(tmp_path, agent="codex", remove=True)
    assert execute_registration(remove, services) == EXIT_CONFLICT_OR_DRIFT
    assert config.read_bytes() == data
    lease = json.loads((request.state_root / "leases" / "codex.json").read_text(encoding="utf-8"))
    assert lease["active"] is False


def test_missing_and_capability_exit_codes_use_only_temp_roots(tmp_path, isolated_homes):
    request = _request(tmp_path, agent="codex")
    assert execute_registration(request, _services(tmp_path, installed=())) == EXIT_CLIENT_MISSING

    all_request = _request(tmp_path, agent="all")
    services = _services(tmp_path, installed=("codex",))
    assert execute_registration(all_request, services) == EXIT_OK
    state = json.loads((request.state_root / "managed-state.json").read_text(encoding="utf-8"))
    assert set(state["targets"]) == {"codex"}

    other = tmp_path / "other"
    other.mkdir()
    monkey_services = _services(
        tmp_path,
        installed=("codex",),
        probe=lambda _target: (_ for _ in ()).throw(CapabilityUnsupported("unsupported")),
    )
    # Even a byte-for-byte no-op revalidates the client so an unsupported
    # upgrade/downgrade cannot inherit an old approval attestation.
    assert execute_registration(all_request, monkey_services) == EXIT_CAPABILITY_UNSUPPORTED
    allowed = tmp_path / "new-root"
    allowed.mkdir()
    changed = _request(tmp_path, agent="codex", roots=(allowed,))
    assert execute_registration(changed, monkey_services) == EXIT_CAPABILITY_UNSUPPORTED


def test_managed_missing_executable_revokes_lease_and_remove_does_not_need_it(
    tmp_path, isolated_homes
):
    codex_home, _ = isolated_homes
    config = codex_home / "config.toml"
    request = _request(tmp_path, agent="codex")
    assert execute_registration(request, _services(tmp_path, installed=("codex",))) == EXIT_OK
    before = config.read_bytes()

    missing = _services(tmp_path, installed=())
    assert execute_registration(request, missing) == EXIT_CLIENT_MISSING
    assert config.read_bytes() == before
    lease_path = request.state_root / "leases" / "codex.json"
    assert json.loads(lease_path.read_text(encoding="utf-8"))["active"] is False

    remove = _request(tmp_path, agent="codex", remove=True)
    assert execute_registration(remove, missing) == EXIT_OK
    assert extract_managed_entry("codex", config.read_bytes()) is None
    assert not (request.state_root / "managed-state.json").exists()


def test_post_write_failure_restores_original_bytes_and_cleans_journal(
    tmp_path, isolated_homes
):
    codex_home, _ = isolated_homes
    config = codex_home / "config.toml"
    original = b'# original\r\nmodel = "keep"\r\n'
    config.write_bytes(original)
    request = _request(tmp_path, agent="codex")
    services = _services(tmp_path, installed=("codex",))
    services.client_validate = lambda _target: (_ for _ in ()).throw(
        CapabilityUnsupported("post failure")
    )
    assert execute_registration(request, services) == EXIT_POST_WRITE_ROLLED_BACK
    assert config.read_bytes() == original
    assert not (request.state_root / "managed-state.json").exists()
    assert not (request.state_root / "transaction-journal.json").exists()
    assert not list(tmp_path.rglob("*.backup"))


def test_interrupted_prepare_recovers_and_cas_preserves_external_change(
    tmp_path, isolated_homes
):
    codex_home, _ = isolated_homes
    config = codex_home / "config.toml"
    original = b'model = "before"\n'
    config.write_bytes(original)
    request = _request(tmp_path, agent="codex")

    def crash(stage, target):
        if stage == "replaced" and target == str(config):
            raise SimulatedInterruption()

    with pytest.raises(SimulatedInterruption):
        execute_registration(request, _services(tmp_path, installed=("codex",), hook=crash))
    assert config.read_bytes() != original
    assert (request.state_root / "transaction-journal.json").exists()

    external = b'model = "external"\n'
    config.write_bytes(external)
    assert execute_registration(request, _services(tmp_path, installed=("codex",))) == EXIT_RECOVERY_INCOMPLETE
    assert config.read_bytes() == external
    assert (request.state_root / "transaction-journal.json").exists()


def test_interrupted_prepare_without_external_write_is_recovered(
    tmp_path, isolated_homes
):
    codex_home, _ = isolated_homes
    config = codex_home / "config.toml"
    original = b'model = "before"\n'
    config.write_bytes(original)
    request = _request(tmp_path, agent="codex")
    crashed = False

    def crash_once(stage, target):
        nonlocal crashed
        if not crashed and stage == "replaced" and target == str(config):
            crashed = True
            raise SimulatedInterruption()

    services = _services(tmp_path, installed=("codex",), hook=crash_once)
    with pytest.raises(SimulatedInterruption):
        execute_registration(request, services)
    services.failure_hook = lambda _stage, _target: None
    assert execute_registration(request, services) == EXIT_OK
    assert b'model = "before"' in config.read_bytes()
    assert extract_managed_entry("codex", config.read_bytes()) is not None
    assert not (request.state_root / "transaction-journal.json").exists()


@pytest.mark.parametrize(
    "crash_stage",
    [
        "journal_prepared",
        "flushed",
        "staged",
        "replace_intent",
        "post_validated",
        "state_written",
        "activated",
        "committed",
        "journal_cleanup",
    ],
)
def test_every_journal_boundary_recovers_or_finishes_cleanly(
    tmp_path, isolated_homes, crash_stage
):
    codex_home, _ = isolated_homes
    config = codex_home / "config.toml"
    original = b'# preserved\nmodel = "before"\n'
    config.write_bytes(original)
    request = _request(tmp_path, agent="codex")
    crashed = False

    def crash_once(stage, _target):
        nonlocal crashed
        if not crashed and stage == crash_stage:
            crashed = True
            raise SimulatedInterruption()

    services = _services(tmp_path, installed=("codex",), hook=crash_once)
    with pytest.raises(SimulatedInterruption):
        execute_registration(request, services)
    assert (request.state_root / "transaction-journal.json").exists()

    services.failure_hook = lambda _stage, _target: None
    assert execute_registration(request, services) == EXIT_OK
    assert b"# preserved\n" in config.read_bytes()
    assert extract_managed_entry("codex", config.read_bytes()) is not None
    assert not (request.state_root / "transaction-journal.json").exists()
    assert not list(tmp_path.rglob("*.backup"))
    assert not list(tmp_path.rglob("*.stage"))


def test_committed_cleanup_failure_keeps_new_state_and_retries_cleanup(
    tmp_path, isolated_homes, monkeypatch
):
    codex_home, _ = isolated_homes
    config = codex_home / "config.toml"
    config.write_bytes(b'model = "before"\n')
    request = _request(tmp_path, agent="codex")
    services = _services(tmp_path, installed=("codex",))

    original_cleanup = registration._cleanup_transaction_files
    cleanup_calls = 0

    def fail_first_cleanup(changes, journal_path):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise RegistrationError("simulated backup cleanup failure", EXIT_RECOVERY_INCOMPLETE)
        return original_cleanup(changes, journal_path)

    monkeypatch.setattr(registration, "_cleanup_transaction_files", fail_first_cleanup)
    assert execute_registration(request, services) == EXIT_RECOVERY_INCOMPLETE
    assert extract_managed_entry("codex", config.read_bytes()) is not None
    state_path = request.state_root / "managed-state.json"
    assert state_path.exists()
    journal_path = request.state_root / "transaction-journal.json"
    assert json.loads(journal_path.read_text(encoding="utf-8"))["phase"] == "committed"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    saved = state["targets"]["codex"]
    assert LeaseGuard(saved["lease_path"], saved["generation"]).stale() is False

    monkeypatch.setattr(registration, "_cleanup_transaction_files", original_cleanup)
    assert execute_registration(request, services) == EXIT_OK
    assert extract_managed_entry("codex", config.read_bytes()) is not None
    assert not journal_path.exists()
    assert not list(tmp_path.rglob("*.backup"))


def test_candidate_config_crash_keeps_old_and_new_generations_stale_until_recovery(
    tmp_path, isolated_homes
):
    codex_home, _ = isolated_homes
    config = codex_home / "config.toml"
    request = _request(tmp_path, agent="codex")
    services = _services(tmp_path, installed=("codex",))
    assert execute_registration(request, services) == EXIT_OK

    state_path = request.state_root / "managed-state.json"
    old_state = json.loads(state_path.read_text(encoding="utf-8"))
    old_saved = old_state["targets"]["codex"]
    old_guard = LeaseGuard(old_saved["lease_path"], old_saved["generation"])
    assert old_guard.stale() is False

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    update = _request(tmp_path, agent="codex", roots=(allowed,))
    post_validation_calls = 0

    def post_client_validate(_target):
        nonlocal post_validation_calls
        post_validation_calls += 1

    def crash_after_candidate_config(stage, target):
        if stage == "replaced" and target == str(config):
            raise SimulatedInterruption()

    services.client_validate = post_client_validate
    services.failure_hook = crash_after_candidate_config
    with pytest.raises(SimulatedInterruption):
        execute_registration(update, services)

    assert post_validation_calls == 0
    assert json.loads(state_path.read_text(encoding="utf-8")) == old_state
    candidate_lease = json.loads(Path(old_saved["lease_path"]).read_text(encoding="utf-8"))
    candidate_guard = LeaseGuard(old_saved["lease_path"], candidate_lease["generation"])
    assert old_guard.stale() is True
    assert candidate_guard.stale() is True
    assert json.loads((request.state_root / "transaction-journal.json").read_text(encoding="utf-8"))["phase"] == "prepared"

    services.failure_hook = lambda _stage, _target: None
    assert execute_registration(update, services) == EXIT_OK
    new_state = json.loads(state_path.read_text(encoding="utf-8"))
    new_saved = new_state["targets"]["codex"]
    assert new_saved["generation"] != old_saved["generation"]
    assert old_guard.stale() is True
    assert LeaseGuard(new_saved["lease_path"], new_saved["generation"]).stale() is False


def test_state_activation_occurs_only_after_post_write_client_validation(
    tmp_path, isolated_homes
):
    request = _request(tmp_path, agent="codex")
    services = _services(tmp_path, installed=("codex",))
    events: list[str] = []
    services.client_validate = lambda _target: events.append("client_validated")

    def record(stage, _target):
        if stage == "state_written":
            events.append("state_written")

    services.failure_hook = record
    assert execute_registration(request, services) == EXIT_OK
    assert events.index("client_validated") < events.index("state_written")


def test_codex_validation_rejects_json_that_does_not_echo_frozen_fields(
    tmp_path, monkeypatch
):
    request = _request(tmp_path, agent="codex")
    config = tmp_path / "codex" / "config.toml"
    config.parent.mkdir()
    lease = request.state_root / "leases" / "codex.json"
    entry = build_managed_entry("codex", request, lease, "generation")
    config.write_bytes(render_config("codex", b"", entry))
    executable = tmp_path / "codex.exe"
    executable.touch()
    target = ClientTarget("codex", config, executable)
    monkeypatch.setattr(
        registration,
        "_run_checked",
        lambda *args, **kwargs: CompletedProcess(args=args, returncode=0, stdout="{}", stderr=""),
    )
    with pytest.raises(CapabilityUnsupported, match="transport"):
        registration._default_client_validate(target)


def test_capability_probe_is_pinned_and_discovery_timeout_is_fifteen_seconds(
    tmp_path, monkeypatch
):
    executable = tmp_path / "codex.exe"
    executable.touch()
    target = ClientTarget("codex", tmp_path / "config.toml", executable)
    monkeypatch.setattr(
        registration,
        "_run_checked",
        lambda *args, **kwargs: CompletedProcess(
            args=args, returncode=0, stdout="codex-cli 0.148.0", stderr=""
        ),
    )
    with pytest.raises(CapabilityUnsupported, match="has not passed"):
        registration._default_capability_probe(target)
    assert MCP_DISCOVERY_TIMEOUT_SECONDS == 15


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point contract")
def test_registration_rejects_reparse_allowed_root(tmp_path):
    target = tmp_path / "ordinary"
    target.mkdir()
    link = tmp_path / "linked-root"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip(f"directory reparse point unavailable: {exc}; {completed.stderr}")
    with pytest.raises(RegistrationError, match="without reparse points"):
        normalize_allowed_roots((link,))


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("codex") is None or shutil.which("hermes") is None,
    reason="Windows real-client isolated registration contract",
)
def test_real_clients_register_from_unrelated_unicode_cwd_without_touching_user_homes(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    unrelated = tmp_path / "无关 cwd with space"
    unrelated.mkdir()
    codex_home = tmp_path / "isolated-codex"
    hermes_home = tmp_path / "isolated-hermes"
    codex_home.mkdir()
    hermes_home.mkdir()
    state_root = tmp_path / "isolated-state"
    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    environment["HERMES_HOME"] = str(hermes_home)
    command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(project_root / "register-agents.ps1"),
        "-Agent",
        "all",
        "-StateRoot",
        str(state_root),
    ]
    registered = subprocess.run(
        command,
        cwd=unrelated,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert registered.returncode == 0, registered.stdout + registered.stderr
    assert extract_managed_entry("codex", (codex_home / "config.toml").read_bytes()) is not None
    assert extract_managed_entry("hermes", (hermes_home / "config.yaml").read_bytes()) is not None

    removed = subprocess.run(
        [*command, "-Remove"],
        cwd=unrelated,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert extract_managed_entry("codex", (codex_home / "config.toml").read_bytes()) is None
    assert extract_managed_entry("hermes", (hermes_home / "config.yaml").read_bytes()) is None
