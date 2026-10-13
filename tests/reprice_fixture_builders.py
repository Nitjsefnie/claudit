"""Seed helpers for the stored-record repricing tests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypedDict

from backend import parser_version
from tests import dims_seed


SEED_MODEL = "claude-opus-4-7"
SEED_TS = datetime(2026, 5, 7, 10, 0, tzinfo=timezone.utc)
SEED_TOKENS = (1_000, 2_000, 3_000, 100, 250, 500)


class SeedInputs(TypedDict):
    fresh: int
    output: int
    eph5: int
    eph1h: int
    unsplit_create: int
    read: int


SEED_INPUTS: SeedInputs = {
    "fresh": 1_000,
    "output": 100,
    "eph5": 250,
    "eph1h": 500,
    "unsplit_create": 1_250,
    "read": 3_000,
}


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
         SEED_TS, SEED_TS, parser_version.PARSER_VERSION),
    )


def seed_record(c, file_key: str, line_num: int,
                record: SeedRecord | None = None) -> None:
    """Insert one fixed-tally row under its project and file parents.

    Since #959 the fact stores dim FK ids: model/provider resolve
    through dims_seed (NULL stays NULL — the FK columns are nullable),
    file_key resolves to the files row's file_id. The reprice-written
    columns (cost, fee, staleness markers) live on the records_price
    sidecar (ruling R4) and are inserted beside the records row.
    """
    record = record or SeedRecord()
    seed_parents(c, file_key)
    fid = dims_seed.file_id(c, file_key)
    c.execute(
        "INSERT INTO records (file_id, line_num, ts, model_id, fresh_tokens, "
        "cache_creation_tokens, cache_read_tokens, output_tokens, "
        "eph5_tokens, eph1h_tokens, web_search_requests, provider_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (fid, line_num, record.ts,
         dims_seed.dim_id(c, "dim_model", record.model), *SEED_TOKENS,
         record.web_search_requests,
         None if record.provider is None
         else dims_seed.dim_id(c, "dim_provider", record.provider)),
    )
    c.execute(
        "INSERT INTO records_price (file_id, line_num, cost_usd, "
        "request_fee_usd, pricing_version_id, rate_fingerprint_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (fid, line_num, record.cost_usd, record.request_fee_usd,
         None if record.pricing_version is None
         else dims_seed.dim_id(c, "dim_pricing_version",
                               record.pricing_version),
         None if record.rate_fingerprint is None
         else dims_seed.dim_id(c, "dim_rate_fingerprint",
                               record.rate_fingerprint)),
    )
