"""Canonical winners match the legacy rule and use file_key text order."""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from backend import db, ingest, ingest_rollup_state, uuid_split
from tests import dims_seed, mini_mirror, scratch_db


_UNATTRIBUTED = ("", "unknown", "(unknown)", "<synthetic>")


def _old_rule_winners(rows: list[dict], key_fields: tuple[str, ...]) -> set:
    """The legacy winner rule, in Python, over rows carrying file_key.

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
