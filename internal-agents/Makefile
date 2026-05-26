.DEFAULT_GOAL := help

# Use bash for all recipes (Windows GNU make defaults to cmd.exe)
SHELL         := /usr/bin/bash

# uv is installed as a Python package (not on PATH), so invoke via python -m uv
UV            := python -m uv
PYTHON        := .venv/Scripts/python
UVICORN       := .venv/Scripts/python -m uvicorn

# ── Setup ─────────────────────────────────────────────────────────────────────

.PHONY: install
install:           ## Install all dependencies (including dev)
	pip install uv
	$(UV) venv
	$(UV) pip install -e ".[dev]" --python .venv

.PHONY: env
env:               ## Copy .env.example → .env (skips if .env already exists)
	@test -f .env && echo ".env already exists — skipping" || (cp .env.example .env && echo "Created .env — edit it before running with real credentials")

# ── Run ───────────────────────────────────────────────────────────────────────

.PHONY: dev
dev:               ## Start the API in mock mode with live reload
	MOCK_MODE=true $(UVICORN) src.api.main:app --reload --log-level info

.PHONY: dev-debug
dev-debug:         ## Start the API in mock mode with debug logging
	MOCK_MODE=true $(UVICORN) src.api.main:app --reload --log-level debug

.PHONY: start
start:             ## Start the API using .env credentials (real mode)
	$(UVICORN) src.api.main:app --host 0.0.0.0 --port 8000

# ── Demo ──────────────────────────────────────────────────────────────────────

.PHONY: demo
demo:              ## Run three demo curl requests against the running API
	@echo "\n── Health check ──────────────────────────────────────────"
	curl -s http://localhost:8000/health | python -m json.tool
	@echo "\n── Prompt 1: Sprint delivery ─────────────────────────────"
	curl -s -X POST http://localhost:8000/ask \
	  -H "Content-Type: application/json" \
	  -d '{"prompt": "Summarize last sprint delivery vs hours logged"}' \
	  | python -m json.tool
	@echo "\n── Prompt 2: Deals without contracts ────────────────────"
	curl -s -X POST http://localhost:8000/ask \
	  -H "Content-Type: application/json" \
	  -d '{"prompt": "Which deals closed last month dont have a signed contract yet?"}' \
	  | python -m json.tool
	@echo "\n── Prompt 3: Contractor costs ───────────────────────────"
	curl -s -X POST http://localhost:8000/ask \
	  -H "Content-Type: application/json" \
	  -d '{"prompt": "Show contractor cost per project for Q1"}' \
	  | python -m json.tool

.PHONY: stream-demo
stream-demo:       ## Run a WebSocket streaming demo (requires the API to be running)
	$(PYTHON) scripts/ws_demo.py

# ── Test ──────────────────────────────────────────────────────────────────────

.PHONY: test
test:              ## Run the full test suite
	$(PYTHON) -m pytest -v

.PHONY: test-gateway
test-gateway:      ## Run gateway tests only
	$(PYTHON) -m pytest tests/test_gateway.py -v

.PHONY: test-agents
test-agents:       ## Run agent unit tests only
	$(PYTHON) -m pytest tests/test_agents.py -v

.PHONY: test-e2e
test-e2e:          ## Run end-to-end pipeline tests only
	$(PYTHON) -m pytest tests/test_e2e.py -v

# ── Misc ──────────────────────────────────────────────────────────────────────

.PHONY: help
help:              ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
