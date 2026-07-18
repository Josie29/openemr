#!/usr/bin/env bash
#
# sync-copilot-scopes.sh — make the registered SMART client's oauth_clients.scope match the code.
#
# The scope a launched token is GRANTED comes from the `oauth_clients.scope` DB row, not from
# CopilotScopes.php: core's AuthorizationController::processAuthorizeFlowForLaunch() overrides the
# request's scopes with whatever is registered on the row. So adding a scope in CopilotScopes is
# INERT until the row is updated — which is easy to forget and already 502'd prod once (JOS-82:
# Observation reads failed until the prod row was fixed by hand). This script closes that gap: it
# reads the canonical value (CopilotScopes::asString(), via print-copilot-scopes.php) in the target
# environment and updates the Co-Pilot client row when they differ. Idempotent.
#
# Usage:
#   # local dev-easy (primary stack) — dry lists nothing special, just applies if drifted:
#   interface/modules/custom_modules/oe-module-ai-copilot/scripts/sync-copilot-scopes.sh
#
#   # a worktree's stack (find containers via `openemr-cmd worktree list`):
#   LOCAL_OPENEMR_CONTAINER=openemr-<slug>-openemr-1 LOCAL_MYSQL_CONTAINER=openemr-<slug>-mysql-1 \
#     interface/modules/custom_modules/oe-module-ai-copilot/scripts/sync-copilot-scopes.sh
#
#   # production (Railway) — run this as the last step of a qa->main promotion when CopilotScopes changed:
#   interface/modules/custom_modules/oe-module-ai-copilot/scripts/sync-copilot-scopes.sh --prod
#
#   # preview only (no write) — prints the drift it WOULD fix:
#   … sync-copilot-scopes.sh [--prod] --dry-run
#
# Requirements:
#   local : the dev-easy stack up (openemr + mysql containers).
#   --prod: railway CLI authed and linked to agentforge-openemr / production (railway whoami; status).
#
set -euo pipefail

WEBROOT="/var/www/localhost/htdocs/openemr"
MODULE_REL="interface/modules/custom_modules/oe-module-ai-copilot"
PRINT_SCOPES_PHP="${WEBROOT}/${MODULE_REL}/scripts/print-copilot-scopes.php"
RAILWAY_SERVICE="openemr"
PROD=0
DRY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prod) PROD=1; shift ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 1 ;;
    esac
done

# --- target-specific runners --------------------------------------------------
# runphp <php-file>   : run PHP in the env that holds the module code.
# sql '<statement>'   : run one SQL statement against the env's openemr DB, -N (no column names).
# Prod runs both inside the `openemr` service (it carries PHP, the mariadb client, and $MYSQL_*).
# Local splits them: PHP in the openemr container, SQL against the mysql container.
if [[ "$PROD" -eq 1 ]]; then
    LABEL="prod (Railway ${RAILWAY_SERVICE})"
    command -v railway >/dev/null 2>&1 || { echo "ERROR: railway CLI not on PATH." >&2; exit 1; }
    railway whoami >/dev/null 2>&1 || { echo "ERROR: railway not authed — run: railway login" >&2; exit 1; }
    runphp() { railway ssh -s "$RAILWAY_SERVICE" "php $1" 2>/dev/null; }
    sql() {
        railway ssh -s "$RAILWAY_SERVICE" \
            "mariadb --skip-ssl -h \"\$MYSQL_HOST\" -u \"\$MYSQL_USER\" -p\"\$MYSQL_PASS\" openemr -N -e \"$1\"" 2>/dev/null
    }
else
    LOCAL_OPENEMR_CONTAINER="${LOCAL_OPENEMR_CONTAINER:-development-easy-openemr-1}"
    LOCAL_MYSQL_CONTAINER="${LOCAL_MYSQL_CONTAINER:-development-easy-mysql-1}"
    LABEL="local (${LOCAL_OPENEMR_CONTAINER})"
    docker inspect "$LOCAL_OPENEMR_CONTAINER" >/dev/null 2>&1 || { echo "ERROR: container '$LOCAL_OPENEMR_CONTAINER' not found." >&2; exit 1; }
    docker inspect "$LOCAL_MYSQL_CONTAINER"   >/dev/null 2>&1 || { echo "ERROR: container '$LOCAL_MYSQL_CONTAINER' not found." >&2; exit 1; }
    runphp() { docker exec "$LOCAL_OPENEMR_CONTAINER" php "$1"; }
    sql() { docker exec "$LOCAL_MYSQL_CONTAINER" mariadb -uroot -proot openemr -N -e "$1" 2>/dev/null; }
fi

echo "==> Target: ${LABEL}"

# --- canonical scope from code ------------------------------------------------
new_scope="$(runphp "$PRINT_SCOPES_PHP")"
[[ -n "$new_scope" ]] || { echo "ERROR: could not read CopilotScopes::asString() (empty)." >&2; exit 1; }
echo "    code scopes: ${new_scope}"

# --- find the Co-Pilot client row(s) ------------------------------------------
# A SMART app the module registered carries both `launch` and at least one `patient/` scope; other
# oauth_clients (portal, third-party) do not match, so they are left untouched. SQL-escape any quote.
esc_scope="${new_scope//\'/\'\'}"
# read loop (not mapfile) so the script runs on macOS's bash 3.2, like clone-prod-to-local.sh
clients=()
while IFS= read -r cid; do
    [[ -n "$cid" ]] && clients+=("$cid")
done < <(sql "SELECT client_id FROM oauth_clients WHERE scope LIKE '%launch%' AND scope LIKE '%patient/%';")
if [[ "${#clients[@]}" -eq 0 ]]; then
    echo "ERROR: no Co-Pilot oauth_clients row found (needs a scope with 'launch' + 'patient/')." >&2
    echo "       Register the SMART client first (see the module README admin prerequisites)." >&2
    exit 1
fi

# --- reconcile each ------------------------------------------------------------
changed=0
for cid in "${clients[@]}"; do
    [[ -n "$cid" ]] || continue
    current="$(sql "SELECT scope FROM oauth_clients WHERE client_id='${cid}';")"
    if [[ "$current" == "$new_scope" ]]; then
        echo "    ${cid}: already in sync"
        continue
    fi
    echo "    ${cid}: DRIFT"
    echo "        was: ${current}"
    echo "        now: ${new_scope}"
    if [[ "$DRY" -eq 1 ]]; then
        echo "        [dry-run] would UPDATE oauth_clients.scope for ${cid}"
    else
        sql "UPDATE oauth_clients SET scope='${esc_scope}' WHERE client_id='${cid}';"
    fi
    changed=1
done

if [[ "$changed" -eq 1 && "$DRY" -eq 1 ]]; then
    echo "==> Dry run — nothing written. Re-run without --dry-run to apply."
elif [[ "$changed" -eq 1 ]]; then
    echo "==> Updated. A NEW SMART launch is required for existing sessions to pick up the change"
    echo "    (a token already minted keeps its old scopes)."
else
    echo "==> Nothing to do — all Co-Pilot clients already match the code."
fi
