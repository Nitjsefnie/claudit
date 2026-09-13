"""Lexical target paths in the transcript's namespace, never the host's.

Drive-absolute and backslash UNC paths are Windows paths. Slash-rooted paths
remain POSIX (including Git-Bash /c and ambiguous // spellings). Device paths
remain verbatim; no mount, symlink, filesystem case or short-name lookup occurs.
"""
from __future__ import annotations

import ntpath
import posixpath
from string import ascii_letters


def _drive_absolute(path: str) -> bool:
    return len(path) >= 3 and path[1] == ":" and path[2] in "/\\" and path[0] in ascii_letters


def _verbatim(path: str) -> bool:
    return path.startswith(("\\\\?\\", "\\\\.\\"))


def windows_absolute(path: str | None) -> bool:
    """Only explicit Windows drive roots, UNC shares and device namespaces."""
    if not path:
        return False
    if _drive_absolute(path) or _verbatim(path):
        return True
    if not path.startswith("\\\\"):
        return False
    parts = path[2:].replace("/", "\\").split("\\", 2)
    return len(parts) >= 2 and bool(parts[0] and parts[1])


def _windows_flavor(path: str, base: str | None) -> bool:
    return not path.startswith("/") and (windows_absolute(path) or windows_absolute(base))


def _normalize_windows(path: str) -> str:
    return path if _verbatim(path) else ntpath.normpath(path)


def resolve_target(path: str, base: str | None = "") -> str | None:
    """Resolve relative targets using recorded cwd; None marks an unknown cd."""
    if path.startswith("/"):
        return path
    if windows_absolute(path):
        return _normalize_windows(path)
    if base is None:
        return None
    if not base:
        return path
    if windows_absolute(base):
        return _normalize_windows(ntpath.join(base, path))
    return posixpath.normpath(posixpath.join(base, path))


def target_key(path: str) -> str:
    """Windows-only equivalence for matching; callers keep their raw arrays."""
    if path.startswith("/") or not windows_absolute(path) or _verbatim(path):
        return path
    normalized = ntpath.normpath(path)
    if _drive_absolute(normalized):
        normalized = normalized[0].upper() + normalized[1:]
    return normalized


def directory_spelling(path: str, base: str | None = "") -> bool:
    """Directory evidence in the text, without probing the destination."""
    if _windows_flavor(path, base):
        return (path.endswith(("/", "\\")) or ntpath.basename(path) in (".", "..")
                or (windows_absolute(path) and not ntpath.splitdrive(path)[1]))
    return path.endswith("/") or posixpath.basename(path) in (".", "..")


def join_child(directory: str, child: str, base: str | None = "") -> str:
    """Append a child in the destination's syntax without normalizing it."""
    if _windows_flavor(directory, base):
        return ntpath.join(directory, child)
    return posixpath.join(directory, child)


def copy_source_name(path: str, base: str | None = "") -> str | None:
    """The literal child name for a directory copy, or an unsupported source."""
    windows = _windows_flavor(path, base)
    if base is None and "\\" in path and not windows:
        return None
    stripped = path.rstrip("/\\" if windows else "/")
    if stripped in (".", ".."):
        return None
    return ntpath.basename(stripped) if windows else posixpath.basename(stripped)
