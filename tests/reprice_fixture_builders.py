"""Seed helpers for the stored-record repricing tests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from backend import constants


SEED_MODEL = "claude-opus-4-7"
SEED_TS = datetime(2026, 5, 7, 10, 0, tzinfo=timezone.utc)
SEED_TOKENS = (1_000, 2_000, 3_000, 100, 250, 500)
SEED_INPUTS = {"fresh": 1_000, "output": 100, "eph5": 250,
               "eph1h": 500, "unsplit_create": 1_250, "read": 3_000}


@dataclass(frozen=True)
class SeedRecord:
    """Optional per-row overrides for the fixed-tally record seed."""
    pricing_version: str | None = None
    cost_usd: float = 0.5
    model: str = SEED_MODEL
    ts: datetime = SEED_TS
    provider: str | None = None
    request_fee_usd: float | None = None
    web_search_requests: int | None = None
    rate_fingerprint: str | None = None


def seed_parents(c, file_key: str) -> None:
    """Insert the project and file rows required by seeded records."""
    c.execute(
        "INSERT INTO projects (project_id, display_name, first_seen_at, "
        "last_seen_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (project_id) DO NOTHING",
        ("reprice-test", "reprice-test", SEED_TS, SEED_TS),
    )
    c.execute(
        "INSERT INTO files (file_key, project_id, session_id, is_main, "
        "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
        "parser_version) VALUES (%s, %s, %s, TRUE, %s, %s, %s, %s, %s) "
        "ON CONFLICT (file_key) DO NOTHING",
        (file_key, "reprice-test", "sess-seed", "seed-etag", 12,
         SEED_TS, SEED_TS, constants.PARSER_VERSION),
    )


def seed_record(c, file_key: str, line_num: int,
                record: SeedRecord | None = None) -> None:
    """Insert one fixed-tally row under its project and file parents."""
    record = record or SeedRecord()
    seed_parents(c, file_key)
    c.execute(
        "INSERT INTO records (file_key, line_num, ts, model, fresh_tokens, "
        "cache_creation_tokens, cache_read_tokens, output_tokens, "
        "eph5_tokens, eph1h_tokens, cost_usd, request_fee_usd, "
        "web_search_requests, provider, pricing_version, rate_fingerprint) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (file_key, line_num, record.ts, record.model, *SEED_TOKENS,
         record.cost_usd, record.request_fee_usd, record.web_search_requests,
         record.provider, record.pricing_version, record.rate_fingerprint),
    )
