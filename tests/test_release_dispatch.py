"""Release preflight logic with invented GitHub facts."""
from __future__ import annotations

import pytest

from scripts.ci.release_dispatch import (
    Decision,
    decide,
    main,
    parse_release_version,
)


@pytest.mark.parametrize("text", ["0.5.0", "10.20.30", "01.2.3"])
def test_parse_release_version_accepts(text):
    assert parse_release_version(text) == text


@pytest.mark.parametrize("text", [
    "", "  ", "v0.5.0", "0.5", "0.5.0-dev", "0.5.0.1",
    "0.5.0-rc1", "0.5.0-dev!", "0.5.0\n", " 0.5.0", "0.5.0 ",
])
def test_parse_release_version_refuses(text):
    with pytest.raises(ValueError, match="X.Y.Z"):
        parse_release_version(text)


def test_decide_free():
    result = decide("9.9.9", False, False)
    assert isinstance(result, Decision)
    assert result.ok is True
    assert "9.9.9" in result.reason
    assert "free" in result.reason


@pytest.mark.parametrize("tag,release,blocker", [
    (True, False, "tag"), (False, True, "release"), (True, True, "tag"),
])
def test_decide_refuses_existing_publication(tag, release, blocker):
    result = decide("9.9.9", tag, release)
    assert result.ok is False
    assert blocker in result.reason
    assert "v9.9.9" in result.reason


def test_decide_refuses_bad_version():
    result = decide("9.9.9-dev", False, False)
    assert result.ok is False
    assert "X.Y.Z" in result.reason
    assert "9.9.9-dev" in result.reason


@pytest.mark.parametrize("version", ["９.９.９", "٩.٩.٩", "9.9.９"])
def test_decide_refuses_unicode_digits(version):
    assert decide(version, False, False).ok is False


@pytest.mark.parametrize("version,expected", [
    ("9.9.9", "9.9.10-dev\n"),
    ("9.9.9007199254740992", "9.9.9007199254740993-dev\n"),
    ("9.9.99", "9.9.100-dev\n"),
])
def test_cli_successor_increments_exactly(version, expected, capsys):
    assert main(["successor", version]) == 0
    output = capsys.readouterr()
    assert output.out == expected
    assert output.err == ""


@pytest.mark.parametrize("version", [
    "", "  ", "v9.9.9", "9.9", "9.9.9-dev", "9.9.9.1",
    "9.9.9-rc1", "9.9.9-dev!", "9.9.9\n", " 9.9.9", "9.9.9 ",
    "９.９.９", "٩.٩.٩", "9.9.９",
])
def test_cli_successor_refuses_invalid_version(version, capsys):
    assert decide(version, False, False).ok is False
    assert main(["successor", version]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "X.Y.Z" in output.err


@pytest.mark.parametrize("tag,release,code,reason", [
    ("FaLsE", "FALSE", 0, "free"),
    ("TRUE", "false", 1, "tag"),
    ("false", "TrUe", 1, "release"),
])
def test_cli_decide(tag, release, code, reason, capsys):
    assert main(["decide", "--version", "9.9.9", "--tag-exists", tag,
                 "--release-exists", release]) == code
    output = capsys.readouterr()
    assert reason in output.out
    assert output.err == ""
