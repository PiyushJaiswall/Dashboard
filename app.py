"""StackWealth Dashboard - login + home page (single file).
Run: streamlit run app.py   |  Tables used (already created by the earlier migration):
clients, current_holdings, price_cache, sw_audit_log."""
from __future__ import annotations
import hmac, html, json, logging, re, time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

log = logging.getLogger("stackwealth")
APP_NAME = "StackWealth Dashboard"
GREEN, RED, AMBER, NAVY, PANEL, MUTED = "#10b981", "#ef4444", "#f59e0b", "#0b1426", "#111d36", "#9fb0cc"
PALETTE = px.colors.qualitative.Safe + px.colors.qualitative.Pastel
# AMFI/SEBI cut-offs are rank based (top 100 = large, 101-250 = mid). These approximate INR cut-offs
# come from the July 2026 AMFI list (~Rs 1,06,300 cr and ~Rs 33,500 cr). Update every six months.
LARGE_CAP_MIN_CR, MID_CAP_MIN_CR = 106300, 33500
BENCHMARKS = {"NIFTY 50": "^NSEI", "SENSEX": "^BSESN", "NIFTY Midcap 100": "^CNXMIDCAP", "NIFTY Bank": "^NSEBANK", "NIFTY IT": "^CNXIT"}
DEFAULT_BENCH = ["NIFTY 50", "SENSEX"]
SESSION_TTL = 8 * 3600
STALE_DAYS = 4


# ----------------------------------------------------------------- pure helpers
def fmt_inr(v, dec: int = 0, sign: bool = False) -> str:
    if v is None:
        return "—"
    d = Decimal(str(v)); neg = d < 0
    ip, _, fp = f"{abs(d):.{dec}f}".partition(".")
    if len(ip) > 3:
        head, tail, parts = ip[:-3], ip[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head: parts.insert(0, head)
        ip = ",".join(parts + [tail])
    return ("-" if neg else "+" if sign and d > 0 else "") + "₹" + ip + (("." + fp) if fp else "")


def pct(v, sign: bool = True) -> str:
    return "—" if v is None else (f"{v:+.2f}%" if sign else f"{v:.2f}%")


def normalize_symbol(raw: str) -> str:
    s = (raw or "").strip().upper().replace(" ", "")
    exch = ".NS"
    if ":" in s:
        pre, s = s.split(":", 1)
        if pre not in ("NSE", "BSE"): raise ValueError("Use NSE: or BSE: prefix.")
        exch = ".NS" if pre == "NSE" else ".BO"
    for suf in (".NS", ".BO"):
        if s.endswith(suf): s, exch = s[:-3], suf
    if not re.match(r"^[A-Z0-9][A-Z0-9&\-]{0,24}$", s): raise ValueError(f"Invalid symbol '{(raw or '')[:30]}'.")
    return s + exch


def short(sym: str) -> str:
    return sym.rsplit(".", 1)[0]


def cap_bucket(market_cap_inr) -> str:
    if not market_cap_inr: return "Unclassified"
    cr = float(market_cap_inr) / 1e7
    return "Large Cap" if cr >= LARGE_CAP_MIN_CR else "Mid Cap" if cr >= MID_CAP_MIN_CR else "Small Cap"


def enrich(rows: list[dict]) -> pd.DataFrame:
    """One row per client-stock position with invested / value / pnl. Missing price => value & pnl are NaN."""
    out = []
    for r in rows:
        q, a = Decimal(str(r["quantity"])), Decimal(str(r["avg_price"]))
        inv = q * a
        p = Decimal(str(r["price"])) if r.get("price") else None
        mv = q * p if p else None
        d = r.get("diagnostics") or {}
        out.append({"client_id": r["client_id"], "Client": r["client_name"], "Symbol": short(r["symbol"]), "sym": r["symbol"],
                    "Qty": float(q), "Avg": float(a), "Price": float(p) if p else None, "Invested": float(inv),
                    "Value": float(mv) if mv is not None else None, "PnL": float(mv - inv) if mv is not None else None,
                    "price_date": r.get("price_date"), "Sector": d.get("sector") or "Unclassified",
                    "Cap": cap_bucket(d.get("market_cap"))})
    df = pd.DataFrame(out)
    df["priced"] = df["Value"].notna() if not df.empty else []
    return df


def totals(df: pd.DataFrame) -> dict:
    pr = df[df.priced]
    inv_all, inv_pr, mv = df.Invested.sum(), pr.Invested.sum(), pr.Value.sum()
    return {"invested": inv_all, "invested_priced": inv_pr, "value": mv, "pnl": mv - inv_pr,
            "ret": (mv - inv_pr) / inv_pr * 100 if inv_pr else None, "n": len(df), "n_priced": len(pr),
            "clients": df.client_id.nunique() if len(df) else 0}


def value_series(df: pd.DataFrame, closes: pd.DataFrame) -> pd.Series:
    """Current holdings valued at historical closes (hypothetical, not actual past performance)."""
    q = df.groupby("sym").Qty.sum()
    q = q[q.index.isin(closes.columns)]
    if q.empty: return pd.Series(dtype=float)
    c = closes[list(q.index)].ffill().dropna()
    return sum(c[s] * v for s, v in q.items())


def period_return(s: pd.Series, days: int):
    if s is None or len(s) < 2: return None
    start = s.asof(s.index[-1] - pd.Timedelta(days=days))
    if pd.isna(start) or not start: return None
    return float((s.iloc[-1] / start - 1) * 100)


# ----------------------------------------------------------------- database
def _dsn() -> str:
    try:
        return st.secrets["DATABASE_URL"]
    except Exception:
        raise RuntimeError("DATABASE_URL is missing in Streamlit secrets.") from None


@contextmanager
def db():
    import psycopg
    try:
        conn = psycopg.connect(_dsn(), connect_timeout=10)
    except Exception as e:
        log.error("db connect failed: %s", type(e).__name__)
        raise RuntimeError("Cannot connect to the database. Check DATABASE_URL (use the Supabase pooler string) and that the project isn't paused.") from None
    try:
        yield conn; conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


POS_SQL = """SELECT c.id AS client_id, c.name AS client_name, h.symbol, h.quantity, h.avg_price,
  p.price, p.price_date, p.retrieved_at, p.diagnostics
  FROM current_holdings h JOIN clients c ON c.id=h.client_id
  LEFT JOIN price_cache p ON p.symbol=h.symbol WHERE c.status='active' ORDER BY c.name, h.symbol"""


def load_positions() -> pd.DataFrame:
    with db() as conn:
        cur = conn.execute(POS_SQL)
        cols = [d.name for d in cur.description]
        return enrich([dict(zip(cols, r)) for r in cur.fetchall()])


# ----------------------------------------------------------------- market data (explicit refresh only)
def download_closes(tickers: list[str], period: str = "1y") -> pd.DataFrame:
    import yfinance as yf
    data = yf.download(sorted(set(tickers)), period=period, interval="1d", auto_adjust=False, progress=False, group_by="column", threads=True)
    if data is None or data.empty: return pd.DataFrame()
    c = data["Close"]
    if isinstance(c, pd.Series): c = c.to_frame(tickers[0])
    return c.dropna(axis=1, how="all")


def fetch_info(sym: str) -> dict:
    try:
        import yfinance as yf
        i = yf.Ticker(sym).info or {}
        return {"sector": i.get("sector") or i.get("industry"), "market_cap": i.get("marketCap")}
    except Exception as e:
        log.warning("info failed %s: %s", sym, type(e).__name__)
        return {}


def refresh_market_data() -> dict:
    syms = sorted(set(load_positions().sym)) if True else []
    if not syms: return {"ok": 0, "missing": []}
    closes = download_closes(syms + list(BENCHMARKS.values()))
    ok = [s for s in syms if s in closes.columns and closes[s].notna().any()]
    with db() as conn:
        for s in ok:
            col = closes[s].dropna()
            if float(col.iloc[-1]) > 0:
                conn.execute("""INSERT INTO price_cache(symbol,price,price_date,retrieved_at,source,status) VALUES (%s,%s,%s,now(),'yfinance','ok')
                  ON CONFLICT (symbol) DO UPDATE SET price=EXCLUDED.price, price_date=EXCLUDED.price_date, retrieved_at=now(), status='ok'""",
                             (s, round(float(col.iloc[-1]), 4), col.index[-1].date()))
    with ThreadPoolExecutor(max_workers=4) as ex:
        infos = dict(zip(ok, ex.map(fetch_info, ok)))
    with db() as conn:
        for s, i in infos.items():
            if i: conn.execute("UPDATE price_cache SET diagnostics = COALESCE(diagnostics,'{}'::jsonb) || %s::jsonb WHERE symbol=%s", (json.dumps(i), s))
        conn.execute("INSERT INTO sw_audit_log(actor,action,entity,details) VALUES (%s,'prices.refresh','price_cache',%s::jsonb)",
                     (st.session_state.get("sw_user", "admin"), json.dumps({"requested": len(syms), "refreshed": len(ok), "missing": [s for s in syms if s not in ok][:50]})))
    st.session_state["closes"] = closes
    return {"ok": len(ok), "missing": [s for s in syms if s not in ok]}


# ----------------------------------------------------------------- authentication
def login_ok(user: str, pw: str):
    """Returns (ok, error_kind). error_kind in {None,'config','invalid'}"""
    try:
        sec = st.secrets["auth"]; u, h = sec["username"], str(sec["password_hash"]).strip()
    except Exception:
        log.error("AUTH CONFIG ERROR: [auth] username/password_hash missing"); return False, "config"
    if not re.match(r"^\$2[abxy]\$\d{2}\$[./A-Za-z0-9]{53}$", h):
        log.error("AUTH CONFIG ERROR: password_hash is not a valid bcrypt hash"); return False, "config"
    import bcrypt
    pw_ok = bcrypt.checkpw((pw or "").encode()[:72], h.encode())
    if hmac.compare_digest((user or "").encode(), u.encode()) and pw_ok: return True, None
    log.warning("AUTH FAILURE: invalid credentials"); return False, "invalid"


def authed() -> bool:
    s = st.session_state
    return bool(s.get("sw_auth")) and time.time() - s.get("sw_login_at", 0) < SESSION_TTL


CSS = f"""<style>
.stApp {{background:{NAVY};}} [data-testid="stSidebar"] {{background:{PANEL};}}
html,body,.stApp,label,p,span,h1,h2,h3,h4 {{color:#e6edf7;}}
.stButton>button[kind="primary"],.stFormSubmitButton>button[kind="primary"] {{background:{GREEN};border-color:{GREEN};color:#04130d;font-weight:600;}}
.brand {{font-size:2rem;font-weight:700;color:#fff;text-align:center}} .brand span,.hero .app span {{color:{GREEN}}}
.tag {{text-align:center;color:{MUTED};margin:.2rem 0 1.2rem}}
.hero {{background:linear-gradient(135deg,{PANEL},#0e1a30);border:1px solid #1f2f52;border-radius:12px;padding:22px 26px;margin-bottom:14px}}
.hero .app {{font-size:1.1rem;font-weight:600;color:#fff;letter-spacing:.3px}} .hero .lbl {{color:{MUTED};margin-top:10px;font-size:.9rem}}
.hero .big {{font-size:3rem;font-weight:700;line-height:1.15}} .hero .sub {{color:{MUTED};margin-top:4px}}
.card {{background:{PANEL};border:1px solid #1f2f52;border-radius:10px;padding:14px 16px;height:100%}}
.card .k {{color:{MUTED};font-size:.8rem;text-transform:uppercase;letter-spacing:.4px}} .card .v {{font-size:1.45rem;font-weight:650;margin-top:4px}}
.card .s {{color:{MUTED};font-size:.8rem;margin-top:2px}}
.warn {{border-left:3px solid {AMBER};background:#2a2310;color:#fde68a;padding:.6rem .9rem;border-radius:4px;margin:8px 0}}
</style>"""


def card(k: str, v: str, s: str = "", color: str = "#e6edf7") -> str:
    return f"<div class='card'><div class='k'>{html.escape(k)}</div><div class='v' style='color:{color}'>{html.escape(v)}</div><div class='s'>{html.escape(s)}</div></div>"


def tone(x) -> str:
    return MUTED if x is None or x == 0 else GREEN if x > 0 else RED


def donut(df: pd.DataFrame, name: str, val: str, title: str, height: int = 380):
    d = df[df[val].fillna(0) > 0].groupby(name, as_index=False)[val].sum()
    if d.empty: st.info("Nothing to chart yet."); return
    f = px.pie(d, names=name, values=val, hole=0.5, template="plotly_dark", title=title, color_discrete_sequence=PALETTE)
    f.update_traces(textinfo="percent+label", hovertemplate="%{label}<br>₹%{value:,.0f}<br>%{percent}<extra></extra>", sort=True)
    f.update_layout(height=height, margin=dict(t=50, l=0, r=0, b=0), showlegend=False, paper_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(f, use_container_width=True)


# ----------------------------------------------------------------- pages
def login_page():
    st.markdown("<div style='height:8vh'></div>", unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown(f"<div class='brand'>Stack<span>Wealth</span> Dashboard</div><div class='tag'>Multi-client Indian equity portfolio monitoring</div>", unsafe_allow_html=True)
        with st.form("login"):
            u = st.text_input("Username"); p = st.text_input("Password", type="password")
            go_ = st.form_submit_button("Sign in", type="primary", use_container_width=True)
        if go_:
            ok, err = login_ok(u, p)
            if ok:
                st.session_state.clear(); st.session_state.update(sw_auth=True, sw_user=u, sw_login_at=time.time()); st.rerun()
            elif err == "config": st.error("Sign-in is not configured correctly. Check the [auth] section of your Streamlit secrets.")
            else: st.error("Invalid username or password.")


def home_page():
    with st.sidebar:
        st.markdown(f"<div class='brand' style='font-size:1.2rem;text-align:left'>Stack<span>Wealth</span> Dashboard</div>", unsafe_allow_html=True)
        present = st.toggle("Presentation mode", help="Hides admin controls.")
        if st.button("Sign out", use_container_width=True): st.session_state.clear(); st.rerun()
    df = load_positions()
    closes = st.session_state.get("closes")

    c1, c2 = st.columns([4, 1])
    if not present:
        with c2:
            st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
            if st.button("Refresh market data", type="primary", use_container_width=True,
                         help="Prices, sectors, market caps and 1-year history from Yahoo Finance (delayed / end-of-day)."):
                with st.spinner("Fetching market data…"):
                    try:
                        r = refresh_market_data()
                        st.session_state["refresh_msg"] = f"{r['ok']} symbols updated" + (f"; no data for: {', '.join(short(s) for s in r['missing'])}" if r["missing"] else "")
                    except Exception:
                        log.exception("refresh failed"); st.session_state["refresh_msg"] = "Refresh failed (provider unavailable or rate-limited). Existing prices were kept."
                st.rerun()
    if st.session_state.get("refresh_msg"): st.caption(st.session_state["refresh_msg"])

    if df.empty:
        st.markdown(f"<div class='hero'><div class='app'>Stack<span>Wealth</span> Dashboard</div></div>", unsafe_allow_html=True)
        st.info("No holdings yet. Add clients and holdings on the Client Details page."); return

    t = totals(df)
    series = value_series(df[df.priced], closes) if closes is not None and t["n_priced"] else pd.Series(dtype=float)
    day = float((series.iloc[-1] / series.iloc[-2] - 1) * 100) if len(series) > 1 and series.iloc[-2] else None
    day_abs = float(series.iloc[-1] - series.iloc[-2]) if day is not None else None
    colr = tone(day if day is not None else (t["pnl"] if t["n_priced"] else None))
    big = fmt_inr(t["value"]) if t["n_priced"] else fmt_inr(t["invested"])
    arrow = "" if day is None else ("▲ " if day >= 0 else "▼ ")
    sub = (f"{arrow}{pct(day)} ({fmt_inr(day_abs, sign=True)}) last session &nbsp;·&nbsp; " if day is not None else "") + \
          (f"Unrealized P&L {fmt_inr(t['pnl'], sign=True)} ({pct(t['ret'])})" if t["n_priced"] else "Prices not loaded - showing invested capital")
    st.markdown(f"<div class='hero'><div class='app'>Stack<span>Wealth</span> Dashboard</div>"
                f"<div class='lbl'>{'Total portfolio value' if t['n_priced'] else 'Total invested capital'}</div>"
                f"<div class='big' style='color:{colr if t['n_priced'] else '#e6edf7'}'>{big}</div><div class='sub'>{sub}</div></div>", unsafe_allow_html=True)
    if t["n_priced"] < t["n"]:
        st.markdown(f"<div class='warn'>{t['n'] - t['n_priced']} of {t['n']} positions have no price and are excluded from value and P&L "
                    f"(priced coverage: {t['invested_priced'] / t['invested'] * 100:.0f}% of invested capital). Click Refresh market data.</div>", unsafe_allow_html=True)

    k = st.columns(5)
    cards = [("Invested capital", fmt_inr(t["invested"]), "all holdings", "#e6edf7"),
             ("Market value", fmt_inr(t["value"]) if t["n_priced"] else "—", "priced holdings", "#e6edf7"),
             ("Unrealized P&L", fmt_inr(t["pnl"], sign=True) if t["n_priced"] else "—", pct(t["ret"]) + " on priced capital", tone(t["pnl"] if t["n_priced"] else None)),
             ("Active clients", str(t["clients"]), "", "#e6edf7"),
             ("Positions", str(t["n"]), f"{t['n_priced']} priced", "#e6edf7")]
    for col, c in zip(k, cards): col.markdown(card(*c), unsafe_allow_html=True)

    basis = "Value" if t["n_priced"] else "Invested"
    lab = "market value" if basis == "Value" else "invested capital"
    st.subheader("Where client money is invested")
    a, b = st.columns([3, 2])
    with a: donut(df, "Symbol", basis, f"Allocation by stock ({lab})", 460)
    with b:
        top = df.groupby("Symbol", as_index=False)[basis].sum().nlargest(8, basis)
        top["Share %"] = (top[basis] / df[basis].fillna(0).sum() * 100).round(2)
        st.markdown("**Top holdings**")
        st.dataframe(top.rename(columns={basis: lab.title()}), hide_index=True, use_container_width=True)
    s1, s2 = st.columns(2)
    with s1: donut(df, "Sector", basis, f"Allocation by sector ({lab})")
    with s2: donut(df, "Cap", basis, f"Allocation by market cap ({lab})")
    if (df.Sector == "Unclassified").any():
        st.caption("Sector / market-cap data comes from Yahoo Finance when you click Refresh market data; 'Unclassified' means it is not available yet. "
                   f"Cap buckets use approximate AMFI cut-offs (large ≥ ₹{LARGE_CAP_MIN_CR:,} cr, mid ≥ ₹{MID_CAP_MIN_CR:,} cr).")

    st.subheader("Performance vs benchmarks")
    pick = st.multiselect("Benchmarks", list(BENCHMARKS), DEFAULT_BENCH)
    if series.empty:
        st.info("Click Refresh market data to load 1-year history for the combined portfolio and the indices.")
    else:
        lines = {"All clients (combined)": series}
        for n in pick:
            tk = BENCHMARKS[n]
            if tk in closes.columns and closes[tk].notna().any(): lines[n] = closes[tk].dropna()
            else: st.caption(f"No data returned for {n}.")
        frame = pd.concat(lines, axis=1).ffill().dropna()
        reb = frame / frame.iloc[0] * 100
        f = go.Figure()
        for i, col in enumerate(reb.columns):
            f.add_trace(go.Scatter(x=reb.index, y=reb[col], name=col, line=dict(width=3 if i == 0 else 1.8, color=GREEN if i == 0 else PALETTE[i + 2])))
        f.update_layout(template="plotly_dark", height=380, yaxis_title="Rebased to 100", margin=dict(t=10, l=0, r=0, b=0), legend=dict(orientation="h"), paper_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(f, use_container_width=True)
        rows = [{"Series": n, **{lbl: period_return(s, d) for lbl, d in (("1M", 30), ("3M", 91), ("6M", 182), ("1Y", 365))}} for n, s in lines.items()]
        tbl = pd.DataFrame(rows).set_index("Series")
        st.dataframe(tbl.style.format("{:+.2f}%", na_rep="—").map(lambda v: f"color:{GREEN}" if v and v > 0 else f"color:{RED}" if v and v < 0 else ""), use_container_width=True)
        st.caption("The combined line values TODAY's holdings at past closing prices (hypothetical; ignores past trades, additions and dividends). "
                   "Benchmarks are price indices. Data: Yahoo Finance, delayed / end-of-day. This is not a record of actual client returns.")

    st.subheader("Clients")
    g = df.groupby("Client").apply(lambda x: pd.Series({"Invested": x.Invested.sum(), "Market value": x.Value.sum(min_count=1),
        "P&L (priced)": x.PnL.sum(min_count=1), "Return %": (x.PnL.sum() / x[x.priced].Invested.sum() * 100) if x.priced.any() and x[x.priced].Invested.sum() else None,
        "Holdings": len(x), "Priced": f"{int(x.priced.sum())}/{len(x)}"}), include_groups=False).reset_index()
    q = st.text_input("Search clients")
    if q: g = g[g.Client.str.contains(q, case=False, regex=False)]
    st.dataframe(g.style.format({"Invested": "₹{:,.0f}", "Market value": "₹{:,.0f}", "P&L (priced)": "₹{:+,.0f}", "Return %": "{:+.2f}%"}, na_rep="—"),
                 hide_index=True, use_container_width=True)
    cc = g.dropna(subset=["P&L (priced)"])
    if not cc.empty:
        f = go.Figure(go.Bar(x=cc.Client, y=cc["P&L (priced)"], marker_color=[GREEN if v >= 0 else RED for v in cc["P&L (priced)"]]))
        f.update_layout(template="plotly_dark", title="Unrealized P&L by client", height=320, margin=dict(t=40, l=0, r=0, b=0), paper_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(f, use_container_width=True)
    if not present:
        with db() as conn:
            cl = conn.execute("SELECT id, name FROM clients WHERE status='active' ORDER BY name").fetchall()
        sel = st.selectbox("Open a client", cl, format_func=lambda r: r[1])
        if sel: st.session_state["open_client"] = sel[0]
        st.page_link("pages/Client_Details.py", label="Open Client Details →")


def main():
    st.set_page_config(page_title=APP_NAME, page_icon="📈", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    if not authed():
        st.session_state.pop("sw_auth", None); return login_page()
    try:
        home_page()
    except RuntimeError as e:
        st.error(str(e))
    except Exception:
        log.exception("home page error"); st.error("Something went wrong. See the app logs (Manage app > Logs) for details.")


if __name__ == "__main__":
    main()
