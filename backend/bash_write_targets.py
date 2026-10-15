"""Literal destinations of recognized shell write commands.

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
_WORKTREE_MODES = _modes(
    "fq", ("--force", "--detach", "--checkout", "--lock", "--no-track",
           "--guess-remote", "--overwrite", "--orphan", "--bare", "--quiet", "--verbose"),
    ("-b", "-B", "--prefix", "--reason"))
_WORKTREE_MODES["--track"] = 2
_CLONE_MODES = _modes(
    "lsnq", ("--local", "--no-hardlinks", "--shared", "--no-checkout", "--bare",
             "--mirror", "--quiet", "--verbose", "--progress", "--no-single-branch",
             "--no-tags", "--sparse", "--dissociate", "--also-filter-submodules"),
    ("-c", "-b", "-B", "--branch", "-o", "--origin", "--template", "--reference",
     "-u", "--upload-pack", "--depth", "--deepen", "-j", "--jobs", "--filter",
     "--bundle-uri", "--separate-git-dir", "--ref-format", "--server-option",
     "--shallow-exclude", "--shallow-since"))
_CLONE_MODES["--recurse-submodules"] = 2
_CHECKOUT_MODES = _modes(
    "fpq", ("--force", "--patch", "--quiet", "--ours", "--theirs", "--detach"),
    ("-b", "-B", "--conflict"))
_RESTORE_MODES = _modes(
    "SWpq", ("--staged", "--worktree", "--patch", "--quiet", "--pathspec-file-nul",
             "--ours", "--theirs", "--merge", "--ignore-unmerged"),
    ("-s", "--source", "--pathspec-from-file", "--conflict"))
_INIT_MODES = _modes("q", ("--quiet", "--bare"),
                     ("-b", "-B", "--initial-branch", "--template", "--separate-git-dir"))
_INIT_MODES["--shared"] = 2
_COMPILER_MODES = _modes(
    "cSEsv", ("-Wall", "-Wextra", "-Werror", "-pedantic", "-pthread", "-shared",
              "-static", "-fPIC", "-fsyntax-only", "--version", "-Q"),
    ("-o", "-I", "-D", "-U", "-L", "-l", "-x", "-include", "-isystem", "-std"))
_COMPILER_MODES.update(dict.fromkeys(("-g", "-O", "--help"), 2))
_PDF_FLAGS = ("layout", "raw", "nopgbrk", "htmlmeta", "bbox", "bbox-layout", "q")
_PDF_VALUES = ("opw", "oupw", "upw", "enc", "eol", "r", "x", "y", "W", "H")
_PDF_MODES = _modes("", tuple("-" + flag for flag in _PDF_FLAGS),
                    ("-f", "-l") + tuple("-" + flag for flag in _PDF_VALUES))
_INITDB_MODES = _modes(
    "dknsW", ("--debug", "--no-clean", "--no-sync", "--data-checksums", "--no-locale"),
    ("-D", "--pgdata", "-U", "--username", "-A", "--auth", "--auth-local", "--auth-host",
     "-E", "--encoding", "--locale", "--locale-provider", "--icu-locale", "--icu-rules",
     "--builtin-locale", "--lc-collate", "--lc-ctype", "--lc-messages", "--lc-monetary",
     "--lc-numeric", "--lc-time", "--pwfile", "-X", "--waldir"))
_RUSTFMT_MODES = _modes(
    "qv", ("--quiet", "--verbose", "--check", "--files-with-diff"),
    ("--config", "--config-path", "--edition", "--emit", "--style-edition"))
_PERL_MODES = _modes("pnwWlc0", values=("-e", "-E", "-I", "-M", "-m", "-F"))
_PERL_MODES["-i"] = 2


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


def _checkout_targets(args: list[str]) -> list[str]:
    if "--" not in args:
        return []
    boundary = args.index("--")
    options, _ = command_options(args[:boundary], _CHECKOUT_MODES)
    if any(flag in ("-p", "--patch") for flag, _ in options):
        return []
    return args[boundary + 1:]


def _restore_targets(args: list[str]) -> list[str]:
    options, paths = command_options(args, _RESTORE_MODES)
    flags = {flag for flag, _ in options}
    if flags.intersection(("-p", "--patch")) or (
            flags.intersection(("-S", "--staged")) and not flags.intersection(("-W", "--worktree"))):
        return []
    return paths


def _git_targets(args: list[str]) -> list[str]:
    if args[:2] == ["worktree", "add"]:
        return command_options(args[2:], _WORKTREE_MODES)[1][:1]
    if not args:
        return []
    name, operands = args[0], args[1:]
    if name in ("checkout", "restore"):
        handler = _checkout_targets if name == "checkout" else _restore_targets
        return handler(operands)
    if name == "clone":
        paths = command_options(operands, _CLONE_MODES)[1]
        return paths[1:2] if len(paths) >= 2 else []
    if name == "init":
        paths = command_options(operands, _INIT_MODES)[1]
        return paths if len(paths) == 1 else ([ShellWord(".")] if not paths else [])
    return []


def _compiler_targets(args: list[str]) -> list[str]:
    options, _ = command_options(args, _COMPILER_MODES)
    if any(flag in ("--version", "--help", "-fsyntax-only") for flag, _ in options):
        return []
    return [value for flag, value in options if flag == "-o" and value is not None][-1:]


def _pdftotext_targets(args: list[str]) -> list[str]:
    paths = command_options(args, _PDF_MODES)[1]
    return paths[1:] if len(paths) == 2 else []


def _initdb_targets(args: list[str]) -> list[str]:
    options, paths = command_options(args, _INITDB_MODES)
    directories = [value for flag, value in options if flag in ("-D", "--pgdata") and value is not None]
    return (directories or paths) if len(directories) + len(paths) == 1 else []


def _rustfmt_targets(args: list[str]) -> list[str]:
    options, paths = command_options(args, _RUSTFMT_MODES)
    if any(flag == "--check" or (flag == "--emit" and value != "files") for flag, value in options):
        return []
    return paths


def _perl_targets(args: list[str]) -> list[str]:
    options, files = command_options(args, _PERL_MODES)
    flags = {flag for flag, _ in options}
    if "-i" not in flags or "-c" in flags:
        return []
    return files if flags.intersection(("-e", "-E")) else files[1:]


_TARGETS: dict[str, Callable[[list[str]], list[str]]] = {
    "ln": _ln_targets,
    "touch": _touch_targets,
    "rsync": _rsync_targets,
    "tar": _tar_targets,
    "unzip": _unzip_targets,
    "python": _venv_targets,
    "python3": _venv_targets,
    "git": _git_targets,
    "gcc": _compiler_targets,
    "g++": _compiler_targets,
    "cc": _compiler_targets,
    "clang": _compiler_targets,
    "clang++": _compiler_targets,
    "pdftotext": _pdftotext_targets,
    "initdb": _initdb_targets,
    "rustfmt": _rustfmt_targets,
    "perl": _perl_targets,
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
