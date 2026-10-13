# Container image for the claudit service (issue #941).
#
# The image ships the code and the interpreter, nothing else: every setting
# arrives as environment at run time, the same variables the systemd unit
# hands the service through its EnvironmentFile. The command is the unit's
# ExecStart — same module, same port, same flags but the bind address (see
# the CMD below) — so the image runs the way examples/claudit.service runs
# the service.

# Tag-pinned (no digest): the repo has no docker-update machinery to keep a
# digest honest, so the tag is the pin the maintainers can see and bump.
FROM public.ecr.aws/docker/library/python:3.13-slim

# Logs reach the container runtime unbuffered (the unit's journal equivalent)
# and the tree stays free of .pyc written at run time.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /opt/claudit

# The requirements file first, on its own, so a code-only change reuses the
# install layer.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# The same unprivileged account the unit runs as, created after the pip
# layer and before any source copy: a code-only change then reuses this
# layer instead of re-running useradd and chown on every edit. The app
# writes nothing durable to disk — transcripts, aggregates and session
# state live in Postgres or in memory, and the export renderer makes its
# file with tempfile.mkstemp().
RUN useradd --system --user-group --home /opt/claudit \
        --shell /usr/sbin/nologin claudit \
    && chown -R claudit:claudit /opt/claudit

# Exactly what the service serves and reads: the app, the browser modules,
# the static assets, the root VERSION that constants.VERSION reads, and the
# scripts/plots/ module backend/api_export.py runs as its /api/export
# subprocess (resolved relative to backend/, so the in-image path
# /opt/claudit/scripts/plots/ccusage_plot_db.py must exist). The schema
# files (db.SCHEMA_PATHS) travel with backend/; there is no node/npm stage
# because React and Babel load from unpkg at run time. What does NOT ship:
# the render interpreter and its dependencies — the default
# CLAUDIT_EXPORT_PYTHON (/usr/bin/python3) is absent from python:slim and matplotlib
# is not in backend/requirements.txt, so in-container /api/export needs
# CLAUDIT_EXPORT_PYTHON pointed at an interpreter with the renderer's dependencies
# (e.g. a derived image); the rest of the service runs stock. --chown stamps
# the owner here rather than in a later chown layer, so the ownership of
# these files stays static with the copy itself.
COPY --chown=claudit:claudit VERSION VERSION
COPY --chown=claudit:claudit backend/ backend/
COPY --chown=claudit:claudit src/ src/
COPY --chown=claudit:claudit public/ public/
COPY --chown=claudit:claudit scripts/plots/ scripts/plots/

USER claudit

# The unit's port.
EXPOSE 8000

# python:slim ships no curl, so the probe is stdlib. urlopen raises on a
# 4xx/5xx but FOLLOWS a 3xx redirect, so the probe installs a handler that
# refuses to follow one and then asserts an explicit 2xx on the response it
# got: a redirect, a 4xx, or the 503 /health answers while the database is
# unreachable all exit non-zero and fail the check. The probe's 4 s socket
# timeout sits under HEALTHCHECK's 5 s --timeout, so a hung probe fails the
# check instead of being killed by it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; H=type('H',(u.HTTPRedirectHandler,),{'redirect_request':lambda self,*a,**k:None}); r=u.build_opener(H).open('http://127.0.0.1:8000/health',timeout=4); r.read(); assert 200<=r.status<300, r.status"]

# The unit's ExecStart, verbatim apart from the bind address: a container
# listening on 127.0.0.1 is unreachable through a published port, and every
# other flag (--proxy-headers, --forwarded-allow-ips, the graceful-shutdown
# budget) is what the unit passes.
CMD ["python", "-m", "uvicorn", "backend.app:app", \
    "--host", "0.0.0.0", "--port", "8000", \
    "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", \
    "--timeout-graceful-shutdown", "5"]
