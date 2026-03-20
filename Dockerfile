FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --no-editable --no-dev --frozen
COPY src/ src/
RUN uv sync --no-editable --no-dev --frozen
EXPOSE 8000
CMD ["uv", "run", "gunicorn", "qfa.main:app", "--worker-class", "asgi", "--bind", "0.0.0.0:8000"]
