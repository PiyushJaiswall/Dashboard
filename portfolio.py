"""Centralised, UI-independent portfolio calculations (Decimal based)."""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

ZERO = Decimal("0")
PRODUCT = "StackWealth Dashboard"


def to_decimal(v: Any) -> Optional[Decimal]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def validate_quantity(v: Any) -> Decimal:
    d = to_decimal(v)
    if d is None or d <= 0:
        raise ValueError("Quantity must be a number greater than zero.")
    return d


def validate_price(v: Any) -> Decimal:
    d = to_decimal(v)
    if d is None or d < 0:
        raise ValueError("Average purchase price must be a number of zero or more.")
    return d


def safe_pct(numerator: Decimal, denominator: Decimal) -> Optional[Decimal]:
    return None if not denominator else numerator / denominator * 100


def position_metrics(quantity: Any, avg_price: Any, price: Any = None) -> dict:
    q, a, p = validate_quantity(quantity), validate_price(avg_price), to_decimal(price)
    invested = q * a
    out = {"invested": invested, "priced": False, "market_value": None, "pnl": None, "return_pct": None}
    if p is not None and p > 0:
        mv = q * p
        out.update(priced=True, market_value=mv, pnl=mv - invested, return_pct=safe_pct(mv - invested, invested))
    return out


def summarize(positions: Iterable[dict]) -> dict:
    """positions: dicts with quantity, avg_price, price (optional). Missing prices are
    excluded from market value / P&L and reported via coverage fields."""
    inv_all = inv_priced = mv = ZERO
    n = n_priced = 0
    for p in positions:
        m = position_metrics(p["quantity"], p["avg_price"], p.get("price"))
        n += 1
        inv_all += m["invested"]
        if m["priced"]:
            n_priced += 1
            inv_priced += m["invested"]
            mv += m["market_value"]
    pnl = mv - inv_priced
    return {
        "invested_total": inv_all, "invested_priced": inv_priced, "market_value": mv,
        "pnl": pnl, "return_pct": safe_pct(pnl, inv_priced),
        "positions": n, "priced_positions": n_priced, "missing_positions": n - n_priced,
        "coverage_pct": (Decimal(n_priced) / Decimal(n) * 100) if n else None,
        "capital_coverage_pct": safe_pct(inv_priced, inv_all),
    }


def group_summary(positions: Iterable[dict], key: str) -> dict:
    groups: dict = {}
    for p in positions:
        groups.setdefault(p[key], []).append(p)
    return {k: summarize(v) for k, v in groups.items()}


def concentration(positions: Iterable[dict], top_n: int = 5) -> dict:
    """Stock-level concentration of market value (priced positions only); same stock held
    by several clients is combined by symbol."""
    by_sym: dict = {}
    for p in positions:
        m = position_metrics(p["quantity"], p["avg_price"], p.get("price"))
        if m["priced"]:
            by_sym[p["symbol"]] = by_sym.get(p["symbol"], ZERO) + m["market_value"]
    total = sum(by_sym.values(), ZERO)
    if not total:
        return {"top_share_pct": None, "hhi": None, "unique_symbols": len(by_sym)}
    shares = sorted((v / total for v in by_sym.values()), reverse=True)
    return {"top_share_pct": sum(shares[:top_n]) * 100, "hhi": sum(s * s for s in shares) * 10000,
            "unique_symbols": len(by_sym)}


def fmt_inr(v: Any, decimals: int = 2, sign: bool = False) -> str:
    d = to_decimal(v)
    if d is None:
        return "—"
    neg = d < 0
    s = f"{abs(d):.{decimals}f}"
    ip, _, fp = s.partition(".")
    if len(ip) > 3:
        head, tail = ip[:-3], ip[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head: parts.insert(0, head)
        ip = ",".join(parts + [tail])
    out = "₹" + ip + (("." + fp) if fp else "")
    return ("-" if neg else ("+" if sign and d > 0 else "")) + out


def fmt_pct(v: Any, sign: bool = True) -> str:
    d = to_decimal(v)
    return "—" if d is None else f"{d:+.2f}%" if sign else f"{d:.2f}%"
