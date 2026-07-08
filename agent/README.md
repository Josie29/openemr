# AgentForge Clinical Co-Pilot — agent service

The standalone Python agent service from `ARCHITECTURE.md` (Option D). This is the **walking
skeleton** (implementation prompt `context/execution/implementation-prompt-01-walking-skeleton.md`): one
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

Credentials use their **native SDK names** (copy them straight from each vendor); the
`COPILOT_`-prefixed forms are also accepted.

| Variable | Makes green | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | LLM probe (`GET api.anthropic.com/v1/models` — metadata, not a completion) | Claude Console |
| `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` | Langfuse probe (`GET {host}/api/public/health`) — **both** required, or tracing stays disabled | Langfuse → Settings → API Keys (free Hobby tier is enough) |
| `LANGFUSE_BASE_URL` | *(optional)* defaults to EU Cloud; **must match your project region** — US is `https://us.cloud.langfuse.com` | your Langfuse instance URL |

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

## Roadmap: observability-driven development loop (planned)

**Goal:** close the loop between the traces this service already emits and the coding agent
that develops it — a self-correcting, trace-driven iteration cycle where Claude Code *reads the
agent's own Langfuse traces, diagnoses failures, fixes the code, re-runs, and repeats until a
defined green signal is reached*, rather than a human ferrying stack traces back and forth.

The architecture already provides the seam. Every turn carries a **correlation ID** (§10) that
ties an HTTP response → its Langfuse trace → and (soon) its eval case, so a failure is always
reproducible. The loop wires two more pieces on top:

- **Feedback channel — the Langfuse MCP server.** Add Langfuse as an MCP server so Claude Code
  can query the agent's traces directly (errors, tool failures, the `verification_grounding`
  score, latency, token cost) instead of being told about them:

  ```bash
  # US project shown; base64-encode "public_key:secret_key"
  claude mcp add --transport http langfuse \
    https://us.cloud.langfuse.com/api/public/mcp \
    --header "Authorization: Basic <base64(pk:sk)>"
  ```

- **Driver — Claude Code `/loop`.** Run the observe → diagnose → fix → re-run cycle on a
  recurring or self-paced loop until the exit signal is met.

**The cycle:** run the agent (or the eval suite) → pull the failing traces via the Langfuse MCP
tool → root-cause from the trace (Langfuse's error-analysis discipline: cluster failures into a
taxonomy before fixing, don't patch symptom-by-symptom) → apply the fix → re-run → re-check the
traces. Repeat until working fully.

**Guardrails (what keeps an autonomous loop honest, not just busy):**

- **Anchor to a measurable exit signal**, never "looks done" — e.g. eval pass-rate ≥ target,
  `verification_grounding` pass-rate ≥ target, zero tool failures, p95 latency within the <15s
  budget. The loop stops when the signal is green, not when it runs out of ideas.
- **Regression protection:** every iteration must keep the deterministic test suite *and* the
  eval suite green. A fix that greens one trace and reds another is a net loss — the suites are
  the ratchet.
- **Turn each new failure into an eval case first, then fix it** — so the loop builds a
  regression net as it goes and can't silently reintroduce a bug it already saw.
- **Bounded and gated:** cap iterations / token budget, and keep a human checkpoint for changes
  that touch contracts, the authorization gate, or anything security-relevant — an autonomous
  loop should harden the agent, not quietly rewrite its trust boundary.

**Status:** planned — pending the Langfuse MCP wiring and an eval suite (`-02`) to anchor the
exit signal. The observability half (traces, scores, correlation IDs) is already in place, so
the loop has real signal to consume the moment it's turned on.
