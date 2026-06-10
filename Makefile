# ============================================================================
# PP-API — Makefile
#
# FastAPI (app.main:app) + Cloud Run. Alvos auto-documentados: `make` ou `make help`.
# Sobrescreva variáveis na linha de comando, ex.:
#   make deploy GCP_PROJECT=meu-projeto GCP_REGION=southamerica-east1
# ============================================================================

# Carrega .env (se existir) para os alvos locais (run, test, db-migrate...).
-include .env
export

# --- Python / venv ----------------------------------------------------------
VENV        ?= .venv
PYTHON      ?= $(VENV)/bin/python
PIP         ?= $(VENV)/bin/pip
APP         ?= app.main:app
HOST        ?= 0.0.0.0
PORT        ?= 8000

# --- GCP / Cloud Run --------------------------------------------------------
GCP_PROJECT ?= parametro-pedagogico
GCP_REGION  ?= southamerica-east1
SERVICE     ?= pp-api
AR_REPO     ?= pp
IMAGE       ?= $(GCP_REGION)-docker.pkg.dev/$(GCP_PROJECT)/$(AR_REPO)/$(SERVICE)
TAG         ?= latest

# Recursos do Cloud Run (ajuste conforme necessidade).
CPU         ?= 1
MEMORY      ?= 512Mi
MIN_INST    ?= 0
MAX_INST    ?= 4
CONCURRENCY ?= 80
TIMEOUT     ?= 120

# Secret Manager: JSON do Firebase Admin montado como ficheiro no contentor.
FIREBASE_SECRET   ?= firebase-admin
FIREBASE_MOUNT    ?= /secrets/firebase-admin.json

# Migrações SQL (vivem no repo pp-bo). Aplicadas via psql usando $$DATABASE_URL.
MIGRATIONS_DIR    ?= ../pp-bo/migrations

.DEFAULT_GOAL := help

# ============================================================================
# Ajuda
# ============================================================================
.PHONY: help
help: ## Lista os alvos disponíveis
	@grep -hE '^[a-zA-Z0-9_.-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ============================================================================
# Ambiente / dependências
# ============================================================================
.PHONY: venv install install-dev clean
venv: ## Cria o virtualenv em $(VENV)
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

install: venv ## Instala as dependências de runtime (requirements.txt)
	$(PIP) install -r requirements.txt

install-dev: install ## Instala dependências + ferramentas de dev (ruff, black, mypy)
	$(PIP) install ruff black mypy

clean: ## Remove caches, venv e artefatos
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# ============================================================================
# Execução local
# ============================================================================
.PHONY: run run-prod shell
run: ## Sobe a API local com reload (uvicorn)
	$(PYTHON) -m uvicorn $(APP) --host $(HOST) --port $(PORT) --reload

run-prod: ## Sobe a API como em produção (sem reload, proxy-headers)
	$(PYTHON) -m uvicorn $(APP) --host $(HOST) --port $(PORT) \
		--proxy-headers --forwarded-allow-ips='*'

shell: ## Abre um shell Python com o app importado
	$(PYTHON) -c "from app.main import app; import IPython; IPython.embed()" \
		|| $(PYTHON) -c "from app.main import app; import code; code.interact(local=locals())"

# ============================================================================
# Qualidade (lint / format / types / testes)
# ============================================================================
.PHONY: lint format typecheck test test-cov check
lint: ## Lint com ruff (requer make install-dev)
	$(PYTHON) -m ruff check app

format: ## Formata com black + ruff --fix
	$(PYTHON) -m ruff check --fix app
	$(PYTHON) -m black app

typecheck: ## Checagem de tipos com mypy
	$(PYTHON) -m mypy app

test: ## Roda os testes (pytest)
	$(PYTHON) -m pytest -q

test-cov: ## Testes com cobertura
	$(PYTHON) -m pytest --cov=app --cov-report=term-missing

check: lint typecheck test ## Pipeline local de CI (lint + tipos + testes)

# ============================================================================
# Banco de dados (migrações SQL via psql)
# ============================================================================
.PHONY: db-migrate db-psql
db-migrate: ## Aplica migrations/*.sql (psql, ordenado) usando $$DATABASE_URL_PSQL ou $$DATABASE_URL
	@command -v psql >/dev/null || { echo "psql não encontrado (instale o postgresql-client)"; exit 1; }
	@url="$${DATABASE_URL_PSQL:-$$DATABASE_URL}"; \
	url="$$(echo "$$url" | sed 's#postgresql+asyncpg://#postgresql://#')"; \
	[ -n "$$url" ] || { echo "Defina DATABASE_URL no .env"; exit 1; }; \
	for f in $$(ls $(MIGRATIONS_DIR)/*.sql | sort); do \
		echo "==> aplicando $$f"; \
		psql "$$url" -v ON_ERROR_STOP=1 -f "$$f" || exit 1; \
	done

db-psql: ## Abre o psql na base apontada por $$DATABASE_URL
	@url="$${DATABASE_URL_PSQL:-$$DATABASE_URL}"; \
	url="$$(echo "$$url" | sed 's#postgresql+asyncpg://#postgresql://#')"; \
	psql "$$url"

# ============================================================================
# Docker (build local)
# ============================================================================
.PHONY: docker-build docker-run compose-up compose-down
docker-build: ## Build da imagem Docker local
	docker build -t $(SERVICE):$(TAG) .

docker-run: ## Roda a imagem local em :$(PORT)
	docker run --rm -p $(PORT):$(PORT) --env-file .env -e PORT=$(PORT) $(SERVICE):$(TAG)

compose-up: ## Sobe via docker compose
	docker compose up --build

compose-down: ## Derruba o docker compose
	docker compose down

# ============================================================================
# GCP — setup
# ============================================================================
.PHONY: gcloud-auth gcloud-config enable-apis ar-create
gcloud-auth: ## Login no gcloud (usuário) + ADC
	gcloud auth login
	gcloud auth application-default login

gcloud-config: ## Define projeto e região padrão no gcloud
	gcloud config set project $(GCP_PROJECT)
	gcloud config set run/region $(GCP_REGION)

enable-apis: ## Habilita as APIs necessárias (run, build, artifact registry, secrets)
	gcloud services enable \
		run.googleapis.com \
		cloudbuild.googleapis.com \
		artifactregistry.googleapis.com \
		secretmanager.googleapis.com \
		--project $(GCP_PROJECT)

ar-create: ## Cria o repositório no Artifact Registry ($(AR_REPO))
	gcloud artifacts repositories create $(AR_REPO) \
		--repository-format=docker \
		--location=$(GCP_REGION) \
		--project=$(GCP_PROJECT) \
		--description="Imagens da PP-API" || true

# ============================================================================
# GCP — deploy no Cloud Run
# ============================================================================
.PHONY: deploy deploy-image build-push describe url logs logs-tail revisions rollback set-env
deploy: ## Deploy no Cloud Run a partir do código (Cloud Build faz o build)
	gcloud run deploy $(SERVICE) \
		--source . \
		--project $(GCP_PROJECT) \
		--region $(GCP_REGION) \
		--platform managed \
		--allow-unauthenticated \
		--cpu $(CPU) --memory $(MEMORY) \
		--min-instances $(MIN_INST) --max-instances $(MAX_INST) \
		--concurrency $(CONCURRENCY) --timeout $(TIMEOUT) \
		--update-secrets $(FIREBASE_MOUNT)=$(FIREBASE_SECRET):latest \
		--set-env-vars FIREBASE_CREDENTIALS_PATH=$(FIREBASE_MOUNT)

build-push: ## Build + push da imagem para o Artifact Registry
	gcloud builds submit --tag $(IMAGE):$(TAG) --project $(GCP_PROJECT)

deploy-image: build-push ## Deploy no Cloud Run a partir de imagem pré-construída
	gcloud run deploy $(SERVICE) \
		--image $(IMAGE):$(TAG) \
		--project $(GCP_PROJECT) \
		--region $(GCP_REGION) \
		--platform managed \
		--allow-unauthenticated \
		--cpu $(CPU) --memory $(MEMORY) \
		--min-instances $(MIN_INST) --max-instances $(MAX_INST) \
		--concurrency $(CONCURRENCY) --timeout $(TIMEOUT) \
		--update-secrets $(FIREBASE_MOUNT)=$(FIREBASE_SECRET):latest \
		--set-env-vars FIREBASE_CREDENTIALS_PATH=$(FIREBASE_MOUNT)

set-env: ## Atualiza variáveis de ambiente do serviço (use VARS="K1=V1,K2=V2")
	@[ -n "$(VARS)" ] || { echo "Use: make set-env VARS=\"CHAVE=valor,...\""; exit 1; }
	gcloud run services update $(SERVICE) \
		--project $(GCP_PROJECT) --region $(GCP_REGION) \
		--set-env-vars $(VARS)

describe: ## Detalhes do serviço no Cloud Run
	gcloud run services describe $(SERVICE) \
		--project $(GCP_PROJECT) --region $(GCP_REGION)

url: ## Mostra a URL pública do serviço
	@gcloud run services describe $(SERVICE) \
		--project $(GCP_PROJECT) --region $(GCP_REGION) \
		--format='value(status.url)'

logs: ## Últimos logs do serviço
	gcloud run services logs read $(SERVICE) \
		--project $(GCP_PROJECT) --region $(GCP_REGION) --limit=100

logs-tail: ## Acompanha os logs em tempo real
	gcloud beta run services logs tail $(SERVICE) \
		--project $(GCP_PROJECT) --region $(GCP_REGION)

revisions: ## Lista as revisões do serviço
	gcloud run revisions list $(SERVICE) \
		--project $(GCP_PROJECT) --region $(GCP_REGION)

rollback: ## Redireciona 100% do tráfego para REV (use REV=nome-da-revisao)
	@[ -n "$(REV)" ] || { echo "Use: make rollback REV=$(SERVICE)-00001-abc"; exit 1; }
	gcloud run services update-traffic $(SERVICE) \
		--project $(GCP_PROJECT) --region $(GCP_REGION) \
		--to-revisions $(REV)=100
