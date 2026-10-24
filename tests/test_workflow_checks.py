"""The workflow checks recognize references and real output writes."""
from __future__ import annotations

import pytest

from tests import test_ci_workflows as ci_checks
from tests import test_workflow_cached_image as base_checks
from tests import test_workflow_postgres_action as pg_checks
from tests.workflow_checks import references, written_outputs


@pytest.mark.parametrize("value, expected", [
    ("${{ matrix.language }}", {("language",)}),
    ("${{ format('{1}', '}}', matrix.language) }}", {("language",)}),
    ("${{ format('{1}', 'it''s }}', matrix.language) }}", {("language",)}),
    ("${{ '}}' }} ${{ matrix.language }}", {("language",)}),
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


@pytest.mark.parametrize("target", ['"$GITHUB_OUTPUT"', '"${GITHUB_OUTPUT}"'])
def test_group_output_redirects_respect_command_overrides(target):
    run = '\n'.join([
        '{',
        '  echo "ref=image" >> "$OTHER_FILE"',
        '  echo "key=cache"',
        '} >> ' + target,
    ])
    assert written_outputs(run) == {"key"}


def test_codeql_accepts_delimiters_inside_a_quoted_identity_expression(tmp_path, monkeypatch):
    source = ci_checks.CODEQL_WORKFLOW.read_text(encoding="utf-8")
    source = source.replace('languages: ${{ matrix.language }}',
                            "languages: ${{ format('{1}', '}}', matrix.language) }}")
    path = tmp_path / "codeql.yml"
    path.write_text(source, encoding="utf-8", newline="\n")
    monkeypatch.setattr(ci_checks, "CODEQL_WORKFLOW", path)
    ci_checks.test_codeql_matrix_analyses_the_workflows()


def test_base_check_rejects_an_output_redirected_out_of_its_group(monkeypatch):
    steps = base_checks._steps()
    producer = next(step for step in steps if "CLAUDIT_BASE_IMAGE" in step.get("env", {}))
    producer["run"] = producer["run"].replace(
        'echo "ref=${CLAUDIT_BASE_IMAGE}"',
        'echo "ref=${CLAUDIT_BASE_IMAGE}" >> "$OTHER_FILE"')
    monkeypatch.setattr(base_checks, "_steps", lambda: steps)
    with pytest.raises(AssertionError):
        base_checks.test_cached_base_image_is_the_dockerfiles_from_reference()


def test_base_check_accepts_a_coordinated_producer_rename(monkeypatch):
    steps = base_checks._steps()
    producer = next(step for step in steps if "CLAUDIT_BASE_IMAGE" in step.get("env", {}))
    old_id = producer["id"]
    producer["id"] = "renamed_base"
    for step in steps:
        for key, value in step.get("with", {}).items():
            step["with"][key] = value.replace(f"steps.{old_id}.", "steps.renamed_base.")
    monkeypatch.setattr(base_checks, "_steps", lambda: steps)
    base_checks.test_cached_base_image_is_the_dockerfiles_from_reference()


def test_postgres_check_accepts_a_coordinated_producer_rename(tmp_path, monkeypatch):
    source = pg_checks.IMAGE_ACTION.read_text(encoding="utf-8")
    old_id = next(step["id"] for step in pg_checks._load(pg_checks.IMAGE_ACTION)["runs"]["steps"]
                  if "CLAUDIT_POSTGRES_DIGEST" in step.get("env", {}))
    source = source.replace(f"id: {old_id}", "id: renamed_image")
    source = source.replace(f"steps.{old_id}.", "steps.renamed_image.")
    path = tmp_path / "action.yml"
    path.write_text(source, encoding="utf-8", newline="\n")
    monkeypatch.setattr(pg_checks, "IMAGE_ACTION", path)
    pg_checks.test_image_digest_matches_compose_and_keys_both_cache_operations()


def test_cache_check_accepts_coordinated_input_renames(tmp_path, monkeypatch):
    source = pg_checks.CACHED_ACTION.read_text(encoding="utf-8")
    for old, new in (("key", "cache_key"), ("archive", "tar_name")):
        source = source.replace(f"  {old}:\n", f"  {new}:\n")
        source = source.replace(f"inputs.{old}", f"inputs.{new}")
    path = tmp_path / "action.yml"
    path.write_text(source, encoding="utf-8", newline="\n")
    monkeypatch.setattr(pg_checks, "CACHED_ACTION", path)
    pg_checks.test_image_digest_matches_compose_and_keys_both_cache_operations()


@pytest.mark.parametrize("redirect, expected", [
    ('> "${GITHUB_OUTPUT}"', {"ref"}),
    ('>> "$GITHUB_OUTPUT" > "$OTHER_FILE"', set()),
    ('>> "$GITHUB_OUTPUT" 2> "$OTHER_FILE"', {"ref"}),
    (" >> '$GITHUB_OUTPUT'", set()),
    ('>> "$OTHER_FILE" >> "${GITHUB_OUTPUT}"', {"ref"}),
    ('1>> "$GITHUB_OUTPUT"', {"ref"}),
])
def test_direct_output_redirects(redirect, expected):
    assert written_outputs('echo "ref=image" ' + redirect) == expected


def test_base_check_accepts_a_braced_output_target(monkeypatch):
    steps = base_checks._steps()
    producer = next(step for step in steps if "CLAUDIT_BASE_IMAGE" in step.get("env", {}))
    producer["run"] = producer["run"].replace("$GITHUB_OUTPUT", "${GITHUB_OUTPUT}")
    monkeypatch.setattr(base_checks, "_steps", lambda: steps)
    base_checks.test_cached_base_image_is_the_dockerfiles_from_reference()
