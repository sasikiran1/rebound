COMPOSE := docker compose -f docker/compose.yml
RUNNER_CPUS ?= 8-15
PY := taskset -c $(RUNNER_CPUS) uv run python

.PHONY: sut-up sut-down seed clone reset pulse endpoints verify-model goal test lint no-sleep check

sut-up:
	mkdir -p var/fielddesk
	$(COMPOSE) up -d --build --wait

sut-down:
	$(COMPOSE) down

seed:
	$(PY) -m data.generator

clone:
	$(PY) -m harness clone

# Fresh trial DB + FieldDesk restart (its pool held connections to the dropped DB).
reset: clone
	$(COMPOSE) restart fielddesk
	$(COMPOSE) up -d --wait fielddesk

pulse:
	$(PY) -m harness pulse

endpoints:
	$(PY) -m harness endpoints

verify-model:
	$(PY) -m harness verify-model $(if $(MODEL),--model $(MODEL))

goal: reset
	$(PY) -m harness goal $(if $(TASK),--task $(TASK)) $(if $(MODEL),--model $(MODEL))

test:
	uv run pytest -q

lint:
	uv run ruff check .

no-sleep:
	@! grep -rni "sleep" backend/ || (echo "'sleep' found in backend/" && false)

check: lint test no-sleep
