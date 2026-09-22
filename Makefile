SHELL := /bin/bash
PY ?= python3
VENV ?= .venv
KIND_CLUSTER ?= ai-gateway
export LITELLM_MASTER_KEY ?= sk-local-master-key
export GATEWAY_URL ?= http://localhost:4000

.PHONY: help
help: ## Lista os alvos
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n",$$1,$$2}'

$(VENV)/bin/activate:
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -q --require-hashes -r requirements.lock
	$(VENV)/bin/pip install -q --no-deps -e . && $(VENV)/bin/pip install -q "pytest>=8" "pytest-asyncio>=0.24" "ruff>=0.6"

.PHONY: dev
dev: $(VENV)/bin/activate ## Cria o virtualenv de desenvolvimento

.PHONY: lint
lint: dev ## Ruff (lint + formatação)
	$(VENV)/bin/ruff check . && $(VENV)/bin/ruff format --check .

.PHONY: test
test: dev ## Testes unitários
	$(VENV)/bin/pytest

.PHONY: lock
lock: ## Regenera os lockfiles com hashes (após mudar dependências)
	uv pip compile pyproject.toml --universal --python-version 3.12 --generate-hashes -o requirements.lock
	uv pip compile clients/github-app/requirements.in --universal --python-version 3.12 --generate-hashes -o clients/github-app/requirements.lock

.PHONY: up
up: ## Sobe gateway + Postgres + mock da Azure (docker compose)
	docker compose up -d --build --wait

.PHONY: up-obs
up-obs: ## Idem, com Prometheus (9091), Grafana (3000) e Jaeger (16686)
	OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4317 docker compose --profile observability up -d --build --wait

.PHONY: down
down: ## Derruba a stack local e apaga o banco
	docker compose --profile observability down -v

.PHONY: seed
seed: dev ## Aplica provisioning/tenants.yaml no gateway local
	$(VENV)/bin/gatewayctl apply -f provisioning/tenants.yaml

.PHONY: test-integration
test-integration: dev ## Testes de ponta a ponta contra o gateway local (make up antes)
	$(VENV)/bin/pip install -q "openai>=1.50"
	$(VENV)/bin/pytest -m integration tests/integration

.PHONY: k8s-validate
k8s-validate: ## Renderiza e valida os overlays do Kustomize (kustomize + kubeconform)
	@for o in local prod; do \
	  kustomize build deploy/k8s/overlays/$$o | kubeconform -strict -summary -kubernetes-version 1.33.0 \
	    -schema-location default \
	    -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' - || exit 1; \
	done

.PHONY: kind-up
kind-up: ## Cluster kind local com o overlay "local"
	kind get clusters | grep -qx $(KIND_CLUSTER) || kind create cluster --name $(KIND_CLUSTER) --wait 120s
	docker build -t ai-gateway:dev . && docker build -t mock-azure:dev tests/mock_azure
	kind load docker-image ai-gateway:dev mock-azure:dev --name $(KIND_CLUSTER)
	kubectl apply -k deploy/k8s/overlays/local
	kubectl -n ai-gateway rollout status deploy/ai-gateway --timeout=600s
	@echo "Acesse com: kubectl -n ai-gateway port-forward svc/ai-gateway 4000:4000 9090:9090"

.PHONY: kind-down
kind-down: ## Remove o cluster kind
	kind delete cluster --name $(KIND_CLUSTER)
