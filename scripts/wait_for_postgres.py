from __future__ import annotations

import os
import time

from dotenv import load_dotenv

import psycopg


DEFAULT_DSN = "postgresql://jobbot:jobbot@localhost:5432/jobbot"


def main() -> None:
    load_dotenv()
    dsn = os.getenv("DATABASE_URL", DEFAULT_DSN)
    timeout_seconds = float(os.getenv("POSTGRES_WAIT_TIMEOUT_SECONDS", "30"))
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
            print("Postgres is ready.")
            return
        except Exception as exc:  # pragma: no cover - transient readiness check
            last_error = exc
            time.sleep(1)

    raise SystemExit(f"Postgres did not become ready in time: {last_error}")


if __name__ == "__main__":
    main()
