from datetime import timedelta

import pytest
from fastapi import HTTPException

from backend.api_common import _parse_range


def test_parse_range_empty_answers_400():
    with pytest.raises(HTTPException) as exc_info:
        _parse_range("")

    assert exc_info.value.status_code == 400


def test_parse_range_single_unit_answers_400():
    with pytest.raises(HTTPException) as exc_info:
        _parse_range("d")

    assert exc_info.value.status_code == 400


def test_parse_range_non_numeric_400():
    with pytest.raises(HTTPException) as exc_info:
        _parse_range("1e5d")

    assert exc_info.value.status_code == 400


def test_parse_range_overflow_400():
    with pytest.raises(HTTPException) as exc_info:
        _parse_range("999999999d")

    assert exc_info.value.status_code == 400


def test_parse_range_valid_shapes():
    for value in ("30d", "12h", "all"):
        delta = _parse_range(value)

        assert isinstance(delta, timedelta)
        assert delta > timedelta(0)
