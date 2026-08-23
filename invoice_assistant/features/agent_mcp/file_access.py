from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_FILE_SIZE = 20 * 1024 * 1024
ALLOWED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
DRIVE_FIXED = 3


class FileAccessError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class VerifiedFile:
    content: bytes
    sha256: str
    display_basename: str
    mime_type: str
    size_bytes: int


def _windows_attributes(path: Path) -> int:
    if os.name != "nt":
        return 0
    get_attributes = ctypes.windll.kernel32.GetFileAttributesW
    get_attributes.argtypes = [ctypes.c_wchar_p]
    get_attributes.restype = ctypes.c_uint32
    return int(get_attributes(str(path)))


def _is_reparse(path: Path) -> bool:
    attributes = _windows_attributes(path)
    return attributes != INVALID_FILE_ATTRIBUTES and bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _components(path: Path) -> list[Path]:
    current = Path(path.anchor)
    result = [current]
    for part in path.parts[1:]:
        current = current / part
        result.append(current)
    return result


def _has_reparse_component(path: Path) -> bool:
    return any(_is_reparse(component) for component in _components(path))


def _is_fixed_local_drive(path: Path) -> bool:
    if os.name != "nt":
        return True
    root = f"{path.drive}\\"
    get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
    get_drive_type.argtypes = [ctypes.c_wchar_p]
    get_drive_type.restype = ctypes.c_uint
    return int(get_drive_type(root)) == DRIVE_FIXED


def canonical_allowed_root(value: str) -> Path:
    raw = str(value)
    if not raw or raw.startswith(("\\\\", "\\\\?\\", "\\\\.\\")):
        raise FileAccessError("invalid_allowed_roots", "Allowed roots must be ordinary absolute local directories.")
    path = Path(raw)
    if not path.is_absolute() or not path.drive or ":" in raw[2:]:
        raise FileAccessError("invalid_allowed_roots", "Allowed roots must be ordinary absolute local directories.")
    # Check the operator-supplied path before resolving it.  Path.resolve()
    # removes junction/symlink components, which would otherwise turn an
    # explicitly forbidden reparse root into an apparently ordinary target.
    if _has_reparse_component(path):
        raise FileAccessError("invalid_allowed_roots", "Allowed roots must not contain reparse points.")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or not _is_fixed_local_drive(resolved) or _has_reparse_component(resolved):
        raise FileAccessError("invalid_allowed_roots", "Allowed roots must be ordinary fixed-disk directories without reparse points.")
    return resolved


def parse_allowed_roots(raw_json: str | None) -> tuple[Path, ...]:
    if raw_json is None:
        return ()
    try:
        values = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(values, list) or any(not isinstance(entry, str) for entry in values):
        return ()
    roots: list[Path] = []
    seen: set[str] = set()
    try:
        for value in values:
            root = canonical_allowed_root(value)
            key = os.path.normcase(str(root))
            if key not in seen:
                roots.append(root)
                seen.add(key)
    except (FileAccessError, OSError, RuntimeError):
        return ()
    return tuple(roots)


def _within_root(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(str(path)), os.path.normcase(str(root))]) == os.path.normcase(str(root))
    except (OSError, ValueError):
        return False


def _final_path_for_fd(fd: int, fallback: Path) -> Path:
    if os.name != "nt":
        return fallback.resolve(strict=True)
    import msvcrt

    handle = msvcrt.get_osfhandle(fd)
    get_final_path = ctypes.windll.kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    get_final_path.restype = ctypes.c_uint32
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    written = int(get_final_path(handle, buffer, size, 0))
    if written == 0 or written >= size:
        raise FileAccessError("file_identity_unavailable", "The file identity could not be verified.")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value).resolve(strict=True)


def _display_basename(path: Path) -> str:
    normalized = unicodedata.normalize("NFC", path.name)
    name = "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in {"Cc", "Cf"}
    )
    return name[:255] or f"document{path.suffix.lower()}"


def _validate_magic(content: bytes, suffix: str) -> None:
    valid = False
    if suffix == ".pdf":
        valid = content.startswith(b"%PDF-")
    elif suffix == ".png":
        valid = content.startswith(b"\x89PNG\r\n\x1a\n")
    elif suffix in {".jpg", ".jpeg"}:
        valid = content.startswith(b"\xff\xd8\xff")
    elif suffix == ".webp":
        valid = len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    if not valid:
        raise FileAccessError("file_type_mismatch", "The file content does not match its supported extension.")


def read_verified_file(raw_path: Any, allowed_roots: tuple[Path, ...]) -> VerifiedFile:
    if not allowed_roots:
        raise FileAccessError("file_tools_disabled", "File tools are disabled until an allowed root is registered.")
    if not isinstance(raw_path, str) or not raw_path:
        raise FileAccessError("invalid_file_path", "An absolute path to one supported file is required.")
    if raw_path.startswith(("\\\\", "\\\\?\\", "\\\\.\\")):
        raise FileAccessError("invalid_file_path", "UNC and device paths are not accepted.")
    path = Path(raw_path)
    if not path.is_absolute() or not path.drive or ":" in raw_path[2:]:
        raise FileAccessError("invalid_file_path", "An ordinary absolute local path is required.")
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise FileAccessError("unsupported_file_type", "Only PDF, PNG, JPEG, and WebP files are supported.")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise FileAccessError("file_not_found", "The selected file is unavailable.")
    if not any(_within_root(resolved, root) for root in allowed_roots):
        raise FileAccessError("file_outside_allowed_roots", "The selected file is outside the registered allowed roots.")
    if _has_reparse_component(path) or _has_reparse_component(resolved):
        raise FileAccessError("reparse_point_rejected", "Files reached through reparse points are not accepted.")
    try:
        before = resolved.stat()
    except OSError:
        raise FileAccessError("file_not_found", "The selected file is unavailable.")
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise FileAccessError("non_regular_file", "Only ordinary, single-link files are accepted.")
    if before.st_size < 1:
        raise FileAccessError("empty_file", "Empty files are not accepted.")
    if before.st_size > MAX_FILE_SIZE:
        raise FileAccessError("file_too_large", "The file exceeds the 20 MiB limit.")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    fd = -1
    try:
        fd = os.open(str(resolved), flags)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise FileAccessError("non_regular_file", "Only ordinary, single-link files are accepted.")
        final_path = _final_path_for_fd(fd, resolved)
        if not any(_within_root(final_path, root) for root in allowed_roots):
            raise FileAccessError("file_outside_allowed_roots", "The opened file is outside the registered allowed roots.")
        if _has_reparse_component(final_path):
            raise FileAccessError("reparse_point_rejected", "Files reached through reparse points are not accepted.")
        identity = (opened.st_dev, opened.st_ino)
        metadata = (
            opened.st_size,
            opened.st_nlink,
            getattr(opened, "st_mtime_ns", None),
            getattr(opened, "st_ctime_ns", None),
        )
        chunks: list[bytes] = []
        remaining = MAX_FILE_SIZE + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > MAX_FILE_SIZE:
            raise FileAccessError("file_too_large", "The file exceeds the 20 MiB limit.")
        after = os.fstat(fd)
        final_after = _final_path_for_fd(fd, final_path)
        after_metadata = (
            after.st_size,
            after.st_nlink,
            getattr(after, "st_mtime_ns", None),
            getattr(after, "st_ctime_ns", None),
        )
        if (
            (after.st_dev, after.st_ino) != identity
            or after_metadata != metadata
            or final_after != final_path
        ):
            raise FileAccessError("file_changed", "The file changed while it was being read.")
        if len(content) != after.st_size:
            raise FileAccessError("file_changed", "The file changed while it was being read.")
        _validate_magic(content, final_path.suffix.lower())
        return VerifiedFile(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            display_basename=_display_basename(final_path),
            mime_type=MIME_BY_SUFFIX[final_path.suffix.lower()],
            size_bytes=len(content),
        )
    except FileAccessError:
        raise
    except OSError:
        raise FileAccessError("file_read_failed", "The selected file could not be read safely.")
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
