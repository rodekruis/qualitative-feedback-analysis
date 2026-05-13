FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-editable --no-dev --frozen
COPY src/ src/
COPY alembic.ini ./
COPY alembic/ alembic/
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh && uv sync --no-editable --no-dev --frozen
EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
