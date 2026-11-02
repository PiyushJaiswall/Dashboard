"""
StackWealth Portfolio
Multi-client equity portfolio management dashboard.

Run:
    streamlit run app.py

Required pages:
    pages/Client_Details.py

Required Streamlit secrets:
    DATABASE_URL
    auth.username
    auth.password_hash

Database tables:
    clients
    current_holdings
    price_cache
    audit_logs
"""

from __future__ import annotations

import hashlib
import hmac
import html
import io
import json
import logging
import math
import re
import time

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


# ============================================================
# APPLICATION CONFIGURATION
# ============================================================

log = logging.getLogger("stackwealth")

APP_NAME = "StackWealth Portfolio"
MANAGER_NAME = "Wealth Manager Sahil Singh"

GREEN = "#10b981"
RED = "#ef4444"
AMBER = "#f59e0b"

NAVY = "#070d19"
PANEL = "#0e1a30"
MUTED = "#9fb0cc"
TEXT = "#e6edf7"

PALETTE = [
    "#22c55e",
    "#3b82f6",
    "#f59e0b",
    "#a855f7",
    "#06b6d4",
    "#ef4444",
    "#eab308",
    "#ec4899",
    "#14b8a6",
    "#64748b",
]

BENCHMARKS = {
    "NIFTY 50": "^NSEI",
    "SENSEX": "^BSESN",
    "NIFTY Midcap 100": "^CNXMIDCAP",
    "NIFTY Bank": "^NSEBANK",
    "NIFTY IT": "^CNXIT",
}

DEFAULT_BENCH = ["NIFTY 50", "SENSEX"]

PERIODS = {
    "1M": 30,
    "3M": 91,
    "6M": 182,
    "1Y": 365,
}

SESSION_TTL = 8 * 3600
STALE_DAYS = 4

CLIENT_PATH = "pages/Client_Details.py"
ROOT = Path(__file__).resolve().parent


# ============================================================
# FORMATTING AND VALIDATION HELPERS
# ============================================================

def number(value):
    """Convert a value to a finite Decimal, or return None."""
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except Exception:
        return None


def fmt_inr(value, dec=0, sign=False):
    """Format a number using Indian currency grouping."""
    amount = number(value)

    if amount is None:
        return "—"

    ip, _, fp = f"{abs(amount):.{dec}f}".partition(".")

    if len(ip) > 3:
        head = ip[:-3]
        tail = ip[-3:]
        parts = []

        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]

        if head:
            parts.insert(0, head)

        ip = ",".join(parts + [tail])

    prefix = ""

    if amount < 0:
        prefix = "-"
    elif sign and amount > 0:
        prefix = "+"

    return prefix + "₹" + ip + (("." + fp) if fp else "")


def fmt_cr(value):
    """Format currency in rupees, lakhs, or crores."""
    amount = number(value)

    if amount is None:
        return "—"

    absolute = abs(float(amount))

    if absolute >= 1e7:
        result = f"₹{absolute / 1e7:,.2f} Cr"
    elif absolute >= 1e5:
        result = f"₹{absolute / 1e5:,.2f} L"
    else:
        result = f"₹{absolute:,.0f}"

    return ("-" if amount < 0 else "") + result


def pct(value, sign=True):
    """Format percentage values."""
    amount = number(value)

    if amount is None:
        return "—"

    return (
        f"{amount:+.2f}%"
        if sign
        else f"{amount:.2f}%"
    )


def tone(value):
    """Return the positive, negative, or neutral display color."""
    amount = number(value)

    if amount is None or amount == 0:
        return MUTED

    return GREEN if amount > 0 else RED


def tone_css(value):
    """Return inline CSS for numeric styling."""
    amount = number(value)

    if amount is None or amount == 0:
        return ""

    return f"color:{tone(amount)};font-weight:600"


def normalize_symbol(raw):
    """
    Normalize Indian stock symbols for Yahoo Finance.

    Examples:
        RELIANCE       -> RELIANCE.NS
        NSE:RELIANCE   -> RELIANCE.NS
        BSE:500325     -> 500325.BO
    """
    symbol = str(raw or "").strip().upper()
    suffix = ".NS"

    if ":" in symbol:
        exchange, symbol = symbol.split(":", 1)

        if exchange not in ("NSE", "BSE"):
            raise ValueError("Use NSE: or BSE: prefixes.")

        suffix = ".NS" if exchange == "NSE" else ".BO"

    for extension in (".NS", ".BO"):
        if symbol.endswith(extension):
            symbol = symbol[:-3]
            suffix = extension
            break

    if not re.fullmatch(r"[A-Z0-9][A-Z0-9&\-]{0,24}", symbol):
        raise ValueError("Invalid stock symbol.")

    return symbol + suffix


def short(symbol):
    """Remove the exchange suffix from a stock symbol."""
    return str(symbol).rsplit(".", 1)[0]


def cap_bucket(market_cap_inr=None):
    """
    Do not guess AMFI market-cap classifications from a raw
    market-cap value. Use verified classifications only.
    """
    return "Unclassified"


# ============================================================
# PORTFOLIO DATA PROCESSING
# ============================================================

def enrich(rows):
    """Transform database holding rows into a normalized DataFrame."""

    columns = [
        "client_id",
        "Client",
        "Symbol",
        "sym",
        "Qty",
        "Avg",
        "Price",
        "Invested",
        "Value",
        "PnL",
        "Ret",
        "price_date",
        "Sector",
        "Cap",
        "priced",
        "source",
        "retrieved_at",
    ]

    output = []

    for row in rows:
        quantity = number(row["quantity"])
        average_price = number(row["avg_price"])

        if (
            quantity is None
            or quantity <= 0
            or average_price is None
            or average_price < 0
        ):
            raise ValueError(
                "Invalid holding in database: quantity must be "
                "positive and average price nonnegative."
            )

        invested = quantity * average_price

        price = number(row.get("price"))
        price = (
            price
            if price is not None and price > 0
            else None
        )

        market_value = (
            quantity * price
            if price is not None
            else None
        )

        pnl = (
            market_value - invested
            if market_value is not None
            else None
        )

        diagnostics = row.get("diagnostics") or {}

        if isinstance(diagnostics, str):
            try:
                diagnostics = json.loads(diagnostics)
            except ValueError:
                diagnostics = {}

        if not isinstance(diagnostics, dict):
            diagnostics = {}

        cap = diagnostics.get(
            "cap_category",
            "Unclassified",
        )

        if cap not in (
            "Large Cap",
            "Mid Cap",
            "Small Cap",
        ):
            cap = "Unclassified"

        output.append(
            {
                "client_id": row["client_id"],
                "Client": row["client_name"],
                "Symbol": short(row["symbol"]),
                "sym": row["symbol"],
                "Qty": float(quantity),
                "Avg": float(average_price),
                "Price": (
                    float(price)
                    if price is not None
                    else None
                ),
                "Invested": float(invested),
                "Value": (
                    float(market_value)
                    if market_value is not None
                    else None
                ),
                "PnL": (
                    float(pnl)
                    if pnl is not None
                    else None
                ),
                "Ret": (
                    float(pnl / invested * 100)
                    if pnl is not None and invested > 0
                    else None
                ),
                "price_date": row.get("price_date"),
                "Sector": (
                    diagnostics.get("sector")
                    or "Unclassified"
                ),
                "Cap": cap,
                "priced": market_value is not None,
                "source": row.get("source"),
                "retrieved_at": row.get("retrieved_at"),
            }
        )

    df = pd.DataFrame(output, columns=columns)

    df["priced"] = df["priced"].astype(bool)

    for column in (
        "Qty",
        "Avg",
        "Price",
        "Invested",
        "Value",
        "PnL",
        "Ret",
    ):
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    return df


def totals(df):
    """Calculate consolidated portfolio totals."""

    priced = df[df["priced"].astype(bool)]

    invested = float(df["Invested"].sum())
    invested_priced = float(priced["Invested"].sum())
    market_value = float(priced["Value"].sum())

    pnl = market_value - invested_priced

    return {
        "invested": invested,
        "invested_priced": invested_priced,
        "value": market_value,
        "pnl": pnl,
        "ret": (
            pnl / invested_priced * 100
            if invested_priced > 0
            else None
        ),
        "n": len(df),
        "n_priced": len(priced),
        "clients": int(df["client_id"].nunique()),
    }


def client_table(df):
    """Calculate portfolio metrics for clients with holdings."""

    rows = []

    for (client_id, client_name), group in df.groupby(
        ["client_id", "Client"]
    ):
        result = totals(group)

        rows.append(
            {
                "client_id": client_id,
                "Client": client_name,
                "Invested": result["invested"],
                "Market value": (
                    result["value"]
                    if result["n_priced"]
                    else None
                ),
                "P&L (priced)": (
                    result["pnl"]
                    if result["n_priced"]
                    else None
                ),
                "Return %": result["ret"],
                "Holdings": result["n"],
                "Priced": (
                    f"{result['n_priced']}/{result['n']}"
                ),
            }
        )

    return pd.DataFrame(
        rows,
        columns=[
            "client_id",
            "Client",
            "Invested",
            "Market value",
            "P&L (priced)",
            "Return %",
            "Holdings",
            "Priced",
        ],
    )


def concentration(df, basis):
    """Calculate top-five concentration and HHI."""

    grouped = df.groupby("sym")[basis].sum()
    grouped = grouped[grouped > 0]

    if grouped.empty:
        return None, None

    weights = grouped / grouped.sum()

    top_five = float(weights.nlargest(5).sum() * 100)
    hhi = float((weights ** 2).sum() * 10000)

    return top_five, hhi


def value_series(df, closes):
    """Estimate historical value using current quantities."""

    if (
        closes is None
        or closes.empty
        or df.empty
    ):
        return pd.Series(dtype=float)

    quantities = df.groupby("sym")["Qty"].sum()

    quantities = quantities[
        quantities.index.isin(closes.columns)
    ]

    if quantities.empty:
        return pd.Series(dtype=float)

    prices = (
        closes[list(quantities.index)]
        .ffill()
        .dropna()
    )

    return prices.mul(
        quantities,
        axis=1,
    ).sum(axis=1)


def period_return(series, days):
    """Calculate historical percentage return over a period."""

    if series is None:
        return None

    series = series.dropna().sort_index()

    if len(series) < 2:
        return None

    target = (
        series.index[-1]
        - pd.Timedelta(days=days)
    )

    if target < series.index[0]:
        if (series.index[0] - target).days > 7:
            return None

        starting_value = series.iloc[0]
    else:
        starting_value = series.asof(target)

    if (
        pd.notna(starting_value)
        and starting_value > 0
    ):
        return float(
            (series.iloc[-1] / starting_value - 1) * 100
        )

    return None


def freshness(df):
    """Summarize cached-price freshness."""

    dates = [
        pd.Timestamp(value).date()
        for value in df["price_date"]
        if pd.notna(value)
    ]

    return {
        "latest": max(dates) if dates else None,
        "stale": sum(
            (date.today() - value).days > STALE_DAYS
            for value in dates
        ),
        "missing": int(
            (~df["priced"].astype(bool)).sum()
        ),
    }


# ============================================================
# DATABASE
# ============================================================

def _dsn():
    """Read the database URL from Streamlit secrets."""

    try:
        url = st.secrets["DATABASE_URL"]
    except Exception:
        raise RuntimeError(
            "DATABASE_URL is missing from Streamlit secrets."
        ) from None

    return str(url)


@contextmanager
def db():
    """
    Open a short-lived PostgreSQL connection.

    Transactions commit on success and roll back on errors.
    """

    import psycopg

    try:
        connection = psycopg.connect(
            _dsn(),
            connect_timeout=10,
            prepare_threshold=None,
        )
    except Exception as exc:
        log.error(
            "Database connection failed: %s",
            type(exc).__name__,
        )

        raise RuntimeError(
            "Cannot connect to Supabase. Check DATABASE_URL, "
            "pooler settings and project availability."
        ) from None

    try:
        yield connection
        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


POS_SQL = """
SELECT
    c.id AS client_id,
    c.name AS client_name,
    h.symbol,
    h.quantity,
    h.avg_price,
    p.price,
    p.price_date,
    p.retrieved_at,
    p.diagnostics,
    p.source
FROM current_holdings h
JOIN clients c
    ON c.id = h.client_id
LEFT JOIN price_cache p
    ON p.symbol = h.symbol
WHERE c.status = 'active'
ORDER BY c.name, h.symbol
"""


def load_positions():
    """Load all holdings for active clients."""

    with db() as connection:
        cursor = connection.execute(POS_SQL)
        columns = [
            description.name
            for description in cursor.description
        ]

        rows = [
            dict(zip(columns, row))
            for row in cursor.fetchall()
        ]

    return enrich(rows)


def load_active_clients():
    """
    Load the active-client directory independently of holdings.

    This allows clients with no holdings to appear in search.
    """

    with db() as connection:
        rows = connection.execute(
            """
            SELECT id, name
            FROM clients
            WHERE status = 'active'
            ORDER BY name
            """
        ).fetchall()

    return pd.DataFrame(
        rows,
        columns=["client_id", "Client"],
    )


# ============================================================
# AUTHENTICATION
# ============================================================

def _secret(*names):
    """Read a secret from the supported configuration locations."""

    try:
        secrets = dict(st.secrets)
    except Exception:
        secrets = {}

    auth = secrets.get("auth", {})

    for source in (auth, secrets):
        if not isinstance(source, dict):
            continue

        for name in names:
            if source.get(name):
                return str(source[name])

    return None


def login_ok(user, password):
    """Validate administrator credentials against a bcrypt hash."""

    username = _secret(
        "username",
        "ADMIN_USERNAME",
    )

    password_hash = _secret(
        "password_hash",
        "ADMIN_PASSWORD_HASH",
    )

    if not username or not password_hash:
        log.error(
            "Authentication configuration is incomplete."
        )

        return (
            False,
            "config:Administrator username or password_hash is missing.",
        )

    password_hash = password_hash.strip()

    if not re.fullmatch(
        r"\$2[aby]\$(0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53}",
        password_hash,
    ):
        log.error(
            "Authentication password hash is malformed."
        )

        return (
            False,
            "config:password_hash must be a valid 60-character bcrypt hash.",
        )

    import bcrypt

    raw_password = (password or "").encode()

    if len(raw_password) > 72:
        return False, "invalid"

    try:
        password_valid = bcrypt.checkpw(
            raw_password,
            password_hash.encode(),
        )
    except ValueError:
        log.error(
            "Configured bcrypt hash was rejected."
        )

        return (
            False,
            "config:Configured bcrypt hash was rejected. Generate a new hash.",
        )

    username_valid = hmac.compare_digest(
        (user or "").encode(),
        username.encode(),
    )

    if username_valid and password_valid:
        return True, None

    log.warning("Authentication failed.")

    return False, "invalid"


def authed():
    """Check whether the session is authenticated and unexpired."""

    session = st.session_state

    return (
        bool(session.get("sw_auth"))
        and 0
        <= time.time() - session.get("sw_login_at", 0)
        < SESSION_TTL
    )


# ============================================================
# MARKET DATA
# ============================================================

def download_closes(tickers, period="1y"):
    """Download daily closing prices for stocks and indices."""

    import yfinance as yf

    names = sorted(set(tickers))

    if not names:
        return pd.DataFrame()

    raw = yf.download(
        names,
        period=period,
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=4,
    )

    if raw is None or raw.empty:
        return pd.DataFrame()

    if "Close" not in raw.columns.get_level_values(0):
        return pd.DataFrame()

    close = raw["Close"]

    if isinstance(close, pd.Series):
        close = close.to_frame(names[0])

    close.index = pd.to_datetime(
        close.index
    ).tz_localize(None)

    return (
        close.apply(
            pd.to_numeric,
            errors="coerce",
        )
        .where(lambda values: values > 0)
        .dropna(axis=1, how="all")
        .sort_index()
    )


def fetch_info(symbol):
    """Fetch basic stock metadata."""

    try:
        import yfinance as yf

        info = yf.Ticker(symbol).info or {}

        return {
            key: value
            for key, value in {
                "sector": info.get("sector"),
                "market_cap": info.get("marketCap"),
            }.items()
            if value
        }

    except Exception as exc:
        log.warning(
            "Metadata failed for %s: %s",
            symbol,
            type(exc).__name__,
        )

        return {}


def refresh_market_data():
    """
    Refresh prices for stocks held by active clients.

    Invalid prices are ignored. Existing cached prices remain intact
    when downloads fail. Prices and the audit entry are committed
    in one database transaction.
    """

    if not authed():
        raise PermissionError(
            "Authentication required."
        )

    df = load_positions()

    symbols = sorted(
        set(df["sym"].dropna().astype(str))
    )

    if not symbols:
        return {
            "ok": 0,
            "missing": [],
        }

    closes = download_closes(
        symbols + list(BENCHMARKS.values())
    )

    quotes = {}
    missing = []

    for symbol in symbols:
        if (
            closes is None
            or closes.empty
            or symbol not in closes.columns
        ):
            missing.append(symbol)
            continue

        prices = pd.to_numeric(
            closes[symbol],
            errors="coerce",
        ).dropna()

        prices = prices[
            prices.map(
                lambda value: (
                    math.isfinite(float(value))
                    and float(value) > 0
                )
            )
        ].sort_index()

        if prices.empty:
            missing.append(symbol)
            continue

        price_date = pd.Timestamp(
            prices.index[-1]
        )

        if pd.isna(price_date):
            missing.append(symbol)
            continue

        quotes[symbol] = {
            "price": Decimal(
                str(prices.iloc[-1])
            ),
            "price_date": price_date.date(),
        }

    valid_symbols = sorted(quotes)

    def safe_fetch_info(symbol):
        """Fetch metadata without blocking price updates."""

        try:
            result = fetch_info(symbol)

            if not isinstance(result, dict):
                return {}

            metadata = {}

            sector = result.get("sector")

            if (
                isinstance(sector, str)
                and sector.strip()
            ):
                metadata["sector"] = sector.strip()

            market_cap = result.get("market_cap")

            if market_cap is not None:
                market_cap = float(market_cap)

                if (
                    math.isfinite(market_cap)
                    and market_cap > 0
                ):
                    metadata["market_cap"] = market_cap

            return metadata

        except Exception as exc:
            log.warning(
                "Metadata refresh failed for %s: %s",
                symbol,
                type(exc).__name__,
            )

            return {}

    if valid_symbols:
        with ThreadPoolExecutor(
            max_workers=min(4, len(valid_symbols))
        ) as executor:
            infos = dict(
                zip(
                    valid_symbols,
                    executor.map(
                        safe_fetch_info,
                        valid_symbols,
                    ),
                )
            )
    else:
        infos = {}

    with db() as connection:
        for symbol in valid_symbols:
            quote = quotes[symbol]

            connection.execute(
                """
                INSERT INTO price_cache (
                    symbol,
                    price,
                    price_date,
                    retrieved_at,
                    source,
                    status,
                    diagnostics
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    now(),
                    'yfinance',
                    'ok',
                    %s::jsonb
                )
                ON CONFLICT (symbol)
                DO UPDATE SET
                    price = EXCLUDED.price,
                    price_date = EXCLUDED.price_date,
                    retrieved_at = now(),
                    source = 'yfinance',
                    status = 'ok',
                    diagnostics = (
                        COALESCE(
                            price_cache.diagnostics,
                            '{}'::jsonb
                        )
                        || EXCLUDED.diagnostics
                    )
                """,
                (
                    symbol,
                    quote["price"],
                    quote["price_date"],
                    json.dumps(
                        infos.get(symbol, {}),
                        allow_nan=False,
                    ),
                ),
            )

        connection.execute(
            """
            INSERT INTO audit_logs (
                action,
                entity_type,
                entity_id,
                details
            )
            VALUES (
                %s,
                %s,
                %s,
                %s::jsonb
            )
            """,
            (
                "prices.refresh",
                "price_cache",
                None,
                json.dumps(
                    {
                        "actor": st.session_state.get(
                            "sw_user",
                            "admin",
                        ),
                        "requested": len(symbols),
                        "refreshed": len(valid_symbols),
                        "missing": missing,
                    },
                    allow_nan=False,
                ),
            ),
        )

    if closes is not None and not closes.empty:
        st.session_state["closes"] = closes

    return {
        "ok": len(valid_symbols),
        "missing": missing,
    }


def refresh_ui():
    """Refresh market data and display the outcome."""

    with st.spinner(
        "Refreshing market prices and historical data…"
    ):
        try:
            result = refresh_market_data()

            message = (
                f"{result['ok']} symbols updated."
            )

            if result["missing"]:
                message += (
                    " No data for: "
                    + ", ".join(result["missing"])
                    + ". Previously cached prices were retained."
                )

            st.session_state["refresh_msg"] = message

        except Exception as exc:
            log.error(
                "Market refresh failed: %s",
                type(exc).__name__,
            )

            st.session_state["refresh_msg"] = (
                "Refresh failed. Previously cached prices were retained. "
                "Check provider availability and database logs."
            )

    st.rerun()


# ============================================================
# VISUAL DESIGN SYSTEM
# ============================================================

CSS = f"""
<style>
:root {{
    color-scheme: dark;
}}

.stApp {{
    background:
        radial-gradient(
            ellipse at 10% 0%,
            #122746 0%,
            transparent 40%
        ),
        linear-gradient(
            145deg,
            #070d19 0%,
            #0a1221 55%,
            #0b1526 100%
        );
    color: {TEXT};
}}

[data-testid="stHeader"] {{
    background: rgba(7, 13, 25, 0.75);
    backdrop-filter: blur(14px);
}}

[data-testid="stSidebar"] {{
    background: #080f1d;
    border-right: 1px solid #1b2a40;
}}

[data-testid="stSidebar"] > div:first-child {{
    padding-top: 1.4rem;
}}

.block-container {{
    max-width: 1740px;
    padding-top: 2rem;
    padding-bottom: 3rem;
}}

.brand {{
    color: #f8fafc;
    font-size: 1.85rem;
    font-weight: 780;
    letter-spacing: -0.8px;
    line-height: 1.2;
}}

.brand span {{
    color: {GREEN};
}}

.brand em {{
    color: {MUTED};
    font-style: normal;
    font-weight: 450;
}}

.page-heading {{
    padding: 6px 0 20px 0;
}}

.page-heading h1 {{
    color: #f8fafc;
    font-size: clamp(2rem, 3vw, 2.8rem);
    font-weight: 780;
    letter-spacing: -1.3px;
    line-height: 1.15;
    margin: 5px 0 8px 0;
}}

.eyebrow {{
    color: {GREEN};
    font-size: 0.69rem;
    font-weight: 750;
    letter-spacing: 2px;
    margin: 0;
    text-transform: uppercase;
}}

.page-subtitle {{
    color: #a9bad1;
    font-size: 0.98rem;
    letter-spacing: 0.1px;
}}

.tag {{
    color: {MUTED};
    font-size: 0.9rem;
    margin: 10px 0 24px;
}}

.sidebar-brand {{
    align-items: center;
    display: flex;
    gap: 11px;
    margin: 0 0 26px 0;
}}

.brand-mark {{
    align-items: center;
    background: linear-gradient(145deg, #10b981, #047857);
    border: 1px solid #34d399;
    border-radius: 12px;
    color: #ffffff;
    display: flex;
    font-size: 0.85rem;
    font-weight: 850;
    height: 42px;
    justify-content: center;
    width: 42px;
}}

.sidebar-name {{
    color: #f8fafc;
    font-size: 1.05rem;
    font-weight: 760;
    line-height: 1.3;
}}

.sidebar-sub {{
    color: #8fa2bd;
    font-size: 0.59rem;
    font-weight: 700;
    letter-spacing: 1.15px;
    margin-top: 3px;
}}

[data-testid="stSidebar"] [data-testid="stPageLink"] a {{
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 0.65rem 0.75rem;
    transition: background 0.15s ease, border-color 0.15s ease;
}}

[data-testid="stSidebar"] [data-testid="stPageLink"] a:hover {{
    background: #111f33;
    border-color: #263c55;
}}

[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {{
    color: #a9bad1;
}}

.hero {{
    background:
        radial-gradient(
            circle at 90% 10%,
            #145b4a66 0%,
            transparent 43%
        ),
        linear-gradient(
            135deg,
            #102e31 0%,
            #102438 58%,
            #111d32 100%
        );
    border: 1px solid #247b65;
    border-radius: 17px;
    box-shadow: 0 12px 36px #0000001c;
    min-height: 176px;
    padding: 25px;
}}

.hero .app,
.hero .lbl {{
    color: #b9d9d2;
    font-size: 0.81rem;
}}

.hero .big {{
    color: #ffffff;
    font-size: clamp(1.75rem, 2.7vw, 3rem);
    font-weight: 820;
    letter-spacing: -1.2px;
    margin: 13px 0 10px;
    overflow-wrap: anywhere;
}}

.hero .sub {{
    color: #bdd1d0;
    font-size: 0.8rem;
    line-height: 1.5;
}}

.card {{
    background: linear-gradient(
        150deg,
        #111f34 0%,
        #0d1728 100%
    );
    border: 1px solid #253650;
    border-radius: 13px;
    box-shadow: 0 8px 24px #00000012;
    min-height: 104px;
    padding: 15px 14px;
    transition: border-color 0.15s ease;
}}

.card:hover {{
    border-color: #3a526d;
}}

.card .k {{
    color: #9aacc5;
    font-size: 0.73rem;
    font-weight: 550;
    line-height: 1.4;
}}

.card .v {{
    font-size: clamp(0.95rem, 1.3vw, 1.35rem);
    font-weight: 760;
    letter-spacing: -0.35px;
    margin: 8px 0 5px;
    overflow-wrap: anywhere;
}}

.card .s {{
    color: #8497b1;
    font-size: 0.7rem;
    line-height: 1.45;
    overflow-wrap: anywhere;
}}

.ctitle {{
    color: #eaf1fa;
    font-size: 0.98rem;
    font-weight: 720;
    letter-spacing: -0.15px;
    margin: 2px 0 12px;
}}

.sec {{
    border-left: 3px solid {GREEN};
    color: #eaf1fa;
    font-size: 1.08rem;
    font-weight: 720;
    margin: 24px 0 12px;
    padding: 3px 0 3px 12px;
}}

.chip {{
    background: #111f32;
    border: 1px solid #2b405a;
    border-radius: 20px;
    color: #b5c4d8;
    display: inline-block;
    font-size: 0.71rem;
    margin: 4px 6px 8px 0;
    padding: 5px 10px;
}}

.warn {{
    background: #302710;
    border-left: 3px solid {AMBER};
    border-radius: 7px;
    color: #fde68a;
    margin: 10px 0;
    padding: 10px 13px;
}}

div[data-testid="stVerticalBlockBorderWrapper"] {{
    background: linear-gradient(
        150deg,
        #0f1b2e,
        #0b1525
    );
    border: 1px solid #22334b;
    border-radius: 15px;
    box-shadow: 0 8px 28px #00000014;
}}

.stButton > button,
.stDownloadButton > button {{
    border-radius: 9px;
    font-weight: 650;
    min-height: 2.55rem;
    transition: all 0.15s ease;
}}

.stButton > button[kind="primary"] {{
    background: linear-gradient(
        135deg,
        #10b981,
        #059669
    );
    border: 1px solid #34d399;
    color: #ffffff;
}}

.stButton > button[kind="primary"]:hover {{
    background: linear-gradient(
        135deg,
        #34d399,
        #10b981
    );
    border-color: #6ee7b7;
}}

.stTextInput input,
.stNumberInput input,
.stSelectbox [data-baseweb="select"] > div,
.stTextArea textarea {{
    background: #0c1728;
    border-color: #2b3e57;
    border-radius: 9px;
}}

.stTextInput input:focus,
.stNumberInput input:focus,
.stTextArea textarea:focus {{
    border-color: {GREEN};
    box-shadow: 0 0 0 1px {GREEN}33;
}}

[data-testid="stDataFrame"] {{
    border: 1px solid #24364f;
    border-radius: 10px;
    overflow: hidden;
}}

[data-testid="stMetric"] {{
    background: #101d30;
    border: 1px solid #263951;
    border-radius: 12px;
    padding: 13px;
}}

[data-testid="stAlert"] {{
    border-radius: 10px;
}}

hr {{
    border-color: #24344b;
}}

@media (max-width: 900px) {{
    .block-container {{
        padding-left: 1rem;
        padding-right: 1rem;
    }}

    .hero {{
        padding: 19px;
    }}
}}
</style>
"""


# ============================================================
# REUSABLE UI COMPONENTS
# ============================================================

def card(k, v, s="", color=TEXT):
    """Render a reusable KPI card."""

    return (
        "<div class='card'>"
        f"<div class='k'>{html.escape(str(k))}</div>"
        f"<div class='v' style='color:{color}'>"
        f"{html.escape(str(v))}</div>"
        f"<div class='s'>{html.escape(str(s))}</div>"
        "</div>"
    )


def hero(title_small, label, big, color, sub):
    """Render the featured portfolio-value panel."""

    return (
        "<div class='hero'>"
        f"<div class='app'>{html.escape(str(title_small))}</div>"
        f"<div class='lbl'>{html.escape(str(label))}</div>"
        f"<div class='big' style='color:{color}'>"
        f"{html.escape(str(big))}</div>"
        f"<div class='sub'>{html.escape(str(sub))}</div>"
        "</div>"
    )


def ctitle(title):
    """Render a chart or card title."""

    st.markdown(
        f"<div class='ctitle'>{html.escape(str(title))}</div>",
        unsafe_allow_html=True,
    )


def section(title):
    """Render a section heading."""

    st.markdown(
        f"<div class='sec'>{html.escape(str(title))}</div>",
        unsafe_allow_html=True,
    )


def chips(df, closes):
    """Render price freshness and data-source indicators."""

    status = freshness(df)

    labels = [
        (
            f"Price date: {status['latest']}"
            if status["latest"]
            else "No cached prices"
        ),
        f"{status['stale']} stale positions",
        f"{status['missing']} missing prices",
        (
            "History loaded"
            if closes is not None
            else "History not loaded"
        ),
        "Yahoo Finance · delayed/end-of-day",
    ]

    if (
        "source" in df
        and df["source"].fillna("").str.contains(
            "demo",
            case=False,
        ).any()
    ):
        st.warning(
            "DEMO PRICES PRESENT — illustrative values, "
            "not actual market performance."
        )

    markup = "".join(
        f"<span class='chip'>{html.escape(label)}</span>"
        for label in labels
    )

    st.markdown(
        markup,
        unsafe_allow_html=True,
    )


def _layout(fig, height=300, title=None):
    """Apply the dashboard's common Plotly styling."""

    fig.update_layout(
        template="plotly_dark",
        height=height,
        title=title,
        margin=dict(
            t=42 if title else 20,
            b=15,
            l=5,
            r=5,
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=TEXT),
        hovermode="closest",
    )

    return fig


def show(fig, empty="Nothing to chart yet."):
    """Display a chart or its empty-state message."""

    if fig is None:
        st.caption(empty)
    else:
        st.plotly_chart(
            fig,
            use_container_width=True,
        )


def donut_fig(
    df,
    name,
    val,
    title=None,
    h=300,
    legend=True,
):
    """Create an allocation doughnut chart."""

    data = df[
        df[val].fillna(0) > 0
    ].groupby(
        name,
        as_index=False,
    )[val].sum().sort_values(
        val,
        ascending=False,
    )

    if data.empty:
        return None

    fig = px.pie(
        data,
        names=name,
        values=val,
        hole=0.64,
        color_discrete_sequence=PALETTE,
    )

    fig.update_traces(
        textinfo="none",
        sort=False,
        marker=dict(
            line=dict(
                color=NAVY,
                width=2,
            )
        ),
        hovertemplate=(
            "%{label}<br>"
            "₹%{value:,.0f}<br>"
            "%{percent}<extra></extra>"
        ),
    )

    fig.add_annotation(
        x=0.5,
        y=0.5,
        text=fmt_cr(data[val].sum()),
        showarrow=False,
        font=dict(size=13),
    )

    fig.update_layout(
        showlegend=legend,
        legend=dict(
            orientation="h",
            y=-0.05,
            font=dict(size=10),
        ),
    )

    return _layout(fig, h, title)


def treemap_fig(df, h=330, title=None):
    """Create a sector-to-stock allocation treemap."""

    if df.empty:
        return None

    basis = (
        "Value"
        if df["priced"].any()
        else "Invested"
    )

    source = df[
        df[basis].fillna(0) > 0
    ]

    if source.empty:
        return None

    data = source.groupby(
        ["Sector", "Symbol"],
        as_index=False,
    ).agg(
        Amount=(basis, "sum"),
        PnL=("PnL", "sum"),
        Invested=("Invested", "sum"),
    )

    data["Ret"] = (
        data["PnL"]
        / data["Invested"].where(data["Invested"] > 0)
        * 100
    ).fillna(0)

    if basis == "Value":
        fig = px.treemap(
            data,
            path=[
                px.Constant("Portfolio"),
                "Sector",
                "Symbol",
            ],
            values="Amount",
            color="Ret",
            color_continuous_scale=[
                RED,
                "#253450",
                GREEN,
            ],
            range_color=[-25, 25],
            custom_data=["PnL", "Ret"],
        )

        fig.update_traces(
            texttemplate=(
                "%{label}<br>"
                "%{customdata[1]:+.1f}%"
            ),
            hovertemplate=(
                "%{label}<br>"
                "₹%{value:,.0f}<br>"
                "P&L ₹%{customdata[0]:,.0f}"
                "<extra></extra>"
            ),
        )

        fig.update_layout(
            coloraxis_showscale=False
        )

    else:
        fig = px.treemap(
            data,
            path=[
                px.Constant("Invested capital"),
                "Sector",
                "Symbol",
            ],
            values="Amount",
            color_discrete_sequence=PALETTE,
        )

    return _layout(fig, h, title)


def gauge_fig(top5, h=240):
    """Create the top-five concentration gauge."""

    if top5 is None:
        return None

    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=top5,
            number=dict(
                suffix="%",
                font=dict(size=28),
            ),
            gauge=dict(
                axis=dict(range=[0, 100]),
                bar=dict(
                    color=TEXT,
                    thickness=0.2,
                ),
                steps=[
                    dict(
                        range=[0, 40],
                        color="#047857",
                    ),
                    dict(
                        range=[40, 65],
                        color="#b45309",
                    ),
                    dict(
                        range=[65, 100],
                        color="#991b1b",
                    ),
                ],
            ),
        )
    )

    return _layout(fig, h)


def bars_fig(ct, h=300):
    """Compare invested capital with current market value."""

    data = ct.dropna(
        subset=["Market value"]
    )

    if data.empty:
        return None

    invested_priced = (
        data["Market value"]
        - data["P&L (priced)"]
    )

    fig = go.Figure(
        [
            go.Bar(
                name="Invested (priced)",
                x=data["Client"],
                y=invested_priced,
                marker_color="#64748b",
            ),
            go.Bar(
                name="Market value",
                x=data["Client"],
                y=data["Market value"],
                marker_color=GREEN,
            ),
        ]
    )

    fig.update_layout(
        barmode="group",
        legend=dict(orientation="h"),
    )

    return _layout(fig, h)


def pnl_bar_fig(x, y, h=300):
    """Create a client-level or holding-level P&L chart."""

    data = pd.DataFrame(
        {
            "x": list(x),
            "y": list(y),
        }
    ).dropna()

    if data.empty:
        return None

    fig = go.Figure(
        go.Bar(
            x=data["x"],
            y=data["y"],
            marker_color=[
                tone(value)
                for value in data["y"]
            ],
            hovertemplate=(
                "%{x}<br>"
                "₹%{y:,.0f}<extra></extra>"
            ),
        )
    )

    return _layout(fig, h)


def bench_fig(
    series,
    closes,
    picks,
    days,
    label,
    h=330,
):
    """Compare estimated portfolio history with selected indices."""

    lines = {label: series}
    missing = []

    for name in picks:
        symbol = BENCHMARKS[name]

        if (
            closes is not None
            and symbol in closes
            and closes[symbol].notna().any()
        ):
            lines[name] = closes[symbol].dropna()
        else:
            missing.append(name)

    frame = pd.concat(
        lines,
        axis=1,
    ).sort_index().ffill().dropna()

    if frame.empty:
        return None, pd.DataFrame(), missing

    view = frame.loc[
        frame.index
        >= frame.index[-1] - pd.Timedelta(days=days)
    ]

    if view.empty:
        return None, pd.DataFrame(), missing

    rebased = (
        view.div(view.iloc[0])
        .mul(100)
    )

    fig = go.Figure()

    for index, column in enumerate(rebased):
        fig.add_trace(
            go.Scatter(
                x=rebased.index,
                y=rebased[column],
                name=column,
                line=dict(
                    color=(
                        GREEN
                        if index == 0
                        else PALETTE[index % len(PALETTE)]
                    ),
                    width=3 if index == 0 else 1.7,
                ),
            )
        )

    fig.update_layout(
        hovermode="x unified",
        legend=dict(orientation="h"),
        yaxis_title="Rebased to 100",
    )

    fig = _layout(fig, h)

    fig.update_layout(
        hovermode="x unified"
    )

    table = pd.DataFrame(
        [
            {
                "Series": column,
                **{
                    period: period_return(
                        frame[column],
                        period_days,
                    )
                    for period, period_days in PERIODS.items()
                },
            }
            for column in frame
        ]
    ).set_index("Series")

    return fig, table, missing


def bench_card(df, closes, key, label):
    """Render the benchmark comparison panel."""

    ctitle("Performance comparison")

    series = value_series(
        df[df["priced"].astype(bool)],
        closes,
    )

    if series.empty:
        st.caption(
            "Use Refresh market data to load historical prices."
        )
        return

    covered = (
        set(closes.columns)
        if closes is not None
        else set()
    )

    omitted = sorted(
        set(df["sym"]) - covered
    )

    if omitted:
        st.warning(
            "History excludes: "
            + ", ".join(omitted)
        )

    picks = st.multiselect(
        "Benchmarks",
        list(BENCHMARKS),
        DEFAULT_BENCH,
        key=f"bm_{key}",
    )

    period = st.radio(
        "Period",
        list(PERIODS),
        index=3,
        horizontal=True,
        key=f"period_{key}",
    )

    fig, table, missing = bench_fig(
        series,
        closes,
        picks,
        PERIODS[period],
        label,
    )

    show(
        fig,
        "No overlapping historical dates available.",
    )

    if not table.empty:
        st.dataframe(
            table.style.format(
                "{:+.2f}%",
                na_rep="—",
            ).map(tone_css),
            use_container_width=True,
        )

    if missing:
        st.caption(
            "No index data: "
            + ", ".join(missing)
        )

    st.caption(
        "Illustrative comparison: current quantities valued at "
        "historical closes, not actual client returns. "
        "Excludes dividends and cash flows. Indices are price indices."
    )


# ============================================================
# EXCEL REPORT
# ============================================================

def build_report(ct, df):
    """Build an Excel workbook with client and holdings sheets."""

    def safe_cell(value):
        if (
            isinstance(value, str)
            and value.startswith(("=", "+", "-", "@"))
        ):
            return "'" + value

        return value

    buffer = io.BytesIO()

    with pd.ExcelWriter(
        buffer,
        engine="openpyxl",
    ) as writer:

        pd.DataFrame(
            {
                "Report": [APP_NAME],
                "Generated UTC": [
                    datetime.now(timezone.utc).isoformat()
                ],
                "Scope": [
                    "Unrealized P&L only; missing prices excluded from valuation."
                ],
            }
        ).to_excel(
            writer,
            sheet_name="About",
            index=False,
        )

        clients_export = ct.drop(
            columns="client_id",
            errors="ignore",
        )

        holdings_export = df.drop(
            columns=[
                "client_id",
                "sym",
                "priced",
                "retrieved_at",
            ],
            errors="ignore",
        )

        # Protect spreadsheet consumers from formula-like user text.
        clients_export = clients_export.apply(
            lambda column: column.map(safe_cell)
        )

        holdings_export = holdings_export.apply(
            lambda column: column.map(safe_cell)
        )

        clients_export.to_excel(
            writer,
            sheet_name="Clients",
            index=False,
        )

        holdings_export.to_excel(
            writer,
            sheet_name="Holdings",
            index=False,
        )

    return buffer.getvalue()


# ============================================================
# LOGIN SCREEN
# ============================================================

def login_page():
    """Render the administrator sign-in screen."""

    st.markdown(
        "<div style='height:7vh'></div>",
        unsafe_allow_html=True,
    )

    _, center, _ = st.columns([1, 1.25, 1])

    with center:
        st.markdown(
            """
            <div class="page-heading">
                <div class="eyebrow">PRIVATE WEALTH MANAGEMENT</div>
                <h1>StackWealth Portfolio</h1>
                <div class="page-subtitle">
                    Wealth Manager Sahil Singh
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown(
            """
            <div class="tag">
                Secure access to client portfolios, holdings,
                valuations and performance analytics.
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.form("login"):
            username = st.text_input(
                "Username",
                autocomplete="username",
            )

            password = st.text_input(
                "Password",
                type="password",
                autocomplete="current-password",
            )

            submit = st.form_submit_button(
                "Sign in",
                type="primary",
                use_container_width=True,
            )

        if not submit:
            return

        if time.time() < st.session_state.get(
            "login_after",
            0,
        ):
            st.warning(
                "Please wait a few seconds before trying again."
            )
            return

        success, error = login_ok(
            username,
            password,
        )

        if success:
            st.session_state.clear()

            st.session_state.update(
                sw_auth=True,
                sw_user=username,
                sw_login_at=time.time(),
            )

            st.rerun()

        if (error or "").startswith("config:"):
            st.error(
                "Sign-in setup problem: "
                + error[7:]
            )
        else:
            st.session_state["login_after"] = (
                time.time() + 3
            )

            st.error(
                "Invalid username or password."
            )


# ============================================================
# NAVIGATION
# ============================================================

def sidebar():
    """Render the application's two main navigation links."""

    with st.sidebar:
        st.markdown(
            """
            <div class="sidebar-brand">
                <div class="brand-mark">SW</div>
                <div>
                    <div class="sidebar-name">StackWealth</div>
                    <div class="sidebar-sub">
                        PORTFOLIO MANAGEMENT
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        home = st.session_state.get(
            "_sw_home_page"
        )

        client = st.session_state.get(
            "_sw_client_page"
        )

        if home is not None:
            st.page_link(
                home,
                label="Home",
                icon="🏠",
            )

        if client is not None:
            st.page_link(
                client,
                label="Client Details",
                icon="👥",
            )

        st.markdown("---")

        st.caption(
            "Signed in as "
            + str(
                st.session_state.get(
                    "sw_user",
                    "Admin",
                )
            )
        )

        if st.button(
            "Sign out",
            icon="🚪",
            use_container_width=True,
        ):
            st.session_state.clear()
            st.rerun()


# ============================================================
# CLIENT DIRECTORY AND SEARCH
# ============================================================

def build_client_directory(positions, active_clients):
    """
    Merge all active clients with their portfolio summaries.

    Clients with zero holdings remain in the directory.
    """

    summaries = client_table(positions)

    directory = active_clients.merge(
        summaries,
        on=["client_id", "Client"],
        how="left",
    )

    directory["Invested"] = pd.to_numeric(
        directory["Invested"],
        errors="coerce",
    ).fillna(0)

    directory["Holdings"] = pd.to_numeric(
        directory["Holdings"],
        errors="coerce",
    ).fillna(0).astype(int)

    directory["Priced"] = (
        directory["Priced"]
        .fillna("0/0")
        .astype(str)
    )

    return directory


def filter_clients(directory, query):
    """Filter clients by name or client ID."""

    query = str(query or "").strip()

    if not query:
        return directory.reset_index(drop=True)

    name_match = directory["Client"].astype(
        str
    ).str.contains(
        query,
        case=False,
        regex=False,
        na=False,
    )

    id_match = directory["client_id"].astype(
        str
    ).str.contains(
        query,
        case=False,
        regex=False,
        na=False,
    )

    return directory[
        name_match | id_match
    ].reset_index(drop=True)


def render_client_directory(directory, query):
    """Render the searchable client table and open selected clients."""

    ctitle("Client directory")

    st.caption(
        "Search by client name or ID. Select a row to open the client's portfolio."
    )

    filtered = filter_clients(
        directory,
        query,
    )

    if filtered.empty:
        st.info(
            f"No active clients found for '{query.strip()}'. "
            "Try another name or client ID."
        )
        return

    fingerprint = hashlib.sha256(
        "|".join(
            filtered["client_id"].astype(str)
        ).encode()
    ).hexdigest()[:12]

    display = filtered.drop(
        columns=["client_id"],
        errors="ignore",
    )

    formatters = {
        "Invested": "₹{:,.0f}",
        "Market value": "₹{:,.0f}",
        "P&L (priced)": "₹{:+,.0f}",
        "Return %": "{:+.2f}%",
    }

    styled = display.style.format(
        formatters,
        na_rep="—",
    )

    numeric_columns = [
        column
        for column in ("P&L (priced)", "Return %")
        if column in display.columns
    ]

    if numeric_columns:
        styled = styled.map(
            tone_css,
            subset=numeric_columns,
        )

    event = st.dataframe(
        styled,
        hide_index=True,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"clients_{fingerprint}",
        height=270,
    )

    if not event.selection.rows:
        return

    selected_index = event.selection.rows[0]

    if not 0 <= selected_index < len(filtered):
        return

    client_id = filtered.iloc[
        selected_index
    ]["client_id"]

    if hasattr(client_id, "item"):
        client_id = client_id.item()

    # Confirm the client remains active before navigating.
    with db() as connection:
        valid_client = connection.execute(
            """
            SELECT id
            FROM clients
            WHERE id = %s
              AND status = 'active'
            """,
            (client_id,),
        ).fetchone()

    if not valid_client:
        st.warning(
            "This client is no longer active. "
            "Refresh the directory and try again."
        )
        return

    st.session_state["open_client"] = valid_client[0]

    target = st.session_state.get(
        "_sw_client_page"
    )

    if target is not None:
        st.switch_page(target)
    else:
        st.error(
            "Client Details is unavailable. "
            "Check pages/Client_Details.py."
        )


# ============================================================
# HOMEPAGE
# ============================================================

def home_page():
    """Render the consolidated portfolio dashboard."""

    if not authed():
        st.stop()

    sidebar()

    # Load the complete portfolio and the active-client directory.
    positions = load_positions()
    active_clients = load_active_clients()

    closes = st.session_state.get("closes")

    # --------------------------------------------------------
    # Main heading
    # --------------------------------------------------------

    st.markdown(
        """
        <div class="page-heading">
            <div class="eyebrow">PORTFOLIO OVERVIEW</div>
            <h1>StackWealth Portfolio</h1>
            <div class="page-subtitle">
                Wealth Manager Sahil Singh
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # --------------------------------------------------------
    # Search and actions
    # --------------------------------------------------------

    search_col, action_col = st.columns(
        [4, 1.25]
    )

    with search_col:
        query = st.text_input(
            "Search clients",
            placeholder="Search by client name or client ID...",
            label_visibility="collapsed",
            key="sw_client_search",
        )

    with action_col:
        if st.button(
            "Refresh market data",
            type="primary",
            use_container_width=True,
        ):
            refresh_ui()

    if st.session_state.get("refresh_msg"):
        st.caption(
            st.session_state["refresh_msg"]
        )

    # --------------------------------------------------------
    # Build consolidated metrics
    # --------------------------------------------------------

    chips(
        positions,
        closes,
    )

    portfolio = totals(positions)

    directory = build_client_directory(
        positions,
        active_clients,
    )

    basis = (
        "Value"
        if portfolio["n_priced"]
        else "Invested"
    )

    # Historical estimate based on current quantities.
    series = value_series(
        positions[
            positions["priced"].astype(bool)
        ],
        closes,
    )

    day_change = None

    if (
        series is not None
        and len(series) > 1
        and series.iloc[-2] > 0
    ):
        day_change = float(
            (
                series.iloc[-1]
                / series.iloc[-2]
                - 1
            ) * 100
        )

    if day_change is not None:
        status = (
            f"{pct(day_change)} latest historical session · "
        )
    else:
        status = ""

    if portfolio["n_priced"]:
        status += (
            "Unrealized P&L "
            + fmt_inr(
                portfolio["pnl"],
                sign=True,
            )
            + " ("
            + pct(portfolio["ret"])
            + ")"
        )
    else:
        status += (
            "No prices loaded — invested capital shown"
        )

    # --------------------------------------------------------
    # Hero portfolio value and KPIs
    # --------------------------------------------------------

    left, right = st.columns(
        [2.3, 5.2]
    )

    with left:
        st.markdown(
            hero(
                (
                    "Total portfolio value"
                    if portfolio["n_priced"]
                    else "Total invested capital"
                ),
                (
                    "Priced holdings only"
                    if portfolio["n_priced"]
                    else "All active holdings"
                ),
                fmt_inr(
                    (
                        portfolio["value"]
                        if portfolio["n_priced"]
                        else portfolio["invested"]
                    )
                ),
                (
                    tone(
                        day_change
                        if day_change is not None
                        else portfolio["pnl"]
                    )
                    if portfolio["n_priced"]
                    else TEXT
                ),
                status,
            ),
            unsafe_allow_html=True,
        )

    with right:
        first_row = [
            (
                "Invested capital",
                fmt_inr(portfolio["invested"]),
                "All holdings",
                TEXT,
            ),
            (
                "Market value",
                (
                    fmt_inr(portfolio["value"])
                    if portfolio["n_priced"]
                    else "—"
                ),
                "Priced holdings",
                TEXT,
            ),
            (
                "Unrealized P&L",
                (
                    fmt_inr(
                        portfolio["pnl"],
                        sign=True,
                    )
                    if portfolio["n_priced"]
                    else "—"
                ),
                pct(portfolio["ret"]),
                (
                    tone(portfolio["pnl"])
                    if portfolio["n_priced"]
                    else MUTED
                ),
            ),
            (
                "Active clients",
                str(len(active_clients)),
                "Including clients with no holdings",
                TEXT,
            ),
            (
                "Positions",
                str(portfolio["n"]),
                f"{portfolio['n_priced']} priced",
                TEXT,
            ),
        ]

        for column, args in zip(
            st.columns(5),
            first_row,
        ):
            column.markdown(
                card(*args),
                unsafe_allow_html=True,
            )

        # Calculate stock-level gainers and losers.
        positions_by_symbol = (
            positions[
                positions["priced"].astype(bool)
            ]
            .groupby("sym")
            .agg(
                PnL=("PnL", "sum"),
                Invested=("Invested", "sum"),
            )
        )

        gainers = positions_by_symbol[
            positions_by_symbol["PnL"] > 0
        ]

        losers = positions_by_symbol[
            positions_by_symbol["PnL"] < 0
        ]

        # Clients with a return figure.
        clients_with_returns = directory.dropna(
            subset=["Return %"]
        )

        best = (
            clients_with_returns.loc[
                clients_with_returns["Return %"].idxmax()
            ]
            if not clients_with_returns.empty
            else None
        )

        worst = (
            clients_with_returns.loc[
                clients_with_returns["Return %"].idxmin()
            ]
            if not clients_with_returns.empty
            else None
        )

        top5, hhi = concentration(
            positions,
            basis,
        )

        second_row = [
            (
                "Best client",
                best["Client"] if best is not None else "—",
                (
                    pct(best["Return %"])
                    if best is not None
                    else ""
                ),
                (
                    tone(best["Return %"])
                    if best is not None
                    else MUTED
                ),
            ),
            (
                "Weakest client",
                worst["Client"] if worst is not None else "—",
                (
                    pct(worst["Return %"])
                    if worst is not None
                    else ""
                ),
                (
                    tone(worst["Return %"])
                    if worst is not None
                    else MUTED
                ),
            ),
            (
                "Top gainer",
                (
                    short(gainers["PnL"].idxmax())
                    if not gainers.empty
                    else "—"
                ),
                (
                    fmt_inr(
                        gainers["PnL"].max(),
                        sign=True,
                    )
                    if not gainers.empty
                    else ""
                ),
                GREEN,
            ),
            (
                "Top loser",
                (
                    short(losers["PnL"].idxmin())
                    if not losers.empty
                    else "—"
                ),
                (
                    fmt_inr(
                        losers["PnL"].min(),
                        sign=True,
                    )
                    if not losers.empty
                    else ""
                ),
                RED,
            ),
            (
                "Invested (priced)",
                fmt_inr(
                    portfolio["invested_priced"]
                ),
                "P&L denominator",
                TEXT,
            ),
        ]

        st.markdown(
            "<div style='height:8px'></div>",
            unsafe_allow_html=True,
        )

        for column, args in zip(
            st.columns(5),
            second_row,
        ):
            column.markdown(
                card(*args),
                unsafe_allow_html=True,
            )

    # --------------------------------------------------------
    # Missing price warning
    # --------------------------------------------------------

    if portfolio["n_priced"] < portfolio["n"]:
        st.warning(
            f"Incomplete valuation: "
            f"{portfolio['n'] - portfolio['n_priced']} "
            "positions have no price. Value and P&L exclude them. "
            "Invested capital for priced positions: "
            f"{fmt_inr(portfolio['invested_priced'])}."
        )

    # --------------------------------------------------------
    # Allocation and benchmark panels
    # --------------------------------------------------------

    column1, column2, column3 = st.columns(
        [1.15, 1.25, 1.7]
    )

    with column1, st.container(border=True):
        ctitle("Allocation by stock")

        show(
            donut_fig(
                positions,
                "sym",
                basis,
                h=340,
            ),
            "Add stock holdings to view allocation.",
        )

    with column2, st.container(border=True):
        ctitle("Sector-to-stock treemap")

        show(
            treemap_fig(
                positions,
                340,
            ),
            "Add stock holdings to view the treemap.",
        )

    with column3, st.container(border=True):
        bench_card(
            positions,
            closes,
            "home",
            "Combined portfolio",
        )

    # --------------------------------------------------------
    # Sector, market cap and concentration
    # --------------------------------------------------------

    column1, column2, column3, column4 = st.columns(
        [1, 1, 0.8, 2]
    )

    with column1, st.container(border=True):
        ctitle("Sector allocation")

        show(
            donut_fig(
                positions,
                "Sector",
                basis,
                h=270,
            ),
            "Sector allocation is unavailable.",
        )

    with column2, st.container(border=True):
        ctitle("Market-cap allocation")

        show(
            donut_fig(
                positions,
                "Cap",
                basis,
                h=270,
            ),
            "Market-cap classification is unavailable.",
        )

        st.caption(
            "Only verified Large Cap, Mid Cap and Small Cap "
            "classifications are used. Unknowns are not guessed."
        )

    with column3, st.container(border=True):
        ctitle("Concentration")

        show(
            gauge_fig(top5),
            "Add holdings to calculate concentration.",
        )

        st.caption(
            (
                f"Top-5 share · HHI {hhi:,.0f}"
                if hhi is not None
                else "No allocation available"
            )
        )

    with column4, st.container(border=True):
        render_client_directory(
            directory,
            query,
        )

    # --------------------------------------------------------
    # Stock-level gainers and losers
    # --------------------------------------------------------

    column1, column2, column3 = st.columns(
        [1.2, 1.2, 1.6]
    )

    for column, title, subset in (
        (
            column1,
            "Top gainers",
            gainers.nlargest(5, "PnL"),
        ),
        (
            column2,
            "Top losers",
            losers.nsmallest(5, "PnL"),
        ),
    ):
        with column, st.container(border=True):
            ctitle(title)

            st.dataframe(
                subset.style.format(
                    {
                        "PnL": "₹{:+,.0f}",
                        "Invested": "₹{:,.0f}",
                    },
                    na_rep="—",
                ).map(
                    tone_css,
                    subset=["PnL"],
                ),
                use_container_width=True,
            )

    with column3, st.container(border=True):
        ctitle("Unrealized P&L by client")

        show(
            pnl_bar_fig(
                directory["Client"],
                directory["P&L (priced)"],
                260,
            ),
            "No priced client positions available.",
        )

    # --------------------------------------------------------
    # Invested vs market value
    # --------------------------------------------------------

    with st.container(border=True):
        ctitle("Invested vs current value")

        show(
            bars_fig(directory),
            "No priced positions available for comparison.",
        )

    # --------------------------------------------------------
    # Positions without prices
    # --------------------------------------------------------

    missing_positions = positions[
        ~positions["priced"].astype(bool)
    ]

    if not missing_positions.empty:
        section("Positions missing prices")

        st.dataframe(
            missing_positions[
                [
                    "Client",
                    "sym",
                    "Qty",
                    "Invested",
                ]
            ],
            hide_index=True,
            use_container_width=True,
        )

    # --------------------------------------------------------
    # Export
    # --------------------------------------------------------

    st.download_button(
        "Download portfolio report",
        build_report(
            directory,
            positions,
        ),
        "StackWealth_Portfolio_Report.xlsx",
        mime=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    )


# ============================================================
# APPLICATION ENTRY POINT
# ============================================================

def main():
    """Configure Streamlit and register the two main pages."""

    st.set_page_config(
        page_title=APP_NAME,
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.markdown(
        CSS,
        unsafe_allow_html=True,
    )

    if not authed():
        st.session_state.pop(
            "sw_auth",
            None,
        )

        login_page()
        return

    home = st.Page(
        home_page,
        title="Home",
        icon="🏠",
        default=True,
        url_path="home",
    )

    pages = [home]
    client = None

    client_file = ROOT / CLIENT_PATH

    if client_file.is_file():
        client = st.Page(
            CLIENT_PATH,
            title="Client Details",
            icon="👥",
            url_path="client-details",
        )

        pages.append(client)

    st.session_state["_sw_home_page"] = home
    st.session_state["_sw_client_page"] = client

    navigation = st.navigation(
        pages,
        position="hidden",
    )

    if client is None:
        st.warning(
            "Client Details is unavailable. Expected file: "
            "pages/Client_Details.py"
        )

    try:
        navigation.run()

    except Exception as exc:
        # Avoid exposing SQL parameters, credentials, or private records.
        log.error(
            "Page failed: %s",
            type(exc).__name__,
        )

        st.error(
            f"Unable to display this page ({type(exc).__name__}). "
            "Check your database schema and installed dependencies. "
            "Credentials and portfolio records are not logged."
        )


if __name__ == "__main__":
    main()
