"""Estimate Bash line churn from command text without executing commands.

Literal heredocs, diffs, Python edits, printf/echo output and supported sed
payloads contribute their declared line counts. Replacements count each
old/new payload once, without discovering match counts or asserting that a
successful command changed the file. Recognized writes with unknown addition
sizes contribute one added line per Bash call only when no additions were
already counted. Known empty/no-addition operations remain zero additions;
overwrite/copy deletions remain unknown and contribute zero.

Read-only commands, null sinks and opaque script invocations are not file-write
evidence. Python write intent can be explicit even when the target path is
unknown; python_write_paths still returns only recoverable paths. Error
handling retains the existing proven heredoc-write-before-later-error rule.
"""
from __future__ import annotations

import ast
import functools
import posixpath
import re
import shlex
import warnings
from dataclasses import dataclass

from backend.bash_literals import MAX_LITERAL_CHARS, ShellWord, effects_from_tokens, shell_tokens

# Commands longer than this are pathological (a base64 blob, a giant
# generated fixture); parsing them buys nothing and costs ingest time.
MAX_COMMAND_CHARS = 1_000_000

_HEREDOC_OPEN = re.compile(
    r"<<(-?)\s*(?:'([^']*)'|\"([^\"]*)\"|([A-Za-z_][A-Za-z0-9_]*))"
)

# A redirect to a path, ignoring fd duplication (`2>&1`, `>&2`).
_REDIRECT = re.compile(r"(?<![0-9<>&])>>?\s*(?:'([^']+)'|\"([^\"]+)\"|([^\s'\";&|<>()]+))")

_CAT = re.compile(r"(?:^|[|;&(]|\s)cat\b")
_TEE = re.compile(r"(?:^|[|;&(]|\s)tee\b")
_PATCH = re.compile(r"(?:^|[|;&(]|\s)(?:git\s+apply|patch)\b")
# `python3 -` / `python -` / `python3.13 -`: an interpreter reading
# stdin — bare, or by path (`.venv/bin/python -`, `/usr/bin/python3 -`),
# which is how a repo with a venv spells it.
_PYTHON_WORD = r"(?:^|[|;&(]|\s)(?:\S*/)?python(?:3(?:\.\d+)?)?"
_PYTHON_STDIN = re.compile(_PYTHON_WORD + r"\s+-(?:\s|$)")
_PYTHON_DASH_C = re.compile(_PYTHON_WORD + r"\s+-c\s")

_NULL_SINKS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")

# Attribute writes that put bytes on disk. `sys.stdout.write` is excluded
# by name below — it is the one common `.write` that is not a file.
_WRITE_ATTRS = ("write_text", "write_bytes", "writelines", "write")
_STD_STREAMS = ("stdout", "stderr")


def count_lines(s: str) -> int:
    """Lines in a payload string, git-style: "a\\nb\\n" and "a\\nb" are
    both 2 lines; "" is 0. Mirrors parse._line_count."""
    if not s:
        return 0
    return s.count("\n") + (0 if s.endswith("\n") else 1)


def _split_heredocs(command: str) -> tuple[list[tuple[str, str]], str]:
    """Split a command into (context, body) heredoc pairs plus the
    command text with every body removed.

    `context` is the opener's line minus the `<<TAG` tokens themselves,
    so a redirect written on either side of the opener is visible to the
    classifier. In leftover text, each opener becomes a descriptor-close
    marker (`<&-`): the body is analyzed separately, but the receiving stage
    must still be disconnected from upstream pipeline input. These markers
    are parser metadata, never executed commands. The leftover text is also
    what the `python -c` scan runs on:
    a `-c` mentioned INSIDE a heredoc body is that body's business, not
    a second script.
    """
    lines = command.split("\n")
    pairs: list[tuple[str, str]] = []
    leftover: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        openers = list(_HEREDOC_OPEN.finditer(line))
        leftover.append(_HEREDOC_OPEN.sub("<&-", line))
        i += 1
        if not openers:
            continue
        context = _HEREDOC_OPEN.sub(" ", line)
        for m in openers:
            dash = m.group(1) == "-"
            tag = m.group(2) or m.group(3) or m.group(4) or ""
            body: list[str] = []
            while i < len(lines):
                cur = lines[i]
                probe = cur.lstrip("\t") if dash else cur
                if probe.strip() == tag:
                    i += 1
                    break
                body.append(cur)
                i += 1
            pairs.append((context, "\n".join(body)))
    return pairs, "\n".join(leftover)


def _writes_to_a_file(context: str) -> bool:
    """True when this heredoc's body lands in a file verbatim."""
    if _TEE.search(context):
        return True
    if not _CAT.search(context):
        return False
    targets = [m.group(1) or m.group(2) or m.group(3)
               for m in _REDIRECT.finditer(context)]
    return any(t and t not in _NULL_SINKS for t in targets)


def _diff_churn(body: str) -> tuple[int, int]:
    """+/- hunk lines of a unified diff. The `+++`/`---` file headers
    name files, they do not change lines."""
    added = deleted = 0
    for line in body.split("\n"):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added, deleted


def _const_str(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _path_literal(node: ast.expr) -> str | None:
    """A string literal, or `Path('lit')` / `pathlib.Path('lit')` —
    the two spellings of a path that is written down in the text."""
    literal = _const_str(node)
    if literal is not None:
        return literal
    if not (isinstance(node, ast.Call) and node.args):
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else "")
    if name != "Path":
        return None
    return _const_str(node.args[0])


def _resolve_str(node: ast.expr, consts: dict[str, str]) -> str | None:
    """Bounded literal strings, string aliases and string addition."""
    budget = 512

    def resolve(expr: ast.expr, depth: int) -> str | None:
        nonlocal budget
        budget -= 1
        if depth > 32 or budget < 0:
            return None
        value = _const_str(expr)
        if isinstance(expr, ast.Name):
            value = consts.get(expr.id)
        elif isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            left, right = resolve(expr.left, depth + 1), resolve(expr.right, depth + 1)
            if left is not None and right is not None and len(left) + len(right) <= MAX_LITERAL_CHARS:
                value = left + right
        if isinstance(value, _PathValue):
            return None
        return value if value is not None and len(value) <= MAX_LITERAL_CHARS else None

    return resolve(node, 0)


class _PathValue(str):
    """A known Path object must not participate in string concatenation."""


def _resolve_binding(node: ast.expr, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else ""
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            name = func.value.id + "." + func.attr
        if name in ("Path", "pathlib.Path"):
            value = _resolve_str(node.args[0], consts)
            return _PathValue(value) if value is not None else None
    return _resolve_str(node, consts)


def _stored_names(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            names.add(child.id)
        elif isinstance(child, ast.alias):
            names.add(child.asname or child.name.split(".")[0])
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(child.name)
    return names


def _statement_nodes(stmt: ast.AST) -> list[ast.AST]:
    """Walk executed statement syntax without entering deferred scopes."""
    nodes: list[ast.AST] = []
    pending = [stmt]
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.GeneratorExp)):
            continue
        nodes.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return nodes


def _loop_exit(stmt: ast.stmt) -> str | None:
    """Direct exits stop traversal; nested exits require refusing state carry."""
    if isinstance(stmt, ast.Continue):
        return "continue"
    if isinstance(stmt, ast.Break):
        return "break"
    # Nested for loops handle their own exits. For other compound statements
    # execution order and finally/exception handling are outside this parser.
    if not isinstance(stmt, ast.For) and any(
            isinstance(node, (ast.Continue, ast.Break, ast.Return, ast.Raise))
            for node in _statement_nodes(stmt)):
        return "uncertain"
    return None


def _loop_paths(loop: ast.For, consts: dict[str, str],
                helpers: dict[str, _Helper]) -> list[str]:
    """Candidate writes through bounded literal loops, without churn scaling.

    Iteration and branch execution can depend on file state. Enumerate paths
    only; carry bindings in order within each candidate, invalidate them after
    uncertain control flow and stop expansion before combinatorial growth.
    """
    paths: list[str] = []
    budget = 512

    def visit_loop(stmt: ast.For, bindings: dict[str, str], depth: int) -> None:
        if isinstance(stmt.target, ast.Name) and isinstance(stmt.iter, (ast.List, ast.Tuple)) and len(stmt.iter.elts) <= 64:
            values = [_resolve_str(e, bindings) for e in stmt.iter.elts]
            if all(v is not None for v in values):
                iteration_bindings = bindings.copy()
                for value in values:
                    assert value is not None
                    iteration_bindings[stmt.target.id] = value
                    if visit(stmt.body, iteration_bindings, depth + 1) in ("break", "uncertain"):
                        break
        for name in _stored_names(stmt):
            bindings.pop(name, None)

    def visit(statements: list[ast.stmt], bindings: dict[str, str], depth: int) -> str | None:
        nonlocal budget
        if depth > 8:
            return "uncertain"
        for stmt in statements:
            budget -= 1
            if budget < 0:
                return "uncertain"
            control = _loop_exit(stmt)
            if control:
                return control
            if isinstance(stmt, ast.For):
                visit_loop(stmt, bindings, depth)
            elif isinstance(stmt, ast.If):
                # A condition's assignment expressions run before either
                # branch; short-circuit evaluation prevents assuming values.
                condition_stores = _stored_names(stmt.test)
                condition_bindings = {k: v for k, v in bindings.items()
                                      if k not in condition_stores}
                visit(stmt.body, condition_bindings.copy(), depth + 1)
                visit(stmt.orelse, condition_bindings.copy(), depth + 1)
                for name in _stored_names(stmt):
                    bindings.pop(name, None)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bindings.pop(stmt.name, None)
            else:
                _, _, found = _statement_effects(stmt, bindings, helpers)
                paths.extend(p for p in found if p not in paths)
        return None

    visit([loop], consts.copy(), 0)
    return paths if budget >= 0 else []


def _is_std_stream_write(func: ast.Attribute) -> bool:
    """`sys.stdout.write(...)` / `sys.stderr.write(...)` — not a file."""
    value = func.value
    if isinstance(value, ast.Attribute) and value.attr in _STD_STREAMS:
        return True
    return isinstance(value, ast.Name) and value.id in _STD_STREAMS


def _opens_for_writing(node: ast.Call) -> bool:
    """`open(path, 'w')` / `open(path, mode='a')` with a literal mode."""
    func = node.func
    if not (isinstance(func, ast.Name) and func.id == "open"):
        return False
    modes = [_const_str(a) for a in node.args[1:2]]
    modes += [_const_str(kw.value) for kw in node.keywords
              if kw.arg == "mode"]
    return any(m and any(ch in m for ch in "wax") for m in modes)


def _is_file_write_attr(func: ast.expr) -> bool:
    """`x.write_text(...)`, `x.write(...)` and kin, minus the std streams."""
    if not (isinstance(func, ast.Attribute) and func.attr in _WRITE_ATTRS):
        return False
    return not (func.attr == "write" and _is_std_stream_write(func))


def _script_writes_a_file(calls: list[ast.Call]) -> bool:
    """Does this script put bytes on disk at all? A reader that
    `.replace()`s on its way to stdout changes nothing."""
    return any(_is_file_write_attr(n.func) or _opens_for_writing(n)
               for n in calls)


def _is_replace(func: ast.expr) -> bool:
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "replace":
        return True
    return (func.attr == "sub" and isinstance(func.value, ast.Name)
            and func.value.id == "re")


def _no_additions(expr: ast.expr, consts: dict[str, str]) -> bool:
    if isinstance(expr, ast.Constant) and expr.value == b"":
        return True
    if isinstance(expr, (ast.List, ast.Tuple)) and not expr.elts:
        return True
    if isinstance(expr, ast.Call) and _is_replace(expr.func) and len(expr.args) >= 2:
        expr = expr.args[1]
    return _resolve_str(expr, consts) == ""


@dataclass(frozen=True)
class _Helper:
    """A helper's payload templates under the bindings at each write/edit."""
    params: tuple[str, ...]
    path: str | None
    payloads: tuple[ast.expr, ...]


def _param(node: ast.expr, params: tuple[str, ...]) -> str | None:
    if isinstance(node, ast.Name) and node.id in params:
        return node.id
    if isinstance(node, ast.Call) and node.args:  # Path(param)
        return _param(node.args[0], params) if _path_literal(
            ast.Call(node.func, [ast.Constant("x")], [])) == "x" else None
    return None


def _payload_snapshot(expr: ast.expr, bindings: dict[str, ast.expr], depth: int = 0) -> ast.expr:
    """Capture bounded literal provenance without mutating the source AST."""
    if depth > 32:
        return ast.Constant(None)
    if isinstance(expr, ast.Name):
        # Return the previous snapshot as-is: do not reinterpret its symbolic
        # caller parameter through a later local rebinding of that same name.
        return bindings.get(expr.id, ast.Constant(None))
    if isinstance(expr, ast.Constant):
        return expr
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return ast.BinOp(_payload_snapshot(expr.left, bindings, depth + 1), ast.Add(),
                         _payload_snapshot(expr.right, bindings, depth + 1))
    if isinstance(expr, ast.Call) and _is_replace(expr.func):
        func = expr.func
        assert isinstance(func, ast.Attribute)
        if func.attr == "replace":
            func = ast.Attribute(_payload_snapshot(func.value, bindings, depth + 1), "replace", ast.Load())
        return ast.Call(func, [_payload_snapshot(arg, bindings, depth + 1)
                               for arg in expr.args], [])
    return expr if isinstance(expr, (ast.List, ast.Tuple)) and not expr.elts else ast.Constant(None)


def _helper_template(fd: ast.FunctionDef) -> _Helper:
    params = tuple(arg.arg for arg in fd.args.args)
    bindings: dict[str, ast.expr] = {name: ast.Name(name, ast.Load()) for name in params}
    payloads: list[ast.expr] = []
    writes = _PythonWrites()
    path = None
    for stmt in fd.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings.pop(stmt.name, None)
            continue
        target = (stmt.targets[0] if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                  and isinstance(stmt.targets[0], ast.Name) else None)
        stores = _stored_names(stmt)
        view = bindings if target else {k: v for k, v in bindings.items() if k not in stores}
        allowed = writes.observe(stmt, {}, {})
        for node in _statement_nodes(stmt):
            if not isinstance(node, ast.Call):
                continue
            if id(node) in allowed:
                payloads.append(_payload_snapshot(node.args[0], view))
            if _opens_for_writing(node) and node.args:
                path = _param(node.args[0], params) or path
            elif _is_file_write_attr(node.func):
                value = node.func.value  # type: ignore[attr-defined]
                if isinstance(value, ast.Call):
                    path = _param(value, params) or path
        value = _payload_snapshot(stmt.value, bindings) if isinstance(stmt, ast.Assign) else ast.Constant(None)
        for name in stores:
            bindings.pop(name, None)
        if target is not None:
            bindings[target.id] = value
    return _Helper(params, path, tuple(payloads))


def _helper_specs(tree: ast.Module) -> dict[str, _Helper]:
    """Precompute module-level helper templates once per Python source scan."""
    specs: dict[str, _Helper] = {}
    for fd in tree.body:
        if isinstance(fd, ast.FunctionDef):
            spec = _helper_template(fd)
            if spec.path or spec.payloads:
                specs[fd.name] = spec
    return specs


def _helper_args(node: ast.Call, spec: _Helper) -> dict[str, ast.expr]:
    bound = dict(zip(spec.params, node.args))
    bound.update({kw.arg: kw.value for kw in node.keywords if kw.arg})
    return bound


def _payload_provenance(payload: ast.expr) -> tuple[list[ast.Call], list[ast.expr]] | None:
    """Edits on the written value's lineage, plus unedited value fragments.

    A replacement's pattern/replacement arguments do not edit its input file.
    Follow its subject instead. Shared snapshots are visited once, and the
    budget bounds long chains and alias/concatenation graphs.
    """
    pending = [(payload, False)]
    seen: set[tuple[int, bool]] = set()
    edits: list[ast.Call] = []
    fragments: list[ast.expr] = []
    while pending:
        node, subject_only = pending.pop()
        key = (id(node), subject_only)
        if key in seen:
            continue
        if len(seen) >= 512:
            return None
        seen.add(key)
        if isinstance(node, ast.Call) and _is_replace(node.func) and len(node.args) >= 2:
            edits.append(node)
            assert isinstance(node.func, ast.Attribute)
            if node.func.attr == "sub":
                pending.extend((arg, True) for arg in node.args[2:3])
            else:
                pending.append((node.func.value, True))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            pending.extend(((node.left, subject_only), (node.right, subject_only)))
        elif not subject_only:
            fragments.append(node)
    return edits, fragments


def _helper_churn(spec: _Helper, bound: dict[str, ast.expr], consts: dict[str, str]) -> tuple[int, int, bool]:
    bindings = {name: bound_value for name, arg in bound.items()
                if (bound_value := _resolve_binding(arg, consts)) is not None}

    def literal(expr: ast.expr) -> str | None:
        value = bound.get(expr.id, expr) if isinstance(expr, ast.Name) else expr
        if isinstance(value, ast.Constant) and isinstance(value.value, bytes):
            return value.value.decode("latin1")
        if isinstance(value, (ast.List, ast.Tuple)) and not value.elts:
            return ""
        return _resolve_str(expr, bindings)

    added = deleted = 0
    unknown = False
    seen_edits: set[int] = set()
    for payload in spec.payloads:
        provenance = _payload_provenance(payload)
        if provenance is None:
            unknown = True
        elif not provenance[0]:
            new = literal(payload)
            unknown |= new is None
            added += count_lines(new or "")
        else:
            unknown |= any(literal(part) is None for part in provenance[1])
            for edit in provenance[0]:
                if id(edit) in seen_edits:
                    continue
                seen_edits.add(id(edit))
                old, new = literal(edit.args[0]), literal(edit.args[1])
                unknown |= new != "" and (old is None or new is None)
                if old is not None and new is not None:
                    added += count_lines(new)
                    deleted += count_lines(old)
    return added, deleted, unknown


def _call_effects(node: ast.Call, consts: dict[str, str],
                  helpers: dict[str, _Helper]
                  ) -> tuple[int, int, str | None]:
    """(added, deleted, written path) from ONE call, counted only when
    its strings are known from the text: a literal, a name whose
    binding in force is a literal, or a same-script helper called
    with either."""
    func = node.func
    if isinstance(func, ast.Name) and func.id in helpers:
        spec = helpers[func.id]
        bound = _helper_args(node, spec)
        added, deleted, _ = _helper_churn(spec, bound, consts)
        path = None
        if spec.path:
            path = _resolve_binding(bound.get(spec.path, ast.Constant(None)), consts)
        return added, deleted, path
    if _is_replace(func) and len(node.args) >= 2:
        old = _resolve_str(node.args[0], consts)
        new = _resolve_str(node.args[1], consts)
        if old is None or new is None:
            return 0, 0, None
        return count_lines(new), count_lines(old), None
    if _opens_for_writing(node) and node.args:
        return 0, 0, _resolve_binding(node.args[0], consts)
    if _is_file_write_attr(func):
        assert isinstance(func, ast.Attribute)
        path = _resolve_binding(func.value, consts)
        literal = _resolve_str(node.args[0], consts) if node.args else None
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, bytes):
            literal = node.args[0].value.decode("latin1")
        return (count_lines(literal) if literal is not None else 0), 0, path
    return 0, 0, None


@dataclass(frozen=True)
class _WriteTarget:
    """A syntactic file receiver, with an optional resolved path."""
    path: str | None
    is_path: bool


class _PythonWrites:
    """Track explicit file receivers and unknown output without inventing paths."""

    def __init__(self) -> None:
        self.targets: dict[str, _WriteTarget] = {}
        self.known_payloads: set[str] = set()
        self.unknown = False
        self.recognized = False

    def target(self, expr: ast.expr, consts: dict[str, str]) -> _WriteTarget | None:
        if isinstance(expr, ast.Name):
            return self.targets.get(expr.id)
        if isinstance(expr, ast.Call):
            if _opens_for_writing(expr) and expr.args:
                return _WriteTarget(_resolve_binding(expr.args[0], consts), False)
            func = expr.func
            is_path = (isinstance(func, ast.Name) and func.id == "Path") or (
                isinstance(func, ast.Attribute) and func.attr == "Path"
                and isinstance(func.value, ast.Name) and func.value.id == "pathlib")
            if is_path and expr.args:
                return _WriteTarget(_resolve_str(expr.args[0], consts), True)
        return None

    def known(self, expr: ast.expr, consts: dict[str, str]) -> bool:
        if _resolve_str(expr, consts) is not None:
            return True
        if isinstance(expr, ast.Constant) and isinstance(expr.value, bytes):
            return True
        if isinstance(expr, (ast.List, ast.Tuple)) and not expr.elts:
            return True
        if isinstance(expr, ast.Name):
            return expr.id in self.known_payloads
        if isinstance(expr, ast.Call) and _is_replace(expr.func) and len(expr.args) >= 2:
            new = _resolve_str(expr.args[1], consts)
            return new == "" or (new is not None and _resolve_str(expr.args[0], consts) is not None)
        return False

    def _call(self, node: ast.Call, consts: dict[str, str],
              helpers: dict[str, _Helper], in_loop: bool) -> bool:
        func = node.func
        if _opens_for_writing(node) and node.args:
            self.recognized |= _resolve_binding(node.args[0], consts) not in _NULL_SINKS
        if isinstance(func, ast.Name) and func.id in helpers:
            spec = helpers[func.id]
            if spec.path or spec.payloads:
                bound = _helper_args(node, spec)
                path = _resolve_binding(bound.get(spec.path or "", ast.Constant(None)), consts)
                if path not in _NULL_SINKS:
                    self.recognized = True
                    self.unknown |= _helper_churn(spec, bound, consts)[2]
        if not isinstance(func, ast.Attribute) or func.attr not in _WRITE_ATTRS or not node.args:
            return False
        receiver = self.target(func.value, consts)
        if receiver is None or receiver.path in _NULL_SINKS:
            return False
        if receiver.is_path != (func.attr in ("write_text", "write_bytes")):
            return False
        self.recognized = True
        self.unknown |= not self.known(node.args[0], consts)
        if in_loop and not _no_additions(node.args[0], consts):
            self.unknown = True
        return True

    def _bind(self, stmt: ast.stmt, consts: dict[str, str]) -> None:
        target = self.target(stmt.value, consts) if isinstance(stmt, ast.Assign) else None
        known = isinstance(stmt, ast.Assign) and self.known(stmt.value, consts)
        for name in _stored_names(stmt):
            self.targets.pop(name, None)
            self.known_payloads.discard(name)
        if isinstance(stmt, ast.Assign):
            for name in stmt.targets:
                if isinstance(name, ast.Name):
                    if target is not None:
                        self.targets[name.id] = target
                    if known:
                        self.known_payloads.add(name.id)

    def observe(self, stmt: ast.stmt, consts: dict[str, str], helpers: dict[str, _Helper]) -> set[int]:
        # Compound stores never establish bindings for later statements.
        saved, saved_payloads = self.targets.copy(), self.known_payloads.copy()
        allowed: set[int] = set()
        if isinstance(stmt, (ast.For, ast.If)):
            for branch in (stmt.body, stmt.orelse):
                for child in branch:
                    allowed.update(self.observe(child, consts, helpers))
                self.targets, self.known_payloads = saved.copy(), saved_payloads.copy()
        if isinstance(stmt, ast.With):
            for item in stmt.items:
                if isinstance(item.optional_vars, ast.Name):
                    target = self.target(item.context_expr, consts)
                    if target is not None:
                        self.targets[item.optional_vars.id] = target
        for node in _statement_nodes(stmt):
            if isinstance(node, ast.Call) and self._call(node, consts, helpers, isinstance(stmt, ast.For)):
                allowed.add(id(node))
        self.targets, self.known_payloads = saved, saved_payloads
        self._bind(stmt, consts)
        return allowed


@functools.lru_cache(maxsize=512)
def _python_scan(src: str) -> tuple[int, int, tuple[str, ...], bool]:
    """(added, deleted, written paths, unknown addition size) from Python text.

    Module-level statements are walked IN ORDER with the bindings in
    force: `old = ...` above a replace is the literal that replace
    used, and a later `old = ...` rebinds it for the statements below.
    A name bound to anything but a literal, or bound inside a compound
    statement (a loop target, a `with ... as`), is unknown from there
    on. Literal list/tuple loops enumerate candidate paths under ordered
    local bindings; unresolved loop sizes use the per-call fallback. Function bodies are counted
    once per CALL through the helper table, never on their own.
    """
    try:
        # Transcript scripts are arbitrary third-party text: compiling
        # them raises SyntaxWarning for things like a stray `\$`, and
        # ingest compiles hundreds of thousands of them. Left alone
        # that is thousands of journald lines per run about files
        # nobody is going to fix.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(src)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return 0, 0, (), False

    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    if not _script_writes_a_file(calls):
        return 0, 0, (), False

    helpers = _helper_specs(tree)
    consts: dict[str, str] = {}
    added = deleted = 0
    paths: list[str] = []
    writes = _PythonWrites()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        allowed = writes.observe(stmt, consts, helpers)
        a, d, stmt_paths = _statement_effects(stmt, consts, helpers, allowed)
        added += a
        deleted += d
        paths.extend(p for p in stmt_paths if p not in paths)
    return (added, deleted, tuple(paths), writes.unknown) if writes.recognized else (0, 0, (), False)


def _statement_effects(stmt: ast.stmt, consts: dict[str, str],
                       helpers: dict[str, _Helper], allowed_writes: set[int] | None = None
                       ) -> tuple[int, int, list[str]]:
    """Effects of one module-level statement under the bindings in
    force, then the statement's own effect on those bindings."""
    if isinstance(stmt, ast.For):
        paths = _loop_paths(stmt, consts, helpers)
        for name in _stored_names(stmt):
            consts.pop(name, None)
        return 0, 0, paths
    target = (stmt.targets[0] if isinstance(stmt, ast.Assign)
              and len(stmt.targets) == 1
              and isinstance(stmt.targets[0], ast.Name) else None)
    shadow = set() if target else _stored_names(stmt)
    view = {k: v for k, v in consts.items() if k not in shadow}
    added = deleted = 0
    paths: list[str] = []
    for node in _statement_nodes(stmt):
        if isinstance(node, ast.Call):
            if allowed_writes is not None and _is_file_write_attr(node.func) and id(node) not in allowed_writes:
                continue
            effect = _call_effects(node, view, helpers)
            added += effect[0]
            deleted += effect[1]
            if effect[2] and effect[2] not in paths:
                paths.append(effect[2])
    if target is not None:
        assert isinstance(stmt, ast.Assign)
        literal = _resolve_binding(stmt.value, consts)
        if literal is not None:
            consts[target.id] = literal
        else:
            consts.pop(target.id, None)
    for name in shadow:
        consts.pop(name, None)
    return added, deleted, paths


def _python_churn(src: str) -> tuple[int, int]:
    """Churn enumerable from a python script's own source."""
    added, deleted, _, _ = _python_scan(src)
    return added, deleted


def _dash_c_sources(text: str) -> list[str]:
    """Script bodies passed as `python3 -c '<code>'`."""
    out: list[str] = []
    for m in _PYTHON_DASH_C.finditer(text):
        try:
            tokens = shlex.split(text[m.end():], comments=False, posix=True)
        except ValueError:
            continue
        if tokens:
            out.append(tokens[0])
    return out


class BashCommand:
    """Lazy syntax shared by churn and access analysis of ONE tool call.

    These consumers used to split heredocs three times and tokenize twice.
    Keep their common input on the call, rather than a global mutable cache:
    concurrent file parsers and separate cwd resolutions remain independent.
    Consumers must treat the cached syntax as read-only.
    """

    def __init__(self, command: str) -> None:
        self.command = command

    @functools.cached_property
    def parts(self) -> tuple[list[tuple[str, str]], str]:
        """Heredoc contexts/bodies and the remaining shell command."""
        return _split_heredocs(self.command)

    @functools.cached_property
    def tokens(self) -> list[ShellWord]:
        """Shell words with quote/expansion provenance, excluding heredoc bodies."""
        return shell_tokens(self.parts[1])

    @functools.cached_property
    def dash_c_sources(self) -> list[str]:
        """Inline Python bodies, decoded once for both consumers."""
        return _dash_c_sources(self.parts[1])

    def churn(self) -> tuple[int, int]:
        """(lines_added, lines_deleted) estimates from this command."""
        if not self.command or len(self.command) > MAX_COMMAND_CHARS:
            return 0, 0
        added = deleted = 0
        unknown = False
        for context, body in self.parts[0]:
            if _PATCH.search(context):
                a, d = _diff_churn(body)
            elif _PYTHON_STDIN.search(context):
                a, d, _, unresolved = _python_scan(body)
                unknown |= unresolved
            elif _writes_to_a_file(context):
                a, d = (count_lines(body + "\n") if body else 0), 0
            else:
                a, d = 0, 0
            added += a
            deleted += d
        for src in self.dash_c_sources:
            a, d, _, unresolved = _python_scan(src)
            unknown |= unresolved
            added += a
            deleted += d
        for payload, removed in effects_from_tokens(self.tokens):
            unknown |= payload is None
            added += count_lines(payload or "")
            deleted += count_lines(removed)
        return added or int(unknown), deleted

    def write_paths(self) -> list[str]:
        """Python write targets, before the caller resolves their cwd."""
        if not self.command or len(self.command) > MAX_COMMAND_CHARS:
            return []
        sources = [body for context, body in self.parts[0] if _PYTHON_STDIN.search(context)]
        sources.extend(self.dash_c_sources)
        paths: list[str] = []
        for src in sources:
            for path in _python_scan(src)[2]:
                if path not in paths:
                    paths.append(path)
        return paths


def bash_churn(command: str) -> tuple[int, int]:
    """(lines_added, lines_deleted) estimates for one Bash call."""
    return BashCommand(command).churn()


def python_write_paths(command: str) -> list[str]:
    """Paths the command's python bodies open for writing, as written
    in the text — relative ones are the interpreter's cwd's business,
    so the caller resolves them."""
    return BashCommand(command).write_paths()


# Shell-level failures of the write itself. The first two poison every
# write in the command; the rest name the path they refused.
_DISK_FAILURE = re.compile(r"No space left on device|Read-only file system")
_TARGET_FAILURE = ("No such file or directory", "Permission denied",
                   "Is a directory", "cannot create")
_CANNOT_CREATE_DIR = re.compile(r"cannot create directory [`'\"]([^'`\"]+)")
_STAGE_SPLIT = re.compile(r"&&|\|\||;|\||\n")


def _verbatim_targets(context: str) -> list[str]:
    """Files a `cat > F` / `tee F` heredoc opener lands its body in."""
    targets = [m.group(1) or m.group(2) or m.group(3)
               for m in _REDIRECT.finditer(context)]
    if _TEE.search(context):
        try:
            tokens = shlex.split(context, posix=True)
        except ValueError:
            tokens = []
        seen_tee = False
        for tok in tokens:
            if seen_tee and tok and not tok.startswith("-"):
                if tok in ("|", "||", "&&", ";"):
                    break
                targets.append(tok)
            seen_tee = seen_tee or posixpath.basename(tok) == "tee"
    return [t for t in targets if t and t not in _NULL_SINKS]


def churn_survives_error(command: str, error_text: str) -> bool:
    """Whether an errored result leaves the command's counted churn
    standing.

    The result's exit status is the LAST stage's. A `cat > f <<EOF`
    heredoc followed by `python3 f` that exits 1 wrote f all the same,
    and that shape — write, then run what was written — is how most
    editing under bypass permissions happens, so zeroing every errored
    call throws those writes away.

    Only a VERBATIM heredoc write (`cat`/`tee`) survives, and only when
    the command has some other stage to fail in and the error text does
    not report the write itself failing (a missing directory, a denied
    path, a full disk). A python body that raised may have raised before
    its write — `assert old in t` is put there to do exactly that — so
    it never survives; a patch that did not apply is the same.
    Approximation: a preceding `&&` stage failing without naming the
    target (a `mkdir` denied on a parent) still counts the heredoc.
    """
    if not command or len(command) > MAX_COMMAND_CHARS:
        return False
    pairs, outside = _split_heredocs(command)
    targets: list[str] = []
    for context, body in pairs:
        if _PATCH.search(context) or _PYTHON_STDIN.search(context):
            continue
        if body and _writes_to_a_file(context):
            targets.extend(_verbatim_targets(context))
    if not targets:
        return False
    stages = [s for s in _STAGE_SPLIT.split(outside) if s.strip()]
    return len(stages) >= 2 and not _write_reported_failed(targets, error_text)


def _write_reported_failed(targets: list[str], error_text: str) -> bool:
    """Does the error text say the write to one of `targets` failed?"""
    if _DISK_FAILURE.search(error_text):
        return True
    names = {posixpath.basename(t) for t in targets}
    for line in error_text.splitlines():
        if not any(marker in line for marker in _TARGET_FAILURE):
            continue
        if any(t in line for t in targets) or any(n in line for n in names):
            return True
        m = _CANNOT_CREATE_DIR.search(line)
        if m and any(t.startswith(m.group(1).rstrip("/") + "/")
                     for t in targets):
            return True
    return False
