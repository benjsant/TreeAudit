# TreeAudit — development shortcuts. Everything runs in Docker; nothing is
# installed on the host (docs/DECISIONS.md D-019).

SHELL := /bin/bash
.DEFAULT_GOAL := help

-include .env
# Hand compose exactly the values make validated. Without this, a variable set in
# the user's shell would take precedence for compose while make validated the
# .env value: check-root and the actual mount could disagree.
export TREEAUDIT_UID TREEAUDIT_GID TREEAUDIT_ROOT_PATH TREEAUDIT_ALLOW_SYSTEM_VOLUME TREEAUDIT_PORT

COMPOSE_FILES := -f compose.yaml
ifeq ($(COMPOSE_SELINUX),1)
COMPOSE_FILES += -f compose.selinux.yaml
endif
COMPOSE := docker compose $(COMPOSE_FILES)
DEV := $(COMPOSE) run --rm dev
HOST_IDS := --user "$$(id -u):$$(id -g)"

.PHONY: help env data lock build check-root up down test lint format typecheck check

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

env: ## Create .env with your UID/GID (never overwrites an existing one)
	@if [ -e .env ]; then echo ".env already exists, not modified"; else \
	  { echo "TREEAUDIT_UID=$$(id -u)"; \
	    echo "TREEAUDIT_GID=$$(id -g)"; \
	    echo "# Absolute path of the folder to audit. It is mounted read-only."; \
	    echo "TREEAUDIT_ROOT_PATH="; \
	    echo "# Set to 1 only to audit, on purpose, a folder on the system volume."; \
	    echo "TREEAUDIT_ALLOW_SYSTEM_VOLUME=0"; \
	    echo "TREEAUDIT_PORT=8765"; } > .env; \
	  chmod 600 .env; echo ".env created: now set TREEAUDIT_ROOT_PATH"; fi

data: ## Create the local data directory (mode 700)
	@mkdir -p -m 700 data

lock: ## Resolve dependencies and update uv.lock (needs network)
	docker build -f docker/Dockerfile --target uvtool -t treeaudit-uv:local .
	docker run --rm $(HOST_IDS) -e HOME=/tmp -v "$(CURDIR):/app" -w /app treeaudit-uv:local uv lock

build: ## Build the dev and app images
	$(COMPOSE) --profile dev build

check-root: build data ## Validate TREEAUDIT_ROOT_PATH on the host before mounting it
	@test -n "$(TREEAUDIT_ROOT_PATH)" || { echo "TREEAUDIT_ROOT_PATH is empty in .env (run 'make env')"; exit 2; }
	@test -d "$(TREEAUDIT_ROOT_PATH)" || { echo "REFUSÉ: '$(TREEAUDIT_ROOT_PATH)' n'est pas un dossier existant"; exit 1; }
	@real="$$(realpath -e -- "$(TREEAUDIT_ROOT_PATH)")" && test "$$real" = "$(TREEAUDIT_ROOT_PATH)" || \
	  { echo "REFUSÉ: '$(TREEAUDIT_ROOT_PATH)' passe par un lien symbolique ou n'est pas normalisé (chemin réel : $$real)"; exit 1; }
	@$(COMPOSE) run --rm -T check \
	  $(if $(filter 1,$(TREEAUDIT_ALLOW_SYSTEM_VOLUME)),--allow-system-volume) \
	  --home "$(HOME)" --data-dir "$(CURDIR)/data" -- "$(TREEAUDIT_ROOT_PATH)" < /proc/self/mountinfo

up: check-root ## Validate the audited folder, then start TreeAudit on http://127.0.0.1:$(TREEAUDIT_PORT)
	$(COMPOSE) up app

down: ## Stop TreeAudit
	$(COMPOSE) down

test: ## Run the test suite (ARGS="..." to pass pytest options)
	$(DEV) pytest $(ARGS)

lint: ## Run ruff (lint and format check)
	$(DEV) ruff check .
	$(DEV) ruff format --check .

format: ## Format the code with ruff
	$(DEV) ruff format .

typecheck: ## Run mypy
	$(DEV) mypy

check: lint typecheck test ## Run every quality gate
