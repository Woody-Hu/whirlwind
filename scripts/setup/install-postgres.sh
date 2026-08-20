#!/usr/bin/env bash
# install-postgres.sh — PostgreSQL server + dev client libs for whirlwind[postgres].
#
# OS REQUIREMENTS (strict): Ubuntu/Debian (apt) or RHEL/Fedora (dnf) or
# macOS (Homebrew). Root/sudo for system package managers.
#
# What you get: a local postgres accepting connections on 127.0.0.1:5432,
# plus libpq headers (asyncpg needs no build chain, but psql is handy for
# verification). The whirlwind integration suite auto-detects reachability
# (tests/integration/conftest.py) and skips honestly when down.
#
# Post-install (create the DB the tests expect):
#   sudo -u postgres createdb whirlwind_test 2>/dev/null || true
#   PG_DSN="postgresql://postgres:postgres@127.0.0.1:5432/whirlwind_test"
#   uv run python scripts/run_tests.py tests/integration -q
set -euo pipefail

ID="$(. /etc/os-release 2>/dev/null && echo "${ID:-}" || echo "")"

if command -v apt-get >/dev/null 2>&1; then
  echo ">> apt-based system (${ID:-debian-family})"
  sudo apt-get update -y
  sudo apt-get install -y postgresql postgresql-client libpq-dev
elif command -v dnf >/dev/null 2>&1; then
  echo ">> dnf-based system (${ID:-rhel-family})"
  sudo dnf install -y postgresql-server postgresql libpq-devel
  sudo postgresql-setup --initdb || true
  sudo systemctl enable --now postgresql || sudo systemctl start postgresql || true
elif command -v brew >/dev/null 2>&1; then
  echo ">> macOS (Homebrew)"
  brew install postgresql@16
  brew services start postgresql@16
else
  echo "error: no supported package manager found (apt/dnf/brew)" >&2
  exit 1
fi

echo ">> verifying ..."
if command -v pg_isready >/dev/null 2>&1; then
  pg_isready || echo "note: server not answering yet — check service status"
fi
echo "done."
