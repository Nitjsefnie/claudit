"""Per-item outcome pairing and failure booking for the ingest pools.

Split out of backend.ingest_fetch for the module-size baseline when the
blob cache (issue #684) joined the fetch unit. The pools and the listing
scan resolve callables over items the same way — each item pairs with
its result OR its exception, and a failed item is booked without
aborting the run — so the shared mechanics live here, leaf-ward of
`ingest`, which re-exports these names to keep its call surface.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

from botocore.exceptions import ClientError

log = logging.getLogger("claudit.ingest")

#: The retry shape of one object fetch: backoff sleeps between attempts.
#: Lives here beside the failure booking that reports it; fetch_with_retry
#: imports it back.
FETCH_BACKOFF_S = (0.5, 1.0)
FETCH_ATTEMPTS = len(FETCH_BACKOFF_S) + 1


class VanishedObject(Exception):
    """A listed transcript that disappeared before its fetch ran."""


class FatalFetchError(Exception):
    """A non-transient fetch failure that indicates a code defect."""


def is_missing(exc: BaseException) -> bool:
    """Whether a fetch error says the object no longer exists."""
    if isinstance(exc, FileNotFoundError):
        return True
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        return code in ("NoSuchKey", "404")
    return False


def record_failure(failed: list[tuple[str, str]], key: str,
                   exc: BaseException) -> None:
    """Book one failed object and retain its qualified key for triage.

    `_record_failure` logs the key for server-side investigation. The
    failure list feeds the admin-only response field; `ingest_runs.error`
    receives `failure_summary`'s count because /health is public.
    """
    failed.append((key, f"{type(exc).__name__}: {exc}"))
    log.warning(
        "ingest: %s failed after %d attempt(s): %s: %s",
        key, FETCH_ATTEMPTS, type(exc).__name__, exc,
    )


def resolve(items: list, call, workers: int) -> list[tuple]:
    """Run `call(item)` over `items`, pairing each with its result OR its
    exception instead of letting the first failure escape.

    Sequential when workers == 1, on a pool otherwise. Collecting with
    `[f.result() for f in as_completed(...)]` re-raised the worker's
    exception out of the collection step, which aborted the whole ingest
    AND discarded every already-fetched result alongside it. The two shapes
    have to behave identically, which is easiest to guarantee with one
    implementation.

    FatalFetchError is the one exception that still escapes: it means the
    fetch is broken rather than one object being unlucky, so it belongs to
    the run, not to the item.

    Returns [(item, result, None) | (item, None, exception)].
    """
    outcomes: list[tuple] = []
    if workers == 1:
        for item in items:
            try:
                outcomes.append((item, call(item), None))
            except FatalFetchError:
                raise
            except Exception as e:  # noqa: BLE001
                outcomes.append((item, None, e))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(call, item): item for item in items}
            for f in as_completed(futures):
                item = futures[f]
                try:
                    outcomes.append((item, f.result(), None))
                except FatalFetchError:
                    raise
                except Exception as e:  # noqa: BLE001
                    outcomes.append((item, None, e))
    return outcomes


def resolve_futures(futures: dict) -> Iterator[tuple]:
    """Iterate a prefabricated futures map, pairing each future's item
    with its result OR its exception — the same shape `resolve` returns,
    over a caller-owned executor whose lifetime spans chunks.

    FatalFetchError still escapes: it means the fetch path is broken, so
    it belongs to the run, not to the item. A dead parse child
    (BrokenProcessPool — OOM, kill) means the same and escapes too.
    """
    for f in as_completed(futures):
        item = futures[f]
        try:
            yield (item, f.result(), None)
        except (FatalFetchError, BrokenProcessPool):
            raise
        except Exception as e:  # noqa: BLE001
            yield (item, None, e)
