# Project Foundry API image. Multi-stage, non-root, no build tools at runtime.

FROM python:3.12-slim AS build

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip && \
    pip wheel --wheel-dir /wheels ".[server,http,postgres]"


FROM python:3.12-slim

# Run as an unprivileged user; the app needs no filesystem writes besides /tmp.
RUN useradd --create-home --shell /usr/sbin/nologin foundry

# Install with the same extras the wheels were built for - the runtime needs
# uvicorn (server), httpx (http) and psycopg2 (postgres) on PATH, not just the
# base package, or the uvicorn CMD below fails with "executable not found".
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels \
        "project-foundry[server,http,postgres]" && \
    rm -rf /wheels

# Ship the Alembic config + migration scripts so the entrypoint can own the
# schema (`alembic upgrade head`) on Postgres. The wheel install above carries
# only the package, not these repo-root files. /app is where the entrypoint
# runs alembic from (script_location resolves relative to the working dir).
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY docker-entrypoint.sh /usr/local/bin/foundry-entrypoint
RUN chmod +x /usr/local/bin/foundry-entrypoint

USER foundry
WORKDIR /home/foundry

# Configuration is environment-first; mount foundry.yaml and set FOUNDRY_CONFIG
# for the behavioural knobs. Secrets only ever come from the environment.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

# The entrypoint guards FOUNDRY_CONFIG and runs migrations for non-SQLite URLs,
# then hands off to this CMD.
ENTRYPOINT ["foundry-entrypoint"]
CMD ["uvicorn", "foundry.api.app:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
