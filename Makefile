# ============================================================================
# PP-API — Makefile
# FastAPI + Cloud Run + Secret Manager + Artifact Registry
# ============================================================================

-include .env
export

# ----------------------------------------------------------------------------
# Python / App
# ----------------------------------------------------------------------------
VENV        ?= .venv
PYTHON      ?= $(VENV)/bin/python
PIP         ?= $(VENV)/bin/pip
APP         ?= app.main:app
HOST        ?= 0.0.0.0
PORT        ?= 8000

# ----------------------------------------------------------------------------
# GCP
# ----------------------------------------------------------------------------
GCP_PROJECT_ID     ?= studious-union-475223-m8
GCP_PROJECT_NUMBER ?= 521006336685
GCP_REGION         ?= southamerica-east1

SERVICE     ?= pp-api
AR_REPO     ?= pp
IMAGE       ?= $(GCP_REGION)-docker.pkg.dev/$(GCP_PROJECT_ID)/$(AR_REPO)/$(SERVICE)
TAG         ?= latest

# ----------------------------------------------------------------------------
# Cloud Run resources
# ----------------------------------------------------------------------------
CPU         ?= 1
MEMORY      ?= 512Mi
MIN_INST    ?= 0
MAX_INST    ?= 4
CONCURRENCY ?= 80
TIMEOUT     ?= 120

# ----------------------------------------------------------------------------
# Secret Manager
# ----------------------------------------------------------------------------
FIREBASE_SECRET          ?= firebase-admin
FIREBASE_MOUNT           ?= /secrets/firebase-admin.json
SECRET_DATABASE_URL      ?= pp-database-url
SECRET_JWT               ?= pp-jwt-secret
SECRET_PEPPER            ?= pp-refresh-pepper
SECRET_FIREBASE_WEB_KEY  ?= pp-firebase-web-api-key

FIREBASE_PROJECT_ID ?= parametro-pedagogico
CORS_ORIGINS        ?= https://app.parametropedagogico.com

RUN_SECRETS ?= $(FIREBASE_MOUNT)=$(FIREBASE_SECRET):latest,DATABASE_URL=$(SECRET_DATABASE_URL):latest,JWT_SECRET=$(SECRET_JWT):latest,REFRESH_TOKEN_PEPPER=$(SECRET_PEPPER):latest,FIREBASE_WEB_API_KEY=$(SECRET_FIREBASE_WEB_KEY):latest

RUN_ENV ?= ^@^FIREBASE_CREDENTIALS_PATH=$(FIREBASE_MOUNT)@FIREBASE_PROJECT_ID=$(FIREBASE_PROJECT_ID)@FIREBASE_CHECK_REVOKED=false@API_DEBUG=false@CORS_ORIGINS=$(CORS_ORIGINS)

MIGRATIONS_DIR ?= ../pp-bo/migrations

.DEFAULT_GOAL := help

# ----------------------------------------------------------------------------
# Help
# ----------------------------------------------------------------------------
.PHONY: help
help:
	@grep -hE '^[a-zA-Z0-9_.-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2}'

# ----------------------------------------------------------------------------
# Local environment
# ----------------------------------------------------------------------------
.PHONY: venv install install-dev clean run run-prod
venv: ## Cria virtualenv
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

install: venv ## Instala dependências
	$(PIP) install -r requirements.txt

install-dev: install ## Instala ferramentas de desenvolvimento
	$(PIP) install ruff black mypy pytest pytest-cov

clean: ## Remove caches locais
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

run: ## Sobe API local
	$(PYTHON) -m uvicorn $(APP) --host $(HOST) --port $(PORT) --reload

run-prod: ## Sobe API local simulando produção
	$(PYTHON) -m uvicorn $(APP) --host $(HOST) --port $(PORT) --proxy-headers --forwarded-allow-ips='*'

# ----------------------------------------------------------------------------
# Quality
# ----------------------------------------------------------------------------
.PHONY: lint format typecheck test test-cov check
lint: ## Lint
	$(PYTHON) -m ruff check app

format: ## Formata código
	$(PYTHON) -m ruff check --fix app
	$(PYTHON) -m black app

typecheck: ## Checagem de tipos
	$(PYTHON) -m mypy app

test: ## Testes
	$(PYTHON) -m pytest -q

test-cov: ## Testes com cobertura
	$(PYTHON) -m pytest --cov=app --cov-report=term-missing

check: lint typecheck test ## Lint + typecheck + testes

# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
.PHONY: db-migrate db-psql
db-migrate: ## Aplica migrations SQL usando DATABASE_URL
	@command -v psql >/dev/null || { echo "psql não encontrado"; exit 1; }
	@url="$${DATABASE_URL_PSQL:-$$DATABASE_URL}"; \
	url="$$(echo "$$url" | sed 's#postgresql+asyncpg://#postgresql://#')"; \
	[ -n "$$url" ] || { echo "Defina DATABASE_URL no .env"; exit 1; }; \
	for f in $$(ls $(MIGRATIONS_DIR)/*.sql | sort); do \
		echo "==> aplicando $$f"; \
		psql "$$url" -v ON_ERROR_STOP=1 -f "$$f" || exit 1; \
	done

db-psql: ## Abre psql usando DATABASE_URL
	@url="$${DATABASE_URL_PSQL:-$$DATABASE_URL}"; \
	url="$$(echo "$$url" | sed 's#postgresql+asyncpg://#postgresql://#')"; \
	psql "$$url"

# ----------------------------------------------------------------------------
# Docker
# ----------------------------------------------------------------------------
.PHONY: docker-build docker-run compose-up compose-down
docker-build: ## Build local Docker
	docker build -t $(SERVICE):$(TAG) .

docker-run: ## Executa Docker local
	docker run --rm -p $(PORT):$(PORT) --env-file .env -e PORT=$(PORT) $(SERVICE):$(TAG)

compose-up: ## Sobe docker compose
	docker compose up --build

compose-down: ## Derruba docker compose
	docker compose down

# ----------------------------------------------------------------------------
# GCP setup
# ----------------------------------------------------------------------------
.PHONY: gcloud-auth gcloud-config gcloud-doctor enable-apis ar-create
gcloud-auth: ## Login gcloud
	gcloud auth login
	gcloud auth application-default login

gcloud-config: ## Define projeto e região padrão
	gcloud config set project $(GCP_PROJECT_ID)
	gcloud config set run/region $(GCP_REGION)

gcloud-doctor: ## Diagnóstico do gcloud
	@echo "== Conta ativa =="; gcloud auth list
	@echo "\n== Config =="; gcloud config list
	@echo "\n== Projeto =="; \
	gcloud projects describe $(GCP_PROJECT_ID) \
		--format='table(projectId,projectNumber,lifecycleState)'

enable-apis: ## Habilita APIs necessárias
	gcloud services enable \
		run.googleapis.com \
		cloudbuild.googleapis.com \
		artifactregistry.googleapis.com \
		secretmanager.googleapis.com \
		cloudresourcemanager.googleapis.com \
		--project $(GCP_PROJECT_ID)

ar-create: ## Cria repositório Artifact Registry
	gcloud artifacts repositories create $(AR_REPO) \
		--repository-format=docker \
		--location=$(GCP_REGION) \
		--project=$(GCP_PROJECT_ID) \
		--description="Imagens do serviço $(SERVICE)" || true

# ----------------------------------------------------------------------------
# GCP permissions
# ----------------------------------------------------------------------------
.PHONY: grant-gcp-deploy-permissions grant-secrets
grant-gcp-deploy-permissions: ## Concede permissões para build/deploy Cloud Run
	gcloud projects add-iam-policy-binding $(GCP_PROJECT_ID) \
		--member="serviceAccount:$(GCP_PROJECT_NUMBER)-compute@developer.gserviceaccount.com" \
		--role="roles/storage.objectViewer"

	gcloud projects add-iam-policy-binding $(GCP_PROJECT_ID) \
		--member="serviceAccount:$(GCP_PROJECT_NUMBER)-compute@developer.gserviceaccount.com" \
		--role="roles/artifactregistry.writer"

	gcloud projects add-iam-policy-binding $(GCP_PROJECT_ID) \
		--member="serviceAccount:$(GCP_PROJECT_NUMBER)@cloudbuild.gserviceaccount.com" \
		--role="roles/storage.objectViewer"

	gcloud projects add-iam-policy-binding $(GCP_PROJECT_ID) \
		--member="serviceAccount:$(GCP_PROJECT_NUMBER)@cloudbuild.gserviceaccount.com" \
		--role="roles/artifactregistry.writer"

	gcloud projects add-iam-policy-binding $(GCP_PROJECT_ID) \
		--member="serviceAccount:$(GCP_PROJECT_NUMBER)@cloudbuild.gserviceaccount.com" \
		--role="roles/run.admin"

	gcloud projects add-iam-policy-binding $(GCP_PROJECT_ID) \
		--member="serviceAccount:$(GCP_PROJECT_NUMBER)@cloudbuild.gserviceaccount.com" \
		--role="roles/iam.serviceAccountUser"

grant-secrets: ## Concede leitura dos secrets ao runtime service account
	@set -e; \
	sa="$${RUNTIME_SA:-$(GCP_PROJECT_NUMBER)-compute@developer.gserviceaccount.com}"; \
	for s in $(FIREBASE_SECRET) $(SECRET_DATABASE_URL) $(SECRET_JWT) $(SECRET_PEPPER) $(SECRET_FIREBASE_WEB_KEY); do \
		echo "==> secret $$s para $$sa"; \
		gcloud secrets add-iam-policy-binding "$$s" \
			--project $(GCP_PROJECT_ID) \
			--member="serviceAccount:$$sa" \
			--role=roles/secretmanager.secretAccessor >/dev/null; \
	done; \
	echo "Acesso concedido a $$sa"

# ----------------------------------------------------------------------------
# Secrets
# ----------------------------------------------------------------------------
.PHONY: secrets-create secret-firebase-file secrets-list
secrets-create: ## Cria/atualiza secrets a partir do .env
	@set -e; \
	put() { \
		name="$$1"; val="$$2"; \
		if [ -z "$$val" ]; then echo "skip $$name: vazio"; return; fi; \
		if gcloud secrets describe "$$name" --project $(GCP_PROJECT_ID) >/dev/null 2>&1; then \
			printf '%s' "$$val" | gcloud secrets versions add "$$name" --project $(GCP_PROJECT_ID) --data-file=- >/dev/null; \
		else \
			printf '%s' "$$val" | gcloud secrets create "$$name" --project $(GCP_PROJECT_ID) --replication-policy=automatic --data-file=- >/dev/null; \
		fi; \
		echo "ok: $$name"; \
	}; \
	put "$(SECRET_DATABASE_URL)" "$$DATABASE_URL"; \
	put "$(SECRET_JWT)" "$$JWT_SECRET"; \
	put "$(SECRET_PEPPER)" "$$REFRESH_TOKEN_PEPPER"; \
	put "$(SECRET_FIREBASE_WEB_KEY)" "$$FIREBASE_WEB_API_KEY"

secret-firebase-file: ## Cria/atualiza secret do Firebase Admin JSON. Use FILE=...
	@f="$${FILE:-./secrets/firebase-admin.json}"; \
	[ -f "$$f" ] || { echo "Arquivo não encontrado: $$f"; exit 1; }; \
	if gcloud secrets describe $(FIREBASE_SECRET) --project $(GCP_PROJECT_ID) >/dev/null 2>&1; then \
		gcloud secrets versions add $(FIREBASE_SECRET) --project $(GCP_PROJECT_ID) --data-file="$$f"; \
	else \
		gcloud secrets create $(FIREBASE_SECRET) --project $(GCP_PROJECT_ID) --replication-policy=automatic --data-file="$$f"; \
	fi

secrets-list: ## Lista secrets
	gcloud secrets list --project $(GCP_PROJECT_ID)

# ----------------------------------------------------------------------------
# Cloud Run deploy
# ----------------------------------------------------------------------------
.PHONY: deploy deploy-source build-push deploy-image
deploy: deploy-source ## Alias para deploy-source

deploy-source: ## Deploy Cloud Run usando --source .
	gcloud run deploy $(SERVICE) \
		--source . \
		--project $(GCP_PROJECT_ID) \
		--region $(GCP_REGION) \
		--platform managed \
		--allow-unauthenticated \
		--cpu $(CPU) \
		--memory $(MEMORY) \
		--min-instances $(MIN_INST) \
		--max-instances $(MAX_INST) \
		--concurrency $(CONCURRENCY) \
		--timeout $(TIMEOUT) \
		--update-secrets "$(RUN_SECRETS)" \
		--set-env-vars "$(RUN_ENV)"

build-push: ## Build e push para Artifact Registry
	gcloud builds submit \
		--tag $(IMAGE):$(TAG) \
		--project $(GCP_PROJECT_ID)

deploy-image: build-push ## Deploy Cloud Run usando imagem do Artifact Registry
	gcloud run deploy $(SERVICE) \
		--image $(IMAGE):$(TAG) \
		--project $(GCP_PROJECT_ID) \
		--region $(GCP_REGION) \
		--platform managed \
		--allow-unauthenticated \
		--cpu $(CPU) \
		--memory $(MEMORY) \
		--min-instances $(MIN_INST) \
		--max-instances $(MAX_INST) \
		--concurrency $(CONCURRENCY) \
		--timeout $(TIMEOUT) \
		--update-secrets "$(RUN_SECRETS)" \
		--set-env-vars "$(RUN_ENV)"

# ----------------------------------------------------------------------------
# Cloud Run operations
# ----------------------------------------------------------------------------
.PHONY: describe url logs logs-tail revisions rollback set-env
describe: ## Detalhes do serviço
	gcloud run services describe $(SERVICE) \
		--project $(GCP_PROJECT_ID) \
		--region $(GCP_REGION)

url: ## Mostra URL pública
	@gcloud run services describe $(SERVICE) \
		--project $(GCP_PROJECT_ID) \
		--region $(GCP_REGION) \
		--format='value(status.url)'

logs: ## Últimos logs
	gcloud run services logs read $(SERVICE) \
		--project $(GCP_PROJECT_ID) \
		--region $(GCP_REGION) \
		--limit=100

logs-tail: ## Logs em tempo real
	gcloud beta run services logs tail $(SERVICE) \
		--project $(GCP_PROJECT_ID) \
		--region $(GCP_REGION)

revisions: ## Lista revisões
	gcloud run revisions list \
		--service $(SERVICE) \
		--project $(GCP_PROJECT_ID) \
		--region $(GCP_REGION)

rollback: ## Rollback para revisão. Use REV=...
	@[ -n "$(REV)" ] || { echo "Use: make rollback REV=nome-da-revisao"; exit 1; }
	gcloud run services update-traffic $(SERVICE) \
		--project $(GCP_PROJECT_ID) \
		--region $(GCP_REGION) \
		--to-revisions $(REV)=100

set-env: ## Atualiza env vars. Use VARS="K1=V1,K2=V2"
	@[ -n "$(VARS)" ] || { echo "Use: make set-env VARS=\"K1=V1,K2=V2\""; exit 1; }
	gcloud run services update $(SERVICE) \
		--project $(GCP_PROJECT_ID) \
		--region $(GCP_REGION) \
		--set-env-vars $(VARS)

# ----------------------------------------------------------------------------
# Bootstrap recomendado
# ----------------------------------------------------------------------------
.PHONY: setup-gcp
setup-gcp: gcloud-config enable-apis ar-create grant-gcp-deploy-permissions grant-secrets ## Setup básico GCP

