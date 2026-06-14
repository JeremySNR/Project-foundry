#!/bin/sh
# Container entrypoint for the Foundry API image.
#
# Two jobs, then it hands off to the CMD (uvicorn):
#
#   1. Tolerate a missing/non-file FOUNDRY_CONFIG. A bare `docker compose up`
#      can bind-mount a config path that doesn't exist yet; Docker then creates
#      a *directory* at that path, and Settings.load would crash trying to read
#      it. If FOUNDRY_CONFIG points at something that isn't a regular file we
#      warn and unset it, so the app falls back to built-in defaults + env vars
#      (a valid configuration) instead of crash-looping.
#
#   2. Own the schema on non-SQLite backends. Alembic is the single schema owner
#      for Postgres (see foundry.db.base.init_schema): we run `alembic upgrade
#      head` here, before the app starts, so a fresh database is created *and*
#      stamped with alembic_version. SQLite (the no-config default) has no
#      migration step and is bootstrapped by the app itself, so we skip it.
set -e

if [ -n "${FOUNDRY_CONFIG:-}" ] && [ ! -f "${FOUNDRY_CONFIG}" ]; then
    echo "foundry: FOUNDRY_CONFIG=${FOUNDRY_CONFIG} is not a readable file" \
         "(missing mount?); falling back to defaults + environment." >&2
    unset FOUNDRY_CONFIG
fi

# Treat an unset URL as the SQLite in-memory default (see db.base.make_engine).
db_url="${FOUNDRY_DATABASE_URL:-sqlite}"
case "${db_url}" in
    sqlite*)
        : # SQLite dev/test DB: the app's init_schema bootstraps it. No migrations.
        ;;
    *)
        echo "foundry: applying Alembic migrations to ${db_url%%:*} database..." >&2
        cd /app
        alembic upgrade head
        ;;
esac

exec "$@"
