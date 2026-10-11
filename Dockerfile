# Container image for the claudit service (issue #941).
#
# The image ships the code and the interpreter, nothing else: every setting
# arrives as environment at run time, the same variables the systemd unit
# hands the service through its EnvironmentFile. The command is the unit's
# ExecStart — same module, same port, same flags — so the image runs the way
# examples/claudit.service runs the service.

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

# Exactly what the service serves and reads: the app, the browser modules,
# the static assets, and the root VERSION that constants.VERSION reads. The
# schema files (db.SCHEMA_PATHS) travel with backend/; there is no node/npm
# stage because React and Babel load from unpkg at run time.
COPY VERSION VERSION
COPY backend/ backend/
COPY src/ src/
COPY public/ public/

# The same unprivileged account the unit runs as. The app writes nothing
# durable to disk — transcripts, aggregates and session state live in
# Postgres or in memory, and the export renderer makes its file with
# tempfile.mkstemp().
RUN useradd --system --user-group --home /opt/claudit \
        --shell /usr/sbin/nologin claudit \
    && chown -R claudit:claudit /opt/claudit
USER claudit

# The unit's port.
EXPOSE 8000

# python:slim ships no curl, so the probe is stdlib. urlopen raises on a
# non-2xx status (/health answers 503 while the database is unreachable),
# and the probe's 4 s socket timeout sits under HEALTHCHECK's 5 s --timeout,
# so a hung probe fails the check instead of being killed by it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).read()"]

# The unit's ExecStart, verbatim apart from the bind address: a container
# listening on 127.0.0.1 is unreachable through a published port, and every
# other flag (--proxy-headers, --forwarded-allow-ips, the graceful-shutdown
# budget) is what the unit passes.
CMD ["python", "-m", "uvicorn", "backend.app:app", \
    "--host", "0.0.0.0", "--port", "8000", \
    "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", \
    "--timeout-graceful-shutdown", "5"]