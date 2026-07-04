# Not given explicit content in the Phase-0 Prompt; written to satisfy
# Section 21's docker-compose.yml `build: .` + the Python 3.11+
# requirement in Section 1.2.
FROM python:3.11-slim

WORKDIR /app

# psycopg2-binary's wheel covers most cases, but keep build tools
# available in case a source build is ever triggered by a platform
# mismatch (e.g. arm64 without a prebuilt wheel).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# The actual startup command (alembic upgrade head && uvicorn ...) is
# supplied by docker-compose.yml's `command:`, not baked in here, so
# that migrations always run against whatever DATABASE_URL the
# container is given at runtime.
CMD ["uvicorn", "aether.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
