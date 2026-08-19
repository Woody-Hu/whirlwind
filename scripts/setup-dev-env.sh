#!/usr/bin/env bash
# Linux dev/CI environment bootstrap (idempotent).
# Installs PostgreSQL + Redis, downloads gVisor runsc, clones the dsh harness
# repo, and syncs Python deps. Designed to be re-run after a sandbox reset —
# every step checks before doing work.
#
# Usage: bash scripts/setup-dev-env.sh [workspace-root]
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
export DEBIAN_FRONTEND=noninteractive

log() { echo "[setup] $*" >&2; }

# --- PostgreSQL + Redis ------------------------------------------------
if ! command -v psql >/dev/null 2>&1; then
    log "installing postgresql + redis-server"
    apt-get update -qq
    apt-get install -y -qq postgresql redis-server
fi
service postgresql start >/dev/null 2>&1 || true
service redis-server start >/dev/null 2>&1 || true
sleep 1

# Default dev credentials: postgres/whirlwind on localhost, db "whirlwind".
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='postgres'" | grep -q 1; then
    sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'whirlwind';"
fi
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='whirlwind'" | grep -q 1 \
    || sudo -u postgres createdb -O postgres whirlwind
log "postgres ready (postgresql://postgres:whirlwind@127.0.0.1:5432/whirlwind)"

redis-cli ping >/dev/null 2>&1 || { log "ERROR: redis not responding"; exit 1; }
log "redis ready (redis://127.0.0.1:6379/0)"

# --- gVisor runsc ------------------------------------------------------
if ! command -v runsc >/dev/null 2>&1; then
    log "downloading runsc (latest x86_64)"
    ARCH="$(uname -m)"
    case "$ARCH" in
        x86_64) GVARCH="x86_64" ;;
        aarch64) GVARCH="aarch64" ;;
        *) log "unsupported arch $ARCH for runsc"; exit 1 ;;
    esac
    curl -sL --max-time 600 -o /usr/local/bin/runsc \
        "https://storage.googleapis.com/gvisor/releases/release/latest/${GVARCH}/runsc"
    chmod +x /usr/local/bin/runsc
fi
log "runsc: $(runsc --version 2>&1 | head -1)"

# --- dsh harness checkout ----------------------------------------------
if [ ! -d "$ROOT/refs/deepseek-harness" ]; then
    log "cloning deepseek-harness into $ROOT/refs/"
    mkdir -p "$ROOT/refs"
    git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness \
        "$ROOT/refs/deepseek-harness"
fi
log "dsh checkout: $ROOT/refs/deepseek-harness"

# --- Python deps -------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
    log "uv sync --all-extras"
    (cd "$ROOT" && uv sync --all-extras)
else
    log "uv not found; skipping (install uv first)"
fi

log "environment ready"
