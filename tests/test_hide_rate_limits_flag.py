"""CLAUDIT_HIDE_RATE_LIMITS rides the /api/dashboard payload (issue #943).

The backend reads the env once at startup — the timing.py pattern — and
exposes the boolean on the dashboard payload; HIDING the hits in the UI
is the frontend's half. Both shapes are pinned here:

- env unset -> `hide_rate_limits: false`, the payload otherwise exactly
  the shape it had before the flag existed;
- env set   -> `hide_rate_limits: true`, with `rate_limit_hits` still
  present and computed identically. The flag is display-only: parsers,
  files.rate_limit_hits and the hits query never see it.
"""
from __future__ import annotations

import importlib
import os

import pytest

from backend import hide_rate_limits

# Reuses test_api's fresh-DB + mini-R2 fixture WITH rate-limit hits —
# an empty hits list would let a broken computation pass unnoticed.
# Importing the function object is what registers the fixture here.
from tests.test_api import _app_with_rl_data_fixture

__all__ = ["_app_with_rl_data_fixture"]

FLAG_ENV = "CLAUDIT_HIDE_RATE_LIMITS"

# The dashboard payload's top-level keys: today's shape plus the flag.
# A payload-shape change must edit this deliberately.
DASHBOARD_KEYS = frozenset({
    "range", "project", "bucket_s", "hourly", "token_types",
    "cost_by_model", "cost_by_model_provider", "cost_by_project",
    "tokens_by_project", "rate_limit_hits", "sessions", "total_sessions",
    "main_w_usage", "main_empty", "subagent_files",
    "subagent_only_sessions", "total_prompts", "total_turns",
    "response_sizes", "hide_rate_limits",
})


@pytest.fixture(name="hide_flag_env")
def _hide_flag_env():
    """Drive CLAUDIT_HIDE_RATE_LIMITS the way a deploy does: set the
    env, then let backend/hide_rate_limits re-read it at import — the
    payload picks the value up through the module reference. Restores
    BOTH the env (a host-exported value must neither leak into a test
    nor be silently dropped) and the module constant afterwards."""
    original = os.environ.get(FLAG_ENV)     # None == unset (env is str)

    def apply(value: str | None) -> None:
        if value is None:
            os.environ.pop(FLAG_ENV, None)
        else:
            os.environ[FLAG_ENV] = value
        importlib.reload(hide_rate_limits)

    apply(None)                     # start from the documented default
    yield apply

    if original is None:
        os.environ.pop(FLAG_ENV, None)
    else:
        os.environ[FLAG_ENV] = original
    importlib.reload(hide_rate_limits)


def test_hide_rate_limits_false_when_env_unset(app_with_rl_data, hide_flag_env):
    client, in_range, _out_range = app_with_rl_data
    hide_flag_env(None)
    # fresh=1 bypasses the response cache — without it this would pin
    # whatever shape a previous request happened to cache.
    body = client.get("/api/dashboard?range=3650d&fresh=1").json()

    assert body["hide_rate_limits"] is False
    # Display-only flag: the hits are still queried and folded.
    assert in_range in [h["ts"] for h in body["rate_limit_hits"]]
    assert set(body) == DASHBOARD_KEYS


def test_hide_rate_limits_true_when_env_set_keeps_hits_and_rest(
    app_with_rl_data, hide_flag_env,
):
    client, in_range, _out_range = app_with_rl_data
    hide_flag_env(None)
    baseline = client.get("/api/dashboard?range=3650d&fresh=1").json()

    hide_flag_env("1")
    body = client.get("/api/dashboard?range=3650d&fresh=1").json()

    assert body["hide_rate_limits"] is True
    assert baseline["hide_rate_limits"] is False
    # Tracking end to end is unchanged: same computed hits...
    assert in_range in [h["ts"] for h in body["rate_limit_hits"]]
    assert body["rate_limit_hits"] == baseline["rate_limit_hits"]
    # ...and every other key identical — the flag flips nothing else.
    rest_on = {k: v for k, v in body.items() if k != "hide_rate_limits"}
    rest_off = {k: v for k, v in baseline.items() if k != "hide_rate_limits"}
    assert rest_on == rest_off
