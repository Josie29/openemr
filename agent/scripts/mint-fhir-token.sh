#!/usr/bin/env bash
#
# mint-fhir-token.sh — re-mint the prod SMART patient-scoped token pair and write
# it into the Bruno collection env(s).
#
# Why this exists: the delivered refresh token is single-use (rotates) and can be
# revoked, so it eventually dies and request 04 returns "401 Token has been revoked".
# When that happens, run this once to get back to green. See
# agent/api-collection/README.md for the token model.
#
# It runs OpenEMR's token CLI on the prod `openemr` service, feeding your OpenEMR
# username/password over stdin (nothing is echoed or stored), then auto-extracts the
# fresh access_token + refresh_token from the CLI's JSON output and writes them into
# prod.bru. No copy/paste.
#
# Usage:
#   agent/scripts/mint-fhir-token.sh              # update api-collection + fhir-substrate (if present)
#   agent/scripts/mint-fhir-token.sh --graded-only
#
# Override the identity via env vars to target a different client/patient:
#   CLIENT_ID=... PATIENT_ID=... agent/scripts/mint-fhir-token.sh

set -euo pipefail

# --- identity (defaults match the demo patient, Adrian Becker) --------------------
CLIENT_ID="${CLIENT_ID:-itdfnJA8SHPTnSpzCGTVDc4FkqaMIiqBwqvvgooYcQU}"
PATIENT_ID="${PATIENT_ID:-a234013f-932b-434c-8f21-9edc54ff3892}"

# Single source of truth for the scopes. patient/DocumentReference.read is REQUIRED
# for the clinical-note read (agent get_encounter_note / substrate request 06);
# offline_access is what makes the CLI emit a refresh token at all.
SCOPES="openid,fhirUser,launch,\
patient/Patient.read,\
patient/Condition.read,\
patient/MedicationRequest.read,\
patient/AllergyIntolerance.read,\
patient/Encounter.read,\
patient/DocumentReference.read,\
offline_access"

# --- resolve target env files -----------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
GRADED_ENV="$AGENT_DIR/api-collection/environments/prod.bru"
SUBSTRATE_ENV="$AGENT_DIR/fhir-substrate/environments/prod.bru"

targets=("$GRADED_ENV")
if [[ "${1:-}" != "--graded-only" && -f "$SUBSTRATE_ENV" ]]; then
  targets+=("$SUBSTRATE_ENV")
fi
for f in "${targets[@]}"; do
  if [[ ! -f "$f" ]]; then
    echo "error: env file not found: $f" >&2
    echo "       copy prod.example.bru -> prod.bru first (see the collection README)." >&2
    exit 1
  fi
done

# --- collect OpenEMR credentials (local, not echoed) ------------------------------
# Prefer env vars; otherwise prompt — but only if we actually have a TTY. Running
# this through a wrapper that pipes /dev/null to stdin (e.g. Claude Code's `!`)
# gives no TTY, so we fail with instructions instead of silently reading EOF.
OE_USER="${OE_USER:-admin}"
if [[ -z "${OE_PASS:-}" ]]; then
  if [[ -t 0 ]]; then
    read -r -p "OpenEMR username [admin]: " _u; OE_USER="${_u:-$OE_USER}"
    read -r -s -p "OpenEMR password: " OE_PASS; echo
  else
    {
      echo "error: no interactive terminal, and OE_PASS is not set."
      echo "  Run this directly in your own terminal (not via Claude Code's '!'):"
      echo "      agent/scripts/mint-fhir-token.sh"
      echo "  or pass the password via env (note: it lands in shell history):"
      echo "      OE_PASS='<admin-password>' agent/scripts/mint-fhir-token.sh"
    } >&2
    exit 1
  fi
fi
if [[ -z "$OE_PASS" ]]; then echo "error: password is required." >&2; exit 1; fi

# --- read oauth config from the graded env (for the self-verify step) -------------
gv() { grep -E "^ *$1:" "$GRADED_ENV" | sed -E "s/^ *$1: *//"; }
OAUTH_URL="$(gv oauth_url)"
CLIENT_SECRET="$(gv client_secret)"
if [[ -z "$OAUTH_URL" || -z "$CLIENT_SECRET" || "$CLIENT_SECRET" == "<CLIENT_SECRET>" ]]; then
  echo "error: oauth_url / client_secret missing from $GRADED_ENV — fill them in first." >&2
  exit 1
fi

# --- run the mint on prod, capture output to a private temp file ------------------
tmp="$(mktemp)"; chmod 600 "$tmp"
trap 'rm -f "$tmp"' EXIT

echo "Minting on prod (client ${CLIENT_ID:0:8}…, patient ${PATIENT_ID:0:8}…)…"

# SHELL_INTERACTIVE=1 keeps Symfony's prompts reading our piped answers even though
# stdin isn't a TTY; --no-ansi strips color codes.
REMOTE="cd /var/www/localhost/htdocs/openemr && SHELL_INTERACTIVE=1 \
php bin/console openemr-dev:api-generate-access-token --no-ansi \
--client-id=${CLIENT_ID} --patient=${PATIENT_ID} --scopes=${SCOPES}"

printf '%s\n%s\n' "$OE_USER" "$OE_PASS" \
  | railway ssh -s openemr "su -s /bin/sh apache -c '${REMOTE}'" >"$tmp" 2>&1 || true
unset OE_PASS

# --- extract the refresh token with a real JSON parser ----------------------------
# The CLI prints a Bearer Token JSON somewhere in decorated output. Stripping all
# whitespace rejoins any wrapped lines and drops SymfonyStyle's decoration spacing;
# the only curly-brace object left is the token JSON. grep-per-line was too fragile
# (it silently grabbed the wrong bytes and wrote a token that mapped to a revoked id).
extract() { # $1 = field name
  python3 - "$tmp" "$1" <<'PY'
import sys, re, json
raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
blob = re.sub(r"\s+", "", raw)               # rejoin wrapped lines; token values have no whitespace
m = re.search(r'\{[^{}]*"access_token"[^{}]*\}', blob)
if not m:
    sys.exit(0)
try:
    print(json.loads(m.group(0)).get(sys.argv[2], ""))
except Exception:
    pass
PY
}
MINT_RT="$(extract refresh_token)"
if [[ -z "$MINT_RT" ]]; then
  echo "error: could not parse a refresh token from the mint output. Fields seen:" >&2
  grep -oE '"[a-z_]+":' "$tmp" | sort -u | tr '\n' ' ' >&2; echo >&2
  echo "(need offline_access in the scopes; check for 'Invalid username or password' above.)" >&2
  exit 1
fi

# --- self-verify: prove the token via a real refresh grant, keep the rotated pair -
# We do NOT trust the parse. A refresh grant with a good token returns a fresh pair
# from clean JSON; that rotated pair is what we write (guaranteed working). If the
# grant fails, we write nothing and surface why.
echo "Verifying the minted token against the token endpoint…"
resp="$(curl -s -w '\n%{http_code}' -X POST "$OAUTH_URL" \
  --data-urlencode "grant_type=refresh_token" \
  --data-urlencode "refresh_token=$MINT_RT" \
  --data-urlencode "client_id=$CLIENT_ID" \
  --data-urlencode "client_secret=$CLIENT_SECRET")"
code="$(printf '%s' "$resp" | tail -1)"
body="$(printf '%s' "$resp" | sed '$d')"
NEW_AT="$(printf '%s' "$body" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)"
NEW_RT="$(printf '%s' "$body" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("refresh_token",""))' 2>/dev/null || true)"

if [[ "$code" != "200" || -z "$NEW_AT" || -z "$NEW_RT" ]]; then
  echo "error: the minted token failed verification (HTTP $code). Nothing written." >&2
  echo "  endpoint said: $(printf '%s' "$body" | head -c 200)" >&2
  exit 1
fi
echo "  verified: refresh grant returned a working pair (access ${#NEW_AT} chars, refresh ${#NEW_RT} chars)."

# --- write the verified, rotated pair into env file(s) ----------------------------
for f in "${targets[@]}"; do
  NEW_RT="$NEW_RT" NEW_AT="$NEW_AT" awk '
    /^[[:space:]]*refresh_token:/ { print "  refresh_token: " ENVIRON["NEW_RT"]; next }
    /^[[:space:]]*access_token:/  { print "  access_token: " ENVIRON["NEW_AT"];  next }
    { print }
  ' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  label="$(basename "$(dirname "$(dirname "$f")")")"
  echo "  updated ${label}/environments/prod.bru  (refresh fp: $(printf '%s' "$NEW_RT" | shasum -a 256 | cut -c1-10))"
done

echo
echo "Done — the written token is verified working. A fresh access_token (1h) is also in place."
echo "In Bruno: select 'prod' and run the reads directly, or run Auth (04 graded / 00 substrate)."
if [[ ${#targets[@]} -gt 1 ]]; then
  echo "Note: both collections now share this token; it rotates on use, so running Auth in one"
  echo "      may eventually 401 the other — just re-run this script if that happens."
fi
