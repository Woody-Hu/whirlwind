#!/usr/bin/env bash
# provision-test-db.sh — closest wiring between the install scripts and the
# integration suite's default DSNs.
#
# WHY (the gap it closes): tests/integration/conftest.py defaults to
#   POSTGRES_DSN = postgresql://whirlwind:whirlwind@127.0.0.1:5432/whirlwind_test
# but install-postgres.sh only creates the database (as the postgres superuser),
# so after a bare install the default DSN fails auth and the suite skips honestly.
# This script provisions exactly what the conftest defaults expect: the
# `whirlwind` login role + `whirlwind_test` database owned by it. It also
# sanity-checks redis from its default URL.
#
# OS REQUIREMENTS (strict): Ubuntu/Debian PostgreSQL 16+ (pg_ctlcluster layout),
# local `redis-cli`. Root/sudo needed to become the `postgres` superuser. NOT for
# RHEL / macOS — those use different cluster tooling (see install-postgres.sh).
#
# Idempotent: rerunning is a no-op. Verify at the end: it prints conn-ok/hello.
set -euo pipefail

PG_ROLE="${WHIRLWIND_TEST_PG_ROLE:-whirlwind}"
PG_PASS="${WHIRLWIND_TEST_PG_PASS:-whirlwind}"
PG_DB="${WHIRLWIND_TEST_PG_DB:-whirlwind_test}"
PG_ADMIN="${WHIRLWIND_TEST_PG_ADMIN:-postgres}"

run_sql() { sudo -u "${PG_ADMIN}" psql -v ON_ERROR_STOP=1 -qAt -c "$1"; }

echo ">> provisioning postgres role='${PG_ROLE}' db='${PG_DB}' ..."

# 1. Login role (idempotent).
if [ "$(run_sql "SELECT 1 FROM pg_roles WHERE rolname='${PG_ROLE}'")" = "1" ]; then
  echo "   role '${PG_ROLE}' exists — ensuring password/creds"
  run_sql "ALTER ROLE ${PG_ROLE} WITH LOGIN PASSWORD '${PG_PASS}';" >/dev/null
else
  run_sql "CREATE ROLE ${PG_ROLE} WITH LOGIN PASSWORD '${PG_PASS}';" >/dev/null
  echo "   created role '${PG_ROLE}'"
fi

# 2. Database owned by the role (idempotent).
if [ "$(run_sql "SELECT 1 FROM pg_database WHERE datname='${PG_DB}'")" = "1" ]; then
  echo "   db '${PG_DB}' already exists — skipping create"
else
  sudo -u "${PG_ADMIN}" createdb -O "${PG_ROLE}" "${PG_DB}"
  echo "   created db '${PG_DB}' (owner ${PG_ROLE})"
fi

# 3. Guest DB needs the schema the store creates at init; grant temp/connection.
run_sql "GRANT CONNECT, TEMPORARY ON DATABASE ${PG_DB} TO ${PG_ROLE};" >/dev/null

echo ">> verifying postgres (password auth over TCP, like the suite does) ..."
PGPASSWORD="${PG_PASS}" psql -h 127.0.0.1 -U "${PG_ROLE}" -d "${PG_DB}" -tAc "SELECT 'pg-conn-ok';"

echo ">> verifying redis (default test URL redis://127.0.0.1:6379/15) ..."
if command -v redis-cli >/dev/null 2>&1; then
  redis-cli -n 15 ping || echo "note: redis not answering — install-redis.sh may be needed"
else
  echo "note: redis-cli not installed; skipped"
fi

echo "done. Run: uv run python scripts/run_tests.py tests/integration/test_storage.py -q"