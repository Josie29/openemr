FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for layer caching.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

EXPOSE 8000

# Railway sets $PORT; default to 8000 locally.
CMD ["sh", "-c", "uvicorn copilot.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
