"""Serve source modules minified, with a bounded process-local transform cache."""
from __future__ import annotations

import hashlib
import logging
import os
from functools import lru_cache
from pathlib import Path

from anyio.to_thread import run_sync
from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import Scope

from backend.js_min import JsMinError, minify_js


log = logging.getLogger("claudit.source_files")
_SCRIPT_SUFFIXES = {".js", ".jsx"}


@lru_cache(maxsize=128)
def _minified(path: str, mtime_ns: int, size: int) -> tuple[bytes, str]:
    """Cache by source identity and stat, preserving raw line terminators."""
    del mtime_ns, size  # Both remain part of the cache key.
    try:
        body = minify_js(Path(path).read_bytes().decode("utf-8")).encode("utf-8")
    except JsMinError:
        log.exception("Cannot minify source module %s", path)
        raise
    return body, '"' + hashlib.sha256(body).hexdigest() + '"'


class MinifiedStaticFiles(StaticFiles):
    """Keep StaticFiles path checks and data serving; transform JS/JSX only."""

    def file_response(self, full_path: str | os.PathLike[str],
                      stat_result: os.stat_result, scope: Scope,
                      status_code: int = 200) -> Response:
        if Path(full_path).suffix in _SCRIPT_SUFFIXES:
            # Evaluate validators only after transforming the representation.
            return FileResponse(full_path, status_code=status_code, stat_result=stat_result)
        return super().file_response(full_path, stat_result, scope, status_code)

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if not isinstance(response, FileResponse) or Path(response.path).suffix not in _SCRIPT_SUFFIXES:
            return response
        source_stat = response.stat_result
        assert source_stat is not None
        body, etag = await run_sync(
            _minified, str(response.path), source_stat.st_mtime_ns, source_stat.st_size)
        headers = {
            "content-length": str(len(body)),
            "last-modified": response.headers["last-modified"],
            "etag": etag,
        }
        transformed = Response(
            body if scope["method"] != "HEAD" else b"",
            status_code=response.status_code,
            media_type=response.media_type, headers=headers,
        )
        if self.is_not_modified(transformed.headers, Headers(scope=scope)):
            return NotModifiedResponse(transformed.headers)
        return transformed
