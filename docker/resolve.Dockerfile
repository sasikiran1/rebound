# Resolve worker and service (one image, two commands). python:3.12-slim, the same base as FieldDesk.
FROM python@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f

RUN pip install --no-cache-dir uv==0.13.6
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group backend --no-install-project
COPY harness/ ./harness/
COPY resolve/ ./resolve/
COPY data/resolve/ ./data/resolve/
COPY ui/resolve.html ./ui/resolve.html
COPY prompts/resolve_interpret_v2.md prompts/resolve_revise_v2.md ./prompts/
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 resolve
USER resolve

# Default: the long-running worker. The service overrides this in compose.
CMD ["python", "-m", "resolve.worker"]
