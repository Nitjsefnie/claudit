"""Section-aware setup classification without network or repository doc edits."""
from __future__ import annotations

import pytest

from scripts.ci import classify_changes as classify

BASE = 'a' * 40
HEAD = 'b' * 40
EVENT = {'name': 'pull_request', 'repository': 'o/r', 'pull_request': '17'}


def _doc(heading='Quick start', command='install', prose='intro'):
    return (f'# Guide\n{prose}\n## {heading}\nSetup prose\n### Commands\n'
            f'```bash\n{command}\npython3 -m uvicorn app:app\n```\n'
            'Follow-on prose\n```sh\nfollow_on\n```\n## Other\nend\n')


def _classify(tmp_path, blobs, paths=None, *, fail_url='', event=None):
    def run(argv):
        url = next(arg for arg in argv if arg.startswith('repos/'))
        if fail_url and url.endswith(fail_url):
            raise OSError('unreadable')
        if url.endswith('/files') or '/compare/' in url:
            return '\n'.join(paths if paths is not None else blobs)
        if url.endswith('/pulls/17'):
            return f'{BASE} {HEAD}'
        if '/actions/workflows/' in url:
            return f'{BASE} completed success 123\n'
        if '/jobs?' in url:
            return 'classify success\ntests success\naggregate success\n'
        prefix = 'repos/o/r/contents/'
        if url.startswith(prefix):
            path, ref = url[len(prefix):].split('?ref=')
            assert ref in (BASE, HEAD)
            return blobs[path][0 if ref == BASE else 1]
        raise AssertionError(f'unexpected call: {argv}')

    result = classify.classify(event or EVENT, run)
    out = tmp_path / 'outputs'
    classify.write_outputs(str(out), *result)
    return dict(line.split('=', 1) for line in out.read_text().splitlines())


def _assert_flags(outputs, *, docs=False, setup=False):
    assert outputs['docs_only'] == str(docs).lower()
    assert outputs.get('setup_only') == str(setup).lower()
    for name in ('data_only', 'comment_only', 'thresholds_only'):
        assert outputs[name] == 'false'


@pytest.mark.parametrize('path,heading', [
    ('README.md', 'Quick start'),
    ('CONTRIBUTING.md', 'Getting it running'),
    ('AGENTS.md', 'Build and test commands'),
])
def test_prose_outside_setup_is_docs_only(tmp_path, path, heading):
    outputs = _classify(tmp_path, {path: (_doc(heading), _doc(heading, prose='new intro'))})
    _assert_flags(outputs, docs=True)


def test_changed_setup_block_is_setup_only(tmp_path):
    outputs = _classify(tmp_path, {'README.md': (_doc(), _doc(command='broken install'))})
    _assert_flags(outputs, setup=True)


def test_setup_and_code_runs_full_gate(tmp_path):
    outputs = _classify(tmp_path, {}, ['README.md', 'backend/app.py'])
    _assert_flags(outputs)
    assert 'outside documentation' in outputs['reason']


@pytest.mark.parametrize('ref', [BASE, HEAD])
def test_blob_read_failure_runs_full_gate(tmp_path, ref):
    outputs = _classify(tmp_path, {'README.md': (_doc(), _doc())},
                        fail_url=f'contents/README.md?ref={ref}')
    _assert_flags(outputs)
    assert 'README.md' in outputs['reason']
    assert 'read' in outputs['reason']


def test_changed_setup_and_another_docs_prose_is_setup_only(tmp_path):
    outputs = _classify(tmp_path, {
        'README.md': (_doc(), _doc(command='new install')),
        'AGENTS.md': (_doc('Build and test commands'),
                      _doc('Build and test commands', prose='reflowed')),
    })
    _assert_flags(outputs, setup=True)


@pytest.mark.parametrize('path,heading', [
    ('README.md', 'Quick start'),
    ('CONTRIBUTING.md', 'Getting it running'),
    ('AGENTS.md', 'Build and test commands'),
])
def test_reflow_inside_pinned_section_is_setup_only(tmp_path, path, heading):
    before = _doc(heading)
    after = before.replace('Setup prose', 'Reflowed\nsetup prose').replace('follow_on', 'other')
    outputs = _classify(tmp_path, {path: (before, after)})
    _assert_flags(outputs, setup=True)


@pytest.mark.parametrize('side', [0, 1])
@pytest.mark.parametrize('broken', [
    _doc().replace('## Quick start', '## Renamed'),
    _doc().replace('python3 -m uvicorn', 'another server'),
    _doc().replace('```\nFollow-on', 'Follow-on'),
    b'\xff',
])
def test_missing_or_unreadable_setup_runs_full_gate(tmp_path, side, broken):
    pair = [_doc(), _doc(command='changed')]
    pair[side] = broken
    outputs = _classify(tmp_path, {'README.md': pair})
    _assert_flags(outputs)
    assert 'README.md' in outputs['reason']
    assert 'full gate' in outputs['reason']


def test_later_failure_overrides_an_earlier_changed_block(tmp_path):
    outputs = _classify(tmp_path, {
        'README.md': (_doc(), _doc(command='changed')),
        'AGENTS.md': (_doc('Build and test commands'), 'missing'),
    })
    _assert_flags(outputs)
    assert 'AGENTS.md' in outputs['reason']


def test_setup_bytes_are_compared_without_normalizing_whitespace(tmp_path):
    before = _doc()
    after = before.replace('install\n', 'install  \n')
    outputs = _classify(tmp_path, {'README.md': (before, after)})
    _assert_flags(outputs, setup=True)


def test_push_compares_setup_since_verified_base(tmp_path):
    event = {'name': 'push', 'repository': 'o/r', 'before': 'e' * 40, 'sha': HEAD}
    outputs = _classify(tmp_path, {'README.md': (_doc(), _doc(command='changed'))}, event=event)
    _assert_flags(outputs, setup=True)


def test_unreadable_refs_run_full_gate(tmp_path):
    outputs = _classify(tmp_path, {'README.md': (_doc(), _doc())}, fail_url='/pulls/17')
    _assert_flags(outputs)
    assert 'read' in outputs['reason']


@pytest.mark.parametrize('replacement', ['REMOVED_GUEST_SETTING', ''])
@pytest.mark.parametrize('event', [
    EVENT,
    {'name': 'push', 'repository': 'o/r', 'before': 'e' * 40, 'sha': HEAD},
], ids=['pull_request', 'push'])
def test_readme_guest_setting_outside_section_is_pinned(tmp_path, replacement, event):
    before = _doc(prose='Guest setting: CLAUDIT_GUEST_LOGIN')
    after = before.replace('CLAUDIT_GUEST_LOGIN', replacement)
    outputs = _classify(tmp_path, {'README.md': (before, after)}, event=event)
    _assert_flags(outputs, setup=True)


def test_readme_guest_setting_presence_allows_unrelated_prose_changes(tmp_path):
    before = _doc(prose='CLAUDIT_GUEST_LOGIN\nCLAUDIT_GUEST_LOGIN')
    after = _doc(prose='Guest setting: CLAUDIT_GUEST_LOGIN')
    outputs = _classify(tmp_path, {'README.md': (before, after)})
    _assert_flags(outputs, docs=True)
