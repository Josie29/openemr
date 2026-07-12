#!/usr/bin/env bash
#
# run.sh — end-to-end load test for the deployed Co-Pilot /chat endpoint (JOS-18).
#
# Flow:
#   1. Bootstrap a local venv with locust (isolated from the agent's venv).
#   2. Mint one 1-hour SMART access token (reused for the whole run).
#   3. Smoke test: a single /chat request must return HTTP 200 before we spend
#      on a wide run — fails fast on a bad token / down service.
#   4. Load levels: 10 then 50 concurrent users, each for $DURATION, writing
#      Locust CSVs (which carry p50/p95/p99 latency + failure counts) to results/.
#
# Overridable via env:
#   LEVELS="10 50"   SPAWN_RATE=10   DURATION=2m   CHAT_BASE_URL=...   CHAT_PATIENT_ID=...
#
# Usage:  agent/loadtest/run.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LEVELS="${LEVELS:-10 50}"
SPAWN_RATE="${SPAWN_RATE:-10}"
DURATION="${DURATION:-2m}"
BASE_URL="${CHAT_BASE_URL:-https://copilot-agent-production-eb24.up.railway.app}"

# Timestamped results dir. Static (no Date.now() concerns here — plain bash date).
RUN_TS="$(date +%Y%m%d-%H%M%S)"
RESULTS_DIR="$SCRIPT_DIR/results/$RUN_TS"
mkdir -p "$RESULTS_DIR"

# --- 1. venv with locust (isolated) ----------------------------------------------
VENV="$SCRIPT_DIR/.venv"
if [[ ! -x "$VENV/bin/locust" ]]; then
  echo "-- bootstrapping locust venv at $VENV"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet locust
fi
PY="$VENV/bin/python"
LOCUST="$VENV/bin/locust"

# --- 2. mint the access token ----------------------------------------------------
echo "-- minting access token"
CHAT_TOKEN="$("$PY" "$SCRIPT_DIR/mint_token.py")"
export CHAT_TOKEN
export CHAT_BASE_URL="$BASE_URL"
[[ -n "${CHAT_PATIENT_ID:-}" ]] && export CHAT_PATIENT_ID

# --- 3. smoke test: one request must 200 -----------------------------------------
PATIENT_ID="${CHAT_PATIENT_ID:-a234013f-932b-434c-8f21-9edc54ff3892}"
echo "-- smoke test: single POST /chat against $BASE_URL"
smoke_code="$(curl -s -o "$RESULTS_DIR/smoke-response.json" -w '%{http_code}' \
  -X POST "$BASE_URL/chat" \
  -H "Authorization: Bearer $CHAT_TOKEN" \
  -H "Content-Type: application/json" \
  --data "{\"patient_id\":\"$PATIENT_ID\",\"message\":\"Give me a one-line clinical snapshot of this patient.\"}" \
  --max-time 120)"
if [[ "$smoke_code" != "200" ]]; then
  echo "error: smoke test returned HTTP $smoke_code (expected 200). Aborting before the wide run." >&2
  echo "       response body saved at $RESULTS_DIR/smoke-response.json" >&2
  exit 1
fi
echo "   smoke OK (HTTP 200)"

# --- 4. load levels --------------------------------------------------------------
for users in $LEVELS; do
  label="c${users}"
  echo "-- load level: $users concurrent users for $DURATION (spawn rate $SPAWN_RATE/s)"
  "$LOCUST" -f "$SCRIPT_DIR/locustfile.py" \
    --headless \
    --users "$users" \
    --spawn-rate "$SPAWN_RATE" \
    --run-time "$DURATION" \
    --host "$BASE_URL" \
    --csv "$RESULTS_DIR/$label" \
    --csv-full-history \
    --only-summary
done

echo
echo "Done. CSV results in $RESULTS_DIR"
echo "Percentiles + error rate: see ${RESULTS_DIR}/c*_stats.csv (p50/p95/p99 columns)."
echo "Cross-check latency + token cost per turn in Langfuse (session = conversation_id)."
