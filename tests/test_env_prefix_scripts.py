"""Checks ordinary JavaScript (`process.env.X`/`process.env['X']`, optional
chaining and parenthesized access) and shell (`$X`, `${X}`) reads. Deliberate
evasions are out of scope: process/environment aliases, destructuring `env`
from `process`, `node:process` imports, reflection (`Reflect.get`, non-literal
`getattr`), indirect reads (`printenv "$var"`/indirect expansion), `eval`, and
generated code.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PLATFORM_PREFIXES = {
    "GITHUB_": "GitHub Actions runner metadata and output files.",
    "RUNNER_": "GitHub Actions runner-owned paths and metadata.",
    "PG": "libpq connection controls used by contributor database tooling.",
    "PYTHON": "Interpreter-owned runtime controls.",
}
SCRIPT_PLATFORM_NAMES = {
    "PATH": "Executable search path owned by the shell and operating system.",
    "HOME": "User home directory owned by the operating system.",
    "CI": "CI runner indicator consumed by developer tooling.",
    "IFS": "Shell-owned input field separator.",
    "PWD": "Shell-owned current working directory.",
    "SKIP_SECRECY_CHECK": "Documented contributor override for the Git pre-push hook.",
}
SCRIPT_DYNAMIC_SITES: dict[tuple[str, int], str] = {}


def is_script_module(path: str, source: str) -> bool:
    return Path(path).suffix in {".js", ".mjs", ".cjs", ".jsx", ".sh", ".bash"} or (
        source.startswith("#!") and re.search(r"\b(?:bash|sh)\b", source.splitlines()[0]) is not None)


_JS_TOKEN = re.compile(
    r'\s+|//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'
    r"'(?:\\.|[^'\\])*'|[A-Za-z_$][\w$]*|\.\.\.|\?\.|.", re.DOTALL,
)


def _javascript_code(source: str, start: int = 0, interpolation: bool = False
                     ) -> tuple[list[tuple[str, int]], int]:
    """Tokenize code, retaining quoted keys and template interpolation code."""
    tokens = []
    depth = 0
    pos = start
    while pos < len(source):
        match = _JS_TOKEN.match(source, pos)
        assert match is not None
        text = match.group()
        line = source.count('\n', 0, pos) + 1
        pos = match.end()
        if text.isspace() or text.startswith(('//', '/*')):
            continue
        if text == "`":
            embedded, pos = _javascript_template(source, pos)
            tokens.extend(embedded)
            continue
        if text == "}" and interpolation and depth == 0:
            return tokens, pos
        if text == "{":
            depth += 1
        elif text == "}":
            depth -= 1
        tokens.append((text, line))
    return tokens, pos


def _javascript_template(source: str, start: int) -> tuple[list[tuple[str, int]], int]:
    tokens = []
    pos = start
    while pos < len(source):
        if source[pos] == "\\":
            pos += 2
        elif source[pos] == "`":
            return tokens, pos + 1
        elif source.startswith("${", pos):
            embedded, pos = _javascript_code(source, pos + 2, interpolation=True)
            tokens.extend(embedded)
        else:
            pos += 1
    return tokens, pos


def _group_pairs(tokens: list[tuple[str, int]]) -> dict[int, int]:
    stack: list[tuple[str, int]] = []
    pairs = {}
    for index, (text, _) in enumerate(tokens):
        if text in {"(", "[", "{"}:
            stack.append((text, index))
        elif stack and text == {"(": ")", "[": "]", "{": "}"}[stack[-1][0]]:
            _, opening = stack.pop()
            pairs[opening] = index
            pairs[index] = opening
    return pairs


def _literal_key(text: str) -> str | None:
    if text.startswith(("'", '"')):
        # Escaped spellings are decoded so an escaped unprefixed name stays visible.
        return re.sub(r'\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|(.))',
                      lambda m: chr(int(m[1] or m[2], 16)) if m[1] or m[2] else m[3],
                      text[1:-1])
    return text if re.fullmatch(r"[A-Za-z_$][\w$]*", text) else None


def _object_keys(tokens: list[tuple[str, int]], opening: int, pairs: dict[int, int],
                 *, allow_spreads: bool = True, spread_sites: set[int] | None = None
                 ) -> list[tuple[int, str | None]]:
    """Read top-level keys, preserving unknown destructuring rest operations."""
    end = pairs[opening]
    index = opening + 1
    keys = []
    while index < end:
        text, line = tokens[index]
        if text == ",":
            index += 1
            continue
        if text == "..." and (not allow_spreads or (spread_sites is not None and index not in spread_sites)):
            keys.append((line, None))
        elif text != "...":
            if text == "[":
                closing = pairs[index]
                key = _literal_key(tokens[index + 1][0]) if closing == index + 2 else None
                # A bare identifier inside [] is an expression, not a literal key.
                if not tokens[index + 1][0].startswith(("'", '"')):
                    key = None
            else:
                key = _literal_key(text)
            keys.append((line, key))
        while index < end and tokens[index][0] != ",":
            index = pairs[index] + 1 if index in pairs and pairs[index] > index else index + 1
    return keys


def _grouped(tokens: list[tuple[str, int]], pairs: dict[int, int], start: int, end: int
             ) -> tuple[int, int]:
    while start > 0 and pairs.get(start - 1) == end + 1:
        before = tokens[start - 2][0] if start > 1 else ""
        call = before in {"}", "]", ")"} or (
            re.fullmatch(r"[\w$]+", before) and before not in {"return", "throw", "yield", "await"})
        if call:
            break  # Function-call arguments are not transparent grouping.
        start, end = start - 1, end + 1
    return start, end


def _member(tokens: list[tuple[str, int]], pairs: dict[int, int], end: int
            ) -> tuple[str | None, int] | None:
    index = end + 1
    if index < len(tokens) and tokens[index][0] in {".", "?."}:
        index += 1
        if index < len(tokens) and tokens[index][0] != "[":
            return _literal_key(tokens[index][0]), index
    if index < len(tokens) and tokens[index][0] == "[":
        closing = pairs.get(index, index)
        text = tokens[index + 1][0] if closing == index + 2 else ""
        return (_literal_key(text) if text.startswith(("'", '"')) else None), closing
    return None


def _environment_refs(tokens: list[tuple[str, int]], pairs: dict[int, int]
                      ) -> tuple[dict[int, tuple[int, int]], list[tuple[int, str | None]]]:
    refs = {}
    unresolved = []
    covered = set()
    for index, (text, _) in enumerate(tokens):
        if index in covered or text not in {"process", "globalThis"}:
            continue
        start, end = _grouped(tokens, pairs, index, index)
        if text == "globalThis":
            member = _member(tokens, pairs, end)
            if member is None or member[0] != "process":
                continue
            start, end = _grouped(tokens, pairs, start, member[1])
        member = _member(tokens, pairs, end)
        if member is not None and member[0] in {"env", None}:
            refs[index] = _grouped(tokens, pairs, start, member[1])
            covered.update(range(start, member[1] + 1))
            if member[0] is None:
                unresolved.append((tokens[index][1], None))
    return refs, unresolved


def _alias_target(tokens: list[tuple[str, int]], pairs: dict[int, int],
                  start: int, end: int) -> str | None:
    if end + 1 < len(tokens) and tokens[end + 1][0] not in {";", ","}:
        return None  # The RHS must be the object itself, not a value read or expression.
    if start >= 4 and tokens[start - 4][0] == "export":
        return None  # Exporting the object is an untracked consumer.
    if (start >= 3 and tokens[start - 1][0] == "="
            and tokens[start - 3][0] in {"const", "let", "var"}
            and _member(tokens, pairs, end) is None):
        return tokens[start - 2][0]
    return None


def _child_env_objects(tokens: list[tuple[str, int]], pairs: dict[int, int]) -> set[int]:
    objects = set()
    bindings: dict[str, set[int]] = {}
    for index, (text, _) in enumerate(tokens):
        if text == "{" and index in pairs and index >= 2 and tokens[index - 1][0] == "=":
            bindings.setdefault(tokens[index - 2][0], set()).add(index)
    for index, (text, _) in enumerate(tokens[:-2]):
        if _literal_key(text) != "env" or tokens[index + 1][0] != ":":
            continue
        enclosing = [opening for opening, closing in pairs.items()
                     if tokens[opening][0] == "(" and opening < index < closing]
        call = max(enclosing) if enclosing else 0
        child_call = call > 0 and tokens[call - 1][0] in {
            "spawn", "spawnSync", "exec", "execSync", "execFile", "execFileSync", "fork"}
        if child_call:
            value = index + 2
            objects.update({value} if tokens[value][0] == "{" and value in pairs
                           else bindings.get(tokens[value][0], set()))
    return objects


def _reference_read(tokens: list[tuple[str, int]], pairs: dict[int, int], start: int,
                    end: int, objects: set[int], spread_sites: set[int]
                    ) -> list[tuple[int, str | None]] | None:
    member = _member(tokens, pairs, end)
    if member is not None:
        return [(tokens[start][1], member[0])]
    if start >= 2 and [text for text, _ in tokens[start - 2:start]] == ["}", "="]:
        return _object_keys(tokens, pairs[start - 2], pairs, allow_spreads=False)
    if start and tokens[start - 1][0] == "...":
        enclosing = [opening for opening, closing in pairs.items()
                     if tokens[opening][0] == "{" and opening < start < closing]
        if enclosing and max(enclosing) in objects:
            return _object_keys(tokens, max(enclosing), pairs, spread_sites=spread_sites)
    return None


def _alias_refs(tokens: list[tuple[str, int]], pairs: dict[int, int],
                refs: dict[int, tuple[int, int]]) -> None:
    aliases: set[str] = set()
    binding_tokens = set()
    while True:  # Monotone finite provenance: no alias/reference can disappear.
        previous = len(refs), len(aliases)
        for start, end in tuple(refs.values()):
            target = _alias_target(tokens, pairs, start, end)
            if target is not None:
                aliases.add(target)
                binding_tokens.add(start - 2)
        for index, (text, _) in enumerate(tokens):
            if (text in aliases and index not in binding_tokens
                    and (index == 0 or tokens[index - 1][0] not in {".", "?."})):
                refs.setdefault(index, _grouped(tokens, pairs, index, index))
        if previous == (len(refs), len(aliases)):
            break


def _javascript_reads(source: str) -> list[tuple[int, str | None]]:
    tokens, _ = _javascript_code(source)
    pairs = _group_pairs(tokens)
    objects = _child_env_objects(tokens, pairs)
    refs, reads = _environment_refs(tokens, pairs)
    _alias_refs(tokens, pairs, refs)
    spread_sites = {start - 1 for start, end in refs.values()
                    if start and tokens[start - 1][0] == "..." and _member(tokens, pairs, end) is None}
    reads.extend(read for opening in objects
                 for read in _object_keys(tokens, opening, pairs, spread_sites=spread_sites))
    successful = set()
    forwarding = []
    for index, (start, end) in refs.items():
        target = _alias_target(tokens, pairs, start, end)
        if target is not None:
            forwarding.append((index, target))
            continue
        observed = _reference_read(tokens, pairs, start, end, objects, spread_sites)
        reads.extend(observed if observed is not None else [(tokens[index][1], None)])
        if observed is not None and all(name is not None for _, name in observed):
            successful.add(tokens[index][0])
    while True:
        previous = len(successful)
        successful.update(tokens[index][0] for index, target in forwarding if target in successful)
        if previous == len(successful):
            break
    reads.extend((tokens[index][1], None) for index, target in forwarding if target not in successful)
    return reads


def _shell_code(source: str, *, bindings: bool = False) -> str:
    """Mask comments, single-quoted literals and escaped dollars; keep offsets."""
    chars = list(source)
    double = False
    quotes: list[bool] = []
    pos = 0
    while pos < len(source):
        char = source[pos]
        if char == "\\":
            chars[pos] = " "
            if pos + 1 < len(source) and source[pos + 1] != '\n':
                chars[pos + 1] = " "
            pos += 2
            continue
        if source.startswith("$(", pos):
            quotes.append(double)
            double = False
            pos += 2
            continue
        if char == ")" and quotes:
            double = quotes.pop()
        if char == '"':
            double = not double
            chars[pos] = " "
        elif char == "'" and not double:
            end = source.find("'", pos + 1)
            end = len(source) if end < 0 else end + 1
            for index in range(pos, end):
                if chars[index] != '\n':
                    chars[index] = " "
            pos = end
            continue
        elif char == "#" and not double and (pos == 0 or source[pos - 1].isspace()):
            end = source.find('\n', pos)
            end = len(source) if end < 0 else end
            chars[pos:end] = [" "] * (end - pos)
            pos = end
            continue
        elif bindings and double and char != '\n':
            chars[pos] = " "
        pos += 1
    return ''.join(chars)


def _shell_reads(source: str) -> list[tuple[int, str | None]]:
    code = _shell_code(source)
    binding_code = _shell_code(source, bindings=True)
    # Binding sites are lexical locals, not process-env reads. An assignment
    # takes effect after its RHS, so X="$X" still observes external X.
    bindings = []
    assignment = re.compile(
        r'(?:^|[;\n])\s*(?:(?:export|local|declare)(?:\s+-\w+)*\s+)?([A-Za-z_]\w*)\+?=')
    for match in assignment.finditer(binding_code):
        end = re.search(r'[;\n]', binding_code[match.end():])
        position = match.end() + end.start() if end else len(binding_code)
        bindings.append((position, match[1]))
    for match in re.finditer(r'\bfor\s+([A-Za-z_]\w*)\s+in\b', binding_code):
        bindings.append((match.end(), match[1]))
    for match in re.finditer(r'\b(?:read|mapfile)\s+(?:-[A-Za-z]+\s+)*([A-Za-z_]\w*)', binding_code):
        bindings.append((match.end(), match[1]))
    reads = []
    for match in re.finditer(r'\$(?:\{[#!]?)?([A-Za-z_]\w*)', code):
        if not any(position <= match.start() and bound == match[1] for position, bound in bindings):
            reads.append((source.count('\n', 0, match.start()) + 1, match[1]))
    for match in re.finditer(r'\$\{!([A-Za-z_]\w*)([^}]*)\}', code):
        local_array = match[2] in {"[@]", "[*]"} and any(
            position <= match.start() and bound == match[1] for position, bound in bindings)
        if not local_array:
            reads.append((source.count('\n', 0, match.start()) + 1, None))
    commands = re.compile(
        r'(?:^|[;\n|&(])\s*(?:(?:then|do|if|while|command|builtin|exec)\s+)*(eval|printenv)\b')
    for match in commands.finditer(binding_code):
        line = source.count('\n', 0, match.start(1)) + 1
        end = re.search(r'[;\n|&)]', binding_code[match.end():])
        try:
            arguments = shlex.split(
                source[match.end():match.end() + end.start() if end else len(source)], comments=True)
        except ValueError:
            arguments = []
        if match[1] == "eval" or not arguments:
            reads.append((line, None))
        else:
            names = [arg for arg in arguments if not arg.startswith("-")]
            reads.extend((line, name if re.fullmatch(r'[A-Za-z_]\w*', name) else None)
                         for name in names)
            if not names:
                reads.append((line, None))
    return reads


def scan_script_reads(source: str, path: str) -> list[tuple[int, str | None]]:
    """Return recognized external env names and unresolved reads with source lines."""
    reads = (_javascript_reads(source) if Path(path).suffix in {".js", ".mjs", ".cjs", ".jsx"}
             else _shell_reads(source))
    return sorted(set(reads), key=lambda item: (item[0], item[1] or ""))


def script_violations(source: str, path: str) -> list[str]:
    failures = []
    for line, name in scan_script_reads(source, path):
        if name is None:
            if (path, line) not in SCRIPT_DYNAMIC_SITES:
                failures.append(f"{path}:{line}: unresolved env name")
        elif not (name.startswith("CLAUDIT_") or name in SCRIPT_PLATFORM_NAMES
                  or any(name.startswith(prefix) for prefix in SCRIPT_PLATFORM_PREFIXES)):
            failures.append(f"{path}:{line}: unprefixed env name {name}")
    return failures


def _assert_live_controls(source: str, path: str, line: int = 1) -> None:
    assert (line, "UNPREFIXED_X") in scan_script_reads(source, path)
    assert script_violations(source, path)
    good = source.replace("UNPREFIXED_X", "CLAUDIT_X")
    assert (line, "CLAUDIT_X") in scan_script_reads(good, path)
    assert not script_violations(good, path)


@pytest.mark.parametrize("read", [
    'process.env.{name}',
    'process.env["{name}"]',
    "process.env['{name}']",
    'process /* comment */ . env . {name}',
    'export const {{ {name} }} = (process.env);',
    'process?.env.{name}',
    'const {{ {name}: renamed = "default" }} = (( process.env ));',
    '((process.env)).{name}',
    'process["env"].{name}',
    "process['env']['{name}']",
    '(process /* comment */)?.["env"].{name}',
    '(globalThis.process).env.{name}',
    '((globalThis)).process /* comment */ .env.{name}',
    'const e = (process.env); e.{name};',
    'const value = process.env.{name}; show(value);',
    'process.env?.{name}',
    'process.env?.["{name}"]',
    'const {{ {name}: renamed }} = process.env;',
    'const {{ "{name}": renamed }} = process.env;',
    'const {{ {name} = "default" }} = process.env;',
    '({{ {name}: renamed }} = process.env);',
    'spawn("child", [], {{env: {{...process.env, {name}: "1"}}}});',
    'spawn("child", [], {{env: {{"{name}": "1"}}}});',
    'const childEnv = {{...process.env, {name}: "1"}}; spawn("child", [], {{env: childEnv}});',
    'const childEnv = {{{name}: "1"}}; spawn("child", [], {{env: childEnv}});',
    'console.log(`setting: ${{process.env.{name}}}`);',
])
def test_javascript_scanner_live_controls(read: str) -> None:
    _assert_live_controls(read.format(name="UNPREFIXED_X"), "synthetic.mjs")


def test_javascript_scanner_preserves_lines_and_ignores_literal_text() -> None:
    source = '\n'.join([
        '// process.env.IGNORED_COMMENT',
        '/* process.env.IGNORED_BLOCK */',
        'const text = "process.env.IGNORED_STRING";',
        'const template = `process.env.IGNORED_TEMPLATE`;',
        'spawn("node", [], {env: {...process.env, CLAUDIT_CHILD: "1"}});',
        'const copy = "process.env";',
        'process.env.CLAUDIT_X;',
        'const {CLAUDIT_Y, BAD_SECOND: renamed = call(1, 2)} = process.env;',
    ])
    assert scan_script_reads(source, "synthetic.mjs") == [
        (5, "CLAUDIT_CHILD"), (7, "CLAUDIT_X"), (8, "BAD_SECOND"), (8, "CLAUDIT_Y"),
    ]
    assert script_violations(source, "synthetic.mjs") == [
        "synthetic.mjs:8: unprefixed env name BAD_SECOND",
    ]


@pytest.mark.parametrize("path, source", [
    *[("synthetic.mjs", "\n" + read) for read in [
        'process.env[key]',
        'process.env["CLAUDIT_" + suffix]',
        'process.env?.[key]',
        'const { [key]: value } = process.env;',
        'spawn("child", [], {env: {[key]: "1"}});',
    ]],
    *[("synthetic.mjs", read) for read in [
        'process.env;', '(( process.env ));', 'process?.env;',
        'process["env"];', '(globalThis.process).env;',
        'const e = process.env;', 'inspect(process.env);',
        'const e = process.env; inspect(e);',
        'const e = process.env; e[key];',
        'const child = {env: process.env};', 'const copy = {...process.env};',
        'Object.keys(process.env);',
        'const {CLAUDIT_X, ...rest} = process.env; rest.CLAUDIT_Y;',
        'process[key].CLAUDIT_X;',
        'export const e = process.env; e.CLAUDIT_X;',
        'const stored = {env: {...process.env, CLAUDIT_X: "1"}};',
        'spawn("child", [], {env: {...process.env, ...unknownOverrides}});',
    ]],
    *[("synthetic.sh", read) for read in [
        'name=CLAUDIT_X; echo "${!name}"',
        'eval "echo $CLAUDIT_X"',
        'name=CLAUDIT_X; printenv "$name"',
    ]],
])
def test_unresolved_reads_need_exact_site_exemption(path: str, source: str, monkeypatch) -> None:
    line = 2 if source.startswith("\n") else 1
    assert (line, None) in scan_script_reads(source, path)
    assert script_violations(source, path)
    if line == 2:
        assert script_violations(source, path) == [f"{path}:{line}: unresolved env name"]
    monkeypatch.setitem(SCRIPT_DYNAMIC_SITES, (path, line), "Synthetic unresolved-read control.")
    assert not script_violations(source, path)
    assert script_violations("\n" + source, path), "an exemption covered another source line"
    assert script_violations(source, "other" + Path(path).suffix), "an exemption covered another file"
    control = 'process.env.CLAUDIT_X;' if path.endswith('.mjs') else 'echo "$CLAUDIT_X"'
    assert scan_script_reads(control, path) == [(1, "CLAUDIT_X")]
    assert not script_violations(control, path)


def test_literal_printenv_names_are_checked() -> None:
    assert script_violations('printenv UNPREFIXED_X', "synthetic.sh")
    assert scan_script_reads('printenv CLAUDIT_X', "synthetic.sh") == [(1, "CLAUDIT_X")]
    assert not script_violations('printenv CLAUDIT_X', "synthetic.sh")


@pytest.mark.parametrize("read", [
    'echo ${name}',
    'echo "${name}"',
    'echo ${{{name}}}',
    'echo "${{{name}:-default}}"',
    'echo "$(printf %s "${name}")"',
    'local_value=${name}',
    'export {name}="${{{name}:-default}}"',
])
def test_shell_scanner_live_controls(read: str) -> None:
    _assert_live_controls(read.format(name="UNPREFIXED_X"), "synthetic.sh")


def test_shell_scanner_distinguishes_locals_from_external_configuration() -> None:
    source = '\n'.join([
        '#!/usr/bin/env bash',
        '# $IGNORED_COMMENT',
        "echo '$IGNORED_SINGLE_QUOTE'",
        r'echo \$IGNORED_ESCAPE',
        'value="$CLAUDIT_X"',
        'echo "$value"',
        'declare -a values=()',
        'for item in "${values[@]}"; do echo "$item"; done',
        'while IFS= read -r line; do echo "$line"; done',
        'mapfile -t files < <(git ls-files)',
        'echo "${files[@]}"',
        'echo "$LATE_BINDING"',
        'LATE_BINDING=local',
        'echo "$LATE_BINDING"',
    ])
    assert scan_script_reads(source, "synthetic.sh") == [
        (5, "CLAUDIT_X"), (12, "LATE_BINDING"),
    ]
    assert script_violations(source, "synthetic.sh") == [
        "synthetic.sh:12: unprefixed env name LATE_BINDING",
    ]


@pytest.mark.parametrize("message", ['"UNPREFIXED_X=1"', 'UNPREFIXED_X=1'])
def test_shell_messages_cannot_hide_later_environment_reads(message: str) -> None:
    source = f'echo {message}\necho "$UNPREFIXED_X"'
    assert scan_script_reads(source, "synthetic.sh") == [(2, "UNPREFIXED_X")]
    assert script_violations(source, "synthetic.sh")
    good = source.replace("UNPREFIXED_X", "CLAUDIT_X")
    assert scan_script_reads(good, "synthetic.sh") == [(2, "CLAUDIT_X")]
    assert not script_violations(good, "synthetic.sh")


@pytest.mark.parametrize("path, source", [
    ("backend/settings.js", "process.env.UNPREFIXED_X"),
    ("scripts/settings.mjs", "process.env.UNPREFIXED_X"),
    ("scripts/settings.cjs", "process.env.UNPREFIXED_X"),
    ("scripts/settings.sh", "echo $UNPREFIXED_X"),
    ("scripts/pre-push", "#!/usr/bin/env bash\necho $UNPREFIXED_X"),
])
def test_script_module_selection_and_scan_have_live_controls(path: str, source: str) -> None:
    assert is_script_module(path, source)
    _assert_live_controls(source, path, line=2 if source.startswith("#!") else 1)
    assert is_script_module(path, source.replace("UNPREFIXED_X", "CLAUDIT_X"))
    assert not is_script_module("scripts/data.json", '{"env": "UNPREFIXED_X"}')


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_panel_seed_resolves_only_the_prefixed_configuration() -> None:
    script = 'await import("./scripts/ci/panel_interactions.mjs"); console.log("imported");'
    base_env = {key: value for key, value in os.environ.items()
                if key not in {"PANEL_INTERACTIONS_SEED", "CLAUDIT_PANEL_INTERACTIONS_SEED"}}
    for name, code in [("PANEL_INTERACTIONS_SEED", 0), ("CLAUDIT_PANEL_INTERACTIONS_SEED", 2)]:
        result = subprocess.run(["node", "--input-type=module"], input=script, cwd=ROOT,
                                env={**base_env, name: "invalid-review-seed"}, capture_output=True,
                                text=True, timeout=30, check=False)
        assert result.returncode == code, result.stderr
        if code:
            assert "unknown CLAUDIT_PANEL_INTERACTIONS_SEED" in result.stderr
            assert "invalid-review-seed" in result.stderr
        else:
            assert result.stdout.strip() == "imported"


@pytest.mark.parametrize("name", [
    "HOME", "PATH", "GITHUB_OUTPUT", "RUNNER_TEMP", "PGHOST", "CI", "SKIP_SECRECY_CHECK",
])
def test_platform_and_documented_tool_overrides_remain_allowed(name: str) -> None:
    for source, path in [(f'process.env.{name}', "synthetic.mjs"),
                         (f'echo "${{{name}}}"', "scripts/pre-push")]:
        assert scan_script_reads(source, path) == [(1, name)]
        assert not script_violations(source, path)
        _assert_live_controls(source.replace(name, "UNPREFIXED_X"), path)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_panel_widths_resolve_only_the_prefixed_configuration() -> None:
    """An old-only value must be ignored; the new spelling must affect widths."""
    script = ('import {WIDTHS} from "./scripts/ci/panel_server.mjs";'
              'console.log(JSON.stringify(WIDTHS));')
    base_env = {key: value for key, value in os.environ.items()
                if key not in {"PANEL_LAYOUT_WIDTHS", "CLAUDIT_PANEL_LAYOUT_WIDTHS"}}
    for supplied, expected in [
        ({"PANEL_LAYOUT_WIDTHS": "777"}, [1440, 1024, 375, 320]),
        ({"CLAUDIT_PANEL_LAYOUT_WIDTHS": "777,333,0,bad"}, [777, 333]),
        ({"PANEL_LAYOUT_WIDTHS": "888", "CLAUDIT_PANEL_LAYOUT_WIDTHS": "777"}, [777]),
    ]:
        result = subprocess.run(["node", "--input-type=module"], input=script, cwd=ROOT,
                                env={**base_env, **supplied}, capture_output=True,
                                text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == expected
