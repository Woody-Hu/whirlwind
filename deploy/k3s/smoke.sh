#!/usr/bin/env bash
# Smoke test against a deployed whirlwind gateway (ADR-0005 D4): drive the same
# REST surface the integration suite uses — build echo image, create agent,
# open a session, run one turn to turn/end. Fails loudly if any step stalls.
#
# Usage:
#   ./smoke.sh [base-url]     # default http://127.0.0.1:30841 (k3s NodePort)
#
# The echo harness skips the LLM relay hop when the sandbox env carries no
# ECHO_LLM_URL, so no real API key is required for this smoke.

set -euo pipefail

BASE="${1:-http://127.0.0.1:30841}"
TIMEOUT="${SMOKE_TIMEOUT:-120}"

say()  { echo "[smoke] $*"; }
die()  { echo "[smoke] FAIL: $*" >&2; exit 1; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1"; }

# --- wait for the gateway -------------------------------------------------
say "waiting for $BASE/healthz ..."
for _ in $(seq 1 60); do
  if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS "$BASE/healthz" >/dev/null || die "gateway never became healthy"
say "gateway healthy"

# --- 1. build the echo image ----------------------------------------------
say "building echo image ..."
code=$(curl -s -o /tmp/smoke_img.json -w '%{http_code}' -X POST "$BASE/images/echo/build")
[[ "$code" == 200 || "$code" == 409 ]] || die "image build failed ($code): $(cat /tmp/smoke_img.json)"  # 409: already built (rerun)
say "echo image built"

# --- 2. create the agent ---------------------------------------------------
say "creating agent ..."
code=$(curl -s -o /tmp/smoke_agent.json -w '%{http_code}' -X POST "$BASE/agents" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"k3s-smoke-$$\",\"version\":{\"harness\":\"echo\",\"image_ref\":\"echo\",\"model_config_decl\":{\"provider\":\"deepseek-official\",\"model\":\"deepseek-chat\"}}}")
[[ "$code" == 200 ]] || die "agent create failed ($code): $(cat /tmp/smoke_agent.json)"
AGENT_ID=$(json "d['agent']['id']" < /tmp/smoke_agent.json)
say "agent $AGENT_ID created"

# --- 3. open a session ------------------------------------------------------
say "opening session ..."
code=$(curl -s -o /tmp/smoke_session.json -w '%{http_code}' -X POST "$BASE/sessions" \
  -H 'Content-Type: application/json' -d "{\"agent_id\":\"$AGENT_ID\"}")
[[ "$code" == 200 ]] || die "session create failed ($code): $(cat /tmp/smoke_session.json)"
SESSION_ID=$(json "d['id']" < /tmp/smoke_session.json)
say "session $SESSION_ID open"

# --- 4. run one turn to completion ------------------------------------------
say "sending turn ..."
code=$(curl -s -o /tmp/smoke_turn.json -w '%{http_code}' -X POST "$BASE/sessions/$SESSION_ID/turns" \
  -H 'Content-Type: application/json' -d '{"text":"k3s smoke"}')
[[ "$code" == 200 ]] || die "turn submit failed ($code): $(cat /tmp/smoke_turn.json)"

deadline=$((SECONDS + TIMEOUT))
while (( SECONDS < deadline )); do
  curl -fsS "$BASE/sessions/$SESSION_ID/events" -o /tmp/smoke_events.json || die "event poll failed"
  if grep -q '"turn/end"' /tmp/smoke_events.json; then
    say "turn/end observed after ${SECONDS}s"
    say "SMOKE PASSED (session $SESSION_ID)"
    exit 0
  fi
  sleep 1
done

die "turn never reached turn/end within ${TIMEOUT}s; last events: $(cat /tmp/smoke_events.json)"
