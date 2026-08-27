SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

ROOT := $(shell pwd)
EXISTING_PYTHONPATH := $(PYTHONPATH)
LOCAL_VENV := $(ROOT)/.venv
LOCAL_PYTHON := $(LOCAL_VENV)/bin/python
export PYTHONPATH := $(ROOT)$(if $(EXISTING_PYTHONPATH),:$(EXISTING_PYTHONPATH))

ifneq ("$(wildcard $(LOCAL_PYTHON))","")
PYTHON := $(LOCAL_PYTHON)
else
PYTHON := python3
endif

UV_VERSION := 0.11.32
GITLEAKS_VERSION := 8.30.1
XDG_CACHE_HOME ?= $(HOME)/.cache
UV_PREFIX := $(XDG_CACHE_HOME)/endure/uv-$(UV_VERSION)
UV := $(UV_PREFIX)/bin/uv
export ENDURE_UV := $(UV)
BOOTSTRAP_PYTHON ?= python3.12
UV_BOOTSTRAP_REQUIREMENTS := docker/uv-bootstrap-requirements.txt
PYTEST := $(PYTHON) -m pytest
NPM ?= npm
JSCPD_VERSION := 5.0.16
JSCPD := $(ROOT)/node_modules/.bin/jscpd

.PHONY: help ensure-bootstrap-python bootstrap seeder-install install dev-install lint format typecheck test test-ci migrations guardrails ensure-node-tools check-duplication verify verify-ci clean dev dev-miner devnet-cycle devnet-fault-miner devnet-fault-validator devnet-fault-miner-state-loss devnet-faults ensure-uv ensure-gitleaks ensure-verify-deps regen-stubs

help:
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

ensure-bootstrap-python:
	@if ! command -v "$(BOOTSTRAP_PYTHON)" >/dev/null 2>&1 || \
	   ! "$(BOOTSTRAP_PYTHON)" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' 2>/dev/null; then \
		printf 'A Python 3.12 interpreter is required; %s is missing or is not 3.12.\n' "$(BOOTSTRAP_PYTHON)"; \
		printf 'Install one:\n'; \
		printf '  macOS:   brew install python@3.12   (or: uv python install 3.12)\n'; \
		printf '  Debian:  apt install python3.12 python3.12-venv\n'; \
		printf 'Then point make at it if it is not on PATH as python3.12:\n'; \
		printf '  make bootstrap BOOTSTRAP_PYTHON=/path/to/python3.12\n'; \
		exit 1; \
	fi

bootstrap: ensure-bootstrap-python ## Install pinned uv and Gitleaks in the local user cache
	@$(BOOTSTRAP_PYTHON) -c 'from pathlib import Path; import sys; expected = "uv==$(UV_VERSION)"; requirement = next((line for line in Path("$(UV_BOOTSTRAP_REQUIREMENTS)").read_text(encoding="utf-8").splitlines() if line.startswith("uv==")), "").rstrip(" \\"); sys.exit(0 if requirement == expected else f"$(UV_BOOTSTRAP_REQUIREMENTS) pins {requirement!r}; expected {expected!r}")'
	$(BOOTSTRAP_PYTHON) -m venv --clear "$(UV_PREFIX)"
	"$(UV_PREFIX)/bin/python" -m pip install --require-hashes --no-deps -r "$(UV_BOOTSTRAP_REQUIREMENTS)"
	@$(UV) --version
	$(BOOTSTRAP_PYTHON) -m scripts.quality_gates.gitleaks install

install: ensure-uv ## Install package as editable (runtime deps only)
	$(UV) sync --locked --no-dev

dev-install: ensure-uv ## Install package with dev extras (tests, lint, types)
	$(UV) sync --locked --extra dev

seeder-install: ensure-bootstrap-python ## Install the hash-locked btcli toolchain for scripts/dev/seed_chain.sh into .venv-seeder
	$(BOOTSTRAP_PYTHON) -m venv --clear .venv-seeder
	.venv-seeder/bin/python -m pip install --no-cache-dir \
		--require-hashes -r scripts/dev/seeder-requirements.txt
	.venv-seeder/bin/btcli --version

ensure-uv:
	@if ! test -x "$(UV)"; then \
		printf 'uv $(UV_VERSION) is required but is not installed.\n'; \
		printf 'Install it with: make bootstrap\n'; \
		exit 1; \
	fi; \
	if ! found_version="$$($(UV) --version 2>&1)"; then \
		printf 'uv $(UV_VERSION) is required but could not run: %s\n' "$$found_version"; \
		printf 'Reinstall it with: make bootstrap\n'; \
		exit 1; \
	fi; \
	actual_version="$$(printf '%s\n' "$$found_version" | cut -d ' ' -f 1-2)"; \
	if test "$$actual_version" != "uv $(UV_VERSION)"; then \
		printf 'uv $(UV_VERSION) is required; found %s\n' "$$found_version"; \
		printf 'Install the exact pinned version with: make bootstrap\n'; \
		exit 1; \
	fi

ensure-gitleaks:
	@$(PYTHON) -c 'from scripts.quality_gates.gitleaks import GITLEAKS_VERSION, default_binary_path, require_version; assert GITLEAKS_VERSION == "$(GITLEAKS_VERSION)"; require_version(default_binary_path())'

ensure-verify-deps: dev-install

lint: ## Run ruff check + format check
	$(PYTHON) -m ruff check endure/ neurons/ scripts/ tests/ verify/
	$(PYTHON) -m ruff format --check endure/ neurons/ scripts/ tests/ verify/

format: ## Apply ruff format + fix
	$(PYTHON) -m ruff check --fix endure/ neurons/ scripts/ tests/ verify/
	$(PYTHON) -m ruff format endure/ neurons/ scripts/ tests/ verify/

typecheck: ## Run pyright only
	$(PYTHON) -m pyright

regen-stubs: ## Refresh bittensor type stubs (review diff before committing!)
	@echo "Regenerating bittensor auto-stubs into /tmp/endure-stubgen ..."
	@rm -rf /tmp/endure-stubgen
	@mkdir -p /tmp/endure-stubgen
	@cd /tmp/endure-stubgen && \
		VIRTUAL_ENV=$(PWD)/.venv PATH=$(PWD)/.venv/bin:$$PATH \
		$(PWD)/.venv/bin/python -m pyright --createstub bittensor
	@echo ""
	@echo "Auto-generated stubs at /tmp/endure-stubgen/typings/bittensor/."
	@echo "Inspect the upstream __all__ surface:"
	@echo "  cat /tmp/endure-stubgen/typings/bittensor/utils/easy_imports.pyi"
	@echo ""
	@echo "Compare against our hand-curated declarations:"
	@echo "  cat typings/bittensor/__init__.pyi"
	@echo ""
	@echo "If a name we depend on dropped from upstream's __all__, EITHER fix"
	@echo "the call sites OR drop the name from the hand-curated stub."
	@echo "Don't blindly overwrite — pyright auto-gen is noisy and incomplete."

test: ## Run all tests
	$(PYTEST) tests/ -v

test-ci: ## Run tests with the release coverage floor
	$(PYTEST) tests/ -v \
		--cov=endure \
		--cov-report=term-missing \
		--cov-report=xml \
		--cov-fail-under=92

migrations: ## Run migration verification tests
	$(PYTEST) tests/storage/test_migrations.py -v

guardrails: ensure-gitleaks ## Run Endure-specific repository and domain guardrails
	$(PYTHON) -m scripts.quality_gates.checks canonical-json
	$(PYTHON) -m scripts.quality_gates.checks decimal-policy
	$(PYTHON) -m scripts.quality_gates.checks spec-references
	ENDURE_ACTIVATION_LINEAGE_REF=$${ENDURE_ACTIVATION_LINEAGE_REF:-origin/staging} \
		$(PYTHON) -m scripts.quality_gates.checks protocol-version
	$(PYTHON) -m scripts.quality_gates.checks markdown-links
	$(PYTHON) -m scripts.quality_gates.public_release_scan --git-tree
	$(PYTHON) -m scripts.quality_gates.gitleaks scan

ensure-node-tools:
	$(NPM) ci --ignore-scripts --no-audit --no-fund
	@test "$$($(JSCPD) --version)" = "cpd $(JSCPD_VERSION)"

check-duplication: ensure-node-tools ## Run jscpd + pylint R0801 duplicate-code checks
	$(JSCPD) --config .jscpd.json endure
	PYLINTHOME=/tmp/endure-pylint $(PYTHON) -m pylint \
		--disable=all --enable=R0801 --min-similarity-lines=10 \
		endure/

verify: lint typecheck test migrations guardrails check-duplication ## Full local quality gate

verify-ci: ## CI-parity verification
	+$(MAKE) ensure-verify-deps
	+$(MAKE) lint typecheck test-ci migrations guardrails check-duplication

dev: ## Run validator in mock mode (kill with Ctrl+C)
	@trap 'kill -INT "$$child" 2>/dev/null; wait "$$child"; exit 0' INT TERM; \
		$(PYTHON) neurons/validator.py --mock --netuid 1 --wallet.name test --wallet.hotkey test --endure.api_port 8714 & child=$$!; \
		wait "$$child"

dev-miner: ## Run miner in mock mode (kill with Ctrl+C)
	@trap 'kill -INT "$$child" 2>/dev/null; wait "$$child"; exit 0' INT TERM; \
		$(PYTHON) neurons/miner.py --mock --netuid 1 --wallet.name test --wallet.hotkey test --logging.debug & child=$$!; \
		wait "$$child"

devnet-cycle: ## Run Alpha Risk R5 compressed full cycle against an already-running local subtensor
	$(PYTHON) scripts/run_devnet_cycle.py --netuid $${NETUID:?set NETUID from scripts/dev/seed_chain.sh} --network $${NETWORK:-ws://127.0.0.1:9946} $(if $(WALLET_PATH),--wallet-path "$(WALLET_PATH)")

devnet-fault-miner: ## Restart the Alpha miner after its commit and require full recovery
	$(PYTHON) scripts/run_devnet_cycle.py --netuid $${NETUID:?set NETUID from scripts/dev/seed_chain.sh} --network $${NETWORK:-ws://127.0.0.1:9946} $(if $(WALLET_PATH),--wallet-path "$(WALLET_PATH)") --fault miner-restart-after-commit --round-seconds 240 --timeout-seconds 720

devnet-fault-validator: ## Restart the Alpha validator after accepting a commit and require full recovery
	$(PYTHON) scripts/run_devnet_cycle.py --netuid $${NETUID:?set NETUID from scripts/dev/seed_chain.sh} --network $${NETWORK:-ws://127.0.0.1:9946} $(if $(WALLET_PATH),--wallet-path "$(WALLET_PATH)") --fault validator-restart-after-commit --round-seconds 240 --timeout-seconds 720

devnet-fault-miner-state-loss: ## Wipe the miner's state after its commit and require the lost round to be surfaced
	$(PYTHON) scripts/run_devnet_cycle.py --netuid $${NETUID:?set NETUID from scripts/dev/seed_chain.sh} --network $${NETWORK:-ws://127.0.0.1:9946} $(if $(WALLET_PATH),--wallet-path "$(WALLET_PATH)") --fault miner-state-loss-after-commit --round-seconds 240 --timeout-seconds 720

devnet-faults: devnet-fault-miner devnet-fault-validator devnet-fault-miner-state-loss ## Run every commit/reveal fault scenario

clean: ## Remove build artifacts and caches
	rm -rf build/ dist/ *.egg-info endure.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name .pytest_cache -prune -exec rm -rf {} +
	find . -type d -name .ruff_cache -prune -exec rm -rf {} +
	rm -rf .coverage coverage.xml htmlcov/
