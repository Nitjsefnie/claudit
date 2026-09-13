"""Line churn recovered from Bash command TEXT.

`Edit` and `Write` are not where most editing happens. In a
bypass-permissions session the model is told to work through the shell,
so a file gets written by a heredoc, patched by an inline diff, or
rewritten by a python one-liner — 197k Bash calls against 41k
Edit+Write in the live corpus. None of that reached the churn panels
before this module existed.

The rule here is DIRECT ENUMERABILITY: a shape counts only when the
number of lines is readable off the call arguments themselves, with no
execution and no inference about the state of the disk. Everything else
contributes 0/0 — an honest undercount, never an estimate dressed as a
measurement. Concretely that means:

  counted    heredoc body redirected into a file (`cat > F`, `cat >> F`,
             `tee F`) — added, 0 deleted, exactly as `Write` is counted;
             inline `git apply` / `patch` diffs, by their +/- hunk lines;
             python read/replace/write bodies (heredoc or `-c`), by the
             string LITERALS passed to `.replace()` / `re.sub()` — given
             inline, through a name whose binding IN FORCE at that
             statement is a literal, or through a helper defined in the
             same script and called with literals; bounded string addition;
             literal printf output reaching a file unchanged, including tee
             and flat redirected brace groups; numeric-addressed sed append
             payloads, counted once without asserting address applicability.

  not        commit messages (`git commit -F -`, `-m "$(cat <<EOF)"`),
  counted    heredocs feeding psql/jq/node/ssh, output-capture redirects
             (`cmd > out.txt` — those bytes come from running it),
             sed substitutions and Perl edits (the matches need the file),
             and Python replacements whose arguments remain runtime values.

The python scan also recovers the PATHS a body opens for writing
(`python_write_paths`), the same way `cat > F` names F — and
`churn_survives_error` answers whether an errored result can be taken
as proof the counted write never landed.
"""
from __future__ import annotations

import ast
import functools
import posixpath
import re
import shlex
import warnings
from dataclasses import dataclass

from backend.bash_literals import MAX_LITERAL_CHARS, shell_payloads

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
    classifier. The leftover text is what the `python -c` scan runs on:
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
        leftover.append(_HEREDOC_OPEN.sub(" ", line))
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


def _loop_paths(loop: ast.For, consts: dict[str, str],
                helpers: dict[str, _Helper]) -> list[str]:
    """Candidate writes through bounded literal loops, without churn scaling.

    Iteration and branch execution can depend on file state. Enumerate paths
    only; carry bindings in order within each candidate, invalidate them after
    uncertain control flow and stop expansion before combinatorial growth.
    """
    paths: list[str] = []
    budget = 512

    def visit(statements: list[ast.stmt], bindings: dict[str, str], depth: int) -> None:
        nonlocal budget
        if depth > 8:
            return
        for stmt in statements:
            budget -= 1
            if budget < 0:
                return
            if isinstance(stmt, ast.For):
                if isinstance(stmt.target, ast.Name) and isinstance(stmt.iter, (ast.List, ast.Tuple)) and len(stmt.iter.elts) <= 64:
                    values = [_resolve_str(e, bindings) for e in stmt.iter.elts]
                    if all(v is not None for v in values):
                        iteration_bindings = bindings.copy()
                        for value in values:
                            assert value is not None
                            iteration_bindings[stmt.target.id] = value
                            visit(stmt.body, iteration_bindings, depth + 1)
                for name in _stored_names(stmt):
                    bindings.pop(name, None)
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
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else "")
    if name != "open":
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


@dataclass(frozen=True)
class _Helper:
    """A function defined in the script whose body does the edit on
    its parameters: which parameter it opens for writing, and which
    two it hands to `.replace()` / `re.sub()`."""
    params: tuple[str, ...]
    path: str | None
    old: str | None
    new: str | None


def _param(node: ast.expr, params: tuple[str, ...]) -> str | None:
    if isinstance(node, ast.Name) and node.id in params:
        return node.id
    if isinstance(node, ast.Call) and node.args:  # Path(param)
        return _param(node.args[0], params) if _path_literal(
            ast.Call(node.func, [ast.Constant("x")], [])) == "x" else None
    return None


def _helper_specs(tree: ast.Module) -> dict[str, _Helper]:
    """Module-level functions whose body writes a parameter path and/or
    replaces one parameter with another. `sub(path, old, new)` is the
    shape every multi-file edit script converges on."""
    specs: dict[str, _Helper] = {}
    for fd in tree.body:
        if not isinstance(fd, ast.FunctionDef):
            continue
        params = tuple(a.arg for a in fd.args.args)
        path = old = new = None
        for node in ast.walk(fd):
            if not isinstance(node, ast.Call):
                continue
            if _is_replace(node.func) and len(node.args) >= 2:
                o, n = _param(node.args[0], params), _param(node.args[1], params)
                if o and n:
                    old, new = o, n
            if _opens_for_writing(node) and node.args:
                path = _param(node.args[0], params) or path
            elif _is_file_write_attr(node.func):
                value = node.func.value  # type: ignore[attr-defined]
                path = _param(value, params) or path
        if path or (old and new):
            specs[fd.name] = _Helper(params, path, old, new)
    return specs


def _helper_args(node: ast.Call, spec: _Helper) -> dict[str, ast.expr]:
    bound = dict(zip(spec.params, node.args))
    bound.update({kw.arg: kw.value for kw in node.keywords if kw.arg})
    return bound


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
        added = deleted = 0
        if spec.old and spec.new:
            old = _resolve_str(bound.get(spec.old, ast.Constant(None)), consts)
            new = _resolve_str(bound.get(spec.new, ast.Constant(None)), consts)
            if old is not None and new is not None:
                added, deleted = count_lines(new), count_lines(old)
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
        return (count_lines(literal) if literal is not None else 0), 0, path
    return 0, 0, None


@functools.lru_cache(maxsize=512)
def _python_scan(src: str) -> tuple[int, int, tuple[str, ...]]:
    """(added, deleted, written paths) enumerable from a python
    script's own source.

    Module-level statements are walked IN ORDER with the bindings in
    force: `old = ...` above a replace is the literal that replace
    used, and a later `old = ...` rebinds it for the statements below.
    A name bound to anything but a literal, or bound inside a compound
    statement (a loop target, a `with ... as`), is unknown from there
    on. Literal list/tuple loops enumerate candidate paths under ordered
    local bindings, but contribute no churn. Function bodies are counted
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
        return 0, 0, ()

    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    if not _script_writes_a_file(calls):
        return 0, 0, ()

    helpers = _helper_specs(tree)
    consts: dict[str, str] = {}
    added = deleted = 0
    paths: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        a, d, stmt_paths = _statement_effects(stmt, consts, helpers)
        added += a
        deleted += d
        paths.extend(p for p in stmt_paths if p not in paths)
    return added, deleted, tuple(paths)


def _statement_effects(stmt: ast.stmt, consts: dict[str, str],
                       helpers: dict[str, _Helper]
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
            a, d, path = _call_effects(node, view, helpers)
            added += a
            deleted += d
            if path and path not in paths:
                paths.append(path)
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
    added, deleted, _ = _python_scan(src)
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


def bash_churn(command: str) -> tuple[int, int]:
    """(lines_added, lines_deleted) enumerable from one Bash call."""
    if not command or len(command) > MAX_COMMAND_CHARS:
        return 0, 0

    added = deleted = 0
    pairs, outside = _split_heredocs(command)
    for context, body in pairs:
        if _PATCH.search(context):
            a, d = _diff_churn(body)
        elif _PYTHON_STDIN.search(context):
            a, d = _python_churn(body)
        elif _writes_to_a_file(context):
            a, d = (count_lines(body + "\n") if body else 0), 0
        else:
            a, d = 0, 0
        added += a
        deleted += d

    for src in _dash_c_sources(outside):
        a, d = _python_churn(src)
        added += a
        deleted += d
    added += sum(count_lines(payload) for payload in shell_payloads(outside))
    return added, deleted


def _python_sources(command: str) -> list[str]:
    """Every python body in the command: stdin heredocs and `-c` args."""
    pairs, outside = _split_heredocs(command)
    out = [body for context, body in pairs if _PYTHON_STDIN.search(context)]
    out.extend(_dash_c_sources(outside))
    return out


def python_write_paths(command: str) -> list[str]:
    """Paths the command's python bodies open for writing, as written
    in the text — relative ones are the interpreter's cwd's business,
    so the caller resolves them."""
    if not command or len(command) > MAX_COMMAND_CHARS:
        return []
    paths: list[str] = []
    for src in _python_sources(command):
        for path in _python_scan(src)[2]:
            if path not in paths:
                paths.append(path)
    return paths


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
