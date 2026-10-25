from __future__ import annotations

import pytest

from tests.test_ci_gate_botdata import classify

BASE = 'a' * 40
HEAD = 'b' * 40
EVENT = {'name': 'push', 'repository': 'o/r', 'before': 'c' * 40, 'sha': HEAD}
PR_EVENT = {'name': 'pull_request', 'repository': 'o/r', 'pull_request': '17', 'sha': HEAD}
BOT_EMAIL = '41898282+github-actions[bot]@users.noreply.github.com'


def _classify(paths, before='', after='', unreadable=False, *, event=None, author=BOT_EMAIL):
    replies = {
        'repos/o/r/pulls/17/files': '\n'.join(paths),
        'repos/o/r/pulls/17': f'{BASE} {HEAD}',
        'repos/o/r/actions/workflows/ci-gate.yml/runs?branch=master&per_page=100':
            f'{BASE} completed success 42',
        'repos/o/r/actions/runs/42/jobs?per_page=100': 'tests success',
        f'repos/o/r/compare/{BASE}...{HEAD}': '\n'.join(paths),
        f'repos/o/r/commits/{HEAD}': author,
    }

    def run(argv):
        url = next(arg for arg in argv if arg.startswith('repos/'))
        if url in replies:
            value = replies[url]
            if value is None:
                raise OSError('cannot read author')
            return value
        if unreadable:
            raise OSError('cannot read blob')
        if f'contents/{paths[0]}?ref={BASE}' in argv[-1]:
            return before
        if f'contents/{paths[0]}?ref={HEAD}' in argv[-1]:
            return after
        raise AssertionError(argv)
    return classify.classify(EVENT if event is None else event, run)[:3]


@pytest.mark.parametrize('path,want', [
    ('examples/x.py', (False, False, False)),
    ('examples/x.pyw', (False, False, False)),
    ('examples/x.mjs', (False, False, False)),
    ('examples/x.ts', (False, False, False)),
    ('.claude/x.mts', (False, False, False)),
    ('.claude/x.es6', (False, False, False)),
    ('examples/x.PY', (False, False, False)),
    ('examples/x.vue', (False, False, False)),
    ('examples/x.raml', (False, False, False)),
    ('examples/a/package.json', (False, False, False)),
    ('examples/a/manifest.json', (False, False, False)),
    ('examples/a/xs-app.json', (False, False, False)),
    ('examples/a/x.view.json', (False, False, False)),
    ('examples/a/codeql-javascript-default.json', (False, False, False)),
    ('examples/requirements.txt', (False, False, False)),
    ('examples/requirements-dev.in', (False, False, False)),
    ('examples/a/tsconfig.build.json', (False, False, False)),
    ('examples/.eslintrc', (False, False, False)),
    ('examples/a/.xsaccess', (False, False, False)),
    ('examples/a/action.yml', (False, False, False)),
    ('examples/claudit.service', (False, False, False)),
    ('examples/a/actions.lock', (False, False, False)),
    ('examples/x.lock', (True, False, False)),
    ('.claude/x.sh', (True, False, False)),
    ('.claude/x.yaml', (False, False, False)),
])
def test_docs_glob_additions_run_full_only_when_a_leg_reads_the_file(path, want):
    assert _classify([path]) == want


def test_extensionless_examples_file_narrows_as_docs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'examples' / 'tool'
    path.parent.mkdir()
    path.write_bytes(b'print("hi")\n')
    assert _classify(['examples/tool']) == (True, False, False)
    path.unlink()
    assert _classify(['examples/tool']) == (False, False, False)


SHEBANG_ROWS = [
    ('env-node', b'#!/usr/bin/env node\n', False),
    ('env-python3', b'#!/usr/bin/env python3\n', False),
    ('env-two-space-python', b'#!/usr/bin/env  python\n', False),
    ('env-s-node-flags', b'#!/usr/bin/env -S node --x\n', False),
    ('ts-node-hyphen-boundary', b'#!/usr/bin/ts-node\n', False),
    ('nodejs-suffix', b'#!/usr/bin/env nodejs\n', False),
    ('usr-local-python313', b'#!/usr/local/bin/python3.13\n', False),
    ('local-env-glued', b'#!/localenv python\n', False),
    ('crlf-ends-at-cr', b'#!/usr/bin/env node\r\n', False),
    ('shebang-no-line-end-in-100', b'#!' + b'a' * 200, False),
    ('plain-sh', b'#!/bin/sh\n', True),
    ('nodemon-word-boundary', b'#!/usr/bin/nodemon\n', True),
    ('bunx-word-boundary', b'#!/usr/bin/bunx\n', True),
    ('deno', b'#!/usr/bin/deno\n', True),
    ('node18-word-boundary', b'#!/usr/bin/node18\n', True),
    ('env-s-python3', b'#!/usr/bin/env -S python3\n', True),
    ('env-var-assignment', b'#!/usr/bin/env VAR=1 python\n', True),
    ('tab-not-space', b'#!/usr/bin/env\tpython\n', True),
    ('opt-python', b'#!/opt/python/bin/python\n', True),
    ('bom-defeats-shebang', b'\xef\xbb\xbf#!/usr/bin/env node\n', True),
    ('empty', b'', True),
    ('no-shebang', b'print("hi")\n', True),
    ('second-line-only', b'#!/bin/sh\nnode\n', True),
    ('shebang-not-at-offset-0', b'x #!/usr/bin/env node\n', True),
    ('cr-cut-second-line-not-read', b'#!/bin/sh\rnode\n', True),
]


@pytest.mark.parametrize('line,docs_only', [row[1:] for row in SHEBANG_ROWS],
                         ids=[row[0] for row in SHEBANG_ROWS])
def test_extensionless_shebang_first_line_runs_full_gates(tmp_path, monkeypatch,
                                                          line, docs_only):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'examples' / 'tool'
    path.parent.mkdir()
    path.write_bytes(line)
    assert _classify(['examples/tool']) == (docs_only, False, False)


@pytest.mark.parametrize('path,line,docs_only', [
    ('.claude/tool', b'#!/usr/bin/env node\n', False),
    ('examples/x.', b'#!/usr/bin/env node\n', False),
    ('examples/x.', b'print("hi")\n', True),
], ids=['hidden-doc-dir-node', 'trailing-dot-node', 'trailing-dot-no-shebang'])
def test_extensionless_shebang_other_doc_paths(tmp_path, monkeypatch, path,
                                               line, docs_only):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_bytes(line)
    assert _classify([path]) == (docs_only, False, False)


def test_pure_markdown_remains_docs_only():
    assert _classify(['docs/guide.md']) == (True, False, False)


@pytest.mark.parametrize('after', [
    'PRICING_VERSION = "7"\nOTHER = 1\n# added comment\n',
    'PRICING_VERSION = "8"\nOTHER = 2\n',
    'PRICING_VERSION = "eight"\nOTHER = 1\n',
    'PRICING_VERSION = "8" # added comment\nOTHER = 1\n',
])
def test_bot_paths_cannot_hide_other_constants_changes(after):
    assert _classify(['backend/constants.py', 'src/pricing.json'],
                     'PRICING_VERSION = "7"\nOTHER = 1\n', after) == (False, False, False)


def test_genuine_numeric_bump_still_uses_bot_data_class():
    for paths in (['backend/constants.py', 'src/pricing.json'],
                  ['backend/constants.py', 'src/pricing.json', 'src/pricing_notices.json']):
        assert _classify(paths, 'PRICING_VERSION = "7"\nOTHER = 1\n',
                         'PRICING_VERSION = "8"\nOTHER = 1\n') == (False, True, False)
        assert _classify(paths, unreadable=True) == (False, False, False)


@pytest.mark.parametrize('notices', [False, True])
@pytest.mark.parametrize('event,author', [
    (EVENT, None),
    (EVENT, ''),
    (EVENT, 'human@example.com'),
    (PR_EVENT, BOT_EMAIL),
], ids=['unreadable-author', 'empty-author', 'human-author', 'pull-request'])
def test_bot_paths_without_a_bot_authored_push_run_full(notices, event, author):
    paths = ['backend/constants.py', 'src/pricing.json']
    if notices:
        paths.append('src/pricing_notices.json')
    assert _classify(paths, 'PRICING_VERSION = "7"\nOTHER = 1\n',
                     'PRICING_VERSION = "8"\nOTHER = 1\n',
                     event=event, author=author) == (False, False, False)


def test_comment_edit_in_a_leg_read_docs_path_is_full():
    assert _classify(['examples/known.py'], 'x = 1\ny = 2\n# before\n',
                     'x = 1\ny = 2\n# after\n') == (False, False, False)


@pytest.mark.parametrize('digits', ['7', '8'])
@pytest.mark.parametrize('line', [
    'PRICING_VERSION  = "{digits}"\n',
    'PRICING_VERSION\t= "{digits}"\n',
    'PRICING_VERSION = "{digits}" \n',
    'PRICING_VERSION = "{digits}"\r\n',
])
def test_bot_version_assignment_changes_only_digits(digits, line):
    before = 'PRICING_VERSION = "7"\nOTHER = 1\n'
    after = line.format(digits=digits) + 'OTHER = 1\n'
    assert _classify(['backend/constants.py', 'src/pricing.json'], before, after) == (False, False, False)


@pytest.mark.parametrize('line', [
    'PRICING_VERSION  = "{digits}"\n',
    'PRICING_VERSION\t= "{digits}"\n',
    'PRICING_VERSION = "{digits}" \n',
    'PRICING_VERSION = "{digits}"\r\n',
])
def test_bot_bump_preserves_existing_assignment_format(line):
    assert _classify(['backend/constants.py', 'src/pricing.json'],
                     line.format(digits='7') + 'OTHER = 1\n',
                     line.format(digits='8') + 'OTHER = 1\n') == (False, True, False)


@pytest.mark.parametrize('before,after,want', [
    ('99', '1', False), ('1', '99', False), ('99', '88', True),
])
def test_bot_version_width_preserves_measured_pair(before, after, want):
    assert _classify(['backend/constants.py', 'src/pricing.json'],
                     f'# comment\nPRICING_VERSION = "{before}"\n',
                     f'# comment\nPRICING_VERSION = "{after}"\n') == (False, want, False)
