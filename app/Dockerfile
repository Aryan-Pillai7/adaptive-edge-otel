# Pinned to 3.13 rather than latest: OTel SDK wheel availability is the constraint
# that matters here, and an observability demo that fails to build because a
# transitive dependency has no wheel for a brand-new interpreter is a bad trade.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies resolve from pyproject alone, so this layer is cached across source
# edits -- the src/ copy below is what invalidates on a code change.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Non-root. The app needs no write access to anything.
RUN useradd --create-home --uid 10001 edgeapp
USER edgeapp

EXPOSE 8000

# One worker on purpose. Multiple workers would each hold their own OTel meter
# provider and export overlapping metric streams for the same instrument, which
# duplicates series and muddies the measurement.
CMD ["uvicorn", "edgeapp.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
