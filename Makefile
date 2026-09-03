# Solventa POC — atajos de operación del experimento.
# El detalle de cada comando vive en el README.

SHELL := /bin/bash
COMPOSE := docker compose
BASELINE := -f docker-compose.yml -f docker-compose.baseline.yml
LOAD := -f docker-compose.yml -f docker-compose.load.yml

.DEFAULT_GOAL := help
.PHONY: help env up down restart logs ps build build-one smoke seed exp collect fault reset clean test

help: ## Muestra esta ayuda
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

env: ## Crea .env a partir de .env.example si no existe
	@test -f .env || (cp .env.example .env && echo "Creado .env desde .env.example")

up: env ## Levanta el stack completo en modo treatment
	$(COMPOSE) up -d --build

baseline: env ## Levanta el stack en modo baseline (control de SP-0)
	$(COMPOSE) $(BASELINE) up -d --build

down: ## Detiene el stack y borra volúmenes
	$(COMPOSE) down -v --remove-orphans

restart: down up ## Reinicio limpio

ps: ## Estado de los contenedores
	$(COMPOSE) ps

logs: ## Sigue los logs (S=nombre-servicio para uno solo)
	$(COMPOSE) logs -f $(S)

build: ## Construye todas las imágenes
	$(COMPOSE) build

build-one: ## Construye un servicio aislado: make build-one S=cotizacion
	@test -n "$(S)" || (echo "Uso: make build-one S=<servicio>" && exit 1)
	docker build -f services/$(S)/Dockerfile -t solventa/$(S):local .

smoke: ## Prueba de humo end-to-end
	./scripts/smoke_test.sh

seed: ## Genera el dataset sintético y precarga la caché según CACHE_PRELOAD_RATIO
	./scripts/seed_data.sh

exp: ## Corre un sub-experimento: make exp SP=SP-2 [FULL=1]
	@test -n "$(SP)" || (echo "Uso: make exp SP=SP-0|SP-1|...|SP-5" && exit 1)
	./scripts/run_experiment.sh $(SP) $(if $(FULL),--full,)

fault: ## Inyecta un fallo en caliente: make fault MODE=error_5xx [MS=1500] [RATE=0.5] [DUR=120]
	./scripts/inject_fault.sh $(MODE) $(MS) $(RATE) $(DUR)

reset: ## Devuelve el mock a modo normal
	./scripts/inject_fault.sh normal

collect: ## Recolecta resultados de una corrida: make collect RUN=<run_id>
	python3 scripts/collect_results.py --run-id $(RUN)

test: ## Tests unitarios de la librería compartida
	python3 -m pytest libs/solventa_common/tests services/*/tests -q

clean: down ## Baja el stack y borra los resultados locales
	rm -rf results/*/ && echo "results/ limpio"
