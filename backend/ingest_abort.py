"""The ingest run's cooperative-abort signal."""
from __future__ import annotations


class IngestAborted(Exception):
    """Cooperative shutdown signal raised between bounded ingest steps.

    Keep this leaf below ingest so fetch, reprice and lock guards can
    raise it without an import cycle. Re-export through ingest and
    ingest_reprice for their callers. run_ingest_locked handles abort
    separately from fatal errors: close as aborted and skip rebuild,
    invalidation and broadcast, so incomplete work announces no change."""
