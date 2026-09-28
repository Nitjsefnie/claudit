"""Run-local dirty keys and crash-safe derived-state fingerprinting."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg

from backend import constants, db


@dataclass
class Scope:
    """The stored contributions a run may need to replace."""

    full: bool
    fingerprint: str
    dirty_limit: float
    dirty_files: set[str] = field(default_factory=set)
    dirty_hours: set[tuple[str, datetime | None]] = field(default_factory=set)
    affected_uuids: set[str] = field(default_factory=set)
    affected_tool_use_ids: set[str] = field(default_factory=set)
    latency_null: bool = False
    reason: str = "incremental"
    rollups_full: bool | None = None

    def promote_full(self, reason: str) -> None:
        self.full = True
        self.reason = reason

    def start_rollups(self) -> None:
        """Record which scope the hour-keyed rollups are about to apply."""
        self.rollups_full = self.full

    @staticmethod
    def _normalize_hour(hour: datetime | None) -> datetime | None:
        """Represent a dirty hour by its instant, preserving repeated hours."""
        return hour.astimezone(timezone.utc) if hour is not None else None

    def add_hour(self, project_id: str, hour: datetime | None) -> None:
        self.dirty_hours.add((project_id, self._normalize_hour(hour)))

    def add_contribution_hour(self, contribution: Contribution,
                              project_id: str,
                              hour: datetime | None) -> None:
        """Keep captured hours distinct by instant before set insertion."""
        contribution.hours.add((project_id, self._normalize_hour(hour)))

    def add_hours(self, project_id: str,
                  hours: Iterable[datetime | None]) -> None:
        for hour in hours:
            self.add_hour(project_id, hour)

    def add_contributions(self,
                          contributions: dict[str, Contribution],
                          file_keys: set[str]) -> None:
        self.dirty_files.update(file_keys)
        for file_key in file_keys:
            contribution = contributions.get(file_key)
            if contribution is None:
                continue
            for project_id, hour in contribution.hours:
                self.add_hour(project_id, hour)
            self.affected_uuids.update(contribution.uuids)
            self.affected_tool_use_ids.update(contribution.tool_use_ids)
            self.latency_null = self.latency_null or contribution.latency_null

    def check_dirty_threshold(self, file_keys: set[str]) -> None:
        if not self.full and len(file_keys) > self.dirty_limit:
            self.promote_full("dirty-file threshold")

    def check_latency_null(self) -> None:
        if not self.full and self.latency_null:
            self.promote_full("latency row with NULL timestamp")


@dataclass
class Contribution:
    """Keys and identity values contributed by one stored file."""

    hours: set[tuple[str, datetime | None]] = field(default_factory=set)
    uuids: set[str] = field(default_factory=set)
    tool_use_ids: set[str] = field(default_factory=set)
    latency_null: bool = False


_CURRENT_SCOPE: ContextVar[Scope | None] = ContextVar(
    "claudit_ingest_scope", default=None)
_CURRENT_TOKEN: ContextVar[Token[Scope | None] | None] = ContextVar(
    "claudit_ingest_scope_token", default=None)


def current_scope() -> Scope | None:
    """Return the active run's scope, if an ingest is in progress."""
    return _CURRENT_SCOPE.get()


def finish_scope() -> Scope | None:
    """Restore the scope context and return its final value for timing."""
    token = _CURRENT_TOKEN.get()
    scope = _CURRENT_SCOPE.get()
    if token is not None:
        _CURRENT_SCOPE.reset(token)
        _CURRENT_TOKEN.set(None)
    return scope


def _fingerprint(conn: psycopg.Connection) -> str:
    """Hash every code and operator input that changes derived rows."""
    suppressed = conn.execute(
        "SELECT pattern FROM suppressed_models ORDER BY pattern"
    ).fetchall()
    aliases = conn.execute(
        "SELECT pattern, project_id FROM project_aliases "
        "ORDER BY pattern, project_id"
    ).fetchall()
    value = {
        "derived": constants.DERIVED_STATE_VERSION,
        "parser": constants.PARSER_VERSION,
        "pricing": constants.PRICING_VERSION,
        "latency_buckets": constants.LATENCY_BUCKETS,
        "ctx_bucket_width": constants.CTX_BUCKET_WIDTH,
        "ctx_bucket_max": constants.CTX_BUCKET_MAX,
        "default_agent_type": constants.DEFAULT_AGENT_TYPE,
        "suppressed_models": [row[0] for row in suppressed],
        "project_aliases": [list(row) for row in aliases],
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def begin_scope() -> Scope:
    """Decide known full-rebuild triggers and commit an incomplete marker."""
    with db.viz_conn() as conn:
        fingerprint = _fingerprint(conn)
        state = conn.execute(
            "SELECT fingerprint, complete, last_full_at "
            "FROM ingest_derived_state WHERE singleton"
        ).fetchone()
        stored_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()
        assert stored_count is not None
        stored_files = stored_count[0]
        now = datetime.now(timezone.utc)
        reason = "incremental"
        full = False
        if state is None:
            reason = "missing fingerprint"
        elif state is not None and not state[1]:
            reason = "incomplete previous run"
        elif state is not None and state[0] != fingerprint:
            reason = "derived fingerprint changed"
        elif (state is not None
              and (state[2] is None
                   or state[2] < now - timedelta(hours=24))):
            reason = "full rebuild older than 24 hours"
        full = reason != "incremental"
        conn.execute(
            "INSERT INTO ingest_derived_state "
            "(singleton, fingerprint, complete, last_full_at) "
            "VALUES (TRUE, 'incomplete', FALSE, NULL) "
            "ON CONFLICT (singleton) DO UPDATE "
            "SET complete = FALSE",
        )
        conn.commit()

    scope = Scope(
        full=full,
        fingerprint=fingerprint,
        dirty_limit=min(2000, stored_files * 0.20),
        reason=reason,
    )
    previous = _CURRENT_TOKEN.get()
    if previous is not None:
        _CURRENT_SCOPE.reset(previous)
    _CURRENT_TOKEN.set(_CURRENT_SCOPE.set(scope))
    return scope


def mark_complete() -> None:
    """Complete only a run whose rollups used its final scope."""
    scope = current_scope()
    if (scope is None or scope.rollups_full is None
            or (scope.full and not scope.rollups_full)):
        return
    with db.viz_conn() as conn:
        conn.execute(
            "UPDATE ingest_derived_state "
            "SET fingerprint = %s, complete = TRUE, "
            "last_full_at = CASE WHEN %s THEN now() ELSE last_full_at END "
            "WHERE singleton",
            (scope.fingerprint, scope.rollups_full),
        )
        conn.commit()


def capture_contributions(scope: Scope, conn: psycopg.Connection,
                          file_keys: set[str]) -> dict[str, Contribution]:
    """Read records and tool-use contributions for files in two bulk queries."""
    contributions = {file_key: Contribution() for file_key in file_keys}
    if not file_keys:
        return contributions
    for file_key, project_id, hour, record_uuid, latency, ts in conn.execute(
        """
        SELECT r.file_key, f.project_id, date_trunc('hour', r.ts), r.uuid,
               r.reply_latency_s, r.ts
          FROM records r JOIN files f ON f.file_key = r.file_key
         WHERE r.file_key = ANY(%s)
        """, (list(file_keys),),
    ).fetchall():
        contribution = contributions[file_key]
        scope.add_contribution_hour(contribution, project_id, hour)
        if record_uuid is not None:
            contribution.uuids.add(record_uuid)
        if latency is not None and ts is None:
            contribution.latency_null = True
    for file_key, project_id, hour, tool_use_id in conn.execute(
        """
        SELECT tu.file_key, f.project_id, date_trunc('hour', tu.ts),
               tu.tool_use_id
          FROM tool_uses tu JOIN files f ON f.file_key = tu.file_key
         WHERE tu.file_key = ANY(%s)
        """, (list(file_keys),),
    ).fetchall():
        contribution = contributions[file_key]
        scope.add_contribution_hour(contribution, project_id, hour)
        if tool_use_id is not None:
            contribution.tool_use_ids.add(tool_use_id)
    return contributions


def capture_and_add(scope: Scope, conn: psycopg.Connection,
                    file_keys: set[str]) -> dict[str, Contribution]:
    """Capture a live contribution and add it to the run's dirty scope."""
    contributions = capture_contributions(scope, conn, file_keys)
    scope.add_contributions(contributions, file_keys)
    return contributions


def add_alias_move_contributions(
        scope: Scope, moved_files: dict[str, tuple[str, str]]) -> None:
    """Add post-fold target keys and source keys for every moved file."""
    moved_keys = set(moved_files)
    with db.viz_conn() as conn:
        contributions = capture_and_add(
            scope, conn, scope.dirty_files | moved_keys)
    for file_key, (source, _) in moved_files.items():
        contribution = contributions.get(file_key)
        if contribution is not None:
            scope.add_hours(
                source, (hour for _, hour in contribution.hours))
    scope.check_dirty_threshold(scope.dirty_files)
