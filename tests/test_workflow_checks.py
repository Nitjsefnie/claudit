"""The workflow checks recognize references and real output writes."""
from __future__ import annotations

import re

import pytest
import yaml

from tests import test_ci_workflows as ci_checks
from tests import test_workflow_cached_image as base_checks
from tests import test_workflow_postgres_action as pg_checks
from tests.workflow_checks import load_workflow, references, rename_references, written_outputs


@pytest.mark.parametrize("value, expected", [
    ("${{ matrix.language }}", {("language",)}),
    ("${{ matrix.language }} + ${{ matrix['build-mode'] }}", {("language",), ("build-mode",)}),
    ("${{ matrix.language", set()),
    ("${{ 'matrix.language }}", set()),
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
    steps = load_workflow(base_checks.WORKFLOW)["jobs"]["docker-smoke"]["steps"]
    producer = next(step for step in steps if "CLAUDIT_BASE_IMAGE" in step.get("env", {}))
    producer["run"] = producer["run"].replace(
        'echo "ref=${CLAUDIT_BASE_IMAGE}"',
        'echo "ref=${CLAUDIT_BASE_IMAGE}" >> "$OTHER_FILE"')
    monkeypatch.setattr(base_checks, "_steps", lambda: steps)
    with pytest.raises(AssertionError):
        base_checks.test_cached_base_image_is_the_dockerfiles_from_reference()


def _rename_fixture(value, context, names):
    if isinstance(value, dict):
        return {key: _rename_fixture(item, context, names) for key, item in value.items()}
    if isinstance(value, list):
        return [_rename_fixture(item, context, names) for item in value]
    if isinstance(value, str):
        return rename_references(value, context, names)
    return value


def test_base_check_accepts_a_coordinated_producer_rename(monkeypatch):
    steps = load_workflow(base_checks.WORKFLOW)["jobs"]["docker-smoke"]["steps"]
    producer = next(step for step in steps if "CLAUDIT_BASE_IMAGE" in step.get("env", {}))
    old_id = producer["id"]
    producer["id"] = old_id + "_renamed"
    steps = _rename_fixture(steps, "steps", {old_id: producer["id"]})
    monkeypatch.setattr(base_checks, "_steps", lambda: steps)
    base_checks.test_cached_base_image_is_the_dockerfiles_from_reference()


def test_postgres_check_accepts_a_coordinated_producer_rename(tmp_path, monkeypatch):
    action = load_workflow(pg_checks.IMAGE_ACTION)
    producer = next(step for step in action["runs"]["steps"]
                    if "CLAUDIT_POSTGRES_DIGEST=" in step.get("run", ""))
    old_id = producer["id"]
    producer["id"] = old_id + "_renamed"
    action = _rename_fixture(action, "steps", {old_id: producer["id"]})
    path = tmp_path / "action.yml"
    path.write_text(yaml.safe_dump(action), encoding="utf-8", newline="\n")
    monkeypatch.setattr(pg_checks, "IMAGE_ACTION", path)
    pg_checks.test_image_digest_matches_compose_and_keys_both_cache_operations()


def test_cache_check_accepts_coordinated_input_renames(tmp_path, monkeypatch):
    action = load_workflow(pg_checks.CACHED_ACTION)
    cache = next(step for step in action["runs"]["steps"]
                 if step.get("uses", "").startswith("actions/cache/"))
    names = {ref[0]: ref[0] + "_renamed" for key in ("key", "path")
             for ref in references(cache["with"][key], "inputs")}
    action["inputs"] = {names.get(key, key): value for key, value in action["inputs"].items()}
    action = _rename_fixture(action, "inputs", names)
    path = tmp_path / "action.yml"
    path.write_text(yaml.safe_dump(action), encoding="utf-8", newline="\n")
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
    steps = load_workflow(base_checks.WORKFLOW)["jobs"]["docker-smoke"]["steps"]
    producer = next(step for step in steps if "CLAUDIT_BASE_IMAGE" in step.get("env", {}))
    producer["run"] = producer["run"].replace("$GITHUB_OUTPUT", "${GITHUB_OUTPUT}")
    monkeypatch.setattr(base_checks, "_steps", lambda: steps)
    base_checks.test_cached_base_image_is_the_dockerfiles_from_reference()


@pytest.mark.parametrize("surface", ["base", "image", "inputs"])
def test_coordinated_rename_fixtures_accept_index_notation(surface, tmp_path, monkeypatch):
    if surface == "base":
        module, attribute = base_checks, "WORKFLOW"
        check = base_checks.test_cached_base_image_is_the_dockerfiles_from_reference
    else:
        module = pg_checks
        attribute = "IMAGE_ACTION" if surface == "image" else "CACHED_ACTION"
        check = pg_checks.test_image_digest_matches_compose_and_keys_both_cache_operations
    source = getattr(module, attribute).read_text(encoding="utf-8")
    pattern = r"\b(steps)\.([A-Za-z_][A-Za-z0-9_-]*)"
    if surface == "inputs":
        action = load_workflow(pg_checks.CACHED_ACTION)
        cache = next(step for step in action["runs"]["steps"]
                     if step.get("uses", "").startswith("actions/cache/"))
        names = {ref[0] for key in ("key", "path")
                 for ref in references(cache["with"][key], "inputs")}
        pattern = r"\b(inputs)\.(" + "|".join(map(re.escape, sorted(names))) + r")\b"
    source = re.sub(pattern, lambda match: f"{match[1]}['{match[2]}']", source)
    path = tmp_path / "indexed.yml"
    path.write_text(source, encoding="utf-8", newline="\n")
    monkeypatch.setattr(module, attribute, path)
    check()
    if surface == "base":
        test_base_check_accepts_a_coordinated_producer_rename(monkeypatch)
    elif surface == "image":
        test_postgres_check_accepts_a_coordinated_producer_rename(tmp_path, monkeypatch)
    else:
        test_cache_check_accepts_coordinated_input_renames(tmp_path, monkeypatch)


@pytest.mark.parametrize("value, context, names, expected", [
    ("${{ steps . base .outputs.ref }}", "steps", {"base": "next"},
     "${{ steps ['next'] .outputs.ref }}"),
    ("${{ runner.temp }}/${{ inputs['archive'] }}", "inputs", {"archive": "tar_name"},
     "${{ runner.temp }}/${{ inputs['tar_name'] }}"),
    ("${{ format('steps.base.outputs.ref }}', steps['base']['outputs']['ref']) }}",
     "steps", {"base": "next"},
     "${{ format('steps.base.outputs.ref }}', steps['next']['outputs']['ref']) }}"),
    ("${{ format('it''s }}', inputs . archive) }}", "inputs", {"archive": "tar_name"},
     "${{ format('it''s }}', inputs ['tar_name']) }}"),
    ("steps.base.outputs.ref", "steps", {"base": "next"}, "steps.base.outputs.ref"),
])
def test_reference_renames_preserve_surrounding_expression_text(value, context, names, expected):
    assert rename_references(value, context, names) == expected


@pytest.mark.parametrize("body", ["a''" * 2000, "${{" * 2000])
def test_unterminated_repetitive_expressions_are_ignored(body):
    value = "${{ '" + body
    assert references(value, "matrix") == set()
    assert rename_references(value, "matrix", {"language": "renamed"}) == value
