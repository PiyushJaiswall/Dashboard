"""StackWealth Dashboard: authentication, home dashboard and page registration.
Run with: streamlit run app.py
Requires the existing clients, current_holdings, price_cache and sw_audit_log tables.
"""
from __future__ import annotations

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

log = logging.getLogger("stackwealth")
APP_NAME = "StackWealth Dashboard"
GREEN, RED, AMBER = "#10b981", "#ef4444", "#f59e0b"
NAVY, PANEL, MUTED, TEXT = "#070f1f", "#0e1a30", "#9fb0cc", "#e6edf7"
PALETTE = ["#22c55e", "#3b82f6", "#f59e0b", "#a855f7", "#06b6d4", "#ef4444", "#eab308", "#ec4899", "#14b8a6", "#64748b"]
BENCHMARKS = {"NIFTY 50": "^NSEI", "SENSEX": "^BSESN", "NIFTY Midcap 100": "^CNXMIDCAP", "NIFTY Bank": "^NSEBANK", "NIFTY IT": "^CNXIT"}
DEFAULT_BENCH = ["NIFTY 50", "SENSEX"]
PERIODS = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365}
SESSION_TTL, STALE_DAYS = 8 * 3600, 4
CLIENT_PATH = "pages/Client_Details.py"
ROOT = Path(__file__).resolve().parent


def number(v):
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except Exception:
        return None


def fmt_inr(v, dec=0, sign=False):
    d = number(v)
    if d is None:
        return "—"
    ip, _, fp = f"{abs(d):.{dec}f}".partition(".")
    if len(ip) > 3:
        head, tail, parts = ip[:-3], ip[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        ip = ",".join(parts + [tail])
    return ("-" if d < 0 else "+" if sign and d > 0 else "") + "₹" + ip + (("." + fp) if fp else "")


def fmt_cr(v):
    d = number(v)
    if d is None:
        return "—"
    a = abs(float(d))
    s = f"₹{a / 1e7:,.2f} Cr" if a >= 1e7 else f"₹{a / 1e5:,.2f} L" if a >= 1e5 else f"₹{a:,.0f}"
    return ("-" if d < 0 else "") + s


def pct(v, sign=True):
    d = number(v)
    return "—" if d is None else (f"{d:+.2f}%" if sign else f"{d:.2f}%")


def tone(x):
    d = number(x)
    return MUTED if d is None or d == 0 else GREEN if d > 0 else RED


def tone_css(v):
    d = number(v)
    return "" if d is None or d == 0 else f"color:{tone(d)};font-weight:600"


def normalize_symbol(raw):
    s = str(raw or "").strip().upper()
    suffix = ".NS"
    if ":" in s:
        exchange, s = s.split(":", 1)
        if exchange not in ("NSE", "BSE"):
            raise ValueError("Use NSE: or BSE: prefixes.")
        suffix = ".NS" if exchange == "NSE" else ".BO"
    for ext in (".NS", ".BO"):
        if s.endswith(ext):
            s, suffix = s[:-3], ext
            break
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9&\-]{0,24}", s):
        raise ValueError("Invalid stock symbol.")
    return s + suffix


def short(sym):
    return str(sym).rsplit(".", 1)[0]


def cap_bucket(market_cap_inr=None):
    # Market cap amount alone does not establish an AMFI rank classification.
    return "Unclassified"


def enrich(rows):
    cols = ["client_id", "Client", "Symbol", "sym", "Qty", "Avg", "Price", "Invested", "Value", "PnL", "Ret", "price_date", "Sector", "Cap", "priced", "source", "retrieved_at"]
    out = []
    for r in rows:
        q, a = number(r["quantity"]), number(r["avg_price"])
        if q is None or q <= 0 or a is None or a < 0:
            raise ValueError("Invalid holding in database: quantity must be positive and average price nonnegative.")
        inv = q * a
        p = number(r.get("price"))
        p = p if p is not None and p > 0 else None
        mv = q * p if p is not None else None
        pnl = mv - inv if mv is not None else None
        d = r.get("diagnostics") or {}
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except ValueError:
                d = {}
        cap = d.get("cap_category", "Unclassified")
        if cap not in ("Large Cap", "Mid Cap", "Small Cap"):
            cap = "Unclassified"
        out.append(dict(client_id=r["client_id"], Client=r["client_name"], Symbol=short(r["symbol"]), sym=r["symbol"], Qty=float(q), Avg=float(a), Price=float(p) if p is not None else None, Invested=float(inv), Value=float(mv) if mv is not None else None, PnL=float(pnl) if pnl is not None else None, Ret=float(pnl / inv * 100) if pnl is not None and inv > 0 else None, price_date=r.get("price_date"), Sector=d.get("sector") or "Unclassified", Cap=cap, priced=mv is not None, source=r.get("source"), retrieved_at=r.get("retrieved_at")))
    df = pd.DataFrame(out, columns=cols)
    df["priced"] = df["priced"].astype(bool)
    for col in ("Qty", "Avg", "Price", "Invested", "Value", "PnL", "Ret"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def totals(df):
    pr = df[df.priced.astype(bool)]
    inv, inv_pr, mv = float(df.Invested.sum()), float(pr.Invested.sum()), float(pr.Value.sum())
    return dict(invested=inv, invested_priced=inv_pr, value=mv, pnl=mv - inv_pr, ret=(mv - inv_pr) / inv_pr * 100 if inv_pr > 0 else None, n=len(df), n_priced=len(pr), clients=int(df.client_id.nunique()))


def client_table(df):
    rows = []
    for (cid, name), x in df.groupby(["client_id", "Client"]):
        t = totals(x)
        rows.append({"client_id": cid, "Client": name, "Invested": t["invested"], "Market value": t["value"] if t["n_priced"] else None, "P&L (priced)": t["pnl"] if t["n_priced"] else None, "Return %": t["ret"], "Holdings": t["n"], "Priced": f"{t['n_priced']}/{t['n']}"})
    return pd.DataFrame(rows, columns=["client_id", "Client", "Invested", "Market value", "P&L (priced)", "Return %", "Holdings", "Priced"])


def concentration(df, basis):
    s = df.groupby("sym")[basis].sum()
    s = s[s > 0]
    if s.empty:
        return None, None
    weights = s / s.sum()
    return float(weights.nlargest(5).sum() * 100), float((weights ** 2).sum() * 10000)


def value_series(df, closes):
    if closes is None or closes.empty or df.empty:
        return pd.Series(dtype=float)
    qty = df.groupby("sym").Qty.sum()
    qty = qty[qty.index.isin(closes.columns)]
    if qty.empty:
        return pd.Series(dtype=float)
    c = closes[list(qty.index)].ffill().dropna()
    return c.mul(qty, axis=1).sum(axis=1)


def period_return(s, days):
    if s is None:
        return None
    s = s.dropna().sort_index()
    if len(s) < 2:
        return None
    target = s.index[-1] - pd.Timedelta(days=days)
    if target < s.index[0]:
        if (s.index[0] - target).days > 7:
            return None
        start = s.iloc[0]
    else:
        start = s.asof(target)
    return float((s.iloc[-1] / start - 1) * 100) if pd.notna(start) and start > 0 else None


def freshness(df):
    dates = [pd.Timestamp(x).date() for x in df.price_date if pd.notna(x)]
    return dict(latest=max(dates) if dates else None, stale=sum((date.today() - d).days > STALE_DAYS for d in dates), missing=int((~df.priced.astype(bool)).sum()))


# Database: parameters only, short-lived connections, rollback on exceptions.
def _dsn():
    try:
        url = st.secrets["DATABASE_URL"]
    except Exception:
        raise RuntimeError("DATABASE_URL is missing from Streamlit secrets.") from None
    return str(url)


@contextmanager
def db():
    import psycopg
    try:
        conn = psycopg.connect(_dsn(), connect_timeout=10, prepare_threshold=None)
    except Exception as exc:
        log.error("Database connection failed: %s", type(exc).__name__)
        raise RuntimeError("Cannot connect to Supabase. Check DATABASE_URL, pooler settings and project availability.") from None
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


POS_SQL = """SELECT c.id AS client_id, c.name AS client_name, h.symbol, h.quantity, h.avg_price,
 p.price, p.price_date, p.retrieved_at, p.diagnostics, p.source
 FROM current_holdings h JOIN clients c ON c.id=h.client_id
 LEFT JOIN price_cache p ON p.symbol=h.symbol WHERE c.status='active' ORDER BY c.name, h.symbol"""


def load_positions():
    with db() as conn:
        cur = conn.execute(POS_SQL)
        cols = [d.name for d in cur.description]
        return enrich([dict(zip(cols, row)) for row in cur.fetchall()])


# Authentication
def _secret(*names):
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


def login_ok(user, pw):
    u = _secret("username", "ADMIN_USERNAME")
    h = _secret("password_hash", "ADMIN_PASSWORD_HASH")
    if not u or not h:
        log.error("AUTH CONFIG: missing administrator username or password_hash")
        return False, "config:Administrator username or password_hash is missing."
    h = h.strip()
    if not re.fullmatch(r"\$2[aby]\$(0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53}", h):
        log.error("AUTH CONFIG: malformed bcrypt hash")
        return False, "config:password_hash must be a valid, 60-character bcrypt hash."
    import bcrypt
    raw = (pw or "").encode()
    if len(raw) > 72:
        return False, "invalid"
    try:
        valid = bcrypt.checkpw(raw, h.encode())
    except ValueError:
        log.error("AUTH CONFIG: bcrypt rejected password_hash")
        return False, "config:Configured bcrypt hash was rejected. Generate a new hash."
    if hmac.compare_digest((user or "").encode(), u.encode()) and valid:
        return True, None
    log.warning("AUTH FAILURE: invalid credentials")
    return False, "invalid"


def authed():
    s = st.session_state
    return bool(s.get("sw_auth")) and 0 <= time.time() - s.get("sw_login_at", 0) < SESSION_TTL


# Explicit market refresh only.
def download_closes(tickers, period="1y"):
    import yfinance as yf
    names = sorted(set(tickers))
    if not names:
        return pd.DataFrame()
    raw = yf.download(names, period=period, interval="1d", auto_adjust=False, progress=False, threads=4)
    if raw is None or raw.empty:
        return pd.DataFrame()
    close = raw["Close"]
    if isinstance(close, pd.Series):
        close = close.to_frame(names[0])
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close.apply(pd.to_numeric, errors="coerce").where(lambda x: x > 0).dropna(axis=1, how="all").sort_index()


def fetch_info(sym):
    try:
        import yfinance as yf
        info = yf.Ticker(sym).info or {}
        return {k: v for k, v in {"sector": info.get("sector"), "market_cap": info.get("marketCap")}.items() if v}
    except Exception as exc:
        log.warning("Metadata failed for %s: %s", sym, type(exc).__name__)
        return {}


def refresh_market_data():
    """
    Refresh prices and metadata for stocks held by active clients.

    - Runs only for an authenticated administrator.
    - Updates only valid, positive prices.
    - Keeps previously cached prices when downloads fail.
    - Saves successful prices and the audit entry in one transaction.
    - Updates session history only after the database transaction succeeds.
    """
    if not authed():
        raise PermissionError("Authentication required.")

    df = load_positions()
    syms = sorted(set(df["sym"].dropna().astype(str)))

    if not syms:
        return {"ok": 0, "missing": []}

    # Includes index history for the benchmark comparison.
    closes = download_closes(
        syms + list(BENCHMARKS.values())
    )

    quotes = {}
    missing = []

    for sym in syms:
        if closes is None or closes.empty or sym not in closes.columns:
            missing.append(sym)
            continue

        # Reject NaN, infinity, zero and negative prices.
        prices = pd.to_numeric(
            closes[sym],
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
            missing.append(sym)
            continue

        price_date = pd.Timestamp(prices.index[-1])

        if pd.isna(price_date):
            missing.append(sym)
            continue

        quotes[sym] = {
            "price": Decimal(str(prices.iloc[-1])),
            "price_date": price_date.date(),
        }

    valid = sorted(quotes)

    # Metadata failure must not prevent a valid price from being saved.
    def safe_fetch_info(sym):
        try:
            result = fetch_info(sym)

            if not isinstance(result, dict):
                return {}

            # Only retain relevant, nonempty metadata.
            metadata = {}

            sector = result.get("sector")
            if isinstance(sector, str) and sector.strip():
                metadata["sector"] = sector.strip()

            market_cap = result.get("market_cap")
            if market_cap is not None:
                market_cap = float(market_cap)

                if math.isfinite(market_cap) and market_cap > 0:
                    metadata["market_cap"] = market_cap

            return metadata

        except Exception as exc:
            log.warning(
                "Metadata refresh failed for %s: %s",
                sym,
                type(exc).__name__,
            )
            return {}

    infos = {}

    if valid:
        with ThreadPoolExecutor(
            max_workers=min(4, len(valid))
        ) as executor:
            infos = dict(
                zip(
                    valid,
                    executor.map(safe_fetch_info, valid),
                )
            )

    # db() commits on success and rolls back on any exception.
    with db() as conn:
        for sym in valid:
            quote = quotes[sym]

            conn.execute(
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
                    sym,
                    quote["price"],
                    quote["price_date"],
                    json.dumps(
                        infos.get(sym, {}),
                        allow_nan=False,
                    ),
                ),
            )

        # Your existing audit table uses these column names.
        conn.execute(
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
                        "requested": len(syms),
                        "refreshed": len(valid),
                        "missing": missing,
                    },
                    allow_nan=False,
                ),
            ),
        )

    # Do not erase existing session history after an empty response.
    if closes is not None and not closes.empty:
        st.session_state["closes"] = closes

    return {
        "ok": len(valid),
        "missing": missing,
    }


def refresh_ui():
    with st.spinner("Refreshing prices and historical data…"):
        try:
            result = refresh_market_data()
            msg = f"{result['ok']} symbols updated."
            if result["missing"]:
                msg += " No data: " + ", ".join(result["missing"]) + ". Previous prices retained."
            st.session_state["refresh_msg"] = msg
        except Exception as exc:
            log.error("Refresh failed: %s", type(exc).__name__)
            st.session_state["refresh_msg"] = "Refresh failed. Previous prices retained. Check provider availability and database logs."
    st.rerun()


# Visual components; all HTML values derived from client records are escaped.
CSS = f"""<style>
.stApp {{background:radial-gradient(1100px 600px at 10% -10%,#0c2342,{NAVY} 60%);color:{TEXT};}}
[data-testid="stSidebar"] {{background:#081224;border-right:1px solid #1b2c52;}}
[data-testid="stHeader"] {{background:transparent;}}
.block-container {{padding-top:1.4rem;max-width:1700px;}}
.brand {{color:#fff;font-size:1.9rem;font-weight:750;line-height:1.3;}}
.brand span {{color:{GREEN};}} .brand em {{font-style:normal;color:{MUTED};font-weight:400;}}
.tag {{color:{MUTED};margin:8px 0 20px;}}
.hero {{background:linear-gradient(135deg,#0d3b33,#0c2a3a 65%,#0e1a30);border:1px solid #10b98188;border-radius:14px;padding:22px;min-height:165px;}}
.hero .app,.hero .lbl {{color:#b5d7d0;font-size:.85rem;}}
.hero .big {{font-size:clamp(1.7rem,3vw,3.1rem);font-weight:800;letter-spacing:-1px;margin:8px 0;overflow-wrap:anywhere;}}
.hero .sub {{color:#adc9c6;font-size:.82rem;}}
.card {{background:linear-gradient(150deg,#101e37,#0b1528);border:1px solid #253450;border-radius:11px;padding:12px;min-height:92px;}}
.card .k {{color:{MUTED};font-size:.75rem;}}
.card .v {{font-size:clamp(.95rem,1.3vw,1.3rem);font-weight:700;margin:4px 0;overflow-wrap:anywhere;}}
.card .s {{color:{MUTED};font-size:.73rem;}}
.ctitle {{color:{TEXT};font-size:1rem;font-weight:650;margin:2px 0 8px;}}
.chip {{display:inline-block;border:1px solid #253450;border-radius:20px;padding:3px 9px;font-size:.74rem;color:{MUTED};margin:4px 5px 8px 0;}}
.warn {{background:#2a2310;color:#fde68a;border-left:3px solid {AMBER};padding:9px 12px;margin:10px 0;border-radius:5px;}}
.sec {{font-size:1.1rem;color:{TEXT};border-left:3px solid {GREEN};padding-left:10px;margin:16px 0 8px;}}
</style>"""


def card(k, v, s="", color=TEXT):
    return f"<div class='card'><div class='k'>{html.escape(str(k))}</div><div class='v' style='color:{color}'>{html.escape(str(v))}</div><div class='s'>{html.escape(str(s))}</div></div>"


def hero(title_small, label, big, color, sub):
    return f"<div class='hero'><div class='app'>{html.escape(str(title_small))}</div><div class='lbl'>{html.escape(str(label))}</div><div class='big' style='color:{color}'>{html.escape(str(big))}</div><div class='sub'>{html.escape(str(sub))}</div></div>"


def ctitle(t):
    st.markdown(f"<div class='ctitle'>{html.escape(str(t))}</div>", unsafe_allow_html=True)


def section(t):
    st.markdown(f"<div class='sec'>{html.escape(str(t))}</div>", unsafe_allow_html=True)


def chips(df, closes):
    f = freshness(df)
    labels = [f"Price date: {f['latest']}" if f["latest"] else "No cached prices", f"{f['stale']} stale positions", f"{f['missing']} missing prices", "History loaded" if closes is not None else "History not loaded", "Yahoo Finance · delayed/end-of-day"]
    if "source" in df and df.source.fillna("").str.contains("demo", case=False).any():
        st.warning("DEMO PRICES PRESENT — illustrative values, not actual market performance.")
    st.markdown("".join(f"<span class='chip'>{html.escape(x)}</span>" for x in labels), unsafe_allow_html=True)


def _layout(fig, height=300, title=None):
    fig.update_layout(template="plotly_dark", height=height, title=title, margin=dict(t=42 if title else 20, b=15, l=5, r=5), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(color=TEXT), hovermode="closest")
    return fig


def show(fig, empty="Nothing to chart yet."):
    if fig is None:
        st.caption(empty)
    else:
        st.plotly_chart(fig, use_container_width=True)


def donut_fig(df, name, val, title=None, h=300, legend=True):
    d = df[df[val].fillna(0) > 0].groupby(name, as_index=False)[val].sum().sort_values(val, ascending=False)
    if d.empty:
        return None
    fig = px.pie(d, names=name, values=val, hole=.64, color_discrete_sequence=PALETTE)
    fig.update_traces(textinfo="none", sort=False, marker=dict(line=dict(color=NAVY, width=2)), hovertemplate="%{label}<br>₹%{value:,.0f}<br>%{percent}<extra></extra>")
    fig.add_annotation(x=.5, y=.5, text=fmt_cr(d[val].sum()), showarrow=False, font=dict(size=13))
    fig.update_layout(showlegend=legend, legend=dict(orientation="h", y=-.05, font=dict(size=10)))
    return _layout(fig, h, title)


def treemap_fig(df, h=330, title=None):
    basis = "Value" if df.priced.any() else "Invested"
    src = df[df[basis].fillna(0) > 0]
    if src.empty:
        return None
    d = src.groupby(["Sector", "Symbol"], as_index=False).agg(Amount=(basis,"sum"), PnL=("PnL","sum"), Invested=("Invested","sum"))
    d["Ret"] = (d.PnL / d.Invested.where(d.Invested > 0) * 100).fillna(0)
    if basis == "Value":
        fig = px.treemap(d, path=[px.Constant("Portfolio"),"Sector","Symbol"], values="Amount", color="Ret", color_continuous_scale=[RED,"#253450",GREEN], range_color=[-25,25], custom_data=["PnL","Ret"])
        fig.update_traces(texttemplate="%{label}<br>%{customdata[1]:+.1f}%", hovertemplate="%{label}<br>₹%{value:,.0f}<br>P&L ₹%{customdata[0]:,.0f}<extra></extra>")
        fig.update_layout(coloraxis_showscale=False)
    else:
        fig = px.treemap(d, path=[px.Constant("Invested capital"),"Sector","Symbol"], values="Amount", color_discrete_sequence=PALETTE)
    return _layout(fig, h, title)


def gauge_fig(top5, h=240):
    if top5 is None:
        return None
    fig = go.Figure(go.Indicator(mode="gauge+number", value=top5, number=dict(suffix="%",font=dict(size=28)), gauge=dict(axis=dict(range=[0,100]),bar=dict(color=TEXT,thickness=.2),steps=[dict(range=[0,40],color="#047857"),dict(range=[40,65],color="#b45309"),dict(range=[65,100],color="#991b1b")])))
    return _layout(fig,h)


def bars_fig(ct, h=300):
    d = ct.dropna(subset=["Market value"])
    if d.empty:
        return None
    fig = go.Figure([go.Bar(name="Invested (priced)",x=d.Client,y=d["Market value"]-d["P&L (priced)"],marker_color="#64748b"),go.Bar(name="Market value",x=d.Client,y=d["Market value"],marker_color=GREEN)])
    fig.update_layout(barmode="group",legend=dict(orientation="h"))
    return _layout(fig,h)


def pnl_bar_fig(x, y, h=300):
    d = pd.DataFrame(dict(x=list(x),y=list(y))).dropna()
    if d.empty:
        return None
    fig = go.Figure(go.Bar(x=d.x,y=d.y,marker_color=[tone(v) for v in d.y],hovertemplate="%{x}<br>₹%{y:,.0f}<extra></extra>"))
    return _layout(fig,h)


def bench_fig(series, closes, picks, days, label, h=330):
    lines, missing = {label:series}, []
    for name in picks:
        symbol = BENCHMARKS[name]
        if closes is not None and symbol in closes and closes[symbol].notna().any():
            lines[name] = closes[symbol].dropna()
        else:
            missing.append(name)
    frame = pd.concat(lines,axis=1).sort_index().ffill().dropna()
    if frame.empty:
        return None, pd.DataFrame(), missing
    view = frame.loc[frame.index >= frame.index[-1]-pd.Timedelta(days=days)]
    rebased = view.div(view.iloc[0]).mul(100)
    fig = go.Figure()
    for i,col in enumerate(rebased):
        fig.add_trace(go.Scatter(x=rebased.index,y=rebased[col],name=col,line=dict(color=GREEN if i==0 else PALETTE[i],width=3 if i==0 else 1.7)))
    fig.update_layout(hovermode="x unified",legend=dict(orientation="h"),yaxis_title="Rebased to 100")
    fig = _layout(fig,h)
    fig.update_layout(hovermode="x unified")
    # Returns use the same overlapping date range for every series.
    table = pd.DataFrame([{"Series":col,**{p:period_return(frame[col],n) for p,n in PERIODS.items()}} for col in frame]).set_index("Series")
    return fig,table,missing


def bench_card(df, closes, key, label):
    ctitle("Performance comparison")
    series = value_series(df[df.priced.astype(bool)],closes)
    if series.empty:
        st.caption("Use Refresh market data to load historical prices.")
        return
    covered = set(closes.columns) if closes is not None else set()
    omitted = sorted(set(df.sym)-covered)
    if omitted:
        st.warning("History excludes: " + ", ".join(omitted))
    picks = st.multiselect("Benchmarks",list(BENCHMARKS),DEFAULT_BENCH,key=f"bm_{key}")
    period = st.radio("Period",list(PERIODS),index=3,horizontal=True,key=f"period_{key}")
    fig,table,missing = bench_fig(series,closes,picks,PERIODS[period],label)
    show(fig,"No overlapping historical dates available.")
    if not table.empty:
        st.dataframe(table.style.format("{:+.2f}%",na_rep="—").map(tone_css),use_container_width=True)
    if missing:
        st.caption("No index data: " + ", ".join(missing))
    st.caption("Hypothetical: current quantities valued at historical closes, not actual client returns. Excludes dividends and cash flows. Indices are price indices.")


def build_report(ct, df):
    def safe_cell(v):
        return "'" + v if isinstance(v,str) and v.startswith(("=","+","-","@")) else v
    buf = io.BytesIO()
    with pd.ExcelWriter(buf,engine="openpyxl") as writer:
        pd.DataFrame({"Report":[APP_NAME],"Generated UTC":[datetime.now(timezone.utc).isoformat()],"Scope":["Unrealized P&L only; missing prices excluded from valuation."]}).to_excel(writer,sheet_name="About",index=False)
        ct.drop(columns="client_id",errors="ignore").map(safe_cell).to_excel(writer,sheet_name="Clients",index=False)
        df.drop(columns=["client_id","sym","priced","retrieved_at"],errors="ignore").map(safe_cell).to_excel(writer,sheet_name="Holdings",index=False)
    return buf.getvalue()


def login_page():
    st.markdown("<div style='height:7vh'></div>",unsafe_allow_html=True)
    _,center,_ = st.columns([1,1.25,1])
    with center:
        st.markdown("<div class='brand'>Stack<span>Wealth</span> Dashboard</div><div class='tag'>Multi-client Indian equity portfolio monitoring</div>",unsafe_allow_html=True)
        with st.form("login"):
            user = st.text_input("Username")
            password = st.text_input("Password",type="password")
            submit = st.form_submit_button("Sign in",type="primary",use_container_width=True)
        if submit:
            if time.time() < st.session_state.get("login_after",0):
                st.warning("Please wait a few seconds before trying again.")
                return
            ok,error = login_ok(user,password)
            if ok:
                st.session_state.clear()
                st.session_state.update(sw_auth=True,sw_user=user,sw_login_at=time.time())
                st.rerun()
            elif (error or "").startswith("config:"):
                st.error("Sign-in setup problem: " + error[7:])
            else:
                st.session_state["login_after"] = time.time()+3
                st.error("Invalid username or password.")


def sidebar():
    with st.sidebar:
        st.markdown("<div class='brand' style='font-size:1.2rem'>Stack<span>Wealth</span></div>",unsafe_allow_html=True)
        home = st.session_state.get("_sw_home_page")
        client = st.session_state.get("_sw_client_page")
        if home is not None:
            st.page_link(home,label="Dashboard",icon="📊")
        if client is not None:
            st.page_link(client,label="Client details",icon="👥")
        present = st.toggle("Presentation mode",key="sw_presentation_mode")
        st.caption(f"Signed in as {st.session_state.get('sw_user','')}")
        if st.button("Sign out",use_container_width=True):
            st.session_state.clear()
            st.rerun()
    return present


def home_page():
    if not authed():
        st.stop()
    present = sidebar()
    df = load_positions()
    closes = st.session_state.get("closes")
    a,b,c = st.columns([3,3,1.4])
    with a:
        st.markdown("<div class='brand'>Stack<span>Wealth</span> <em>Dashboard</em></div>",unsafe_allow_html=True)
    with b:
        query = st.text_input("Search clients",placeholder="Search clients",label_visibility="collapsed")
    with c:
        if not present and st.button("Refresh market data",type="primary",use_container_width=True):
            refresh_ui()
    if st.session_state.get("refresh_msg") and not present:
        st.caption(st.session_state["refresh_msg"])
    if df.empty:
        st.info("No active-client holdings. Open Client details to add clients and holdings.")
        return
    chips(df,closes)
    t = totals(df)
    ct = client_table(df)
    basis = "Value" if t["n_priced"] else "Invested"
    series = value_series(df[df.priced],closes)
    day = float((series.iloc[-1]/series.iloc[-2]-1)*100) if len(series)>1 and series.iloc[-2]>0 else None
    status = f"{pct(day)} latest historical session · " if day is not None else ""
    status += f"Unrealized P&L {fmt_inr(t['pnl'],sign=True)} ({pct(t['ret'])})" if t["n_priced"] else "No prices loaded — invested capital shown"
    left,right = st.columns([2.3,5.2])
    with left:
        st.markdown(hero("Total portfolio value" if t["n_priced"] else "Total invested capital","Priced holdings only" if t["n_priced"] else "All active holdings",fmt_inr(t["value"] if t["n_priced"] else t["invested"]),tone(day if day is not None else t["pnl"]) if t["n_priced"] else TEXT,status),unsafe_allow_html=True)
    with right:
        with db() as conn:
            active_count = conn.execute("SELECT count(*) FROM clients WHERE status='active'").fetchone()[0]
        first = [("Invested capital",fmt_inr(t["invested"]),"All holdings",TEXT),("Market value",fmt_inr(t["value"]) if t["n_priced"] else "—","Priced holdings",TEXT),("Unrealized P&L",fmt_inr(t["pnl"],sign=True) if t["n_priced"] else "—",pct(t["ret"]),tone(t["pnl"]) if t["n_priced"] else MUTED),("Active clients",str(active_count),"Including clients with no holdings",TEXT),("Positions",str(t["n"]),f"{t['n_priced']} priced",TEXT)]
        for col,args in zip(st.columns(5),first):
            col.markdown(card(*args),unsafe_allow_html=True)
        prct = ct.dropna(subset=["Return %"])
        positions = df[df.priced].groupby("sym").agg(PnL=("PnL","sum"),Invested=("Invested","sum"))
        gains,losses = positions[positions.PnL>0],positions[positions.PnL<0]
        best = prct.loc[prct["Return %"].idxmax()] if len(prct) else None
        worst = prct.loc[prct["Return %"].idxmin()] if len(prct) else None
        top5,hhi = concentration(df,basis)
        second = [("Best client",best.Client if best is not None else "—",pct(best["Return %"]) if best is not None else "",tone(best["Return %"]) if best is not None else MUTED),("Weakest client",worst.Client if worst is not None else "—",pct(worst["Return %"]) if worst is not None else "",tone(worst["Return %"]) if worst is not None else MUTED),("Top gainer",short(gains.PnL.idxmax()) if len(gains) else "—",fmt_inr(gains.PnL.max(),sign=True) if len(gains) else "",GREEN),("Top loser",short(losses.PnL.idxmin()) if len(losses) else "—",fmt_inr(losses.PnL.min(),sign=True) if len(losses) else "",RED),("Invested (priced)",fmt_inr(t["invested_priced"]),"P&L denominator",TEXT)]
        st.markdown("<div style='height:8px'></div>",unsafe_allow_html=True)
        for col,args in zip(st.columns(5),second):
            col.markdown(card(*args),unsafe_allow_html=True)
    if t["n_priced"]<t["n"]:
        st.warning(f"Incomplete valuation: {t['n']-t['n_priced']} positions have no price. Value and P&L exclude them. Invested capital for priced positions: {fmt_inr(t['invested_priced'])}.")
    r1,r2,r3 = st.columns([1.15,1.25,1.7])
    with r1,st.container(border=True):
        ctitle("Allocation by stock")
        show(donut_fig(df,"sym",basis,h=340))
    with r2,st.container(border=True):
        ctitle("Sector-to-stock treemap")
        show(treemap_fig(df,340))
    with r3,st.container(border=True):
        bench_card(df,closes,"home","Combined portfolio")
    r1,r2,r3,r4 = st.columns([1,1,.8,2])
    with r1,st.container(border=True):
        ctitle("Sector allocation")
        show(donut_fig(df,"Sector",basis,h=270))
    with r2,st.container(border=True):
        ctitle("Market-cap allocation")
        show(donut_fig(df,"Cap",basis,h=270))
        st.caption("Requires verified cap_category metadata: Large Cap, Mid Cap or Small Cap. Unknowns are not guessed.")
    with r3,st.container(border=True):
        ctitle("Concentration")
        show(gauge_fig(top5))
        st.caption(f"Top-5 share · HHI {hhi:,.0f}" if hhi is not None else "No allocation available")
    with r4,st.container(border=True):
        ctitle("Clients — select a row to open")
        filtered = ct[ct.Client.str.contains(query,case=False,regex=False)].reset_index(drop=True) if query else ct.reset_index(drop=True)
        # Change widget identity when its row mapping changes, avoiding stale selection.
        import hashlib
        fingerprint = hashlib.sha256("|".join(map(str,filtered.client_id)).encode()).hexdigest()[:12]
        event = st.dataframe(filtered.drop(columns="client_id").style.format({"Invested":"₹{:,.0f}","Market value":"₹{:,.0f}","P&L (priced)":"₹{:+,.0f}","Return %":"{:+.2f}%"},na_rep="—").map(tone_css,subset=["P&L (priced)","Return %"]),hide_index=True,use_container_width=True,on_select="rerun",selection_mode="single-row",key=f"clients_{fingerprint}",height=270)
        if event.selection.rows:
            index = event.selection.rows[0]
            if 0 <= index < len(filtered):
                cid = filtered.iloc[index]["client_id"]
                if hasattr(cid,"item"):
                    cid = cid.item()
                st.session_state["open_client"] = cid
                target = st.session_state.get("_sw_client_page")
                if target is not None:
                    st.switch_page(target)
                else:
                    st.error("Client page is not registered. Restore pages/Client_Details.py and reboot.")
    a,b,c = st.columns([1.2,1.2,1.6])
    for col,title,subset in ((a,"Top gainers",gains.nlargest(5,"PnL")),(b,"Top losers",losses.nsmallest(5,"PnL"))):
        with col,st.container(border=True):
            ctitle(title)
            st.dataframe(subset.style.format({"PnL":"₹{:+,.0f}","Invested":"₹{:,.0f}"}).map(tone_css,subset=["PnL"]),use_container_width=True)
    with c,st.container(border=True):
        ctitle("Unrealized P&L by client")
        show(pnl_bar_fig(ct.Client,ct["P&L (priced)"],260))
    with st.container(border=True):
        ctitle("Invested vs current value")
        show(bars_fig(ct))
    if t["n_priced"]<t["n"]:
        section("Positions missing prices")
        st.dataframe(df[~df.priced][["Client","sym","Qty","Invested"]],hide_index=True,use_container_width=True)
    if not present:
        st.download_button("Download portfolio report",build_report(ct,df),"StackWealth_Dashboard_Report.xlsx",mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def main():
    st.set_page_config(page_title=APP_NAME,page_icon="📈",layout="wide")
    st.markdown(CSS,unsafe_allow_html=True)
    if not authed():
        st.session_state.pop("sw_auth",None)
        login_page()
        return
    home = st.Page(home_page,title="Dashboard",icon="📊",default=True,url_path="dashboard")
    pages = [home]
    client = None
    if (ROOT / CLIENT_PATH).is_file():
        client = st.Page(CLIENT_PATH,title="Client details",icon="👥",url_path="client-details")
        pages.append(client)
    st.session_state["_sw_home_page"] = home
    st.session_state["_sw_client_page"] = client
    selected = st.navigation(pages,position="hidden")
    if client is None:
        st.warning("Client page unavailable: expected pages/Client_Details.py beside app.py. Home remains available.")
    try:
        selected.run()
    except Exception as exc:
        # Do not print exception text/SQL parameters, which may contain private data.
        log.error("Page failed: %s",type(exc).__name__)
        st.error(f"Unable to display this page ({type(exc).__name__}). Check your schema and installed dependencies. Credentials and portfolio records are not logged.")


if __name__ == "__main__":
    main()
