#!/usr/bin/env bash
#
# bootstrap-worktree-copilot.sh — wire the AI Co-Pilot into a fresh OpenEMR
# worktree stack so the SMART-launched sidebar renders and answers against the
# worktree's *own* live FHIR data.
#
# A worktree created by `openemr-cmd worktree add <branch> --start` gets an
# isolated, EMPTY database: no registered SMART OAuth client, no enabled
# Co-Pilot module, and the generated compose override carries none of the
# AI_COPILOT_* env the module needs. This script performs the one-time,
# per-worktree bootstrap the Module Manager / OAuth admin screens would do by
# hand, entirely against the worktree's own containers (never the primary).
#
# It does NOT load patient data — do that first with the demo pack:
#     openemr-cmd worktree exec <branch> drid      # dev-reset-install-demodata
#
# Usage:
#     bootstrap-worktree-copilot.sh <branch> [--agent-port N]
#
#   <branch>        the worktree branch (e.g. feature/my-thing)
#   --agent-port N  host port the Co-Pilot agent will listen on (default 8001;
#                   keep it off :8000 so a primary/other-session agent doesn't
#                   collide). The sidebar is pointed at http://localhost:N.
#
# Idempotent: re-running reuses an already-registered client (it never
# double-registers) and re-applies the module-enable / env injection.
#
# After it finishes, start the agent it prints, e.g.:
#     cd agent && COPILOT_FHIR_CLIENT_MODE=http \
#       COPILOT_FHIR_BASE_URL=http://localhost:<port>/apis/default/fhir \
#       COPILOT_CORS_ORIGINS=http://localhost:<port> \
#       .venv/bin/uvicorn copilot.main:app --port <agent-port>

set -euo pipefail

MODULE_DIR="oe-module-ai-copilot"
CONTAINER_WEBROOT="/var/www/localhost/htdocs/openemr"
AGENT_PORT=8001

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "  --> $*"; }

branch=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent-port) AGENT_PORT="${2:?--agent-port needs a value}"; shift 2 ;;
        -*) die "unknown option: $1" ;;
        *) [[ -z "$branch" ]] && branch="$1" || die "unexpected argument: $1"; shift ;;
    esac
done
[[ -n "$branch" ]] || die "usage: bootstrap-worktree-copilot.sh <branch> [--agent-port N]"

command -v docker >/dev/null || die "docker not found"

# Docker-safe slug — mirrors openemr-cmd's wt_slug so container names match.
slug="$(printf '%s' "$branch" | tr '/' '-' | tr -cd 'a-zA-Z0-9_-' | tr '[:upper:]' '[:lower:]')"
[[ -n "$slug" ]] || die "branch '$branch' has no slug-safe characters"

openemr_ctr="openemr-${slug}-openemr-1"
mysql_ctr="openemr-${slug}-mysql-1"
docker inspect "$openemr_ctr" >/dev/null 2>&1 || die "container '$openemr_ctr' not found — is the stack up? (openemr-cmd worktree up $branch)"
docker inspect "$mysql_ctr"   >/dev/null 2>&1 || die "container '$mysql_ctr' not found"

# Discover the published HTTP port (host side of container port 80) rather than
# recomputing the offset — decouples this script from openemr-cmd state schema.
http_port="$(docker port "$openemr_ctr" 80/tcp 2>/dev/null | head -1 | sed -E 's/.*:([0-9]+)$/\1/')"
[[ -n "$http_port" ]] || die "could not determine published HTTP port for $openemr_ctr"
origin="http://localhost:${http_port}"

# The compose dir (holds docker-compose.override.yml) from the compose label.
compose_dir="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$openemr_ctr")"
override="${compose_dir}/docker-compose.override.yml"
[[ -f "$override" ]] || die "override not found: $override"

redirect_uri="${origin}/interface/modules/custom_modules/${MODULE_DIR}/public/callback.php"

# The exact scopes the Co-Pilot's tools need. `launch` (never `launch/patient`,
# whose substring match re-triggers the patient picker). See CopilotScopes.php.
scope="openid fhirUser online_access launch patient/Patient.read patient/Condition.read patient/MedicationRequest.read patient/AllergyIntolerance.read patient/Encounter.read patient/DocumentReference.read"

sql() { docker exec -i "$mysql_ctr" mariadb -uroot -proot openemr "$@"; }

echo "== Bootstrapping Co-Pilot for worktree '$branch' =="
echo "   openemr container : $openemr_ctr"
echo "   origin            : $origin"
echo "   agent port        : $AGENT_PORT"

# 1. Point OpenEMR's OAuth/SMART origin at the browser origin (HTTP, so the
#    HTTPS-vs-HTTP mixed-content block doesn't kill the agent fetch). The demo
#    reinstall resets this to the compose HTTPS default, so always set it.
info "setting site_addr_oath = $origin"
sql -e "UPDATE globals SET gl_value='${origin}' WHERE gl_name='site_addr_oath';"
sql -e "INSERT INTO globals (gl_name, gl_index, gl_value) SELECT 'site_addr_oath', 0, '${origin}' WHERE NOT EXISTS (SELECT 1 FROM globals WHERE gl_name='site_addr_oath');"

# 2. Register the confidential SMART client (RFC 7591 dynamic registration).
#    application_type=private is what makes OpenEMR mint a client_secret, and it
#    is stored encrypted with THIS install's keys automatically — never clone
#    the keys table across installs. Reuse an existing client if one is already
#    wired into the override (idempotency; avoids orphan clients on re-run).
client_id="$(sed -nE 's/.*AI_COPILOT_CLIENT_ID: *"([^"]+)".*/\1/p' "$override" | head -1)"
client_secret=""
if [[ -n "$client_id" ]]; then
    info "reusing already-registered client ($client_id)"
    client_secret="$(sed -nE 's/.*AI_COPILOT_CLIENT_SECRET: *"([^"]+)".*/\1/p' "$override" | head -1)"
else
    info "registering SMART client at ${origin}/oauth2/default/registration"
    reg="$(curl -sk -X POST "${origin}/oauth2/default/registration" \
        -H 'Content-Type: application/json' \
        -d "{\"application_type\":\"private\",\"client_name\":\"AI Clinical Co-Pilot (worktree ${slug})\",\"token_endpoint_auth_method\":\"client_secret_post\",\"redirect_uris\":[\"${redirect_uri}\"],\"scope\":\"${scope}\"}")"
    client_id="$(printf '%s' "$reg" | sed -nE 's/.*"client_id":"([^"]+)".*/\1/p')"
    client_secret="$(printf '%s' "$reg" | sed -nE 's/.*"client_secret":"([^"]+)".*/\1/p')"
    [[ -n "$client_id" && -n "$client_secret" ]] || die "registration failed: $reg"
fi

# 3. Enable the client + skip the per-launch consent screen (EHR launch).
info "enabling client + authorization-flow skip"
sql -e "UPDATE oauth_clients SET is_enabled=1, skip_ehr_launch_authorization_flow=1 WHERE client_id='${client_id}';"

# 4. Register + enable the custom module in the fresh DB (Module Manager parity).
#    Run as the web user — OpenEMR CLI refuses to run as root.
info "registering + enabling the $MODULE_DIR module"
docker exec -i "$openemr_ctr" su -s /bin/sh apache -c \
    "php ${CONTAINER_WEBROOT}/interface/modules/custom_modules/${MODULE_DIR}/scripts/register-enable-module.php"

# 5. Inject the module env into the worktree's compose override, then recreate
#    the openemr container so it re-reads it. The override is regenerated by
#    `openemr-cmd worktree add/regen`, so this step is safe to re-run.
info "injecting AI_COPILOT_* env into $override"
python3 - "$override" "$client_id" "$client_secret" "$AGENT_PORT" <<'PY'
import sys
override, cid, secret, agent_port = sys.argv[1:5]
env = [
    f'      AI_COPILOT_CLIENT_ID: "{cid}"\n',
    f'      AI_COPILOT_CLIENT_SECRET: "{secret}"\n',
    f'      AI_COPILOT_AGENT_URL: "http://localhost:{agent_port}"\n',
    '      AI_COPILOT_OAUTH_INTERNAL_BASE: "http://localhost"\n',
]
lines = [l for l in open(override).readlines() if 'AI_COPILOT_' not in l]  # idempotent
out = []
for l in lines:
    out.append(l)
    if l.rstrip() == '      HOST_GID: "20"':
        out.extend(env)
open(override, 'w').writelines(out)
PY
grep -q 'AI_COPILOT_CLIENT_ID' "$override" || die "env injection did not take (no 'HOST_GID: \"20\"' anchor in override?)"

info "recreating openemr container to load env"
docker compose --project-directory "$compose_dir" up -d openemr >/dev/null 2>&1 \
    || die "compose up failed; run: openemr-cmd worktree up $branch"

cat <<EOF

== Done. Co-Pilot bootstrapped for '$branch' ==
   OpenEMR : $origin   (login admin / pass)
   Client  : $client_id  (enabled, flow-skip)

Next — start the agent (live FHIR against this worktree), from the repo root:

   cd agent && COPILOT_FHIR_CLIENT_MODE=http \\
     COPILOT_FHIR_BASE_URL=${origin}/apis/default/fhir \\
     COPILOT_CORS_ORIGINS=${origin} \\
     .venv/bin/uvicorn copilot.main:app --port ${AGENT_PORT}

Then open a patient chart at $origin and toggle the Co-Pilot sidebar.
EOF
