"""Process-local disk generations and lazy, shared source preprocessing.

Returned ASTs and artifacts are shared read-only objects. File requests stat
the path afresh; exact source strings share preprocessing across paths.
"""
from __future__ import annotations

import __future__
import ast
from dataclasses import dataclass, field
import hashlib
import io
import locale
import os
from pathlib import Path
import time
import tokenize
import types
from typing import Any, Literal, Never, cast


@dataclass(frozen=True)
class _Failure:
    kind: type[Exception]
    args: tuple[Any, ...]
    attributes: tuple[tuple[str, object], ...]

    @classmethod
    def capture(cls, error: Exception) -> _Failure:
        names = ("filename", "lineno", "offset", "end_lineno", "end_offset",
                 "text", "msg")
        attributes = tuple((name, getattr(error, name)) for name in names
                           if hasattr(error, name))
        return cls(type(error), error.args, attributes)

    def raise_for(self, filename: str | None = None) -> Never:
        args = self.args
        if (filename is not None and issubclass(self.kind, SyntaxError)
                and len(args) > 1):
            details = (filename, *args[1][1:])
            args = (args[0], details, *args[2:])
        error = self.kind(*args)
        for name, value in self.attributes:
            if name == "filename" and value is not None and filename is not None:
                value = filename
            setattr(error, name, value)
        raise error


@dataclass
class SourceSnapshot:
    """One resolved path's raw content, with read_text-compatible views."""

    path: Path
    content_hash: str
    raw: bytes
    _decoded: dict[str, str | _Failure] = field(
        default_factory=dict, init=False, repr=False)

    def source(self, *, encoding: str | None = "utf-8") -> str:
        effective_encoding = io.text_encoding(encoding)
        if effective_encoding == "locale":
            effective_encoding = locale.getencoding()
        if effective_encoding not in self._decoded:
            try:
                with io.TextIOWrapper(io.BytesIO(self.raw),
                                      encoding=effective_encoding,
                                      errors="strict", newline=None) as reader:
                    self._decoded[effective_encoding] = reader.read()
            except UnicodeError as error:
                self._decoded[effective_encoding] = _Failure.capture(error)
        result = self._decoded[effective_encoding]
        if isinstance(result, _Failure):
            result.raise_for()
        return result


_TreeKey = tuple[str, bool, int | tuple[int, int] | None, int]


@dataclass
class SourceArtifacts:
    """Lazy preprocessing for an exact string; clients must not mutate ASTs."""

    source: str
    _tokens: tuple[tokenize.TokenInfo, ...] | _Failure | None = field(
        default=None, init=False, repr=False)
    _trees: dict[_TreeKey, ast.AST | _Failure] = field(
        default_factory=dict, init=False, repr=False)
    _code: types.CodeType | _Failure | None = field(
        default=None, init=False, repr=False)
    _nodes: tuple[ast.AST, ...] | None = field(
        default=None, init=False, repr=False)

    def tokens(self) -> tuple[tokenize.TokenInfo, ...]:
        if self._tokens is None:
            try:
                self._tokens = tuple(tokenize.generate_tokens(
                    io.StringIO(self.source).readline))
            except (tokenize.TokenError, SyntaxError) as error:
                self._tokens = _Failure.capture(error)
        if isinstance(self._tokens, _Failure):
            self._tokens.raise_for()
        return self._tokens

    def tree(self, *, filename: str = "<unknown>", mode: str = "exec",
             type_comments: bool = False,
             feature_version: int | tuple[int, int] | None = None,
             optimize: int = -1) -> ast.AST:
        key = (mode, type_comments, feature_version, optimize)
        if key not in self._trees:
            try:
                self._trees[key] = ast.parse(
                    self.source, mode=mode, type_comments=type_comments,
                    feature_version=feature_version,
                    optimize=cast(Literal[-1, 0, 1, 2], optimize))
            except (SyntaxError, ValueError) as error:
                self._trees[key] = _Failure.capture(error)
        result = self._trees[key]
        if isinstance(result, _Failure):
            result.raise_for(filename)
        return result

    def nodes(self) -> tuple[ast.AST, ...]:
        """The default exec tree's nodes, in exactly ast.walk order."""
        if self._nodes is None:
            self._nodes = tuple(ast.walk(self.tree()))
        return self._nodes

    def scan_code(self) -> types.CodeType:
        """Compile the shared AST with scan_gate's inherited future flags."""
        if self._code is None:
            try:
                tree = self.tree(filename="<scan_gate>")
                assert isinstance(tree, ast.Module)
                self._code = compile(
                    tree, "<scan_gate>", "exec",
                    flags=__future__.annotations.compiler_flag,
                    dont_inherit=True)
            except (SyntaxError, ValueError) as error:
                self._code = _Failure.capture(error)
        if isinstance(self._code, _Failure):
            self._code.raise_for()
        assert isinstance(self._code, types.CodeType)
        return self._code


_Signature = tuple[int, int, int, int, int]
RACY_MARGIN_NS = 2_000_000_000
_current: dict[Path, tuple[_Signature, int, SourceSnapshot]] = {}
_history: dict[tuple[Path, str], SourceSnapshot] = {}
_artifacts: dict[str, SourceArtifacts] = {}


def _signature(stat: os.stat_result) -> _Signature:
    return (stat.st_dev, stat.st_ino, stat.st_size,
            stat.st_mtime_ns, stat.st_ctime_ns)


def _now_ns() -> int:
    return time.time_ns()


def snapshot(path: Path) -> SourceSnapshot:
    resolved = path.resolve()
    before = _signature(resolved.stat())
    current = _current.get(resolved)
    if current is not None:
        signature, record_time_ns, record = current
        cutoff = record_time_ns - RACY_MARGIN_NS
        # Recent timestamp collisions force a read; an old equal-stat rewrite
        # remains undetectable when both timestamps already precede the margin.
        if (signature == before and before[3] < cutoff
                and before[4] < cutoff):
            return record
    record_time_ns = _now_ns()
    with resolved.open("rb") as reader:
        opened = _signature(os.fstat(reader.fileno()))
        data = reader.read()
        after = _signature(os.fstat(reader.fileno()))
    if opened != after:
        raise RuntimeError(f"Source generation changed during read: {resolved}")
    content_hash = hashlib.sha256(data).hexdigest()
    key = (resolved, content_hash)
    record = _history.get(key)
    if record is None:
        record = SourceSnapshot(resolved, content_hash, data)
        _history[key] = record
    # Keep path-stat metadata separate from descriptor metadata. A replacement
    # before open leaves the old signature here, forcing the next call to read.
    _current[resolved] = (before, record_time_ns, record)
    return record


def source(path: Path, *, encoding: str | None = "utf-8") -> str:
    return snapshot(path).source(encoding=encoding)


def raw(path: Path) -> bytes:
    return snapshot(path).raw


def for_source(source: str) -> SourceArtifacts:  # pylint: disable=redefined-outer-name
    result = _artifacts.get(source)
    if result is None:
        result = SourceArtifacts(source)
        _artifacts[source] = result
    return result
