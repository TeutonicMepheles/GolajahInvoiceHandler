from __future__ import annotations

import argparse
import asyncio
import copy
import ctypes
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import tomlkit
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .contracts import exposed_contracts
from .file_access import FileAccessError, canonical_allowed_root


SERVICE_NAME = "invoice_assistant"
STATE_SCHEMA_VERSION = 1
JOURNAL_SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_CLIENT_MISSING = 20
EXIT_CAPABILITY_UNSUPPORTED = 21
EXIT_CONFLICT_OR_DRIFT = 30
EXIT_PREPARE_FAILED = 40
EXIT_POST_WRITE_ROLLED_BACK = 50
EXIT_RECOVERY_INCOMPLETE = 51

LEASE_ENV = "INVOICE_MCP_REGISTRATION_LEASE_PATH"
GENERATION_ENV = "INVOICE_MCP_REGISTRATION_GENERATION"
ROOTS_ENV = "INVOICE_MCP_ALLOWED_ROOTS_JSON"
MCP_DISCOVERY_TIMEOUT_SECONDS = 15

# Registration is deliberately fail-closed around client upgrades.  These are
# the Windows builds whose config parsing and approval implementation were
# inspected for this Plan.  A newer build needs the same isolated acceptance
# gate before this allowlist is advanced; a version string is only one part of
# the capability check below.
SUPPORTED_CLIENT_VERSIONS = {
    "codex": {(0, 149, 0)},
    "hermes": {(0, 20, 1)},
}

POWERSHELL_ARGS_PREFIX = (
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
)


class RegistrationError(Exception):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class CapabilityUnsupported(RegistrationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, EXIT_CAPABILITY_UNSUPPORTED)


class SimulatedInterruption(BaseException):
    """Test-only crash signal. Normal registration never raises this itself."""


@dataclass(frozen=True)
class RegistrationRequest:
    agent: str
    allowed_roots: tuple[Path, ...]
    state_root: Path
    project_root: Path
    powershell_path: Path
    launcher_path: Path
    remove: bool = False


@dataclass(frozen=True)
class ClientTarget:
    name: str
    config_path: Path
    executable: Path | None


@dataclass
class FileChange:
    path: Path
    original_exists: bool
    original_sha256: str | None
    desired_bytes: bytes | None
    written_sha256: str | None
    stage_path: Path
    backup_path: Path
    replace_intent: bool = False
    replaced: bool = False

    @property
    def is_noop(self) -> bool:
        if not self.original_exists:
            return self.desired_bytes is None
        if self.desired_bytes is None:
            return False
        return self.original_sha256 == self.written_sha256


@dataclass
class RegistrationServices:
    resolve_executable: Callable[[str], Path | None]
    capability_probe: Callable[[ClientTarget], None]
    client_validate: Callable[[ClientTarget], None]
    mcp_validate: Callable[[str, Mapping[str, Any], frozenset[str]], None]
    secure_state_root: Callable[[Path, Path], None]
    staged_client_validate: Callable[[ClientTarget, bytes, Path], None] = field(
        default=lambda _target, _data, _state_root: None
    )
    failure_hook: Callable[[str, str | None], None] = field(default=lambda _stage, _target: None)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple, CommentedSeq)):
        return [_plain(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "unwrap"):
        return _plain(value.unwrap())
    return str(value)


def canonical_entry_hash(entry: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _plain(entry), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256(encoded)


def _without_managed_entry(document: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(_plain(document))
    servers = value.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop(SERVICE_NAME, None)
        if not servers:
            value.pop("mcp_servers", None)
    return value


def _decode_config(data: bytes) -> tuple[str, bool, str]:
    has_bom = data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text, has_bom, newline


def _encode_config(text: str, has_bom: bool, newline: str) -> bytes:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if newline == "\r\n":
        normalized = normalized.replace("\n", "\r\n")
    encoded = normalized.encode("utf-8")
    return (b"\xef\xbb\xbf" + encoded) if has_bom else encoded


def _parse_toml(data: bytes) -> tuple[Any, bool, str]:
    text, has_bom, newline = _decode_config(data)
    try:
        document = tomlkit.parse(text)
    except Exception as exc:
        raise RegistrationError(f"Codex config is not valid TOML: {exc}", EXIT_PREPARE_FAILED) from exc
    return document, has_bom, newline


def _parse_yaml(data: bytes) -> tuple[Any, bool, str]:
    text, has_bom, newline = _decode_config(data)
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.allow_duplicate_keys = False
    try:
        document = yaml.load(text)
    except Exception as exc:
        raise RegistrationError(f"Hermes config is not valid YAML: {exc}", EXIT_PREPARE_FAILED) from exc
    if document is None:
        document = CommentedMap()
    if not isinstance(document, Mapping):
        raise RegistrationError("Hermes config root must be a mapping.", EXIT_PREPARE_FAILED)
    return document, has_bom, newline


def _get_servers(document: Mapping[str, Any], *, client: str) -> Mapping[str, Any] | None:
    servers = document.get("mcp_servers")
    if servers is None:
        return None
    if not isinstance(servers, Mapping):
        raise RegistrationError(
            f"{client} mcp_servers must be a mapping.", EXIT_PREPARE_FAILED
        )
    return servers


def parse_config(client: str, data: bytes) -> Mapping[str, Any]:
    if client == "codex":
        document, _, _ = _parse_toml(data)
    elif client == "hermes":
        document, _, _ = _parse_yaml(data)
    else:
        raise ValueError(client)
    return document


def extract_managed_entry(client: str, data: bytes) -> Mapping[str, Any] | None:
    document = parse_config(client, data)
    servers = _get_servers(document, client=client)
    if servers is None:
        return None
    entry = servers.get(SERVICE_NAME)
    if entry is None:
        return None
    if not isinstance(entry, Mapping):
        raise RegistrationError(
            f"{client} {SERVICE_NAME} entry must be a mapping.", EXIT_PREPARE_FAILED
        )
    return _plain(entry)


def _tool_names(allowed_roots: Sequence[Path]) -> tuple[str, ...]:
    names = tuple(contract.name for contract in exposed_contracts(bool(allowed_roots)))
    if not names or len(names) != len(set(names)):
        raise RegistrationError("The generated MCP tool allowlist is empty or duplicated.", EXIT_PREPARE_FAILED)
    if any(any(char in name for char in "*?[") for name in names):
        raise RegistrationError("The MCP tool allowlist must contain exact names only.", EXIT_PREPARE_FAILED)
    return names


def _managed_env(allowed_roots: Sequence[Path], lease_path: Path, generation: str) -> dict[str, str]:
    return {
        ROOTS_ENV: json.dumps([str(path) for path in allowed_roots], ensure_ascii=False, separators=(",", ":")),
        LEASE_ENV: str(lease_path),
        GENERATION_ENV: generation,
    }


def build_managed_entry(
    client: str,
    request: RegistrationRequest,
    lease_path: Path,
    generation: str,
) -> dict[str, Any]:
    tools = list(_tool_names(request.allowed_roots))
    args = [*POWERSHELL_ARGS_PREFIX, str(request.launcher_path)]
    common: dict[str, Any] = {
        "command": str(request.powershell_path),
        "args": args,
        "env": _managed_env(request.allowed_roots, lease_path, generation),
    }
    if client == "codex":
        return {
            **common,
            "cwd": str(request.project_root),
            "enabled_tools": tools,
            "default_tools_approval_mode": "writes",
            "startup_timeout_sec": 15,
            "tool_timeout_sec": 360,
        }
    if client == "hermes":
        return {
            **common,
            "enabled": True,
            "trust": "untrusted",
            "timeout": 360,
            "connect_timeout": 15,
            "supports_parallel_tool_calls": False,
            "tools": {
                "include": tools,
                "resources": False,
                "prompts": False,
            },
        }
    raise ValueError(client)


def _validate_managed_entry(client: str, entry: Mapping[str, Any], expected_tools: frozenset[str]) -> None:
    command = entry.get("command")
    args = entry.get("args")
    env = entry.get("env")
    if not isinstance(command, str) or not Path(command).is_absolute():
        raise RegistrationError(f"{client} command is not absolute.", EXIT_PREPARE_FAILED)
    if not isinstance(args, list) or tuple(args[: len(POWERSHELL_ARGS_PREFIX)]) != POWERSHELL_ARGS_PREFIX:
        raise RegistrationError(f"{client} PowerShell arguments are not fixed.", EXIT_PREPARE_FAILED)
    if not isinstance(env, Mapping) or set(env) != {ROOTS_ENV, LEASE_ENV, GENERATION_ENV}:
        raise RegistrationError(f"{client} managed environment is incomplete.", EXIT_PREPARE_FAILED)
    if client == "codex":
        names = entry.get("enabled_tools")
        if entry.get("default_tools_approval_mode") != "writes":
            raise RegistrationError("Codex write approval policy is not fail-closed.", EXIT_PREPARE_FAILED)
        if entry.get("startup_timeout_sec") != 15 or entry.get("tool_timeout_sec") != 360:
            raise RegistrationError("Codex timeouts differ from the frozen contract.", EXIT_PREPARE_FAILED)
    else:
        policy = entry.get("tools")
        if not isinstance(policy, Mapping):
            raise RegistrationError("Hermes tools policy is missing.", EXIT_PREPARE_FAILED)
        names = policy.get("include")
        if entry.get("trust") != "untrusted":
            raise RegistrationError("Hermes must require approval for every write-capable call.", EXIT_PREPARE_FAILED)
        if policy.get("resources") is not False or policy.get("prompts") is not False:
            raise RegistrationError("Hermes resource/prompt utility tools must be disabled.", EXIT_PREPARE_FAILED)
        if entry.get("supports_parallel_tool_calls") is not False:
            raise RegistrationError("Hermes parallel calls must be disabled.", EXIT_PREPARE_FAILED)
        if entry.get("timeout") != 360 or entry.get("connect_timeout") != 15:
            raise RegistrationError("Hermes timeouts differ from the frozen contract.", EXIT_PREPARE_FAILED)
    if not isinstance(names, list) or not names:
        raise RegistrationError(
            f"{client} tool allowlist is empty; refusing fail-open registration.", EXIT_PREPARE_FAILED
        )
    if any(not isinstance(name, str) or any(char in name for char in "*?[") for name in names):
        raise RegistrationError(f"{client} allowlist must contain exact names.", EXIT_PREPARE_FAILED)
    if len(names) != len(set(names)) or frozenset(names) != expected_tools:
        raise RegistrationError(f"{client} allowlist differs from the MCP manifest.", EXIT_PREPARE_FAILED)


def render_config(client: str, original: bytes, entry: Mapping[str, Any] | None) -> bytes:
    if client == "codex":
        document, has_bom, newline = _parse_toml(original)
        before = _without_managed_entry(document)
        servers = _get_servers(document, client=client)
        if servers is None:
            servers_table = tomlkit.table()
            document["mcp_servers"] = servers_table
            servers = servers_table
        if entry is None:
            servers.pop(SERVICE_NAME, None)
        else:
            table = tomlkit.table()
            for key, value in entry.items():
                if key == "env":
                    env_table = tomlkit.table()
                    for env_key, env_value in value.items():
                        env_table.add(env_key, env_value)
                    table.add(key, env_table)
                else:
                    table.add(key, value)
            servers[SERVICE_NAME] = table
        rendered = tomlkit.dumps(document)
        result = _encode_config(rendered, has_bom, newline)
    elif client == "hermes":
        document, has_bom, newline = _parse_yaml(original)
        before = _without_managed_entry(document)
        servers = _get_servers(document, client=client)
        if servers is None:
            servers_map = CommentedMap()
            document["mcp_servers"] = servers_map
            servers = servers_map
        if entry is None:
            servers.pop(SERVICE_NAME, None)
        else:
            servers[SERVICE_NAME] = copy.deepcopy(entry)
        yaml = YAML(typ="rt")
        yaml.preserve_quotes = True
        yaml.allow_duplicate_keys = False
        yaml.default_flow_style = False
        yaml.line_break = newline
        stream = io.StringIO()
        yaml.dump(document, stream)
        result = _encode_config(stream.getvalue(), has_bom, newline)
    else:
        raise ValueError(client)

    reparsed = parse_config(client, result)
    if _without_managed_entry(reparsed) != before:
        raise RegistrationError(
            f"{client} staging changed semantics outside {SERVICE_NAME}.", EXIT_PREPARE_FAILED
        )
    staged_entry = extract_managed_entry(client, result)
    if (entry is None) != (staged_entry is None):
        raise RegistrationError(f"{client} staging did not apply the requested entry.", EXIT_PREPARE_FAILED)
    if entry is not None and canonical_entry_hash(staged_entry or {}) != canonical_entry_hash(entry):
        raise RegistrationError(f"{client} staging changed the managed entry.", EXIT_PREPARE_FAILED)
    return result


def _validate_local_absolute(path: Path, label: str, *, must_exist: bool) -> Path:
    raw = str(path)
    if not path.is_absolute() or raw.startswith("\\\\") or raw.startswith("//"):
        raise RegistrationError(f"{label} must be an absolute local path: {path}", EXIT_PREPARE_FAILED)
    resolved = path.resolve(strict=False)
    if must_exist and not resolved.exists():
        raise RegistrationError(f"{label} does not exist: {resolved}", EXIT_PREPARE_FAILED)
    return resolved


def normalize_allowed_roots(paths: Iterable[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            resolved = canonical_allowed_root(str(path))
        except (FileAccessError, OSError, RuntimeError) as exc:
            raise RegistrationError(
                f"AllowedRoot must be an ordinary fixed-disk directory without reparse points: {path}",
                EXIT_PREPARE_FAILED,
            ) from exc
        key = os.path.normcase(str(resolved))
        if key not in seen:
            result.append(resolved)
            seen.add(key)
    return tuple(result)


def _config_path(client: str) -> Path:
    if client == "codex":
        home = os.environ.get("CODEX_HOME")
        return (Path(home) if home else Path.home() / ".codex") / "config.toml"
    if client == "hermes":
        home = os.environ.get("HERMES_HOME")
        if home:
            return Path(home) / "config.yaml"
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) / "hermes" if local else Path.home() / ".hermes") / "config.yaml"
    raise ValueError(client)


def default_state_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RegistrationError("LOCALAPPDATA is required for the default StateRoot.", EXIT_PREPARE_FAILED)
    return Path(local) / "InvoiceAssistant" / "agent-registration"


def _default_resolve_executable(client: str) -> Path | None:
    candidates = (f"{client}.exe", client)
    for candidate in candidates:
        value = shutil.which(candidate)
        if value:
            return Path(value).resolve()
    return None


def _run_checked(command: Sequence[str], *, env: Mapping[str, str] | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(env) if env is not None else None,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CapabilityUnsupported(f"Capability probe failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no diagnostic").strip().splitlines()
        summary = detail[-1] if detail else "no diagnostic"
        raise CapabilityUnsupported(f"Capability probe failed ({completed.returncode}): {summary[:500]}")
    return completed


def _default_capability_probe(target: ClientTarget) -> None:
    if target.executable is None:
        raise CapabilityUnsupported(f"{target.name} executable is missing.")
    result = _run_checked([str(target.executable), "--version"], timeout=15)
    output = (result.stdout or result.stderr).strip()
    if not output:
        raise CapabilityUnsupported(f"{target.name} did not report a version.")
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", output)
    if match is None:
        raise CapabilityUnsupported(f"{target.name} reported an unparseable version.")
    version = tuple(int(part) for part in match.groups())
    if version not in SUPPORTED_CLIENT_VERSIONS[target.name]:
        supported = ", ".join(".".join(str(part) for part in value) for value in sorted(SUPPORTED_CLIENT_VERSIONS[target.name]))
        raise CapabilityUnsupported(
            f"{target.name} {'.'.join(str(part) for part in version)} has not passed the frozen approval/config gate; "
            f"validated version(s): {supported}."
        )
    if target.name == "hermes":
        _verify_hermes_approval_source(target.executable)


def _verify_hermes_approval_source(executable: Path) -> None:
    """Verify the inspected 0.20.1 trust gate is present beside the executable.

    This is a local source/config capability gate, not a claim that a real UI
    approval was observed.  The latter remains an explicit Windows acceptance
    step in the owning Plan.
    """

    candidates = [parent / "tools" / "mcp_tool.py" for parent in executable.parents[:3]]
    source_path = next((path for path in candidates if path.is_file()), None)
    if source_path is None:
        raise CapabilityUnsupported("Hermes approval source could not be located beside the validated executable.")
    try:
        source = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CapabilityUnsupported(f"Hermes approval source could not be read: {exc}") from exc
    required_fragments = (
        'def _trust_gate_check(server_name: str, tool_name: str)',
        'if _tool_read_only_hints.get(server_name, {}).get(tool_name) is True:',
        'from tools.approval import request_elicitation_consent',
        'Approve to run \'{tool_name}\' once, or deny to block it.',
        'gate_error = _trust_gate_check(server_name, tool_name)',
    )
    if any(fragment not in source for fragment in required_fragments):
        raise CapabilityUnsupported(
            "Hermes 0.20.1 source does not match the inspected fail-closed per-call MCP approval gate."
        )


def _default_client_validate(target: ClientTarget) -> None:
    if target.executable is None:
        raise CapabilityUnsupported(f"{target.name} executable is missing.")
    try:
        config_bytes = target.config_path.read_bytes()
        entry = extract_managed_entry(target.name, config_bytes)
    except (OSError, RegistrationError) as exc:
        raise CapabilityUnsupported(f"{target.name} could not parse the installed MCP config: {exc}") from exc
    if entry is None:
        raise CapabilityUnsupported(f"{target.name} did not retain the managed MCP entry.")
    if target.name == "codex":
        names_value = entry.get("enabled_tools")
    else:
        policy = entry.get("tools")
        names_value = policy.get("include") if isinstance(policy, Mapping) else None
    if not isinstance(names_value, list):
        raise CapabilityUnsupported(f"{target.name} did not retain an exact tool allowlist.")
    expected_tools = frozenset(str(name) for name in names_value)
    try:
        _validate_managed_entry(target.name, entry, expected_tools)
    except RegistrationError as exc:
        raise CapabilityUnsupported(f"{target.name} managed config failed the frozen field gate: {exc}") from exc

    env = os.environ.copy()
    if target.name == "codex":
        env["CODEX_HOME"] = str(target.config_path.parent)
        result = _run_checked(
            [str(target.executable), "mcp", "get", SERVICE_NAME, "--json"], env=env, timeout=30
        )
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CapabilityUnsupported("Codex did not return parseable MCP JSON.") from exc
        if not isinstance(parsed, Mapping):
            raise CapabilityUnsupported("Codex MCP result is not an object.")
        transport = parsed.get("transport")
        if not isinstance(transport, Mapping):
            raise CapabilityUnsupported("Codex MCP result omitted its transport.")
        expected_transport = {
            "type": "stdio",
            "command": entry["command"],
            "args": entry["args"],
            "env": entry["env"],
            "cwd": entry["cwd"],
        }
        for key, expected in expected_transport.items():
            if _plain(transport.get(key)) != _plain(expected):
                raise CapabilityUnsupported(f"Codex MCP result changed frozen transport field {key}.")
        if parsed.get("name") != SERVICE_NAME or parsed.get("enabled") is not True:
            raise CapabilityUnsupported("Codex did not report the managed MCP server as enabled.")
        if _plain(parsed.get("enabled_tools")) != _plain(entry["enabled_tools"]):
            raise CapabilityUnsupported("Codex did not retain the exact MCP tool allowlist.")
        if parsed.get("startup_timeout_sec") != entry["startup_timeout_sec"]:
            raise CapabilityUnsupported("Codex did not retain the frozen startup timeout.")
        if parsed.get("tool_timeout_sec") != entry["tool_timeout_sec"]:
            raise CapabilityUnsupported("Codex did not retain the frozen tool timeout.")
        # Codex 0.149.0 does not echo default_tools_approval_mode from `mcp
        # get --json`.  The exact TOML field was checked above, and the pinned
        # client build passed the owning Plan's separate real-session gate; do
        # not represent this parse check as fresh UI approval evidence.
    else:
        env["HERMES_HOME"] = str(target.config_path.parent)
        _run_checked(
            [str(target.executable), "mcp", "test", SERVICE_NAME], env=env, timeout=390
        )


def _default_staged_client_validate(target: ClientTarget, data: bytes, state_root: Path) -> None:
    filename = "config.toml" if target.name == "codex" else "config.yaml"
    with tempfile.TemporaryDirectory(prefix=f"preflight-{target.name}-", dir=state_root) as raw_home:
        config_path = Path(raw_home) / filename
        _flush_file(config_path, data)
        _default_client_validate(ClientTarget(target.name, config_path, target.executable))


async def _list_tools(entry: Mapping[str, Any]) -> frozenset[str]:
    env = os.environ.copy()
    env.update({str(key): str(value) for key, value in entry["env"].items()})
    params = StdioServerParameters(
        command=str(entry["command"]),
        args=[str(value) for value in entry["args"]],
        env=env,
        cwd=entry.get("cwd"),
    )
    async with asyncio.timeout(MCP_DISCOVERY_TIMEOUT_SECONDS):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return frozenset(tool.name for tool in result.tools)


def _default_mcp_validate(_client: str, entry: Mapping[str, Any], expected: frozenset[str]) -> None:
    try:
        actual = asyncio.run(_list_tools(entry))
    except Exception as exc:
        raise CapabilityUnsupported(f"Independent MCP discovery failed: {exc}") from exc
    if actual != expected:
        raise CapabilityUnsupported(
            f"Independent MCP discovery differed from the manifest: expected {len(expected)}, got {len(actual)}."
        )


def _default_secure_state_root(path: Path, powershell_path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)
        return
    script = r"""
$ErrorActionPreference = 'Stop'
$target = [IO.Path]::GetFullPath($env:INVOICE_REGISTRATION_ACL_TARGET)
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetOwner($identity.User)
$acl.SetAccessRuleProtection($true, $false)
$inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
$propagation = [Security.AccessControl.PropagationFlags]::None
$rights = [Security.AccessControl.FileSystemRights]::FullControl
$allow = [Security.AccessControl.AccessControlType]::Allow
foreach ($sidText in @($identity.User.Value, 'S-1-5-18', 'S-1-5-32-544')) {
    $sid = New-Object Security.Principal.SecurityIdentifier($sidText)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, $rights, $inheritance, $propagation, $allow)
    [void]$acl.AddAccessRule($rule)
}
[IO.Directory]::SetAccessControl($target, $acl)
$check = [IO.Directory]::GetAccessControl($target, [Security.AccessControl.AccessControlSections]::Access)
$allowed = @($identity.User.Value, 'S-1-5-18', 'S-1-5-32-544')
foreach ($rule in $check.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
    if ($rule.IsInherited -or $rule.AccessControlType -ne $allow -or $allowed -notcontains $rule.IdentityReference.Value) {
        throw "StateRoot ACL is not private"
    }
}
"""
    acl_env = os.environ.copy()
    acl_env["INVOICE_REGISTRATION_ACL_TARGET"] = str(path)
    completed = subprocess.run(
        [
            str(powershell_path),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=acl_env,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "ACL command failed").strip()
        raise RegistrationError(f"Could not secure StateRoot: {detail[:500]}", EXIT_PREPARE_FAILED)


def default_services() -> RegistrationServices:
    return RegistrationServices(
        resolve_executable=_default_resolve_executable,
        capability_probe=_default_capability_probe,
        client_validate=_default_client_validate,
        mcp_validate=_default_mcp_validate,
        secure_state_root=_default_secure_state_root,
        staged_client_validate=_default_staged_client_validate,
    )


class FileLockSet(AbstractContextManager["FileLockSet"]):
    def __init__(self, targets: Iterable[Path]) -> None:
        paths = {
            target.parent / f"{target.name}.invoice-assistant.lock"
            for target in targets
        }
        self.paths = sorted(paths, key=lambda path: os.path.normcase(str(path.resolve(strict=False))))
        self.handles: list[tuple[Path, Any]] = []

    def __enter__(self) -> "FileLockSet":
        try:
            for path in self.paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(path, "a+b")
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.handles.append((path, handle))
        except (OSError, IOError) as exc:
            self.__exit__(None, None, None)
            raise RegistrationError(f"Could not acquire registration lock: {exc}", EXIT_PREPARE_FAILED) from exc
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        for path, handle in reversed(self.handles):
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
            try:
                path.unlink()
            except OSError:
                pass
        self.handles.clear()


def _flush_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_existing(path: Path) -> None:
    # Windows rejects FlushFileBuffers for a read-only handle. Open without
    # truncation and with write access so the copied backup is durable.
    with open(path, "r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    stage = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        _flush_file(stage, _json_bytes(value))
        os.replace(stage, path)
    except OSError as exc:
        try:
            stage.unlink(missing_ok=True)
        except OSError:
            pass
        raise RegistrationError(
            f"Could not persist transaction metadata {path}: {exc}", EXIT_PREPARE_FAILED
        ) from exc


def _replace_with_backup(stage: Path, target: Path, backup: Path) -> None:
    if not target.exists():
        os.replace(stage, target)
        return
    if os.name != "nt":
        shutil.copyfile(target, backup)
        _fsync_existing(backup)
        os.replace(stage, target)
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    replace_file.restype = ctypes.c_int
    if not replace_file(str(target), str(stage), str(backup), 0, None, None):
        error = ctypes.get_last_error()
        raise OSError(error, f"ReplaceFileW failed for {target}")


def _current_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    return _sha256(path.read_bytes())


def _state_path(state_root: Path) -> Path:
    return state_root / "managed-state.json"


def _journal_path(state_root: Path) -> Path:
    return state_root / "transaction-journal.json"


def _lease_path(state_root: Path, client: str) -> Path:
    return state_root / "leases" / f"{client}.json"


def _empty_state(state_root: Path) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "state_root": str(state_root),
        "targets": {},
    }


def _load_state(state_root: Path) -> dict[str, Any]:
    path = _state_path(state_root)
    if not path.exists():
        return _empty_state(state_root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegistrationError(f"Managed state cannot be read: {exc}", EXIT_PREPARE_FAILED) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != STATE_SCHEMA_VERSION
        or value.get("state_root") != str(state_root)
        or not isinstance(value.get("targets"), dict)
    ):
        raise RegistrationError("Managed state has an unsupported shape or StateRoot.", EXIT_PREPARE_FAILED)
    return value


def _lease_bytes(
    client: str,
    generation: str,
    active: bool,
    managed_state_path: Path | None = None,
) -> bytes:
    value: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "client": client,
        "generation": generation,
        "active": active,
        "updated_at": _utc_now(),
    }
    if managed_state_path is not None:
        value["managed_state_path"] = str(managed_state_path)
    return _json_bytes(
        value
    )


def _lease_matches(
    path: Path,
    client: str,
    generation: str,
    managed_state_path: Path | None = None,
) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not (
        isinstance(value, dict)
        and value.get("schema_version") == STATE_SCHEMA_VERSION
        and value.get("client") == client
        and value.get("generation") == generation
        and value.get("active") is True
    ):
        return False
    if managed_state_path is None:
        return True
    if value.get("managed_state_path") != str(managed_state_path):
        return False
    try:
        state = json.loads(managed_state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    targets = state.get("targets") if isinstance(state, dict) else None
    target = targets.get(client) if isinstance(targets, dict) else None
    return (
        isinstance(target, dict)
        and target.get("generation") == generation
        and os.path.normcase(str(Path(str(target.get("lease_path"))).resolve(strict=False)))
        == os.path.normcase(str(path.resolve(strict=False)))
    )


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes() if path.exists() else b""
    except OSError as exc:
        raise RegistrationError(f"Could not read {path}: {exc}", EXIT_PREPARE_FAILED) from exc


def _new_change(path: Path, desired: bytes | None, transaction_id: str) -> FileChange:
    exists = path.exists()
    original = _read_bytes(path) if exists else b""
    return FileChange(
        path=path,
        original_exists=exists,
        original_sha256=_sha256(original) if exists else None,
        desired_bytes=desired,
        written_sha256=_sha256(desired) if desired is not None else None,
        stage_path=path.parent / f".{path.name}.{transaction_id}.stage",
        backup_path=path.parent / f".{path.name}.{transaction_id}.backup",
    )


def _journal_value(transaction_id: str, phase: str, changes: Sequence[FileChange]) -> dict[str, Any]:
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "phase": phase,
        "updated_at": _utc_now(),
        "files": [
            {
                "path": str(change.path),
                "original_exists": change.original_exists,
                "original_sha256": change.original_sha256,
                "written_sha256": change.written_sha256,
                "stage_path": str(change.stage_path),
                "backup_path": str(change.backup_path),
                "replace_intent": change.replace_intent,
                "replaced": change.replaced,
            }
            for change in changes
        ],
    }


def _changes_from_journal(value: Mapping[str, Any]) -> list[FileChange]:
    if value.get("schema_version") != JOURNAL_SCHEMA_VERSION or not isinstance(value.get("files"), list):
        raise RegistrationError("Registration journal has an unsupported shape.", EXIT_RECOVERY_INCOMPLETE)
    result: list[FileChange] = []
    for item in value["files"]:
        if not isinstance(item, Mapping):
            raise RegistrationError("Registration journal file entry is invalid.", EXIT_RECOVERY_INCOMPLETE)
        try:
            result.append(
                FileChange(
                    path=Path(str(item["path"])),
                    original_exists=bool(item["original_exists"]),
                    original_sha256=item.get("original_sha256"),
                    desired_bytes=None,
                    written_sha256=item.get("written_sha256"),
                    stage_path=Path(str(item["stage_path"])),
                    backup_path=Path(str(item["backup_path"])),
                    replace_intent=bool(item.get("replace_intent", False)),
                    replaced=bool(item.get("replaced", False)),
                )
            )
        except KeyError as exc:
            raise RegistrationError("Registration journal file entry is incomplete.", EXIT_RECOVERY_INCOMPLETE) from exc
    return result


def _load_journal(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegistrationError(f"Registration journal cannot be read: {exc}", EXIT_RECOVERY_INCOMPLETE) from exc
    if not isinstance(value, dict):
        raise RegistrationError("Registration journal root is invalid.", EXIT_RECOVERY_INCOMPLETE)
    return value


def _cleanup_transaction_files(changes: Sequence[FileChange], journal_path: Path) -> None:
    failures: list[str] = []
    for change in changes:
        for path in (change.stage_path, change.backup_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                failures.append(str(path))
    if not failures:
        try:
            journal_path.unlink(missing_ok=True)
        except OSError:
            failures.append(str(journal_path))
    if failures:
        raise RegistrationError(
            "Sensitive transaction cleanup is incomplete: " + ", ".join(failures),
            EXIT_RECOVERY_INCOMPLETE,
        )


def _restore_changes(changes: Sequence[FileChange], journal_path: Path) -> None:
    for change in reversed(changes):
        current = _current_hash(change.path)
        if current == change.original_sha256 and current is not None:
            continue
        if not change.original_exists and current is None:
            continue
        if current != change.written_sha256:
            if not change.replace_intent and not change.replaced:
                continue
            raise RegistrationError(
                f"CAS recovery refused to overwrite an external change: {change.path}",
                EXIT_RECOVERY_INCOMPLETE,
            )
        if change.original_exists:
            if not change.backup_path.exists():
                raise RegistrationError(
                    f"Recovery backup is missing: {change.backup_path}", EXIT_RECOVERY_INCOMPLETE
                )
            recovery_stage = change.path.parent / f".{change.path.name}.{uuid.uuid4().hex}.restore"
            shutil.copyfile(change.backup_path, recovery_stage)
            _fsync_existing(recovery_stage)
            replacement_backup = change.path.parent / f".{change.path.name}.{uuid.uuid4().hex}.discard"
            _replace_with_backup(recovery_stage, change.path, replacement_backup)
            replacement_backup.unlink(missing_ok=True)
        else:
            change.path.unlink()
    _cleanup_transaction_files(changes, journal_path)


def recover_pending_transaction(state_root: Path) -> None:
    try:
        journal_path = _journal_path(state_root)
        initial = _load_journal(journal_path)
        if initial is None:
            return
        changes = _changes_from_journal(initial)
        lock_targets = [journal_path, _state_path(state_root), *(change.path for change in changes)]
        with FileLockSet(lock_targets):
            value = _load_journal(journal_path)
            if value is None:
                return
            changes = _changes_from_journal(value)
            if value.get("phase") == "committed":
                for change in changes:
                    if _current_hash(change.path) != change.written_sha256:
                        raise RegistrationError(
                            f"Committed transaction no longer matches {change.path}.",
                            EXIT_RECOVERY_INCOMPLETE,
                        )
                _cleanup_transaction_files(changes, journal_path)
                return
            if value.get("phase") != "prepared":
                raise RegistrationError("Registration journal phase is invalid.", EXIT_RECOVERY_INCOMPLETE)
            _restore_changes(changes, journal_path)
    except RegistrationError:
        raise
    except OSError as exc:
        raise RegistrationError(
            f"Registration recovery could not complete: {exc}", EXIT_RECOVERY_INCOMPLETE
        ) from exc


def _apply_transaction(
    changes: Sequence[FileChange],
    state_root: Path,
    services: RegistrationServices,
    post_validate: Callable[[], None],
    *,
    activation_changes: Sequence[FileChange] = (),
    final_validate: Callable[[], None] = lambda: None,
) -> None:
    changes = [change for change in changes if not change.is_noop]
    activation_changes = [change for change in activation_changes if not change.is_noop]
    all_changes = [*changes, *activation_changes]
    if not all_changes:
        return
    journal_path = _journal_path(state_root)
    transaction_id = uuid.uuid4().hex
    wrote_any = False
    committed = False

    def replace(change: FileChange) -> None:
        nonlocal wrote_any
        if _current_hash(change.path) != change.original_sha256:
            raise RegistrationError(
                f"Concurrent modification detected before replace: {change.path}",
                EXIT_PREPARE_FAILED,
            )
        change.replace_intent = True
        _atomic_json_write(journal_path, _journal_value(transaction_id, "prepared", all_changes))
        services.failure_hook("replace_intent", str(change.path))
        if _current_hash(change.path) != change.original_sha256:
            change.replace_intent = False
            _atomic_json_write(journal_path, _journal_value(transaction_id, "prepared", all_changes))
            raise RegistrationError(
                f"Concurrent modification detected before replace: {change.path}",
                EXIT_PREPARE_FAILED,
            )
        if change.desired_bytes is None:
            change.backup_path.parent.mkdir(parents=True, exist_ok=True)
            if change.original_exists:
                shutil.copyfile(change.path, change.backup_path)
                _fsync_existing(change.backup_path)
                change.path.unlink()
        else:
            _replace_with_backup(change.stage_path, change.path, change.backup_path)
        wrote_any = True
        change.replaced = True
        _atomic_json_write(journal_path, _journal_value(transaction_id, "prepared", all_changes))
        services.failure_hook("replaced", str(change.path))
        if change.path == _state_path(state_root):
            services.failure_hook("state_written", str(change.path))

    try:
        _atomic_json_write(journal_path, _journal_value(transaction_id, "prepared", all_changes))
        services.failure_hook("journal_prepared", None)
        for change in all_changes:
            if change.desired_bytes is not None:
                _flush_file(change.stage_path, change.desired_bytes)
                if _sha256(change.stage_path.read_bytes()) != change.written_sha256:
                    raise RegistrationError(
                        f"Staging verification failed: {change.path}", EXIT_PREPARE_FAILED
                    )
                services.failure_hook("flushed", str(change.path))
            services.failure_hook("staged", str(change.path))

        for change in changes:
            replace(change)

        post_validate()
        services.failure_hook("post_validated", None)
        for change in activation_changes:
            replace(change)
        final_validate()
        services.failure_hook("activated", None)
        _atomic_json_write(journal_path, _journal_value(transaction_id, "committed", all_changes))
        committed = True
        services.failure_hook("committed", None)
        services.failure_hook("journal_cleanup", None)
        _cleanup_transaction_files(all_changes, journal_path)
    except SimulatedInterruption:
        raise
    except BaseException as exc:
        if committed:
            if isinstance(exc, RegistrationError) and exc.exit_code == EXIT_RECOVERY_INCOMPLETE:
                raise exc
            raise RegistrationError(
                f"Registration committed, but sensitive transaction cleanup is incomplete: {exc}",
                EXIT_RECOVERY_INCOMPLETE,
            ) from exc
        try:
            current = _load_journal(journal_path)
            rollback_changes = _changes_from_journal(current) if current is not None else list(all_changes)
            _restore_changes(rollback_changes, journal_path)
        except RegistrationError:
            raise
        if not wrote_any:
            if isinstance(exc, RegistrationError):
                raise exc
            raise RegistrationError(
                f"Registration staging or replace preparation failed: {exc}", EXIT_PREPARE_FAILED
            ) from exc
        message = f"Post-write validation failed and changes were restored: {exc}"
        raise RegistrationError(message, EXIT_POST_WRITE_ROLLED_BACK) from exc


def _target_state(
    client: str,
    config_path: Path,
    entry: Mapping[str, Any],
    generation: str,
    lease_path: Path,
) -> dict[str, Any]:
    return {
        "client": client,
        "config_path": str(config_path),
        "canonical_hash": canonical_entry_hash(entry),
        "generation": generation,
        "lease_path": str(lease_path),
        "registered_at": _utc_now(),
    }


def _target_from_state(client: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RegistrationError(f"Managed state for {client} is invalid.", EXIT_PREPARE_FAILED)
    required = {"client", "config_path", "canonical_hash", "generation", "lease_path"}
    if not required.issubset(value) or value.get("client") != client:
        raise RegistrationError(f"Managed state for {client} is incomplete.", EXIT_PREPARE_FAILED)
    return value


def _revoke_lease(state_value: Mapping[str, Any]) -> None:
    lease_path = Path(str(state_value["lease_path"]))
    generation = uuid.uuid4().hex
    desired = _lease_bytes(
        str(state_value["client"]),
        generation,
        False,
        lease_path.parent.parent / "managed-state.json",
    )
    stage = lease_path.parent / f".{lease_path.name}.{uuid.uuid4().hex}.revoke"
    try:
        _flush_file(stage, desired)
        os.replace(stage, lease_path)
    except OSError as exc:
        try:
            stage.unlink(missing_ok=True)
        except OSError:
            pass
        raise RegistrationError(f"Could not revoke stale registration lease: {exc}", EXIT_RECOVERY_INCOMPLETE) from exc


def _validate_request(request: RegistrationRequest) -> RegistrationRequest:
    if request.agent not in {"all", "codex", "hermes"}:
        raise RegistrationError(f"Unsupported Agent: {request.agent}", EXIT_PREPARE_FAILED)
    state_root = _validate_local_absolute(request.state_root, "StateRoot", must_exist=False)
    project_root = _validate_local_absolute(request.project_root, "project root", must_exist=True)
    powershell = _validate_local_absolute(request.powershell_path, "PowerShell", must_exist=True)
    launcher = _validate_local_absolute(request.launcher_path, "MCP launcher", must_exist=True)
    if launcher.parent != project_root:
        raise RegistrationError("MCP launcher must be in the project root.", EXIT_PREPARE_FAILED)
    roots = normalize_allowed_roots(request.allowed_roots)
    return RegistrationRequest(
        agent=request.agent,
        allowed_roots=roots,
        state_root=state_root,
        project_root=project_root,
        powershell_path=powershell,
        launcher_path=launcher,
        remove=request.remove,
    )


def _selected_clients(agent: str) -> tuple[str, ...]:
    return ("codex", "hermes") if agent == "all" else (agent,)


def execute_registration(
    request: RegistrationRequest,
    services: RegistrationServices | None = None,
) -> int:
    services = services or default_services()
    try:
        request = _validate_request(request)
        services.secure_state_root(request.state_root, request.powershell_path)
        recover_pending_transaction(request.state_root)
        state = _load_state(request.state_root)
        selected = _selected_clients(request.agent)
        if request.remove:
            return _execute_remove(request, state, selected, services)
        return _execute_register(request, state, selected, services)
    except RegistrationError as exc:
        print(f"registration: {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:
        print(f"registration: unexpected preparation failure: {exc}", file=sys.stderr)
        return EXIT_PREPARE_FAILED


def _execute_register(
    request: RegistrationRequest,
    state: dict[str, Any],
    selected: Sequence[str],
    services: RegistrationServices,
) -> int:
    targets: list[ClientTarget] = []
    missing_new: list[str] = []
    for client in selected:
        managed = state["targets"].get(client)
        executable = services.resolve_executable(client)
        if managed is not None and executable is None:
            _revoke_lease(_target_from_state(client, managed))
            raise RegistrationError(
                f"{client} executable is missing; its lease was revoked. Run -Remove to clean the entry.",
                EXIT_CLIENT_MISSING,
            )
        if managed is None and executable is None:
            missing_new.append(client)
            continue
        targets.append(
            ClientTarget(client, _config_path(client).resolve(strict=False), executable)
        )
    if missing_new and request.agent != "all":
        raise RegistrationError(f"{missing_new[0]} is not installed.", EXIT_CLIENT_MISSING)
    if not targets:
        raise RegistrationError("No supported agent client is installed.", EXIT_CLIENT_MISSING)

    state_path = _state_path(request.state_root)
    lock_targets = [state_path, _journal_path(request.state_root)]
    lock_targets.extend(target.config_path for target in targets)
    lock_targets.extend(_lease_path(request.state_root, target.name) for target in targets)
    with FileLockSet(lock_targets):
        services.failure_hook("lock_acquired", None)
        state = _load_state(request.state_root)
        config_changes: list[FileChange] = []
        lease_changes: list[FileChange] = []
        new_state = copy.deepcopy(state)
        entries: dict[str, Mapping[str, Any]] = {}
        changed_targets: list[ClientTarget] = []
        transaction_id = uuid.uuid4().hex
        expected_tools = frozenset(_tool_names(request.allowed_roots))

        for target in targets:
            original = _read_bytes(target.config_path)
            existing_entry = extract_managed_entry(target.name, original) if target.config_path.exists() else None
            services.failure_hook("parsed", str(target.config_path))
            saved = state["targets"].get(target.name)
            if saved is None and existing_entry is not None:
                raise RegistrationError(
                    f"Unmanaged {SERVICE_NAME} already exists in {target.name}; rename or remove it manually.",
                    EXIT_CONFLICT_OR_DRIFT,
                )
            saved_value = _target_from_state(target.name, saved) if saved is not None else None
            if saved_value is not None:
                if Path(str(saved_value["config_path"])) != target.config_path:
                    raise RegistrationError(
                        f"{target.name} is managed from another StateRoot/config path; remove it there first.",
                        EXIT_CONFLICT_OR_DRIFT,
                    )
                if existing_entry is None or canonical_entry_hash(existing_entry) != saved_value["canonical_hash"]:
                    _revoke_lease(saved_value)
                    raise RegistrationError(
                        f"Managed {target.name} entry drifted; user bytes were preserved and the lease was revoked.",
                        EXIT_CONFLICT_OR_DRIFT,
                    )
                generation = str(saved_value["generation"])
            else:
                generation = uuid.uuid4().hex
            lease_path = _lease_path(request.state_root, target.name).resolve(strict=False)
            desired_entry = build_managed_entry(target.name, request, lease_path, generation)
            _validate_managed_entry(target.name, desired_entry, expected_tools)
            entry_matches = (
                existing_entry is not None
                and canonical_entry_hash(existing_entry) == canonical_entry_hash(desired_entry)
            )
            lease_matches = (
                saved_value is not None
                and _lease_matches(lease_path, target.name, generation, state_path)
            )
            if entry_matches and lease_matches:
                entries[target.name] = desired_entry
                continue

            if saved_value is not None:
                generation = uuid.uuid4().hex
                desired_entry = build_managed_entry(target.name, request, lease_path, generation)
                _validate_managed_entry(target.name, desired_entry, expected_tools)
            rendered = render_config(target.name, original, desired_entry)
            staged_entry = extract_managed_entry(target.name, rendered)
            _validate_managed_entry(target.name, staged_entry or {}, expected_tools)
            config_changes.append(_new_change(target.config_path, rendered, transaction_id))
            lease_changes.append(
                _new_change(
                    lease_path,
                    _lease_bytes(target.name, generation, True, state_path),
                    transaction_id,
                )
            )
            new_state["targets"][target.name] = _target_state(
                target.name, target.config_path, desired_entry, generation, lease_path
            )
            entries[target.name] = desired_entry
            changed_targets.append(target)

        # Re-probe no-op targets too.  A client downgrade/upgrade must not keep
        # a previously valid registration alive without passing the frozen
        # config and approval capability gate again.
        for target in targets:
            services.capability_probe(target)
            if target in changed_targets:
                rendered = next(change.desired_bytes for change in config_changes if change.path == target.config_path)
                assert rendered is not None
                services.staged_client_validate(target, rendered, request.state_root)
            else:
                services.client_validate(target)
            services.mcp_validate(target.name, entries[target.name], expected_tools)
            services.failure_hook("client_validated", target.name)

        if not changed_targets:
            return EXIT_OK
        state_change = _new_change(state_path, _json_bytes(new_state), transaction_id)
        # Rotate the lease first.  The lease's managed-state gate makes both
        # the old process (generation mismatch) and a candidate new process
        # (state still names the old generation) stale until post-validation
        # succeeds and state is activated last.
        changes = [*lease_changes, *config_changes]

        def post_validate() -> None:
            for target in changed_targets:
                data = target.config_path.read_bytes()
                current_entry = extract_managed_entry(target.name, data)
                if current_entry is None or canonical_entry_hash(current_entry) != canonical_entry_hash(entries[target.name]):
                    raise CapabilityUnsupported(f"{target.name} did not preserve the managed entry.")
                _validate_managed_entry(target.name, current_entry, expected_tools)
                services.client_validate(target)
                services.mcp_validate(target.name, current_entry, expected_tools)
                services.failure_hook("client_validated", target.name)

        def final_validate() -> None:
            committed_state = json.loads(state_path.read_text(encoding="utf-8"))
            if committed_state != new_state:
                raise CapabilityUnsupported("Managed state did not survive atomic replacement.")
            for target in changed_targets:
                saved = new_state["targets"][target.name]
                if not _lease_matches(
                    Path(str(saved["lease_path"])),
                    target.name,
                    str(saved["generation"]),
                    state_path,
                ):
                    raise CapabilityUnsupported(f"{target.name} lease did not activate with managed state.")

        _apply_transaction(
            changes,
            request.state_root,
            services,
            post_validate,
            activation_changes=[state_change],
            final_validate=final_validate,
        )
    return EXIT_OK


def _execute_remove(
    request: RegistrationRequest,
    state: dict[str, Any],
    selected: Sequence[str],
    services: RegistrationServices,
) -> int:
    managed_selected = [client for client in selected if client in state["targets"]]
    if not managed_selected:
        return EXIT_OK
    state_path = _state_path(request.state_root)
    saved_values = {
        client: _target_from_state(client, state["targets"][client]) for client in managed_selected
    }
    lock_targets = [state_path, _journal_path(request.state_root)]
    lock_targets.extend(Path(str(value["config_path"])) for value in saved_values.values())
    lock_targets.extend(Path(str(value["lease_path"])) for value in saved_values.values())
    with FileLockSet(lock_targets):
        services.failure_hook("lock_acquired", None)
        state = _load_state(request.state_root)
        saved_values = {
            client: _target_from_state(client, state["targets"][client])
            for client in managed_selected
            if client in state["targets"]
        }
        transaction_id = uuid.uuid4().hex
        config_changes: list[FileChange] = []
        lease_changes: list[FileChange] = []
        new_state = copy.deepcopy(state)
        expected_removed: list[tuple[str, Path]] = []

        for client, saved in saved_values.items():
            config_path = Path(str(saved["config_path"]))
            lease_path = Path(str(saved["lease_path"]))
            if config_path.exists():
                original = _read_bytes(config_path)
                entry = extract_managed_entry(client, original)
                services.failure_hook("parsed", str(config_path))
                if entry is None or canonical_entry_hash(entry) != saved["canonical_hash"]:
                    _revoke_lease(saved)
                    raise RegistrationError(
                        f"Managed {client} entry drifted; user bytes were preserved and the lease was revoked.",
                        EXIT_CONFLICT_OR_DRIFT,
                    )
                rendered = render_config(client, original, None)
                config_changes.append(_new_change(config_path, rendered, transaction_id))
                expected_removed.append((client, config_path))
            generation = uuid.uuid4().hex
            lease_changes.append(
                _new_change(
                    lease_path,
                    _lease_bytes(client, generation, False, state_path),
                    transaction_id,
                )
            )
            new_state["targets"].pop(client, None)

        desired_state = _json_bytes(new_state) if new_state["targets"] else None
        state_change = _new_change(state_path, desired_state, transaction_id)
        changes = [*lease_changes, *config_changes]

        def post_validate() -> None:
            for client, config_path in expected_removed:
                if extract_managed_entry(client, config_path.read_bytes()) is not None:
                    raise CapabilityUnsupported(f"{client} entry remained after removal.")
            for client, saved in saved_values.items():
                lease = json.loads(Path(str(saved["lease_path"])).read_text(encoding="utf-8"))
                if lease.get("active") is not False:
                    raise CapabilityUnsupported(f"{client} lease was not revoked.")

        def final_validate() -> None:
            if desired_state is None:
                if state_path.exists():
                    raise CapabilityUnsupported("Managed state remained after removal.")
            elif json.loads(state_path.read_text(encoding="utf-8")) != new_state:
                raise CapabilityUnsupported("Managed state was not updated after removal.")

        _apply_transaction(
            changes,
            request.state_root,
            services,
            post_validate,
            activation_changes=[state_change],
            final_validate=final_validate,
        )
    return EXIT_OK


def _powershell_path() -> Path:
    system_root = os.environ.get("SystemRoot")
    if system_root:
        candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.exists():
            return candidate
    found = shutil.which("powershell.exe") or shutil.which("powershell")
    if found:
        return Path(found)
    return Path("powershell.exe")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely register the local invoice MCP server.")
    parser.add_argument("--agent", choices=("all", "codex", "hermes"), default="all")
    parser.add_argument("--allowed-root", action="append", default=[])
    parser.add_argument("--state-root")
    parser.add_argument("--project-root")
    parser.add_argument("--powershell-path")
    parser.add_argument("--launcher-path")
    parser.add_argument("--remove", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(args.project_root) if args.project_root else Path(__file__).resolve().parents[3]
    state_root = Path(args.state_root) if args.state_root else default_state_root()
    powershell = Path(args.powershell_path) if args.powershell_path else _powershell_path()
    launcher = Path(args.launcher_path) if args.launcher_path else project_root / "run-agent-mcp.ps1"
    request = RegistrationRequest(
        agent=args.agent,
        allowed_roots=tuple(Path(value) for value in args.allowed_root),
        state_root=state_root,
        project_root=project_root,
        powershell_path=powershell,
        launcher_path=launcher,
        remove=args.remove,
    )
    code = execute_registration(request)
    if code == EXIT_OK:
        action = "removed" if request.remove else "registered"
        print(
            f"invoice_assistant {action}. Close/refresh the selected clients and start a new session; "
            "old sessions are protected by a rotated or revoked lease."
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
