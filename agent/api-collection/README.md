# Clinical Co-Pilot — runnable API collection

A [Bruno](https://www.usebruno.com/) collection that exercises the deployed Clinical Co-Pilot
agent end to end — liveness, readiness, the authorization boundary, and a multi-turn grounded
conversation — **without reading any source code**. Bruno collections are plain-text `.bru`
files, so this whole folder version-controls and diffs in git (unlike a Postman JSON blob).

Target: the live prod agent at `https://copilot-agent-production-eb24.up.railway.app`.

## Requests (run in order)

| # | Request | Auth | Expect | What it proves |
|---|---------|------|--------|----------------|
| 01 | Health | none | 200 `alive` | Process is up |
| 02 | Ready | none | 200 `ready:true` | FHIR + LLM + Langfuse all reachable |
| 03 | Chat - No Token | none | **401** | A tokenless turn is refused, not served |
| 04 | Auth - Refresh Token | — | 200 + `access_token` | Mints a fresh 1-hour, patient-scoped token |
| 05 | Chat - New Turn | bearer | 200 `summary` + `claims` | Grounded answer with per-claim citations |
| 06 | Chat - Follow-up | bearer | 200 | Multi-turn context (reuses `conversation_id`) |
| 07 | Chat - Patient Mismatch | bearer | **403** | A conversation can't be steered to another patient |

01–03 run cold against prod with **zero setup** — they need no token. 04 mints the token
04–07 depend on. The two negative cases (03, 401 and 07, 403) are the authorization
boundary tests: they hold whether or not you have a valid token.

## Setup

1. **Install Bruno** (desktop app or `npm i -g @usebruno/cli`) and open this folder as a collection.
2. **Create the working environment.** Copy `environments/prod.example.bru` to
   `environments/prod.bru` and fill in `client_secret` and `refresh_token`.
   - `prod.bru` is **git-ignored** — it holds live credentials and is delivered with the
     submission, never committed. If you received a filled `prod.bru` with the submission,
     drop it in and skip the copy.
3. **Select the `prod` environment** in Bruno (top-right).
4. **Run 04 (Refresh Token) first**, then 05 → 06 → 07. Or use the CLI:
   ```bash
   bru run --env prod
   ```

## Credentials & security

- **No secrets are committed.** `client_id`, `client_secret`, and `refresh_token` live only in
  the git-ignored `prod.bru`, handed over with the submission. The committed
  `prod.example.bru` carries placeholders.
- **The token is narrow.** It is a SMART **patient-scoped**, read-only token
  (`patient/*.read`) bound to one demo patient (Adrian Becker, all-synthetic Synthea data).
  It cannot write, and cannot read another patient — request 07 shows the agent refusing a
  cross-patient turn. There are no database credentials anywhere in the agent; the token is
  the only key it holds.
- **Prod stays locked down.** The insecure OAuth2 password grant is **not** enabled on the
  deployment. Tokens are minted out-of-band with OpenEMR's CLI (below), never via a public
  self-service grant.

## Re-minting a token

The refresh token lasts 3 months; the access token 1 hour (request 04 refreshes it). The
refresh token is also **single-use** — OpenEMR rotates it on every refresh — so if you run
Auth from two places, or a stale copy gets used, it can revoke. **If request 04 returns
`401 "Token has been revoked"` (or `invalid_grant`), the saved refresh token is dead and you
need to re-mint.**

### The easy way — helper script

```bash
agent/scripts/mint-fhir-token.sh          # updates api-collection + fhir-substrate (if present)
agent/scripts/mint-fhir-token.sh --graded-only
```

It prompts for your OpenEMR username (default `admin`) and password, runs the mint on prod,
then **auto-extracts** the fresh access + refresh tokens from the CLI's JSON output and writes
both into `prod.bru` — no copy/paste. The scopes — including `patient/DocumentReference.read`,
needed for clinical-note reads — live in one place at the top of the script. Override
`CLIENT_ID` / `PATIENT_ID` via env vars to target a different client or patient.

### The manual way — CLI directly

Run OpenEMR's token CLI on the prod `openemr` service as the `apache` web user (enter the admin
password at the hidden prompt; username defaults to `admin`):

```bash
railway ssh -s openemr "su -s /bin/sh apache -c 'cd /var/www/localhost/htdocs/openemr && php bin/console openemr-dev:api-generate-access-token --client-id=itdfnJA8SHPTnSpzCGTVDc4FkqaMIiqBwqvvgooYcQU --patient=a234013f-932b-434c-8f21-9edc54ff3892 --scopes=openid,fhirUser,launch,patient/Patient.read,patient/Condition.read,patient/MedicationRequest.read,patient/AllergyIntolerance.read,patient/Encounter.read,patient/DocumentReference.read,offline_access'"
```

- `--scopes` is a **comma-separated** list (the CLI splits on commas, not spaces).
- `--patient` binds the token to one patient; `launch` + `patient/*.read` scope the FHIR
  reads; `offline_access` is what makes the CLI also emit a refresh token.
- Paste the printed **refresh token** into `refresh_token` in `prod.bru`. The access token it
  also prints is optional — request 04 will mint a fresh one.
- To target a different patient, swap `--patient` for another Patient UUID and update
  `patient_id` in `prod.bru`.

> **Note:** the prod DB (and thus the OAuth token store) lives on a persistent Railway volume,
> so tokens survive `railway redeploy -s openemr`. A revoked token is a lifecycle event (rotation
> or a re-mint), not a deploy wiping state — so re-minting fixes it durably.
