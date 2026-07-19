#
# locustfile.py — load/stress test for the deployed Clinical Co-Pilot /chat endpoint.
#
# Models N concurrent users each posting clinical questions to POST /chat and
# waiting for the full JSON answer (the endpoint is not streaming). With
# wait_time = constant(0), a user fires its next request the instant the prior
# one returns, so the number of in-flight requests tracks the user count — the
# honest reading of "N concurrent users" for a slow LLM endpoint.
#
# Configuration is via environment variables (set by run.sh):
#   CHAT_BASE_URL    - agent base URL (default: deployed prod)
#   CHAT_TOKEN       - SMART bearer access token (required; see mint_token.py)
#   CHAT_PATIENT_ID  - FHIR Patient logical id the token is scoped to
#
# Run headless, e.g.:
#   locust -f locustfile.py --headless -u 50 -r 10 -t 2m --csv results/c50
# The --csv output carries p50/p95/p99 latency and failure counts natively.

from __future__ import annotations

import os
import random

from locust import HttpUser, constant, events, task

# Default target is the deployed prod agent; override with CHAT_BASE_URL.
DEFAULT_BASE_URL = "https://copilot-agent-production-eb24.up.railway.app"
# Adrian Becker — the demo patient the committed SMART token is scoped to.
DEFAULT_PATIENT_ID = "a234013f-932b-434c-8f21-9edc54ff3892"

# A varied corpus so requests don't all hash to the same LLM/agent path. Reusing
# one identical prompt would let any response caching mask real per-turn latency;
# rotating through clinically distinct asks keeps the measured latency honest.
CLINICAL_QUESTIONS: list[str] = [
    "What are this patient's active problems?",
    "Summarize this patient's current medications.",
    "Does this patient have any documented drug allergies?",
    "What were the findings from the most recent encounter?",
    "Are there any medications that interact with the patient's conditions?",
    "What chronic conditions is this patient being managed for?",
    "List this patient's recent diagnoses in order of importance.",
    "Is this patient due for any follow-up based on their conditions?",
    "What is the patient's medication adherence picture?",
    "Summarize the clinical note from the latest visit.",
    "Are there any red-flag findings I should know about for this patient?",
    "What allergies should I check before prescribing an antibiotic?",
    "Give me a one-line clinical snapshot of this patient.",
    "What conditions were newly documented at the most recent encounter?",
    "Which of this patient's medications treat their documented conditions?",
]

# Questions that route the supervisor to the intake-extractor, so the turn actually
# OCRs a document. Kept separate from CLINICAL_QUESTIONS (and reported under their own
# Locust name) because document turns are a different latency population: they pay a
# Mistral OCR round-trip the chart-only turns never touch, and averaging the two hides
# exactly the number the ingestion SLO needs.
DOCUMENT_QUESTIONS: list[str] = [
    "What does his uploaded lab report show?",
    "What did the patient report on their intake form?",
    "What medications are on his uploaded medication list?",
    "Are any values on the uploaded lab report abnormal?",
    "What allergies did the patient list at intake?",
]

# Weight of chart-only turns against document turns (4:1). Document turns cost real OCR
# spend per call, so this bounds it: a 200-turn run does ~40 extractions, not 200.
CHART_TASK_WEIGHT = 4
DOCUMENT_TASK_WEIGHT = 1

# The LLM turn can legitimately take many seconds; cap it so a genuinely hung
# request eventually counts as a failure instead of pinning a user forever.
REQUEST_TIMEOUT_SECONDS = 120


@events.test_start.add_listener
def _require_token(environment, **_kwargs) -> None:
    """Fail fast at test start if no bearer token is configured.

    Without this, every request would 401 and the run would waste time producing
    a meaningless 100%-error profile.

    Args:
        environment: The Locust environment (unused beyond the hook contract).
        **_kwargs: Additional Locust event kwargs, ignored.

    Raises:
        RuntimeError: If ``CHAT_TOKEN`` is not set in the environment.
    """
    if not os.environ.get("CHAT_TOKEN"):
        raise RuntimeError(
            "CHAT_TOKEN is not set. Mint one first: "
            "export CHAT_TOKEN=$(python mint_token.py)"
        )


class ChatUser(HttpUser):
    """A simulated clinician repeatedly asking the Co-Pilot about one patient."""

    # constant(0): no think time, so in-flight requests ~= configured user count.
    wait_time = constant(0)
    host = os.environ.get("CHAT_BASE_URL", DEFAULT_BASE_URL)

    def on_start(self) -> None:
        """Cache per-user request headers and the patient id once at spawn."""
        self.patient_id = os.environ.get("CHAT_PATIENT_ID", DEFAULT_PATIENT_ID)
        self.headers = {
            "Authorization": f"Bearer {os.environ['CHAT_TOKEN']}",
            "Content-Type": "application/json",
        }

    @task(CHART_TASK_WEIGHT)
    def ask(self) -> None:
        """Post one clinical question and validate the answer.

        Omitting ``conversation_id`` starts a fresh thread each turn, avoiding
        cross-request state contention. A response is a success only if it is
        HTTP 200 and carries a ``summary`` field; anything else is recorded as a
        failure so the error rate reflects real answer quality, not just status.
        """
        self._ask(random.choice(CLINICAL_QUESTIONS), "/chat")  # noqa: S311 (not security-sensitive)

    @task(DOCUMENT_TASK_WEIGHT)
    def ask_about_document(self) -> None:
        """Post one question that forces a document extraction, timed separately.

        Reported under ``/chat [document]`` so the ingestion SLO can be read straight
        off the Locust stats rather than inferred from a blended p95. Whether the turn
        truly extracted is confirmed from the ``attach_and_extract`` spans in Langfuse
        over the same window — the router decides routing, so this only makes it likely.
        """
        self._ask(random.choice(DOCUMENT_QUESTIONS), "/chat [document]")  # noqa: S311

    def _ask(self, message: str, name: str) -> None:
        """Post one turn and record success only on a 200 carrying a ``summary``.

        Args:
            message: The question to ask.
            name: The Locust stats bucket to report under.
        """
        payload = {"patient_id": self.patient_id, "message": message}
        with self.client.post(
            "/chat",
            json=payload,
            headers=self.headers,
            name=name,
            timeout=REQUEST_TIMEOUT_SECONDS,
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return
            try:
                body = response.json()
            except ValueError:
                response.failure("response body was not valid JSON")
                return
            if "summary" not in body:
                response.failure("200 response missing 'summary' field")
                return
            response.success()
