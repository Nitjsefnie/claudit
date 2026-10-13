"""Measure UTF-8 comment bytes on tracked source files.

Shell subset: words/operators, ordinary and ANSI-C string quotes, escapes,
command/process/backtick/parameter scopes, grouping, quote-free arithmetic,
here-strings and continued commands with ordered literal-word heredocs.
Delimiters must be nonempty words, unquoted, wholly single/double quoted, or
backslash-prefixed; Whitespace/escape syntax in delimiters, ANSI-C or mixed
quoting refuses. Single/double quotes anywhere in arithmetic and unquoted case
words inside command/process/backtick substitutions refuse.
Top-level case words are opaque; this is lexical measurement, not execution.
Words split only at space, tab, newline and ;&|()<> (`<(`/`>(` open a process
substitution inside the word); braces are word text.
`=~` operands and extglob patterns (a word-glued `(` other than `name()` or
`name=(`) refuse; a compound assignment's `)` continues its word.
"""
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

import yaml
from yaml.tokens import KeyToken, ScalarToken, ValueToken

ROOT = Path(__file__).resolve().parents[2]
SUFFIXES = {'.py': 'python', '.js': 'javascript', '.jsx': 'javascript',
            '.mjs': 'javascript', '.cjs': 'javascript', '.yaml': 'yaml',
            '.yml': 'yaml', '.sh': 'shell'}
SHELLS = {'sh', 'bash', 'dash', 'ash', 'ksh', 'mksh', 'zsh'}
BOUNDARY = ' \t\n;&|()<>'
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
        raise MeasurementError('unsupported heredoc delimiter')
    word = match[2]
    if word.startswith(("'", '"')):
        if len(word) < 2 or word[-1] != word[0] or word[0] in word[1:-1]:
            raise MeasurementError('mixed-quoted heredoc delimiter refuses')
        literal = word[1:-1]
        if word[0] == '"':
            literal = re.sub(r'\\(\r?\n|[^\n])',
                             lambda part: '' if part[1].endswith('\n') else part[0], literal)
    else:
        literal = word[1:] if word.startswith('\\') else word
        if any(char in "\"'" for char in literal):
            raise MeasurementError('ANSI-C or mixed-quoted heredoc delimiter refuses')
    if not literal or re.search(r'[\s<>|;&()\\]', literal):
        raise MeasurementError('heredoc delimiter must be a literal plain word')
    if match.end() < len(line) and not (line[match.end()].isspace() or line[match.end()] in '<>|;&()'):
        raise MeasurementError('unsupported heredoc delimiter suffix')
    return match.end(), (literal, bool(match[1]))


@dataclass
class ShellScope:
    close: str = ''
    quote: str = ''
    depth: int = 0
    command_substitution: bool = False
    inert: bool = False
    word_start: bool = True


@dataclass
class ShellLexer:
    scopes: list[ShellScope] = field(default_factory=lambda: [ShellScope()])
    waiting: list[tuple[str, bool]] = field(default_factory=list)
    bodies: list[tuple[str, bool]] = field(default_factory=list)

    def push(self, close: str) -> None:
        scope = self.scopes[-1]
        self.scopes.append(ShellScope(close=close, inert=scope.inert or scope.close == '}',
                                      command_substitution=close == '`'))

    def substitution(self, line: str, index: int) -> int | None:
        scope = self.scopes[-1]
        char = line[index]
        if scope.quote in ("'", "$'"):
            return None
        arithmetic = not scope.quote
        if line[index:index + 3] == '$((' or arithmetic and line[index:index + 2] == '((':
            self.push('))')
            return index + (3 if line[index] == '$' else 2)
        opener = line[index:index + 2]
        if opener in ('${', '$(') or arithmetic and scope.close != '))' and opener in ('<(', '>('):
            close = '}' if opener == '${' else ')'
            self.push(close)
            self.scopes[-1].command_substitution = close == ')'
            return index + 2
        if char == '`':
            if scope.close == '`':
                self.scopes.pop()
            else:
                self.push('`')
            return index + 1
        if scope.quote:
            return None
        return self.parenthesis(line, index)

    def parenthesis(self, line: str, index: int) -> int | None:
        scope, char = self.scopes[-1], line[index]
        if scope.close == '))' and not scope.depth and line[index:index + 2] == '))':
            self.scopes.pop()
            return index + 2
        if char == '(':
            glued, prev = not scope.word_start, line[index - 1:index]
            if scope.close != '))' and glued and prev != '=' and (
                    prev in tuple('@!+*?') or line[index + 1:index + 2] != ')'):
                raise MeasurementError('extglob pattern refuses')
            if scope.close == '))' or scope.close == ')' and prev != '=':
                scope.depth += 1
            else:
                self.push(')')
        elif char == ')':
            if scope.depth:
                scope.depth -= 1
            elif scope.close == ')':
                self.scopes.pop()
            elif scope.close == '))':
                raise MeasurementError('unbalanced arithmetic parentheses')
        else:
            return None
        return index + 1

    def parameter(self, line: str, index: int) -> int:
        scope, char = self.scopes[-1], line[index]
        if scope.quote not in ("'", "$'") and (line[index:index + 2] in ('${', '$(') or char == '`'):
            moved = self.substitution(line, index)
            if moved is not None:
                return moved
        if scope.quote:
            if char == scope.quote[-1]:
                scope.quote = ''
        elif char in "\"'":
            scope.quote = char
        elif char == '}':
            self.scopes.pop()
        return index + 1

    def word(self, line: str, index: int) -> int | None:
        scope = self.scopes[-1]
        if not scope.word_start or scope.quote or scope.close in ('}', '))') or line[index] == '#':
            return None
        match = re.match(r'''(?:\\[^\n]|[^ \t\n\\'"`$;&|()<>])+''', line[index:])
        if not match:
            return None
        end = index + match.end()
        complete = (end == len(line) or line[end] in BOUNDARY
                    or line[end] == '`' and scope.close == '`')
        spelling = re.sub(r'\\(.)', r'\1', match[0])
        if complete and spelling == '=~':
            raise MeasurementError('[[ =~ ]] regex operand refuses')
        if complete:
            self.refuse_case(spelling)
        return end

    def refuse_case(self, spelling: str) -> None:
        if spelling == 'case' and any(frame.command_substitution for frame in self.scopes):
            raise MeasurementError('case word inside command substitution refuses')

    def scope_step(self, line: str, index: int) -> int | None:
        scope = self.scopes[-1]
        char = line[index]
        if char == '\\' and scope.quote != "'":
            return index + 2
        if not scope.quote and line[index:index + 2] == "$'":
            scope.quote = "$'"
            return index + 2
        if scope.close == '}':
            return self.parameter(line, index)
        moved = self.substitution(line, index)
        if moved is not None:
            return moved
        if scope.quote or scope.close == '))':
            if scope.quote and char == scope.quote[-1]:
                scope.quote = ''
            return index + 1
        return None

    def step(self, line: str, index: int) -> tuple[int, int]:
        scope, char = self.scopes[-1], line[index]
        moved = self.word(line, index)
        if moved is None:
            moved = self.scope_step(line, index)
        if moved is not None:
            scope.word_start = char in '()' and not scope.quote and line[index - 1:index + 1] != '=('
            return moved, 0
        if char in "\"'":
            scope.quote = char
        elif char == '#' and scope.word_start:
            match = re.match(r'(?:\\(?:\r?\n|[^\n]|$)|[^\\\n])*', line[index:])
            assert match is not None
            return index + match.end(), int(not scope.inert)
        elif line[index:index + 2] == '<<':
            moved, heredoc = heredoc_at(line, index)
            if heredoc:
                word = re.sub(r'^<<-?[ \t]*', '', line[index:moved])
                if not word.startswith(("'", '"')):
                    self.refuse_case(heredoc[0])
                self.waiting.append(heredoc)
            scope.word_start = False
            return moved, 0
        scope.word_start = char in BOUNDARY
        return index + 1, 0

    def join_prefix(self, text: str, index: int, positions: list[int]
                    ) -> tuple[str, list[int], int]:
        match = re.match(r'''(?:\\(?:\r?\n|[^\n])|[^\\'"\n])*''', text[index:])
        assert match is not None
        prefix = match[0]
        removed = {offset for gap in re.finditer(r'\\\r?\n', prefix)
                   for offset in range(gap.start(), gap.end())}
        kept = [offset for offset in range(len(prefix)) if offset not in removed]
        end = index + len(prefix)
        return (text[:index] + ''.join(prefix[offset] for offset in kept) + text[end:],
                positions[:index] + [positions[index + offset] for offset in kept] + positions[end:],
                index + len(kept))

    def body(self, line: str) -> None:
        if any(frame.close == '))' for frame in self.scopes) and any(char in line for char in "\"'"):
            raise MeasurementError('quotes inside arithmetic refuse')
        delimiter, strip_tabs = self.bodies[0]
        candidate = line.rstrip('\r\n')
        if strip_tabs:
            candidate = candidate.lstrip('\t')
        if candidate == delimiter:
            self.bodies.pop(0)

    def count(self, text: str) -> int:
        original, positions = text, list(range(len(text) + 1))
        index = count = 0
        joined_until = 0
        while index < len(text):
            if self.bodies:
                end = text.find('\n', index) + 1 or len(text)
                self.body(text[index:end])
                index = end
                continue
            if index >= joined_until and self.scopes[-1].quote != "'":
                text, positions, joined_until = self.join_prefix(text, index, positions)
                if index == len(text):
                    break
            arithmetic = any(frame.close == '))' for frame in self.scopes)
            moved, comment = self.step(text, index)
            raw = original[positions[index]:positions[min(moved, len(text))]]
            if arithmetic and any(char in raw for char in "\"'"):
                raise MeasurementError('quotes inside arithmetic refuse')
            if comment:
                count += non_whitespace_bytes(raw)
            scope = self.scopes[-1]
            if text[index] == '\n' and not scope.quote and scope.close != '}':
                self.bodies.extend(self.waiting)
                self.waiting.clear()
            index = moved
        return count

    def finish(self) -> None:
        if len(self.scopes) != 1 or any(scope.quote for scope in self.scopes):
            raise MeasurementError('unterminated shell scope or quote')
        pending = self.bodies or self.waiting
        if pending:
            raise MeasurementError(f'unterminated heredoc: {pending[0][0]}')


def yaml_block(text: str, token: ScalarToken, key_column: int) -> int:
    extent = text[token.start_mark.index:token.end_mark.index]
    newline = re.search(r'\r\n|[\r\n\x85\u2028\u2029]', extent)
    if newline is None:
        return 0
    header = extent[:newline.start()]
    body = extent[newline.end():].splitlines(keepends=True)
    indicator = re.match(r'[|>][+-]?([1-9])', header)
    indent = key_column + int(indicator[1]) if indicator else next(
        (len(line) - len(line.lstrip(' ')) for line in body if line.strip()), key_column + 1)
    script = ''.join(line[min(indent, len(line) - len(line.lstrip(' '))):] for line in body)
    return quoted_comments(script, shell=True)


def yaml_comments(text: str) -> int:
    try:
        tokens = list(yaml.scan(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError as error:
        raise MeasurementError(f'YAML tokenization failed: {error}') from error
    count = end = 0
    for index, token in enumerate(tokens):
        gap = text[end:token.start_mark.index]
        count += sum(non_whitespace_bytes(match[0]) for match in
                     re.finditer(r'#[^\r\n\x85\u2028\u2029]*', gap))
        if isinstance(token, ScalarToken) and token.style in ('|', '>') and index >= 3:
            marker, key, value = tokens[index - 3:index]
            if (isinstance(marker, KeyToken) and isinstance(key, ScalarToken)
                    and key.plain and key.value == 'run' and isinstance(value, ValueToken)):
                count += yaml_block(text, token, marker.start_mark.column)
        end = max(end, token.end_mark.index)
    return count


def quoted_comments(text: str, shell: bool = False) -> int:
    if not shell:
        return yaml_comments(text)
    lexer = ShellLexer()
    count = lexer.count(text)
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
