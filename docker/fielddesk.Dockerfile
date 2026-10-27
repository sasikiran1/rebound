# python:3.12-slim
FROM python@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f

RUN pip install --no-cache-dir uv==0.13.6
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --only-group backend --no-install-project
COPY backend/ ./backend/
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/backend PYTHONUNBUFFERED=1

# One worker by design.
CMD ["uvicorn", "fielddesk.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
