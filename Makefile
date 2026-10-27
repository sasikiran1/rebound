COMPOSE := docker compose -f docker/compose.yml
RUNNER_CPUS ?= 8-15
PY := taskset -c $(RUNNER_CPUS) uv run python

.PHONY: sut-up sut-down seed clone pulse goal test lint no-sleep check

sut-up:
	mkdir -p var/fielddesk
	$(COMPOSE) up -d --build --wait

sut-down:
	$(COMPOSE) down

seed:
	$(PY) -m data.generator

clone:
	$(PY) -m harness clone

pulse:
	$(PY) -m harness pulse

goal:
	$(PY) -m harness goal $(if $(TASK),--task $(TASK))

test:
	uv run pytest -q

lint:
	uv run ruff check .

no-sleep:
	@! grep -rni "sleep" backend/ || (echo "'sleep' found in backend/" && false)

check: lint test no-sleep
