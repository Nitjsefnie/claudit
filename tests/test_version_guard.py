"""Unit tests for scripts/ci/version_guard.py, the decider behind
.github/workflows/version-guard.yml (issue #131).

The workflow gathers the facts with `gh` and calls the script; these tests
pin the decision logic itself — version shape, the pricing-bot carve-out,
and the precedence order that decides between them. Facts are invented per
case; nothing here touches the network or a database.
"""
from __future__ import annotations

import pytest

from scripts.ci.version_guard import (
    BOT_EMAIL,
    BOT_FILES,
    Decision,
    decide,
    is_bot_exempt,
    main,
    parse_version,
)

BOT = BOT_EMAIL
HUMAN = "dev@example.com"
BOTH_FILES = list(BOT_FILES)


# --- parse_version ---------------------------------------------------------

def test_parse_version_final():
    assert parse_version("0.3.0") == ("0.3.0", "")


def test_parse_version_dev():
    assert parse_version("0.4.0-dev") == ("0.4.0", "dev")


def test_parse_version_dated_prerelease():
    assert parse_version("0.4.0-20260731") == ("0.4.0", "20260731")


@pytest.mark.parametrize("text", [
    "",             # empty
    "   ",          # whitespace only
    "not a version",
    "v0.3.0",       # release.yml adds the tag prefix itself; a `v` here would tag vv0.3.0
    "0.3",          # two components
    "0.3.0-",       # a lone dash is no suffix
    "0.3.0-dev!x",  # the suffix charset is [0-9A-Za-z.-]
])
def test_parse_version_refuses(text):
    with pytest.raises(ValueError):
        parse_version(text)


# --- is_bot_exempt ---------------------------------------------------------

def test_bot_exempt_bot_author_both_files():
    assert is_bot_exempt(BOT, BOTH_FILES) is True


def test_bot_exempt_bot_author_subset_of_its_files():
    # A strict subset of the carve-out set keeps the bot's shape.
    assert is_bot_exempt(BOT, ["src/pricing.json"]) is True


def test_bot_exempt_requires_a_non_empty_file_set():
    assert is_bot_exempt(BOT, []) is False


def test_bot_exempt_bot_author_extra_file():
    assert is_bot_exempt(BOT, BOTH_FILES + ["VERSION"]) is False


def test_bot_exempt_needs_bot_author_and_file_shape():
    # The carve-out is keyed on author AND files — either alone is not enough.
    assert is_bot_exempt(HUMAN, BOTH_FILES) is False
    assert is_bot_exempt("", BOTH_FILES) is False
    assert is_bot_exempt(HUMAN, []) is False


# --- decide ----------------------------------------------------------------

def test_decide_unparseable_refuses():
    result = decide("garbage", False, HUMAN, [])
    assert result.ok is False
    assert "semver" in result.reason


def test_decide_unparseable_refuses_even_with_no_tag():
    # Precedence (a): an unparseable version refuses before anything else.
    result = decide("garbage", False, HUMAN, [])
    assert result.ok is False


def test_decide_unparseable_refuses_with_tag():
    result = decide("garbage", True, HUMAN, [])
    assert result.ok is False


def test_decide_tag_free_passes_final_version():
    result = decide("0.4.0", False, HUMAN, [])
    assert result.ok is True


def test_decide_tag_free_passes_dev_version():
    result = decide("0.4.0-dev", False, HUMAN, [])
    assert result.ok is True


def test_decide_tag_plus_human_refuses():
    result = decide("0.3.0", True, HUMAN, BOTH_FILES)
    assert result.ok is False
    assert "already published" in result.reason


def test_decide_tag_plus_bot_commit_passes():
    result = decide("0.3.0", True, BOT, BOTH_FILES)
    assert result.ok is True


def test_decide_tag_plus_bot_commit_extra_file_refuses():
    result = decide("0.3.0", True, BOT, BOTH_FILES + ["VERSION"])
    assert result.ok is False


def test_decide_tag_plus_non_bot_with_exactly_bot_files_refuses():
    result = decide("0.3.0", True, HUMAN, BOTH_FILES)
    assert result.ok is False


def test_decide_result_is_a_decision():
    assert isinstance(decide("0.4.0-dev", False, HUMAN, []), Decision)


# --- CLI -------------------------------------------------------------------

def test_cli_pass_exits_zero_and_prints_the_reason(capsys):
    rc = main([
        "decide", "--version", "0.4.0-dev", "--tag-exists", "false",
        "--author-email", HUMAN, "--changed-files", "",
    ])
    assert rc == 0
    assert capsys.readouterr().out.strip()


def test_cli_refuse_exits_one():
    rc = main([
        "decide", "--version", "0.3.0", "--tag-exists", "true",
        "--author-email", HUMAN, "--changed-files", "src/pricing.json",
    ])
    assert rc == 1


def test_cli_empty_bot_facts_still_decides(capsys):
    # How the workflow calls it on PRs: no commit facts, tag check only.
    rc = main([
        "decide", "--version", "0.4.0-dev", "--tag-exists", "false",
        "--author-email", "", "--changed-files", "",
    ])
    assert rc == 0
    assert capsys.readouterr().out.strip()
