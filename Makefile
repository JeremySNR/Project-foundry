# Convenience targets. Everything works without make; these just save typing.

.PHONY: dev test lint policy up down migrate image

dev:  ## run the API locally with auto-reload (SQLite)
	uvicorn foundry.api.app:app_from_env --factory --reload --port 8000

test:  ## full offline test suite
	pytest -q

lint:
	ruff check src tests

policy:  ## OPA policy tests (requires opa on PATH)
	opa test src/foundry/policy -v

up:  ## API + Postgres via docker compose
	docker compose up --build

down:
	docker compose down

migrate:  ## apply Alembic migrations to FOUNDRY_DATABASE_URL (the API container does this automatically on startup)
	# Against the docker compose stack from the host, point at the published port:
	#   FOUNDRY_DATABASE_URL=postgresql+psycopg2://foundry:foundry@localhost:5432/foundry make migrate
	alembic upgrade head

image:
	docker build -t project-foundry .
