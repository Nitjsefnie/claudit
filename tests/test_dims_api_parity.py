"""API-response parity across the surrogate-id migration.

Every payload below was captured from the PRE-migration code over the
committed mini mirror (CLAUDIT_WRITE_DIMS_GOLDEN=1 regenerates it) and is
compared after the migration, so a response that changes shape, loses
`file_key` through `r2.public_key`, or stops honouring a guest strip
fails here. Dict keys sort and floats round to 9 decimals; LIST order
is preserved and compared, except under _UNORDERED_KEYS — the keys
whose source query orders nothing, where only "already unspecified"
ordering may be tolerated per the change's byte-identity constraint —
so a real ORDER BY regression on an ordered endpoint reddens this test.

Rate-derived leaves are QUANTISED, not compared (see _RATE_DERIVED_
KEYS): the golden's money was captured under the src/pricing.json rates
of capture day, so the next rate-refresh commit would redden this test
for an unrelated reason. Their KEY must
still be present on both sides (a dropped field is a shape change and
is caught); everything else — tokens, counts, ids, models, file_keys,
guest strips — keeps full value parity.
"""
from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend import api, db, ingest
from tests import mini_mirror, scratch_db

pytest_plugins = ("tests.test_api",)

GOLDEN = Path(__file__).resolve().parent / "dims_api_golden.json"
CAPTURE_ENV = "CLAUDIT_WRITE_DIMS_GOLDEN"

_PROJECT = "projA"
_MODEL = "claude"

# Leaves whose VALUES are functions of src/pricing.json (money, the
# TTL-split cost buckets, cost shares, and the rate-row flags): one
# sentinel on both sides of the comparison, key presence kept. Token
# counts sharing a bucket name live under cost_buckets only — the
# container itself is quantised, so the token fields elsewhere
# (fresh/output/n/...) keep their value parity untouched.
_RATE_DERIVED_KEYS = frozenset({
    "cost", "cost_usd", "cost_total", "cost_buckets", "cum_share",
    "share", "total_cost", "total_cost_usd", "estimated_rate",
})
_RATE_SENTINEL = "<<rate-derived>>"

# List keys whose source query has NO ORDER BY: their row order is
# unspecified, so the golden neutralises exactly these (sorted by JSON
# text on both sides) and compares every other list in order — an
# ordering regression on an ordered endpoint fails the comparison.
# `traces` comes from ctx_traces_rows, whose SELECT orders nothing.
_UNORDERED_KEYS = frozenset({"traces"})

# top-N lists whose query orders by a MEASURE with LIMIT: the measure's
# DESC order is specified (asserted on the raw payload by
# _assert_order_contract), but the order of rows TIED on the measure is
# unspecified and moved with the migration's plan — so _canon_body
# canonicalises them by (measure DESC, JSON text) on both sides.
_ORDER_CONTRACTS = {
    "/api/cache?range=all": {
        "top_output": "output",
        "top_cache_create": "c_create",
        "top_cache_read": "c_read",
    },
}


def _canon(obj: Any, orders: dict[str, str] | None = None) -> Any:
    """Sort dict keys, round floats; preserve list order by default.

    Rate-derived leaves collapse to _RATE_SENTINEL on BOTH sides (the
    golden is canon'd too), so a rate refresh cannot redden this test.
    Lists under _UNORDERED_KEYS sort by JSON text; lists named by
    `orders` (a top-N measure) sort by that measure DESC then JSON text,
    neutralising ties only — everywhere else the order is compared as
    returned, so ordered endpoints stay pinned.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in sorted(obj.items()):
            if k in _RATE_DERIVED_KEYS:
                out[k] = _RATE_SENTINEL
            elif isinstance(v, list) and k in _UNORDERED_KEYS:
                out[k] = sorted((_canon(x) for x in v),
                                key=lambda x: json.dumps(x, sort_keys=True))
            elif isinstance(v, list) and orders and k in orders:
                field = orders[k]
                out[k] = sorted(
                    (_canon(x) for x in v),
                    # bound per-iteration: pylint cell-var-from-loop
                    key=lambda x, m=field: (-x[m], json.dumps(x,
                                                              sort_keys=True)))
            else:
                out[k] = _canon(v)
        return out
    if isinstance(obj, list):
        return [_canon(x) for x in obj]
    if isinstance(obj, float):
        return round(obj, 9)
    return obj


def _canon_body(path: str, body: Any) -> Any:
    """Canon one response body under its path's order contract."""
    return _canon(body, orders=_ORDER_CONTRACTS.get(path))


def _assert_order_contract(path: str, body: Any) -> None:
    """The raw payload must satisfy each contracted list's measure order.

    _canon_body neutralises ties, so THIS check is what reddens when a
    top-N query loses or reverses its ORDER BY.
    """
    for key, field in (_ORDER_CONTRACTS.get(path) or {}).items():
        values = [row[field] for row in (body.get(key) or [])]
        bad = [(a, b) for a, b in zip(values, values[1:]) if a < b]
        assert not bad, (
            f"{path}:{key} is not sorted {field} DESC as its query "
            f"orders it: {bad[:3]}")


def _user_paths(sid: str) -> list[str]:
    return [
        "/api/me",
        "/api/dashboard?range=all",
        "/api/dashboard?range=1d",
        f"/api/dashboard?range=all&project={_PROJECT}&model={_MODEL}",
        "/api/cache?range=all",
        f"/api/cache?range=all&project={_PROJECT}",
        "/api/sessions",
        f"/api/sessions?project={_PROJECT}",
        f"/api/sessions/{sid}",
        "/api/sessions/no-such-session",
        "/api/projects?range=all",
        "/api/models",
        "/api/tool-usage?range=all",
        "/api/tool-usage?range=1d",
        f"/api/tool-usage?range=all&project={_PROJECT}&model={_MODEL}",
        "/api/tool-error-rate?range=all",
        "/api/tool-error-rate?range=1d",
        "/api/activity-heatmap?range=all",
        "/api/reply-latency?range=all",
        "/api/reply-latency?range=7d",
        "/api/cost-by-context?range=all",
        "/api/cost-by-context?range=1d",
        "/api/cost-by-agent?range=all",
        "/api/cost-by-agent?range=1d",
        "/api/context-growth/agg?range=all",
        "/api/context-growth/traces?range=all",
        f"/api/context-growth/session/{sid}",
        "/api/context-growth/session/no-such-session",
    ]


def _guest_paths(sid: str) -> list[str]:
    """The routes with a guest-specific strip or gate."""
    return [
        "/api/dashboard?range=all",
        "/api/cache?range=all",
        "/api/reply-latency?range=all",
        f"/api/context-growth/session/{sid}",
        "/api/sessions",
    ]


@pytest.fixture(name="parity_clients", scope="module")
def _parity_clients_fixture():
    """Fresh DB + mini-mirror ingest, with a user and a guest client.

    Every db.viz_conn session is pinned to TimeZone UTC for the
    fixture's life: responses render timestamptz through psycopg's
    session offset, so capture and comparison produce identical bytes
    on any runner (CI's postgres defaults to UTC, a dev cluster to its
    system zone)."""
    mp = pytest.MonkeyPatch()
    test_db = scratch_db.create_database("dims_parity")
    mp.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    src = Path(mini_mirror.MIRROR)
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"dims-parity-{os.getpid()}"
    shutil.copytree(src, tmp / "r2")
    mp.setenv("CLAUDIT_R2_ENDPOINT", f"file://{tmp / 'r2'}/")
    db.reset_viz_pool()
    original_viz_conn = db.viz_conn

    @contextmanager
    def utc_conn():
        with original_viz_conn() as conn:
            conn.execute("SET TimeZone = 'UTC'")
            yield conn

    db.viz_conn = utc_conn
    ingest.run_ingest(trigger="manual")

    with psycopg.connect(f"postgresql:///{test_db}") as conn:
        row = conn.execute(
            "SELECT session_id FROM files WHERE is_main "
            "ORDER BY session_id LIMIT 1"
        ).fetchone()
    assert row is not None, "mini mirror must have a main session"

    user_app = FastAPI()
    user_app.include_router(api.router)

    guest_app = FastAPI()

    @guest_app.middleware("http")
    async def _guest_flag(request: Request, call_next):
        request.state.is_guest = True
        return await call_next(request)

    guest_app.include_router(api.router)

    with TestClient(user_app) as user, TestClient(guest_app) as guest:
        yield user, guest, row[0]

    db.viz_conn = original_viz_conn
    db.reset_viz_pool()
    shutil.rmtree(tmp, ignore_errors=True)
    scratch_db.drop_database(test_db)


def _collect(client: TestClient, paths: list[str]) -> dict:
    out = {}
    for path in paths:
        resp = client.get(path)
        raw = resp.json()
        _assert_order_contract(path, raw)
        out[path] = {"status": resp.status_code,
                     "body": _canon_body(path, raw)}
    return out


@pytest.mark.db
def test_api_responses_match_the_precapture(parity_clients):
    """Byte parity (modulo never-specified ordering) with the pre-
    capture, guest strips and public file_key included."""
    user, guest, sid = parity_clients
    current = {
        "user": _collect(user, _user_paths(sid)),
        "guest": _collect(guest, _guest_paths(sid)),
    }
    if os.environ.get(CAPTURE_ENV):
        GOLDEN.write_text(
            json.dumps(current, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        pytest.skip(f"golden capture rewritten at {GOLDEN}")
    assert GOLDEN.is_file(), (
        f"missing {GOLDEN}; capture once with {CAPTURE_ENV}=1 on the "
        "pre-migration code"
    )
    # The golden is canon'd too: its raw BASE-era rates collapse to the
    # same sentinel current rates do, keeping the file's captured bytes
    # (its provenance) untouched while the test stays rate-independent.
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for group in golden.values():
        for path, entry in group.items():
            entry["body"] = _canon_body(path, entry["body"])
    assert current == golden
