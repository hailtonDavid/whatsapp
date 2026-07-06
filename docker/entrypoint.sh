#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f /app/config/targets.json ]]; then
  cp /app/config/targets.example.json /app/config/targets.json
  echo "[Docker] Criado config/targets.json a partir do exemplo."
fi

python - <<'PY'
import os
import sys
import time


def wait(label: str, probe, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            if probe():
                print(f"[Docker] {label} pronto.")
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(2)
    print(f"[Docker] Timeout aguardando {label}: {last_error}", file=sys.stderr)
    sys.exit(1)


mongo_uri = os.getenv("MONGODB_URI", "").strip()
if mongo_uri and mongo_uri != "memory://":
    from pymongo import MongoClient

    def mongo_ok() -> bool:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
        client.admin.command("ping")
        client.close()
        return True

    wait("MongoDB", mongo_ok)

pg_uri = os.getenv("SEMANTIC_DB_URI", "").strip()
if pg_uri and pg_uri != "memory://":
    import psycopg

    def pg_ok() -> bool:
        with psycopg.connect(pg_uri, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True

    wait("PostgreSQL", pg_ok)
PY

exec "$@"
