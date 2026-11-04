# Local secrets (MODEL_API_KEY=...), gitignored. Exported to every recipe; never echoed.
-include .env
export

COMPOSE := docker compose -f docker/compose.yml

.PHONY: up down seed clone preflight resolve-up check test lint no-sleep resolve-ui resolve-live resolve-demo \
	resolve-faults resolve-form resolve-eval

# PostgreSQL 16, Toxiproxy (fault injection on the database link) and FieldDesk, the booking API.
up:
	mkdir -p var/fielddesk
	$(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) --profile resolve down

# Seeded synthetic template database (identical checksum on every run).
seed:
	uv run python -m data.generator

clone:
	uv run python -m harness clone

# Fresh trial database from the template, FieldDesk restarted (its pool held connections to the dropped database).
preflight: clone
	$(COMPOSE) restart fielddesk
	$(COMPOSE) up -d --wait fielddesk
	curl -fsS http://127.0.0.1:8080/healthz && curl -fsS http://127.0.0.1:8474/proxies/pg/toxics && echo

# Calendar server and test inbox (the deployed worker and service: `$(COMPOSE) --profile resolve up -d`).
resolve-up:
	mkdir -p var/calendar var/mail
	$(COMPOSE) --profile resolve up -d --build resolve-calendar resolve-mail

check: lint test no-sleep

test:
	uv run pytest -q

lint:
	uv run ruff check .

no-sleep:
	@! grep -rnE --include='*.py' 'sleep[[:space:]]*\(' backend/ || (echo "a sleep call found in backend/" && false)

# Resolve. resolve-faults and resolve-form need no model. resolve-live, resolve-demo and resolve-eval use the local
# model (Ollama) unless MODEL=muse-spark-1.3 (needs MODEL_API_KEY; paid, capped).
resolve-ui:
	uv run uvicorn resolve.app:app --host 127.0.0.1 --port 8090

# The live demo screen on http://127.0.0.1:8092/ (scenario 2 loaded).
resolve-live:
	scripts/resolve_live_ui.sh --case D1 $(if $(MODEL),--model $(MODEL) --max-calls 20 --max-cost 0.45)

resolve-demo:
	uv run python -m resolve.eval.demo $(if $(MODEL),--model $(MODEL))

resolve-faults:
	uv run python -m resolve.eval.faults

resolve-form:
	uv run python -m resolve.eval.run --split heldout --arm form --out runs/resolve/form-$$(date +%Y%m%d-%H%M%S)

resolve-eval:
	scripts/resolve_eval_local.sh heldout-local-$$(date +%Y%m%d-%H%M%S) $(MODEL)
