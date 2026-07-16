# AgentForge local dev orchestration.
#
# OpenEMR and the agent have different lifecycles, so they have separate controls:
#   - OpenEMR: heavy multi-container stack, managed by openemr-cmd / docker compose. Boot once,
#     leave up. Not owned here.
#   - Agent:   one lightweight FastAPI process you restart whenever agent code changes. Owned here.
#
# Commands:
#   make agent       # agent in FIXTURE mode (seed patients, no OpenEMR needed) — fast dev loop
#   make agent-live  # agent in live-FHIR mode for browser testing against a running OpenEMR
#   make qdrant      # start the local Qdrant vector DB (idempotent) — backs guideline evidence
#   make index       # (re)index the guideline corpus into Qdrant — idempotent; FORCE=1 rebuilds clean
#   make dev         # full local stack: Qdrant + corpus index, then agent-live (foreground)
#   make stop        # stop the agent (from another terminal)
#   make qdrant-down # remove the Qdrant container (its named volume / indexed data persists)
#
# Both agent targets run uvicorn in the foreground: logs (colored) stream to the terminal, and
# Ctrl-C stops it cleanly. --reload picks up code edits without a manual restart.
#
# agent-live defaults target the local dev OpenEMR (docker/development-easy) at
# http://localhost:8300, with the agent at http://localhost:8000. Keep both HTTP: an HTTPS page
# calling an HTTP agent is a mixed-content block, so browse :8300 (not the :9300 TLS port).
# NOTE: the *agent* wiring here is necessary but NOT sufficient for the sidebar — the :8300 OpenEMR
# must also be configured for the module (enable it in Module Manager, register a SMART OAuth
# client, set the AI_COPILOT_* env + site_addr_oath=http://localhost:8300). Without that the panel
# won't render / can't mint a token.
#
# Point at a different stack by overriding, e.g.:
#   make agent-live OEMR_ORIGIN=http://localhost:8301 FHIR_BASE=http://localhost:8301/apis/default/fhir
#
# COPILOT_* config is passed inline, so agent/.env is never mutated (real env vars beat .env).
#
# Guideline evidence (the RAG panel) needs a local Qdrant holding the indexed corpus. `make dev`
# brings it up and indexes before starting the agent; `make agent-live` alone assumes Qdrant is
# already up (`make qdrant`). Without it, every guideline query fails and the evidence panel is empty.

AGENT_DIR  := agent
AGENT_PORT := 8000
UVICORN    := .venv/bin/uvicorn
PYTHON     := .venv/bin/python

# Local Qdrant vector DB backing the guideline-evidence RAG path (see `make qdrant` / `make index`).
QDRANT_CONTAINER := qdrant-copilot
QDRANT_PORT      := 6333
QDRANT_IMAGE     := qdrant/qdrant
QDRANT_VOLUME    := qdrant_copilot_storage

# Origin the browser loads OpenEMR from (must equal the agent's CORS origin, same scheme) and the
# FHIR base the agent reads (a server-side hop — plain http is fine).
OEMR_ORIGIN := http://localhost:8300
FHIR_BASE   := http://localhost:8300/apis/default/fhir

# Clear the agent port first, so every agent target is a clean restart. Force-kills the whole
# process group (reloader + worker) with SIGKILL, which also reaps a suspended (Ctrl-Z'd) agent
# that a plain SIGTERM cannot — SIGTERM stays pending on a stopped process and the port stays held.
define kill_agent
	-@for pid in $$(lsof -tiTCP:$(AGENT_PORT) -sTCP:LISTEN 2>/dev/null); do \
	    kill -9 -$$(ps -o pgid= -p $$pid | tr -d ' ') 2>/dev/null; \
	  done; true
endef

.PHONY: agent agent-live qdrant index dev stop qdrant-down

## Agent in FIXTURE mode: seed patients, no OpenEMR dependency. Foreground, auto-reload.
agent:
	$(call kill_agent)
	@echo "-- agent (fixture) on http://localhost:$(AGENT_PORT) | traces: Langfuse"
	cd $(AGENT_DIR) && COPILOT_FHIR_CLIENT_MODE=fixture \
		$(UVICORN) copilot.main:app --port $(AGENT_PORT) --reload

## Agent in live-FHIR mode for browser testing. Reads FHIR under the caller's SMART token; a
## tokenless /chat is rejected (static token left blank on purpose). Foreground, auto-reload.
agent-live:
	$(call kill_agent)
	@echo "-- agent (live) on http://localhost:$(AGENT_PORT) | warnings/errors log below | full traces: Langfuse"
	cd $(AGENT_DIR) && \
		COPILOT_FHIR_CLIENT_MODE=http \
		COPILOT_FHIR_BASE_URL=$(FHIR_BASE) \
		COPILOT_FHIR_BEARER_TOKEN= \
		COPILOT_CORS_ORIGINS=$(OEMR_ORIGIN) \
		$(UVICORN) copilot.main:app --port $(AGENT_PORT) --reload

## Stop the agent (OpenEMR keeps running — it is slow to boot and rarely needs restarting).
stop:
	$(call kill_agent)

## Start the local Qdrant vector DB (idempotent) and block until it answers. Data persists in a
## named docker volume, so the corpus survives restarts — index once, not every boot.
qdrant:
	@if [ -n "$$(docker ps -q -f name=^$(QDRANT_CONTAINER)$$)" ]; then \
	    echo "-- qdrant already up on http://localhost:$(QDRANT_PORT)"; \
	elif [ -n "$$(docker ps -aq -f name=^$(QDRANT_CONTAINER)$$)" ]; then \
	    echo "-- starting existing qdrant container"; \
	    docker start $(QDRANT_CONTAINER) >/dev/null; \
	else \
	    echo "-- creating qdrant container on http://localhost:$(QDRANT_PORT)"; \
	    docker run -d --name $(QDRANT_CONTAINER) \
	        -p $(QDRANT_PORT):6333 -p 6334:6334 \
	        -v $(QDRANT_VOLUME):/qdrant/storage $(QDRANT_IMAGE) >/dev/null; \
	fi
	@printf '%s' '-- waiting for qdrant '; \
	for i in $$(seq 1 30); do \
	    if curl -sf http://localhost:$(QDRANT_PORT)/collections >/dev/null 2>&1; then echo 'ready'; exit 0; fi; \
	    printf '.'; sleep 1; \
	done; echo ' TIMEOUT — is docker running?'; exit 1

## Index the guideline corpus into Qdrant. Idempotent — every run upserts all chunks by stable id
## (edits land in place, no duplicates), so it is safe to re-run any time. Pass FORCE=1 to drop and
## recreate the collection (clears orphan points left by deleted chunks). Qdrant config from agent/.env.
index: qdrant
	cd $(AGENT_DIR) && $(PYTHON) -m copilot.rag.index $(if $(FORCE),--force)

## Full local co-pilot stack in one command: bring Qdrant up, index the corpus (if needed), then run
## the live agent in the foreground. OpenEMR (:8300) must already be up — see the header note.
dev: qdrant index agent-live

## Remove the local Qdrant container. The named volume ($(QDRANT_VOLUME)) persists, so a later
## `make qdrant` restores the data without re-indexing. Drop that volume manually to wipe the corpus.
qdrant-down:
	-@docker rm -f $(QDRANT_CONTAINER) >/dev/null 2>&1; true
