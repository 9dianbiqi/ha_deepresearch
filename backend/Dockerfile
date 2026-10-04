# Bullseye keeps pthread creation compatible with Docker Engine 20.10's
# default seccomp profile while the deployment host is upgraded.
FROM python:3.11-slim-bullseye

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_CONCURRENT_DOWNLOADS=1 \
    UV_CONCURRENT_INSTALLS=1 \
    UV_CONCURRENT_BUILDS=1 \
    UV_NO_PROGRESS=1

WORKDIR /app

RUN python -m pip install --no-cache-dir --progress-bar off uv==0.11.29 \
    && groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /home/app app

COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/src ./src

RUN uv sync --frozen --no-dev

RUN mkdir -p /data \
    && chown -R app:app /app /data

ENV PYTHONPATH=/app/src \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000

VOLUME ["/data"]
EXPOSE 8000

USER app

CMD ["uv", "run", "--no-sync", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
