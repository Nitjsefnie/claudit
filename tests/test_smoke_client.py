"""The smoke client presents the same origin a browser page would (#136).

PR #132 made the auth middleware refuse a mutating request whose Origin
or Referer does not match Host, so a bare urllib POST gets 403 and the
smoke gate goes red. A browser form POST always carries Origin — the
defect was in the smoke client, not the server. These tests boot a real
local HTTP server and read the headers off the wire, so the assertion is
about the request the server receives, not about what the client intends.
"""
from __future__ import annotations

import http.server
import importlib.util
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load():
    """Import scripts/ci/smoke.py by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library. (Same
    loader as test_smoke_cleanup; kept local so this file stands alone.)
    """
    path = REPO_ROOT / "scripts" / "ci" / "smoke.py"
    spec = importlib.util.spec_from_file_location("ci_smoke_client", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_smoke_client"] = module
    spec.loader.exec_module(module)
    return module


ci_smoke = _load()


def _probe_handler(captured: dict):
    """A handler class bound to this test's capture dict."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            captured.update({
                "origin": self.headers.get("Origin"),
                "host": self.headers.get("Host", ""),
            })
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def log_message(self, fmt, *args):
            pass

    return _Handler


def test_post_sends_origin_matching_host():
    captured: dict = {}
    probe = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _probe_handler(captured)
    )
    with probe:
        port = probe.server_address[1]
        thread = threading.Thread(target=probe.serve_forever, daemon=True)
        thread.start()
        try:
            status, _ = ci_smoke.post_no_redirect(
                f"http://127.0.0.1:{port}/login/guest"
            )
        finally:
            probe.shutdown()
            thread.join()

    assert status == 303
    # The contract the auth middleware enforces (backend/session.py):
    # Origin's netloc must equal Host. Assert the equality the server
    # applies, not merely that a header exists.
    assert captured["origin"] is not None
    assert urlparse(captured["origin"]).netloc == captured["host"]
    assert captured["origin"] == f"http://127.0.0.1:{port}"
    assert captured["host"] == f"127.0.0.1:{port}"
