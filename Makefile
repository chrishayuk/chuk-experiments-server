.PHONY: install dev-install migrate serve test lint format db-up db-down clean

install:
	uv pip install --system .

dev-install:
	uv pip install -e ".[dev]"

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

migrate:
	uv run chuk-experiments-server migrate

serve:
	uv run chuk-experiments-server serve

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

clean:
	find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache dist build *.egg-info
