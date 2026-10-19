from datetime import timedelta

import pytest
from fastapi import HTTPException

from backend.api_common import _parse_range


@pytest.mark.parametrize("value", ["", "d"], ids=["empty", "bare-unit"])
def test_parse_range_missing_quantity_answers_400(value):
    with pytest.raises(HTTPException) as exc_info:
        _parse_range(value)

    assert exc_info.value.status_code == 400


def test_parse_range_valid_shapes():
    for value in ("30d", "12h", "all"):
        delta = _parse_range(value)

        assert isinstance(delta, timedelta)
        assert delta > timedelta(0)
