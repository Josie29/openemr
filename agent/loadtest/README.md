# Co-Pilot `/chat` load test (JOS-18)

Simulates concurrent clinicians hitting the deployed agent's `POST /chat`
endpoint and records **p50/p95/p99 latency and error rate** at 10 and 50
concurrent users. Feeds the cost analysis and the baseline infra profiles.

## What it does

- **`mint_token.py`** — mints one ~1-hour SMART patient-scoped access token via
  a refresh-token grant against the OpenEMR OAuth endpoint (reads creds from
  `../api-collection/environments/prod.bru`). Access tokens are reusable across
  concurrent requests, so the run mints once. The refresh token rotates on use,
  so the rotated pair is written back to `prod.bru` (pass `--no-write` to skip).
- **`locustfile.py`** — a Locust `HttpUser` that posts a varied clinical
  question to `/chat` with the bearer token, no think time (`wait_time =
  constant(0)`), so in-flight requests track the configured user count. Each
  request starts a fresh conversation (no `conversation_id`). A response counts
  as a success only if it is HTTP 200 **and** carries a `summary` field.
- **`run.sh`** — bootstraps an isolated venv with locust, mints the token, runs
  a single-request smoke test (aborts on non-200 before any wide run), then
  drives each load level and writes Locust CSVs to `results/<timestamp>/`.

## Run it

```bash
agent/loadtest/run.sh
```

Tunables (env vars):

| Var               | Default                          | Meaning                          |
|-------------------|----------------------------------|----------------------------------|
| `LEVELS`          | `10 50`                          | concurrency levels to run        |
| `DURATION`        | `2m`                             | run time per level               |
| `SPAWN_RATE`      | `10`                             | users spawned per second         |
| `CHAT_BASE_URL`   | deployed prod URL                | target agent base URL            |
| `CHAT_PATIENT_ID` | Adrian Becker (demo patient)     | must match the token's scope     |

To drive Locust manually against an already-minted token:

```bash
export CHAT_TOKEN=$(python mint_token.py)
.venv/bin/locust -f locustfile.py --headless -u 50 -r 10 -t 2m \
  --host https://copilot-agent-production-eb24.up.railway.app --csv results/manual-c50
```

## Reading the results

`results/<ts>/c10_stats.csv` and `c50_stats.csv` carry the percentile columns
Locust computes natively:

- Latency: `50%`, `95%`, `99%` columns on the aggregated row (milliseconds).
- Error rate: `Failure Count` / `Request Count`.

Locust measures **wall-clock latency of the full `/chat` call** (the endpoint
returns a single JSON body, not a stream), which is the number the cost analysis
and infra profile care about.

Two stats rows, on purpose: **`/chat`** is the chart-only turn and
**`/chat [document]`** is the turn that OCRs a document. Document turns pay a
Mistral OCR round-trip the chart turns never touch, so blending them would hide
the ingestion p95 the SLO is written against (`alerting.md` §6).

**Document turns cost real OCR spend.** The task weighting is 4:1 chart:document
(`CHART_TASK_WEIGHT` / `DOCUMENT_TASK_WEIGHT`), so roughly **one turn in five**
runs an extraction — a 200-turn run does ~40, not 200. Scale that before running
at high concurrency, and note the router decides routing, so a document question
makes extraction likely, not certain; confirm the real count from the
`attach_and_extract` spans in Langfuse over the run window.

## Cross-checking in Langfuse

The agent is instrumented (OTel + Pydantic AI); every load request lands as a
trace, with `conversation_id` as the session id. Use Langfuse to corroborate the
client-side latency and to attribute **token cost per turn** (which the load
client can't see) — that per-turn cost, multiplied by the throughput observed
here, is the input to the production cost projection.

## Caveats

- **Single patient.** The SMART token is patient-scoped, so all traffic reads
  one patient's (Adrian Becker's) FHIR data. This is a latency/throughput
  baseline, not a multi-tenant fan-out; per-request FHIR read shape is realistic
  but cache behavior on the FHIR side may be warmer than production.
- **Loads two services.** Each turn drives the LLM API *and* the OpenEMR FHIR
  service. Run against prod during a quiet window; it is real spend on both.
