.PHONY: help bootstrap up down api web contracts test-contract lint test check

help:
	@echo "Mandate Guardian — development targets"
	@echo ""
	@echo "  make bootstrap       Install Python and Node dependencies"
	@echo "  make up              Start Docker Compose services"
	@echo "  make down            Stop Docker Compose services"
	@echo "  make api             Start the FastAPI dev server"
	@echo "  make web             Start the Next.js dev server"
	@echo "  make contracts       Export OpenAPI and generate browser types"
	@echo "  make test-contract   Run contract and health tests"
	@echo "  make lint            Run Ruff, mypy and ESLint"
	@echo "  make test            Run all tests"
	@echo "  make check           Run contracts, lint and test in order"

bootstrap:
	uv --directory apps/api sync --all-groups
	npm --prefix apps/web install

up:
	docker compose up -d

down:
	docker compose down

api:
	uv --directory apps/api run uvicorn app.main:app --reload --port 8000

web:
	npm --prefix apps/web run dev

contracts:
	uv --directory apps/api run python ../../scripts/export_openapi.py
	npm --prefix apps/web run generate:api

test-contract:
	uv --directory apps/api run pytest tests/contract tests/unit/test_health.py -q

lint:
	uv --directory apps/api run ruff check app tests
	uv --directory apps/api run ruff format --check app tests
	uv --directory apps/api run mypy app
	npm --prefix apps/web run lint

test:
	uv --directory apps/api run pytest -q

check: contracts lint test
