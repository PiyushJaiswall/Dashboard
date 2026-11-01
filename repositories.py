"""Data access. All SQL is parameterised; each public function is one atomic unit of work
on the supplied connection (caller commits via database.connect)."""
from __future__ import annotations
import json, re
from decimal import Decimal
from typing import Any, Optional
from market_data import normalize_symbol
from portfolio import validate_price, validate_quantity

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^\+?[0-9][0-9 \-()]{6,18}$")


class NotFound(LookupError):
    pass


def validate_client(name: str, email: str = "", phone: str = "", notes: str = "") -> dict:
    name = (name or "").strip()
    if not 1 <= len(name) <= 120:
        raise ValueError("Client name is required (max 120 characters).")
    email, phone = (email or "").strip(), (phone or "").strip()
    if email and not _EMAIL.match(email): raise ValueError("Email address looks invalid.")
    if phone and not _PHONE.match(phone): raise ValueError("Phone number looks invalid.")
    return {"name": name, "email": email or None, "phone": phone or None, "notes": (notes or "").strip() or None}


def audit(conn, actor: str, action: str, entity: str = None, entity_id: Any = None, details: dict = None):
    safe = {k: v for k, v in (details or {}).items()
            if not re.search(r"pass|secret|hash|token|url|key", k, re.I)}
    conn.execute("INSERT INTO sw_audit_log(actor, action, entity, entity_id, details) VALUES (%s,%s,%s,%s,%s)",
                 (actor, action, entity, None if entity_id is None else str(entity_id), json.dumps(safe, default=str)))


def _rows(cur):
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def list_clients(conn, status: Optional[str] = "active", search: str = "") -> list[dict]:
    q, a = "SELECT id, name, email, phone, notes, status, created_at, updated_at FROM clients WHERE 1=1", []
    if status: q += " AND status=%s"; a.append(status)
    if search.strip(): q += " AND name ILIKE %s"; a.append(f"%{search.strip()}%")
    return _rows(conn.execute(q + " ORDER BY name", a))


def get_client(conn, client_id) -> dict:
    r = _rows(conn.execute("SELECT id, name, email, phone, notes, status FROM clients WHERE id=%s", (client_id,)))
    if not r: raise NotFound("Client not found.")
    return r[0]


def create_client(conn, actor, **f) -> Any:
    v = validate_client(**f)
    cid = conn.execute("INSERT INTO clients(name,email,phone,notes,status) VALUES (%s,%s,%s,%s,'active') RETURNING id",
                       (v["name"], v["email"], v["phone"], v["notes"])).fetchone()[0]
    audit(conn, actor, "client.create", "client", cid, {"name": v["name"]})
    return cid


def update_client(conn, actor, client_id, **f):
    v = validate_client(**f); get_client(conn, client_id)
    conn.execute("UPDATE clients SET name=%s,email=%s,phone=%s,notes=%s,updated_at=now() WHERE id=%s",
                 (v["name"], v["email"], v["phone"], v["notes"], client_id))
    audit(conn, actor, "client.update", "client", client_id, {"name": v["name"]})


def set_client_status(conn, actor, client_id, status: str):
    if status not in ("active", "archived"): raise ValueError("Invalid status.")
    get_client(conn, client_id)
    conn.execute("UPDATE clients SET status=%s, updated_at=now() WHERE id=%s", (status, client_id))
    audit(conn, actor, "client.archive" if status == "archived" else "client.reactivate", "client", client_id)


def upsert_holding(conn, actor, client_id, symbol, quantity, avg_price) -> str:
    """Returns 'created' or 'updated'. Validates client server-side."""
    sym, q, p = normalize_symbol(symbol), validate_quantity(quantity), validate_price(avg_price)
    if get_client(conn, client_id)["status"] != "active":
        raise ValueError("Cannot modify holdings of an archived client. Reactivate the client first.")
    row = conn.execute("""INSERT INTO current_holdings(client_id,symbol,quantity,avg_price) VALUES (%s,%s,%s,%s)
        ON CONFLICT (client_id,symbol) DO UPDATE SET quantity=EXCLUDED.quantity, avg_price=EXCLUDED.avg_price, updated_at=now()
        RETURNING (xmax = 0)""", (client_id, sym, q, p)).fetchone()
    outcome = "created" if row[0] else "updated"
    audit(conn, actor, f"holding.{outcome}", "holding", client_id, {"symbol": sym, "quantity": q, "avg_price": p})
    return outcome


def holding_exists(conn, client_id, symbol) -> bool:
    return conn.execute("SELECT 1 FROM current_holdings WHERE client_id=%s AND symbol=%s",
                        (client_id, normalize_symbol(symbol))).fetchone() is not None


def remove_holding(conn, actor, client_id, symbol):
    n = conn.execute("DELETE FROM current_holdings WHERE client_id=%s AND symbol=%s", (client_id, symbol)).rowcount
    if n != 1: raise NotFound("Holding not found for this client.")
    audit(conn, actor, "holding.remove", "holding", client_id, {"symbol": symbol})


def list_positions(conn, client_id=None, include_archived=False) -> list[dict]:
    q = """SELECT c.id AS client_id, c.name AS client_name, c.status, h.symbol, h.quantity, h.avg_price,
                  p.price, p.price_date, p.retrieved_at, p.source
           FROM current_holdings h JOIN clients c ON c.id=h.client_id
           LEFT JOIN price_cache p ON p.symbol=h.symbol WHERE 1=1"""
    a = []
    if client_id is not None: q += " AND c.id=%s"; a.append(client_id)
    if not include_archived: q += " AND c.status='active'"
    return _rows(conn.execute(q + " ORDER BY c.name, h.symbol", a))


def active_symbols(conn) -> list[str]:
    return [r[0] for r in conn.execute("""SELECT DISTINCT h.symbol FROM current_holdings h
        JOIN clients c ON c.id=h.client_id WHERE c.status='active' ORDER BY 1""").fetchall()]


def save_prices(conn, quotes, source="yfinance") -> int:
    """Only successful quotes are written; failures never touch existing cache rows."""
    for q in quotes:
        conn.execute("""INSERT INTO price_cache(symbol,price,price_date,retrieved_at,source,status)
            VALUES (%s,%s,%s,now(),%s,'ok') ON CONFLICT (symbol) DO UPDATE SET price=EXCLUDED.price,
            price_date=EXCLUDED.price_date, retrieved_at=now(), source=EXCLUDED.source, status='ok'""",
            (q.symbol, q.price, q.price_date, source))
    return len(quotes)


def import_holdings(conn, actor, rows: list[dict]) -> dict:
    """rows already validated (client_id,symbol,quantity,avg_price). All-or-nothing: the
    caller's transaction (database.connect) rolls everything back on any exception."""
    c = u = 0
    for r in rows:
        out = upsert_holding(conn, actor, r["client_id"], r["symbol"], r["quantity"], r["avg_price"])
        c += out == "created"; u += out == "updated"
    audit(conn, actor, "holding.bulk_import", "holding", None, {"created": c, "updated": u})
    return {"created": c, "updated": u}


def recent_audit(conn, limit=100) -> list[dict]:
    return _rows(conn.execute("SELECT occurred_at, actor, action, entity, entity_id, details FROM sw_audit_log "
                              "ORDER BY occurred_at DESC LIMIT %s", (limit,)))
