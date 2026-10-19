"""Legacy hide alias omits served hits while retaining their stored source."""
from __future__ import annotations

import pytest

from backend import db, flags
from backend.cache import response_cache
from backend.flags import resolve_config

# Reuses test_api's fresh-DB + mini-R2 fixture WITH rate-limit hits —
# an empty hits list would let a broken computation pass unnoticed.
# Importing the function object is what registers the fixture here.
from tests.test_api import _app_with_rl_data_fixture

__all__ = ["_app_with_rl_data_fixture"]

FLAG_ENV = "CLAUDIT_HIDE_RATE_LIMITS"


@pytest.fixture(name="hide_flag_env")
def _hide_flag_env(monkeypatch):
    original = flags.CONFIG

    def apply(value: str | None) -> None:
        env = {} if value is None else {FLAG_ENV: value}
        monkeypatch.setattr(flags, "CONFIG", resolve_config(env))
        response_cache.clear()

    apply(None)                     # start from the documented default
    yield apply
    monkeypatch.setattr(flags, "CONFIG", original)
    response_cache.clear()


def test_hide_rate_limits_true_omits_hits_and_keeps_unrelated_fields(
    app_with_rl_data, hide_flag_env,
):
    client, in_range, _out_range = app_with_rl_data
    hide_flag_env(None)
    baseline = client.get("/api/dashboard?range=3650d&fresh=1").json()

    assert in_range in [h["ts"] for h in baseline["rate_limit_hits"]]
    with db.viz_conn() as conn:
        row = conn.execute("SELECT rate_limit_hits FROM files "
                           "WHERE session_id = %s", ("sess-RL",)).fetchone()
    assert row is not None
    stored = row[0]
    assert stored

    hide_flag_env("1")
    body = client.get("/api/dashboard?range=3650d&fresh=1").json()

    assert body["hide_rate_limits"] is True
    assert baseline["hide_rate_limits"] is False
    assert "rate_limit_hits" not in body
    with db.viz_conn() as conn:
        row = conn.execute("SELECT rate_limit_hits FROM files "
                           "WHERE session_id = %s", ("sess-RL",)).fetchone()
        assert row is not None and row[0] == stored
    rest_on = {k: v for k, v in body.items() if k != "hide_rate_limits"}
    rest_off = {k: v for k, v in baseline.items()
                if k not in ("hide_rate_limits", "rate_limit_hits")}
    assert rest_on == rest_off
