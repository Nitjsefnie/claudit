from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUFFIXES = {'.py': 'python', '.js': 'javascript', '.jsx': 'javascript',
            '.mjs': 'javascript', '.cjs': 'javascript', '.yaml': 'yaml',
            '.yml': 'yaml', '.sh': 'shell'}
SHELLS = {'sh', 'bash', 'dash', 'ash', 'ksh', 'mksh', 'zsh'}
HEREDOC = re.compile(r'<<(-?)[ \t]*((?:[^\s<>|;&()\\\'"`]|\\.|\'[^\']*\'|"[^"]*")+)')


class MeasurementError(ValueError):
    pass


def non_whitespace_bytes(text: str) -> int:
    """UTF-8 bytes of characters for which str.isspace() is false."""
    return sum(len(char.encode('utf-8')) for char in text
               if not char.isspace())


def language_for(path: Path) -> str | None:
    language = SUFFIXES.get(path.suffix)
    if language or path.suffix:
        return language
    with path.open('rb') as handle:
        first = handle.readline()
    if not first.startswith(b'#!'):
        return None
    words = shlex.split(first[2:].decode('utf-8'))
    if not words:
        return None
    executable = Path(words[0]).name
    if executable == 'env':
        words = [word for word in words[1:]
                 if not word.startswith('-') and '=' not in word]
        executable = Path(words[0]).name if words else ''
    return 'shell' if executable in SHELLS else None


def tracked_sources(root: Path = ROOT) -> dict[str, str]:
    """Return git-tracked relative paths mapped to the four languages."""
    listed = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'],
                            capture_output=True, check=True, timeout=30)
    sources = {}
    for raw in listed.stdout.split(b'\0'):
        if not raw:
            continue
        rel = raw.decode('utf-8')
        try:
            language = language_for(root / rel)
        except (OSError, ValueError) as error:
            raise MeasurementError(f'{rel}: {error}') from error
        if language:
            sources[rel] = language
    return sources


def python_comments(text: str) -> int:
    """Count COMMENT tokens and STRING tokens in bare str expressions.

    Include source prefixes and delimiters, exclude whitespace and syntax
    outside the tokens. Bytes literals and f-strings remain code.
    """
    tree = ast.parse(text)
    lines = text.split('\n')
    spans = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            value = node.value
            spans.append(((value.lineno, value.col_offset),
                          (value.end_lineno, value.end_col_offset)))
    count = 0
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        is_comment = token.type == tokenize.COMMENT
        if token.type == tokenize.STRING:
            row, col = token.start
            start = row, len(lines[row - 1][:col].encode('utf-8'))
            row, col = token.end
            end = row, len(lines[row - 1][:col].encode('utf-8'))
            is_comment = any(begin <= start and end <= finish
                             for begin, finish in spans)
        if is_comment:
            count += non_whitespace_bytes(token.string)
    return count


def heredoc_at(line: str, index: int) -> tuple[int, tuple[str, bool] | None]:
    if line[index:index + 3] == '<<<':
        return index + 3, None
    match = HEREDOC.match(line, index)
    if not match:
        return index + 2, None
    return match.end(), (shlex.split(match[2])[0], bool(match[1]))


@dataclass
class ShellScope:
    close: str = ''
    quote: str = ''
    depth: int = 0


@dataclass
class ShellLexer:
    scopes: list[ShellScope] = field(default_factory=lambda: [ShellScope()])
    waiting: list[tuple[str, bool]] = field(default_factory=list)
    bodies: list[tuple[str, bool]] = field(default_factory=list)

    def substitution(self, line: str, index: int) -> int | None:
        scope = self.scopes[-1]
        char = line[index]
        if scope.quote == "'":
            return None
        if line[index:index + 2] in ('${', '$('):
            close = '}' if line[index + 1] == '{' else ')'
            self.scopes.append(ShellScope(close=close))
            return index + 2
        if char == '`':
            if scope.close == '`':
                self.scopes.pop()
            else:
                self.scopes.append(ShellScope(close='`'))
            return index + 1
        if scope.quote or scope.close != ')':
            return None
        if char == '(':
            scope.depth += 1
        elif char == ')':
            if scope.depth:
                scope.depth -= 1
            else:
                self.scopes.pop()
        else:
            return None
        return index + 1

    def parameter(self, char: str) -> None:
        scope = self.scopes[-1]
        if scope.quote:
            if char == scope.quote:
                scope.quote = ''
        elif char in "\"'":
            scope.quote = char
        elif char == '{':
            scope.depth += 1
        elif char == '}':
            if scope.depth:
                scope.depth -= 1
            else:
                self.scopes.pop()

    def step(self, line: str, index: int) -> tuple[int, int]:
        scope = self.scopes[-1]
        char = line[index]
        if char == '\\' and scope.quote != "'":
            return index + 2, 0
        if scope.close == '}':
            self.parameter(char)
            return index + 1, 0
        moved = self.substitution(line, index)
        if moved is not None:
            return moved, 0
        if scope.quote:
            if char == scope.quote:
                scope.quote = ''
        elif char in "\"'":
            scope.quote = char
        elif char == '#' and (index == 0 or line[index - 1].isspace()):
            return len(line), non_whitespace_bytes(line[index:])
        elif line[index:index + 2] == '<<':
            moved, heredoc = heredoc_at(line, index)
            if heredoc:
                self.waiting.append(heredoc)
            return moved, 0
        return index + 1, 0

    def line(self, line: str) -> int:
        if self.bodies:
            delimiter, strip_tabs = self.bodies[0]
            candidate = line.rstrip('\r\n')
            if strip_tabs:
                candidate = candidate.lstrip('\t')
            if candidate == delimiter:
                self.bodies.pop(0)
            return 0
        index = count = 0
        continued = False
        while index < len(line):
            if (line[index:index + 2] in ('\\\n', '\\\r')
                    and self.scopes[-1].quote != "'"):
                continued = True
            index, added = self.step(line, index)
            count += added
        scope = self.scopes[-1]
        if not continued and not scope.quote and scope.close != '}':
            self.bodies.extend(self.waiting)
            self.waiting.clear()
        return count

    def finish(self) -> None:
        pending = self.bodies or self.waiting
        if pending:
            raise MeasurementError(f'unterminated heredoc: {pending[0][0]}')


def yaml_line(line: str, quote: str) -> tuple[int, str]:
    count = 0
    index = 0
    scalar_start = True
    while index < len(line):
        char = line[index]
        if char == '\\' and quote == '"':
            index += 2
            continue
        if quote:
            if char == quote:
                if quote == "'" and line[index:index + 2] == "''":
                    index += 2
                    continue
                quote = ''
                scalar_start = False
        elif char in "\"'" and scalar_start:
            quote = char
        elif char == '#' and (index == 0 or line[index - 1].isspace()):
            count += non_whitespace_bytes(line[index:])
            break
        elif not char.isspace():
            next_char = line[index + 1:index + 2]
            scalar_start = (char in '[{,' or char == ':'
                            and (not next_char or next_char.isspace()
                                 or next_char in '[]{},')
                            or char in '-?' and scalar_start
                            and next_char.isspace())
        index += 1
    return count, quote


BLOCK_SCALAR = re.compile(
    r'^(?P<prefix> *(?:- +)?)(?:(?P<key>[^:#]+?):[ \t]*)?'
    r'[|>](?P<flags>[1-9][+-]?|[+-][1-9]?)?[ \t]*(?:#.*)?(?:\r?\n)?$')


def yaml_comments(text: str) -> int:
    lines = list(io.StringIO(text))
    count = index = 0
    quote = ''
    while index < len(lines):
        line = lines[index]
        block = BLOCK_SCALAR.match(line) if not quote else None
        added, quote = yaml_line(line, quote)
        count += added
        index += 1
        if not block:
            continue
        parent = len(block['prefix'])
        body = []
        while index < len(lines):
            current = lines[index]
            indent = len(current) - len(current.lstrip(' '))
            if current.strip() and indent <= parent:
                break
            body.append(current)
            index += 1
        if (block['key'] or '').strip().strip("\"'") == 'run':
            flags = block['flags'] or ''
            digit = next((int(char) for char in flags if char.isdigit()), None)
            indent = parent + digit if digit is not None else next(
                (len(value) - len(value.lstrip(' ')) for value in body
                 if value.strip()), parent + 1)
            script = ''.join(value[indent:] if value.strip() else '\n'
                             for value in body)
            count += quoted_comments(script, shell=True)
        quote = ''
    return count


def quoted_comments(text: str, shell: bool = False) -> int:
    if not shell:
        return yaml_comments(text)
    lexer = ShellLexer()
    count = sum(lexer.line(line) for line in io.StringIO(text))
    lexer.finish()
    return count


def javascript_comments(root: Path, paths: list[str]) -> dict[str, int]:
    if not paths:
        return {}
    espree_dir = os.environ.get('CLAUDIT_ESPREE_DIR') or str(ROOT / 'node_modules')
    helper = Path(__file__).with_name('js_comment_ranges.mjs')
    try:
        result = subprocess.run(
            ['node', str(helper), str(Path(espree_dir).resolve())],
            input=json.dumps([str(root / rel) for rel in paths]),
            capture_output=True, text=True, check=True, timeout=60)
    except subprocess.CalledProcessError as error:
        raise MeasurementError(error.stderr.strip()) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise MeasurementError(f'node/espree measurement failed: {error}') from error
    comments = json.loads(result.stdout)
    return {rel: sum(non_whitespace_bytes(comment) for comment in groups)
            for rel, groups in zip(paths, comments, strict=True)}


def measure_files(root: Path = ROOT, sources: dict[str, str] | None = None
                  ) -> dict[str, tuple[int, int]]:
    """Return path -> (comment bytes, non-whitespace bytes), without policy.

    Every count uses original UTF-8 source, including comment delimiters,
    and excludes Unicode whitespace. Empty files have share zero.
    """
    root = root.resolve()
    sources = tracked_sources(root) if sources is None else sources
    js_paths = [rel for rel, language in sources.items() if language == 'javascript']
    js_counts = javascript_comments(root, js_paths)
    measured = {}
    for rel, language in sources.items():
        try:
            text = (root / rel).read_bytes().decode('utf-8')
            if language == 'python':
                comments = python_comments(text)
            elif language in ('yaml', 'shell'):
                comments = quoted_comments(text, shell=language == 'shell')
            elif language == 'javascript':
                comments = js_counts[rel]
            else:
                raise MeasurementError(f'unsupported language: {language}')
            measured[rel] = comments, non_whitespace_bytes(text)
        except (OSError, ValueError, SyntaxError, tokenize.TokenError) as error:
            raise MeasurementError(f'{rel}: {error}') from error
    return measured


def share(counts: tuple[int, int]) -> float:
    comments, total = counts
    return comments / total if total else 0.0


def report(sources: dict[str, str], measured: dict[str, tuple[int, int]]) -> None:
    print('path\tcomment_bytes\tnon_whitespace_bytes\tshare')
    for rel, counts in sorted(measured.items()):
        print(f'{rel}\t{counts[0]}\t{counts[1]}\t{share(counts):.6%}')
    print('language\tfiles\tcomment_bytes\tnon_whitespace_bytes\tshare')
    for language in (*sorted(set(sources.values())), 'overall'):
        values = [counts for rel, counts in measured.items()
                  if language in ('overall', sources[rel])]
        counts = sum(value[0] for value in values), sum(value[1] for value in values)
        print(f'{language}\t{len(values)}\t{counts[0]}\t{counts[1]}\t{share(counts):.6%}')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Measure tracked source comment shares.')
    parser.add_argument('--report', action='store_true', help='print per-file and aggregate counts')
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        sources = tracked_sources(args.root)
        measured = measure_files(args.root, sources)
        report(sources, measured)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'comment measurement failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
