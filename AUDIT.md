# OpenEMR System Audit

**Scope:** A general system audit of the existing OpenEMR installation in this repository, across five dimensions — Security, Performance, Architecture, Data Quality, and Compliance & Regulatory. Findings are grounded in the actual code, schema (`sql/database.sql`), and the live development database in this tree; file paths are repo-relative. Severity ratings follow each finding.

**Method:** Five parallel audits reading real source and, for data quality, the running dev database (MariaDB, 25 patients / 1,186 encounters — Synthea-generated synthetic seed data). Where a finding is schema/code-only it is marked as such.

---

## Executive Summary

OpenEMR is a ~20-year-old PHP electronic health record (EHR) system, mid-migration from a procedural, `$GLOBALS`-driven architecture to a modern PSR-4 / Symfony stack — both paradigms sharing one request lifecycle, one global-state bag, and a single DB connection. Overall verdict: **a genuinely mature security and compliance core, undercut by a few specific, addressable gaps and a large legacy-debt surface.** *(Acronyms are expanded in the [Glossary](#glossary-of-acronyms) below.)*

**Security.** The crypto and auth cores are strong and modern — bound-parameter DB access, AES-256 encrypt-then-MAC, bcrypt/Argon2 hashing, timing-attack mitigation, dual brute-force throttling, MFA, and an exemplary CSRF/escaping toolkit. The risks are narrow but real: **(1) authorization is role-scoped, not patient-scoped — any authenticated user with a capability can fetch any patient by `pid` (IDOR, High); (2) GitHub PAT-format tokens are committed to docker-compose files (High); (3) both template engines ship with autoescaping OFF, leaving XSS defense to manual `text()`/`attr()` discipline (Medium); (4) the brute-force counter resets via a spoofed `X-Forwarded-For` header (Medium).**

**Compliance.** OpenEMR has real HIPAA machinery: an audit engine that logs PHI *reads*, not just writes, by default; ATNA/syslog support; disclosure accounting; break-the-glass; encounter-level data-sensitivity access controls; and mature at-rest crypto. The gaps: **audit-log integrity uses an unkeyed SHA3 hash in the same mutable DB (forgeable — High); no in-code TLS/HSTS enforcement (Medium); a loose 2-hour auto-logoff default; patient deletion is a hard purge with no retention policy.** Several outbound PHI paths — FHIR/EHI export, Direct messaging, eRx, fax — already constitute a BAA surface.

**Performance.** The dominant runtime cost is **audit-on-read write amplification** — by default every clinical `SELECT` triggers two hashed audit INSERTs — compounded by **pervasive N+1 queries** from uncached `list_options` lookups (a patient summary fires 40–60+ queries). Indexing gaps worsen it: `patient_data` has **no PRIMARY KEY**, and the unbounded `log` table is unindexed on `date`/`user`. There is no Twig compile cache and no query-result cache.

**Data Quality.** DB-level `NOT NULL default ''` makes completeness enforcement cosmetic — real validation exists only on the API path (`PatientValidator`, 4 fields), not the legacy UI. Medications use a bare-integer RxNorm scheme inconsistent with the prefixed `SNOMED-CT:` coding on problems/allergies; there is **no patient de-duplication** beyond system-generated keys; and ICD-10 terminology tables are empty.

**Architecture.** The clean, well-adopted extension path is the **event-driven module system** (`oe-module-*` + Symfony `EventDispatcher` + `RestApiCreateEvent` route injection). The main constraint: the DI container is inert on the web path, so services reach infrastructure via service location on the globals bag, not constructor injection — the biggest testability limitation.

**Top priorities:** (1) patient-level authorization to close the IDOR gap; (2) rotate and remove committed secrets; (3) keyed-HMAC or append-only audit-log integrity; (4) enable template autoescaping; (5) index remediation + audit-on-read tuning for latency.

---

## Glossary of Acronyms

Every acronym used in this document, expanded. Terms are used bare in the prose below.

### Clinical & Regulatory

| Term | Meaning |
|------|---------|
| ATNA | Audit Trail and Node Authentication — a healthcare audit-logging standard |
| BAA | Business Associate Agreement — the HIPAA contract a vendor must sign before it may handle PHI |
| CCDA | Consolidated Clinical Document Architecture — a standard clinical-document exchange format |
| CDR | Clinical Decision Rules |
| CFR | Code of Federal Regulations — the codified US federal regulations (used in legal citations) |
| CQM | Clinical Quality Measures |
| EHI | Electronic Health Information |
| EHR | Electronic Health Record |
| eRx | Electronic prescribing |
| FHIR | Fast Healthcare Interoperability Resources — the standard health-data exchange API |
| HIPAA | Health Insurance Portability and Accountability Act — the US health-data privacy/security law |
| ICD-10 | International Classification of Diseases, 10th revision — standard diagnosis/procedure codes |
| IHE | Integrating the Healthcare Enterprise — a healthcare-interoperability standards initiative |
| LBF | Layout-Based Forms — OpenEMR's user-customizable form definitions |
| ONC | Office of the National Coordinator for Health IT — the US federal EHR-certification authority |
| PHI | Protected Health Information — identifiable patient data |
| RxNorm | A normalized US drug naming/coding vocabulary |
| RXCUI | RxNorm Concept Unique Identifier |
| SMART | SMART on FHIR — the health-app authorization standard |
| SNOMED CT | Systematized Nomenclature of Medicine — Clinical Terms; a standardized clinical vocabulary |
| USCDI | US Core Data for Interoperability — the federally-required clinical data set |
| US Core | A US-specific FHIR data profile / implementation guide |

### Security & Cryptography

| Term | Meaning |
|------|---------|
| ACL | Access Control List |
| ACO | Access Control Object — a phpGACL permission target |
| AES | Advanced Encryption Standard (AES-256 = 256-bit key) |
| bcrypt / Argon2 | Modern password-hashing algorithms |
| CA | Certificate Authority |
| CBC | Cipher Block Chaining — an AES cipher mode |
| CSP | Content Security Policy |
| CSRF | Cross-Site Request Forgery |
| GACL / phpGACL | (PHP) Generic Access Control List — the permissions engine |
| HMAC | Hash-based Message Authentication Code |
| HSTS | HTTP Strict Transport Security |
| HTTPS | HTTP Secure — HTTP over TLS |
| IDOR | Insecure Direct Object Reference — reaching another user's record by changing an ID in the request |
| IV | Initialization Vector — randomizes each encryption |
| JWT | JSON Web Token — a signed, self-contained auth token |
| LDAP | Lightweight Directory Access Protocol |
| MAC | Message Authentication Code |
| MFA | Multi-Factor Authentication |
| OAuth | Open Authorization |
| PAT | Personal Access Token |
| ROPC | Resource Owner Password Credentials — an OAuth grant type that passes the password directly to the app |
| RSA | Rivest–Shamir–Adleman — a public-key cryptosystem |
| SHA / SHA3 | Secure Hash Algorithm (family; e.g. SHA3, SHA-512, SHA-384) |
| TDE | Transparent Data Encryption — encryption applied at the database-storage layer |
| TLS | Transport Layer Security |
| TOTP | Time-based One-Time Password (e.g. an authenticator app) |
| U2F | Universal 2nd Factor — hardware security keys |
| XFF | X-Forwarded-For — the HTTP header naming the originating IP behind a proxy |
| XSS | Cross-Site Scripting |

### Software & Infrastructure

| Term | Meaning |
|------|---------|
| ADODB | Active Data Objects for Data Base — the legacy PHP database-abstraction library |
| APCu | Alternative PHP Cache (user) — an in-memory cache |
| API | Application Programming Interface |
| CLI | Command-Line Interface |
| CORS | Cross-Origin Resource Sharing |
| DB | Database |
| DBAL | Database Abstraction Layer (Doctrine) |
| DI | Dependency Injection — supplying a class's collaborators from outside rather than having it construct them |
| HTTP | HyperText Transfer Protocol |
| InnoDB / MyISAM | MySQL storage engines (InnoDB is the modern, row-locking, crash-safe one) |
| JSON | JavaScript Object Notation |
| MVC | Model-View-Controller |
| N+1 | Query antipattern: one query per row in a loop instead of one batched query |
| ORM | Object-Relational Mapping — maps database rows to objects |
| PSR | PHP Standard Recommendation (e.g. PSR-4 autoloading, PSR-11 container interface) |
| REST | Representational State Transfer |
| SQL | Structured Query Language |
| TCP | Transmission Control Protocol |
| UUID | Universally Unique Identifier |
| VNC | Virtual Network Computing |

---

## Table of Contents

1. [Security Audit](#security-audit)
2. [Compliance & Regulatory Audit](#compliance--regulatory-audit)
3. [Performance Audit](#performance-audit)
4. [Data Quality Audit](#data-quality-audit)
5. [Architecture Audit](#architecture-audit)

**Severity legend:** Critical / High / Medium / Low / Informational. In security and compliance sections, "Critical/High" denotes a clear exploitable or regulatory gap; performance uses High/Medium/Low impact; data quality uses reliability-risk severity.

---

## Security Audit

This audit is grounded in the actual code in this repository. OpenEMR shows a genuinely mature security core (parameterized DB layer, authenticated encryption, a fully-featured auth stack, a well-designed CSRF and output-escaping toolkit). The material risks cluster in three places: authorization is role-scoped rather than patient-scoped, both template engines ship with autoescaping OFF, and several credentials/keys are committed to the repo.

### Top findings (most severe first)

- **High — No patient-level access scoping (IDOR).** The ACL model grants global capabilities (`patients|demo`, etc.); `pid` is accepted straight from `$_GET`/`$_POST` and no check verifies the user may see *that* patient. Any authenticated user with the relevant capability can pull any patient by id. (`interface/globals.php:770-774`, `src/Common/Session/PatientSessionUtil.php:44-81`)
- **High — Committed GitHub PAT-format tokens in the repo.** `ghp_`-format tokens (plus base64/decimal-encoded variants) are hardcoded in four docker-compose files. Real committed credentials — rotate. (`docker/development-easy/docker-compose.yml:75-77` + 3 mirrors)
- **Medium — Template autoescaping globally disabled.** Twig is built with `autoescape => false` (`src/Common/Twig/TwigContainer.php:70`) and Smarty has no `escape_html`; XSS defense rests entirely on developers manually calling `text()`/`attr()`. A forgotten filter is an XSS, not a caught error.
- **Medium — Per-IP brute-force counter bypass via `X-Forwarded-For`.** The lockout key `ip_string` concatenates the client-controllable `HTTP_X_FORWARDED_FOR`; rotating that header yields a fresh failure counter, defeating the per-IP throttle. (`library/sanitize.inc.php:35-38`, keyed into `ip_tracking` in `AuthUtils`)
- **Medium — REST skip-auth prefix match has reversed arguments.** `str_starts_with($route, $pathInfo)` tests whether the request path is a *prefix of* a whitelist route (inverse of intent); an empty/partial path can match and bypass authentication. Low current exploitability (no data-serving route affected) but a real auth-bypass logic defect. (`src/RestControllers/Authorization/SkipAuthorizationStrategy.php:56-61`)
- **Medium — Core session cookie is not `Secure` and not `HttpOnly`.** `forCore()` leaves `cookie_secure=false` (builder default) and explicitly sets `HttpOnly=false`, so the core session cookie is JS-readable (XSS → session theft) and can traverse plain HTTP. No app-wide HSTS/CSP either. (`src/Common/Session/SessionConfigurationBuilder.php:26-27,83-90`)

### 1. Authentication

**Strengths (well done):**
- Password hashing is configurable and modern: bcrypt / Argon2i / Argon2id / SHA-512-crypt, resolved safely with fallbacks (`src/Common/Auth/AuthHash.php:34-133`). Verify uses `password_verify` and `hash_equals` for the SHA-512 path (`AuthHash.php:187-234`); `passwordNeedsRehash` transparently upgrades stored hashes on successful login (`AuthUtils.php:449-458`).
- Timing-attack mitigation: a persisted dummy hash makes non-existent-user and wrong-password paths take equal time (`AuthUtils.php:94-113, 1406-1416`).
- Dual brute-force controls — per-username and per-IP counters with configurable auto-reset windows and admin email notification (`AuthUtils.php:1163-1309`).
- MFA (TOTP + U2F) with per-user registrations, secrets stored via `PasswordBasedCrypto` (`src/Common/Auth/MfaUtils.php:20-60`).
- Password history reuse prevention (up to 4 prior hashes), min/max length, and strength policy (`AuthUtils.php:726-750, 1017-1080`).
- Session fixation is handled: session ID is regenerated after login via `$session->migrate(true)` (`interface/main/main_screen.php:403`). Session validity is re-checked each request against the live DB hash with `hash_equals` (`AuthUtils.php:837-861`).
- Passwords passed by reference and wiped with `sodium_memzero` after use (`AuthUtils.php:1427-1434`).
- LDAP path supports TLS with CA/client-cert enforcement (`AuthUtils.php:909-961`).

**Weaknesses:**
- **XFF brute-force bypass (Medium)** — as in Top Findings; `collectIpAddresses()` embeds raw `HTTP_X_FORWARDED_FOR` into the `ip_tracking` key. *Remediation:* derive the throttle key from `REMOTE_ADDR` only (or a trusted-proxy-validated forwarded IP), and store XFF separately for logging.
- **Google Sign-In bypasses the login counters and MFA (Low/Medium).** `verifyGoogleSignIn()` establishes a full session on a valid Google token without consulting the failed-login counters or MFA flow (`AuthUtils.php:1443-1517`). Trust is delegated wholly to Google Workspace; acceptable if intended, but note MFA enforced in OpenEMR is skipped on this path.
- **Timing attack note:** the dummy hash lives in the `globals` table (`hidden_auth_dummy_hash`); fine, but it means the anti-timing baseline is a shared static value.

### 2. Authorization / Access Control

**Strengths:** `AclMain::aclCheckCore()` is fail-closed — empty ACL result returns `false` and deny overrides allow (`src/Common/Acl/AclMain.php:166,182-237`). phpGACL namespaced into `src/Gacl/`. REST/FHIR uses `league/oauth2-server` correctly: `ResourceServer::validateAuthenticatedRequest()` for JWT signature/expiry, layered DB token-revocation and trusted-session checks, and a **fail-closed default deny** in the strategy chain (`src/RestControllers/Subscriber/AuthorizationListener.php:237-251`). Scope enforcement matches against **token-embedded** scopes via structured SMART `containsScope()` (`src/Common/Http/HttpRestRequest.php:373-380`), and `ScopeRepository::finalizeScopes()` only grants scopes the client registered with — a solid anti-escalation control (`src/Common/Auth/OpenIDConnect/Repositories/ScopeRepository.php:137-188`). System wildcard scopes are config-gated. A record-level data-sensitivity control also exists: the `sensitivities` ACO (`Normal`/`High`, provisioned at install in `library/classes/Installer.class.php:924`) gates encounters flagged `form_encounter.sensitivity='high'`, enforced centrally in `EncounterService` via `AclMain::aclCheckCore('sensitivities', …)` (`src/Services/EncounterService.php:449`).

**Weaknesses:**
- **No patient-level scoping / IDOR (High)** — Top Findings. The model (`AclMain.php:36-52`) has no per-patient/per-provider ACO — the one record-level ACO that exists (`sensitivities`, gating `sensitivity='high'` encounters) is a data-classification filter, not patient-ownership scoping. `setpid()` call sites (`library/ajax/set_pt.php:27`, `demographics.php:86`) apply CSRF + `intval()` but no ownership check. A `ViewEvent` hook exists (`demographics.php:1054-1058`) but core ships no handler, so default posture is "any authenticated user with the capability sees all patients." *Remediation:* add a patient-authorization layer (care-team/facility scoping) enforced centrally on pid resolution.
- **Enforcement is per-page and manual (Medium).** No central middleware guarantees an ACL check; a page missing its `aclCheckCore` guard is silently open. `interface/globals.php` bootstraps auth but does not enforce a resource ACL.
- **REST skip-auth reversed match (Medium)** — Top Findings, `SkipAuthorizationStrategy.php:56-61`. *Remediation:* `str_starts_with($pathInfo, $route)` with exact-segment matching.
- **Broad superuser short-circuit (Informational).** `admin|super` returns `true` for every ACO unconditionally (`AclMain.php:174`).
- **Dual scope-check implementations (Low).** Legacy `RestConfig::scope_check()` string match (`src/RestControllers/Config/RestConfig.php:201-220`) coexists with the new `ScopeEntity` path — drift risk; consolidate. `RestConfig::authorization_check()` does a bare `exit()` on ACL failure for non-REST calls (`RestConfig.php:189-190`).

### 3. Data Exposure Vectors

**SQL injection — mostly safe by design.** Public query functions delegate to `QueryUtils`, which uses ADODB **real bound parameters**: `getADODB()->Execute($statement, $binds, true)` (`src/Common/Database/QueryUtils.php:222`; wrappers `library/sql.inc.php:96,241,262`). A mature identifier-escaping toolkit exists for contexts that can't be bound — `add_escape_custom`, `escape_limit`, `escape_sort_order`, `escape_sql_column_name` (whitelists against live `SHOW COLUMNS`), `escape_table_name`, `escape_identifier` (`library/formdata.inc.php:24-210`). *Residual risk (Low/Medium):* legacy files that hand-assemble entire statements from string fragments with no binds — e.g. `interface/patient_file/encounter/find_code_dynamic_ajax.php:296-298` (`sqlStatement("SELECT $sellist FROM $from $where1 $orderby")`) and `interface/billing/edit_payment.php:214-249`. Each fragment *appears* routed through `add_escape_custom`/`escape_*` upstream, but safety depends on every branch escaping — worth a targeted review/refactor to bound params.

**XSS — strong helpers, no automatic backstop (Medium, structural).** `library/htmlspecialchars.inc.php` provides a comprehensive, context-correct, null-safe escaper set: `text()` (ENT_NOQUOTES, 234), `attr()` (ENT_QUOTES, 291), `xlt`/`xla`, `js_escape()` (json_encode, 46), `attr_js()`, `js_url()`, plus `safe_href()` with a scheme allowlist blocking `javascript:`/`data:` (85-125) and `csvEscape()`. But **Twig `autoescape=false`** (`src/Common/Twig/TwigContainer.php:70`) and **Smarty has no `escape_html`** — so `{{ var }}`/`{$var}` emit raw. Direct raw superglobal echo is essentially absent (only non-exploitable ternary uses found), but 32 `|raw` filters exist, concentrated in the calendar module; `CATEGORY_OPTIONS`/`PROVIDER_OPTIONS` pre-rendered `<option>` lists (`templates/calendar/default/user/ajax_search.html.twig:51-87`) combine `|raw` with potentially admin-editable names and warrant a manual exploitability check. *Remediation:* enable Twig autoescape (allowlist genuine HTML fragments with `|raw`) or add a default output filter.

**CSRF — solid (Strength).** HMAC-SHA256 tokens derived from a per-session 32-byte random key, truncated to 40 chars, compared with `hash_equals`; `checkCsrfInput()` centralizes extraction+verify with a `dieOnFail` legacy mode (`src/Common/Csrf/CsrfUtils.php:35-133`). Design is sound; coverage is per-endpoint (each form must call it), so completeness depends on call-site discipline.

**Error/debug leakage:** code consistently logs internals via `error_log`/PSR-3 and returns generic user messages (e.g. CSRF returns `xlt('Authentication Error')`); no systemic message leakage observed.

### 4. PHI Handling

**Encryption at rest — strong primitive (Strength).** `CryptoGen` uses AES-256-CBC with **encrypt-then-MAC** (HMAC-SHA384) and constant-time verification, two independent key sets (DB `keys` table + drive `documents/logs_and_misc/methods/`), drive keys encrypted by DB keys, and versioned key/algorithm support for migration (`src/Common/Crypto/CryptoGen.php:173-264`). Keys are 256-bit from `random_bytes`, round-tripped on creation.

**Weaknesses:**
- **Column encryption is opt-in, off by default (Medium).** `shouldEncryptForDatabase`/`shouldEncryptForFilesystem` come from `database_encryption`/`drive_encryption` globals (`CryptoGen.php:81-84`), and "not all code uses the `encryptForDatabase` path" (class comment 62-66). So most PHI columns are stored plaintext in MySQL unless TDE or these flags are deliberately enabled.
- **Audit-log comments are no longer encrypted (Medium).** `recordLogItem()` only base64-encodes comments — the encryption path was removed (`src/Common/Logging/EventAuditLogger.php:660-664`). base64 is not confidentiality.
- **PHI in URLs, then logged (Low/Medium).** `pid` and other identifiers travel in `$_GET`; the HTTP-request audit logger records the full `QUERY_STRING` into log comments (`EventAuditLogger.php:720-733`). PHI in GET params also lands in web-server access logs outside the app's control.
- **No application-enforced TLS/HSTS (Medium).** No `Strict-Transport-Security` anywhere; only the login page sets anti-clickjacking headers (`X-Frame-Options: DENY` + CSP `frame-ancestors 'none'`, `interface/login/login.php:31-32`). Transport security is left entirely to deployment. Combined with the core session cookie lacking `Secure`, this is a real exposure if TLS isn't terminated correctly.

### 5. Secrets & Configuration

- **Committed GitHub PAT tokens (High)** — Top Findings; `docker/development-easy/docker-compose.yml:75-77` and mirrors in `development-easy-light`, `development-easy-redis`, `development-insane`. Rotate and move to build secrets.
- **`sites/default/sqlconf.php` committed with working defaults, not gitignored (Low/Medium).** Ships `$login='openemr'`, `$pass='openemr'` (`sqlconf.php:6-10`); `git check-ignore` returns nothing, so a developer editing it in place risks committing real production DB creds. *Remediation:* gitignore per-site `sqlconf.php` and ship a `.template`.
- **Committed private key material (Low — dev/CI fixtures).** RSA private keys under `docker/library/{sql,couchdb,ldap}-ssl-certs-keys/**/*-key.pem`, `ci/nginx/dummy-key`, and `tests/Tests/data/Unit/Common/Auth/Grant/openemr-rsa384-private.key`. Scoped to local dev/CI, but real key material in the repo.
- **Default dev/prod credentials (Informational, but note prod).** `root/root`, `openemr/openemr`, `admin/pass`, couchdb `password`, VNC `openemr123` in `docker/development-*`; the same `root`/`openemr`/`pass` defaults also appear in `docker/production/docker-compose.yml:37-41`. Dev compose also enables `oauth_password_grant` (ROPC) which is generally discouraged. Ensure prod deployments override all of these.

**Overall:** the cryptographic and authentication cores are strong and modern; CSRF and the escaping library are exemplary. The priorities for remediation are (1) introduce patient-level authorization to close the IDOR gap, (2) rotate and remove committed secrets, (3) enable template autoescaping, and (4) harden the brute-force key and session-cookie `Secure`/`HttpOnly`/HSTS posture.

---

## Compliance & Regulatory Audit

*Scope: HIPAA Security Rule (45 CFR §164.308/.312), Privacy Rule disclosure accounting (§164.528), and ONC certification posture, assessed against the code actually present in this tree.*

### Top findings

- **Strong, purpose-built audit engine exists and is on by default.** `src/Common/Logging/EventAuditLogger.php` + the `Audit\*` sink classes write every logged event to `log` / `log_comment_encrypt` / `api_log`, with `enable_auditlog` and `audit_events_query` both defaulting to `1` — so PHI **reads** (SELECTs), not just writes, are captured out of the box. This is real §164.312(b) machinery, credit where due.
- **Log integrity is checksummed but not cryptographically tamper-*resistant*.** `LogTablesSink` stores a plain `sha3-512(...)` hash (not a keyed HMAC) in `log_comment_encrypt`, in the same mutable database. `interface/reports/audit_log_tamper_report.php` recomputes and compares it — good for detecting naive edits, but anyone with DB write access can recompute the hash and forge silently. **High** integrity gap under §164.312(c)(1).
- **Patient deletion is a hard purge, largely unaudited by design.** `interface/patient_file/deleter.php` issues real `DELETE FROM patient_data/lists/history_data/immunizations/...` — data is destroyed, not flagged. Good for §164.310(d)(2) disposal, but eliminates any post-hoc disclosure/access reconstruction for that patient and there is no documented data-retention policy or configurable retention window.
- **Transport security is not enforced in-tree.** No forced-HTTPS redirect (no HTTPS `RewriteRule` in `.htaccess`), no HSTS header, and `SessionConfigurationBuilder` defaults `cookie_secure => false`. TLS is left to the deployment. §164.312(e)(1) is "addressable," but the app ships no enforcement.
- **Automatic-logoff default is loose.** `timeout` defaults to **7200s (2 hours)** for the main app (`library/globals.inc.php`), long for an unattended clinical workstation under §164.312(a)(2)(iii).
- **Multiple existing outbound PHI paths already constitute a BAA surface** — FHIR/REST + Bulk Data export, EHI export, phiMail Direct messaging, WENO eRx, FaxSMS, Comlink telehealth. Certification machinery (FHIR R4 / US Core 8.0 / USCDI / SMART v2.2) is present and self-attested compliant.

### 1. Audit logging — §164.312(b) (central)

**Strength — comprehensive event capture.** `src/Common/Logging/EventAuditLogger.php` is a singleton audit engine fed by a `MultiSink` (`src/Common/Logging/Audit/`). `auditSQLEvent()` (lines 405–525) hooks the DB layer: it classifies each statement (`select/update/insert/delete/replace`), maps the touched table to an event category via the `LOG_TABLES` map (lines 107–174, covering `patient_data`, `lists`, `forms`, `immunizations`, `procedure_*`, `insurance_data`, `prescriptions`, GACL tables, etc.), and records it. Writes are logged; **reads are logged too** when `audit_events_query` is enabled — and its default is `1` (`library/globals.inc.php:2832`), so PHI-access read logging is on by default. `enable_auditlog` also defaults to `1` (`:2778`). Auth failures route through `logAuthFailure()` (lines 233–243); HTTP requests through `logHttpRequest()` (lines 700–734, gated by `audit_events_http-request`).

**Storage & sinks.** `Audit\LogTablesSink` writes to `log` (schema `sql/database.sql:7758`), `log_comment_encrypt`, and `api_log` (`:92`), deliberately on a **separate DB connection** (`EventAuditLogger.php:42-44`) to avoid autoincrement cross-talk. Comments are `base64`-encoded before storage (`EventAuditLogger.php:664`).

**Strength — ATNA / syslog support.** `enable_atna_audit` wires an `Audit\Atna\TcpWriter` + `AtnaSink` over TLS (mutual-cert) to a syslog/ATNA server (`EventAuditLogger.php:49-66`) — a genuine IHE ATNA path for shipping the trail off-box (which also mitigates the tamper concern below). Default is **off** (`:2858`); enabling it is the recommended hardening for tamper-evidence.

**Strength — Privacy Rule disclosure accounting (§164.528).** `recordDisclosure()` / `updateRecordedDisclosure()` / `deleteDisclosure()` (lines 567–626) maintain the `extended_log` table for third-party disclosures — the accounting-of-disclosures mechanism.

**Gaps / limits.**
- **Read-logging is a SQL-string heuristic, best-effort.** `auditSQLEvent()` skips SELECTs on tables not in `LOG_TABLES` (lines 501–505) and matches table names by substring on a truncated statement. Reads via views, unusual joins, or unlisted tables silently escape the trail. **Medium** — completeness gap under §164.312(b).
- **Log retention is unbounded and unconfigurable.** No purge/retention setting exists. Logs grow forever — good for the 6-year HIPAA retention floor, but there is no policy engine, and hard-deleting a patient does not scrub their prior `log` rows, so orphaned `patient_id` references persist. **Informational/Low.**

*Remediation:* Enable `enable_atna_audit` to an append-only external collector for tamper-evidence; document a retention policy; consider service-layer (not SQL-string) read auditing for PHI-view completeness.

### 2. Access controls & authentication (compliance view)

Present and citable (the Security Audit covers depth): **unique user IDs** via `users`/GACL tables; **automatic logoff** via `timeout` (default 7200s) and `portal_timeout` (default 1800s) in `library/globals.inc.php:2113-2122` — the 2-hour main default is loose for §164.312(a)(2)(iii) (**Medium**, tighten to ≤15–30 min). **Emergency access ("break the glass")** is real: `Documentation/Emergency_User_README.txt` + `src/Common/Logging/BreakglassChecker.php`, with `gbl_force_log_breakglass` forcing full audit capture for `breakglass*`/`emergency*` users even when normal audit filters would skip (`EventAuditLogger.php:411,441,516`). **RBAC** via phpGACL (`gacl_*` tables). **Data-sensitivity segregation** is enforced through an encounter-level `sensitivity` flag (`form_encounter.sensitivity`) backed by the `sensitivities` ACO (`Normal`/`High`, provisioned at install, `library/classes/Installer.class.php:924`): `EncounterService` filters flagged-`high` encounters from users lacking the grant via `AclMain::aclCheckCore('sensitivities', …)` (`src/Services/EncounterService.php:449`) — the mechanism for walling off 42 CFR Part 2 (the federal substance-use-disorder confidentiality rule) and other super-confidential encounters (behavioral health, HIV). **Password controls**: `secure_password` (strong, default on), `password_expiration_days` (default 180), `password_grace_time` (default 30).

### 3. Data retention & disposal

**Hard-delete purge.** `interface/patient_file/deleter.php` (`deleter_row_delete()` → `DELETE FROM`, line 82) destroys most clinical data on patient deletion (`patient_data`, `lists`, `history_data`, `insurance_data`, `immunizations`, `transactions`, `forms`/`form_encounter`, lines 223–252). A **mix of soft-delete** exists for some tables (`pnotes.deleted=1`, `documents.deleted=1`, `ar_activity.deleted=NOW()`, `billing.activity=0`) but the demographic/clinical core is purged. Gated by `AclMain::aclCheckCore('admin','super')` **and** `allow_pat_delete` global (lines 217–219) — good access control. **Disposal is compliant (§164.310(d)(2)(i))** but irreversible and not itself recorded in a dedicated deletion audit beyond the per-statement SQL events. Document file blob is flagged-not-deleted (`deleter.php:182`). No configurable retention window. **Backup**: `interface/main/backup.php` / `backuplog.sh` exist; backup encryption/handling is deployment-defined.

### 4. Encryption & transmission

**At-rest — strong (§164.312(a)(2)(iv), addressable).** `src/Common/Crypto/CryptoGen.php` implements `aes-256-cbc` (lines 188–346) with **HMAC** integrity (`hash_hmac`, line 627), random IVs per record, **key versioning** for algorithm migration, and a **two-tier key hierarchy** (separate drive vs database key sets, the drive set encrypted by the database set — header lines 6–13). `PasswordBasedCrypto.php` covers passphrase mode. This is mature, real crypto.

**In-transit — not enforced in code (§164.312(e)(1), addressable).** No forced-HTTPS redirect (`.htaccess` has no HTTPS `RewriteRule`), no `Strict-Transport-Security`/HSTS emission, and `src/Common/Session/SessionConfigurationBuilder.php:26` defaults `cookie_secure => false` (with `SameSite=None` at `:99`). TLS is entirely the operator's responsibility. **Medium** — ship a forced-TLS + HSTS posture and default secure cookies on HTTPS deployments.

### 5. Breach notification & integrity — §164.312(c)(1)

**Integrity control present but weak.** `LogTablesSink` computes `hash('sha3-512', implode('', array_values($logData)))` (`LogTablesSink.php:63,83`) and stores it in `log_comment_encrypt`; `interface/reports/audit_log_tamper_report.php` (lines 241–265) recomputes and flags mismatches. **Limitation:** it is an unkeyed hash, not an HMAC, stored in the same writable DB — so an attacker with DB access can edit a row *and* recompute a valid checksum, defeating detection. It catches accidental/naive tampering only. **High.** *Remediation:* switch log checksums to a keyed HMAC with a key outside the DB, and/or forward to the append-only ATNA sink. **No automated breach-detection/alerting** pipeline exists — tamper detection is a manually-run admin report, not a monitored control supporting timely §164.400-414 breach-notification obligations. **Medium.**

### 6. Third-party / BAA-relevant data flows (existing outbound PHI surface)

PHI already leaves the system through these in-tree paths — each is an existing BAA surface:
- **FHIR R4 / REST API + Bulk Data ($export)** — `FHIR_README.md`, `API_README.md`; patient/Group bulk export.
- **EHI export** — `Documentation/EHI_Export/`, the ONC single-patient electronic-health-information export.
- **phiMail Direct messaging** — `library/direct_message_check.inc.php`, registered as a background service.
- **WENO eRx** — `interface/modules/custom_modules/oe-module-weno` (prescription routing to pharmacies).
- **FaxSMS (EtherFax)** — `interface/modules/custom_modules/oe-module-faxsms`.
- **Comlink telehealth** — `interface/modules/custom_modules/oe-module-comlink-telehealth`.
- **External CCDA validator** — globals default points at `ccda.healthit.gov` (test only; `globals.inc.php:3728` explicitly warns not to transmit PHI there).

*Forward-looking (not a finding on the existing system):* routing any PHI to an external LLM/AI provider would introduce a **new** outbound data flow requiring a signed BAA and a de-identification (§164.514) assessment before enablement — worth flagging now given the direction of travel, but no such flow exists in this tree today.

### 7. Certification context

Present and self-attested in `FHIR_README.md`: **FHIR R4** with **US Core 8.0**, **USCDI v1**, **SMART on FHIR v2.2.0**, **Bulk Data v1.0**, **EHI export**, and **ONC Cures information-blocking** support. This is the ONC-certification-oriented interoperability machinery; the README's "Compliant" claims are self-asserted and were not independently verified against a certification test harness in this audit.

---

## Performance Audit

### Top findings

- **Audit-on-read write amplification is the single biggest runtime cost.** With OpenEMR's default settings (`enable_auditlog=1`, `audit_events_query=1`, `audit_events_patient-record=1` — all defaulted to `'1'` in `library/globals.inc.php:2778/2785/2832`), *every* `SELECT` that touches a clinical table writes **2 INSERTs** (`log` + `log_comment_encrypt`, each with a SHA3-512 hash over base64 comment text) on a separate DB connection. Every non-`NoLog` query is routed through this path (`library/ADODB_mysqli_log.php:50`). A single report render can emit thousands of synchronous audit-table writes.
- **Pervasive N+1 via uncached display helpers.** `getListItemTitle()` (`src/Common/Layouts/LayoutsUtils.php:17`) and `generate_display_field()` (`library/options.inc.php:2361`) each hit `list_options` with no memoization, so any call inside a fetch loop multiplies queries. Rendering one patient summary fires ~40-60+ queries; `appointments_report.php` runs the full CDR rules engine *per appointment*.
- **`BaseService` runs 2 schema-introspection queries on every instantiation** (`SHOW COLUMNS FROM` via `listTableFields` + `getAutoIncrements`, `src/Services/BaseService.php:69-70`) — a fixed per-object DB tax on the entire modern service layer, and it never caches column metadata.
- **Schema indexing gaps on hot columns.** `patient_data` has **no declared PRIMARY KEY** (`id` is only a secondary `KEY`); the unbounded `log` table indexes only `patient_id` (not `date`/`user`/`event`); `billing.encounter`, `audit_master.pid/user_id`, and `documents.encounter_id/list_id` are unindexed; `lists` lacks a `(pid, type)` composite for its dominant query shape.
- **No Twig template cache and no query-result cache.** The Twig `Environment` is constructed without a `cache` option (`src/Common/Twig/TwigContainer.php:70`), so templates recompile in-memory on every request. There is no application query cache (Redis/APCu/memcache are used only for sessions, not query results).
- **Already efficient:** 100% InnoDB (zero MyISAM), DB connection pooling defaults ON (persistent mysqli), the calendar uses single ranged queries, and clinical fields are not field-encrypted (no per-read decrypt cost).

### 1. Database access patterns

**Every query flows through an auditing wrapper (`ADODB_mysqli_log`).** `sqlStatement`/`sqlQuery` (`library/sql.inc.php`) and `QueryUtils` (`src/Common/Database/QueryUtils.php:222`) both call `getADODB()->Execute()`, which is overridden in `library/ADODB_mysqli_log.php:26` to invoke `EventAuditLogger::auditSQLEvent()` on line 50 after *every* query. Even when a query is ultimately not logged, `auditSQLEvent` still runs per query: `trim`, multiple `stripos` checks, a query-type loop, and (for known tables) a `str_contains` scan over the ~70-entry `LOG_TABLES` map (`EventAuditLogger.php:486`). The only audit-free path is `ExecuteNoLog()` (line 64), reached via `sqlStatementNoLog`/`sqlQueryNoLog`. **Impact: High.** *Mitigation: route hot bulk read helpers through `ExecuteNoLog`/`$skipAuditLog`, or reconsider the `audit_events_query` default.*

**`SELECT *` habit in the service layer.** `BaseService::getSelectFields()` (`src/Services/BaseService.php:121-140`) builds the select list from `getFields()`, populated in the constructor by `QueryUtils::listTableFields($table)` — a live `SHOW COLUMNS FROM` (line 69) — plus a second `SHOW COLUMNS ... WHERE extra LIKE '%auto_increment%'` in `getAutoIncrements` (line 316). So every service object issues 2 introspection round-trips at construction and then selects all columns of wide tables like `patient_data`. **Impact: Medium-High.** *Mitigation: cache column metadata per table (static/APCu) instead of re-querying per instance.*

**No query-result caching.** Grep across `src/` and `library/` finds no query cache layer; Redis/Predis appears only for sessions (`src/Common/Session/Predis/*`). **Connection pooling is ON by default** though — `enable_database_connection_pooling` defaults to `'1'` (`globals.inc.php:2941`), and `DatabaseConnectionFactory` uses the `p:` persistent mysqli prefix (`src/BC/DatabaseConnectionFactory.php:113`), so the TCP/auth handshake is reused across requests. Efficient.

### 2. Schema & data structure

~281 tables; **100% InnoDB, zero MyISAM** (255 explicit `ENGINE=InnoDB`, remainder default to InnoDB). Good baseline (row-level locking, crash-safe). Central/largest tables: `patient_data`, `forms`, `form_encounter`, `lists`, `billing`, `documents`, `log`. All from `sql/database.sql`:

| Table | Indexing observed | Gap |
|---|---|---|
| `patient_data` (~132 cols, L8334) | `UNIQUE pid`, `UNIQUE uuid`, `idx_patient_name(lname,fname)`, `idx_patient_dob` | **No PRIMARY KEY** — `id` is only `KEY id (id)` (L8471); InnoDB falls back to a hidden rowid. Very wide table with ~30 rarely-used inline TEXT columns. |
| `forms` (L2460) | `pid_encounter(pid,encounter)`, `form_id` | `encounter` alone not indexable (2nd col of composite); `form_name`/`formdir` are oversized LONGTEXT. |
| `form_encounter` (L2022) | `UNIQUE uuid`, `pid_encounter(pid,encounter)`, `encounter_date` | lookup by `encounter` alone can't use composite. |
| `lists` (L7671) | `pid`, `type`, `UNIQUE uuid` | **No `(pid,type)` composite** for its dominant `WHERE pid=? AND type=?` shape; `activity` soft-delete flag unindexed. |
| `billing` (L245) | `pid` | `encounter`, `user`, `provider_id`, `payer_id` unindexed despite common encounter-scoped lookups. |
| `documents` (L1391) | `UNIQUE uuid/drive_uuid`, `foreign_id`, `foreign_reference`, `owner` | `encounter_id` (L1415), `list_id` (L1407) unindexed; `document_data` MEDIUMTEXT stored inline bloats the clustered index. |
| `log` (L7758) | `patient_id` only | **`date`/`user`/`event`/`category` all unindexed** on an unbounded, ever-growing table — audit queries by date/user/event full-scan. |
| `audit_master` (L149) | none but PK | `pid`, `user_id` unindexed. |

**Impact: High** for `patient_data` missing PK and `log` unindexed on `date`/`user`; **Medium** for the rest. *Remediation: add the missing PRIMARY KEY and the `(pid,type)`/`billing.encounter`/`log(date,user,event)` indexes via Doctrine migrations.*

### 3. Bottlenecks

**Report generation** (`interface/reports/`):
- `appointments_report.php` — base fetch is one query (`fetchAppointments`, L498), but results are **sorted in PHP** (`sortAppointments`, L506), and the render loop (`foreach`, L545) calls `appointments_fetch_reminders()` (L621) which runs the **full CDR rules engine per appointment** (`test_rules_clinic`, L134) plus 3 `list_options` queries per reminder (L141-147). N+1 amplified by the rules engine. **Impact: High.**
- `collections_report.php` — two separate `insurance_data` `sqlQuery` calls *per encounter row* inside the fetch loop (L966, L973; loop at L795). **Impact: Medium.**
- `clinical_reports.php` — `generate_display_field` list lookups inside the `while` loop (L761-763, L818-821; loop at L729) → ~6-8 extra queries per patient row. **Impact: Medium.**
- `patient_list.php` — one large multi-join aggregate over `patient_data × form_encounter × insurance_data` with `GROUP BY` and **no LIMIT** (L221-260); becomes a full `LEFT OUTER JOIN` over all encounters when the date filter is empty, then **de-dups PIDs in PHP** (L264-266) rather than in SQL. **Impact: High** on large datasets.

**Patient summary/dashboard** (`interface/patient_file/summary/`): building one patient's summary fires roughly **40-60+ queries** — `demographics.php` alone has 26 query/`getPatientData` calls; `stats.php` multiplies per-row (`generate_display_field` ×4 per medication at L113-116/L162-165, `getListItemTitle` per allergy at L85/L89, and a `SHOW TABLES LIKE 'form_...'` per form type at L240). **Impact: High.**

**Calendar** (`interface/main/calendar/`): **efficient** — `find_appt_popup.php` uses a single ranged `pc_eventDate/pc_endDate BETWEEN` query (`fetchEvents`, via `library/appointments.inc.php:106`) and expands slots in PHP; no per-day/per-slot query loop. CPU cost is slot bit-mapping, not queries.

**Audit-log overhead on reads (confirmed):** every clinical `SELECT` under default config writes 2 hashed audit rows, so the N+1 read loops above are each further multiplied by 2 audit INSERTs. **Impact: High.**

### 4. Caching & assets

- **Twig: no compilation cache.** `new Environment($twigLoader, ['autoescape' => false])` (`src/Common/Twig/TwigContainer.php:70`) omits the `cache` option, so templates are re-parsed/recompiled every request (no on-disk compiled-PHP that opcache could then serve). **Impact: Medium.** *Mitigation: set a `cache` directory on the Twig Environment.*
- **No application query/object cache** (Redis used only for sessions). **Impact: Medium.**
- **Frontend footprint is moderate.** `package.json` pins jQuery 3.7.1 and Bootstrap 4.6.2; no `angular` dependency is present in `package.json` and no webpack app-bundle config beyond `webpack.themes.js` (theme SCSS compilation). Built `public/assets` is ~648K. The frontend is not the primary latency driver relative to server-side query volume.

### 5. Latency-affecting constraints on a programmatic clinical read

For a service call fetching a patient's full record, the added latency stack is:
1. **Bootstrap cost** — `interface/globals.php` (863 lines) loads all globals from the `globals` table on each request (`SELECT gl_name, gl_index, gl_value FROM globals`, L451) and initializes ACL/session state.
2. **Service construction** — 2 `SHOW COLUMNS` introspection queries per `BaseService` subclass instantiated.
3. **ADODB connection** — mitigated by persistent pooling (default ON), so no per-request handshake.
4. **Audit writes on reads** — the dominant per-query tax: each clinical `SELECT` → `auditSQLEvent` classification + 2 INSERTs with SHA3-512 hashing on a separate DBAL connection.
5. **Encryption** — **not** a per-read cost: no field-level encryption of clinical data in `src/Services/`. Only audit *comment* text is hashed. Efficient.

Net: the largest programmatic-read latency contributors are (a) audit-write amplification and (b) N+1 uncached list/label lookups — both scale with the number of rows/fields returned, so they hurt exactly the "fetch a full patient record" case most.

---

## Data Quality Audit

**Data source:** Findings below are from the **live development database** (MariaDB container `development-easy-mysql-1`, DB `openemr`, 25 patients / 1,186 encounters / 998 problem-list entries) combined with schema (`sql/database.sql`) and application code (`src/Services/`, `src/Validators/`). Where a finding is schema/code-only it is marked as such. The seed data is Synthea-generated synthetic data (patient names carry numeric suffixes, e.g. `Ashley34 Bergstrom287`), so several "empty field" observations reflect the generator, but the *enforcement gaps* they expose are real and apply to production data.

### Top findings

- **DB-level "NOT NULL" is cosmetic** — nearly every key `patient_data` column is `NOT NULL default ''`, so completeness is enforced only in application code, and only on the API/service path (`PatientValidator`), not at the database or legacy-UI layer. (High)
- **Medications are coded with a different, prefix-less scheme than problems/allergies** — all 183 medication rows store bare RxNorm integers (`313782`) while problems/allergies use prefixed `SNOMED-CT:444814009`. An automated consumer cannot tell what code system the medication values belong to. (High)
- **No patient de-duplication or identity uniqueness beyond surrogate keys** — `patient_data` has UNIQUE constraints only on `pid` and `uuid` (both system-generated); `pubpid`, `ss`, and name+DOB are unconstrained and unchecked in code. (High)
- **ICD-10 terminology tables are empty** (`icd10_dx_order_code` = 0 rows); only the 444-row built-in `codes` table and SNOMED text are available. Any lookup/validation of ICD-10 codes will silently find nothing. (Medium)
- **Vitals are largely hollow** — 25 vitals rows exist but `bps`, `bpd`, `weight`, and `temperature` are NULL in the sample; only `height`/`pulse` populated. Contact data is 0% populated (no phone, email, or SSN on any of 25 patients). (Medium, partly a seed-data artifact)
- **Strengths:** unique-constrained UUIDs via `UuidRegistry`, consistently prefixed SNOMED coding on 796/796 problems, a populated audit `log` (920 rows), and richly coded `list_options` (race=922, language=185 entries) give a solid backbone to build on.

### 1. Completeness

**"NOT NULL default ''" defeats DB-level enforcement (High, schema).** In `sql/database.sql` the `patient_data` definition declares `fname`, `lname`, `ss`, `phone_home`, `sex`, `email`, `race`, `ethnicity`, and `pubpid` all as `varchar(255) NOT NULL default ''`, and `DOB date default NULL`. The `NOT NULL` gives no real guarantee — a row with every clinical field blank inserts cleanly, and `DOB` is outright nullable. There is no CHECK constraint and no trigger backing these.

**Enforcement lives only in the API validator, and covers 4 fields (High, code).** `src/Validators/PatientValidator.php` (lines 56-59) requires only `fname`, `lname`, `sex`, and `DOB`, plus format-checks `email`/`email_direct` when present. It does **not** validate `race`, `ethnicity`, `pubpid`, `ss`, or phone. `PatientService::insert()` calls it (`src/Services/PatientService.php:221`), so the REST/FHIR path is guarded — but the legacy `interface/` UI insert path does not route through this validator, so the DB remains the only backstop, and per above the DB enforces nothing.

**Live completeness numbers (from DB):**
- Contact info: **0 of 25** patients have any phone, email, or SSN (all blank).
- `providerID`: NULL for all sampled patients — encounters/patients not linked to a rendering provider.
- Vitals: of 25 `form_vitals` rows, **25/25** have NULL `bps` and `weight`, **22/25** NULL `temperature`.
- Positive: `fname`, `lname`, `DOB`, `sex`, `race`, `ethnicity`, `pubpid`, `uuid` are 100% populated in this seed set.

*Why it matters:* a data consumer cannot assume any contact field, provider link, or vital sign is present; blank-vs-NULL is inconsistent (`''` in patient_data, `NULL` in vitals), so null-checks must cover both representations.

### 2. Consistency / formatting

**Medication coding scheme diverges from all other clinical lists (High, DB).** `lists.diagnosis` uses OpenEMR's `CODETYPE:code` convention for 796/796 problems (`SNOMED-CT:...`) and allergies — but **183/183 medications store a bare RxNorm integer with no prefix** (`sodium fluoride ... Oral Gel` → `1535362`). `code_types` defines `RXCUI` (id 109) as the medication code key, so the stored values don't even match the registered key. A parser splitting on `:` gets the raw integer as the "code type."

**Dates are mixed types (Medium, schema/DB).** `patient_data.DOB` is `date`, `regdate`/`date` are `DATETIME`, `lists.enddate` is `datetime` while the effective date is `date`. The `date_display_format` global is `0` (parsing/display is a runtime concern, not stored), so storage is at least ISO — good — but DATE vs DATETIME mixing means time-of-day is meaningful for some clinical events and absent for others.

**`sex` stored as spelled-out free-ish text (Medium, DB).** Values are `Male`/`Female` only (validator requires `lengthBetween(4,30)`, so an abbreviation `M`/`F` would be *rejected*). Internally consistent here but brittle: any external feed using single-letter codes fails validation rather than normalizing.

**Coded dropdowns are well-populated (Strength).** `list_options` has 922 race, 185 language, 6 marital, 3 ethnicity entries — coded reference data is present rather than free-text.

### 3. Duplicates & identity

**Only surrogate keys are unique (High, schema/DB).** `SHOW INDEX FROM patient_data`: UNIQUE on `pid` and `uuid` only. `pubpid` (the public/human patient identifier), `ss`, and name+DOB have **no uniqueness constraint**. `pubpid` is auto-filled from a fresh pid when blank (`PatientService.php:183-184`) but a caller-supplied duplicate `pubpid` is never checked. No de-duplication or patient-matching service exists. UUIDs are minted per-insert via `UuidRegistry` (`src/Common/Uuid/UuidRegistry.php`), so two records for the same human get two distinct UUIDs and pids with nothing flagging them as the same person.

*Why it matters:* an ingestion path or HL7/FHIR feed can silently create duplicate patient records; downstream aggregation (problem lists, meds) will fragment across the duplicates with no linkage.

*Strength:* the UUID surrogate is genuinely unique and stable, giving a reliable join key **within** the system.

### 4. Reliability / staleness

**Soft-delete / current-vs-historical relies on flag columns, not enforced (Medium).** `lists.activity` (`tinyint default NULL`) distinguishes active/inactive; in live data it's populated (0 NULLs across all 998 rows), and 268 problems / 51 meds / 19 allergies carry NULL `enddate` (open) — so the active-record signal is usable here. But the column is nullable with no default, so a row with `activity IS NULL` has undefined currency; consumers must treat NULL activity as ambiguous.

**All seed patients share one creation instant (Low, artifact).** Every `patient_data.date`/`regdate` is `2026-07-06 21:00:38`-ish (bulk import), while encounters span `1945` to `2026` — timestamps are not a reliable "recency" signal on imported data.

**Audit trail is populated (Strength).** The `log` table holds 920 rows, so mutation history exists for reconstructing who-changed-what.

### 5. Coded data & terminology

**ICD-10 lookup tables empty (Medium, DB).** `icd10_dx_order_code` and `icd10_pcs_order_code` have **0 rows**; `valueset` is empty. The only code inventory is the 444-row built-in `codes` table. Clinical problems are SNOMED-coded via free-standing `SNOMED-CT:` strings in `lists`, but there is no loaded ICD-10 crosswalk, so any code that needs ICD-10 validation or SNOMED→ICD-10 mapping will find nothing and must fall back to the raw string.

**Clinical data is well-coded where it exists (Strength).** 796/796 problems and all allergies carry a code, not free text — high coding density. The weakness is the *medication* scheme mismatch (Section 2), not absence of codes.

*Why the terminology gaps matter:* an automated consumer that trusts code-system prefixes will misclassify every medication, and any ICD-10-driven logic (billing, CQM, decision support) has no reference table to resolve against in this environment.

---

## Architecture Audit

OpenEMR is a ~20-year-old PHP EHR mid-migration from a procedural, globals-driven architecture to a modern PSR-4 / Symfony-component stack. The two paradigms coexist in the same request lifecycle, wired together through a shared global state bag and a single ADODB connection. This section maps the layering, data locations, request flows, and — most importantly — the supported extension points, grounded in in-tree files.

### Top findings

- **Two live architectures share one process.** A modern REST/FHIR stack (`apis/dispatch.php` → `OpenEMR\RestControllers\ApiApplication` → Symfony `HttpKernel` + event subscribers → `HttpRestRouteHandler` → `RestController` → `Services\BaseService` → `QueryUtils`) runs alongside the legacy UI stack (`interface/*.php` → `interface/globals.php` → `library/*.inc.php` helpers → `sqlStatement()`). Both ultimately hit the **same single ADODB connection** stored at `$GLOBALS['adodb']['db']` (`library/sql.inc.php:62`).
- **`$GLOBALS` is still the source of truth**, even for new code. `OEGlobalsBag` (`src/Core/OEGlobalsBag.php`) is a typed Symfony `ParameterBag` wrapper, but its `set()` writes through to `$GLOBALS[$key]` (line 51) and, for the singleton, `get()` reads `$GLOBALS` as the *sole* source of truth (lines 59-61). It is a typed façade over global state, not a replacement for it.
- **The DI container exists but is barely used.** `src/Core/Kernel.php` builds a Symfony `ContainerBuilder`, but it holds essentially only the `event_dispatcher` service. Services and controllers are instantiated with `new` inline in the route map (e.g. `new FacilityRestController()` in `apis/routes/_rest_routes_standard.inc.php`). The kernel is fetched from the globals bag (`OEGlobalsBag::getInstance()->getKernel()`), so it acts as a service locator, not constructor injection. A second, cleaner PSR-11 container (`Firehed\Container` in `bootstrap.php`) exists but is explicitly scoped to "an experimental CLI tool."
- **The event system is the mature, well-populated extension surface.** ~80 event classes across 22 namespaces under `src/Events/` (`RestApiExtend`, `Globals`, `Patient`, `Menu`, `PatientDemographics`, etc.) driven by Symfony `EventDispatcher`. This — plus the `oe-module-*` bootstrap convention — is the clean, supported way to add capability without touching core.
- **Data-access modernization is partial.** ~103 files reference `QueryUtils::` in `src/` vs ~363 legacy `sqlStatement(` call sites in `library/`+`interface/`. Doctrine DBAL is present but wrapped for backward compat and marked `@deprecated`; Doctrine ORM appears in only a handful of files (`src/Entities/` has just 3 entities: `Code`, `CodeType`, `ListOption`).
- **Multi-tenancy is directory-and-session based.** `sites/<siteId>/` holds per-tenant `sqlconf.php` (DB credentials) and `config.php`; the active site is resolved from the session (`$session->get('site_id')`) at `interface/globals.php:272`, then frozen into globals (`OE_SITE_DIR`, `OE_SITE_WEBROOT`).

### 1. System organization & layering

**Entry / bootstrap.** Three distinct front doors:
- `index.php` / `interface/globals.php` — legacy web UI bootstrap. `globals.php` is a ~700-line procedural setup script: resolves site from session (line 272), instantiates the `Kernel` into the globals bag (line 376), pulls in `library/sql.inc.php` (line 384) which opens the DB and copies the handle into the bag (line 385), then loads the site `config.php` (line 643).
- `apis/dispatch.php` — modern REST/FHIR entry. Minimal (45 lines): builds an `HttpRestRequest`, instantiates `ApiApplication`, calls `run()`. All setup is deferred to event subscribers.
- `bootstrap.php` — aspirational unified bootstrap (autoloader, dotenv, `Firehed\Container`, `ErrorHandler`). Its own header states it "MUST NOT" connect to the DB or touch sessions, and it is "only used in an experimental CLI tool." Intended-future foundation, not the live web path.

**Routing.** REST routing is a two-stage design. `_rest_routes.inc.php` loads three route maps into static `RestConfig` properties (`$ROUTE_MAP`, `$FHIR_ROUTE_MAP`, `$PORTAL_ROUTE_MAP`), each an assoc array of `"VERB /path/:param" => closure` defined in `apis/routes/_rest_routes_*.inc.php`. At request time `RoutesExtensionListener` (a `kernel.request` subscriber, priority 40) branches by request type to a `*RouteFinder`, then `HttpRestRouteHandler::dispatch()` (`src/Common/Http/HttpRestRouteHandler.php:40`) linear-scans the map, regex-matches via `HttpRestParsedRoute`, runs `checkSecurity()`, and sets the matched closure as the `_controller` request attribute for the Symfony kernel to invoke.

**Controllers — three coexisting kinds:**
- Modern `src/RestControllers/*RestController.php` (26 classes) — thin adapters that new-up a Service and shape JSON.
- Legacy `interface/` PHP pages — server-rendered UI, each `require`ing `globals.php`.
- Legacy `controllers/C_*.class.php` (10 classes, e.g. `C_Document.class.php`) dispatched via the root `controller.php` — an older MVC-ish pattern largely superseded but still in use for documents/prescriptions.

**Service layer.** `src/Services/BaseService.php` is the modern spine: 18 sub-namespaces, ~310 service files. `BaseService::__construct($table)` auto-discovers columns via `QueryUtils::listTableFields()` and pulls the dispatcher from the globals-bag kernel (line 72). Provides FHIR search-where building, UUID handling, processing-result wrapping. New services are expected to `extend BaseService` per the project's own convention.

**Data access.** `QueryUtils` is the modern facade; notably the legacy `sqlStatement()` in `library/sql.inc.php` now *delegates* to `QueryUtils::sqlStatementThrowException()` (line 99) — so the two layers converge on one code path. `DatabaseConnectionFactory` (`src/BC/`, `@deprecated`) can mint either an `ADODB_mysqli_log` or a Doctrine DBAL `Connection`, but production uses the ADODB path. **Rough split:** modern REST/service/`QueryUtils` code is a substantial minority (~100 files) against a legacy majority (~360 `sqlStatement` sites); ORM adoption is negligible (3 entities).

### 2. Where data lives

- **Database:** MySQL, reached through one shared ADODB connection at `$GLOBALS['adodb']['db']` set in `library/sql.inc.php:62`. Fetch mode `ADODB_FETCH_ASSOC`; `sql_mode` is explicitly blanked (`SET sql_mode = ''`, `DatabaseConnectionFactory.php`), i.e. STRICT mode is disabled — a data-integrity risk carried forward from legacy.
- **Multi-site / tenancy:** `sites/<siteId>/` per tenant. `sites/default/` contains `sqlconf.php` (host/port/login/pass/dbase as loose PHP vars), `config.php`, `documents/`, `LBF`, images, statement templates. Site selection flows from session → `globals.php` → `OE_SITE_DIR`/`OE_SITE_WEBROOT`. The `Kernel` exposes `getSiteDir($siteId)` / `getSiteWebRoot($siteId)` (`src/Core/Kernel.php:181-192`).
- **File storage:** patient documents and EHI exports live under the site's `documents/` tree (filesystem), referenced from DB rows — not in the DB itself.
- **Sessions:** abstracted behind `SessionWrapperFactory::getInstance()->getActiveSession()` (Symfony `SessionInterface`), but session keys (`site_id`, `authUserID`, `pid`, language) are still used as a de-facto request-scoped service locator throughout `globals.php`.

### 3. How layers interact

**Modern REST trace:** `apis/dispatch.php` → `HttpRestRequest::createFromGlobals()` → `ApiApplication::run()` registers ~12 event subscribers on a Symfony `EventDispatcher` (`ExceptionHandler`, `Telemetry`, `SessionCleanup`, `SiteSetup` [does site id + DB + globals], `CORS`, `OAuth2Authorization`, `Authorization`, `RoutesExtension`, `ViewRenderer`) and runs `OEHttpKernel::handle()`. On `kernel.request`, `RoutesExtensionListener` picks a route finder → `HttpRestRouteHandler::dispatch()` matches and stages a closure as `_controller` → the closure does `new SomeRestController()` → controller calls a `Service` → service calls `QueryUtils` → same ADODB handle. Response objects are normalized by `ViewRendererListener`. Note: `SiteSetupListener` — not `globals.php` — bootstraps site/DB/globals for the API path.

**Legacy UI trace:** browser hits `interface/<page>.php` → `require globals.php` → session resolves site → `library/sql.inc.php` opens/reuses the ADODB connection → page calls `library/*.inc.php` helpers and `sqlStatement()` directly → renders via Smarty or Twig. No routing layer, no controller resolver; the file path *is* the route.

**Global state flow:** `OEGlobalsBag` is a singleton bridging both worlds. Writes fan out to `$GLOBALS` (`set()` line 51); reads on the singleton come straight from `$GLOBALS` (lines 59-61). So legacy code writing `$GLOBALS['x']` and modern code calling `$bag->getString('x')` see the same value. The `Kernel`, `eventDispatcher`, and `adodb` handle are all parked in this bag — meaning modern services reach infrastructure via `OEGlobalsBag::getInstance()->getKernel()->getEventDispatcher()`, which is service location, not injection.

### 4. Integration & extension points

**Module system.** Custom modules live in `interface/modules/custom_modules/oe-module-*/` (8 present, e.g. `oe-module-weno`, `oe-module-faxsms`, `oe-module-ehi-exporter`). Each has a top-level `openemr.bootstrap.php` (const `CUSTOM_MODULE_BOOSTRAP_NAME` in `ModulesApplication.php:39`) plus a PSR-4 `src/` (namespace like `OpenEMR\Modules\WenoModule`), `composer.json`, `info.txt`, SQL install/upgrade files, and templates. `ModulesApplication` (`src/Core/ModulesApplication.php`) loads DB-registered modules, calls `loadCustomModule()` per module (line 160), and fires `ModuleLoadEvents::MODULES_LOADED` (line 163). It also bridges the **Laminas MVC** container (for older "zend_modules") by injecting the Symfony `EventDispatcher` as a shared service — the two frameworks are stitched together here. A module's `Bootstrap::subscribeToEvents()` (see `oe-module-weno/src/Bootstrap.php:90+`) is where it wires menu items, demographics render hooks, patient save/update hooks, and global settings — all via `EventDispatcherInterface` injected into its constructor.

**Event system.** Symfony `EventDispatcher`, dispatcher held on `Kernel` (`src/Core/Kernel.php:226`). ~80 event classes under `src/Events/`. Key extension events: `RestApiExtend\RestApiCreateEvent` (route-map injection), `Globals\GlobalsInitializedEvent` (add settings/config tabs), `Menu\MenuEvent` (menu items), `PatientDemographics\RenderEvent` (inject UI into demographics), `Core\ModuleLoadEvents`.

**REST route registration for modules.** `StandardRouteFinder` constructs a `RestApiCreateEvent($routes, …)` and dispatches it on `RestApiCreateEvent::EVENT_HANDLE` (`'restConfig.route_map.create'`) (`src/RestControllers/Finder/StandardRouteFinder.php:36-37`). A module subscribing to that event calls `$event->addToRouteMap($route, $closure)` / `addToFHIRRouteMap(...)` (`src/Events/RestApiExtend/RestApiCreateEvent.php`) to register new endpoints without editing core route files. This is the sanctioned dynamic-route mechanism.

**Menus.** `src/Menu/` (`MenuEvent`, `PatientMenuEvent`, `MenuItems`, roles). Modules add nav entries by subscribing to `MenuEvent`.

#### Integration Points for New Capabilities

The cleanest supported path to add a full new capability (service + REST endpoint + UI), all without patching core files:

1. **Scaffold a module** at `interface/modules/custom_modules/oe-module-<name>/` with `openemr.bootstrap.php`, a PSR-4 `src/` (namespace `OpenEMR\Modules\<Name>`), `composer.json`, and `table.sql`/`sql/` for schema. Register it through the Modules admin UI (DB-backed); `ModulesApplication` auto-loads it and fires `MODULES_LOADED`.
2. **Add a service** by extending `OpenEMR\Services\BaseService` with a `TABLE_NAME` const and `parent::__construct(self::TABLE_NAME)`. Use `QueryUtils` for all data access (never `new` a connection; never call legacy `sqlStatement` in new code). This gives you column discovery, FHIR search, UUID, and `ProcessingResult` for free.
3. **Expose a REST endpoint** by subscribing to `RestApiCreateEvent` (`restConfig.route_map.create`) in your module's `subscribeToEvents()` and calling `addToRouteMap('GET /apis/... ', $closure)`. The closure instantiates your `RestController`, which calls your Service. No edits to `_rest_routes_standard.inc.php`.
4. **Add UI/menu/settings** via `MenuEvent` (nav), `GlobalsInitializedEvent` (admin config tab, as `oe-module-weno` does), and `PatientDemographics\RenderEvent` (inject into patient screens). Render with Twig (modern) rather than Smarty.

**Constraint to design around:** because the DI container is inert on the web path, your services will still reach infrastructure through `OEGlobalsBag::getInstance()->getKernel()` and the shared ADODB handle rather than constructor-injected dependencies. Accept the dispatcher via your module `Bootstrap` constructor (that *is* injected by `ModulesApplication`), but expect to service-locate the kernel/globals elsewhere.

### 5. Coupling & tech-debt observations

- **Global-state coupling (Medium).** `$GLOBALS`, `$_SESSION`, and `$_GET` function as a service locator. `OEGlobalsBag` types the access but does not decouple it — its singleton reads/writes `$GLOBALS` directly. Any "modern" service transitively depends on global request state.
- **Dual template engines (Low–Medium).** Twig 3 (modern) and Smarty 4.5 (legacy) both live; correct engine depends on file extension. Doubles the template-security and maintenance surface.
- **Dual DB layers (Low, converging).** ADODB is the real connection; Doctrine DBAL/ORM present but deprecated-wrapped and thinly used. Legacy `sqlStatement()` now delegates to `QueryUtils`, so the *execution* path has largely unified even though call-site style hasn't. `SET sql_mode=''` disables STRICT mode globally — silent coercion risk.
- **DI immaturity (Medium).** No constructor injection on the web path; `new` in route closures, service location via the globals bag. This is the single biggest testability constraint: services can't be instantiated without a live kernel + DB, forcing DB-backed tests. The `bootstrap.php` + `Firehed\Container` path shows the intended future but is CLI-experimental only.
- **Linear route matching (Low).** `HttpRestRouteHandler::dispatch()` iterates the entire route map per request doing regex matches — fine at current scale, but O(n) and a soft ceiling as modules add routes.

**Strengths.** The event-driven module architecture is genuinely clean and well-adopted — new capability can be added entirely out-of-core. The `RestApiCreateEvent` route-injection and `BaseService`/`QueryUtils` conventions give a coherent, documented "happy path." The REST/FHIR stack is a real Symfony `HttpKernel` pipeline with proper subscriber separation (auth, CORS, telemetry, exception handling, view rendering), and the convergence of legacy `sqlStatement()` onto `QueryUtils` shows the migration is actively reducing, not just accreting, divergence.
