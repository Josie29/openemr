# FHIR Substrate collection (what the tools wrap)

A personal, **exploration-only** Bruno collection that hits the raw OpenEMR FHIR R4
endpoints each agent tool wraps — so you can eyeball the exact request/response shapes the
agent sees before it parses, dedups, filters, and grounds them.

This is **not** a graded deliverable. The grader-facing collection is `../api-collection/`
(JOS-29), which exercises the agent's actual HTTP surface (`/health`, `/ready`, `/chat`).
This one deliberately **bypasses the agent** and talks straight to OpenEMR's FHIR server —
useful for seeing the substrate, useless for judging the agent.

## Tool → FHIR mapping

| Request | Agent tool | FHIR call | Notes |
| -- | -- | -- | -- |
| 01 | `get_patient` | `GET /Patient/{id}` | Direct read; returns one resource, not a Bundle |
| 02 | `get_problems` | `GET /Condition?patient={id}` | searchset Bundle |
| 03 | `get_medications` | `GET /MedicationRequest?patient={id}` | Tool then `dedup_medications` → fewer rows |
| 04 | `get_allergies` | `GET /AllergyIntolerance?patient={id}` | Empty result is valid ("none on record") |
| 05 | `get_encounters` | `GET /Encounter?patient={id}` | Grab an `id` for request 06 |
| 06 | `get_encounter_note` | `GET /DocumentReference?patient={id}&category=clinical-note` | Tool filters to one `encounter_id` in Python |
| 07 | `ping` (`/ready`) | `GET /metadata` | Unauthenticated CapabilityStatement |

## Run order

1. **07 Capability** (optional, no auth) — confirm the FHIR server is reachable.
2. **00 Auth** — mint a 1h `access_token` from the patient-scoped refresh token.
3. **01–06** — read each resource type. All bearer-authed with the token from step 2.

Select the **`prod`** environment first.

## Shared refresh token — the one gotcha

`prod.bru` here was seeded from `../api-collection/environments/prod.bru`, so **both
collections share one refresh_token**. OpenEMR rotates it on every refresh grant, and the
post-response script writes the rotated value back into *this* collection's `prod.bru`.
Consequence: whichever collection you run **00/04 Auth** in last holds the valid token; the
other will `400 invalid_grant` on its next refresh. Fixes:

- Just re-run Auth in whichever collection you're using (it re-reads its own stored token), or
- Copy the current `refresh_token` from the collection you used last, or
- Re-mint a fresh refresh token (see `../api-collection/README.md`).

## Secrets

`environments/prod.bru` (client_secret, refresh_token, runtime access_token) is git-ignored.
`environments/prod.example.bru` is the committed placeholder template.
