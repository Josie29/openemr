#!/usr/bin/env bash
#
# reset-derived-facts.sh — undo agent-derived (preliminary) lab write-backs for one patient.
#
# The write-back path (JOS-81) persists extracted lab facts as `preliminary` FHIR Observations by
# synthesizing a procedure_order → procedure_order_code → procedure_report → procedure_result chain
# (LabResultWriter.php), plus a non-clinical provenance row in `ai_copilot_document_facts`. For a
# demo you rehearse the upload→extract→write→chart flow, then reset so the real run shows the point
# APPEAR rather than being a leftover. This deletes ONLY rows carrying the derived signature — it
# never touches a real result (which has a real provider/lab and result_status='final').
#
# Derived signature (all must hold):
#   procedure_result.result_status = 'preliminary' AND procedure_result.document_id IS NOT NULL
#   procedure_report.report_status = 'preliminary'
#   procedure_order_code.procedure_source = '2'   (external source)
#   procedure_order.provider_id = 0 AND lab_id = 0 AND order_status = 'completed'
#                  AND procedure_order_type = 'laboratory_test'
#
# DRY RUN BY DEFAULT — prints the rows it would delete; deletes only with --confirm.
#
# Usage:
#   # preview what would be removed for Sergio (pid 23) on the local dev-easy stack:
#   … scripts/reset-derived-facts.sh --pid 23
#
#   # actually delete them, local:
#   … scripts/reset-derived-facts.sh --pid 23 --confirm
#
#   # a worktree's stack:
#   LOCAL_MYSQL_CONTAINER=openemr-<slug>-mysql-1 … reset-derived-facts.sh --pid 23 [--confirm]
#
#   # production (Railway) — you run this; the script never applies to prod without --confirm:
#   … scripts/reset-derived-facts.sh --pid 23 --prod            # dry run against prod
#   … scripts/reset-derived-facts.sh --pid 23 --prod --confirm  # apply on prod
#
# Optional: --document <documents.id> narrows to one uploaded document's facts.
#
set -euo pipefail

RAILWAY_SERVICE="openemr"
PID=""
DOCUMENT=""
PROD=0
CONFIRM=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pid) PID="${2:?--pid needs a value}"; shift 2 ;;
        --document) DOCUMENT="${2:?--document needs a value}"; shift 2 ;;
        --prod) PROD=1; shift ;;
        --confirm) CONFIRM=1; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 1 ;;
    esac
done

[[ "$PID" =~ ^[0-9]+$ ]] || { echo "ERROR: --pid <integer> is required." >&2; exit 1; }
[[ -z "$DOCUMENT" || "$DOCUMENT" =~ ^[0-9]+$ ]] || { echo "ERROR: --document must be a documents.id integer." >&2; exit 1; }

# --- target-specific SQL runner -----------------------------------------------
# sql  : plain output (-N, no headers/borders) — for existence checks and DELETEs.
# sqlt : boxed table output (-t) — for the human-facing preview and the final count.
if [[ "$PROD" -eq 1 ]]; then
    LABEL="prod (Railway ${RAILWAY_SERVICE})"
    command -v railway >/dev/null 2>&1 || { echo "ERROR: railway CLI not on PATH." >&2; exit 1; }
    railway whoami >/dev/null 2>&1 || { echo "ERROR: railway not authed — run: railway login" >&2; exit 1; }
    sql()  { railway ssh -s "$RAILWAY_SERVICE" "mariadb --skip-ssl -h \"\$MYSQL_HOST\" -u \"\$MYSQL_USER\" -p\"\$MYSQL_PASS\" openemr -N -e \"$1\"" 2>/dev/null; }
    sqlt() { railway ssh -s "$RAILWAY_SERVICE" "mariadb --skip-ssl -h \"\$MYSQL_HOST\" -u \"\$MYSQL_USER\" -p\"\$MYSQL_PASS\" openemr -t -e \"$1\"" 2>/dev/null; }
else
    LOCAL_MYSQL_CONTAINER="${LOCAL_MYSQL_CONTAINER:-development-easy-mysql-1}"
    LABEL="local (${LOCAL_MYSQL_CONTAINER})"
    docker inspect "$LOCAL_MYSQL_CONTAINER" >/dev/null 2>&1 || { echo "ERROR: container '$LOCAL_MYSQL_CONTAINER' not found." >&2; exit 1; }
    sql()  { docker exec "$LOCAL_MYSQL_CONTAINER" mariadb -uroot -proot openemr -N -e "$1" 2>/dev/null; }
    sqlt() { docker exec "$LOCAL_MYSQL_CONTAINER" mariadb -uroot -proot openemr -t -e "$1" 2>/dev/null; }
fi

# The shared WHERE predicate that pins a row to the derived signature for this patient. Kept in one
# place so the preview and the deletes can never diverge. $DOC_FILTER narrows to one document.
DOC_FILTER=""
[[ -n "$DOCUMENT" ]] && DOC_FILTER="AND pr.document_id = ${DOCUMENT}"
DERIVED_WHERE="po.patient_id = ${PID}
  AND pr.result_status = 'preliminary' AND pr.document_id IS NOT NULL
  AND po.provider_id = 0 AND po.lab_id = 0
  AND po.order_status = 'completed' AND po.procedure_order_type = 'laboratory_test'
  ${DOC_FILTER}"

echo "==> Target: ${LABEL}   patient pid=${PID}${DOCUMENT:+   document=${DOCUMENT}}"
echo "==> Derived preliminary lab rows that match:"
sqlt "SELECT po.procedure_order_id AS order_id, pr.result_code AS loinc, pr.result_text AS test,
            pr.result AS value, pr.units, DATE(pr.date) AS result_date, pr.document_id AS doc
     FROM procedure_result pr
     JOIN procedure_report prep ON prep.procedure_report_id = pr.procedure_report_id
     JOIN procedure_order po ON po.procedure_order_id = prep.procedure_order_id
     WHERE ${DERIVED_WHERE}
     ORDER BY pr.result_code;"

if [[ "$CONFIRM" -ne 1 ]]; then
    echo "==> DRY RUN — nothing deleted. Re-run with --confirm to remove the rows above."
    exit 0
fi

echo "==> Deleting (transaction: results -> reports -> order_code -> orders) ..."
# Multi-table DELETEs (MariaDB) scoped by the derived signature; each later step only removes a
# parent once it has no surviving children, so a real result sharing a synthesized order (there are
# none, but belt-and-braces) keeps its chain.
sql "START TRANSACTION;

DELETE pr FROM procedure_result pr
JOIN procedure_report prep ON prep.procedure_report_id = pr.procedure_report_id
JOIN procedure_order po ON po.procedure_order_id = prep.procedure_order_id
WHERE ${DERIVED_WHERE};

DELETE prep FROM procedure_report prep
JOIN procedure_order po ON po.procedure_order_id = prep.procedure_order_id
LEFT JOIN procedure_result pr ON pr.procedure_report_id = prep.procedure_report_id
WHERE po.patient_id = ${PID} AND prep.report_status = 'preliminary'
  AND po.provider_id = 0 AND po.lab_id = 0
  AND po.order_status = 'completed' AND po.procedure_order_type = 'laboratory_test'
  AND pr.procedure_result_id IS NULL;

DELETE poc FROM procedure_order_code poc
JOIN procedure_order po ON po.procedure_order_id = poc.procedure_order_id
LEFT JOIN procedure_report prep ON prep.procedure_order_id = po.procedure_order_id
WHERE po.patient_id = ${PID} AND poc.procedure_source = '2'
  AND po.provider_id = 0 AND po.lab_id = 0
  AND po.order_status = 'completed' AND po.procedure_order_type = 'laboratory_test'
  AND prep.procedure_report_id IS NULL;

DELETE po FROM procedure_order po
LEFT JOIN procedure_report prep ON prep.procedure_order_id = po.procedure_order_id
LEFT JOIN procedure_order_code poc ON poc.procedure_order_id = po.procedure_order_id
WHERE po.patient_id = ${PID} AND po.provider_id = 0 AND po.lab_id = 0
  AND po.order_status = 'completed' AND po.procedure_order_type = 'laboratory_test'
  AND prep.procedure_report_id IS NULL AND poc.procedure_order_id IS NULL;

COMMIT;"

# Sidecar cleanup — best-effort and OUT of the main transaction. The provenance table
# (ai_copilot_document_facts) is not present on every env (a worktree DB predating the module's SQL
# install lacks it), and nothing clinical gates on it, so a missing table or a leftover orphan is
# harmless. Only rows whose (document_id, LOINC) no longer resolve to a procedure_result are removed.
if [[ -n "$(sql "SHOW TABLES LIKE 'ai_copilot_document_facts';")" ]]; then
    sql "DELETE sc FROM ai_copilot_document_facts sc
         LEFT JOIN procedure_result pr ON pr.document_id = sc.document_id AND pr.result_code = sc.field
         WHERE sc.fact_table = 'procedure_result' AND pr.procedure_result_id IS NULL;"
    echo "    sidecar provenance rows for the removed facts cleared."
else
    echo "    (no ai_copilot_document_facts table on this env — sidecar cleanup skipped.)"
fi

echo "==> Done. Remaining derived preliminary rows for pid=${PID}:"
sqlt "SELECT COUNT(*) AS remaining
     FROM procedure_result pr
     JOIN procedure_report prep ON prep.procedure_report_id = pr.procedure_report_id
     JOIN procedure_order po ON po.procedure_order_id = prep.procedure_order_id
     WHERE ${DERIVED_WHERE};"
