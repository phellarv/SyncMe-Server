FROM python:3.12-slim AS build
RUN pip install --no-cache-dir uv==0.12.22
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
ENV PORT=8000
WORKDIR /app
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /app/.venv /app/.venv
COPY app ./app
COPY log_config.json ./
USER app
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --log-config log_config.json"]
