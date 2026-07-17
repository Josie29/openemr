#!/usr/bin/env bash
#
# Clone the Railway prod OpenEMR patient/clinical data down to the local
# dev-easy MySQL, so local mirrors prod for demos and Co-Pilot testing.
#
# Direction: prod is the environment of record; we clone DOWN to local
#   (see context/decisions/synthetic-data-generation.md and deployment-strategy.md).
#   This never writes to prod.
#
# Scope: patient + clinical data ONLY. The local auth/config/registry tables are
#   PRESERVED so local keeps working the way a dev box should:
#     - login stays admin/pass         (users, users_secure, users_facility, user_settings)
#     - access control stays local     (gacl_*)
#     - Co-Pilot SMART launch keeps working (oauth_*)
#     - site config stays localhost    (globals)
#     - module registration stays local (registry, modules*, module_configuration)
#     - login-session + audit/log noise is not dragged over (session_tracker, log*, audit_*)
#     - id generators and facility stay local (sequences, facility, version, product_registration, background_services)
#   Everything else -- patient_data, forms, form_*, lists, prescriptions, immunizations,
#   procedure_*, documents, uuid_registry (so FHIR/SMART can resolve patient UUIDs),
#   ar_session (billing), etc. -- is cloned wholesale (DROP + CREATE + data).
#
# Idempotent: re-running re-clones prod's CURRENT state over local's clinical tables.
#   The excluded tables are left exactly as they are locally.
#
# After a clone, re-run the clinical-note seeder so Sergio's note lands locally
# (prod's form_clinical_notes is currently empty -- see JOS-33):
#   openemr-cmd e 'php interface/modules/custom_modules/oe-module-ai-copilot/scripts/seed_demo_clinical_notes.php'
#
# Requirements:
#   - railway CLI authed and linked to agentforge-openemr / production
#     (railway whoami; railway status)
#   - local dev-easy stack up and healthy (see LOCAL_MYSQL_CONTAINER below)
#
# Usage:
#   interface/modules/custom_modules/oe-module-ai-copilot/scripts/clone-prod-to-local.sh
#
#   Against a worktree's stack (find the container via `openemr-cmd worktree list`):
#     LOCAL_MYSQL_CONTAINER=openemr-<branch-slug>-mysql-1 \
#       interface/modules/custom_modules/oe-module-ai-copilot/scripts/clone-prod-to-local.sh
#
set -euo pipefail

# --- Config ------------------------------------------------------------------
RAILWAY_SERVICE="openemr"          # prod OpenEMR service (app container has the mariadb client + $MYSQL_*)
PROD_DB="openemr"                  # prod database name (not exposed as an env var; it is literally "openemr")

# Target container. Defaults to the primary clone's stack; override to target a
# worktree's stack, whose compose project is named for its branch:
#   LOCAL_MYSQL_CONTAINER=openemr-<branch-slug>-mysql-1 clone-prod-to-local.sh
# `docker compose` from inside a worktree does NOT resolve to that worktree -- every
# tree's compose dir is named development-easy, so the project name collides and the
# command silently hits the primary. Always name the container explicitly.
LOCAL_MYSQL_CONTAINER="${LOCAL_MYSQL_CONTAINER:-development-easy-mysql-1}"
LOCAL_MYSQL_USER="root"
LOCAL_MYSQL_PASS="root"
LOCAL_DB="openemr"

# Tables to PRESERVE locally (never cloned). Exact names + LIKE-pattern families.
# NOTE: ar_session (accounts-receivable / billing) is deliberately NOT here -- it
# is patient/financial data we want. Only session_tracker is a login-session table.
#
# CRITICAL: `keys` MUST be preserved. It holds the DB-half of this install's encryption
# keys (v7 sevena/sevenb + the oauth2 keypair). The matching drive-half lives on the local
# filesystem (sites/default/documents/logs_and_misc/methods/), which a DB clone never
# touches. Cloning `keys` desyncs the two halves and breaks ALL CryptoGen encrypt/decrypt
# (symptom: "Key in drive is not compatible with key in database" -> the Co-Pilot SMART
# launch fails with "Could not authorize against the record"). Never clone it.
PRESERVE_EXACT=(
  users users_secure users_facility user_settings
  globals
  keys
  version product_registration background_services
  registry modules modules_settings modules_hooks_settings module_configuration
  log log_comment_encrypt log_validator audit_master audit_details
  session_tracker
  sequences
  facility
)
PRESERVE_LIKE=(
  'gacl_%'
  'oauth_%'
)

# --- Preflight ---------------------------------------------------------------
echo "==> Preflight"

if ! command -v railway >/dev/null 2>&1; then
  echo "ERROR: railway CLI not found on PATH." >&2
  exit 1
fi
if ! railway whoami >/dev/null 2>&1; then
  echo "ERROR: railway is not authed. Run: railway login" >&2
  exit 1
fi
if ! docker inspect "$LOCAL_MYSQL_CONTAINER" >/dev/null 2>&1; then
  echo "ERROR: local mysql container '$LOCAL_MYSQL_CONTAINER' not found. Start the dev-easy stack first." >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
DUMP="$WORKDIR/prod_clinical_dump.sql"
trap 'rm -rf "$WORKDIR"' EXIT

# --- Build the ignore-table list from prod's actual schema -------------------
# Expanding the LIKE patterns against prod (rather than hardcoding every gacl_*/
# oauth_* table) keeps this robust to schema drift between the two installs.
echo "==> Resolving tables to preserve (querying prod schema)"

# Assemble a SQL predicate: exact IN (...) OR ( name LIKE '...' ) ...
exact_in="$(printf "'%s'," "${PRESERVE_EXACT[@]}")"; exact_in="${exact_in%,}"
like_clause=""
for p in "${PRESERVE_LIKE[@]}"; do
  like_clause+=" OR table_name LIKE '$p'"
done

# $MYSQL_HOST/USER/PASS are expanded REMOTELY (single-quoted \$), the predicate
# is expanded LOCALLY (double-quoted heredoc-free string).
preserve_list="$(
  railway ssh -s "$RAILWAY_SERVICE" "mariadb --skip-ssl -h \"\$MYSQL_HOST\" -u \"\$MYSQL_USER\" -p\"\$MYSQL_PASS\" $PROD_DB -N -e \"SELECT table_name FROM information_schema.tables WHERE table_schema='$PROD_DB' AND (table_name IN ($exact_in)$like_clause) ORDER BY table_name;\"" \
    2>/dev/null
)"

if [[ -z "$preserve_list" ]]; then
  echo "ERROR: could not resolve the preserve-list from prod (empty result)." >&2
  exit 1
fi

ignore_flags=""
while IFS= read -r t; do
  [[ -z "$t" ]] && continue
  ignore_flags+=" --ignore-table=${PROD_DB}.${t}"
done <<< "$preserve_list"

preserve_count="$(printf '%s\n' "$preserve_list" | grep -c . || true)"
echo "    preserving $preserve_count local tables (users/gacl/oauth/globals/registry/logs/...)"

# --- Snapshot before ---------------------------------------------------------
prod_patients="$(railway ssh -s "$RAILWAY_SERVICE" "mariadb --skip-ssl -h \"\$MYSQL_HOST\" -u \"\$MYSQL_USER\" -p\"\$MYSQL_PASS\" $PROD_DB -N -e 'SELECT COUNT(*) FROM patient_data;'" 2>/dev/null | tr -dc '0-9')"
local_before="$(docker exec "$LOCAL_MYSQL_CONTAINER" mariadb -u "$LOCAL_MYSQL_USER" -p"$LOCAL_MYSQL_PASS" "$LOCAL_DB" -N -e 'SELECT COUNT(*) FROM patient_data;' 2>/dev/null | tr -dc '0-9')"
echo "==> Patients  prod=$prod_patients  local(before)=$local_before"

# --- Dump prod clinical data -------------------------------------------------
echo "==> Dumping prod clinical data (this streams over railway ssh)"
railway ssh -s "$RAILWAY_SERVICE" \
  "mariadb-dump --skip-ssl -h \"\$MYSQL_HOST\" -u \"\$MYSQL_USER\" -p\"\$MYSQL_PASS\" --single-transaction --no-tablespaces --skip-lock-tables $ignore_flags $PROD_DB" \
  > "$DUMP" 2>/dev/null

# The dump must start with a real mysqldump header, not a stray banner/error.
if ! head -c 4000 "$DUMP" | grep -q "MariaDB dump\|MySQL dump\|CREATE TABLE"; then
  echo "ERROR: prod dump looks malformed (no dump header). First lines:" >&2
  head -5 "$DUMP" >&2
  exit 1
fi
dump_bytes="$(wc -c < "$DUMP" | tr -d ' ')"
echo "    dumped ${dump_bytes} bytes -> $DUMP"

# --- Load into local ---------------------------------------------------------
echo "==> Loading into local ($LOCAL_MYSQL_CONTAINER / $LOCAL_DB)"
docker exec -i "$LOCAL_MYSQL_CONTAINER" mariadb -u "$LOCAL_MYSQL_USER" -p"$LOCAL_MYSQL_PASS" "$LOCAL_DB" < "$DUMP"

# --- Verify ------------------------------------------------------------------
local_after="$(docker exec "$LOCAL_MYSQL_CONTAINER" mariadb -u "$LOCAL_MYSQL_USER" -p"$LOCAL_MYSQL_PASS" "$LOCAL_DB" -N -e 'SELECT COUNT(*) FROM patient_data;' 2>/dev/null | tr -dc '0-9')"
pid23="$(docker exec "$LOCAL_MYSQL_CONTAINER" mariadb -u "$LOCAL_MYSQL_USER" -p"$LOCAL_MYSQL_PASS" "$LOCAL_DB" -N -e 'SELECT CONCAT(fname," ",lname) FROM patient_data WHERE pid=23;' 2>/dev/null)"
echo "==> Done.  local(after)=$local_after patients (prod=$prod_patients).  pid 23 = ${pid23:-<none>}"
echo "    Next: re-seed the clinical note ->"
echo "      openemr-cmd e 'php interface/modules/custom_modules/oe-module-ai-copilot/scripts/seed_demo_clinical_notes.php'"
