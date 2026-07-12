# oe-module-ai-copilot

The AgentForge Clinical Co-Pilot's OpenEMR half: a docked chat sidebar mounted into the EHR shell,
and the SMART client that mints the patient-scoped token the agent reads under.

All agent logic — orchestration, tool-calling, the verification gate, observability — lives in the
standalone Python service under `/agent`. This module contains **no agent logic**. It does two
things: render the sidebar, and obtain a token bound to exactly one patient.

See `ARCHITECTURE.md` §4/§5, `context/decisions/deployment-strategy.md` (Option D), and
`context/specs/copilot-sidebar.md` (the docked-sidebar UI design).

---

## UI: a docked sidebar in the shell

The panel is a VS Code-style **docked sidebar** on the right of the EHR, toggled by a button in the
patient banner (next to DOB/Age). It is mounted into OpenEMR's **outer shell**
(`interface/main/tabs/main.php`) via `Main\Tabs\RenderEvent::EVENT_BODY_RENDER_POST`, so it persists
across every chart sub-view (Dashboard → History → …) without reloading. Assets are enqueued onto
the shell via `ScriptFilterEvent`/`StyleFilterEvent` (pageName `main.php`).

- `Bootstrap.php` wires the three shell events; `Controller/CopilotSidebarController.php` renders the
  sidebar shell + a JSON config island.
- `public/assets/js/ai-copilot.js` drives it: banner-button injection (via a `MutationObserver` on
  `#attendantData`), open/close, left-edge resize, push layout (a body **width cap**,
  `width: calc(100vw - var(--ai-copilot-width))` — a plain `margin-right` is silently defeated by the
  theme's `min-width:100vw` pin), the active pid read from the shell's Knockout observable,
  patient-switch reset, and the chat/launch flow.
- The sidebar carries **no patient id**; the active patient is resolved client-side and every turn
  is scoped by the token's own `patient` claim.
- Width + open/closed persist in `localStorage` (a non-PHI UI preference).
- **Conversation persistence (server-side, per user+patient) is designed in the spec but deferred**
  — conversations are currently in-memory (they reset on reload). The JS carries clearly marked
  Phase 3 hooks (`loadThread`/`persistTurn`/`clearThread`) and the config island already exposes the
  `conversationUrl`.

An earlier revision mounted the panel as a Dashboard card inside the demographics iframe; that is
superseded by the sidebar (see `context/execution/implementation-prompt-03b-copilot-widget.md`).

---

## Token flow

```
physician clicks the banner "Co-Pilot" toggle / sends a turn
  │
  ▼
hidden iframe ──► public/launch.php
                    • verifies CSRF, reads pid from the *session* (never a query param)
                    • pid → FHIR Patient UUID
                    • mints a SMARTLaunchToken carrying that patient
                    • seals {PKCE verifier, expected patient UUID} into the encrypted
                      OAuth `state` (LaunchStateCodec, 300s TTL) — no server-side session write
                    • 302 ──► /oauth2/default/authorize?launch=…&aud=…&iss=…&state=…
                                │
                                │  first-party session cookie ⇒ skip login/consent
                                ▼
                              302 ──► public/callback.php?code=…&state=…
                                        • decrypts + authenticates `state` to recover the verifier
                                          and expected patient (also session-read-only)
                                        • exchanges the code server-side (client_secret stays in PHP)
                                        • asserts token.patient == the chart's patient
                                        • postMessage ──► chart page
  ▼
sidebar holds {accessToken, patient} ──► POST {AGENT}/chat
                                        Authorization: Bearer <token>
                                        {"patient_id": <token.patient>, "message": …}
```

The EHR never navigates. The whole OAuth redirect chain happens inside a hidden iframe, and the
token crosses back over `postMessage` pinned to OpenEMR's own origin.

### Two properties worth stating plainly

**The panel never chooses the patient id.** `ChatRequest.patient_id` is the `patient` claim of the
token itself, not a value read from the page. The id the agent is asked about and the id the token
permits are therefore the same by construction, and cannot drift.

**Every non-2xx is a hard denial.** OpenEMR returns a bare `HTTP 500` for a denied cross-patient
FHIR read, not a clean `403` (`context/execution/smart-token-spike-findings.md` §1). `TokenExchanger`
and the agent's FHIR client both treat *any* non-2xx as failure; neither special-cases a status code.

---

## One-time admin prerequisites

Installer automation is deliberately out of scope for this pass. These are manual, one-time steps.

### 1. Register the OAuth2 client

Administration → Config → **Connectors**, or `interface/smart/register-app.php`.

- **Confidential client** (`token_endpoint_auth_method = client_secret_post`)
- **Redirect URI** — must byte-match `ModuleUrls::callbackUrl()`:
  `{site_addr_oath}/interface/modules/custom_modules/oe-module-ai-copilot/public/callback.php`
- **Scopes** — exactly the set in `src/Smart/CopilotScopes.php`:

  ```
  openid fhirUser online_access launch
  patient/Patient.read patient/Condition.read patient/MedicationRequest.read
  patient/AllergyIntolerance.read patient/Encounter.read patient/DocumentReference.read
  ```

> **`launch`, never `launch/patient`.** Two independent reasons, both verified against core:
>
> - `SMARTAuthorizationController::needSMARTAuthorization()` tests
>   `str_contains($scopes, 'launch/patient')` — a *substring* match on the raw scope string.
>   Registering `launch/patient` re-triggers the interactive patient-select picker, which is exactly
>   what an EHR launch exists to bypass.
> - `SMARTSessionTokenContextBuilder::getContextForScopes()` only copies the launch token's patient
>   into the token response's `patient` claim when **`launch`** is among the *granted* scopes.
>   Without it the token carries no patient binding at all.
>
> And `AuthorizationController::processAuthorizeFlowForLaunch()` overrides the request's scopes with
> whatever is registered on the `oauth_clients` row — so the registration *is* the scope set. The
> `scope` query parameter cannot widen or narrow it.

### 2. Enable the client

Newly registered clients are disabled (`oauth_clients.is_enabled` defaults to `0`).
Administration → Config → Connectors → **Enable**.

### 3. Turn on the authorization-flow skip

Both switches are required (`AuthorizationController::shouldSkipAuthorizationFlow()`):

| Switch | Where | Default |
|---|---|---|
| `oauth_ehr_launch_authorization_flow_skip` (global) | Administration → Globals → Connectors, *"OAuth2 EHR-Launch Authorization Flow Skip Enable App Setting"* | already `1` |
| `oauth_clients.skip_ehr_launch_authorization_flow` (per client) | Connectors → **Enable Authorization Flow Skip** | `0` |

Without these the physician re-consents on every launch.

### 4. Confirm the API globals

`rest_api = 1` and `rest_fhir_api = 1`. The panel stays hidden if `rest_fhir_api` is off.

---

## Configuration

Secrets live in the environment — never in the database, never in this repository
(`AUDIT.md` secrets-hygiene finding). Reading them is confined to `CopilotConfig::fromEnvironment()`.

| Variable | Required | Purpose |
|---|---|---|
| `AI_COPILOT_CLIENT_ID` | yes | OAuth2 client id from step 1 |
| `AI_COPILOT_CLIENT_SECRET` | yes | OAuth2 client secret. Server-side only; never reaches the browser |
| `AI_COPILOT_AGENT_URL` | yes | Browser-reachable agent base URL, e.g. `http://localhost:8000` |
| `AI_COPILOT_OAUTH_INTERNAL_BASE` | no (`http://localhost`) | How the *container* reaches OpenEMR's own token endpoint |

That last one is not redundant. `ServerConfig::getTokenUrl()` returns the browser-facing address
(`http://localhost:8301/…`), which resolves to nothing from inside the OpenEMR container — Apache
listens on port 80 there. The browser gets the public authorize URL; PHP's server-to-server token
exchange gets the internal one.

If the required variables are absent the panel does not render at all, rather than mounting a widget
whose first interaction is guaranteed to fail.

---

## Local demo runs over HTTP, on purpose

The dev stack serves OpenEMR on **both** `http://localhost:8301` and `https://localhost:9301`. The
demo uses the HTTP origin, and `site_addr_oath` is set to match.

The reason is mixed content. A `fetch()` from an HTTPS page to an HTTP origin is blocked by the
browser *before* CORS is ever consulted, so the widget's call to a plain-HTTP agent would fail no
matter how the agent's CORS is configured. The alternatives are worse for a demo: a self-signed cert
on the agent does not prompt for `fetch()` (browsers only show the click-through interstitial for
top-level navigation), so the call fails with an opaque network error until someone manually visits
the agent's origin and accepts the certificate.

**This is a local-development artifact, not the production posture.** In production both services sit
behind Railway's edge, which terminates real TLS for each. Mixed content never arises there; CORS is
the only thing needed, and the HTTP demo exercises exactly that mechanism because
`http://localhost:8301` → `http://localhost:8000` is still cross-origin.

`AUDIT.md` already records that OpenEMR enforces no transport security itself and treats TLS as the
operator's responsibility. Nothing here changes that finding either way.

Set the matching origin on the agent side:

```bash
COPILOT_CORS_ORIGINS=http://localhost:8301
```

`cors_origins` defaults to empty — no browser origin is allowed unless named. It never defaults to
`*`, which would let any page on the internet spend a stolen token.

---

## Token lifetime

`online_access`, no refresh token, 3600s. On expiry (or on a `401` from the agent) the panel silently
re-runs the launch chain. Requesting `offline_access` would buy silent renewal at the cost of holding
a months-long refresh token — a much larger secret than a one-hour bearer, and a worse trade against
the audit posture. See `context/execution/smart-token-spike-findings.md` §5.

---

## Known sharp edges in core

Found while building this; none are blocking, all are worth knowing.

- **The launch token has no expiry, no nonce, and no single-use enforcement.**
  `SMARTLaunchToken::serialize()` is `base64(encryptStandard(json))` and its own comment reads
  *"no security is really needed here"*. It is opaque and tamper-evident, not unforgeable-once. The
  replay defences are that the client must be registered and enabled, and that the skip-auth path
  requires a live core session. Our `state` + PKCE pair is single-use on top of it.
- **`ClientEntity::getLaunchUri()` concatenates `?launch=…` onto the registered launch URI** with no
  `?`-vs-`&` awareness. We sidestep it entirely by navigating the iframe straight to `/authorize`,
  so this module needs no `initiate_login_uri` at all.
- **`needSMARTAuthorization()` substring-matches the raw scope string** — see the `launch/patient`
  warning above.
- **The EHR-launch skip path only works for provider sessions**, not patient-portal logins
  (`AuthorizationController.php:1853`).
