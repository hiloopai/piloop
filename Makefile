.DEFAULT_GOAL := all
PYTEST_WORKERS ?= 8
SUITE_MEMORY ?= 24G

.PHONY: install
install:  ## Install deps and pre-commit hooks
	uv sync --frozen
	uv run pre-commit install --install-hooks
	git config merge.generated.driver true

.PHONY: format
format:  ## Format the code
	uv run ruff format
	uv run ruff check --fix --fix-only

.PHONY: lint
lint:  ## Lint the code
	uv run ruff format --check
	uv run ruff check

.PHONY: typecheck
typecheck:  ## Static type check
	uv run ty check

.PHONY: test
test:  ## Run tests in parallel (no coverage)
	COLUMNS=150 uv run pytest -n $(PYTEST_WORKERS) --durations=10

.PHONY: testcov
testcov:  ## Run tests in parallel with coverage; fails under 100%
	COLUMNS=150 uv run pytest -n $(PYTEST_WORKERS) --durations=10 --cov --cov-report=term-missing:skip-covered

.PHONY: quarantine
quarantine:  ## Run only the quarantined flaky tests (tests/quarantine.toml); none quarantined passes
	COLUMNS=150 uv run pytest -n $(PYTEST_WORKERS) -m quarantine -rA || [ $$? -eq 5 ]

.PHONY: e2e
e2e:  ## Run the end-to-end scenarios with real agents (about 15 minutes and 1 USD)
	COLUMNS=150 uv run pytest -m e2e tests/e2e -rA

.PHONY: docs
docs:  ## Regenerate the generated docs and the trigger SQL from code
	uv run -m scripts.generate_all

.PHONY: docs-check
docs-check:  ## Fail if a generated file is stale
	uv run -m scripts.generate_all --mode check

.PHONY: changelog
changelog:  ## Write the section of the version in pyproject.toml into CHANGELOG.md, from the commits since the last tag
	uv run --group release git-cliff --unreleased --tag "v$$(uv version --short)" --prepend CHANGELOG.md

.PHONY: all
all: format lint typecheck testcov docs

.PHONY: shared
shared:  ## Run `all` with 16 workers on a machine that others share: one suite at a time, OOM-killed past SUITE_MEMORY (Linux, systemd)
	flock /tmp/piloop-suite.lock systemd-run --user --scope -q -p MemoryMax=$(SUITE_MEMORY) -p MemorySwapMax=0 -- $(MAKE) all PYTEST_WORKERS=16
