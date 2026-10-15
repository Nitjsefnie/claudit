"""Literal destinations of link, timestamp, transfer, archive and venv commands.

Option parsing shares the shell words' quote provenance with bash_reads.
Unknown options or runtime operands refuse inference; directory destinations
stay directories rather than guessing the files created underneath them.
"""
from __future__ import annotations

from collections.abc import Callable
import re
from urllib.parse import urlsplit

from backend.bash_literals import ShellWord, _file_sink, command_options, literal_path
from backend.target_paths import windows_absolute


def _modes(short: str, long: tuple[str, ...] = (),
           values: tuple[str, ...] = ()) -> dict[str, int]:
    modes = dict.fromkeys(["-" + char for char in short] + list(long), 0)
    modes.update(dict.fromkeys(values, 1))
    return modes


_LN_MODES = _modes(
    "sfnviLPT", ("--symbolic", "--force", "--no-dereference", "--verbose",
                 "--interactive", "--logical", "--physical", "--no-target-directory"),
    ("-t", "--target-directory", "-S", "--suffix"))
_LN_MODES["--backup"] = 2
_TOUCH_MODES = _modes(
    "acmh", ("--no-create", "--no-dereference"),
    ("-d", "-t", "-r", "--date", "--reference", "--time"))
_RSYNC_MODES = _modes(
    "avzrlptgoDHAXSnqhcIRLuKkOWx",
    ("--archive", "--verbose", "--recursive", "--compress", "--delete",
     "--delete-excluded", "--progress", "--partial", "--checksum",
     "--dry-run", "--relative", "--links", "--copy-links"),
    ("--exclude", "--include", "--exclude-from", "--include-from", "--filter",
     "-f", "-e", "--rsh", "--rsync-path", "--chmod", "--chown", "--bwlimit"))
_TAR_MODES = _modes(
    "xzjJvpfctrukOm",
    ("--extract", "--get", "--create", "--list", "--append", "--update",
     "--gzip", "--bzip2", "--xz", "--verbose", "--to-stdout"),
    ("-f", "--file", "-C", "--directory", "--strip-components"))
_UNZIP_MODES = _modes("oqnufjCLlvtpcZz", values=("-d", "-P", "-x"))
_CURL_MODES = _modes(
    "fsSLOkvI", ("--fail", "--silent", "--show-error", "--location",
                 "--remote-name", "--insecure", "--verbose", "--head"),
    ("-o", "--output", "--connect-timeout", "--max-time", "--retry",
     "-H", "--header", "-A", "--user-agent", "-u", "--user", "-X", "--request"))
_WGET_MODES = _modes("qcv", ("--quiet", "--continue", "--verbose"), ("-O",))
_VENV_MODES = _modes(
    "", ("--system-site-packages", "--symlinks", "--copies", "--clear",
         "--upgrade", "--without-pip", "--upgrade-deps"))


def _ln_targets(args: list[str]) -> list[str]:
    options, operands = command_options(args, _LN_MODES)
    directories = [value for flag, value in options
                   if flag in ("-t", "--target-directory") and value is not None]
    if directories:
        if not operands or any(flag in ("-T", "--no-target-directory") for flag, _ in options):
            return []
        return directories[-1:]
    return operands[-1:] if len(operands) >= 2 else []


def _touch_targets(args: list[str]) -> list[str]:
    return command_options(args, _TOUCH_MODES)[1]


def _rsync_targets(args: list[str]) -> list[str]:
    options, operands = command_options(args, _RSYNC_MODES)
    if len(operands) < 2 or any(flag in ("-n", "--dry-run") for flag, _ in options):
        return []
    destination = operands[-1]
    if not windows_absolute(destination) and re.match(r"^[^/]+:", destination):
        return []
    return [destination]


def _tar_targets(args: list[str]) -> list[str]:
    options, _ = command_options(args, _TAR_MODES)
    flags = {flag for flag, _ in options}
    if not flags.intersection(("-x", "--extract", "--get")) or flags.intersection(
            ("-c", "-t", "-r", "-u", "--create", "--list", "--append", "--update", "-O", "--to-stdout")):
        return []
    directories = [value for flag, value in options
                   if flag in ("-C", "--directory") and value is not None]
    return directories if len(directories) == 1 else []


def _unzip_targets(args: list[str]) -> list[str]:
    options, operands = command_options(args, _UNZIP_MODES)
    if not operands or any(flag in ("-l", "-v", "-t", "-p", "-c", "-Z", "-z") for flag, _ in options):
        return []
    directories = [value for flag, value in options if flag == "-d" and value is not None]
    return directories if len(directories) == 1 else []


def _url_name(url: str) -> list[str]:
    """The final URL path segment, without query/fragment or authority."""
    parsed = urlsplit(url)
    if not parsed.scheme or not (parsed.netloc or (parsed.scheme == "file" and parsed.path.startswith("/"))):
        return []
    name = parsed.path.rsplit("/", 1)[-1]
    return [ShellWord(name)] if name and name not in (".", "..") else []


def _download_targets(name: str, args: list[str]) -> list[str]:
    options, urls = command_options(args, _CURL_MODES if name == "curl" else _WGET_MODES)
    if len(urls) != 1:
        return []
    output_flags = ("-o", "--output") if name == "curl" else ("-O",)
    outputs = [value for flag, value in options if flag in output_flags and value is not None]
    if outputs:
        return outputs[-1:]
    if name == "wget" or any(flag in ("-O", "--remote-name") for flag, _ in options):
        return _url_name(urls[0])
    return []


def _venv_targets(args: list[str]) -> list[str]:
    if args[:2] != ["-m", "venv"]:
        return []
    return command_options(args[2:], _VENV_MODES)[1]


_TARGETS: dict[str, Callable[[list[str]], list[str]]] = {
    "ln": _ln_targets,
    "touch": _touch_targets,
    "rsync": _rsync_targets,
    "tar": _tar_targets,
    "unzip": _unzip_targets,
    "python": _venv_targets,
    "python3": _venv_targets,
}
WRITE_TARGET_COMMANDS = frozenset(_TARGETS) | {"curl", "wget"}


def write_targets(name: str, operands: list[str], *, literal_only: bool = True) -> list[str]:
    """Named destinations, or sink operands for churn when literal_only=False.

    Only the churn caller may use unresolved sink operands as write intent;
    stored targets always require literal paths after assignment expansion.
    """
    if name not in WRITE_TARGET_COMMANDS:
        return []
    if any(getattr(arg, "operator", False) or (literal_only and not getattr(arg, "literal", False))
           for arg in operands):
        return []
    try:
        if name in ("curl", "wget"):
            targets = _download_targets(name, operands)
        elif handler := _TARGETS.get(name):
            targets = handler(operands)
        else:
            return []
    except ValueError:
        return []
    is_target = literal_path if literal_only else _file_sink
    return [path for path in targets if is_target(path)]
