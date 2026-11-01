"""Idempotent, non-destructive migrations. Adapts to the existing clients table
(inspects its id type) and never drops/truncates/overwrites anything."""
from __future__ import annotations
import logging, re

log = logging.getLogger("stackwealth.migrate")
SAFE_TYPES = {"uuid", "integer", "bigint", "smallint", "text", "character varying"}
CLIENT_EXTRA_COLS = {
    "email": "text", "phone": "text", "notes": "text",
    "status": "text NOT NULL DEFAULT 'active'",
    "created_at": "timestamptz NOT NULL DEFAULT now()",
    "updated_at": "timestamptz NOT NULL DEFAULT now()",
}


class SchemaError(RuntimeError):
    pass


def _cols(cur, table):
    cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s", (table,))
    return {r[0]: r[1] for r in cur.fetchall()}


def migrate(conn) -> list[str]:
    done: list[str] = []
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(727274)")
        cols = _cols(cur, "clients")
        if not cols:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            cur.execute("CREATE TABLE clients (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL)")
            done.append("created clients")
            cols = _cols(cur, "clients")
        if "id" not in cols or "name" not in cols:
            raise SchemaError("Existing 'clients' table must have 'id' and 'name' columns; found: " + ", ".join(sorted(cols)))
        id_type = cols["id"]
        if id_type not in SAFE_TYPES:
            raise SchemaError(f"Unsupported clients.id type '{id_type}'.")
        sql_type = {"character varying": "text"}.get(id_type, id_type)
        for c, ddl in CLIENT_EXTRA_COLS.items():
            if c not in cols:
                cur.execute(f'ALTER TABLE clients ADD COLUMN "{c}" {ddl}')
                done.append(f"clients.{c} added")
        cur.execute("UPDATE clients SET status='active' WHERE status IS NULL OR status NOT IN ('active','archived')")
        cur.execute("""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='clients_status_chk') THEN
              ALTER TABLE clients ADD CONSTRAINT clients_status_chk CHECK (status IN ('active','archived')) NOT VALID;
            END IF; END $$""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS current_holdings (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            client_id {sql_type} NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
            symbol text NOT NULL,
            quantity numeric(20,4) NOT NULL CHECK (quantity > 0),
            avg_price numeric(20,4) NOT NULL CHECK (avg_price >= 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT current_holdings_client_symbol_uq UNIQUE (client_id, symbol))""")
        cur.execute("CREATE INDEX IF NOT EXISTS current_holdings_symbol_idx ON current_holdings(symbol)")
        cur.execute("""CREATE TABLE IF NOT EXISTS price_cache (
            symbol text PRIMARY KEY,
            price numeric(20,4) NOT NULL CHECK (price > 0),
            price_date date NOT NULL,
            retrieved_at timestamptz NOT NULL DEFAULT now(),
            source text NOT NULL DEFAULT 'yfinance',
            status text, diagnostics jsonb)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS sw_audit_log (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            actor text NOT NULL, action text NOT NULL, entity text,
            entity_id text, details jsonb)""")
        cur.execute("CREATE INDEX IF NOT EXISTS sw_audit_log_time_idx ON sw_audit_log(occurred_at DESC)")
        cur.execute("CREATE TABLE IF NOT EXISTS sw_schema_version (version int PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        cur.execute("INSERT INTO sw_schema_version(version) VALUES (1) ON CONFLICT DO NOTHING")
    log.info("migration complete: %s", done or "no changes")
    return done


def validate_schema(conn) -> list[str]:
    cur = conn.cursor()
    problems = []
    for t, need in {"clients": {"id", "name", "status", "email", "phone", "notes"},
                    "current_holdings": {"client_id", "symbol", "quantity", "avg_price"},
                    "price_cache": {"symbol", "price", "price_date", "retrieved_at"},
                    "sw_audit_log": {"actor", "action"}}.items():
        c = _cols(cur, t)
        if not c: problems.append(f"Missing table: {t}")
        else:
            miss = need - set(c)
            if miss: problems.append(f"Table {t} missing columns: {', '.join(sorted(miss))}")
    return problems
