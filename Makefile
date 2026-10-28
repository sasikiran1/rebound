# Local secrets (MODEL_API_KEY=...), gitignored. Exported to every recipe; never echoed.
-include .env
export

COMPOSE := docker compose -f docker/compose.yml
RUNNER_CPUS ?= 8-15
PY := taskset -c $(RUNNER_CPUS) uv run python
MANIFEST ?= manifests/frozen/eval_v1.yaml

.PHONY: sut-up sut-down seed tasks clone reset preflight pulse endpoints calibrate verify-model goal trial \
	analyze report bundle test lint no-sleep check

sut-up:
	mkdir -p var/fielddesk
	$(COMPOSE) up -d --build --wait

sut-down:
	$(COMPOSE) down

# Template database and both task sets (seeded; identical checksums on every run).
seed:
	$(PY) -m data.generator

tasks: clone
	$(PY) -m harness verify-tasks data/tasks/tasks_dev_A.jsonl
	$(PY) -m harness verify-tasks data/tasks/tasks_eval_B.jsonl

clone:
	$(PY) -m harness clone

# Fresh trial DB + FieldDesk restart (its pool held connections to the dropped DB).
reset: clone
	$(COMPOSE) restart fielddesk
	$(COMPOSE) up -d --wait fielddesk

preflight: reset
	curl -fsS http://127.0.0.1:8080/healthz && curl -fsS http://127.0.0.1:8474/proxies/pg/toxics && echo

pulse:
	$(PY) -m harness pulse

endpoints:
	$(PY) -m harness endpoints

calibrate:
	$(PY) -m harness step-test --manifest manifests/exploratory/explore_v1.yaml

verify-model:
	$(PY) -m harness verify-model $(if $(MODEL),--model $(MODEL))

goal: reset
	$(PY) -m harness goal $(if $(TASK),--task $(TASK)) $(if $(MODEL),--model $(MODEL))

# make trial CONDITION=agent GATE=off [MANIFEST=...] [REPEAT=1] [BLOCK=1]
trial:
	$(PY) -m harness trial --manifest $(MANIFEST) --condition $(CONDITION) --gate $(GATE) \
		$(if $(REPEAT),--repeat $(REPEAT)) $(if $(BLOCK),--block $(BLOCK))

# Re-analysis needs no model key and no running containers: it reads only the trial bundles.
analyze:
	uv run python -m harness analyze $(RUN)

report:
	uv run python -m analysis.experiment_report $(RUN)

bundle:
	tar -czf runs.tar.gz runs/

test:
	uv run pytest -q

lint:
	uv run ruff check .

no-sleep:
	@! grep -rni "sleep" backend/ || (echo "'sleep' found in backend/" && false)

check: lint test no-sleep
