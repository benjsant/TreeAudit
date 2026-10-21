# TreeAudit — development shortcuts. Everything runs in Docker; nothing is
# installed on the host (docs/DECISIONS.md D-019).

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Precedence: command line (make up TREEAUDIT_ROOT_PATH=...), then the shell
# environment, then .env. Assignments in an included file would otherwise hide
# the environment, so remember it before reading .env.
SETTINGS := TREEAUDIT_ROOT_PATH TREEAUDIT_ALLOW_SYSTEM_VOLUME TREEAUDIT_PORT TREEAUDIT_ARCHIVE_PATH TREEAUDIT_EXTERNAL_DISKS
$(foreach v,$(SETTINGS),$(eval _FROM_ENV_$(v) := $(if $(filter environment command line,$(origin $(v))),$(value $(v)))))
-include .env
$(foreach v,$(SETTINGS),$(if $(_FROM_ENV_$(v)),$(eval override $(v) := $(_FROM_ENV_$(v)))))
# A trailing "/" typed by the user is harmless: strip it before validation, so
# that what is validated and what is mounted are the same normalized path.
override TREEAUDIT_ROOT_PATH := $(patsubst %/,%,$(TREEAUDIT_ROOT_PATH))
override TREEAUDIT_ARCHIVE_PATH := $(patsubst %/,%,$(TREEAUDIT_ARCHIVE_PATH))
# Hand compose exactly the values make validated, so check-root and the actual
# mount can never disagree.
export TREEAUDIT_UID TREEAUDIT_GID $(SETTINGS)
# Host time zone (e.g. Europe/Paris), so that archive names use local time.
TREEAUDIT_TZ ?= $(shell readlink /etc/localtime 2>/dev/null | sed -n 's|^.*/zoneinfo/||p')
export TREEAUDIT_TZ

# Base: images, tools and tests. It mounts no audited folder.
BASE_FILES := -f compose.yaml
ifeq ($(COMPOSE_SELINUX),1)
BASE_FILES += -f compose.selinux.yaml
endif
COMPOSE_BASE := docker compose $(BASE_FILES)

# Running TreeAudit: each audited folder is mounted by an override, only once
# it has been validated on the host (check-root, disks-file).
COMPOSE_FILES := $(BASE_FILES)
ifneq ($(TREEAUDIT_ROOT_PATH),)
COMPOSE_FILES += -f compose.root.yaml
endif
# The archive directory is mounted (read-write) only when it is set (D-032).
ifneq ($(TREEAUDIT_ARCHIVE_PATH),)
COMPOSE_FILES += -f compose.archive.yaml
endif
# External disks (D-033): generated before every use from the host's current
# mounts, outside data/ (which the container can write to).
DISKS_FILE := .treeaudit-disks.json
EXTERNAL := $(filter 1,$(TREEAUDIT_EXTERNAL_DISKS))
ifneq ($(EXTERNAL),)
COMPOSE_FILES += -f $(DISKS_FILE)
endif
COMPOSE := docker compose $(COMPOSE_FILES)
DEV := $(COMPOSE_BASE) run --rm dev
# Root used by the command-line targets: the folder of .env, or a disk id
# shown by `make disks` (ROOT=ext-...).
ROOT ?= main
HOST_IDS := --user "$$(id -u):$$(id -g)"

.PHONY: help env data lock build disks-file disks check-root up scan parasites dupes micro dupes-show search archive bench scans down test lint format typecheck check

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
	    echo "TREEAUDIT_PORT=8765"; \
	    echo "# Optional: existing folder where TreeAudit may write verified archives."; \
	    echo "# Keep it on another disk than the audited folder. Empty: archiving off."; \
	    echo "TREEAUDIT_ARCHIVE_PATH="; \
	    echo "# Set to 1 to choose external disks in the interface (never the system disk)."; \
	    echo "TREEAUDIT_EXTERNAL_DISKS=0"; } > .env; \
	  chmod 600 .env; echo ".env created: now set TREEAUDIT_ROOT_PATH and/or TREEAUDIT_EXTERNAL_DISKS=1"; fi

data: ## Create the local data directory (mode 700)
	@mkdir -p -m 700 data

lock: ## Resolve dependencies and update uv.lock (needs network)
	docker build -f docker/Dockerfile --target uvtool -t treeaudit-uv:local .
	docker run --rm $(HOST_IDS) -e HOME=/tmp -v "$(CURDIR):/app" -w /app treeaudit-uv:local uv lock

build: ## Build the dev and app images
	$(COMPOSE_BASE) --profile dev build

# Regenerated before every use: the disks plugged in now, validated now.
disks-file: build data
ifneq ($(EXTERNAL),)
	@$(COMPOSE_BASE) run --rm -T check-disks --user "$$(id -un)" --data-dir "$(realpath $(CURDIR)/data)" \
	  < /proc/self/mountinfo > $(DISKS_FILE).tmp && mv -f $(DISKS_FILE).tmp $(DISKS_FILE)
endif

disks: disks-file ## List the external disks offered or refused (TREEAUDIT_EXTERNAL_DISKS=1)
	@test -n "$(EXTERNAL)" || echo "Disques externes désactivés : mettre TREEAUDIT_EXTERNAL_DISKS=1 dans .env"

check-root: build data disks-file ## Validate the audited folders (.env, external disks, archives) on the host before mounting
ifneq ($(TREEAUDIT_ROOT_PATH),)
	@test -d "$(TREEAUDIT_ROOT_PATH)" || { echo "REFUSÉ: '$(TREEAUDIT_ROOT_PATH)' n'est pas un dossier existant"; exit 1; }
	@real="$$(realpath -e -- "$(TREEAUDIT_ROOT_PATH)")" && test "$$real" = "$(TREEAUDIT_ROOT_PATH)" || \
	  { echo "REFUSÉ: '$(TREEAUDIT_ROOT_PATH)' passe par un lien symbolique ou n'est pas normalisé (chemin réel : $$real)"; exit 1; }
	@$(COMPOSE_BASE) run --rm -T check \
	  $(if $(filter 1,$(TREEAUDIT_ALLOW_SYSTEM_VOLUME)),--allow-system-volume) \
	  --home "$(HOME)" --data-dir "$(CURDIR)/data" -- "$(TREEAUDIT_ROOT_PATH)" < /proc/self/mountinfo
else
	@test -n "$(EXTERNAL)" || { echo "Rien à analyser : indiquer TREEAUDIT_ROOT_PATH ou mettre TREEAUDIT_EXTERNAL_DISKS=1 dans .env (run 'make env')"; exit 2; }
endif
ifneq ($(TREEAUDIT_ARCHIVE_PATH),)
	@test -d "$(TREEAUDIT_ARCHIVE_PATH)" || { echo "REFUSÉ: dossier d'archives '$(TREEAUDIT_ARCHIVE_PATH)' inexistant (le créer d'abord)"; exit 1; }
	@real="$$(realpath -e -- "$(TREEAUDIT_ARCHIVE_PATH)")" && test "$$real" = "$(TREEAUDIT_ARCHIVE_PATH)" || \
	  { echo "REFUSÉ: '$(TREEAUDIT_ARCHIVE_PATH)' passe par un lien symbolique ou n'est pas normalisé (chemin réel : $$real)"; exit 1; }
	@test -w "$(TREEAUDIT_ARCHIVE_PATH)" || { echo "REFUSÉ: '$(TREEAUDIT_ARCHIVE_PATH)' n'est pas accessible en écriture"; exit 1; }
	@$(COMPOSE_BASE) run --rm -T check-archive --home "$(HOME)" --data-dir "$(realpath $(CURDIR)/data)" \
	  $(if $(TREEAUDIT_ROOT_PATH),--root "$(TREEAUDIT_ROOT_PATH)") -- "$(TREEAUDIT_ARCHIVE_PATH)" < /proc/self/mountinfo
endif

up: check-root ## Validate the audited folder, then start TreeAudit on http://127.0.0.1:$(TREEAUDIT_PORT)
	$(COMPOSE) up app gateway

scan: check-root ## Scan the audited folder (ROOT=ext-... for a disk, SUBPATH=sub/folder for part of it)
	$(COMPOSE) run --rm app scan run "$(ROOT)" $(if $(SUBPATH),--path "$(SUBPATH)")
	$(COMPOSE) run --rm app scan show "$$($(COMPOSE) run --rm -T app scan list | head -1 | cut -f1)"

parasites: disks-file ## Rebuildable/useless folders of the last scan (SCAN=id SELECT=green ACTION=trash|delete|archive)
	$(COMPOSE) run --rm app scan parasites "$(or $(SCAN),$$($(COMPOSE) run --rm -T app scan list | head -1 | cut -f1))" \
	  $(if $(SELECT),--select "$(SELECT)" --action "$(or $(ACTION),trash)") $(ARGS)

LAST_SCAN = $$($(COMPOSE) run --rm -T app scan list | head -1 | cut -f1)
LAST_DUPES = $$($(COMPOSE) run --rm -T app dupes list | head -1 | cut -f1)

dupes: disks-file ## Find exact duplicates (>= 1 KiB) in the last scan (SCAN=id), then show them
	$(COMPOSE) run --rm app dupes run "$(or $(SCAN),$(LAST_SCAN))" $(ARGS)
	$(COMPOSE) run --rm app dupes show "$(LAST_DUPES)"

micro: disks-file ## Micro-cleanup: identical files under 1 KiB and empty files of the last scan
	$(COMPOSE) run --rm app dupes run "$(or $(SCAN),$(LAST_SCAN))" --micro $(ARGS)
	$(COMPOSE) run --rm app dupes show "$(LAST_DUPES)"

dupes-show: disks-file ## Show an analysis (RUN=id SELECT=G1,G3|all KEEP=n ACTION=trash|delete)
	$(COMPOSE) run --rm app dupes show "$(or $(RUN),$(LAST_DUPES))" \
	  $(if $(SELECT),--select "$(SELECT)" --action "$(or $(ACTION),trash)") $(if $(KEEP),--keep "$(KEEP)") $(ARGS)

search: disks-file ## Search the last scan (SCAN=id ARGS="--ext jpg --min-size 10M", see treeaudit search -h)
	$(COMPOSE) run --rm app search "$(or $(SCAN),$(LAST_SCAN))" $(ARGS)

archive: check-root ## Verified archive of the audited folder into TREEAUDIT_ARCHIVE_PATH (SUBPATH=sub/folder, ARGS=--xz)
	@test -n "$(TREEAUDIT_ARCHIVE_PATH)" || { echo "TREEAUDIT_ARCHIVE_PATH est vide dans .env : indiquer un dossier d'archives"; exit 2; }
	$(COMPOSE) run --rm app archive run "$(ROOT)" $(if $(SUBPATH),--path "$(SUBPATH)") $(ARGS)

bench: check-root ## Measure performance on the folder of .env (numbers only; ARGS="--dupes --micro")
	$(COMPOSE) run --rm app bench "$(ROOT)" $(if $(SUBPATH),--path "$(SUBPATH)") $(ARGS)

scans: disks-file ## List recorded scans
	$(COMPOSE) run --rm app scan list

down: ## Stop TreeAudit
	$(COMPOSE_BASE) down

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
