FROM python:3.12-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY pyproject.toml README.md ./
COPY pyliveatc ./pyliveatc
COPY examples ./examples

RUN pip install --no-cache-dir ".[stealth]"

ENV PYLIVEATC_MODE=flaresolverr \
    PYLIVEATC_FLARESOLVERR=http://flaresolverr:8191 \
    PYTHONUNBUFFERED=1

WORKDIR /data

CMD ["python", "/app/examples/build_airport_catalog.py", \
     "--out", "/data/airports.json", "--workers", "4"]
