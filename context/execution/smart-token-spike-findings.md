# SMART patient-scoped token spike — findings

**Spike:** `context/execution/implementation-prompt-03a-smart-token-spike.md`
**Claim under test:** `ARCHITECTURE.md` §5 — a SMART `patient/*.read` token binds one patient,
making the audit's #1 finding (patient-level IDOR) *physically unreachable through the agent*.
**Verdict:** **KEYSTONE HOLDS.** §5's security claim is verified against the live stack. §5's
*mechanism* description ("the module mints the token") needs one clarification for `-03b`
(the mint is a SMART **EHR-launch** authorization_code flow, not a silent server-side call).

All work is throwaway under `tmp/spike/` (gitignored — holds the client secret + tokens).
No `src/`, `interface/`, `library/`, module, migration, or core change was made. The only
persisted state is one registered OAuth client, enabled via a DB flag that mirrors the admin
UI toggle (see Prerequisite 3).

---

## 1. The keystone result — cross-patient is unreachable by every vector

One token, minted bound to **patient 1** (`a2325b4d-a20e-41a6-9a3e-f8b9f4f38e75`,
Ashley34 Bergstrom287). Patient 2 (`a2325b4e-94dc-4e89-b1d2-9d2304d646f9`, Bessie Muller251)
is a real, existing seed patient (confirmed in `patient_data` and in the consent patient-picker).

| # | Request (same token) | Result | Meaning |
|---|---|---|---|
| Positive | `GET /Patient/{P1}` (bound) | **200**, returns P1's `Patient` | own-patient read works |
| Negative (direct) | `GET /Patient/{P2}` (different) | **500** `{"message":"patient id invalid"}` | cross-patient id **rejected** |
| Search (bare) | `GET /Patient` | **200**, `total=1`, only P1 | search auto-filtered to bound patient |
| Search (clinical) | `GET /Condition` | **200**, `total=14`, **all** `subject = Patient/P1` | clinical resources auto-scoped too |
| **Query injection** | `GET /Condition?patient={P2}` | **200**, `total=14`, **still P1's** conditions | explicit cross-patient param is **ignored/overridden** |
| Expiry | any read after 3600 s | **401** | fail-closed on TTL |

The query-injection row is the decisive one: an attacker who *explicitly asks* for another
patient's data (`?patient=P2`) does not get an error they could route around — they get the
**bound** patient's data. The patient context is injected server-side into every FHIR query
and cannot be widened by the caller. There is no cross-patient code path to exploit, exactly
as §5 asserts.

Raw evidence (captured while the token was valid): `tmp/spike/patient_positive.json`,
`patient_negative.json`, `patient_search.json`, `condition_search.json`, `condition_p2.json`,
`token_response.json`, and the token binding via `tmp/spike/` introspection
(`patient: a2325b4d…`, full `patient/*.read` scope set).

> **Impl quirk worth noting:** the direct cross-patient read returns **HTTP 500**
> `{"error":"An error occurred","message":"patient id invalid"}`, not a clean 403/404. It *is*
> a denial (no PHI leaks), but it is an ugly error surface. `-01`'s FHIR client must treat any
> non-2xx as a hard denial / fail-closed — do not special-case 403.

---

## 2. The working token flow (reproducible)

Driver: `tmp/spike/flow.py` (host-side, same-origin against `https://localhost:9300`, cookie
jar + CSRF scraping). It reproduces what a browser does; the earlier Panther/Selenium attempt
(`tmp/spike_authorize.php`) dead-ended because after login OpenEMR redirects to its configured
public URL `https://localhost:9300`, which the in-container Selenium browser cannot reach —
see "drift" note below.

1. **Register** a confidential client — `POST /oauth2/default/registration` (open, no auth):
   `token_endpoint_auth_method=client_secret_post`, a `redirect_uris` entry, and
   `scope="openid fhirUser online_access launch/patient patient/Patient.read …"`.
   → returns `client_id` + `client_secret`. (`tmp/spike/client_registration.json`)
2. **Enable** the client: newly registered clients are **disabled by default**
   (`oauth_clients.is_enabled = 0`). Set `is_enabled = 1` (admin action — see Prerequisite 3).
3. **Authorize** — `GET /oauth2/default/authorize?response_type=code&client_id=…`
   `&redirect_uri=…&scope=openid fhirUser online_access launch/patient patient/*.read`
   `&aud=https://localhost:9300/apis/default/fhir&code_challenge=…&code_challenge_method=S256`.
   PKCE **S256 is the only method advertised**; `aud` (the FHIR base) is required.
   This walks an interactive, session-stateful chain:
   - **login** (`/oauth2/default/login`, `user_role=api`, admin session) →
   - **patient-select** (`/smart/patient-select` — because `launch/patient` was requested with
     no launch token, OpenEMR *forces* an interactive patient pick) →
   - **scope-authorize** consent (`/scope-authorize-confirm`). Clinical `patient/*.read` scopes
     render as per-resource action checkboxes that page JS reassembles into
     `scope[patient/Patient.read]=…` on submit (v1 read+search → `.read`). `flow.py` replicates
     that reconstruction — a naive "grab the hidden scope inputs" misses every clinical scope.
4. **Redirect** to `…/spike-callback?code=…&state=…` carries the authorization code.
5. **Token exchange** — `POST /oauth2/default/token` `grant_type=authorization_code` +
   `client_secret` + `code_verifier`. Response (`tmp/spike/token_response.json`) includes the
   SMART launch context: **`patient: a2325b4d…`**, `need_patient_banner`, `smart_style_url`,
   `scope`, `expires_in: 3600`.

The **"one patient" binding is established at step 3's patient-select** and travels in the
token as the `patient` launch-context claim; the FHIR layer reads that claim and scopes every
query to it (Section 1).

---

## 3. Enablement / config prerequisites discovered (inputs for `-03b`)

1. **API + FHIR globals ON.** Verified already set on the dev stack: `rest_api=1`,
   `rest_fhir_api=1`. (`rest_portal_api`, `oauth_password_grant`, `rest_system_scopes_api` are
   **not** needed for this flow — we used authorization_code, not password/client-credentials.)
2. **Open client registration** is available (no bootstrap credential needed to register).
3. **Client must be enabled after registration** — default `is_enabled=0`. In the UI this is
   **Administration → Config → Connectors → (registered API clients) → Enable**. In the spike we
   flipped `oauth_clients.is_enabled=1` in the DB as a stand-in for that admin click. → `-03b`
   must either ship the module as a pre-trusted/enabled first-party client or document this as a
   one-time admin step.
4. **Confidential client** (`client_secret_post`) with a registered `redirect_uri`, **PKCE S256**,
   and **`aud`** = FHIR base on the authorize request. All mandatory.
5. **A physician (OpenEMR user) session** is required to authorize — the token is minted *on
   behalf of* the logged-in user, not anonymously.

---

## 4. What OpenEMR actually does vs. what §5 assumed — the one correction for `-03b`

§5 says *"Module mints patient/*.read token for P."* That is directionally right and the
security outcome is exactly as claimed — but the **mechanism** is not a silent server-side mint.
OpenEMR only issues a `patient/`-context token through the **SMART authorization_code flow**,
which by default requires (a) an authenticated user session, (b) a patient selection, and
(c) a scope-consent screen. In the spike we satisfied (b) via the interactive **patient-select**
picker. For the module to mint *seamlessly for the already-open patient P* — with no picker and
no repeated consent — `-03b` must implement the **SMART EHR launch**:

- Generate a **launch token** carrying patient context = P from the physician's authenticated
  chart session (the `PatientDemographics\RenderEvent` mount point already knows P), and pass
  `launch=<token>` + `scope=launch …` to `/authorize`. EHR launch **replaces** the interactive
  patient-select with the pre-established context (`.well-known/smart-configuration` advertises
  `launch-ehr` and `context-ehr-patient`, confirming support).
- Set the **"OAuth2 EHR-Launch Authorization Flow Skip"** app setting for the trusted first-party
  client so the consent screen is skipped (docs `AUTHENTICATION.md` line 413) — otherwise the
  physician re-consents on every launch.

So the concrete `-03b` mechanism is: **first-party trusted client + EHR launch (patient context
from the chart) + skip-consent app setting → authorization_code exchange → patient-bound token.**
This is a *refinement*, not a contradiction, of §5. No core patch was required to prove the flow,
so `ARCHITECTURE.md` §3's "zero core patches" still stands (EHR-launch context generation is
module code using published extension points, not a core edit).

**Doc drift noted (code wins):** `.well-known/smart-configuration` advertises only
`client_credentials` + `authorization_code`, but the OIDC config and live behavior also enable
`password` + `refresh_token` (dev `oauth_password_grant=3`). Password grant is a dead end for
this use case anyway: `user_role=users` yields multi-patient `user/` scopes (no single-patient
binding), and `user_role=patient` needs the *patient's own* portal login (seed patients have
none) — neither is the physician-launches-on-open-patient shape §5 needs.

---

## 5. Token lifetime / refresh

- `expires_in = 3600` (1 h). Empirically enforced — reads returned **401** once elapsed.
- **No refresh token** here: we requested `online_access`, not `offline_access`. For a single
  chart-orientation session 1 h is ample. If `-03b` wants silent renewal, request `offline_access`
  (refresh tokens live ~3 months per docs) — but weigh that against the audit posture (a
  long-lived refresh token is a bigger secret to hold than a 1 h bearer).
- Token introspection (`POST /oauth2/default/introspect`) echoes the binding (`patient`, `scope`)
  and requires **post-body** client auth for this `client_secret_post` client (HTTP Basic
  returned `active:false`).

---

## 6. Data-quality note for `-01`'s `PatientDemographics` contract

Captured `Patient` fixture: `agent/tests/fixtures/patient_bergstrom287.fhir.json`.
Top-level fields present: `active, address, birthDate, communication, deceasedBoolean,
extension, gender, id, identifier, meta, name, resourceType, text`.
**Absent for this seed patient:** `telecom` (phone/email) and `maritalStatus`. `-01`'s
`PatientDemographics` model must treat those (and any not-guaranteed field) as **nullable** —
parse-don't-validate at the boundary, do not assume telecom exists.

---

## 7. Decision-grade recommendation

**Proceed with `-03b` as scoped — `ARCHITECTURE.md` §5 stands as a security claim and needs only
a one-line mechanism refinement, not a redesign.** The load-bearing assumption is verified end to
end: a `patient/*.read` token binds exactly one patient, and cross-patient access is unreachable
through the FHIR surface by direct id (500), by bare search (filtered to the bound patient), and
even by explicit `?patient=` query injection (silently overridden to the bound patient) — so the
audit's #1 IDOR finding is answered by construction, as designed. The single required edit to §5
is to name the mint mechanism precisely: it is a **SMART EHR-launch authorization_code flow**
(first-party trusted client + launch context for the open patient + skip-consent app setting),
not a silent server-side mint. Track two follow-ups into `-03b`: (1) the client-enable / trust /
skip-consent steps become install-time configuration the module must own or document; (2) the
agent's FHIR client must fail-closed on the non-standard 500 denial surface.

### Reproduce
```bash
# 1. register + enable client, 2. mint token (interactive flow, no browser):
python3 tmp/spike/flow.py               # writes tmp/spike/token_response.json
# 3. positive / negative / search / query-injection evidence:
TOKEN=$(python3 -c "import json;print(json.load(open('tmp/spike/token_response.json'))['access_token'])")
FB=https://localhost:9300/apis/default/fhir
curl -sk -H "Authorization: Bearer $TOKEN" "$FB/Patient/a2325b4d-a20e-41a6-9a3e-f8b9f4f38e75"   # 200
curl -sk -H "Authorization: Bearer $TOKEN" "$FB/Patient/a2325b4e-94dc-4e89-b1d2-9d2304d646f9"   # 500 denied
curl -sk -H "Authorization: Bearer $TOKEN" "$FB/Condition?patient=a2325b4e-94dc-4e89-b1d2-9d2304d646f9"  # returns P1's, not P2's
```
