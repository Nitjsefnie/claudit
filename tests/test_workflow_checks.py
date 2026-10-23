"""The workflow checks recognize references and real output writes."""
from __future__ import annotations

import pytest

from tests.workflow_checks import references, written_outputs


@pytest.mark.parametrize("value, expected", [
    ("${{ matrix.language }}", {("language",)}),
    ("${{matrix ['build-mode']}}", {("build-mode",)}),
    ("/language:${{ format('{0}', matrix['language']) }}", {("language",)}),
    ("${{ 'matrix.language' }}", set()),
    ("matrix.language", set()),
    ("${{ format('it''s matrix.language', 'python') }}", set()),
])
def test_matrix_references(value, expected):
    assert references(value, "matrix") == expected


@pytest.mark.parametrize("value", [
    "${{steps.base.outputs.ref}}",
    "${{ steps['base']['outputs']['ref'] }}",
    "${{ format('{0}', steps . base . outputs . ref) }}",
])
def test_step_output_references(value):
    assert references(value, "steps") == {("base", "outputs", "ref")}


def test_written_outputs_excludes_echoes_that_do_not_reach_the_output_file():
    run = '\n'.join([
        'echo "decoy=wrong"',
        '{',
        '  echo "ref=registry/image"',
        '  echo "key=image-cache"',
        '} >> "$GITHUB_OUTPUT"',
        'echo "fallback=mirror/image" >> "$GITHUB_OUTPUT"',
        'echo "lost=wrong" >> "$OTHER_FILE"',
        'echo "similar=wrong" >> "$GITHUB_OUTPUT_OTHER"',
    ])
    assert written_outputs(run) == {"ref", "key", "fallback"}
