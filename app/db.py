"""Postgres connection. Credentials come from DATABASE_* environment variables (.env in dev)."""
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from app.paths import ROOT

SCHEMA_SQL = ROOT / "db" / "schema.sql"


def connect(**kwargs) -> psycopg.Connection:
    load_dotenv(ROOT / ".env")
    host = os.environ["DATABASE_HOST"]
    # Docker publishes the port on 127.0.0.1 only; "localhost" tries ::1 first and hangs until timeout.
    if host == "localhost":
        kwargs.setdefault("hostaddr", "127.0.0.1")
    kwargs.setdefault("connect_timeout", 10)
    return psycopg.connect(
        host=host,
        port=os.environ.get("DATABASE_PORT", "5432"),
        dbname=os.environ["DATABASE_NAME"],
        user=os.environ["DATABASE_USER"],
        password=os.environ["DATABASE_PASSWORD"],
        **kwargs,
    )


def apply_schema(conn: psycopg.Connection, path: Path = SCHEMA_SQL) -> None:
    conn.execute(path.read_text(encoding="utf-8"))
