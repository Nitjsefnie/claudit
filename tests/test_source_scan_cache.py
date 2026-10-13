"""Compatibility and preprocessing budgets for shared source artifacts."""
from __future__ import annotations

import ast
import builtins
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
import io
import locale
import os
from pathlib import Path
import subprocess
import sys
import tokenize
import types
from typing import Any, Iterator

import pytest

from tests import scan_gate, source_scan_cache, source_scan_index


def _failure(call: Callable[[], object]) -> Exception:
    try:
        call()
    except Exception as error:
        return error
    raise AssertionError("Expected a reference-compatible failure")


def _diagnostic(error: Exception) -> tuple[object, ...]:
    attributes = ("filename", "lineno", "offset", "end_lineno",
                  "end_offset", "text", "msg")
    return (type(error), error.args,
            tuple(getattr(error, name, None) for name in attributes))


def _code_signature(value: object) -> object:
    if isinstance(value, types.CodeType):
        return (value.co_code, _code_signature(value.co_consts),
                value.co_names, value.co_varnames, value.co_name,
                value.co_flags, value.co_filename)
    if isinstance(value, tuple):
        return tuple(_code_signature(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_code_signature(item) for item in value)
    return value


def _reference_code(text: str) -> types.CodeType:
    return compile(text, "<scan_gate>", "exec")


def _count_preprocessing(
        text: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[Counter[Path], Counter[str], Counter[str], Counter[str]]:
    read_calls: Counter[Path] = Counter()
    parse_calls: Counter[str] = Counter()
    tokenize_calls: Counter[str] = Counter()
    other_calls: Counter[str] = Counter()
    originals = types.SimpleNamespace(
        open=Path.open, parse=ast.parse, tokens=tokenize.generate_tokens,
        compile=builtins.compile, walk=ast.walk)

    def counted_open(file: Path, *args: Any, **kwargs: Any) -> Any:
        read_calls[file.resolve()] += 1
        return originals.open(file, *args, **kwargs)

    def counted_parse(source: str, *args: Any, **kwargs: Any) -> ast.AST:
        parse_calls[source] += 1
        return originals.parse(source, *args, **kwargs)

    def counted_tokens(readline: Callable[[], str]) -> Any:
        tokenize_calls[text] += 1
        return originals.tokens(readline)

    def counted_compile(source: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(source, (ast.Module, ast.Expression, ast.Interactive)):
            other_calls["compile"] += 1
        return originals.compile(source, *args, **kwargs)

    def counted_walk(tree: ast.AST) -> Any:
        other_calls["walk"] += 1
        return originals.walk(tree)

    monkeypatch.setattr(Path, "open", counted_open)
    monkeypatch.setattr(ast, "parse", counted_parse)
    monkeypatch.setattr(tokenize, "generate_tokens", counted_tokens)
    monkeypatch.setattr(builtins, "compile", counted_compile)
    monkeypatch.setattr(ast, "walk", counted_walk)
    return read_calls, parse_calls, tokenize_calls, other_calls


def _old_clock(paths: tuple[Path, ...],
               monkeypatch: pytest.MonkeyPatch) -> None:
    for path in paths:
        os.utime(path, ns=(1, 1))
    now = max(path.stat().st_ctime_ns for path in paths) + 10_000_000_000
    monkeypatch.setattr(source_scan_cache, "_now_ns", lambda: now,
                        raising=False)


def test_read_once_and_lazy_artifact_identity(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "source.py"
    path.write_text(f"value = 1  # {tmp_path.name}\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    expected_tokens = tuple(tokenize.generate_tokens(io.StringIO(text).readline))
    expected_ast = ast.parse(text)
    expected_nodes = tuple(ast.dump(node) for node in ast.walk(expected_ast))
    expected_code = _code_signature(_reference_code(text))
    _old_clock((path,), monkeypatch)
    read_calls, parse_calls, tokenize_calls, other_calls = _count_preprocessing(
        text, monkeypatch)
    snap = source_scan_cache.snapshot(path)
    assert source_scan_cache.raw(path) is snap.raw
    assert not parse_calls and not tokenize_calls and not other_calls
    assert source_scan_cache.source(path) is snap.source()
    one = source_scan_cache.for_source(source_scan_cache.source(path))
    two = source_scan_cache.for_source(snap.source())
    assert one is two
    assert one.source == text
    assert one.tokens() is two.tokens()
    assert one.tokens() == expected_tokens
    assert one.tree() is two.tree(filename="another.py")
    assert ast.dump(one.tree(), include_attributes=True) == ast.dump(
        expected_ast, include_attributes=True)
    assert one.scan_code() is two.scan_code()
    assert _code_signature(one.scan_code()) == expected_code
    assert one.nodes() is two.nodes()
    assert tuple(ast.dump(node) for node in one.nodes()) == expected_nodes
    assert read_calls[path.resolve()] == 1
    assert parse_calls[text] == tokenize_calls[text] == 1
    assert other_calls == {"compile": 1, "walk": 1}


@pytest.mark.parametrize("recent", ["mtime", "ctime"])
def test_racy_reads_reuse_content_and_stop_when_clock_advances(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recent: str) -> None:
    path = tmp_path / "racy.py"
    text = f"value = 1  # {tmp_path.name}\n"
    path.write_text(text, encoding="utf-8")
    if recent == "ctime":
        os.utime(path, ns=(1, 1))
        now = [path.stat().st_ctime_ns]
    else:
        now = [path.stat().st_ctime_ns + 10_000_000_000]
        os.utime(path, ns=(1, now[0]))
    stat = path.stat()
    monkeypatch.setattr(source_scan_cache, "_now_ns", lambda: now[0],
                        raising=False)
    calls = _count_preprocessing(text, monkeypatch)
    one = source_scan_cache.snapshot(path)
    artifacts = source_scan_cache.for_source(one.source())
    expected = (one.source(), artifacts.tokens(), artifacts.tree(),
                artifacts.nodes(), artifacts.scan_code())
    for _ in range(3):
        assert source_scan_cache.snapshot(path) is one
        assert source_scan_cache.source(path) is expected[0]
        shared = source_scan_cache.for_source(source_scan_cache.source(path))
        assert shared.tokens() is expected[1]
        assert shared.tree() is expected[2]
        assert shared.nodes() is expected[3]
        assert shared.scan_code() is expected[4]
    assert calls[0][path.resolve()] == 10
    assert calls[1] == calls[2] == {text: 1}
    assert calls[3] == {"walk": 1, "compile": 1}
    now[0] = max(stat.st_mtime_ns, stat.st_ctime_ns) + 2_000_000_000
    # Equality at the margin remains racy; one ns older allows a fast path.
    assert source_scan_cache.snapshot(path) is one
    assert source_scan_cache.snapshot(path) is one
    assert calls[0][path.resolve()] == 12
    now[0] += 1
    assert source_scan_cache.snapshot(path) is one
    assert calls[0][path.resolve()] == 13
    for _ in range(3):
        assert source_scan_cache.snapshot(path) is one
        assert source_scan_cache.source(path) is expected[0]
    assert calls[0][path.resolve()] == 13
    assert calls[1] == calls[2] == {text: 1}
    assert calls[3] == {"walk": 1, "compile": 1}


def test_independent_roots_addition_deletion_and_path_aliases(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    first = left / "same.py"
    second = right / "same.py"
    first.write_text("left = 1\n", encoding="utf-8")
    second.write_text("right = 2\n", encoding="utf-8")
    one = source_scan_cache.snapshot(first)
    two = source_scan_cache.snapshot(second)
    assert one.path == first.resolve()
    assert two.path == second.resolve()
    assert one is not two and one.content_hash != two.content_hash
    monkeypatch.chdir(tmp_path)
    assert source_scan_cache.snapshot(Path("left/same.py")) is one
    later = left / "later.py"
    assert not later.exists()
    later.write_text("added = True\n", encoding="utf-8")
    assert source_scan_cache.source(later) == "added = True\n"
    first.unlink()
    for call in (source_scan_cache.snapshot, source_scan_cache.source,
                 source_scan_cache.raw):
        assert isinstance(_failure(lambda call=call: call(first)),
                          FileNotFoundError)
    assert source_scan_cache.snapshot(second) is two


def test_same_size_restored_mtime_and_inode_replacement(tmp_path: Path) -> None:
    path = tmp_path / "rewrite.py"
    path.write_text("before = 1\n", encoding="utf-8")
    old_stat = path.stat()
    old = source_scan_cache.snapshot(path)
    old_artifacts = source_scan_cache.for_source(old.source())
    old_tree, old_tokens = old_artifacts.tree(), old_artifacts.tokens()
    path.write_text("after_ = 2\n", encoding="utf-8")
    os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    changed = source_scan_cache.snapshot(path)
    assert path.stat().st_size == old_stat.st_size
    assert path.stat().st_mtime_ns == old_stat.st_mtime_ns
    assert changed.source() == "after_ = 2\n"
    assert changed.content_hash != old.content_hash
    artifacts = source_scan_cache.for_source(changed.source())
    assert artifacts.tree() is not old_tree
    assert artifacts.tokens() is not old_tokens
    assert ast.dump(artifacts.tree()) != ast.dump(old_tree)
    assert artifacts.tokens() != old_tokens
    replacement = tmp_path / "replacement.py"
    replacement.write_text("third_ = 3\n", encoding="utf-8")
    os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_ino != old_stat.st_ino
    replaced = source_scan_cache.snapshot(path)
    assert replaced.content_hash != changed.content_hash
    assert replaced.source() == "third_ = 3\n"
    path.write_text(old.source(), encoding="utf-8")
    returned = source_scan_cache.snapshot(path)
    assert returned is old
    assert source_scan_cache.for_source(returned.source()) is old_artifacts


def test_identical_content_shares_artifacts_across_distinct_files(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = f"value = 'shared'  # {tmp_path.name}\n"
    paths = (tmp_path / "first.py", tmp_path / "second.py")
    for path in paths:
        path.write_text(text, encoding="utf-8")
    _old_clock(paths, monkeypatch)
    calls = _count_preprocessing(text, monkeypatch)
    one, two = (source_scan_cache.snapshot(path) for path in paths)
    assert one is not two
    assert one.content_hash == two.content_hash
    first = source_scan_cache.for_source(one.source())
    second = source_scan_cache.for_source(two.source())
    assert first is second
    assert first.tree() is second.tree()
    assert first.tokens() is second.tokens()
    assert first.scan_code() is second.scan_code()
    assert calls[0] == {path.resolve(): 1 for path in paths}
    assert calls[1] == calls[2] == {text: 1}
    assert calls[3] == {"compile": 1}


def test_failed_preprocessing_is_cached_without_reusing_exceptions(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = f"# {tmp_path.name}\nx = (\n"
    expected_parse = _failure(lambda: ast.parse(text, filename="named.py"))
    expected_tokens = _failure(lambda: tuple(tokenize.generate_tokens(
        io.StringIO(text).readline)))
    expected_code = _failure(lambda: _reference_code(text))
    calls = _count_preprocessing(text, monkeypatch)
    artifacts = source_scan_cache.for_source(text)
    for _ in range(2):
        assert _diagnostic(_failure(artifacts.tokens)) == _diagnostic(expected_tokens)
        assert _diagnostic(_failure(lambda: artifacts.tree(filename="named.py"))) == _diagnostic(expected_parse)
        assert _diagnostic(_failure(artifacts.scan_code)) == _diagnostic(expected_code)
    assert calls[1] == calls[2] == {text: 1}
    assert not calls[3]


@pytest.mark.parametrize("data", [
    b"", b"x = 1", b"x = 1\r\ny = 2\r\n", b"x = 1\ry = 2\r",
    b"\xef\xbb\xbfx = 1\n", b"# marker\ns = 'marker'\n", b"\xff\n",
])
@pytest.mark.parametrize("encoding", ["utf-8", None])
def test_bytes_and_decoding_match_read_text(
        tmp_path: Path, data: bytes, encoding: str | None) -> None:
    path = tmp_path / "decoding.py"
    path.write_bytes(data)
    try:
        expected = path.read_text(encoding=encoding)
    except UnicodeError as reference_error:
        one = _failure(lambda: source_scan_cache.source(path, encoding=encoding))
        two = _failure(lambda: source_scan_cache.source(path, encoding=encoding))
        assert _diagnostic(one) == _diagnostic(reference_error)
        assert _diagnostic(two) == _diagnostic(reference_error)
        assert one is not two
    else:
        assert source_scan_cache.source(path, encoding=encoding) == expected
    assert source_scan_cache.raw(path) == data


def test_default_locale_and_explicit_encoding_are_separate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "locale.py"
    path.write_bytes(b"# caf\xe9\r\n")
    expected = path.read_text(encoding="latin-1")
    # Force the locale route even on an interpreter running in UTF-8 mode.
    original_text_encoding = io.text_encoding
    monkeypatch.setattr(io, "text_encoding", lambda encoding: (
        "locale" if encoding is None else original_text_encoding(encoding)))
    monkeypatch.setattr(locale, "getencoding", lambda: "latin-1")
    assert source_scan_cache.source(path, encoding=None) == expected
    assert isinstance(_failure(lambda: source_scan_cache.source(path)),
                      UnicodeDecodeError)


@pytest.mark.parametrize("text", [
    "# markers inside comments\nx = 'marker'\n", "", "x = 1", "x = 1\r\n",
    "\ufeffx = 1\n", "x = (\n", "'''unfinished", "if True:\n  a=1\n b=2\n",
])
def test_tokens_and_token_errors_match_reference(text: str) -> None:
    try:
        expected = tuple(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError) as reference_error:
        one = _failure(source_scan_cache.for_source(text).tokens)
        two = _failure(source_scan_cache.for_source(text).tokens)
        assert _diagnostic(one) == _diagnostic(reference_error)
        assert _diagnostic(two) == _diagnostic(reference_error)
        assert one is not two
    else:
        assert source_scan_cache.for_source(text).tokens() == expected


@pytest.mark.parametrize("text", [
    "x = (\n", "\ufeffx = 1\n", "if True:\n  a=1\n b=2\n", "x = '\x00'\n",
])
@pytest.mark.parametrize("filename", ["<unknown>", "supplied.py"])
def test_parser_errors_relabel_and_recreate(text: str, filename: str) -> None:
    expected = _failure(lambda: ast.parse(text, filename=filename))
    artifacts = source_scan_cache.for_source(text)
    one = _failure(lambda: artifacts.tree(filename=filename))
    two = _failure(lambda: artifacts.tree(filename=filename))
    assert _diagnostic(one) == _diagnostic(expected)
    assert _diagnostic(two) == _diagnostic(expected)
    assert one is not two


def test_parser_options_are_isolated() -> None:
    text = "result = 1  # type: int\n"
    artifacts = source_scan_cache.for_source(text)
    plain = artifacts.tree()
    commented = artifacts.tree(type_comments=True)
    assert plain is not commented
    assert ast.dump(plain) == ast.dump(ast.parse(text))
    assert ast.dump(commented) == ast.dump(ast.parse(text, type_comments=True))
    expression = source_scan_cache.for_source("1 + 2")
    assert isinstance(expression.tree(), ast.Module)
    assert isinstance(expression.tree(mode="eval"), ast.Expression)
    assert isinstance(expression.tree(mode="single"), ast.Interactive)
    option_sets: tuple[dict[str, Any], ...] = (
        {"feature_version": (3, 10)}, {"feature_version": 11},
        {"optimize": 1}, {"optimize": 2})
    for options in option_sets:
        tree = artifacts.tree(**options)
        assert tree is artifacts.tree(**options)
        assert tree is not plain
        assert ast.dump(tree) == ast.dump(ast.parse(text, **options))
    match_text = "match x:\n    case 1: pass\n"
    match = source_scan_cache.for_source(match_text)
    expected = _failure(lambda: ast.parse(match_text, feature_version=(3, 9)))
    assert _diagnostic(_failure(lambda: match.tree(feature_version=(3, 9)))) == _diagnostic(expected)
    assert isinstance(match.tree(), ast.Module)


@pytest.mark.parametrize("options", [
    {"mode": "invalid"}, {"optimize": 3}, {"feature_version": (4, 0)},
])
def test_invalid_parser_options_match_reference(options: dict[str, Any]) -> None:
    text = "x = 1\n"
    expected = _failure(lambda: ast.parse(text, **options))
    artifacts = source_scan_cache.for_source(text)
    assert _diagnostic(_failure(lambda: artifacts.tree(**options))) == _diagnostic(expected)


@pytest.mark.parametrize("text", [
    "value: int = 1\ndef annotated(x: str) -> tuple[str, int]:\n    return x, value\n",
    "from __future__ import annotations\nx: Missing = 1\n",
    "from __future__ import barry_as_FLUFL\nx = 1\n",
    "import pytest\ndef outer(request):\n    @pytest.fixture\n    def inner(parameter):\n        return parameter\n    return inner\n",
    "label = 'first' 'second'\nescaped = '\\x66ixture'\n",
    "values = ('one', ('two', 'three'))\nif item in {'four', 'five'}: pass\n",
    "if 0:\n    hidden = 'dis' 'carded'\nelse:\n    visible = 'kept'\n",
    "assert False, 'message'\ndef f():\n    'docstring'\n    return 1\n",
])
def test_recursive_compiler_and_vocabulary_equivalence(text: str) -> None:
    expected_code = _code_signature(_reference_code(text))
    expected_vocab = scan_gate.module_vocab(text)
    expected_def_vocab = scan_gate.module_def_vocab(text)
    artifacts = source_scan_cache.for_source(text)
    assert _code_signature(artifacts.scan_code()) == expected_code
    assert source_scan_index.vocab(text) == expected_vocab
    assert source_scan_index.def_vocab(text) == expected_def_vocab


@pytest.mark.parametrize("optimization", ["-O", "-OO"])
def test_compiler_preserves_interpreter_optimization(optimization: str) -> None:
    script = "\n".join([
        "from tests.test_source_scan_cache import _code_signature, _reference_code",
        "from tests.source_scan_cache import for_source",
        "text = \"'module doc'\\nassert True, 'assertion'\\ndef f(x: int):\\n    'function doc'\\n    return x\\n\"",
        "expected = _code_signature(_reference_code(text))",
        "actual = _code_signature(for_source(text).scan_code())",
        "if actual != expected: raise AssertionError((actual, expected))",
    ])
    result = subprocess.run([sys.executable, optimization, "-B", "-c", script],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("text", ["return 1\n", "break\n", "x = (\n"])
def test_compile_errors_match_direct_compile(text: str) -> None:
    expected = _failure(lambda: _reference_code(text))
    artifacts = source_scan_cache.for_source(text)
    if text != "x = (\n":
        assert isinstance(artifacts.tree(), ast.Module)
    one = _failure(artifacts.scan_code)
    two = _failure(artifacts.scan_code)
    assert _diagnostic(one) == _diagnostic(expected)
    assert _diagnostic(two) == _diagnostic(expected)
    assert one is not two


def test_one_compile_and_combined_index_walk(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = (f"# {tmp_path.name}\nimport pytest\n"
            "def outer(parameter):\n"
            "    @pytest.fixture\n"
            "    def inner(request):\n"
            "        return ('alpha', ('beta', 'gamma'))\n"
            "    return inner\n")
    expected_vocab = scan_gate.module_vocab(text)
    expected_def_vocab = scan_gate.module_def_vocab(text)
    calls: Counter[str] = Counter()
    original_compile = builtins.compile
    original_build = getattr(source_scan_index, "_build")

    def counted_compile(source: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(source, (ast.Module, ast.Expression, ast.Interactive)):
            calls["compile"] += 1
        return original_compile(source, *args, **kwargs)

    def counted_build(code: types.CodeType) -> Any:
        calls["index"] += 1
        return original_build(code)

    monkeypatch.setattr(builtins, "compile", counted_compile)
    monkeypatch.setattr(source_scan_index, "_build", counted_build)
    narrow = source_scan_index.vocab(text)
    broad = source_scan_index.def_vocab(text)
    assert narrow == expected_vocab
    assert broad == expected_def_vocab
    assert "parameter" not in narrow[1]
    assert "parameter" in broad[0]
    assert broad[1]
    assert source_scan_index.vocab(text) is narrow
    assert source_scan_index.def_vocab(text) is broad
    assert calls == {"compile": 1, "index": 1}


def test_generation_changed_during_read_fails_explicitly(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "racing.py"
    path.write_text("old = 1\n", encoding="utf-8")
    original_open = Path.open

    @contextmanager
    def changing_open(file: Path, *args: Any,
                      **kwargs: Any) -> Iterator[Any]:
        with original_open(file, *args, **kwargs) as handle:
            with original_open(file, "wb") as writer:
                writer.write(b"new_value = 2\n")
            yield handle

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", changing_open)
        assert isinstance(_failure(lambda: source_scan_cache.snapshot(path)),
                          RuntimeError)
    assert source_scan_cache.source(path) == "new_value = 2\n"
