# AgentForge Clinical Co-Pilot — agent service

The standalone Python agent service from `ARCHITECTURE.md` (Option D). This is the **walking
skeleton** (implementation prompt `context/implementation-prompt-01-walking-skeleton.md`): one
end-to-end turn — `POST /chat` → correlation ID → one FHIR tool → Claude → verification gate →
grounded structured answer → Langfuse trace — plus real `/health` and `/ready` probes.

It is deliberately narrow. Only `get_patient` (FHIR `Patient`) is wired; the verification gate
enforces **grounding only** (every claim cites a fetched resource). The other four FHIR tools,
faithfulness/domain verification, SSE streaming, tiered routing, and the PHP module are
follow-up increments (`-02`, `-03`). See the prompt's Non-goals.

## Layout

```
src/copilot/
  main.py          FastAPI app, routes, middleware wiring
  config.py        pydantic-settings; all config/secrets from env (COPILOT_ prefix)
  schemas.py       ChatRequest / ChatResponse / Claim / SourceRef contracts
  agent.py         Pydantic AI agent: get_patient tool + output_validator gate
  verification.py  FetchLog registry + grounding check (ARCHITECTURE.md §7)
  correlation.py   X-Correlation-ID middleware
  observability.py Langfuse tracer (tokens, cost, verification outcome) + NullTracer
  health.py        /health + /ready dependency probes
  fhir/            FhirClient protocol, httpx impl, fixture impl, PatientDemographics
tests/             deterministic tests (fixtures + FunctionModel; no live LLM/FHIR)
```

## Run locally

```bash
cd agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # fixture FHIR mode works with no external services

# Serve (fixture patient id "1" is bundled):
uvicorn copilot.main:app --reload

curl localhost:8000/health
curl localhost:8000/ready       # 503 until an LLM key (and Langfuse) are configured
curl -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"patient_id":"1","message":"Who is this patient?"}'
```

`/chat` needs `COPILOT_ANTHROPIC_API_KEY` for a live answer. Without it, run the tests — they
drive the agent with a scripted model and need no key.

## Making `/ready` green

`/ready` returns 503 until every dependency probe passes, and 200 only when all do (that is
the point — it must never return an unconditional 200). It reports each dependency's status
in the body, e.g. `{"fhir": true, "llm": false, "langfuse": false}`. The LLM and Langfuse
probes fail out of the box because no credentials are set.

> **Secrets never go in this file.** Set the variables below in your gitignored `.env`
> (locally) or as Railway service variables (prod) — `.env.example` lists them with empty
> values to copy. Do not paste real keys into the README or `.env.example`.

| Variable | Makes green | Where to get it |
|---|---|---|
| `COPILOT_ANTHROPIC_API_KEY` | LLM probe (`GET api.anthropic.com/v1/models` — metadata, not a completion) | Claude Console |
| `COPILOT_LANGFUSE_PUBLIC_KEY` + `COPILOT_LANGFUSE_SECRET_KEY` | Langfuse probe (`GET {host}/api/public/health`) — **both** required, or tracing stays disabled and the probe reports "not configured" | Langfuse project → Settings → API Keys (free Hobby tier is enough) |
| `COPILOT_LANGFUSE_HOST` | *(optional)* defaults to Langfuse Cloud; override only for self-hosted | your Langfuse instance URL |

With those set in `.env`:

```bash
curl -s localhost:8000/ready | jq       # 200, every dependency "ok": true
```

The same three probes back `/ready` whether run locally or on Railway — setting these as
Railway service variables is what makes the deployed `/ready` report healthy.

## Quality gates

```bash
pytest        # deterministic; no network, no live LLM
ruff check .
mypy
```

## Deploy (Railway)

Same project/region as OpenEMR (internal networking). Build from this `agent/` directory using
the `Dockerfile`; set the `COPILOT_*` variables as Railway service variables. For live FHIR set
`COPILOT_FHIR_CLIENT_MODE=http` plus `COPILOT_FHIR_BASE_URL` and `COPILOT_FHIR_BEARER_TOKEN`
(the SMART token; minted by the PHP module once `-03` lands).
