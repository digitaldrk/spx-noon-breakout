# Hosted tastytrade worker. No port, no health check, nothing to serve.
# The image carries the package and the seed config only: config.json,
# secrets/, tokens, state/, logs/, and reports/ live on the mounted disk.
FROM python:3.11-slim

# DATA is the mounted disk. MODE and TASTY_ORDERS are deliberately not here: an
# image default would hide a dashboard that still has only an old SPX_NOON_ name.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA=/data

# tzdata so America/New_York resolves; the session clock is Eastern.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY spx_noon ./spx_noon
COPY config.render.example.json ./

# ib_async comes in with the core dependencies; the tasty extra is what Linux runs on.
RUN pip install --no-cache-dir '.[tasty]'

CMD ["python", "-m", "spx_noon.service"]
