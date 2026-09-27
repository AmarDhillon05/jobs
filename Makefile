# Internship Job Monitor - developer workflow
#
#   make setup            install python + node toolchains
#   make test             levels 1-4 + 6 (no docker, no network)
#   make test-scrapers    level 2 only
#   make test-app         level 5 (both clients: Expo app + web feed)
#   make infra-validate   cdk synth + cdk assertion tests + cfn-lint
#   make local            boot LocalStack and provision the architecture
#   make e2e              level 7+8 end-to-end (boots LocalStack itself)
#   make verify           THE HARD GATE - everything, from clean, with a report
#
.DEFAULT_GOAL := help
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

REPO_ROOT   := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
VENV        := $(REPO_ROOT)/.venv
PY          := $(VENV)/bin/python
PIP         := $(VENV)/bin/python -m pip
PYTEST      := $(VENV)/bin/python -m pytest
RUFF        := $(VENV)/bin/python -m ruff
MYPY        := $(VENV)/bin/python -m mypy
CFN_LINT    := $(VENV)/bin/cfn-lint
CDK         := $(REPO_ROOT)/tools/node_modules/.bin/cdk
CDKLOCAL    := $(REPO_ROOT)/tools/node_modules/.bin/cdklocal
MOBILE      := $(REPO_ROOT)/mobile
WEB         := $(REPO_ROOT)/web
CDK_OUT     := $(REPO_ROOT)/infrastructure/cdk.out
REPORT_DIR  := $(REPO_ROOT)/.verify

LOCALSTACK_URL ?= http://localhost:4566

export PYTHONPATH := $(REPO_ROOT)/src:$(REPO_ROOT)/infrastructure
# `cdk synth` must never try to reach an AWS account.
export CDK_DEFAULT_ACCOUNT ?= 000000000000
export CDK_DEFAULT_REGION  ?= us-east-1

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- setup ------
.PHONY: setup
setup: setup-python setup-node ## Install all toolchains

.PHONY: setup-python
setup-python: ## Create .venv and install python dev dependencies
	test -d $(VENV) || python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip setuptools wheel
	$(PIP) install -q -e '.[dev]'

.PHONY: setup-node
setup-node: ## Install CDK CLI and both client toolchains
	cd $(REPO_ROOT)/tools && npm install --no-fund --no-audit
	cd $(MOBILE) && npm install --no-fund --no-audit
	cd $(WEB) && npm install --no-fund --no-audit

# ----------------------------------------------------------------- lint ------
.PHONY: lint
lint: ## Ruff lint + format check
	$(RUFF) check $(REPO_ROOT)/src $(REPO_ROOT)/infrastructure $(REPO_ROOT)/tests $(REPO_ROOT)/scripts
	$(RUFF) format --check $(REPO_ROOT)/src $(REPO_ROOT)/infrastructure $(REPO_ROOT)/tests $(REPO_ROOT)/scripts

.PHONY: fmt
fmt: ## Autoformat python
	$(RUFF) format $(REPO_ROOT)/src $(REPO_ROOT)/infrastructure $(REPO_ROOT)/tests $(REPO_ROOT)/scripts
	$(RUFF) check --fix $(REPO_ROOT)/src $(REPO_ROOT)/infrastructure $(REPO_ROOT)/tests $(REPO_ROOT)/scripts

.PHONY: typecheck
typecheck: ## mypy on the backend package
	$(MYPY)

# ---------------------------------------------------------------- tests ------
.PHONY: test
test: ## Levels 1-4 + 6: unit, scrapers, persistence, notifications, integration
	$(PYTEST) $(REPO_ROOT)/tests -m "not aws_local and not live" -q

.PHONY: test-unit
test-unit: ## Level 1 only
	$(PYTEST) $(REPO_ROOT)/tests/unit -q

.PHONY: test-scrapers
test-scrapers: ## Level 2 only
	$(PYTEST) $(REPO_ROOT)/tests/scrapers -q

.PHONY: test-integration
test-integration: ## Level 3/4/6: persistence, notifications, pipeline (moto)
	$(PYTEST) $(REPO_ROOT)/tests/integration -q

.PHONY: test-app
test-app: test-mobile test-web ## Level 5: both clients

.PHONY: test-mobile
test-mobile: ## Level 5a: Expo app (jest-expo + React Native Testing Library)
	cd $(MOBILE) && npm run test:run

.PHONY: test-web
test-web: ## Level 5b: web feed (Vitest + Testing Library)
	cd $(WEB) && npm run test:run

.PHONY: test-app-e2e
test-app-e2e: ## Level 5c: web feed in a real browser (Playwright)
	cd $(WEB) && npm run test:e2e

.PHONY: test-live
test-live: ## Opt-in live public-endpoint spot checks (never a completion gate)
	ENABLE_LIVE_TESTS=1 $(PYTEST) $(REPO_ROOT)/tests -m live -q

.PHONY: coverage
coverage: ## Backend tests with coverage report
	$(PYTEST) $(REPO_ROOT)/tests -m "not aws_local and not live" \
	  --cov --cov-report=term-missing --cov-report=xml -q

# ---------------------------------------------------------------- infra ------
# The CDK CLI shells out to `python3 app.py`, so the venv must be first on PATH
# for aws_cdk to be importable.
CDK_ENV := PATH=$(VENV)/bin:$$PATH PYTHONPATH=$(REPO_ROOT)/src:$(REPO_ROOT)/infrastructure

.PHONY: infra-synth
infra-synth: ## cdk synth into infrastructure/cdk.out
	cd $(REPO_ROOT)/infrastructure && $(CDK_ENV) $(CDK) synth --output $(CDK_OUT) --quiet

.PHONY: infra-test
infra-test: ## CDK assertion / template tests
	$(PYTEST) $(REPO_ROOT)/infrastructure/tests -q

.PHONY: infra-lint
infra-lint: infra-synth ## cfn-lint the synthesized CloudFormation
	$(CFN_LINT) $(CDK_OUT)/*.template.json

.PHONY: infra-validate
infra-validate: infra-synth infra-test infra-lint ## All infrastructure validation

# ------------------------------------------------------------ localstack -----
.PHONY: localstack-up
localstack-up: ## Boot LocalStack and wait for health
	docker compose up -d localstack
	$(PY) $(REPO_ROOT)/scripts/wait_for_localstack.py --url $(LOCALSTACK_URL) --timeout 240

.PHONY: localstack-down
localstack-down: ## Tear LocalStack down and remove volumes
	-docker compose down -v --remove-orphans

.PHONY: local-provision
local-provision: ## Create the architecture inside LocalStack
	$(PY) $(REPO_ROOT)/scripts/local_provision.py --url $(LOCALSTACK_URL)

.PHONY: local
local: localstack-up local-provision ## Boot + provision a local cloud
	@echo ""
	@echo "LocalStack architecture is up. Trigger a poll with:"
	@echo "  make local-poll"

.PHONY: local-poll
local-poll: ## Inject one scheduled-poll event into the local architecture
	$(PY) $(REPO_ROOT)/scripts/local_poll.py --url $(LOCALSTACK_URL)

.PHONY: local-health
local-health: ## Print the scraper-health view from the local stack
	$(PY) -m jobmonitor.cli health --url $(LOCALSTACK_URL)

.PHONY: serve-api
serve-api: ## Run the jobs API locally (stdlib http.server, no AWS needed)
	$(PY) -m jobmonitor.api.local_server

.PHONY: serve-app
serve-app: ## Run the web feed's dev server
	cd $(WEB) && npm run dev

.PHONY: serve-mobile
serve-mobile: ## Start Expo for the phone app (scan the QR code with Expo Go)
	cd $(MOBILE) && npm start

.PHONY: mobile-bundle
mobile-bundle: ## Prove the Expo app bundles (Metro + Hermes, no device needed)
	cd $(MOBILE) && npx expo export --platform ios --output-dir $(REPO_ROOT)/build/expo

# ------------------------------------------------------------------ e2e ------
.PHONY: e2e-local
e2e-local: ## Level 8 acceptance scenarios against the in-process/moto stack
	$(PYTEST) $(REPO_ROOT)/tests/e2e -m "not aws_local" -q

.PHONY: e2e-aws
e2e-aws: ## Level 7 architecture test against an ALREADY-RUNNING LocalStack
	AWS_ENDPOINT_URL=$(LOCALSTACK_URL) $(PYTEST) $(REPO_ROOT)/tests/aws_local -m aws_local -q

.PHONY: e2e
e2e: ## Full E2E: boot LocalStack, provision, run levels 7+8, tear down
	$(MAKE) localstack-up
	$(MAKE) local-provision
	$(MAKE) e2e-local
	$(MAKE) e2e-aws
	$(MAKE) localstack-down

# --------------------------------------------------------------- verify ------
.PHONY: verify
verify: ## THE HARD GATE: everything, from clean, with a pass/fail report
	$(PY) $(REPO_ROOT)/scripts/verify.py --report-dir $(REPORT_DIR)

.PHONY: clean
clean: ## Remove caches and build output
	rm -rf $(CDK_OUT) $(REPORT_DIR) $(REPO_ROOT)/build \
	       $(REPO_ROOT)/.pytest_cache $(REPO_ROOT)/.mypy_cache $(REPO_ROOT)/.ruff_cache \
	       $(REPO_ROOT)/.coverage $(REPO_ROOT)/coverage.xml
	find $(REPO_ROOT)/src $(REPO_ROOT)/tests $(REPO_ROOT)/infrastructure $(REPO_ROOT)/scripts \
	     -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ------------------------------------------------------------- registry ------
.PHONY: companies
companies: ## Rebuild companies.json from the seed repositories
	$(PY) $(REPO_ROOT)/scripts/build_company_registry.py

.PHONY: validate-companies
validate-companies: ## Probe every company's LIVE source and update companies.json
	$(PY) $(REPO_ROOT)/scripts/validate_companies.py --write --json $(REPORT_DIR)/validation.json

.PHONY: coverage-report
coverage-report: ## Regenerate COMPANY_COVERAGE.md from companies.json
	$(PY) $(REPO_ROOT)/scripts/company_coverage.py --write
