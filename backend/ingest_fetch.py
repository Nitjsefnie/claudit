"""Bounded object fetch and per-file parsing for the ingest pool."""
from __future__ import annotations

import logging
import time
import lzma
from collections.abc import Callable

from botocore.exceptions import BotoCoreError, ClientError

from backend import agent_sidecar, parse, r2

log = logging.getLogger("claudit.ingest")

TRANSIENT_FETCH_ERRORS = (OSError, BotoCoreError, ClientError)
CORRUPT_PAYLOAD_ERRORS = (lzma.LZMAError, EOFError)
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


def fetch_with_retry(key: str) -> bytes:
    """Retry transient object-store failures; never retry bad payloads."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return r2.get_object(key)
        except CORRUPT_PAYLOAD_ERRORS:
            raise
        except TRANSIENT_FETCH_ERRORS as exc:
            if is_missing(exc):
                raise VanishedObject(key) from exc
            if attempt == FETCH_ATTEMPTS:
                raise
            log.warning(
                "ingest: fetch of %s failed (attempt %d/%d), retrying",
                key, attempt, FETCH_ATTEMPTS,
            )
            time.sleep(FETCH_BACKOFF_S[attempt - 1])
        except Exception as exc:  # noqa: BLE001
            log.error("ingest: fatal fetch failure on %s", key)
            raise FatalFetchError(
                f"{type(exc).__name__} while fetching an object; "
                "details are in the server log") from exc
    raise AssertionError("unreachable")  # pragma: no cover


def fetch_and_parse(key: str, sidecar_key: str | None,
                    fetch: Callable[[str], bytes],
                    parse_file: Callable[[str, bytes], dict] | None = None
                    ) -> dict:
    """Fetch and parse one object without opening a database connection."""
    parsed = (parse.parse_file if parse_file is None else parse_file)(
        key, fetch(key))
    if sidecar_key is None or parsed["agent_type_in_band"]:
        return parsed
    try:
        sidecar = fetch(sidecar_key)
    except (VanishedObject, *CORRUPT_PAYLOAD_ERRORS):
        return parsed
    return agent_sidecar.apply_agent_sidecar(
        parsed, sidecar, r2.split_key(key)[1])
