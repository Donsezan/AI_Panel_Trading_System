PY := .venv/bin/python

.PHONY: check format lint types test coverage

check: lint types test coverage

format:
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

lint:
	$(PY) -m ruff format --check .
	$(PY) -m ruff check .

types:
	$(PY) -m mypy tradebot

test:
	$(PY) -m pytest --cov --cov-report=term-missing:skip-covered --cov-report=json

coverage:
	$(PY) scripts/coverage_gate.py
