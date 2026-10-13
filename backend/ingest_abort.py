"""The ingest run's cooperative-abort signal (issues #103, #374)."""
from __future__ import annotations


class IngestAborted(Exception):
    """Internal signal: the shutdown event was seen between bounded steps.

    Defined as a leaf below backend.ingest in the import graph, so every
    ingest module that needs it — the reprice pass, the fetch pool, the
    advisory-lock guard — raises it without importing ingest back, a
    cycle pylint refuses. backend.ingest_reprice re-exports it, so
    `ingest_reprice.IngestAborted` and `ingest.IngestAborted` resolve
    exactly as before. run_ingest_locked catches it SEPARATELY from the
    generic fatal path: the row is closed as aborted, but the rebuild,
    the cache invalidation and the ingest_done broadcast are all
    skipped — an aborted run must not tell clients data changed.
    """
