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


@pytest.mark.parametrize('path', ['examples/x.py', '.claude/x.sh', 'examples/x.mjs', '.claude/x.yaml'])
def test_measured_additions_in_docs_globs_run_full(path):
    assert _classify([path]) == (False, False, False)


def test_extensionless_head_shebang_and_read_failures_run_full(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'examples/tool'
    path.parent.mkdir()
    for content in ('#!/bin/sh\n# comment\n', "#!'\n"):
        path.write_text(content)
        assert _classify(['examples/tool']) == (False, False, False)
    path.unlink()
    assert _classify(['examples/tool']) == (False, False, False)


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


def test_comment_edit_in_a_measured_docs_path_is_full():
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
