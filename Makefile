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
#   make stop        # stop the agent (from another terminal)
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

AGENT_DIR  := agent
AGENT_PORT := 8000
UVICORN    := .venv/bin/uvicorn

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

.PHONY: agent agent-live stop

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
