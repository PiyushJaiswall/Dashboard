"""CSV / Excel generation and bulk-import validation (pure functions)."""
from __future__ import annotations
import io
from datetime import datetime
import pandas as pd
from market_data import display_symbol, freshness, normalize_symbol
from portfolio import position_metrics, summarize, group_summary, validate_price, validate_quantity

IMPORT_COLUMNS = ["client_name", "symbol", "quantity", "avg_purchase_price"]


def _f(v):
    return None if v is None else float(v)


def holdings_frame(positions: list[dict]) -> pd.DataFrame:
    rows = []
    for p in positions:
        m = position_metrics(p["quantity"], p["avg_price"], p.get("price"))
        rows.append({"Client": p["client_name"], "Symbol": display_symbol(p["symbol"]), "Quantity": _f(p["quantity"]),
                     "Avg Purchase Price (INR)": _f(p["avg_price"]), "Invested Capital (INR)": _f(m["invested"]),
                     "Latest Price (INR)": _f(p.get("price")), "Market Value (INR)": _f(m["market_value"]),
                     "Unrealized P&L (INR)": _f(m["pnl"]), "Return %": None if m["return_pct"] is None else round(float(m["return_pct"]), 2),
                     "Price Date": p.get("price_date"), "Price Status": freshness(p.get("price_date"))})
    return pd.DataFrame(rows)


def client_summary_frame(positions: list[dict]) -> pd.DataFrame:
    names = {p["client_id"]: p["client_name"] for p in positions}
    rows = []
    for cid, s in group_summary(positions, "client_id").items():
        rows.append({"Client": names[cid], "Invested Capital - All (INR)": _f(s["invested_total"]),
                     "Invested Capital - Priced (INR)": _f(s["invested_priced"]), "Market Value - Priced (INR)": _f(s["market_value"]),
                     "Unrealized P&L - Priced (INR)": _f(s["pnl"]),
                     "Return % (priced)": None if s["return_pct"] is None else round(float(s["return_pct"]), 2),
                     "Holdings": s["positions"], "Missing Prices": s["missing_positions"]})
    return pd.DataFrame(rows)


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    stamp = f"# StackWealth Dashboard export generated {datetime.now():%Y-%m-%d %H:%M:%S}\n"
    return (stamp + df.to_csv(index=False)).encode("utf-8-sig")


def to_excel_bytes(sheets: dict) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        meta = pd.DataFrame({"Report": ["StackWealth Dashboard"], "Generated": [f"{datetime.now():%Y-%m-%d %H:%M:%S}"],
                             "Note": ["Unrealized P&L only. Prices from Yahoo Finance may be delayed/end-of-day."]})
        meta.to_excel(xw, sheet_name="About", index=False)
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name[:31], index=False)
            ws = xw.sheets[name[:31]]
            for i, col in enumerate(df.columns, 1):
                ws.column_dimensions[ws.cell(1, i).column_letter].width = max(14, len(str(col)) + 2)
                if "INR" in col:
                    for c in ws.iter_cols(min_col=i, max_col=i, min_row=2):
                        for cell in c: cell.number_format = "#,##0.00"
    return buf.getvalue()


def import_template_bytes() -> bytes:
    return pd.DataFrame(columns=IMPORT_COLUMNS).to_csv(index=False).encode()


def validate_import(df: pd.DataFrame, clients: list[dict]) -> tuple[list[dict], list[dict]]:
    """Returns (valid_rows, errors). Matches client by exact (case-insensitive) name among given
    clients; ambiguous or unknown names are rejected. Duplicate client+symbol rows are rejected.
    Default behaviour: existing client-symbol positions are UPDATED (replaced); new ones created."""
    errors, valid, seen = [], [], {}
    missing = [c for c in IMPORT_COLUMNS if c not in df.columns]
    if missing:
        return [], [{"row": 0, "error": "Missing columns: " + ", ".join(missing)}]
    by_name: dict = {}
    for c in clients: by_name.setdefault(c["name"].strip().lower(), []).append(c)
    for i, r in df.iterrows():
        n = i + 2
        try:
            nm = str(r["client_name"]).strip().lower()
            m = by_name.get(nm, [])
            if not m: raise ValueError("Unknown client (clients are never created by import).")
            if len(m) > 1: raise ValueError("Client name is ambiguous.")
            sym = normalize_symbol(str(r["symbol"]))
            q, p = validate_quantity(r["quantity"]), validate_price(r["avg_purchase_price"])
            key = (m[0]["id"], sym)
            if key in seen: raise ValueError(f"Duplicate of row {seen[key]} (same client and symbol).")
            seen[key] = n
            valid.append({"client_id": m[0]["id"], "client_name": m[0]["name"], "symbol": sym, "quantity": q, "avg_price": p})
        except ValueError as e:
            errors.append({"row": n, "error": str(e)})
    return valid, errors
