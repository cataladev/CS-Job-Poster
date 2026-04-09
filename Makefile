-include .env

export DATABASE_URL ?= postgresql://jobbot:jobbot@localhost:5432/jobbot
export SQLITE_DB_PATH ?= job_bot.db

PYTHON := .venv/bin/python
PYTEST := .venv/bin/pytest
ALEMBIC := .venv/bin/alembic

.PHONY: postgres-up postgres-down db-migrate db-import-sqlite dev-postgres test-postgres run-postgres

postgres-up:
	docker compose up -d postgres
	$(PYTHON) scripts/wait_for_postgres.py

postgres-down:
	docker compose down

db-migrate:
	$(ALEMBIC) upgrade head

db-import-sqlite:
	$(PYTHON) scripts/import_sqlite_to_postgres.py

run-postgres:
	$(PYTHON) bot.py

dev-postgres: postgres-up db-migrate
	@if [ -f "$(SQLITE_DB_PATH)" ] && [ "$$IMPORT_SQLITE" = "1" ]; then \
		echo "Importing existing SQLite data into Postgres..."; \
		$(PYTHON) scripts/import_sqlite_to_postgres.py; \
	fi
	$(PYTHON) bot.py

test-postgres: postgres-up db-migrate
	$(PYTEST) tests/test_postgres_backend.py -q
