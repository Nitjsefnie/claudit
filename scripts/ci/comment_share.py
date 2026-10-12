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
from yaml.tokens import AliasToken, AnchorToken, KeyToken, ScalarToken, TagToken

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
    word = re.sub(r"\$'((?:\\.|[^'])*)'", ansi_delimiter, match[2])
    return match.end(), (shlex.split(word)[0], bool(match[1]))


def ansi_delimiter(match: re.Match[str]) -> str:
    prefix = match.string[:match.start()]
    if (len(prefix) - len(prefix.rstrip('\\'))) % 2:
        return match[0]
    escapes = dict(zip('abefnrtv', '\a\b\x1b\f\n\r\t\v', strict=True))
    escapes['E'] = '\x1b'

    def expand(escape: re.Match[str]) -> str:
        value = escape[1]
        if value[0] in 'xuU':
            return chr(int(value[1:], 16))
        if value[0] in '01234567':
            return chr(int(value, 8))
        if value[0] == 'c' and len(value) == 2:
            return chr(127 if value[1] == '?' else ord(value[1].upper()) & 31)
        return escapes.get(value, value if value in "\\\"'?" else '\\' + value)

    decoded = re.sub(r'\\(x[\da-fA-F]{1,2}|u[\da-fA-F]{1,4}|U[\da-fA-F]{1,8}|[0-7]{1,3}|c.|.)',
                     expand, match[1])
    return shlex.quote(decoded)


@dataclass
class ShellScope:
    close: str = ''
    quote: str = ''
    depth: int = 0
    cases: list[str] = field(default_factory=list)
    pattern_depth: int = 0
    command: bool = True
    inert: bool = False


@dataclass
class ShellLexer:
    scopes: list[ShellScope] = field(default_factory=lambda: [ShellScope()])
    waiting: list[tuple[str, bool]] = field(default_factory=list)
    bodies: list[tuple[str, bool]] = field(default_factory=list)

    def push(self, close: str) -> None:
        scope = self.scopes[-1]
        scope.command = False
        self.scopes.append(ShellScope(close=close, inert=scope.inert or scope.close == '}'))

    def substitution(self, line: str, index: int) -> int | None:
        scope = self.scopes[-1]
        char = line[index]
        if scope.quote in ("'", "$'"):
            return None
        arithmetic = not scope.quote and not (scope.cases and scope.cases[-1] == 'pattern')
        if line[index:index + 3] == '$((' or arithmetic and line[index:index + 2] == '((':
            self.push('))')
            return index + (3 if line[index] == '$' else 2)
        if line[index:index + 2] in ('${', '$('):
            close = '}' if line[index + 1] == '{' else ')'
            self.push(close)
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
        if scope.cases and scope.cases[-1] == 'pattern':
            self.pattern(line[index], line[index - 1:index])
            return index + 1 if char in '()' else None
        if scope.close == '))' and not scope.depth and line[index:index + 2] == '))':
            self.scopes.pop()
            return index + 2
        if char == '(':
            if scope.close in (')', '))'):
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

    def pattern(self, char: str, previous: str) -> None:
        scope = self.scopes[-1]
        if char == '(' and (scope.pattern_depth or previous in '@*+?!' and previous):
            scope.pattern_depth += 1
        elif char == ')':
            if scope.pattern_depth:
                scope.pattern_depth -= 1
            else:
                scope.cases[-1], scope.command = 'body', True

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
        if index and not (line[index - 1].isspace() or line[index - 1] in ';&|(){}'):
            return None
        match = re.match(r'[a-zA-Z_]\w*', line[index:])
        if not match:
            return None
        scope, word = self.scopes[-1], match[0]
        end = index + match.end()
        complete = end == len(line) or line[end].isspace() or line[end] in ';&|(){}'
        closing_case = bool(scope.cases) and not scope.pattern_depth and (scope.command or scope.cases[-1] == 'pattern')
        if complete and word == 'in' and scope.cases and scope.cases[-1] == 'subject':
            scope.cases[-1] = 'pattern'
        elif complete and word == 'esac' and line[end:end + 1] not in (')', '|') and closing_case:
            scope.cases.pop()
        elif complete and word == 'case' and scope.command and (not scope.cases or scope.cases[-1] == 'body'):
            scope.cases.append('subject')
        scope.command = complete and word in ('if', 'then', 'elif', 'else', 'do', 'while', 'until')
        return end

    def scope_step(self, line: str, index: int) -> int | None:
        scope = self.scopes[-1]
        char = line[index]
        if char == '\\' and scope.quote != "'":
            return index + 2
        if not scope.quote and line[index:index + 2] == "$'":
            scope.quote, scope.command = "$'", False
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

    def command_step(self, line: str, index: int) -> int | None:
        scope = self.scopes[-1]
        moved = self.word(line, index)
        if moved is not None:
            return moved
        if scope.cases and line[index:index + 2] in (';;', ';&'):
            scope.cases[-1], scope.command = 'pattern', True
            return index + (3 if line[index:index + 3] == ';;&' else 2)
        return None

    def step(self, line: str, index: int) -> tuple[int, int]:
        moved = self.scope_step(line, index)
        if moved is None:
            moved = self.command_step(line, index)
        if moved is not None:
            return moved, 0
        scope, char = self.scopes[-1], line[index]
        if char in "\"'":
            scope.quote = char
        elif char == '#' and (index == 0 or line[index - 1].isspace()):
            return len(line), 0 if scope.inert else non_whitespace_bytes(line[index:])
        elif line[index:index + 2] == '<<':
            moved, heredoc = heredoc_at(line, index)
            if heredoc:
                self.waiting.append(heredoc)
            return moved, 0
        if char in ';&|\n{}' or char == '!' and line[index + 1:index + 2].isspace():
            scope.command = True
        elif not char.isspace():
            scope.command = False
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
        if len(self.scopes) != 1 or any(scope.quote or scope.cases for scope in self.scopes):
            raise MeasurementError('unterminated shell scope or quote')
        pending = self.bodies or self.waiting
        if pending:
            raise MeasurementError(f'unterminated heredoc: {pending[0][0]}')


def yaml_block(text: str, token: ScalarToken, key: str, parent: int
               ) -> tuple[int, int, int]:
    newline = re.search(r'\r\n|[\r\n\x85\u2028\u2029]', text[token.start_mark.index:])
    if newline is None:
        return len(text), len(text), 0
    start = token.start_mark.index + newline.end()
    body = []
    for line in text[start:].splitlines(keepends=True):
        if line.strip() and len(line) - len(line.lstrip(' ')) <= parent:
            break
        body.append(line)
    end = start + sum(len(line) for line in body)
    count = 0
    if key == 'run':
        header = text[token.start_mark.index:start].split('#', 1)[0]
        digit = next((int(char) for char in header if char.isdigit()), None)
        indent = parent + digit if digit is not None else next(
            (len(line) - len(line.lstrip(' ')) for line in body if line.strip()), parent + 1)
        script = ''.join(line[min(indent, len(line) - len(line.lstrip(' '))):]
                         for line in body)
        count = quoted_comments(script, shell=True)
    return start, end, count


def yaml_comments(text: str) -> int:
    try:
        tokens = list(yaml.scan(text))
    except yaml.YAMLError as error:
        raise MeasurementError(f'YAML tokenization failed: {error}') from error
    spans = []
    anchors: dict[str, str] = {}
    key = anchor = ''
    parent = count = 0
    reading_key = False
    for token in tokens:
        if isinstance(token, KeyToken):
            reading_key, key, parent = True, '', token.start_mark.column
        elif isinstance(token, AnchorToken):
            anchor = token.value
        elif isinstance(token, AliasToken):
            key = anchors.get(token.value, '') if reading_key else ''
            reading_key, anchor = False, ''
        elif isinstance(token, ScalarToken):
            if anchor:
                anchors[anchor] = token.value
                anchor = ''
            if token.style in ("'", '"'):
                spans.append((token.start_mark.index, token.end_mark.index))
            if reading_key:
                key, reading_key = token.value, False
            else:
                if token.style in ('|', '>'):
                    start, end, added = yaml_block(text, token, key, parent)
                    spans.append((start, end))
                    count += added
                key = ''
        elif not isinstance(token, TagToken):
            anchor = ''
    return count + yaml_hashes(text, spans)


def yaml_hashes(text: str, spans: list[tuple[int, int]]) -> int:
    count = 0
    comment_end = 0
    for match in re.finditer(r'(?<!\S)#', text):
        start = match.start()
        if start < comment_end or any(begin <= start < end for begin, end in spans):
            continue
        newline = re.search(r'[\r\n\x85\u2028\u2029]', text[start:])
        comment_end = start + newline.start() if newline else len(text)
        count += non_whitespace_bytes(text[start:comment_end])
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
