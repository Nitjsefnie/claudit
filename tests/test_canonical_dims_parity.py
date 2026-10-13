"""SV-CANONICAL-FLAG parity across the issue #959 surrogate ids (R2).

The winner rule still orders by `(is_replay, attribution, file_key,
line_num[, idx])` over the TEXT `files.file_key` — never `file_id`,
which is first-seen order. Each test computes the expected winners with
a Python copy of the PRE-#959 rule and asserts the stored flags match
it, plus one trap case whose file_id order is the opposite of its
file_key order: a file_id-ordered implementation flips that case's
winner and fails here.
"""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from backend import db, ingest, ingest_rollup_state, uuid_split
from tests import dims_seed, mini_mirror, scratch_db

_UNATTRIBUTED = ("", "unknown", "(unknown)", "<synthetic>")


def _old_rule_winners(rows: list[dict], key_fields: tuple[str, ...]) -> set:
    """The pre-#959 winner rule, in Python, over rows carrying file_key.

    `rows` items need uuid/tool_use_id, model, is_replay and the key
    fields; a NULL identity is always canonical (never a duplicate).
    """
    groups: dict = {}
    always = set()
    for row in rows:
        identity = row.get("uuid", row.get("tool_use_id"))
        if identity is None:
            always.add(tuple(row[f] for f in key_fields))
            continue
        groups.setdefault(identity, []).append(row)
    winners = set(always)
    for members in groups.values():
        best = min(
            members,
            key=lambda r: (
                r.get("is_replay") is True,
                (r["model"] or "") in _UNATTRIBUTED,
                r["file_key"],
                r["line_num"],
                *[r[f] for f in key_fields if f == "idx"],
            ),
        )
        winners.add(tuple(best[f] for f in key_fields))
    return winners


@pytest.mark.db
def test_fixture_corpus_winners_match_the_old_text_rule(monkeypatch,
                                                        tmp_path):
    """fixtures/r2_mini ingested end to end: stored flags == the old rule."""
    import shutil  # pylint: disable=import-outside-toplevel

    name = scratch_db.create_database("canon_fixture")
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    mirror = tmp_path / "r2"
    shutil.copytree(mini_mirror.MIRROR, mirror)
    monkeypatch.setenv("CLAUDIT_R2_ENDPOINT", f"file://{mirror}/")
    db.reset_viz_pool()
    try:
        ingest.run_ingest(trigger="manual")
        with psycopg.connect(f"postgresql:///{name}") as conn:
            rec_rows = conn.execute(
                f"SELECT f.file_key, r.line_num, "
                f"{uuid_split.uuid_text_sql('r')} AS uuid, dm.value, "
                "r.is_replay, r.is_canonical "
                "FROM records r "
                "JOIN files f ON f.file_id = r.file_id "
                "JOIN dim_model dm ON dm.id = r.model_id"
            ).fetchall()
            records = [
                {"file_key": fk, "line_num": ln, "uuid": u, "model": m,
                 "is_replay": rep, "is_canonical": canon}
                for fk, ln, u, m, rep, canon in rec_rows
            ]
            tu_rows = conn.execute(
                "SELECT f.file_key, t.line_num, t.idx, t.tool_use_id, "
                "dm.value, t.is_replay, t.is_canonical "
                "FROM tool_uses t "
                "JOIN files f ON f.file_id = t.file_id "
                "LEFT JOIN dim_model dm ON dm.id = t.model_id"
            ).fetchall()
            tool_uses = [
                {"file_key": fk, "line_num": ln, "idx": idx,
                 "tool_use_id": tid, "model": m, "is_replay": rep,
                 "is_canonical": canon}
                for fk, ln, idx, tid, m, rep, canon in tu_rows
            ]

            expected_records = _old_rule_winners(records, ("file_key",
                                                           "line_num"))
            got_records = {
                (fk, ln) for fk, ln, _u, _m, _rep, canon in rec_rows
                if canon
            }
            assert got_records == expected_records

            expected_tools = _old_rule_winners(
                tool_uses, ("file_key", "line_num", "idx"))
            got_tools = {
                (fk, ln, idx) for fk, ln, idx, _t, _m, _r, canon in tu_rows
                if canon
            }
            assert got_tools == expected_tools
        # The corpus actually contains a cross-file duplicate, so the
        # comparison above compared real winners, not one per identity.
        dupes = [rows for rows in _groups(records, "uuid") if len(rows) > 1]
        assert dupes, "fixture corpus must carry a shared uuid"
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)


def _groups(rows: list[dict], field: str) -> list[list[dict]]:
    out: dict = {}
    for row in rows:
        if row[field] is not None:
            out.setdefault(row[field], []).append(row)
    return list(out.values())


@pytest.mark.db
def test_winner_order_is_file_key_text_not_file_id(monkeypatch):
    """The R2 trap: file_id order opposite to file_key order.

    `zzz…` is inserted first (the lower file_id); the old rule and the
    browser's arrival-order stand-in both rank `aaa…` first by text.
    A file_id-ordered tie-break flips both winners and fails here.
    """
    name = scratch_db.create_database("canon_order")
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        with db.viz_conn() as conn:
            conn.execute(
                "INSERT INTO projects (project_id, display_name, "
                "first_seen_at, last_seen_at) VALUES ('p', 'p', now(), now())"
            )
            now = datetime.now(timezone.utc)
            for key in ("bucket/p/zzz.jsonl", "bucket/p/aaa.jsonl"):
                conn.execute(
                    "INSERT INTO files (file_key, project_id, session_id, "
                    "is_main, r2_etag, r2_size_bytes, r2_last_modified, "
                    "parsed_at, parser_version) VALUES (%s, 'p', 's', TRUE, "
                    "'e', 1, %s, %s, '0')", (key, now, now),
                )
            ids = dict(conn.execute(
                "SELECT file_key, file_id FROM files").fetchall())
            assert ids["bucket/p/zzz.jsonl"] < ids["bucket/p/aaa.jsonl"], (
                "the trap needs insertion order opposite to text order")
            mid = dims_seed.dim_id(conn, "dim_model", "model-x")
            for key, line in (("bucket/p/zzz.jsonl", 1),
                              ("bucket/p/aaa.jsonl", 1)):
                conn.execute(
                    "INSERT INTO records (file_id, line_num, fallback, "
                    "model_id, ts) VALUES (%s, %s, 'shared-uuid', %s, %s)",
                    (ids[key], line, mid, now),
                )
            tid = dims_seed.dim_id(conn, "dim_tool_name", "Bash")
            mmid = dims_seed.dim_id(conn, "dim_model", "model-x")
            for key in ("bucket/p/zzz.jsonl", "bucket/p/aaa.jsonl"):
                conn.execute(
                    "INSERT INTO tool_uses (file_id, line_num, idx, "
                    "tool_name_id, model_id, tool_use_id, ts) "
                    "VALUES (%s, 1, 0, %s, %s, 'shared-call', %s)",
                    (ids[key], tid, mmid, now),
                )
            conn.commit()

        ingest_rollup_state.recompute_canonical()

        with db.viz_conn() as conn:
            winners = conn.execute(
                "SELECT f.file_key FROM records r "
                "JOIN files f ON f.file_id = r.file_id "
                "WHERE r.is_canonical"
            ).fetchall()
            assert winners == [("bucket/p/aaa.jsonl",)], winners
            tu_winners = conn.execute(
                "SELECT f.file_key FROM tool_uses t "
                "JOIN files f ON f.file_id = t.file_id "
                "WHERE t.is_canonical"
            ).fetchall()
            assert tu_winners == [("bucket/p/aaa.jsonl",)], tu_winners
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)


@pytest.mark.db
def test_replay_and_attribution_ranks_outrank_key_order(monkeypatch):
    """The full chain: replay rank and attribution rank still beat the
    key order, exactly as the old rule had them."""
    name = scratch_db.create_database("canon_ranks")
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        now = datetime.now(timezone.utc)
        # aaa: replayed copy, sorts first by text.
        # bbb: attributed original, sorts second.
        # ccc: unattributed original vs ddd's attributed copy.
        seeds = [
            # (file_key, line, uuid, model, is_replay)
            ("bucket/p/aaa.jsonl", 1, "u-replay", "model-a", True),
            ("bucket/p/bbb.jsonl", 1, "u-replay", "model-a", None),
            ("bucket/p/ccc.jsonl", 1, "u-attr", "unknown", None),
            ("bucket/p/ddd.jsonl", 1, "u-attr", "model-b", None),
        ]
        with db.viz_conn() as conn:
            conn.execute(
                "INSERT INTO projects (project_id, display_name, "
                "first_seen_at, last_seen_at) VALUES ('p', 'p', now(), now())"
            )
            for key in sorted({s[0] for s in seeds}):
                conn.execute(
                    "INSERT INTO files (file_key, project_id, session_id, "
                    "is_main, r2_etag, r2_size_bytes, r2_last_modified, "
                    "parsed_at, parser_version) VALUES (%s, 'p', 's', TRUE, "
                    "'e', 1, %s, %s, '0')", (key, now, now),
                )
            ids = dict(conn.execute(
                "SELECT file_key, file_id FROM files").fetchall())
            models = {"model-a": dims_seed.dim_id(conn, "dim_model", "model-a"),
                      "model-b": dims_seed.dim_id(conn, "dim_model", "model-b"),
                      "unknown": dims_seed.dim_id(conn, "dim_model", "unknown")}
            for key, line, uuid, model, replay in seeds:
                conn.execute(
                    "INSERT INTO records (file_id, line_num, fallback, "
                    "model_id, is_replay, ts) VALUES (%s, %s, %s, %s, %s, %s)",
                    (ids[key], line, uuid, models[model], replay, now),
                )
            conn.commit()

        ingest_rollup_state.recompute_canonical()

        with db.viz_conn() as conn:
            rows = conn.execute(
                f"SELECT f.file_key, {uuid_split.uuid_text_sql('r')} AS uuid, "
                "r.is_canonical FROM records r "
                "JOIN files f ON f.file_id = r.file_id"
            ).fetchall()
        winners = {u: fk for fk, u, canon in rows if canon}
        # An original beats a replay whatever the key order (issue #687).
        assert winners["u-replay"] == "bucket/p/bbb.jsonl"
        # An attributed copy beats an unattributed one (issue #529).
        assert winners["u-attr"] == "bucket/p/ddd.jsonl"
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)
