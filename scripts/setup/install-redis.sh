#!/usr/bin/env bash
# install-redis.sh — Redis server for whirlwind[redis] (KV/Locks backend).
#
# OS REQUIREMENTS (strict): Ubuntu/Debian (apt) or RHEL/Fedora (dnf) or
# macOS (Homebrew). Root/sudo for system package managers.
#
# What you get: a local redis on 127.0.0.1:6379. The whirlwind integration
# suite auto-detects reachability (tests/integration/conftest.py) and skips
# honestly when down.
#
# Post-install verification:
#   redis-cli ping                     # -> PONG
#   uv run python scripts/run_tests.py tests/integration -q
set -euo pipefail

ID="$(. /etc/os-release 2>/dev/null && echo "${ID:-}" || echo "")"

if command -v apt-get >/dev/null 2>&1; then
  echo ">> apt-based system (${ID:-debian-family})"
  sudo apt-get update -y
  sudo apt-get install -y redis-server
  sudo systemctl enable --now redis-server || sudo service redis-server start || true
elif command -v dnf >/dev/null 2>&1; then
  echo ">> dnf-based system (${ID:-rhel-family})"
  sudo dnf install -y redis
  sudo systemctl enable --now redis || true
elif command -v brew >/dev/null 2>&1; then
  echo ">> macOS (Homebrew)"
  brew install redis
  brew services start redis
else
  echo "error: no supported package manager found (apt/dnf/brew)" >&2
  exit 1
fi

echo ">> verifying ..."
redis-cli ping || echo "note: server not answering yet — check service status"
echo "done."
