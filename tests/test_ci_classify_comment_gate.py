from __future__ import annotations

import pytest

from tests.test_ci_gate_botdata import classify

EVENT = {'name': 'pull_request', 'repository': 'o/r', 'pull_request': '17'}
BASE = 'a' * 40
HEAD = 'b' * 40


def _classify(paths, before='', after='', unreadable=False):
    def run(argv):
        if any(arg.endswith('/pulls/17/files') for arg in argv):
            return '\n'.join(paths)
        if argv[-1] == '.base.sha + " " + .head.sha':
            return f'{BASE} {HEAD}'
        if unreadable:
            raise OSError('cannot read blob')
        if f'contents/{paths[0]}?ref={BASE}' in argv[-1]:
            return before
        if f'contents/{paths[0]}?ref={HEAD}' in argv[-1]:
            return after
        raise AssertionError(argv)
    return classify.classify(EVENT, run)[:3]


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
