# TreeAudit — development shortcuts. Everything runs in Docker; nothing is
# installed on the host (docs/DECISIONS.md D-019).

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Precedence: command line (make up TREEAUDIT_ROOT_PATH=...), then the shell
# environment, then .env. Assignments in an included file would otherwise hide
# the environment, so remember it before reading .env.
SETTINGS := TREEAUDIT_ROOT_PATH TREEAUDIT_ALLOW_SYSTEM_VOLUME TREEAUDIT_PORT
$(foreach v,$(SETTINGS),$(eval _FROM_ENV_$(v) := $(if $(filter environment command line,$(origin $(v))),$(value $(v)))))
-include .env
$(foreach v,$(SETTINGS),$(if $(_FROM_ENV_$(v)),$(eval override $(v) := $(_FROM_ENV_$(v)))))
# A trailing "/" typed by the user is harmless: strip it before validation, so
# that what is validated and what is mounted are the same normalized path.
override TREEAUDIT_ROOT_PATH := $(patsubst %/,%,$(TREEAUDIT_ROOT_PATH))
# Hand compose exactly the values make validated, so check-root and the actual
# mount can never disagree.
export TREEAUDIT_UID TREEAUDIT_GID $(SETTINGS)

COMPOSE_FILES := -f compose.yaml
ifeq ($(COMPOSE_SELINUX),1)
COMPOSE_FILES += -f compose.selinux.yaml
endif
COMPOSE := docker compose $(COMPOSE_FILES)
DEV := $(COMPOSE) run --rm dev
HOST_IDS := --user "$$(id -u):$$(id -g)"

.PHONY: help env data lock build check-root up scan parasites dupes micro dupes-show search scans down test lint format typecheck check

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
	$(COMPOSE) up app gateway

scan: check-root ## Scan the audited folder (SUBPATH=sub/folder to scan only part of it)
	$(COMPOSE) run --rm app scan run main $(if $(SUBPATH),--path "$(SUBPATH)")
	$(COMPOSE) run --rm app scan show "$$($(COMPOSE) run --rm -T app scan list | head -1 | cut -f1)"

parasites: ## Rebuildable/useless folders of the last scan (SCAN=id SELECT=green ACTION=trash|delete|archive)
	$(COMPOSE) run --rm app scan parasites "$(or $(SCAN),$$($(COMPOSE) run --rm -T app scan list | head -1 | cut -f1))" \
	  $(if $(SELECT),--select "$(SELECT)" --action "$(or $(ACTION),trash)") $(ARGS)

LAST_SCAN = $$($(COMPOSE) run --rm -T app scan list | head -1 | cut -f1)
LAST_DUPES = $$($(COMPOSE) run --rm -T app dupes list | head -1 | cut -f1)

dupes: ## Find exact duplicates (>= 1 KiB) in the last scan (SCAN=id), then show them
	$(COMPOSE) run --rm app dupes run "$(or $(SCAN),$(LAST_SCAN))" $(ARGS)
	$(COMPOSE) run --rm app dupes show "$(LAST_DUPES)"

micro: ## Micro-cleanup: identical files under 1 KiB and empty files of the last scan
	$(COMPOSE) run --rm app dupes run "$(or $(SCAN),$(LAST_SCAN))" --micro $(ARGS)
	$(COMPOSE) run --rm app dupes show "$(LAST_DUPES)"

dupes-show: ## Show an analysis (RUN=id SELECT=G1,G3|all KEEP=n ACTION=trash|delete)
	$(COMPOSE) run --rm app dupes show "$(or $(RUN),$(LAST_DUPES))" \
	  $(if $(SELECT),--select "$(SELECT)" --action "$(or $(ACTION),trash)") $(if $(KEEP),--keep "$(KEEP)") $(ARGS)

search: ## Search the last scan (SCAN=id ARGS="--ext jpg --min-size 10M", see treeaudit search -h)
	$(COMPOSE) run --rm app search "$(or $(SCAN),$(LAST_SCAN))" $(ARGS)

scans: ## List recorded scans
	$(COMPOSE) run --rm app scan list

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
