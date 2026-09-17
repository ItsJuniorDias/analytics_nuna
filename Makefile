# Atalhos locais. Requer python3 (3.9+).
PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin
HOST ?= 127.0.0.1
PORT ?= 8000
DAYS ?= 180

# Carrega .env (se existir) no shell de cada receita.
LOAD_ENV := set -a; [ -f .env ] && . ./.env; set +a;

.PHONY: setup test run purge purge-dry-run docker-up clean

setup: $(BIN)/uvicorn

$(BIN)/uvicorn: requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt
	@touch $@

test: setup
	$(BIN)/python -m pytest -q

# --no-access-log e --no-proxy-headers são obrigatórios: sem eles o uvicorn
# loga o IP de cada requisição.
run: setup
	$(LOAD_ENV) $(BIN)/uvicorn app.main:app --host $(HOST) --port $(PORT) --workers 1 --no-access-log --no-proxy-headers --no-server-header

purge: setup
	$(LOAD_ENV) $(BIN)/python -m app.purge --days $(DAYS)

purge-dry-run: setup
	$(LOAD_ENV) $(BIN)/python -m app.purge --days $(DAYS) --dry-run

docker-up:
	docker compose up -d --build

clean:
	rm -rf .pytest_cache
	find . -path ./$(VENV) -prune -o -name __pycache__ -type d -exec rm -rf {} +
