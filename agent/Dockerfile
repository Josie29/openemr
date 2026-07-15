FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for layer caching. Install the [extraction] extra (mistralai) so live
# Mistral OCR works in prod, then force opentelemetry-semantic-conventions back to the version
# langfuse needs — mistralai 2.x pins an older one that would otherwise break tracing (see the
# pyproject [extraction] note).
COPY pyproject.toml ./
COPY src ./src
RUN pip install --upgrade pip \
    && pip install '.[extraction]' \
    && pip install 'opentelemetry-semantic-conventions==0.63b1'

# Guard: mistralai's OTel telemetry is dormant (never activated at runtime), so langfuse's OTel wins
# at the forced version above. Fail the BUILD, not runtime, if a future dep bump breaks that coexistence.
RUN python -c "import langfuse; from mistralai.client import Mistral; from copilot.ingestion.extractor import MistralOcrBackend; print('extraction+tracing deps OK')"

EXPOSE 8000

# Railway sets $PORT; default to 8000 locally.
CMD ["sh", "-c", "uvicorn copilot.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
