#!/usr/bin/env bash
#
# Seed the OpenEMR "Medication List" document category (JOS-91).
#
# The Co-Pilot resolves a document's extraction schema from its OpenEMR category
# (agent `resolve_doc_type`): "Lab Report" -> lab_pdf, "Patient Information" ->
# intake_form, and "Medication List" -> medication_list. Only the first two ship
# with OpenEMR, so the medication-list category must be seeded for the third
# document type to resolve. Uploading a med-list PDF under any other category
# leaves it unlisted by the agent.
#
# `categories` is a nested-set (MPTT) tree, so a naive INSERT would break every
# lft/rght range. This appends the new node as the last child of the root
# ("Categories", id 1): shift the root's rght past the new node, then insert it
# in the freed slot. Idempotent — it no-ops if the category already exists.
#
# Usage:
#   seed-medication-list-category.sh                 # dev-easy stack (openemr-cmd e)
#   seed-medication-list-category.sh <worktree>      # a named worktree's stack
#
# Runs the SQL inside the openemr container via openemr-cmd, so no host mysql
# client is needed.
set -euo pipefail

WORKTREE="${1:-}"

# Route the mysql invocation through the right container. `openemr-cmd e` runs a
# command in the dev-easy openemr container; `worktree exec <name> e` targets a
# worktree's own stack.
run_sql() {
    local sql="$1"
    local cmd="mysql -h mysql -uopenemr -popenemr openemr -N -e \"${sql}\""
    if [ -n "$WORKTREE" ]; then
        openemr-cmd worktree exec "$WORKTREE" e "$cmd"
    else
        openemr-cmd e "$cmd"
    fi
}

exists="$(run_sql "SELECT COUNT(*) FROM categories WHERE name='Medication List' AND parent=1;" | tr -d '[:space:]')"
if [ "$exists" != "0" ]; then
    echo "Medication List category already present — nothing to do."
    exit 0
fi

# MPTT append under root (id 1): read the root's rght as the new node's lft, shift
# every node whose range sits at/after that boundary, then insert into the slot.
run_sql "
SET @r := (SELECT rght FROM categories WHERE id=1);
SET @newid := (SELECT COALESCE(MAX(id),0)+1 FROM categories);
UPDATE categories SET rght = rght + 2 WHERE rght >= @r;
UPDATE categories SET lft = lft + 2 WHERE lft >= @r;
INSERT INTO categories (id, name, value, parent, lft, rght, aco_spec)
VALUES (@newid, 'Medication List', '', 1, @r, @r + 1, 'patients|docs');
"
echo "Seeded 'Medication List' document category."
