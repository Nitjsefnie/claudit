"""response_sizes from response_size_rollup vs the live pass."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import floor

from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture as ingest_fresh_db_fixture,
)
from backend import db, ingest
from backend.api_dashboard import _dashboard_sources, _response_size_source
from backend.constants import LATENCY_BUCKETS
from backend.dashboard_queries import _dashboard_queries
from backend.timing import Phases
from tests import dims_seed

UTC = timezone.utc
_START = datetime(2026, 3, 1, 5, 17, tzinfo=UTC)
# Every stored width's first bucket holds rows from before it.
_SINCE = _START + timedelta(minutes=97 * 19 + 10)
_MODELS = ("size-probe-alpha", "size-probe-beta")


def _seed() -> None:
    """Two projects x two models over four days, plus excluded rows."""
    with db.viz_conn() as conn:
        files = {}
        for project in ("size-pA", "size-pB"):
            dims_seed.seed_project(conn, project)
            key = f"{project}/s/{project}.jsonl"
            conn.execute(
                "INSERT INTO files (file_key, project_id, session_id, "
                "is_main, r2_etag, r2_size_bytes, r2_last_modified, "
                "parsed_at, parser_version) VALUES (%s, %s, 's', TRUE, "
                "'e', 1, now(), now(), 'test')", (key, project))
            files[project] = dims_seed.file_id(conn, key)
        models = [dims_seed.dim_id(conn, "dim_model", m) for m in _MODELS]
        rows = []
        for i in range(60):
            ts = _START + timedelta(minutes=97 * i)
            fid = files["size-pA" if i % 2 else "size-pB"]
            for offset, chars in ((0, (i * 37) % 500 + 1), (7, i * 11 + 3)):
                rows.append((fid, len(rows) + 1, ts + timedelta(minutes=offset),
                             models[1 if i % 3 == 0 else 0], chars, True))
        extreme, at = 999_999, _START + timedelta(minutes=97 * 24)
        rows += [(files["size-pB"], 9001, at, models[1], extreme, False),
                 (files["size-pB"], 9002, at, models[1], 0, True),
                 (files["size-pB"], 9003, None, models[1], extreme, True)]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO records (file_id, line_num, fallback, ts, "
                "model_id, output_tokens, text_chars, is_canonical) "
                "VALUES (%s, %s, 'size-' || %s::text, %s, %s, 1, %s, %s)",
                [(f, n, n, ts, m, c, k) for f, n, ts, m, c, k in rows])
        conn.commit()


def _sizes(bucket_s: int, since: datetime, project: str | None,
           model: str | None, use_rollup: bool | None = None) -> list[tuple]:
    src = _dashboard_sources(project, model, since, bucket_s)
    if use_rollup is not None:
        src.update(_response_size_source(project, model, since, bucket_s,
                                         use_rollup))
    with db.viz_conn() as conn:
        rows = _dashboard_queries(conn, Phases("test"), bucket_s, src)
    return [tuple(row) for row in rows["response_sizes"]]


def _floored(since: datetime, bucket_s: int) -> datetime:
    return datetime.fromtimestamp(
        floor(since.timestamp() / bucket_s) * bucket_s, UTC)


def test_rollup_serves_the_live_percentiles_at_every_stored_width(fresh_db):
    """The first bucket is served whole: live from the floored since."""
    _seed()
    ingest.rebuild_response_size_rollup()
    for bucket_s in LATENCY_BUCKETS:
        floored = _floored(_SINCE, bucket_s)
        assert floored < _SINCE
        per_project = {}
        for project in (None, "size-pA", "size-pB"):
            for model in (None, "alpha"):
                live = _sizes(bucket_s, floored, project, model, False)
                served = _sizes(bucket_s, _SINCE, project, model)
                assert served == live, (bucket_s, project, model)
                assert live, (bucket_s, project, model)
                per_project.setdefault(project, live)
        assert {m for _b, m, *_ in per_project[None]} == set(_MODELS)
        assert len({b for b, *_ in per_project[None]}) >= 2
        assert per_project["size-pA"] != per_project["size-pB"]
        assert _sizes(bucket_s, _SINCE, None, None, False) != per_project[None]


def test_dashboard_reads_the_rollup_only_at_stored_widths(fresh_db):
    """Live below the stored widths; the rollup, empty until rebuilt, at one."""
    _seed()
    assert _sizes(1800, _SINCE, None, None)
    assert not _sizes(3600, _SINCE, None, None)
    ingest.rebuild_response_size_rollup()
    assert _sizes(3600, _SINCE, None, None) == _sizes(
        3600, _floored(_SINCE, 3600), None, None, False)
