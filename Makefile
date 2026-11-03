# Local secrets (MODEL_API_KEY=...), gitignored. Exported to every recipe; never echoed.
-include .env
export

COMPOSE := docker compose -f docker/compose.yml
RUNNER_CPUS ?= 8-15
PY := taskset -c $(RUNNER_CPUS) uv run python
MANIFEST ?= manifests/frozen/eval_v1.yaml

.PHONY: sut-up sut-down seed tasks clone reset preflight pulse endpoints calibrate verify-model goal trial \
	analyze verify report bundle test lint no-sleep check resolve-ui resolve-demo resolve-faults resolve-form \
	resolve-eval

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
	$(PY) -m harness verify-model $(if $(MODEL),--model $(MODEL)) $(if $(CONTINUATION),--continuation $(CONTINUATION))

goal: reset
	$(PY) -m harness goal $(if $(TASK),--task $(TASK)) $(if $(MODEL),--model $(MODEL)) \
		$(if $(CONTINUATION),--continuation $(CONTINUATION))

# make trial CONDITION=agent GATE=off [MANIFEST=...] [REPEAT=1] [BLOCK=1]
trial:
	$(PY) -m harness trial --manifest $(MANIFEST) --condition $(CONDITION) --gate $(GATE) \
		$(if $(REPEAT),--repeat $(REPEAT)) $(if $(BLOCK),--block $(BLOCK))

# Re-analysis needs no model key and no running containers: it reads only the trial bundles.
analyze:
	uv run python -m harness analyze $(RUN)

# Key-free check of saved evidence: raw checksums, then re-analysis must reproduce every saved version 1 result.
verify:
	uv run python -m harness analyze --strict $(RUN)

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

# Resolve. resolve-demo and resolve-eval use the local model (Ollama, free); add MODEL=muse-spark-1.3 to use the Meta
# API (needs MODEL_API_KEY; paid, capped by the scripts in scripts/resolve_*.sh). resolve-faults and resolve-form need
# no model at all.
resolve-ui:
	uv run uvicorn resolve.ui:app --host 127.0.0.1 --port 8090

resolve-demo:
	uv run python -m resolve.eval.demo $(if $(MODEL),--model $(MODEL))

resolve-faults:
	uv run python -m resolve.eval.faults

resolve-form:
	uv run python -m resolve.eval.run --split heldout --arm form --out runs/resolve/form-$$(date +%Y%m%d-%H%M%S)

resolve-eval:
	scripts/resolve_eval_local.sh heldout-local-$$(date +%Y%m%d-%H%M%S) $(MODEL)
